"""The OpenAPI/Swagger export flow (T-14). See `docs/SPEC.md` §6 Group B
`apim_get_api_spec` - "the tool with the most implementation gotchas".

Two ARM-adjacent calls, not one:

1. `GET {resourceId}/apis/{apiId}?export=true&format={f}` - an `ArmClient`
   call, bearer-token authenticated like every other read in this server.
   It does **not** return the spec. It returns a link into blob storage.
2. A plain `httpx` GET against that link, with **no `Authorization`
   header** - the SAS query string *is* the credential, and attaching a
   bearer token to the blob request fails it outright. This is
   deliberately a different, unauthenticated path from every other
   downstream call in this codebase.

The SAS is valid for five minutes, so step 1's link is never cached -
only the fetched document is, keyed by `(oid, service, api_id, format)`
per `docs/PRINCIPLES.md` §3. If the blob fetch fails (most likely an
expired SAS - the export call and the fetch are not atomic), re-export
once and retry rather than failing outright.

**Documented vs. actual response shape:** `docs/SPEC.md` §6 documents the
export response as `{"format": "...", "value": {"link": "..."}}`, but a
real APIM instance returns a bare `{"link": "..."}` (see
`tests/fixtures/api_export_echo-api.json`, recorded from a non-prod
instance). Handle both, the same way `tools/config.py`'s
`_extract_policy_xml` handles the documented-vs-actual gap for policy
export.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx
import yaml

from apim_mcp.auth.context import CallContext
from apim_mcp.clients.arm import ArmClient
from apim_mcp.common.errors import ToolError, upstream_error

SpecFormat = Literal["openapi_json", "openapi_yaml", "swagger_json"]
SpecMode = Literal["summary", "full"]

_API_VERSION = "2024-05-01"

# Maps our public `format` literal to the ARM `export=true&format=` value,
# per docs/SPEC.md §6 Group B.
_EXPORT_FORMAT_PARAM: dict[SpecFormat, str] = {
    "openapi_json": "openapi+json-link",
    "openapi_yaml": "openapi-link",
    "swagger_json": "swagger-link",
}

_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})

# One export, and (if the blob fetch fails - most likely an expired SAS)
# exactly one re-export. Never loop indefinitely on a genuinely broken API.
_MAX_EXPORT_ATTEMPTS = 2


@dataclass(frozen=True)
class SpecCacheKey:
    """Cache key for a fetched spec document. Always carries `oid`
    (docs/PRINCIPLES.md §3), even though v1's shared managed identity
    means every caller would otherwise see the same document."""

    oid: str
    service: str
    api_id: str
    format: SpecFormat


class SpecDocumentCache:
    """TTL cache of *fetched documents* only - never the SAS link itself,
    which is valid for five minutes and must always be fetched fresh.

    One instance per running server (constructed in
    `register_config_tools`), not a module-level singleton - a fresh
    server process should not inherit another process's cached specs, and
    tests must not leak state between cases.
    """

    def __init__(self, *, ttl_seconds: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: dict[SpecCacheKey, tuple[float, dict[str, Any]]] = {}

    def get(self, key: SpecCacheKey) -> dict[str, Any] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, document = entry
        if self._clock() >= expires_at:
            del self._entries[key]
            return None
        return document

    def put(self, key: SpecCacheKey, document: dict[str, Any]) -> None:
        self._entries[key] = (self._clock() + self._ttl_seconds, document)


def _extract_link(body: Any) -> str | None:
    """Handle both the documented `{"value": {"link": "..."}}` shape and
    the shape a real APIM instance actually returns, `{"link": "..."}`."""
    if not isinstance(body, dict):
        return None
    direct = body.get("link")
    if isinstance(direct, str) and direct:
        return direct
    value = body.get("value")
    if isinstance(value, dict):
        nested = value.get("link")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _parse_document(raw_text: str, *, format: SpecFormat) -> dict[str, Any] | None:  # noqa: A002
    try:
        parsed = yaml.safe_load(raw_text) if format == "openapi_yaml" else json.loads(raw_text)
    except (ValueError, yaml.YAMLError):
        return None
    return parsed if isinstance(parsed, dict) else None


def spec_summary(document: dict[str, Any]) -> dict[str, Any]:
    """`mode="summary"` per docs/SPEC.md §6 Group B: `info`, `servers`,
    security scheme *names* (never the schemes themselves - they can embed
    flow URLs but never secrets, this is purely about keeping the output
    compact), and a compact per-path listing of `method`, `operationId`,
    `summary`, and parameter names."""
    components = document.get("components")
    security_schemes = list((components or {}).get("securitySchemes") or {})
    if not security_schemes:
        # OpenAPI 2.0 (Swagger) names this top-level, not under `components`.
        security_schemes = list(document.get("securityDefinitions") or {})

    operations: list[dict[str, Any]] = []
    paths = document.get("paths")
    if isinstance(paths, dict):
        for path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for method, operation in methods.items():
                if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                    continue
                parameters = [
                    p.get("name")
                    for p in (operation.get("parameters") or [])
                    if isinstance(p, dict) and p.get("name")
                ]
                operations.append(
                    {
                        "path": path,
                        "method": method.upper(),
                        "operationId": operation.get("operationId"),
                        "summary": operation.get("summary"),
                        "parameters": parameters,
                    }
                )

    return {
        "info": document.get("info"),
        "servers": document.get("servers"),
        "securitySchemes": security_schemes,
        "paths": operations,
    }


async def _export_link(
    ctx: CallContext,
    resource_id: str,
    *,
    format: SpecFormat,  # noqa: A002 - matches docs/SPEC.md §6 param name
    arm_transport: httpx.AsyncBaseTransport | None,
) -> str | ToolError:
    arm_client = ArmClient(ctx, transport=arm_transport)
    body = await arm_client.get(
        resource_id,
        api_version=_API_VERSION,
        params={"export": "true", "format": _EXPORT_FORMAT_PARAM[format]},
    )
    if isinstance(body, ToolError):
        # Covers the SOAP-passthrough/GraphQL case: export genuinely isn't
        # possible for some API types, and ARM reports that as an error
        # response rather than a link - propagate it as-is rather than
        # inventing a different failure shape.
        return body
    link = _extract_link(body)
    if link is None:
        return upstream_error(
            log_detail=f"apim_get_api_spec: export response had no link for {resource_id}"
        )
    return link


async def _fetch_blob(
    link: str, *, transport: httpx.AsyncBaseTransport | None
) -> httpx.Response | ToolError:
    # Deliberately a bare client with no default headers: the SAS query
    # string on `link` is the credential. Sending an `Authorization`
    # header here would make the request fail, not merely be redundant.
    async with httpx.AsyncClient(transport=transport, timeout=30.0) as client:
        try:
            response = await client.get(link)
        except httpx.TransportError as exc:
            return upstream_error(
                log_detail=f"apim_get_api_spec: blob fetch transport error: {exc!r}"
            )
    if response.status_code >= 400:
        return upstream_error(
            log_detail=f"apim_get_api_spec: blob fetch returned {response.status_code}"
        )
    return response


async def fetch_spec_document(
    ctx: CallContext,
    resource_id: str,
    *,
    format: SpecFormat,  # noqa: A002 - matches docs/SPEC.md §6 param name
    arm_transport: httpx.AsyncBaseTransport | None = None,
    blob_transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any] | ToolError:
    """Run the full two-call export flow and return the parsed document.

    Never returns or caches the link itself - only `register_config_tools`
    decides whether/how long to cache the *document* this returns. On a
    blob-fetch failure (most likely an expired SAS), re-exports once and
    retries before giving up.
    """
    last_error: ToolError = upstream_error(
        log_detail=f"apim_get_api_spec: exhausted export attempts for {resource_id}"
    )
    for _attempt in range(_MAX_EXPORT_ATTEMPTS):
        link = await _export_link(ctx, resource_id, format=format, arm_transport=arm_transport)
        if isinstance(link, ToolError):
            return link

        blob = await _fetch_blob(link, transport=blob_transport)
        if isinstance(blob, ToolError):
            last_error = blob
            continue  # SAS may have expired between export and fetch - re-export.

        document = _parse_document(blob.text, format=format)
        if document is None:
            return upstream_error(
                log_detail=f"apim_get_api_spec: unparseable {format} document for {resource_id}"
            )
        return document
    return last_error
