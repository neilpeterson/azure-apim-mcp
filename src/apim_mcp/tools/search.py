"""Group C tools: search over the API index (T-16). See docs/SPEC.md §6
Group C and §7.4.

Both tools are thin wrappers over `IndexManager` (`apim_mcp.index.search`):
this module only owns tool registration, parameter validation, and
response rendering - the BM25 ranking, snippet/matchedFields computation,
and refresh rate-limiting all live in `IndexManager` itself so they can be
unit-tested without going through the MCP transport.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from apim_mcp.auth.context import CallContext
from apim_mcp.common.errors import ToolError, invalid_input
from apim_mcp.common.formatting import ResponseFormat
from apim_mcp.index.search import IndexManager
from apim_mcp.server import ToolRegistration, audited_tool
from apim_mcp.settings import Settings, UnknownServiceAliasError

SearchScope = Literal["operations", "apis", "both"]

_UNKNOWN_SERVICE_HINT = "one of the aliases configured in APIM_SERVICES - see apim_list_services"


def _validate_service(settings: Settings, service: str | None) -> ToolError | None:
    if service is None:
        return None
    try:
        settings.service(service)
    except UnknownServiceAliasError:
        return invalid_input("service", _UNKNOWN_SERVICE_HINT)
    return None


def register_search_tools(
    mcp: FastMCP[Any],
    registry: list[ToolRegistration],
    settings: Settings,
    index_manager: IndexManager,
) -> None:
    """Register the Group C search tools against `mcp`."""

    @audited_tool(mcp, registry, name="apim_search_apis")
    async def apim_search_apis(
        *,
        ctx: CallContext,
        query: str,
        terms: list[str] | None = None,
        service: str | None = None,
        scope: SearchScope = "both",
        limit: int = 15,
        response_format: ResponseFormat = "markdown",
    ) -> dict[str, Any] | ToolError:
        """Searches API names, descriptions, operation names, URL paths, parameter
        names, and OpenAPI schema property names across all indexed APIM instances.

        Matching is lexical, not semantic - it will not infer that "stock levels"
        relates to "inventory". Supply likely synonyms in `terms`. For "getting
        inventory" you would pass `terms: ["inventory", "stock", "availability",
        "catalog", "items", "quantity", "sku", "warehouse"]`.

        Returns ranked hits with `matchedFields` (which indexed fields matched)
        and `snippet` (surrounding context). When nothing matched strongly, hits
        are still returned but `lowConfidence: true` is set - treat that as "no
        confident answer", not as a real match.
        """
        # OBO: this reads one index shared by every caller
        # (docs/PRINCIPLES.md §3's one exception) - `ctx` is threaded
        # through for the eventual post-filter-by-read-access mitigation,
        # not to key a per-caller index.
        error = _validate_service(settings, service)
        if error is not None:
            return error
        result = await index_manager.search(
            ctx, query=query, terms=terms, service=service, scope=scope, limit=limit
        )
        return result

    @audited_tool(mcp, registry, name="apim_refresh_index")
    async def apim_refresh_index(
        *,
        ctx: CallContext,
        service: str | None = None,
        response_format: ResponseFormat = "markdown",
    ) -> dict[str, Any] | ToolError:
        """Force a rebuild of the API search index used by `apim_search_apis`.

        Exposed as a tool (not just an internal TTL refresh) so a user who
        has just deployed or changed an API can say "refresh and search
        again" rather than waiting out the index TTL. Rate-limited to once
        per service per 60s - a call within that window returns a
        `throttled` error naming the remaining wait.
        """
        # OBO: rebuilds the one shared-across-callers index - see the note
        # on apim_search_apis above.
        error = _validate_service(settings, service)
        if error is not None:
            return error
        result = await index_manager.refresh(ctx, service=service)
        return result
