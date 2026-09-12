"""Tests for src/apim_mcp/server.py and src/apim_mcp/common/telemetry.py (T-09).

See docs/SPEC.md §9 (audit event) and §4.2 (permission canary).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

import apim_mcp.clients.arm as arm_module
from apim_mcp.auth.context import CallContext
from apim_mcp.auth.middleware import (
    MCP_DELEGATED_SCOPE,
    OAUTH_AUTHORIZATION_SERVER_PATH,
    OAUTH_PROTECTED_RESOURCE_MCP_PATH,
    OPENID_CONFIGURATION_PATH,
)
from apim_mcp.clients.arm import ArmClient
from apim_mcp.common.errors import ToolError, upstream_error
from apim_mcp.common.telemetry import AUDIT_LOGGER_NAME, run_permission_canary
from apim_mcp.server import (
    ToolRegistration,
    audited_tool,
    create_app,
    create_mcp,
    wrap_with_middleware,
)
from apim_mcp.settings import ApimServiceConfig, Settings

TENANT_ID = "11111111-1111-1111-1111-111111111111"
AUDIENCE = "http://testserver/mcp"
REQUIRED_ROLE = "Apim.Read"
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
KID = "server-test-kid"

_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(_private_key.public_key(), as_dict=True)
_public_jwk["kid"] = KID
_public_jwk["use"] = "sig"
JWKS_BODY: dict[str, Any] = {"keys": [_public_jwk]}

RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000"
    "/resourceGroups/rg-fixture"
    "/providers/Microsoft.ApiManagement/service/apim-fixture"
)

REQUEST_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _settings() -> Settings:
    return Settings(
        azure_tenant_id=TENANT_ID,
        azure_client_id="22222222-2222-2222-2222-222222222222",
        mcp_server_audience=AUDIENCE,
        mcp_server_app_id=AUDIENCE,
        mcp_required_role=REQUIRED_ROLE,
        apim_services=[],
        applicationinsights_connection_string=(
            "InstrumentationKey=00000000-0000-0000-0000-000000000000"
        ),
    )


def _token(*, roles: list[str] | None = None) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": now + 3600,
        "nbf": now - 10,
        "iat": now,
        "oid": "caller-oid",
        "preferred_username": "caller@example.com",
        "roles": roles if roles is not None else [REQUIRED_ROLE],
    }
    return jwt.encode(claims, _private_key, algorithm="RS256", headers={"kid": KID})


def _jwks_cache() -> Any:
    from apim_mcp.auth.middleware import JWKSCache

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=JWKS_BODY)

    return JWKSCache(TENANT_ID, transport=httpx.MockTransport(handler))


def _rpc(method: str, params: dict[str, Any], *, req_id: int = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}


def _init_params() -> dict[str, Any]:
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "0.1"},
    }


def test_healthz_and_readyz_available_without_auth() -> None:
    mcp = create_mcp(_settings(), allowed_hosts=["testserver"])
    app = wrap_with_middleware(mcp, _settings())
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200


@pytest.mark.parametrize(
    "audience",
    [
        "http://localhost:8000/mcp",
        "https://host.example/mcp",
    ],
)
def test_oauth_protected_resource_metadata(audience: str) -> None:
    """§10.2: VS Code discovers the Entra tenant to authenticate against
    from this endpoint, without a hand-configured header. Must be reachable
    without a token, must be served at both the root and RFC 9728
    path-suffixed route, must name `MCP_SERVER_AUDIENCE` exactly as the
    resource (no re-derivation from the request), and must advertise the
    fully-qualified delegated scope derived from that audience."""
    settings = _settings().model_copy(update={"mcp_server_audience": audience})
    mcp = create_mcp(settings, allowed_hosts=["testserver"])
    app = wrap_with_middleware(mcp, settings)
    with TestClient(app) as client:
        for path in ("/.well-known/oauth-protected-resource", OAUTH_PROTECTED_RESOURCE_MCP_PATH):
            response = client.get(path)
            assert response.status_code == 200, path
            body = response.json()
            assert body["resource"] == audience
            assert body["authorization_servers"] == [
                f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
            ]
            assert body["scopes_supported"] == [f"{audience}/{MCP_DELEGATED_SCOPE}"]
            assert body["bearer_methods_supported"] == ["header"]


_FAKE_ENTRA_AS_METADATA = {
    "issuer": ISSUER,
    "authorization_endpoint": f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/authorize",
    "token_endpoint": f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
    "jwks_uri": f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys",
}


def test_authorization_server_metadata_mirror_matches_entra_verbatim() -> None:
    """§10.2.1: workaround for a VS Code MCP client discovery bug. Both
    well-known paths must be unauthenticated and must return Entra's own
    document verbatim - never a fabricated one, and never a
    `registration_endpoint` (this server implements no DCR)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_FAKE_ENTRA_AS_METADATA)

    settings = _settings()
    mcp = create_mcp(
        settings,
        allowed_hosts=["testserver"],
        auth_metadata_transport=httpx.MockTransport(handler),
    )
    app = wrap_with_middleware(mcp, settings)
    with TestClient(app) as client:
        for path in (OAUTH_AUTHORIZATION_SERVER_PATH, OPENID_CONFIGURATION_PATH):
            response = client.get(path)
            assert response.status_code == 200, path
            body = response.json()
            assert body == _FAKE_ENTRA_AS_METADATA
            assert "registration_endpoint" not in body


def test_authorization_server_metadata_fetch_failure_returns_503() -> None:
    """A transient fetch failure is a result, not a crash (docs/PRINCIPLES.md §8)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    settings = _settings()
    mcp = create_mcp(
        settings,
        allowed_hosts=["testserver"],
        auth_metadata_transport=httpx.MockTransport(handler),
    )
    app = wrap_with_middleware(mcp, settings)
    with TestClient(app) as client:
        response = client.get(OAUTH_AUTHORIZATION_SERVER_PATH)
    assert response.status_code == 503


def test_server_responds_to_mcp_initialize() -> None:
    """Server starts and responds to MCP `initialize` (T-09 Done-when #1)."""
    settings = _settings()
    mcp = create_mcp(settings, allowed_hosts=["testserver"])
    app = wrap_with_middleware(mcp, settings, jwks_cache=_jwks_cache())

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            json=_rpc("initialize", _init_params()),
            headers={**REQUEST_HEADERS, "Authorization": f"Bearer {_token()}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["serverInfo"]["name"] == "apim-mcp"


def _build_app_with_tools() -> tuple[Any, list[ToolRegistration], Settings]:
    settings = _settings()
    mcp = create_mcp(settings, allowed_hosts=["testserver"])
    registry: list[ToolRegistration] = []

    @audited_tool(mcp, registry, name="dummy_ok")
    async def dummy_ok(*, ctx: CallContext, service: str) -> dict[str, Any]:
        return {"service_seen": service, "caller": ctx.oid}

    @audited_tool(mcp, registry, name="dummy_error")
    async def dummy_error(*, ctx: CallContext) -> dict[str, Any] | ToolError:
        return upstream_error(log_detail="synthetic failure for testing")

    @audited_tool(mcp, registry, name="dummy_boom")
    async def dummy_boom(*, ctx: CallContext) -> dict[str, Any]:
        raise RuntimeError("boom - this must never reach the transport")

    app = wrap_with_middleware(mcp, settings, jwks_cache=_jwks_cache())
    return app, registry, settings


def _call_tool(client: TestClient, name: str, arguments: dict[str, Any]) -> httpx.Response:
    response: httpx.Response = client.post(
        "/mcp",
        json=_rpc("tools/call", {"name": name, "arguments": arguments}),
        headers={**REQUEST_HEADERS, "Authorization": f"Bearer {_token()}"},
    )
    return response


def test_every_tool_emits_audit_event(caplog: pytest.LogCaptureFixture) -> None:
    app, registry, _ = _build_app_with_tools()
    assert {r.name for r in registry} == {"dummy_ok", "dummy_error", "dummy_boom"}

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER_NAME), TestClient(app) as client:
        client.post(
            "/mcp",
            json=_rpc("initialize", _init_params()),
            headers={**REQUEST_HEADERS, "Authorization": f"Bearer {_token()}"},
        )
        for reg in registry:
            arguments = {"service": "prod"} if reg.name == "dummy_ok" else {}
            _call_tool(client, reg.name, arguments)

    audit_records = [r for r in caplog.records if r.name == AUDIT_LOGGER_NAME]
    assert len(audit_records) == len(registry)
    logged_tools = {json.loads(r.message)["tool"] for r in audit_records}
    assert logged_tools == {reg.name for reg in registry}


def test_audit_event_contains_all_fields_and_no_response_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, _registry, _ = _build_app_with_tools()

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER_NAME), TestClient(app) as client:
        response = _call_tool(client, "dummy_ok", {"service": "prod"})

    assert "service_seen" in response.text  # the actual tool response did contain it

    audit_records = [r for r in caplog.records if r.name == AUDIT_LOGGER_NAME]
    assert len(audit_records) == 1
    event = json.loads(audit_records[0].message)

    for field in (
        "event",
        "caller_oid",
        "caller_upn",
        "caller_roles",
        "tool",
        "arguments",
        "service",
        "outcome",
        "duration_ms",
        "result_bytes",
        "truncated",
    ):
        assert field in event, field

    assert event["tool"] == "dummy_ok"
    assert event["arguments"] == {"service": "prod"}
    assert event["service"] == "prod"
    assert event["outcome"] == "ok"
    assert event["caller_oid"] == "caller-oid"
    assert event["caller_upn"] == "caller@example.com"
    assert event["caller_roles"] == [REQUIRED_ROLE]
    # The response body itself (e.g. the echoed service_seen value) must
    # never appear in the audit record - only its size.
    assert "service_seen" not in json.dumps(event)


def test_stray_exception_becomes_upstream_error_and_logs_at_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, _, _ = _build_app_with_tools()

    with caplog.at_level(logging.ERROR, logger="apim_mcp.server"), TestClient(app) as client:
        response = _call_tool(client, "dummy_boom", {})

    assert response.status_code == 200  # MCP-level success; failure is inside the result
    body = response.json()
    text = body["result"]["content"][0]["text"]
    parsed = json.loads(text)
    assert parsed["error"]["kind"] == "upstream_error"

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("dummy_boom" in r.getMessage() for r in error_records)


def test_declared_error_result_is_not_treated_as_a_raised_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`dummy_error` returns a `ToolError` deliberately - that is not a
    stray exception and must not be logged at ERROR."""
    app, _, _ = _build_app_with_tools()

    with caplog.at_level(logging.ERROR, logger="apim_mcp.server"), TestClient(app) as client:
        response = _call_tool(client, "dummy_error", {})

    assert response.status_code == 200
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records == []


class _FakeToken:
    def __init__(self, token: str) -> None:
        self.token = token


class _FakeCredential:
    async def get_token(self, *scopes: str) -> _FakeToken:
        return _FakeToken("fake-token")


def _service(alias: str) -> ApimServiceConfig:
    return ApimServiceConfig(alias=alias, resource_id=RESOURCE_ID)


async def test_permission_canary_flags_secret_bearing_actions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(arm_module, "credential_for", lambda ctx, scope: _FakeCredential())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "actions": [
                            "Microsoft.ApiManagement/service/read",
                            "Microsoft.ApiManagement/service/listSecrets/action",
                        ]
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    original_client = ArmClient

    def patched(ctx: CallContext, **kwargs: Any) -> ArmClient:
        return original_client(ctx, transport=transport)

    monkeypatch.setattr("apim_mcp.common.telemetry.ArmClient", patched)

    ctx = CallContext(oid="oid", upn="u@example.com", roles=(), bearer_token="tok")
    with caplog.at_level(logging.WARNING, logger="apim_mcp.permission_canary"):
        findings = await run_permission_canary(ctx, [_service("prod")])

    assert findings["prod"] == ["Microsoft.ApiManagement/service/listSecrets/action"]
    assert any("secret-bearing" in r.getMessage() for r in caplog.records)


async def test_permission_canary_is_quiet_when_read_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(arm_module, "credential_for", lambda ctx, scope: _FakeCredential())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"value": [{"actions": ["Microsoft.ApiManagement/service/read"]}]}
        )

    transport = httpx.MockTransport(handler)
    original_client = ArmClient

    def patched(ctx: CallContext, **kwargs: Any) -> ArmClient:
        return original_client(ctx, transport=transport)

    monkeypatch.setattr("apim_mcp.common.telemetry.ArmClient", patched)

    ctx = CallContext(oid="oid", upn="u@example.com", roles=(), bearer_token="tok")
    with caplog.at_level(logging.WARNING, logger="apim_mcp.permission_canary"):
        findings = await run_permission_canary(ctx, [_service("prod")])

    assert findings["prod"] == []
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


def test_permission_canary_runs_at_startup_and_logs_findings(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Permission canary runs at startup and logs its findings (T-09
    Done-when #5) - driven through the real ASGI lifespan, not called
    directly, so this also proves `create_app` wires it up."""
    monkeypatch.setattr(arm_module, "credential_for", lambda ctx, scope: _FakeCredential())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"value": [{"actions": ["Microsoft.ApiManagement/service/listSecrets/action"]}]},
        )

    transport = httpx.MockTransport(handler)
    original_client = ArmClient

    def patched(ctx: CallContext, **kwargs: Any) -> ArmClient:
        return original_client(ctx, transport=transport)

    monkeypatch.setattr("apim_mcp.common.telemetry.ArmClient", patched)

    settings = Settings(
        azure_tenant_id=TENANT_ID,
        azure_client_id="22222222-2222-2222-2222-222222222222",
        mcp_server_audience=AUDIENCE,
        mcp_server_app_id=AUDIENCE,
        mcp_required_role=REQUIRED_ROLE,
        apim_services=[_service("prod")],
        applicationinsights_connection_string=(
            "InstrumentationKey=00000000-0000-0000-0000-000000000000"
        ),
    )
    app = create_app(settings=settings, allowed_hosts=["testserver"])

    with (
        caplog.at_level(logging.WARNING, logger="apim_mcp.permission_canary"),
        TestClient(app),
    ):
        pass  # entering/exiting the context runs ASGI lifespan startup/shutdown

    assert any(
        "secret-bearing" in r.getMessage() and "prod" in r.getMessage() for r in caplog.records
    )
