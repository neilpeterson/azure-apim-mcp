"""Fixed-shape, parameterized KQL construction for APIM gateway logs (T-18).

Only the resource-specific ``ApiManagementGatewayLogs`` and legacy
``AzureDiagnostics`` tables are addressable. User-controlled values are
encoded into a ``declare query_parameters`` preamble and the query body
references parameter names exclusively
(``docs/development/PRINCIPLES.md`` §6).

Shared Log Analytics plumbing lives in ``_loganalytics``. What stays here is
the two fixed query shapes, the gateway-log error wording, and the
log-specific redaction applied to untrusted error text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from apim_mcp.auth.context import CallContext
from apim_mcp.auth.credentials import LOGS_SCOPE
from apim_mcp.clients._loganalytics import (
    Timespan,
    kql_string,
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
from apim_mcp.common.redaction import (
    redact_free_text,
    strip_url_query_string,
    wrap_untrusted_content,
)
from apim_mcp.queries.catalog import (
    GATEWAY_ERROR_SUMMARY,
    GATEWAY_LOG_DETAIL,
    QueryDefinition,
)

ResponseCodeCategory = Literal["2xx", "3xx", "4xx", "5xx"]
GatewayLogTableMode = Literal["auto", "resourceSpecific", "azureDiagnostics"]
_MAX_TIMESPAN = timedelta(days=7)
_MAX_LIMIT = 200
_MAX_TOP = 100
_TIMESPAN_EXAMPLE = "'PT1H', with a maximum of 'P7D'"
_UNTRUSTED_FIELDS = ("LastErrorReason", "LastErrorSource", "LastErrorMessage")
_MISSING_TABLE_WARNING_CODES = frozenset(
    {"FailedToResolveTableExpression", "FuzzyUnionSourceNotFound"}
)
_RESOURCE_SPECIFIC_SOURCE = """
ApiManagementGatewayLogs
| where _ResourceId =~ resource_id
| project TimeGenerated=todatetime(column_ifexists("TimeGenerated", datetime(null))),
          ApiId=tostring(column_ifexists("ApiId", "")),
          OperationId=tostring(column_ifexists("OperationId", "")),
          Method=tostring(column_ifexists("Method", "")),
          ResponseCode=toint(column_ifexists("ResponseCode", 0)),
          TotalTime=tolong(column_ifexists("TotalTime", 0)),
          BackendTime=tolong(column_ifexists("BackendTime", 0)),
          IsRequestSuccess=tobool(column_ifexists("IsRequestSuccess", true)),
          LastErrorReason=tostring(column_ifexists("LastErrorReason", "")),
          LastErrorSource=tostring(column_ifexists("LastErrorSource", "")),
          LastErrorMessage=tostring(column_ifexists("LastErrorMessage", "")),
          CorrelationId=tostring(column_ifexists("CorrelationId", "")),
          Region=tostring(column_ifexists("Region", "")),
          Url=tostring(column_ifexists("Url", ""))
""".strip()
_AZURE_DIAGNOSTICS_SOURCE = """
AzureDiagnostics
| where _ResourceId =~ resource_id
| where Category == "GatewayLogs"
| extend _AdditionalFields=todynamic(column_ifexists("AdditionalFields", dynamic({})))
| extend _ResponseCode=coalesce(
             toint(column_ifexists("responseCode_d", real(null))),
             toint(_AdditionalFields["responseCode"]),
             int(0)
         )
| project TimeGenerated=todatetime(column_ifexists("TimeGenerated", datetime(null))),
          ApiId=coalesce(
              tostring(column_ifexists("apiId_s", "")),
              tostring(_AdditionalFields["apiId"])
          ),
          OperationId=coalesce(
              tostring(column_ifexists("operationId_s", "")),
              tostring(_AdditionalFields["operationId"])
          ),
          Method=coalesce(
              tostring(column_ifexists("method_s", "")),
              tostring(_AdditionalFields["method"])
          ),
          ResponseCode=_ResponseCode,
          TotalTime=coalesce(
              tolong(column_ifexists("DurationMs", long(null))),
              tolong(_AdditionalFields["duration"]),
              long(0)
          ),
          BackendTime=coalesce(
              tolong(column_ifexists("backendTime_d", real(null))),
              tolong(_AdditionalFields["backendTime"]),
              long(0)
          ),
          IsRequestSuccess=coalesce(
              tobool(column_ifexists("isRequestSuccess_b", bool(null))),
              tobool(_AdditionalFields["isRequestSuccess"]),
              _ResponseCode < 400
          ),
          LastErrorReason=coalesce(
              tostring(column_ifexists("lastError_reason_s", "")),
              tostring(_AdditionalFields["lastError_reason"])
          ),
          LastErrorSource=coalesce(
              tostring(column_ifexists("lastError_source_s", "")),
              tostring(_AdditionalFields["lastError_source"])
          ),
          LastErrorMessage=coalesce(
              tostring(column_ifexists("lastError_message_s", "")),
              tostring(_AdditionalFields["lastError_message"])
          ),
          CorrelationId=coalesce(
              tostring(column_ifexists("correlationId_g", "")),
              tostring(_AdditionalFields["correlationId"])
          ),
          Region=coalesce(
              tostring(column_ifexists("region_s", "")),
              tostring(_AdditionalFields["region"])
          ),
          Url=coalesce(
              tostring(column_ifexists("requestUrl_s", "")),
              tostring(_AdditionalFields["requestUrl"])
          )
""".strip()
_EMPTY_SOURCE = """
datatable(
    TimeGenerated:datetime,
    ApiId:string,
    OperationId:string,
    Method:string,
    ResponseCode:int,
    TotalTime:long,
    BackendTime:long,
    IsRequestSuccess:bool,
    LastErrorReason:string,
    LastErrorSource:string,
    LastErrorMessage:string,
    CorrelationId:string,
    Region:string,
    Url:string
)[]
""".strip()


@dataclass(frozen=True)
class GatewayLogQuery:
    definition: QueryDefinition
    query: str
    timespan: Timespan
    limit: int
    truncated: bool
    mode: Literal["detail", "summary"]
    include_urls: bool = False
    tolerated_empty_partial_codes: frozenset[str] = frozenset()


def _bounded_timespan(value: str) -> Timespan | ToolError:
    try:
        parsed = parse_timespan(value)
    except ValueError:
        return invalid_input("timespan", _TIMESPAN_EXAMPLE)
    duration = parsed if isinstance(parsed, timedelta) else parsed[1] - parsed[0]
    if duration > _MAX_TIMESPAN:
        return invalid_input("timespan", _TIMESPAN_EXAMPLE)
    return parsed


def _normalized_gateway_sources(table_mode: GatewayLogTableMode) -> str:
    """Return the configured fixed gateway-log source with a normalized shape."""
    if table_mode == "resourceSpecific":
        return _RESOURCE_SPECIFIC_SOURCE
    if table_mode == "azureDiagnostics":
        return _AZURE_DIAGNOSTICS_SOURCE
    return (
        "union isfuzzy=true\n"
        f"(\n{_EMPTY_SOURCE}\n),\n"
        f"(\n{_RESOURCE_SPECIFIC_SOURCE}\n),\n"
        f"(\n{_AZURE_DIAGNOSTICS_SOURCE}\n)"
    )


def build_gateway_log_query(
    *,
    resource_id: str,
    table_mode: GatewayLogTableMode = "auto",
    timespan: str = "PT1H",
    api_id: str | None = None,
    operation_id: str | None = None,
    response_code_category: ResponseCodeCategory | None = None,
    min_duration_ms: int | None = None,
    correlation_id: str | None = None,
    limit: int = 50,
    include_urls: bool = False,
) -> GatewayLogQuery | ToolError:
    """Build the only allowed detailed gateway-log query shape."""
    parsed_timespan = _bounded_timespan(timespan)
    if isinstance(parsed_timespan, ToolError):
        return parsed_timespan
    if limit < 1:
        return invalid_input("limit", "an integer from 1 through 200")
    if min_duration_ms is not None and min_duration_ms < 0:
        return invalid_input("min_duration_ms", "a non-negative integer such as 250")

    effective_limit = min(limit, _MAX_LIMIT)
    declarations = (
        "declare query_parameters("
        f"resource_id:string = {kql_string(resource_id)}, "
        f"has_api_id:bool = {str(api_id is not None).lower()}, "
        f"api_id:string = {kql_string(api_id or '')}, "
        f"has_operation_id:bool = {str(operation_id is not None).lower()}, "
        f"operation_id:string = {kql_string(operation_id or '')}, "
        f"has_response_category:bool = {str(response_code_category is not None).lower()}, "
        f"response_category:string = {kql_string(response_code_category or '')}, "
        f"has_min_duration:bool = {str(min_duration_ms is not None).lower()}, "
        f"min_duration_ms:long = {min_duration_ms or 0}, "
        f"has_correlation_id:bool = {str(correlation_id is not None).lower()}, "
        f"correlation_id:string = {kql_string(correlation_id or '')}, "
        f"include_urls:bool = {str(include_urls).lower()}, "
        f"limit_value:long = {effective_limit}"
        ");"
    )
    body = f"""
{_normalized_gateway_sources(table_mode)}
| where not(has_api_id) or ApiId == api_id
| where not(has_operation_id) or OperationId == operation_id
| where not(has_response_category)
    or tostring(ResponseCode) startswith substring(response_category, 0, 1)
| where not(has_min_duration) or TotalTime >= min_duration_ms
| where not(has_correlation_id) or CorrelationId == correlation_id
| project TimeGenerated,
          ApiId,
          OperationId,
          Method,
          ResponseCode,
          TotalTime,
          BackendTime,
          IsRequestSuccess,
          LastErrorReason,
          LastErrorSource,
          LastErrorMessage,
          CorrelationId,
          Region,
          Url=iff(include_urls, tostring(split(Url, "?")[0]), "")
| order by TimeGenerated desc
| take limit_value
""".strip()
    return GatewayLogQuery(
        definition=GATEWAY_LOG_DETAIL,
        query=f"{declarations}\n{body}",
        timespan=parsed_timespan,
        limit=effective_limit,
        truncated=limit > _MAX_LIMIT,
        mode="detail",
        include_urls=include_urls,
        tolerated_empty_partial_codes=(
            _MISSING_TABLE_WARNING_CODES if table_mode == "auto" else frozenset()
        ),
    )


def build_error_summary_query(
    *,
    resource_id: str,
    table_mode: GatewayLogTableMode = "auto",
    timespan: str = "PT24H",
    top: int = 10,
) -> GatewayLogQuery | ToolError:
    """Build the fixed failure-summary query shape."""
    parsed_timespan = _bounded_timespan(timespan)
    if isinstance(parsed_timespan, ToolError):
        return parsed_timespan
    if top < 1:
        return invalid_input("top", "an integer from 1 through 100")
    effective_top = min(top, _MAX_TOP)
    declarations = (
        "declare query_parameters("
        f"resource_id:string = {kql_string(resource_id)}, "
        f"top_value:long = {effective_top}"
        ");"
    )
    body = f"""
{_normalized_gateway_sources(table_mode)}
| where ResponseCode >= 400 or IsRequestSuccess == false or isnotempty(LastErrorReason)
| summarize Count=count(),
            FirstSeen=min(TimeGenerated),
            LastSeen=max(TimeGenerated),
            CorrelationId=take_any(CorrelationId)
    by ApiId, LastErrorReason, ResponseCode
| top top_value by Count desc
""".strip()
    return GatewayLogQuery(
        definition=GATEWAY_ERROR_SUMMARY,
        query=f"{declarations}\n{body}",
        timespan=parsed_timespan,
        limit=effective_top,
        truncated=top > _MAX_TOP,
        mode="summary",
        tolerated_empty_partial_codes=(
            _MISSING_TABLE_WARNING_CODES if table_mode == "auto" else frozenset()
        ),
    )


def _gateway_log_error(status_code: int, retry_after: int) -> ToolError:
    if status_code == 403:
        return access_denied("APIM gateway logs in Log Analytics", "Log Analytics Reader")
    if status_code == 429:
        return throttled(retry_after)
    if status_code == 400:
        return upstream_error(log_detail="Log Analytics rejected the fixed gateway KQL")
    return upstream_error(log_detail=f"Log Analytics gateway query returned HTTP {status_code}")


def _protect_untrusted_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Label attacker-influenceable gateway error text as data (§9).

    Error text is published by whoever owns the API or backend, so it is
    redacted, stripped of URL query strings, and wrapped before it reaches
    the model.
    """
    protected = dict(row)
    for field in _UNTRUSTED_FIELDS:
        value = protected.get(field)
        if isinstance(value, str) and value:
            sanitized = redact_free_text(value, strip_urls=True)
            protected[field] = wrap_untrusted_content(sanitized)
    return protected


def _apply_url_policy(row: dict[str, Any], *, include_urls: bool) -> None:
    """`Url` is dropped unless asked for, and always loses its query string."""
    if not include_urls:
        row.pop("Url", None)
        return
    url = row.get("Url")
    if isinstance(url, str):
        row["Url"] = strip_url_query_string(url)


class LogsClient:
    """Per-request Log Analytics client for the two fixed gateway queries."""

    def __init__(self, ctx: CallContext) -> None:
        self._ctx = ctx

    async def execute(
        self,
        workspace_resource_id: str,
        query: GatewayLogQuery,
    ) -> dict[str, Any] | ToolError:
        result = await run_workspace_query(
            self._ctx,
            scope=LOGS_SCOPE,
            workspace_resource_id=workspace_resource_id,
            definition=query.definition,
            query=query.query,
            timespan=query.timespan,
            on_status=_gateway_log_error,
            tolerated_empty_partial_codes=query.tolerated_empty_partial_codes,
        )
        if isinstance(result, ToolError):
            return result

        items = [_protect_untrusted_fields(row) for row in result.rows]
        if query.mode == "detail":
            for item in items:
                _apply_url_policy(item, include_urls=query.include_urls)
        return {
            "items": items,
            "truncated": result.partial or query.truncated or len(items) >= query.limit,
            "partial": result.partial,
        }
