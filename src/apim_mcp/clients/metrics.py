"""APIM platform metrics queried from the fixed ``AzureMetrics`` table.

APIM ``AllMetrics`` diagnostic settings export metric series to Log
Analytics. The server reads that fixed table through the existing
workspace-scoped Log Analytics Reader role, avoiding broad Monitoring Reader
permissions on the APIM resource.

Shared Log Analytics plumbing lives in ``_loganalytics``. What stays here is
the metric vocabulary, the two fixed query shapes, and the metric-specific
error wording.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from apim_mcp.auth.context import CallContext
from apim_mcp.auth.credentials import LOGS_SCOPE
from apim_mcp.clients._loganalytics import (
    kql_string,
    kql_timespan,
    parse_interval,
    parse_timespan,
    run_workspace_query,
)
from apim_mcp.common.errors import (
    ToolError,
    access_denied,
    invalid_input,
    throttled,
    upstream_error,
)
from apim_mcp.settings import ApimServiceConfig

MetricAggregation = Literal["average", "minimum", "maximum", "total", "count"]

SUPPORTED_METRICS = frozenset(
    {"Capacity", "Requests", "Duration", "BackendDuration", "ClientDuration"}
)
DEPRECATED_METRICS = frozenset({"TotalRequests", "SuccessfulRequests", "FailedRequests"})

logger = logging.getLogger("apim_mcp.metrics")

_DEFINITIONS_TIMESPAN = parse_timespan("P1D")
_AGGREGATION_EXPRESSION = {
    "average": "sum(Total) / sum(Count)",
    "minimum": "min(Minimum)",
    "maximum": "max(Maximum)",
    "total": "sum(Total)",
    "count": "sum(Count)",
}
_NO_WORKSPACE = ("service", "a configured service with logAnalyticsWorkspaceId for telemetry")


def validate_metric_name(metric: str) -> ToolError | None:
    if metric in DEPRECATED_METRICS:
        return invalid_input(
            "metric",
            (
                "'Requests' for aggregate counts, or apim_query_gateway_logs "
                "for response-code, API, operation, or error breakdowns"
            ),
        )
    if metric not in SUPPORTED_METRICS:
        return invalid_input("metric", ", ".join(sorted(SUPPORTED_METRICS)))
    return None


def _metric_query_error(status_code: int, retry_after: int) -> ToolError:
    if status_code == 403:
        return access_denied("APIM metrics in Log Analytics")
    if status_code == 429:
        return throttled(retry_after)
    if status_code == 400:
        return invalid_input("metric query", "'Requests' over timespan 'PT1H'")
    return upstream_error(log_detail=f"Log Analytics metrics query returned HTTP {status_code}")


class MetricsClient:
    """Per-request metrics client using ARM only to resolve the workspace ID."""

    def __init__(self, ctx: CallContext) -> None:
        self._ctx = ctx

    async def query(
        self,
        resource_id: str,
        workspace_resource_id: str | None,
        *,
        metric: str,
        timespan: str,
        interval: str,
        aggregation: MetricAggregation,
        dimension_filter: str | None = None,
    ) -> dict[str, Any] | ToolError:
        metric_error = validate_metric_name(metric)
        if metric_error is not None:
            return metric_error
        if workspace_resource_id is None:
            return invalid_input(*_NO_WORKSPACE)
        if dimension_filter is not None:
            return invalid_input(
                "filter",
                (
                    "omit this parameter for aggregate metrics; APIM AllMetrics export "
                    "flattens dimensions. Use apim_query_gateway_logs for response-code, "
                    "API, operation, or error filtering"
                ),
            )
        try:
            parsed_timespan = parse_timespan(timespan)
            parsed_interval = parse_interval(interval)
        except ValueError:
            return invalid_input(
                "timespan or interval",
                "timespan 'PT1H' or '2026-09-10T00:00:00Z/2026-09-10T01:00:00Z'; interval 'PT5M'",
            )
        declaration = (
            "declare query_parameters("
            f"resource_id:string = {kql_string(resource_id)}, "
            f"metric_name:string = {kql_string(metric)}, "
            f"interval:timespan = {kql_timespan(parsed_interval)}"
            ");"
        )
        expression = _AGGREGATION_EXPRESSION[aggregation]
        query = f"""
{declaration}
AzureMetrics
| where _ResourceId =~ resource_id
| where MetricName == metric_name
| summarize Value={expression}, SampleCount=sum(Count)
    by bin(TimeGenerated, interval), UnitName
| order by TimeGenerated asc
""".strip()

        result = await run_workspace_query(
            self._ctx,
            scope=LOGS_SCOPE,
            workspace_resource_id=workspace_resource_id,
            query=query,
            timespan=parsed_timespan,
            on_status=_metric_query_error,
        )
        if isinstance(result, ToolError):
            return result
        return {
            "source": "AzureMetrics",
            "metric": metric,
            "aggregation": aggregation,
            "timespan": timespan,
            "interval": interval,
            "filter": dimension_filter,
            "items": result.rows,
            "partial": result.partial,
            "truncated": result.partial,
        }

    async def list_definitions(
        self,
        resource_id: str,
        workspace_resource_id: str | None,
    ) -> list[str] | ToolError:
        if workspace_resource_id is None:
            return invalid_input(*_NO_WORKSPACE)
        declaration = f"declare query_parameters(resource_id:string = {kql_string(resource_id)});"
        query = f"""
{declaration}
AzureMetrics
| where _ResourceId =~ resource_id
| summarize by MetricName
| order by MetricName asc
""".strip()
        result = await run_workspace_query(
            self._ctx,
            scope=LOGS_SCOPE,
            workspace_resource_id=workspace_resource_id,
            query=query,
            timespan=_DEFINITIONS_TIMESPAN,
            on_status=_metric_query_error,
        )
        if isinstance(result, ToolError):
            return result
        names = sorted(
            str(row["MetricName"]) for row in result.rows if row.get("MetricName") is not None
        )
        logger.info("available APIM metrics for %s: %s", resource_id, names)
        return names


async def probe_metric_availability(
    ctx: CallContext,
    services: list[ApimServiceConfig],
) -> None:
    """Log recent exported metric names for each configured APIM service.

    Diagnostic export has ingestion latency, so an empty or failing probe is
    a warning, never a readiness failure (SPEC §6 Group D). Nothing is
    returned: the caller in `server.py` acts on the log, not on a value.
    """
    for service in services:
        result = await MetricsClient(ctx).list_definitions(
            service.resource_id,
            service.log_analytics_workspace_id,
        )
        if isinstance(result, ToolError):
            logger.warning(
                "metric availability probe failed for %s: %s",
                service.alias,
                result.message,
            )
        elif not result:
            logger.warning(
                "metric availability probe found no recent AzureMetrics rows for %s",
                service.alias,
            )
