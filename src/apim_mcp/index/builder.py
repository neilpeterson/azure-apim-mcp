"""The API index build pipeline (T-15). See docs/SPEC.md §7.1-7.3, §7.5.

Per configured service: list current-revision APIs, list each API's
operations, and (best-effort) export each API's OpenAPI document to pull
out parameter/schema property names for `search_text`. Spec export is
allowed to fail per-API - SOAP passthrough, GraphQL, and WebSocket APIs
cannot export - and a failure there degrades that one API to
`spec_indexed=False` rather than failing the whole build
(`docs/SPEC.md` §7.2).

`asyncio.Semaphore(index_max_concurrency)` bounds how many APIs are being
indexed (operations list + spec export) at once, so a service with a few
hundred APIs doesn't fan out unbounded and get throttled by ARM. Throttling
itself is already handled transparently by `ArmClient` (`docs/PRINCIPLES.md`
§7's retry/backoff, honouring `Retry-After`) - a 429 that eventually
succeeds never surfaces here at all; only an exhausted retry becomes a
per-API failure, and that failure is swallowed the same way an export
failure is (the API is still indexed from its operations list).

A hard build-time cap (default 600s) bounds the whole per-service build:
on timeout, whatever APIs finished indexing already are kept and the
result is marked `partial=True` rather than raising or hanging forever.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from apim_mcp.auth.context import CallContext
from apim_mcp.clients.apim import SpecFormat, fetch_spec_document
from apim_mcp.clients.arm import ArmClient
from apim_mcp.common.errors import ToolError
from apim_mcp.index.tokenize import tokenize
from apim_mcp.settings import ApimServiceConfig

_API_VERSION = "2024-05-01"
_SPEC_FORMAT: SpecFormat = "openapi_json"

# §7.2: "Cap schema property extraction at depth 3 and 200 properties per
# operation."
_MAX_SCHEMA_DEPTH = 3
_MAX_SCHEMA_PROPERTIES = 200

# §7.2: "Hard cap total build time at 10 minutes."
DEFAULT_BUILD_TIMEOUT_SECONDS = 600.0

FetchSpecFn = Callable[..., Awaitable[dict[str, Any] | ToolError]]


class OperationIndexEntry(BaseModel):
    """One indexed operation. See docs/SPEC.md §7.1."""

    service: str
    api_id: str
    api_display_name: str
    api_description: str | None
    api_path: str
    api_tags: list[str]
    operation_id: str
    operation_display_name: str
    operation_description: str | None
    method: str
    url_template: str
    parameter_names: list[str]
    schema_property_names: list[str]
    search_text: str
    # Not in §7.1's illustrative model, but required by §7.2's "set
    # specIndexed: false" on export failure - the search tool (T-16) and
    # `apim_refresh_index`'s summary both need to report it per-operation.
    spec_indexed: bool


class ServiceIndexResult(BaseModel):
    """The outcome of building the index for one service."""

    service: str
    entries: list[OperationIndexEntry]
    partial: bool
    api_count: int
    operation_count: int
    spec_failures: int
    duration_seconds: float
    error: ToolError | None = None


def _resolve_ref(document: dict[str, Any], ref: str) -> dict[str, Any] | None:
    """Resolve a local `#/a/b/c` JSON-Schema reference within `document`."""
    if not ref.startswith("#/"):
        return None
    node: Any = document
    for part in ref[2:].split("/"):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node if isinstance(node, dict) else None


def _collect_property_names(
    schema: Any,
    document: dict[str, Any],
    *,
    depth: int,
    seen_refs: frozenset[str],
    names: list[str],
    budget: list[int],
) -> None:
    if depth > _MAX_SCHEMA_DEPTH or budget[0] <= 0 or not isinstance(schema, dict):
        return

    ref = schema.get("$ref")
    if isinstance(ref, str):
        if ref in seen_refs:
            return  # cycle guard - a schema referencing itself (directly or transitively)
        resolved = _resolve_ref(document, ref)
        if resolved is not None:
            _collect_property_names(
                resolved,
                document,
                depth=depth,
                seen_refs=seen_refs | {ref},
                names=names,
                budget=budget,
            )
        return

    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, sub_schema in properties.items():
            if budget[0] <= 0:
                return
            if name not in names:
                names.append(name)
                budget[0] -= 1
            _collect_property_names(
                sub_schema,
                document,
                depth=depth + 1,
                seen_refs=seen_refs,
                names=names,
                budget=budget,
            )

    items = schema.get("items")
    if isinstance(items, dict):
        _collect_property_names(
            items, document, depth=depth + 1, seen_refs=seen_refs, names=names, budget=budget
        )

    for key in ("allOf", "oneOf", "anyOf"):
        variants = schema.get(key)
        if isinstance(variants, list):
            for variant in variants:
                _collect_property_names(
                    variant,
                    document,
                    depth=depth,
                    seen_refs=seen_refs,
                    names=names,
                    budget=budget,
                )


def _operation_schemas(operation: dict[str, Any]) -> list[dict[str, Any]]:
    """Every schema attached to `operation`'s request/response bodies -
    OpenAPI 3 (`requestBody`/`responses[].content`) and Swagger 2
    (`parameters[].schema` for `in: body`, `responses[].schema`)."""
    schemas: list[dict[str, Any]] = []

    request_body = operation.get("requestBody")
    if isinstance(request_body, dict):
        for media in (request_body.get("content") or {}).values():
            if isinstance(media, dict) and isinstance(media.get("schema"), dict):
                schemas.append(media["schema"])

    for parameter in operation.get("parameters") or []:
        if (
            isinstance(parameter, dict)
            and parameter.get("in") == "body"
            and isinstance(parameter.get("schema"), dict)
        ):
            schemas.append(parameter["schema"])

    responses = operation.get("responses")
    if isinstance(responses, dict):
        for response in responses.values():
            if not isinstance(response, dict):
                continue
            if isinstance(response.get("schema"), dict):
                schemas.append(response["schema"])
            for media in (response.get("content") or {}).values():
                if isinstance(media, dict) and isinstance(media.get("schema"), dict):
                    schemas.append(media["schema"])

    return schemas


def _schema_property_names_by_operation(
    document: dict[str, Any],
) -> dict[tuple[str, str], list[str]]:
    """Map `(METHOD, urlTemplate)` -> depth/count-capped schema property
    names, for every operation in an exported OpenAPI/Swagger document."""
    result: dict[tuple[str, str], list[str]] = {}
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return result
    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for method, operation in methods.items():
            method_upper = method.upper()
            if method_upper not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            names: list[str] = []
            budget = [_MAX_SCHEMA_PROPERTIES]
            for schema in _operation_schemas(operation):
                if budget[0] <= 0:
                    break
                _collect_property_names(
                    schema, document, depth=0, seen_refs=frozenset(), names=names, budget=budget
                )
            result[(method_upper, path)] = names
    return result


_HTTP_METHODS = frozenset({"GET", "PUT", "POST", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE"})


# §7.3: field -> repetition count in the composed `search_text`.
def _compose_search_text(
    *,
    operation_display_name: str,
    api_display_name: str,
    url_template: str,
    operation_description: str | None,
    api_description: str | None,
    parameter_names: list[str],
    schema_property_names: list[str],
    api_tags: list[str],
) -> str:
    parts: list[str] = []
    parts.extend(tokenize(operation_display_name) * 3)
    parts.extend(tokenize(api_display_name) * 2)
    parts.extend(tokenize(url_template) * 2)
    parts.extend(tokenize(operation_description))
    parts.extend(tokenize(api_description))
    for name in parameter_names:
        parts.extend(tokenize(name))
    for name in schema_property_names:
        parts.extend(tokenize(name))
    for tag in api_tags:
        parts.extend(tokenize(tag) * 2)
    return " ".join(parts)


def _build_entry(
    *,
    service: str,
    api: dict[str, Any],
    operation: dict[str, Any],
    schema_property_names: list[str],
    spec_indexed: bool,
) -> OperationIndexEntry:
    api_properties = api.get("properties") or {}
    op_properties = operation.get("properties") or {}
    method = str(op_properties.get("method") or "")
    url_template = str(op_properties.get("urlTemplate") or "")
    parameter_names: list[str] = [
        str(p.get("name"))
        for p in (op_properties.get("templateParameters") or [])
        if isinstance(p, dict) and p.get("name")
    ]
    request = op_properties.get("request") or {}
    parameter_names.extend(
        str(p.get("name"))
        for p in (request.get("queryParameters") or [])
        if isinstance(p, dict) and p.get("name")
    )
    parameter_names.extend(
        str(p.get("name"))
        for p in (request.get("headers") or [])
        if isinstance(p, dict) and p.get("name")
    )

    api_display_name = str(api_properties.get("displayName") or api.get("name") or "")
    operation_display_name = str(op_properties.get("displayName") or operation.get("name") or "")
    api_description = api_properties.get("description")
    operation_description = op_properties.get("description")
    api_tags = [t for t in (api_properties.get("tags") or []) if isinstance(t, str)]

    search_text = _compose_search_text(
        operation_display_name=operation_display_name,
        api_display_name=api_display_name,
        url_template=url_template,
        operation_description=operation_description,
        api_description=api_description,
        parameter_names=parameter_names,
        schema_property_names=schema_property_names,
        api_tags=api_tags,
    )

    return OperationIndexEntry(
        service=service,
        api_id=str(api.get("name") or ""),
        api_display_name=api_display_name,
        api_description=api_description,
        api_path=str(api_properties.get("path") or ""),
        api_tags=api_tags,
        operation_id=str(operation.get("name") or ""),
        operation_display_name=operation_display_name,
        operation_description=operation_description,
        method=method,
        url_template=url_template,
        parameter_names=parameter_names,
        schema_property_names=schema_property_names,
        search_text=search_text,
        spec_indexed=spec_indexed,
    )


async def _index_one_api(
    ctx: CallContext,
    config: ApimServiceConfig,
    api: dict[str, Any],
    *,
    semaphore: asyncio.Semaphore,
    entries: list[OperationIndexEntry],
    spec_failure_count: list[int],
    arm_transport: Any,
    blob_transport: Any,
    fetch_spec: FetchSpecFn,
) -> None:
    api_id = str(api.get("name") or "")
    async with semaphore:
        client = ArmClient(ctx, transport=arm_transport)
        ops_result = await client.list_all(
            f"{config.resource_id}/apis/{api_id}/operations", api_version=_API_VERSION
        )
        if ops_result.error is not None:
            # The API itself couldn't be listed (e.g. exhausted retries on
            # a persistent 429, or access denied to this one API) - skip
            # it. One bad API must not fail the whole service build.
            spec_failure_count[0] += 1
            return

        spec_document = await fetch_spec(
            ctx,
            f"{config.resource_id}/apis/{api_id}",
            format=_SPEC_FORMAT,
            arm_transport=arm_transport,
            blob_transport=blob_transport,
        )
        spec_indexed = not isinstance(spec_document, ToolError)
        schema_by_operation: dict[tuple[str, str], list[str]] = {}
        if isinstance(spec_document, dict):
            schema_by_operation = _schema_property_names_by_operation(spec_document)
        else:
            spec_failure_count[0] += 1

        for operation in ops_result.items:
            op_properties = operation.get("properties") or {}
            method = str(op_properties.get("method") or "").upper()
            url_template = str(op_properties.get("urlTemplate") or "")
            schema_property_names = schema_by_operation.get((method, url_template), [])
            entries.append(
                _build_entry(
                    service=config.alias,
                    api=api,
                    operation=operation,
                    schema_property_names=schema_property_names,
                    spec_indexed=spec_indexed,
                )
            )


async def build_service_index(
    ctx: CallContext,
    config: ApimServiceConfig,
    *,
    max_concurrency: int,
    build_timeout_seconds: float = DEFAULT_BUILD_TIMEOUT_SECONDS,
    fetch_spec: FetchSpecFn = fetch_spec_document,
    arm_transport: Any = None,
    blob_transport: Any = None,
    clock: Callable[[], float] = time.monotonic,
) -> ServiceIndexResult:
    """Build the operation index for one configured service.

    Never raises: an ARM failure listing the API set itself comes back as
    `ServiceIndexResult.error`; a per-API failure (operations list or spec
    export) just drops that one API's spec contribution, per §7.2.

    # OBO: the resulting index is deliberately shared across every caller
    # (docs/PRINCIPLES.md §3's one documented exception to per-oid cache
    # keys) - under OBO the mitigation is post-filtering search hits
    # against the caller's read access, not building one index per caller.
    """
    started = clock()
    client = ArmClient(ctx, transport=arm_transport)
    apis_result = await client.list_all(f"{config.resource_id}/apis", api_version=_API_VERSION)
    if apis_result.error is not None:
        return ServiceIndexResult(
            service=config.alias,
            entries=[],
            partial=False,
            api_count=0,
            operation_count=0,
            spec_failures=0,
            duration_seconds=clock() - started,
            error=apis_result.error,
        )

    apis = [a for a in apis_result.items if (a.get("properties") or {}).get("isCurrent", True)]

    entries: list[OperationIndexEntry] = []
    spec_failure_count = [0]
    semaphore = asyncio.Semaphore(max_concurrency)
    tasks = [
        asyncio.create_task(
            _index_one_api(
                ctx,
                config,
                api,
                semaphore=semaphore,
                entries=entries,
                spec_failure_count=spec_failure_count,
                arm_transport=arm_transport,
                blob_transport=blob_transport,
                fetch_spec=fetch_spec,
            )
        )
        for api in apis
    ]

    partial = False
    if tasks:
        remaining = build_timeout_seconds - (clock() - started)
        _done, pending = await asyncio.wait(tasks, timeout=max(remaining, 0))
        if pending:
            partial = True
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    return ServiceIndexResult(
        service=config.alias,
        entries=entries,
        partial=partial,
        api_count=len(apis),
        operation_count=len(entries),
        spec_failures=spec_failure_count[0],
        duration_seconds=clock() - started,
    )
