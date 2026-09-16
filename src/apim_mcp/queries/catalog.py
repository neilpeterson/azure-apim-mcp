"""Metadata registry for every fixed Log Analytics query.

The catalog is intentionally code-defined rather than configuration-defined.
Allowing environment variables or tool input to select arbitrary KQL would
break the fixed-table boundary in ``docs/development/PRINCIPLES.md`` §6.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

QueryVisibility = Literal["tool", "internal"]


@dataclass(frozen=True)
class QueryDefinition:
    """Stable identity and maintenance metadata for one KQL query shape."""

    id: str
    title: str
    purpose: str
    visibility: QueryVisibility
    tools: tuple[str, ...]
    source_tables: tuple[str, ...]
    parameters: tuple[str, ...]
    result_fields: tuple[str, ...]
    implementation: str
    max_timespan: str | None = None
    max_rows: int | None = None


GATEWAY_LOG_DETAIL = QueryDefinition(
    id="gateway-log-detail",
    title="Gateway log detail",
    purpose="Return recent APIM gateway events matching bounded filters.",
    visibility="tool",
    tools=("apim_query_gateway_logs",),
    source_tables=("ApiManagementGatewayLogs", "AzureDiagnostics"),
    parameters=(
        "resource_id",
        "table_mode",
        "timespan",
        "api_id",
        "operation_id",
        "response_code_category",
        "min_duration_ms",
        "correlation_id",
        "limit",
        "include_urls",
    ),
    result_fields=(
        "TimeGenerated",
        "ApiId",
        "OperationId",
        "Method",
        "ResponseCode",
        "TotalTime",
        "BackendTime",
        "IsRequestSuccess",
        "LastErrorReason",
        "LastErrorSource",
        "LastErrorMessage",
        "CorrelationId",
        "Region",
        "Url",
    ),
    implementation="src/apim_mcp/clients/logs.py",
    max_timespan="P7D",
    max_rows=200,
)

GATEWAY_ERROR_SUMMARY = QueryDefinition(
    id="gateway-error-summary",
    title="Gateway error summary",
    purpose="Aggregate APIM gateway failures by API, error reason, and response code.",
    visibility="tool",
    tools=("apim_summarize_errors",),
    source_tables=("ApiManagementGatewayLogs", "AzureDiagnostics"),
    parameters=("resource_id", "table_mode", "timespan", "top"),
    result_fields=(
        "ApiId",
        "LastErrorReason",
        "ResponseCode",
        "Count",
        "FirstSeen",
        "LastSeen",
        "CorrelationId",
    ),
    implementation="src/apim_mcp/clients/logs.py",
    max_timespan="P7D",
    max_rows=100,
)

METRIC_TIMESERIES = QueryDefinition(
    id="metric-timeseries",
    title="Metric time series",
    purpose="Aggregate an approved APIM metric into fixed time intervals.",
    visibility="tool",
    tools=("apim_get_metrics",),
    source_tables=("AzureMetrics",),
    parameters=("resource_id", "metric_name", "timespan", "interval", "aggregation"),
    result_fields=("TimeGenerated", "Value", "SampleCount", "UnitName"),
    implementation="src/apim_mcp/clients/metrics.py",
)

METRIC_DEFINITIONS = QueryDefinition(
    id="metric-definitions",
    title="Metric availability probe",
    purpose="List APIM metric names observed during the startup lookback period.",
    visibility="internal",
    tools=(),
    source_tables=("AzureMetrics",),
    parameters=("resource_id",),
    result_fields=("MetricName",),
    implementation="src/apim_mcp/clients/metrics.py",
    max_timespan="P1D",
)

QUERY_CATALOG = (
    GATEWAY_LOG_DETAIL,
    GATEWAY_ERROR_SUMMARY,
    METRIC_TIMESERIES,
    METRIC_DEFINITIONS,
)

QUERY_BY_ID = MappingProxyType({definition.id: definition for definition in QUERY_CATALOG})
