"""Group D telemetry tools. See docs/development/SPEC.md §6 Group D."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from apim_mcp.auth.context import CallContext
from apim_mcp.clients.logs import (
    LogsClient,
    ResponseCodeCategory,
    build_error_summary_query,
    build_gateway_log_query,
)
from apim_mcp.clients.metrics import MetricAggregation, MetricsClient
from apim_mcp.common.errors import ToolError
from apim_mcp.common.formatting import ResponseFormat, apply_truncation, build_list_envelope
from apim_mcp.server import ToolRegistration, audited_tool
from apim_mcp.settings import Settings
from apim_mcp.tools._common import require_workspace, resolve_service


def _list_result(
    items: list[dict[str, Any]],
    *,
    truncated: bool,
    partial: bool,
    max_bytes: int,
    narrow_param: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    envelope = {
        **(extra or {}),
        **build_list_envelope(items, total=len(items), offset=0),
    }
    envelope["truncated"] = truncated
    envelope["partial"] = partial
    if truncated:
        envelope["hint"] = (
            f"More results may be available. Narrow `{narrow_param}` or lower the result limit."
        )
    return apply_truncation(envelope, max_bytes=max_bytes, narrow_param=narrow_param)


def register_telemetry_tools(
    mcp: FastMCP[Any], registry: list[ToolRegistration], settings: Settings
) -> None:
    """Register the Group D Azure Monitor and Log Analytics tools."""

    @audited_tool(mcp, registry, name="apim_get_metrics")
    async def apim_get_metrics(
        *,
        ctx: CallContext,
        service: str,
        metric: str,
        timespan: str = "PT1H",
        interval: str = "PT5M",
        aggregation: MetricAggregation = "average",
        filter: str | None = None,  # noqa: A002 - public MCP parameter required by the spec
        response_format: ResponseFormat = "markdown",
    ) -> dict[str, Any] | ToolError:
        """Query Azure Monitor metrics for an APIM instance.

        Supported metrics are `Capacity`, `Requests`, `Duration`,
        `BackendDuration`, and `ClientDuration`. These are aggregate metrics:
        APIM `AllMetrics` diagnostic export flattens dimensions, so use
        `apim_query_gateway_logs` for API, operation, response-code, or error
        filtering. Supplying `filter` returns an actionable error. Deprecated
        `TotalRequests`, `SuccessfulRequests`, and `FailedRequests` are
        rejected. Returns metric timestamps, units, aggregate values, and
        sample counts; does not return dimensions, gateway requests, or
        response bodies.
        """
        config = resolve_service(settings, service)
        if isinstance(config, ToolError):
            return config
        result = await MetricsClient(ctx).query(
            config.resource_id,
            config.log_analytics_workspace_id,
            metric=metric,
            timespan=timespan,
            interval=interval,
            aggregation=aggregation,
            dimension_filter=filter,
        )
        if isinstance(result, ToolError):
            return result
        items = result.pop("items")
        partial = bool(result.pop("partial", False))
        source_truncated = bool(result.pop("truncated", False))
        return _list_result(
            items,
            truncated=source_truncated,
            partial=partial,
            max_bytes=settings.max_response_bytes,
            narrow_param="timespan",
            extra=result,
        )

    @audited_tool(mcp, registry, name="apim_query_gateway_logs")
    async def apim_query_gateway_logs(
        *,
        ctx: CallContext,
        service: str,
        timespan: str = "PT1H",
        api_id: str | None = None,
        operation_id: str | None = None,
        response_code_category: ResponseCodeCategory | None = None,
        min_duration_ms: int | None = None,
        correlation_id: str | None = None,
        limit: int = 50,
        include_urls: bool = False,
        response_format: ResponseFormat = "markdown",
    ) -> dict[str, Any] | ToolError:
        """Query the fixed `ApiManagementGatewayLogs` table.

        Filters are limited to API, operation, response-code category,
        minimum duration, and correlation ID. Returns request timing,
        outcome, error, correlation, and region fields. Never returns
        request/response bodies or headers. `Url` is omitted unless
        `include_urls=true`, and its query string is always removed.
        """
        config = resolve_service(settings, service)
        if isinstance(config, ToolError):
            return config
        workspace_id = require_workspace(config)
        if isinstance(workspace_id, ToolError):
            return workspace_id
        query = build_gateway_log_query(
            resource_id=config.resource_id,
            timespan=timespan,
            api_id=api_id,
            operation_id=operation_id,
            response_code_category=response_code_category,
            min_duration_ms=min_duration_ms,
            correlation_id=correlation_id,
            limit=limit,
            include_urls=include_urls,
        )
        if isinstance(query, ToolError):
            return query
        result = await LogsClient(ctx).execute(workspace_id, query)
        if isinstance(result, ToolError):
            return result
        return _list_result(
            result["items"],
            truncated=bool(result["truncated"]),
            partial=bool(result["partial"]),
            max_bytes=settings.max_response_bytes,
            narrow_param="timespan",
        )

    @audited_tool(mcp, registry, name="apim_summarize_errors")
    async def apim_summarize_errors(
        *,
        ctx: CallContext,
        service: str,
        timespan: str = "PT24H",
        top: int = 10,
        response_format: ResponseFormat = "markdown",
    ) -> dict[str, Any] | ToolError:
        """Summarize APIM gateway failures by API, last-error reason, and
        response code, including count, first/last seen, and one
        representative correlation ID. Never returns request/response
        bodies, headers, URLs, or credentials.
        """
        config = resolve_service(settings, service)
        if isinstance(config, ToolError):
            return config
        workspace_id = require_workspace(config)
        if isinstance(workspace_id, ToolError):
            return workspace_id
        query = build_error_summary_query(
            resource_id=config.resource_id,
            timespan=timespan,
            top=top,
        )
        if isinstance(query, ToolError):
            return query
        result = await LogsClient(ctx).execute(workspace_id, query)
        if isinstance(result, ToolError):
            return result
        return _list_result(
            result["items"],
            truncated=bool(result["truncated"]),
            partial=bool(result["partial"]),
            max_bytes=settings.max_response_bytes,
            narrow_param="timespan",
        )
