"""Safety tests for the fixed-shape APIM gateway-log KQL builder (T-18)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from apim_mcp.clients.logs import (
    GatewayLogTableMode,
    build_error_summary_query,
    build_gateway_log_query,
)
from apim_mcp.common.errors import ToolError

RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000"
    "/resourceGroups/rg-fixture"
    "/providers/Microsoft.ApiManagement/service/apim-fixture"
)


def _query_body(query: str) -> str:
    return query.split(");\n", 1)[1]


def test_no_interpolation() -> None:
    result = build_gateway_log_query(
        resource_id=RESOURCE_ID,
        api_id="orders-api",
        operation_id="get-order",
        response_code_category="5xx",
        min_duration_ms=250,
        correlation_id="trace-123",
        limit=50,
    )
    assert not isinstance(result, ToolError)
    body = _query_body(result.query)
    for value in ("orders-api", "get-order", "5xx", "trace-123"):
        assert value not in body
    assert "declare query_parameters" in result.query
    assert result.timespan == timedelta(hours=1)


def test_injection_attempt_is_inert() -> None:
    attack = "'; SigninLogs | take 100 //"
    baseline = build_gateway_log_query(resource_id=RESOURCE_ID, api_id="safe-api")
    attacked = build_gateway_log_query(resource_id=RESOURCE_ID, api_id=attack)

    assert not isinstance(baseline, ToolError)
    assert not isinstance(attacked, ToolError)
    assert _query_body(attacked.query) == _query_body(baseline.query)
    assert attack not in _query_body(attacked.query)
    assert "SigninLogs" not in _query_body(attacked.query)
    assert "\\u003b" in attacked.query


def test_timespan_capped() -> None:
    result = build_gateway_log_query(resource_id=RESOURCE_ID, timespan="P30D")
    assert isinstance(result, ToolError)
    assert result.kind == "invalid_input"
    assert "P7D" in result.message


def test_limit_capped() -> None:
    result = build_gateway_log_query(resource_id=RESOURCE_ID, limit=500)
    assert not isinstance(result, ToolError)
    assert result.limit == 200
    assert result.truncated is True
    assert "limit_value:long = 200" in result.query


@pytest.mark.parametrize(
    "result",
    [
        build_gateway_log_query(resource_id=RESOURCE_ID),
        build_error_summary_query(resource_id=RESOURCE_ID),
    ],
)
def test_only_supported_gateway_log_tables_are_referenced(
    result: object,
) -> None:
    assert not isinstance(result, ToolError)
    assert hasattr(result, "query")
    body = _query_body(result.query)
    assert "ApiManagementGatewayLogs" in body
    assert "AzureDiagnostics" in body
    assert "SigninLogs" not in body
    assert "AzureActivity" not in body
    assert "union isfuzzy=true" in body
    assert body.count("_ResourceId =~ resource_id") == 2
    assert 'Category == "GatewayLogs"' in body
    assert RESOURCE_ID not in body


@pytest.mark.parametrize(
    ("table_mode", "expected_table", "unexpected_table"),
    [
        ("resourceSpecific", "ApiManagementGatewayLogs", "AzureDiagnostics"),
        ("azureDiagnostics", "AzureDiagnostics", "ApiManagementGatewayLogs"),
    ],
)
def test_explicit_gateway_log_table_mode_queries_one_table(
    table_mode: GatewayLogTableMode,
    expected_table: str,
    unexpected_table: str,
) -> None:
    result = build_gateway_log_query(
        resource_id=RESOURCE_ID,
        table_mode=table_mode,
    )

    assert not isinstance(result, ToolError)
    body = _query_body(result.query)
    assert expected_table in body
    assert unexpected_table not in body
    assert "union isfuzzy=true" not in body


@pytest.mark.parametrize(
    ("table_mode", "expected_table", "unexpected_table"),
    [
        ("resourceSpecific", "ApiManagementGatewayLogs", "AzureDiagnostics"),
        ("azureDiagnostics", "AzureDiagnostics", "ApiManagementGatewayLogs"),
    ],
)
def test_explicit_error_summary_table_mode_queries_one_table(
    table_mode: GatewayLogTableMode,
    expected_table: str,
    unexpected_table: str,
) -> None:
    result = build_error_summary_query(
        resource_id=RESOURCE_ID,
        table_mode=table_mode,
    )

    assert not isinstance(result, ToolError)
    body = _query_body(result.query)
    assert expected_table in body
    assert unexpected_table not in body
    assert "union isfuzzy=true" not in body


def test_auto_mode_has_empty_fallback_for_uncreated_tables() -> None:
    result = build_gateway_log_query(resource_id=RESOURCE_ID)

    assert not isinstance(result, ToolError)
    body = _query_body(result.query)
    assert "datatable(" in body
    for declaration in (
        "TimeGenerated:datetime",
        "ApiId:string",
        "OperationId:string",
        "Method:string",
        "ResponseCode:int",
        "TotalTime:long",
        "BackendTime:long",
        "IsRequestSuccess:bool",
        "LastErrorReason:string",
        "LastErrorSource:string",
        "LastErrorMessage:string",
        "CorrelationId:string",
        "Region:string",
        "Url:string",
    ):
        assert declaration in body
    assert 'ResponseCode=toint(column_ifexists("ResponseCode", 0))' in body
    assert 'toint(column_ifexists("responseCode_d", real(null)))' in body
    assert "int(0)" in body
    assert result.tolerated_empty_partial_codes == frozenset(
        {"FailedToResolveTableExpression", "FuzzyUnionSourceNotFound"}
    )


def test_explicit_mode_does_not_tolerate_missing_table_errors() -> None:
    result = build_gateway_log_query(
        resource_id=RESOURCE_ID,
        table_mode="azureDiagnostics",
    )

    assert not isinstance(result, ToolError)
    assert result.tolerated_empty_partial_codes == frozenset()


@pytest.mark.parametrize(
    "result",
    [
        build_gateway_log_query(
            resource_id=RESOURCE_ID,
            table_mode="azureDiagnostics",
            include_urls=True,
        ),
        build_error_summary_query(
            resource_id=RESOURCE_ID,
            table_mode="azureDiagnostics",
        ),
    ],
)
def test_azure_diagnostics_fields_are_normalized(result: object) -> None:
    assert not isinstance(result, ToolError)
    assert hasattr(result, "query")
    body = _query_body(result.query)
    expected_mappings = (
        '"apiId_s"',
        '"operationId_s"',
        '"method_s"',
        '"responseCode_d"',
        '"DurationMs"',
        '"backendTime_d"',
        '"isRequestSuccess_b"',
        '"lastError_reason_s"',
        '"lastError_source_s"',
        '"lastError_message_s"',
        '"correlationId_g"',
        '"region_s"',
        '"requestUrl_s"',
    )
    for mapping in expected_mappings:
        assert mapping in body
    for incorrect_name in (
        '"CorrelationId"',
        '"Region_s"',
        '"lastErrorReason_s"',
        '"lastErrorSource_s"',
        '"lastErrorMessage_s"',
        '"url_s"',
    ):
        assert incorrect_name not in body


def test_azure_diagnostics_additional_fields_are_strictly_allowlisted() -> None:
    result = build_gateway_log_query(resource_id=RESOURCE_ID, include_urls=True)
    assert not isinstance(result, ToolError)
    body = _query_body(result.query)
    assert '"AdditionalFields"' in body
    for property_name in (
        "apiId",
        "operationId",
        "method",
        "responseCode",
        "duration",
        "backendTime",
        "isRequestSuccess",
        "lastError_reason",
        "lastError_source",
        "lastError_message",
        "correlationId",
        "region",
        "requestUrl",
    ):
        assert f'_AdditionalFields["{property_name}"]' in body

    lowered = body.lower()
    for forbidden in (
        "requestbody",
        "responsebody",
        "requestheaders",
        "responseheaders",
        "authorization",
        "credential",
        "subscriptionkey",
        "token",
        "bag_unpack",
        "mv-expand",
    ):
        assert forbidden not in lowered
    assert "| project AdditionalFields" not in body


def test_gateway_sources_are_not_lossily_deduplicated() -> None:
    result = build_gateway_log_query(resource_id=RESOURCE_ID)
    assert not isinstance(result, ToolError)
    body = _query_body(result.query)
    assert "_SourcePriority" not in body
    assert "_EventKey" not in body
    assert "arg_min" not in body
    assert "requestBody" not in body
    assert "responseBody" not in body
    assert "headers" not in body.lower()


def test_url_query_string_is_stripped_in_final_kql_projection() -> None:
    result = build_gateway_log_query(resource_id=RESOURCE_ID, include_urls=True)
    assert not isinstance(result, ToolError)
    body = _query_body(result.query)
    final_projection = body.rsplit("| project ", 1)[1].split("| order by", 1)[0]
    assert 'Url=iff(include_urls, tostring(split(Url, "?")[0]), "")' in final_projection
    assert "\n          Url\n" not in final_projection


def test_include_urls_is_bound_without_changing_query_shape() -> None:
    excluded = build_gateway_log_query(resource_id=RESOURCE_ID, include_urls=False)
    included = build_gateway_log_query(resource_id=RESOURCE_ID, include_urls=True)

    assert not isinstance(excluded, ToolError)
    assert not isinstance(included, ToolError)
    assert _query_body(excluded.query) == _query_body(included.query)
    assert "include_urls:bool = false" in excluded.query.split(");", 1)[0]
    assert "include_urls:bool = true" in included.query.split(");", 1)[0]


def test_azure_diagnostics_injection_attempt_is_inert() -> None:
    attack = "'; AzureActivity | take 100 //"
    baseline = build_gateway_log_query(
        resource_id=RESOURCE_ID,
        correlation_id="safe-correlation",
    )
    attacked = build_gateway_log_query(resource_id=RESOURCE_ID, correlation_id=attack)

    assert not isinstance(baseline, ToolError)
    assert not isinstance(attacked, ToolError)
    assert _query_body(attacked.query) == _query_body(baseline.query)
    assert attack not in _query_body(attacked.query)
    assert "\\u003b" in attacked.query


@pytest.mark.parametrize("timespan", ["PT1H", "P1D", "P7D"])
def test_valid_timespans(timespan: str) -> None:
    result = build_gateway_log_query(resource_id=RESOURCE_ID, timespan=timespan)
    assert not isinstance(result, ToolError)


def test_summary_query_is_fixed_table_and_bounded() -> None:
    query = build_error_summary_query(
        resource_id=RESOURCE_ID,
        timespan="PT24H",
        top=10,
    )
    assert not isinstance(query, ToolError)
    assert "ApiManagementGatewayLogs" in query.query
    assert "AzureDiagnostics" in query.query
    assert 'Category == "GatewayLogs"' in query.query
    assert "summarize Count=count()" in query.query
    assert "_SourcePriority" not in query.query
    assert "_EventKey" not in query.query
    assert "arg_min" not in query.query
    assert "top top_value by Count desc" in query.query
    assert query.query.count("_ResourceId =~ resource_id") == 2


def test_summary_query_binds_resource_id_and_caps_top() -> None:
    """The summary shape is subject to the same two rules as the detail
    shape: no user value in the body, and a hard server-side cap."""
    query = build_error_summary_query(resource_id=RESOURCE_ID, top=5000)
    assert not isinstance(query, ToolError)
    body = _query_body(query.query)
    assert RESOURCE_ID not in body
    assert "SigninLogs" not in body
    assert "union isfuzzy=true" in body
    assert query.limit == 100
    assert query.truncated is True
    assert "top_value:long = 100" in query.query


def test_summary_timespan_capped() -> None:
    result = build_error_summary_query(resource_id=RESOURCE_ID, timespan="P30D")
    assert isinstance(result, ToolError)
    assert result.kind == "invalid_input"
    assert "P7D" in result.message
