"""Safety tests for the fixed-shape APIM gateway-log KQL builder (T-18)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from apim_mcp.clients.logs import build_error_summary_query, build_gateway_log_query
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


def test_only_gateway_log_table_is_referenced() -> None:
    result = build_gateway_log_query(resource_id=RESOURCE_ID)
    assert not isinstance(result, ToolError)
    body = _query_body(result.query)
    assert "ApiManagementGatewayLogs" in body
    assert "SigninLogs" not in body
    assert "AzureActivity" not in body
    assert "union" not in body.lower()
    assert "_ResourceId =~ resource_id" in body
    assert RESOURCE_ID not in body


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
    assert "summarize Count=count()" in query.query
    assert "top top_value by Count desc" in query.query
    assert "_ResourceId =~ resource_id" in query.query


def test_summary_query_binds_resource_id_and_caps_top() -> None:
    """The summary shape is subject to the same two rules as the detail
    shape: no user value in the body, and a hard server-side cap."""
    query = build_error_summary_query(resource_id=RESOURCE_ID, top=5000)
    assert not isinstance(query, ToolError)
    body = _query_body(query.query)
    assert RESOURCE_ID not in body
    assert "SigninLogs" not in body
    assert "union" not in body.lower()
    assert query.limit == 100
    assert query.truncated is True
    assert "top_value:long = 100" in query.query


def test_summary_timespan_capped() -> None:
    result = build_error_summary_query(resource_id=RESOURCE_ID, timespan="P30D")
    assert isinstance(result, ToolError)
    assert result.kind == "invalid_input"
    assert "P7D" in result.message
