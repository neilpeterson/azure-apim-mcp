"""Tests for exported APIM platform metrics (T-17)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from types import NoneType
from typing import Any, cast, get_type_hints

import pytest
from azure.core.exceptions import ServiceRequestError
from starlette.testclient import TestClient

from _mcp_harness import (
    REQUEST_HEADERS,
)
from _mcp_harness import (
    authorization_header as _authorization_header,
)
from _mcp_harness import (
    jwks_cache as _jwks_cache,
)
from _mcp_harness import (
    rpc as _rpc,
)
from _mcp_harness import (
    settings as _base_settings,
)
from apim_mcp.auth.context import CallContext
from apim_mcp.clients._loganalytics import parse_interval, parse_timespan
from apim_mcp.clients.metrics import MetricsClient, probe_metric_availability
from apim_mcp.common.errors import ToolError
from apim_mcp.server import ToolRegistration, create_mcp, wrap_with_middleware
from apim_mcp.settings import ApimServiceConfig, Settings
from apim_mcp.tools.telemetry import register_telemetry_tools

RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000"
    "/resourceGroups/rg-fixture"
    "/providers/Microsoft.ApiManagement/service/apim-fixture"
)
WORKSPACE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000"
    "/resourceGroups/rg-fixture"
    "/providers/Microsoft.OperationalInsights/workspaces/law-fixture"
)


class _FakeToken:
    def __init__(self, token: str) -> None:
        self.token = token


class _FakeCredential:
    async def get_token(self, *scopes: str) -> _FakeToken:
        return _FakeToken("fake-token")


class _FakeArmClient:
    def __init__(self, ctx: CallContext) -> None:
        self.ctx = ctx

    async def get(self, resource_id: str, *, api_version: str) -> dict[str, Any]:
        assert resource_id == WORKSPACE_ID
        assert api_version == "2023-09-01"
        return {"properties": {"customerId": "workspace-customer-id"}}


class _FakeTable:
    def __init__(self, *, definitions: bool = False) -> None:
        self.columns: list[str]
        self.rows: list[list[Any]]
        if definitions:
            self.columns = ["MetricName"]
            self.rows = [["Requests"]]
        else:
            self.columns = ["TimeGenerated", "Value", "SampleCount", "UnitName", "Dimension"]
            self.rows = [
                [
                    datetime(2026, 9, 11, 12, 0, tzinfo=UTC),
                    42.5,
                    2.0,
                    "Count",
                    '[{"Name":"GatewayResponseCodeCategory","Value":"5xx"}]',
                ]
            ]


class _FakeLogsResult:
    def __init__(self, *, definitions: bool = False, partial: bool = False) -> None:
        table = _FakeTable(definitions=definitions)
        self.tables = [] if partial else [table]
        self.partial_data = [table] if partial else []
        self.partial_error = object() if partial else None


class _FakeLogsClient:
    last_query: dict[str, Any] | None = None

    def __init__(self, credential: Any) -> None:
        self.credential = credential

    async def __aenter__(self) -> _FakeLogsClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def query_workspace(
        self, workspace_id: str, query: str, **kwargs: Any
    ) -> _FakeLogsResult:
        type(self).last_query = {"workspace_id": workspace_id, "query": query, **kwargs}
        return _FakeLogsResult(definitions="summarize by MetricName" in query)


class _FailingLogsClient(_FakeLogsClient):
    async def query_workspace(
        self, workspace_id: str, query: str, **kwargs: Any
    ) -> _FakeLogsResult:
        raise ServiceRequestError("synthetic transport failure")


class _PartialLogsClient(_FakeLogsClient):
    async def query_workspace(
        self, workspace_id: str, query: str, **kwargs: Any
    ) -> _FakeLogsResult:
        return _FakeLogsResult(partial=True)


def _settings() -> Settings:
    return _base_settings(
        services=[
            ApimServiceConfig(
                alias="prod",
                resource_id=RESOURCE_ID,
                log_analytics_workspace_id=WORKSPACE_ID,
            )
        ]
    )


def _call_tool(client: TestClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        json=_rpc("tools/call", {"name": name, "arguments": arguments}),
        headers={**REQUEST_HEADERS, "Authorization": _authorization_header()},
    )
    body = response.json()
    assert "result" in body, body
    text: str = body["result"]["content"][0]["text"]
    return cast(dict[str, Any], json.loads(text))


def _patch_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the shared Log Analytics plumbing, which is where the credential,
    ARM lookup, and SDK client are actually constructed."""
    monkeypatch.setattr(
        "apim_mcp.clients._loganalytics.credential_for", lambda ctx, scope: _FakeCredential()
    )
    monkeypatch.setattr("apim_mcp.clients._loganalytics.ArmClient", _FakeArmClient)
    monkeypatch.setattr("apim_mcp.clients._loganalytics.LogsQueryClient", _FakeLogsClient)


def test_timespan_accepts_duration_and_start_end() -> None:
    assert parse_timespan("PT1H") == timedelta(hours=1)
    parsed = parse_timespan("2026-09-10T00:00:00Z/2026-09-10T01:30:00Z")
    assert isinstance(parsed, tuple)
    assert parsed == (
        datetime(2026, 9, 10, tzinfo=UTC),
        datetime(2026, 9, 10, 1, 30, tzinfo=UTC),
    )
    assert parse_interval("PT5M") == timedelta(minutes=5)


@pytest.mark.parametrize("value", ["one hour", "P", "2026-09-10/garbage"])
def test_invalid_timespan_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        parse_timespan(value)


async def test_dimension_filter_rejected_with_gateway_log_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_clients(monkeypatch)
    ctx = CallContext(oid="oid", upn="u@example.com", roles=(), bearer_token="token")

    result = await MetricsClient(ctx).query(
        RESOURCE_ID,
        WORKSPACE_ID,
        metric="Requests",
        timespan="PT1H",
        interval="PT5M",
        aggregation="average",
        dimension_filter="GatewayResponseCodeCategory eq '5xx'",
    )

    assert isinstance(result, ToolError)
    assert result.kind == "invalid_input"
    assert "flattens dimensions" in result.message
    assert "apim_query_gateway_logs" in result.message


async def test_interval_is_bound_and_average_is_weighted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_clients(monkeypatch)
    ctx = CallContext(oid="oid", upn="u@example.com", roles=(), bearer_token="token")

    result = await MetricsClient(ctx).query(
        RESOURCE_ID,
        WORKSPACE_ID,
        metric="Capacity",
        timespan="PT1H",
        interval="PT5M",
        aggregation="average",
    )

    assert not isinstance(result, ToolError)
    assert _FakeLogsClient.last_query is not None
    query = _FakeLogsClient.last_query["query"]
    declaration, body = query.split(");", 1)
    assert "interval:timespan = time(0.00:05:00)" in declaration
    assert "0.00:05:00" not in body
    assert RESOURCE_ID not in body
    assert "Capacity" not in body
    assert "sum(Total) / sum(Count)" in body
    assert _FakeLogsClient.last_query["timespan"] == timedelta(hours=1)
    assert _FakeLogsClient.last_query["server_timeout"] == 50


def test_deprecated_metric_names_rejected_with_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_clients(monkeypatch)
    settings = _settings()
    mcp = create_mcp(settings, allowed_hosts=["testserver"])
    registry: list[ToolRegistration] = []
    register_telemetry_tools(mcp, registry, settings)
    app = wrap_with_middleware(mcp, settings, jwks_cache=_jwks_cache())

    with TestClient(app) as client:
        payload = _call_tool(
            client,
            "apim_get_metrics",
            {
                "service": "prod",
                "metric": "FailedRequests",
                "response_format": "json",
            },
        )

    assert payload["error"]["kind"] == "invalid_input"
    assert "Requests" in payload["error"]["message"]
    assert "apim_query_gateway_logs" in payload["error"]["message"]


async def test_metric_definitions_probe_logs_available_metrics(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_clients(monkeypatch)
    ctx = CallContext(oid="startup", upn="startup", roles=(), bearer_token="")

    with caplog.at_level(logging.INFO, logger="apim_mcp.metrics"):
        available = await MetricsClient(ctx).list_definitions(RESOURCE_ID, WORKSPACE_ID)

    assert available == ["Requests"]
    assert any("Requests" in record.getMessage() for record in caplog.records)


async def test_probe_returns_none_and_logs_available_metrics(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The startup probe exists to log, not to return: `server.py` discards
    its result, so it must not build a findings dict nobody reads."""
    _patch_clients(monkeypatch)
    ctx = CallContext(oid="startup", upn="startup", roles=(), bearer_token="")

    assert get_type_hints(probe_metric_availability)["return"] is NoneType

    with caplog.at_level(logging.INFO, logger="apim_mcp.metrics"):
        await probe_metric_availability(ctx, _settings().apim_services)

    assert any("Requests" in record.getMessage() for record in caplog.records)


async def test_probe_warns_when_a_service_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreachable workspace is a warning, never a startup failure."""
    _patch_clients(monkeypatch)
    monkeypatch.setattr("apim_mcp.clients._loganalytics.LogsQueryClient", _FailingLogsClient)
    ctx = CallContext(oid="startup", upn="startup", roles=(), bearer_token="")

    with caplog.at_level(logging.WARNING, logger="apim_mcp.metrics"):
        await probe_metric_availability(ctx, _settings().apim_services)

    assert any("prod" in record.getMessage() for record in caplog.records)


async def test_transport_failure_returns_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_clients(monkeypatch)
    monkeypatch.setattr("apim_mcp.clients._loganalytics.LogsQueryClient", _FailingLogsClient)
    ctx = CallContext(oid="oid", upn="u@example.com", roles=(), bearer_token="token")

    result = await MetricsClient(ctx).query(
        RESOURCE_ID,
        WORKSPACE_ID,
        metric="Capacity",
        timespan="PT1H",
        interval="PT5M",
        aggregation="average",
    )

    assert isinstance(result, ToolError)
    assert result.kind == "timeout"


async def test_metric_partial_rows_are_returned_and_marked_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_clients(monkeypatch)
    monkeypatch.setattr("apim_mcp.clients._loganalytics.LogsQueryClient", _PartialLogsClient)
    ctx = CallContext(oid="oid", upn="u@example.com", roles=(), bearer_token="token")

    result = await MetricsClient(ctx).query(
        RESOURCE_ID,
        WORKSPACE_ID,
        metric="Capacity",
        timespan="PT1H",
        interval="PT5M",
        aggregation="average",
    )

    assert not isinstance(result, ToolError)
    assert result["items"]
    assert result["partial"] is True
    assert result["truncated"] is True
