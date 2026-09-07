"""BM25 search over the API index, plus its build/refresh lifecycle (T-16).

See docs/SPEC.md §7.4 (lexical search, no embeddings in v1) and §7.5
(refresh: eager at startup, TTL-based background rebuild, never block a
request on a rebuild).

`IndexManager` owns one `_ServiceIndex` per configured service: the list of
`OperationIndexEntry` plus the `BM25Okapi` corpus built over their tokenized
`search_text`. A service missing from `_indexes` (first build not finished
yet) makes `search()` return `index_unavailable()` for that service only -
other, already-built services still answer. A stale-but-present service is
served as-is while a background rebuild is kicked off, never awaited by the
request that noticed the staleness.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from rank_bm25 import BM25Okapi

from apim_mcp.auth.context import CallContext
from apim_mcp.common.errors import ToolError, index_unavailable
from apim_mcp.index.builder import OperationIndexEntry, ServiceIndexResult, build_service_index
from apim_mcp.index.tokenize import tokenize
from apim_mcp.settings import Settings

logger = logging.getLogger(__name__)

SearchScope = Literal["operations", "apis", "both"]

# Below this BM25 score, a hit is not a meaningfully strong match - it is
# still returned (never hide a possible answer), but flagged
# `lowConfidence: true` per docs/SPEC.md §7.4's "rank honestly" guidance.
_LOW_CONFIDENCE_SCORE_FLOOR = 0.01

# §6 Group C: "Rate-limit [apim_refresh_index] to one call per service per 60s."
_REFRESH_RATE_LIMIT_SECONDS = 60.0

_SNIPPET_RADIUS = 120

# Fields considered for `matchedFields`/`snippet`, in the same priority
# order as §7.3's weighting table (strongest signal first).
_SEARCHABLE_FIELDS: tuple[str, ...] = (
    "operation_display_name",
    "api_display_name",
    "url_template",
    "operation_description",
    "api_description",
    "parameter_names",
    "schema_property_names",
    "api_tags",
)

BuildFn = Callable[..., Awaitable[ServiceIndexResult]]


def _field_text(entry: OperationIndexEntry, field_name: str) -> str:
    value = getattr(entry, field_name)
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return str(value) if value else ""


def _matched_fields(entry: OperationIndexEntry, query_tokens: frozenset[str]) -> list[str]:
    matched = []
    for field_name in _SEARCHABLE_FIELDS:
        tokens = set(tokenize(_field_text(entry, field_name)))
        if tokens & query_tokens:
            matched.append(field_name)
    return matched


def _snippet(entry: OperationIndexEntry, query_tokens: frozenset[str]) -> str:
    """±120 chars of raw (untokenized) text around the first matched token,
    from the strongest field that actually contains one. Falls back to the
    operation name when nothing matched (still returned - see the
    low-confidence handling this backs)."""
    for field_name in _SEARCHABLE_FIELDS:
        text = _field_text(entry, field_name)
        if not text:
            continue
        lower = text.lower()
        for token in query_tokens:
            index = lower.find(token)
            if index != -1:
                start = max(0, index - _SNIPPET_RADIUS)
                end = min(len(text), index + len(token) + _SNIPPET_RADIUS)
                return text[start:end]
    return _field_text(entry, "operation_display_name")[: 2 * _SNIPPET_RADIUS]


@dataclass
class SearchHit:
    service: str
    api_id: str
    api_display_name: str
    operation_id: str
    method: str
    url_template: str
    score: float
    matched_fields: list[str]
    snippet: str


@dataclass
class _ServiceIndex:
    entries: list[OperationIndexEntry]
    bm25: BM25Okapi | None
    built_at: float
    partial: bool
    api_count: int
    operation_count: int
    spec_failures: int


@dataclass
class _ServiceLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _rank(index: _ServiceIndex, query_tokens: list[str]) -> list[tuple[float, OperationIndexEntry]]:
    if index.bm25 is None or not index.entries:
        return []
    scores = index.bm25.get_scores(query_tokens)
    return sorted(zip(scores, index.entries, strict=True), key=lambda pair: pair[0], reverse=True)


class IndexManager:
    """Owns the per-service index build/refresh lifecycle. One instance per
    running server (constructed in `create_app`), not a module-level
    singleton - see docs/PRINCIPLES.md §7."""

    def __init__(
        self,
        settings: Settings,
        *,
        build_fn: BuildFn = build_service_index,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._build_fn = build_fn
        self._clock = clock
        self._indexes: dict[str, _ServiceIndex] = {}
        self._locks: dict[str, _ServiceLock] = {
            svc.alias: _ServiceLock() for svc in settings.apim_services
        }
        self._last_refresh_at: dict[str, float] = {}

    def _service_aliases(self, service: str | None) -> list[str]:
        if service is not None:
            return [service]
        return [svc.alias for svc in self._settings.apim_services]

    async def _build_one(self, ctx: CallContext, alias: str) -> None:
        # OBO: builds the one shared-across-callers index for this service
        # (docs/PRINCIPLES.md §3's one exception) - never key this per-oid.
        lock = self._locks.setdefault(alias, _ServiceLock()).lock
        if lock.locked():
            return  # a build (eager startup, TTL refresh, or forced) is already in flight
        async with lock:
            try:
                config = self._settings.service(alias)
                result = await self._build_fn(
                    ctx, config, max_concurrency=self._settings.index_max_concurrency
                )
            except Exception:
                logger.exception("index build failed for service %s", alias)
                return
            if result.error is not None:
                logger.warning("index build for service %s failed: %s", alias, result.error.message)
                return  # keep serving whatever (possibly stale) index we already had
            bm25 = (
                BM25Okapi([tokenize(e.search_text) for e in result.entries])
                if result.entries
                else None
            )
            self._indexes[alias] = _ServiceIndex(
                entries=result.entries,
                bm25=bm25,
                built_at=self._clock(),
                partial=result.partial,
                api_count=result.api_count,
                operation_count=result.operation_count,
                spec_failures=result.spec_failures,
            )

    async def build_all(self, ctx: CallContext) -> None:
        """Eager build at startup - every configured service, in parallel.
        Never raises (`_build_one` swallows its own failures) and callers
        must not await this on the request path; see docs/SPEC.md §7.5.

        # OBO: the index is shared across all callers by design (see
        # docs/PRINCIPLES.md §3's one exception) - under OBO this becomes a
        # leak unless search hits are post-filtered against the caller's
        # read access. Do not turn this into a per-caller cache instead."""
        await asyncio.gather(
            *(self._build_one(ctx, svc.alias) for svc in self._settings.apim_services)
        )

    def _is_stale(self, alias: str) -> bool:
        state = self._indexes.get(alias)
        if state is None:
            return True
        return (self._clock() - state.built_at) >= self._settings.index_ttl_seconds

    def _kick_off_background_refresh(self, ctx: CallContext, aliases: list[str]) -> None:
        # OBO: see the note on build_all/search - shared index by design.
        for alias in aliases:
            if self._is_stale(alias):
                asyncio.create_task(self._build_one(ctx, alias))  # noqa: RUF006 - fire and forget

    async def search(
        self,
        ctx: CallContext,
        *,
        query: str,
        terms: list[str] | None,
        service: str | None,
        scope: SearchScope,
        limit: int,
    ) -> dict[str, Any] | ToolError:
        # OBO: this reads a single index shared by every caller
        # (docs/PRINCIPLES.md §3's one exception). Under OBO, hits must be
        # post-filtered against `ctx`'s read access before returning - do
        # not key the index by `ctx.oid` instead, that defeats the point.
        aliases = self._service_aliases(service)
        self._kick_off_background_refresh(ctx, aliases)

        available = [a for a in aliases if a in self._indexes]
        if not available:
            return index_unavailable()

        query_tokens = tokenize(query)
        for term in terms or []:
            query_tokens.extend(tokenize(term))
        query_token_set = frozenset(query_tokens)

        ranked: list[tuple[float, OperationIndexEntry]] = []
        for alias in available:
            ranked.extend(_rank(self._indexes[alias], query_tokens))
        ranked.sort(key=lambda pair: pair[0], reverse=True)

        if scope == "apis":
            seen_apis: set[tuple[str, str]] = set()
            deduped: list[tuple[float, OperationIndexEntry]] = []
            for score, entry in ranked:
                key = (entry.service, entry.api_id)
                if key in seen_apis:
                    continue
                seen_apis.add(key)
                deduped.append((score, entry))
            ranked = deduped

        top = ranked[:limit]
        hits = [
            SearchHit(
                service=entry.service,
                api_id=entry.api_id,
                api_display_name=entry.api_display_name,
                operation_id=entry.operation_id,
                method=entry.method,
                url_template=entry.url_template,
                score=round(float(score), 4),
                matched_fields=_matched_fields(entry, query_token_set),
                snippet=_snippet(entry, query_token_set),
            )
            for score, entry in top
        ]
        low_confidence = not hits or hits[0].score <= _LOW_CONFIDENCE_SCORE_FLOOR

        return {
            "hits": [
                {
                    "service": h.service,
                    "apiId": h.api_id,
                    "apiDisplayName": h.api_display_name,
                    "operationId": h.operation_id,
                    "method": h.method,
                    "urlTemplate": h.url_template,
                    "score": h.score,
                    "matchedFields": h.matched_fields,
                    "snippet": h.snippet,
                }
                for h in hits
            ],
            "lowConfidence": low_confidence,
            "indexBuilding": any(a not in self._indexes for a in aliases),
            "partial": any(self._indexes[a].partial for a in available),
        }

    async def refresh(self, ctx: CallContext, *, service: str | None) -> dict[str, Any] | ToolError:
        # OBO: rebuilds the one shared-across-callers index (see build_all)
        # - forcing a refresh must never fork a per-caller copy.
        aliases = self._service_aliases(service)
        now = self._clock()
        for alias in aliases:
            last = self._last_refresh_at.get(alias)
            if last is not None and (now - last) < _REFRESH_RATE_LIMIT_SECONDS:
                retry_in = int(_REFRESH_RATE_LIMIT_SECONDS - (now - last))
                return ToolError(
                    kind="throttled",
                    message=(
                        f"apim_refresh_index was already called for '{alias}' within the last "
                        f"{int(_REFRESH_RATE_LIMIT_SECONDS)}s. Retry in {retry_in}s."
                    ),
                )
        for alias in aliases:
            self._last_refresh_at[alias] = now

        started = self._clock()
        await asyncio.gather(*(self._build_one(ctx, alias) for alias in aliases))
        duration_seconds = self._clock() - started

        counts = {}
        for alias in aliases:
            state = self._indexes.get(alias)
            if state is None:
                counts[alias] = {"apiCount": 0, "operationCount": 0, "specFailures": 0}
                continue
            counts[alias] = {
                "apiCount": state.api_count,
                "operationCount": state.operation_count,
                "specFailures": state.spec_failures,
                "partial": state.partial,
            }
        return {
            "refreshed": aliases,
            "durationSeconds": round(duration_seconds, 3),
            "counts": counts,
        }
