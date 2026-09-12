"""Tests for src/apim_mcp/tools/discovery.py (T-10).

See docs/development/SPEC.md §6.0 (tool conventions) and §6 Group A.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

import apim_mcp.clients.arm as arm_module
from _fixture_transport import FixtureTransport
from _mcp_harness import (
    call_tool as _shared_call_tool,
)
from _mcp_harness import (
    jwks_cache as _jwks_cache,
)
from _mcp_harness import (
    result_text as _shared_result_text,
)
from _mcp_harness import (
    settings as _base_settings,
)
from apim_mcp.auth.context import CallContext
from apim_mcp.clients.arm import ArmClient
from apim_mcp.server import ToolRegistration, create_mcp, wrap_with_middleware
from apim_mcp.settings import ApimServiceConfig, Settings
from apim_mcp.tools.discovery import register_discovery_tools

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000"
    "/resourceGroups/rg-fixture"
    "/providers/Microsoft.ApiManagement/service/apim-fixture"
)


class _FakeToken:
    def __init__(self, token: str) -> None:
        self.token = token


class _FakeCredential:
    async def get_token(self, *scopes: str) -> _FakeToken:
        return _FakeToken("fake-token")


class _FakeMetricsClient:
    def __init__(self, ctx: CallContext) -> None:
        self.ctx = ctx

    async def query(
        self, resource_id: str, workspace_resource_id: str | None, **kwargs: Any
    ) -> dict[str, Any]:
        return {
            "items": [
                {"Value": 40.0, "SampleCount": 1.0},
                {"Value": 60.0, "SampleCount": 3.0},
            ]
        }


def _service(alias: str) -> ApimServiceConfig:
    return ApimServiceConfig(alias=alias, resource_id=RESOURCE_ID)


def _settings(*, services: list[ApimServiceConfig] | None = None) -> Settings:
    return _base_settings(services=services if services is not None else [_service("prod")])


def _build_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    transport: httpx.AsyncBaseTransport,
    services: list[ApimServiceConfig] | None = None,
) -> tuple[Any, list[ToolRegistration], Settings]:
    """Build the real discovery-tool app, with `ArmClient` calls served
    from `transport` instead of the network."""
    monkeypatch.setattr(arm_module, "credential_for", lambda ctx, scope: _FakeCredential())
    monkeypatch.setattr("apim_mcp.tools.discovery.MetricsClient", _FakeMetricsClient)

    original_client = ArmClient

    def patched(ctx: CallContext, **kwargs: Any) -> ArmClient:
        return original_client(ctx, transport=transport)

    monkeypatch.setattr("apim_mcp.tools.discovery.ArmClient", patched)

    settings = _settings(services=services)
    mcp = create_mcp(settings, allowed_hosts=["testserver"])
    registry: list[ToolRegistration] = []
    register_discovery_tools(mcp, registry, settings)
    app = wrap_with_middleware(mcp, settings, jwks_cache=_jwks_cache())
    return app, registry, settings


def _call_tool(client: TestClient, name: str, arguments: dict[str, Any]) -> httpx.Response:
    return _shared_call_tool(client, name, arguments)


def _result_text(response: httpx.Response) -> str:
    return _shared_result_text(response)


def _fixture_transport() -> FixtureTransport:
    return FixtureTransport(FIXTURES_DIR)


def test_discovery_tools_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    _app, registry, _settings_obj = _build_app(monkeypatch, transport=_fixture_transport())
    assert set(registry) == {
        "apim_list_services",
        "apim_get_service",
        "apim_get_service_health",
    }


def test_list_services_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _registry, _settings_obj = _build_app(monkeypatch, transport=_fixture_transport())
    with TestClient(app) as client:
        started = time.monotonic()
        response = _call_tool(client, "apim_list_services", {"response_format": "json"})
        elapsed = time.monotonic() - started
    assert elapsed < 5.0  # Done-when: every tool returns quickly against fixtures (spec: <60s)
    payload = json.loads(_result_text(response))
    assert payload["count"] == 1
    item = payload["items"][0]
    assert item["alias"] == "prod"
    assert item["name"] == "apim-fixture"
    assert item["resourceGroup"] == "rg-fixture"
    assert item["hasLogAnalytics"] is False


def test_get_service_maps_fields_and_omits_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _registry, _settings_obj = _build_app(monkeypatch, transport=_fixture_transport())
    with TestClient(app) as client:
        response = _call_tool(
            client, "apim_get_service", {"service": "prod", "response_format": "json"}
        )
    text = _result_text(response)
    assert "encodedCertificate" not in text
    assert "certificatePassword" not in text
    payload = json.loads(text)
    assert payload["name"] == "apim-fixture"
    assert payload["sku"]
    assert payload["hostnameConfigurations"][0]["hostName"] == "apim-fixture.azure-api.net"


def test_get_service_unknown_alias_returns_invalid_input(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _registry, _settings_obj = _build_app(monkeypatch, transport=_fixture_transport())
    with TestClient(app) as client:
        response = _call_tool(
            client, "apim_get_service", {"service": "does-not-exist", "response_format": "json"}
        )
    payload = json.loads(_result_text(response))
    assert payload["error"]["kind"] == "invalid_input"


def test_cert_expiry_computed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Done-when: `daysUntilExpiry` is computed correctly for a hostname
    that does carry a `certificate.expiry` (the real recorded fixture's
    only hostname uses the built-in cert, which has none)."""
    from datetime import UTC, datetime, timedelta

    # A one-hour buffer keeps this deterministic: normal test overhead is
    # milliseconds, nowhere near enough to shift the day count.
    expiry = (datetime.now(UTC) + timedelta(days=15, hours=1)).isoformat().replace("+00:00", "Z")
    body = {
        "id": RESOURCE_ID,
        "name": "apim-fixture",
        "location": "Central US",
        "sku": {"name": "Premium", "capacity": 1},
        "properties": {
            "provisioningState": "Succeeded",
            "platformVersion": "stv2.1",
            "hostnameConfigurations": [
                {
                    "hostName": "custom.example.com",
                    "certificateSource": "Custom",
                    "certificate": {
                        "expiry": expiry,
                        "thumbprint": "AAAA",
                        "subject": "CN=custom.example.com",
                    },
                    "encodedCertificate": "c2VjcmV0",
                    "certificatePassword": "hunter2",
                }
            ],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    app, _registry, _settings_obj = _build_app(monkeypatch, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = _call_tool(
            client, "apim_get_service", {"service": "prod", "response_format": "json"}
        )
    text = _result_text(response)
    assert "c2VjcmV0" not in text
    assert "hunter2" not in text
    payload = json.loads(text)
    hostname = payload["hostnameConfigurations"][0]
    assert hostname["expiry"] == expiry
    assert hostname["daysUntilExpiry"] == 15


def test_health_partial_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Done-when: when one sub-call fails, the others still return, and the
    failing section is marked `unavailable` rather than failing the whole
    tool."""
    fixture_transport = _fixture_transport()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/networkstatus"):
            return httpx.Response(500, text="synthetic upstream failure")
        return await fixture_transport.handle_async_request(request)

    app, _registry, _settings_obj = _build_app(monkeypatch, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = _call_tool(
            client, "apim_get_service_health", {"service": "prod", "response_format": "json"}
        )
    payload = json.loads(_result_text(response))
    assert payload["networkStatus"]["status"] == "unavailable"
    assert "reason" in payload["networkStatus"]
    assert payload["provisioningState"]["status"] == "ok"
    assert payload["provisioningState"]["provisioningState"] == "Succeeded"
    assert payload["capacityMetric"] == {
        "status": "ok",
        "average": 55.0,
        "sampleCount": 4.0,
        "timespan": "PT1H",
    }
    assert payload["resourceHealth"]["status"] == "ok"
    assert payload["resourceHealth"]["availabilityState"] == "Available"
    assert payload["certificateExpiry"]["status"] == "ok"


def test_health_happy_path_within_latency_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _registry, _settings_obj = _build_app(monkeypatch, transport=_fixture_transport())
    with TestClient(app) as client:
        started = time.monotonic()
        response = _call_tool(
            client, "apim_get_service_health", {"service": "prod", "response_format": "json"}
        )
        elapsed = time.monotonic() - started
    assert elapsed < 5.0  # Done-when: returns well within the 60s ceiling
    payload = json.loads(_result_text(response))
    assert payload["provisioningState"]["status"] == "ok"
    assert payload["resourceHealth"]["status"] == "ok"
    assert payload["networkStatus"]["status"] == "ok"
    # The recorded fixture has one failing dependency ("Scm") - proving the
    # network-status section surfaces failures rather than only successes.
    failing_names = {d["name"] for d in payload["networkStatus"]["failingDependencies"]}
    assert "Scm" in failing_names


def test_markdown_response_never_contains_certificate_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _registry, _settings_obj = _build_app(monkeypatch, transport=_fixture_transport())
    with TestClient(app) as client:
        response = _call_tool(client, "apim_get_service", {"service": "prod"})
    text = _result_text(response)
    assert "encodedCertificate" not in text
    assert "certificatePassword" not in text


def test_audit_events_emitted_for_all_three_tools(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from apim_mcp.common.telemetry import AUDIT_LOGGER_NAME

    app, registry, _settings_obj = _build_app(monkeypatch, transport=_fixture_transport())
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER_NAME), TestClient(app) as client:
        _call_tool(client, "apim_list_services", {})
        _call_tool(client, "apim_get_service", {"service": "prod"})
        _call_tool(client, "apim_get_service_health", {"service": "prod"})

    audit_records = [r for r in caplog.records if r.name == AUDIT_LOGGER_NAME]
    logged_tools = {json.loads(r.message)["tool"] for r in audit_records}
    assert logged_tools == set(registry)
