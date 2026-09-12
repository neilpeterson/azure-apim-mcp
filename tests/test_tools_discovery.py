"""Tests for src/apim_mcp/tools/discovery.py (T-10).

See docs/SPEC.md §6.0 (tool conventions) and §6 Group A.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

import apim_mcp.clients.arm as arm_module
from _fixture_transport import FixtureTransport
from apim_mcp.auth.context import CallContext
from apim_mcp.clients.arm import ArmClient
from apim_mcp.server import ToolRegistration, create_mcp, wrap_with_middleware
from apim_mcp.settings import ApimServiceConfig, Settings
from apim_mcp.tools.discovery import register_discovery_tools

TENANT_ID = "11111111-1111-1111-1111-111111111111"
AUDIENCE = "http://localhost:8000/mcp"
REQUIRED_ROLE = "Apim.Read"
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
KID = "discovery-test-kid"

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000"
    "/resourceGroups/rg-fixture"
    "/providers/Microsoft.ApiManagement/service/apim-fixture"
)

REQUEST_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(_private_key.public_key(), as_dict=True)
_public_jwk["kid"] = KID
_public_jwk["use"] = "sig"
JWKS_BODY: dict[str, Any] = {"keys": [_public_jwk]}


class _FakeToken:
    def __init__(self, token: str) -> None:
        self.token = token


class _FakeCredential:
    async def get_token(self, *scopes: str) -> _FakeToken:
        return _FakeToken("fake-token")


def _service(alias: str) -> ApimServiceConfig:
    return ApimServiceConfig(alias=alias, resource_id=RESOURCE_ID)


def _settings(*, services: list[ApimServiceConfig] | None = None) -> Settings:
    return Settings(
        azure_tenant_id=TENANT_ID,
        azure_client_id="22222222-2222-2222-2222-222222222222",
        mcp_server_audience=AUDIENCE,
        mcp_server_app_id=AUDIENCE,
        mcp_required_role=REQUIRED_ROLE,
        apim_services=services if services is not None else [_service("prod")],
        applicationinsights_connection_string=(
            "InstrumentationKey=00000000-0000-0000-0000-000000000000"
        ),
    )


def _token() -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": now + 3600,
        "nbf": now - 10,
        "iat": now,
        "oid": "caller-oid",
        "preferred_username": "caller@example.com",
        "roles": [REQUIRED_ROLE],
    }
    return jwt.encode(claims, _private_key, algorithm="RS256", headers={"kid": KID})


def _jwks_cache() -> Any:
    from apim_mcp.auth.middleware import JWKSCache

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=JWKS_BODY)

    return JWKSCache(TENANT_ID, transport=httpx.MockTransport(handler))


def _rpc(method: str, params: dict[str, Any], *, req_id: int = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}


def _build_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    transport: httpx.AsyncBaseTransport,
    services: list[ApimServiceConfig] | None = None,
) -> tuple[Any, list[ToolRegistration], Settings]:
    """Build the real discovery-tool app, with `ArmClient` calls served
    from `transport` instead of the network."""
    monkeypatch.setattr(arm_module, "credential_for", lambda ctx, scope: _FakeCredential())

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
    response: httpx.Response = client.post(
        "/mcp",
        json=_rpc("tools/call", {"name": name, "arguments": arguments}),
        headers={**REQUEST_HEADERS, "Authorization": f"Bearer {_token()}"},
    )
    return response


def _result_text(response: httpx.Response) -> str:
    body = response.json()
    text: str = body["result"]["content"][0]["text"]
    return text


def _fixture_transport() -> FixtureTransport:
    return FixtureTransport(FIXTURES_DIR)


def test_discovery_tools_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    _app, registry, _settings_obj = _build_app(monkeypatch, transport=_fixture_transport())
    assert {r.name for r in registry} == {
        "apim_list_services",
        "apim_get_service",
        "apim_get_service_health",
    }


def test_tools_have_correct_annotations(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _registry, _settings_obj = _build_app(monkeypatch, transport=_fixture_transport())
    with TestClient(app) as client:
        client.post(
            "/mcp",
            json=_rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}}),
            headers={**REQUEST_HEADERS, "Authorization": f"Bearer {_token()}"},
        )
        response = client.post(
            "/mcp",
            json=_rpc("tools/list", {}, req_id=2),
            headers={**REQUEST_HEADERS, "Authorization": f"Bearer {_token()}"},
        )
    tools = {t["name"]: t for t in response.json()["result"]["tools"]}
    assert set(tools) == {"apim_list_services", "apim_get_service", "apim_get_service_health"}
    for tool in tools.values():
        annotations = tool["annotations"]
        assert annotations["readOnlyHint"] is True
        assert annotations["destructiveHint"] is False
        assert annotations["idempotentHint"] is True
        assert annotations["openWorldHint"] is True


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
    assert logged_tools == {r.name for r in registry}
