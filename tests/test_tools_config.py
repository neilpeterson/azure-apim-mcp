"""Tests for src/apim_mcp/tools/config.py (T-13).

See docs/development/SPEC.md §6.0 (tool conventions) and §6 Group B.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

import apim_mcp.clients.arm as arm_module
from _fixture_transport import FixtureTransport
from _mcp_harness import (
    REQUEST_HEADERS,
)
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
    rpc as _rpc,
)
from _mcp_harness import (
    settings as _base_settings,
)
from _mcp_harness import (
    token as _token,
)
from apim_mcp.auth.context import CallContext
from apim_mcp.clients.arm import ArmClient
from apim_mcp.common.errors import ToolError, not_found
from apim_mcp.server import ToolRegistration, create_mcp, wrap_with_middleware
from apim_mcp.settings import ApimServiceConfig, Settings
from apim_mcp.tools.config import register_config_tools

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


def _service(alias: str) -> ApimServiceConfig:
    return ApimServiceConfig(alias=alias, resource_id=RESOURCE_ID)


def _settings() -> Settings:
    return _base_settings(services=[_service("prod")])


def _build_app(
    monkeypatch: pytest.MonkeyPatch, *, transport: httpx.AsyncBaseTransport
) -> tuple[Any, list[ToolRegistration], Settings]:
    """Build the real config-tool app, with `ArmClient` calls served from
    `transport` instead of the network."""
    monkeypatch.setattr(arm_module, "credential_for", lambda ctx, scope: _FakeCredential())

    original_client = ArmClient

    def patched(ctx: CallContext, **kwargs: Any) -> ArmClient:
        return original_client(ctx, transport=transport)

    monkeypatch.setattr("apim_mcp.tools.config.ArmClient", patched)

    settings = _settings()
    mcp = create_mcp(settings, allowed_hosts=["testserver"])
    registry: list[ToolRegistration] = []
    register_config_tools(mcp, registry, settings)
    app = wrap_with_middleware(mcp, settings, jwks_cache=_jwks_cache())
    return app, registry, settings


def _call_tool(client: TestClient, name: str, arguments: dict[str, Any]) -> httpx.Response:
    return _shared_call_tool(client, name, arguments)


def _result_text(response: httpx.Response) -> str:
    return _shared_result_text(response)


def _fixture_transport() -> FixtureTransport:
    return FixtureTransport(FIXTURES_DIR)


def test_config_tools_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    _app, registry, _settings_obj = _build_app(monkeypatch, transport=_fixture_transport())
    assert set(registry) == {
        "apim_list_apis",
        "apim_get_api",
        "apim_get_policy",
        "apim_list_products",
        "apim_list_backends",
        "apim_list_named_values",
        "apim_list_subscriptions",
        "apim_get_api_spec",
    }


def test_tool_descriptions_state_what_is_not_returned(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert "never" in tools["apim_list_named_values"]["description"].lower()
    assert "never" in tools["apim_list_subscriptions"]["description"].lower()
    assert "never" in tools["apim_list_backends"]["description"].lower()


def test_list_apis_excludes_revisions_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {
        "value": [
            {
                "id": f"{RESOURCE_ID}/apis/echo-api",
                "type": "Microsoft.ApiManagement/service/apis",
                "name": "echo-api",
                "properties": {"displayName": "Echo API", "path": "echo", "isCurrent": True},
            },
            {
                "id": f"{RESOURCE_ID}/apis/echo-api;rev=1",
                "type": "Microsoft.ApiManagement/service/apis",
                "name": "echo-api;rev=1",
                "properties": {
                    "displayName": "Echo API (old revision)",
                    "path": "echo",
                    "isCurrent": False,
                },
            },
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    app, _registry, _settings_obj = _build_app(monkeypatch, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        default_response = _call_tool(
            client, "apim_list_apis", {"service": "prod", "response_format": "json"}
        )
        all_revisions_response = _call_tool(
            client,
            "apim_list_apis",
            {"service": "prod", "include_revisions": True, "response_format": "json"},
        )
    default_payload = json.loads(_result_text(default_response))
    all_revisions_payload = json.loads(_result_text(all_revisions_response))
    assert default_payload["total"] == 1
    assert default_payload["items"][0]["name"] == "echo-api"
    assert all_revisions_payload["total"] == 2


def test_list_apis_happy_path_from_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _registry, _settings_obj = _build_app(monkeypatch, transport=_fixture_transport())
    with TestClient(app) as client:
        response = _call_tool(
            client, "apim_list_apis", {"service": "prod", "response_format": "json"}
        )
    payload = json.loads(_result_text(response))
    assert payload["total"] == 2
    names = {item["name"] for item in payload["items"]}
    assert names == {"echo-api", "hello-web"}
    assert all(item["operationCount"] is None for item in payload["items"])


def test_list_apis_filter_matches_path_or_name(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _registry, _settings_obj = _build_app(monkeypatch, transport=_fixture_transport())
    with TestClient(app) as client:
        response = _call_tool(
            client,
            "apim_list_apis",
            {"service": "prod", "filter": "hello", "response_format": "json"},
        )
    payload = json.loads(_result_text(response))
    assert payload["total"] == 1
    assert payload["items"][0]["name"] == "hello-web"


def test_get_api_includes_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recorder never captured a single-API-entity fetch (only the list,
    operations, and export targets - see `scripts/record_fixtures.py`), so
    this composes the real recorded `apis_list` entry for `echo-api` with
    the real recorded `operations_echo-api` fixture, rather than fetching
    both through `FixtureTransport`."""
    apis_list = json.loads((FIXTURES_DIR / "apis_list.json").read_text())
    echo_api = next(item for item in apis_list["value"] if item["name"] == "echo-api")
    operations = json.loads((FIXTURES_DIR / "operations_echo-api.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/operations"):
            return httpx.Response(200, json=operations)
        return httpx.Response(200, json=echo_api)

    app, _registry, _settings_obj = _build_app(monkeypatch, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = _call_tool(
            client,
            "apim_get_api",
            {"service": "prod", "api_id": "echo-api", "response_format": "json"},
        )
    payload = json.loads(_result_text(response))
    assert payload["name"] == "echo-api"
    assert payload["truncated"] is False
    assert len(payload["operations"]) == 6
    methods = {op["method"] for op in payload["operations"]}
    assert "GET" in methods


def test_get_api_truncates_over_100_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    api_body = {
        "id": f"{RESOURCE_ID}/apis/big-api",
        "type": "Microsoft.ApiManagement/service/apis",
        "name": "big-api",
        "properties": {"displayName": "Big API", "path": "big", "isCurrent": True},
    }
    operations_body = {
        "value": [
            {
                "id": f"{RESOURCE_ID}/apis/big-api/operations/op-{i}",
                "properties": {
                    "displayName": f"Op {i}",
                    "method": "GET",
                    "urlTemplate": f"/op-{i}",
                },
            }
            for i in range(150)
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/operations"):
            return httpx.Response(200, json=operations_body)
        return httpx.Response(200, json=api_body)

    app, _registry, _settings_obj = _build_app(monkeypatch, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = _call_tool(
            client,
            "apim_get_api",
            {"service": "prod", "api_id": "big-api", "response_format": "json"},
        )
    payload = json.loads(_result_text(response))
    assert payload["truncated"] is True
    assert len(payload["operations"]) == 100
    assert "apim_get_api_spec" in payload["hint"]


def test_get_api_unknown_id_returns_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "ResourceNotFound"}})

    app, _registry, _settings_obj = _build_app(monkeypatch, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = _call_tool(
            client,
            "apim_get_api",
            {"service": "prod", "api_id": "does-not-exist", "response_format": "json"},
        )
    payload = json.loads(_result_text(response))
    assert payload["error"]["kind"] == "not_found"


def test_get_policy_redacts_secrets_and_preserves_named_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real recorded `policies/policy` fixture was never captured (the
    non-prod instance had no global policy set) - see
    `scripts/record_fixtures.py`'s `policy_global` target and
    `tests/fixtures/README.md`. This constructs a response matching the
    documented ARM schema (`properties.value`/`properties.format`) instead
    of hand-writing a fixture file from a guessed shape."""
    policy_xml = (
        '<policies><inbound><set-header name="Authorization" exists-action="override">'
        "<value>Bearer abcSECRETtoken1234567890</value></set-header>"
        '<set-header name="X-Named" exists-action="override">'
        "<value>{{my-value}}</value></set-header>"
        "</inbound></policies>"
    )
    body = {
        "id": f"{RESOURCE_ID}/policies/policy",
        "type": "Microsoft.ApiManagement/service/policies",
        "name": "policy",
        "properties": {"value": policy_xml, "format": "rawxml"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("format") == "rawxml"
        return httpx.Response(200, json=body)

    app, _registry, _settings_obj = _build_app(monkeypatch, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = _call_tool(
            client,
            "apim_get_policy",
            {"service": "prod", "scope": "global", "response_format": "json"},
        )
    payload = json.loads(_result_text(response))
    assert "abcSECRETtoken1234567890" not in payload["policyXml"]
    assert "[REDACTED:sensitive-header]" in payload["policyXml"]
    assert "{{my-value}}" in payload["policyXml"]


def test_get_policy_handles_bare_xml_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """`format=rawxml` is documented as returning a JSON-wrapped
    `PolicyContract`, but in practice ARM has been observed returning the
    policy as a bare XML document with no JSON envelope at all - this must
    not crash `response.json()` (regression test for the `JSONDecodeError:
    Expecting value: line 1 column 1 (char 0)` bug)."""
    policy_xml = (
        '<policies><inbound><set-header name="Authorization" exists-action="override">'
        "<value>abcSECRETtoken1234567890</value></set-header>"
        '<set-header name="X-Named" exists-action="override">'
        "<value>{{my-value}}</value></set-header>"
        "</inbound></policies>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("format") == "rawxml"
        return httpx.Response(200, text=policy_xml, headers={"content-type": "application/xml"})

    app, _registry, _settings_obj = _build_app(monkeypatch, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = _call_tool(
            client,
            "apim_get_policy",
            {"service": "prod", "scope": "global", "response_format": "json"},
        )
    payload = json.loads(_result_text(response))
    assert "abcSECRETtoken1234567890" not in payload["policyXml"]
    assert "[REDACTED:sensitive-header]" in payload["policyXml"]
    assert "{{my-value}}" in payload["policyXml"]


@pytest.mark.parametrize(
    ("scope", "arguments"),
    [
        ("api", {}),
        ("operation", {"api_id": "echo-api"}),
        ("product", {}),
    ],
)
def test_get_policy_requires_scope_specific_ids(
    monkeypatch: pytest.MonkeyPatch, scope: str, arguments: dict[str, str]
) -> None:
    app, _registry, _settings_obj = _build_app(monkeypatch, transport=_fixture_transport())
    with TestClient(app) as client:
        response = _call_tool(
            client,
            "apim_get_policy",
            {"service": "prod", "scope": scope, "response_format": "json", **arguments},
        )
    payload = json.loads(_result_text(response))
    assert payload["error"]["kind"] == "invalid_input"


def test_list_products_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _registry, _settings_obj = _build_app(monkeypatch, transport=_fixture_transport())
    with TestClient(app) as client:
        response = _call_tool(
            client, "apim_list_products", {"service": "prod", "response_format": "json"}
        )
    payload = json.loads(_result_text(response))
    assert payload["total"] == 2
    names = {item["name"] for item in payload["items"]}
    assert names == {"starter", "unlimited"}


def test_backends_omit_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uses a synthetic payload (the real recorded `backends_list` fixture
    is empty on the non-prod instance) to prove `credentials` is stripped
    even if a source payload somehow carried one."""
    body = {
        "value": [
            {
                "id": f"{RESOURCE_ID}/backends/echo-backend",
                "name": "echo-backend",
                "properties": {
                    "url": "https://echo.example.com",
                    "protocol": "http",
                    "title": "Echo backend",
                    "description": "Backend for the echo API",
                    "tls": {"validateCertificateChain": True},
                    "credentials": {
                        "authorization": {"parameter": "hunter2", "scheme": "Basic"},
                        "header": {"X-Api-Key": ["super-secret-value"]},
                    },
                },
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    app, _registry, _settings_obj = _build_app(monkeypatch, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = _call_tool(
            client, "apim_list_backends", {"service": "prod", "response_format": "json"}
        )
    text = _result_text(response)
    assert "hunter2" not in text
    assert "super-secret-value" not in text
    assert "credentials" not in text
    payload = json.loads(text)
    assert payload["items"][0]["name"] == "echo-backend"
    assert payload["items"][0]["url"] == "https://echo.example.com"


def test_named_values_never_returns_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real recorded fixture already has a secret entry with no `value`
    key; this adds a synthetic entry where a `value` is somehow present
    despite `secret: true`, to prove the tool actively strips it rather
    than merely forwarding whatever ARM sent (PRINCIPLES §5 defence in
    depth), alongside a non-secret entry whose `value` does pass through."""
    body = {
        "value": [
            {
                "id": f"{RESOURCE_ID}/namedValues/leaky-secret",
                "name": "leaky-secret",
                "properties": {
                    "displayName": "Leaky secret",
                    "tags": None,
                    "secret": True,
                    "value": "this-must-never-appear",
                },
            },
            {
                "id": f"{RESOURCE_ID}/namedValues/plain-config",
                "name": "plain-config",
                "properties": {
                    "displayName": "Plain config",
                    "tags": ["config"],
                    "secret": False,
                    "value": "some-plain-value",
                },
            },
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    app, _registry, _settings_obj = _build_app(monkeypatch, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = _call_tool(
            client, "apim_list_named_values", {"service": "prod", "response_format": "json"}
        )
    text = _result_text(response)
    assert "this-must-never-appear" not in text
    payload = json.loads(text)
    items = {item["name"]: item for item in payload["items"]}
    assert items["leaky-secret"]["secret"] is True
    assert "value" not in items["leaky-secret"]
    assert items["plain-config"]["secret"] is False
    assert items["plain-config"]["value"] == "some-plain-value"


def test_named_values_fixture_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _registry, _settings_obj = _build_app(monkeypatch, transport=_fixture_transport())
    with TestClient(app) as client:
        response = _call_tool(
            client, "apim_list_named_values", {"service": "prod", "response_format": "json"}
        )
    payload = json.loads(_result_text(response))
    assert payload["total"] == 1
    assert payload["items"][0]["secret"] is True
    assert "value" not in payload["items"][0]


def test_subscriptions_never_return_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uses a synthetic payload with `primaryKey`/`secondaryKey` present
    (simulating an older `api-version` behaviour) to prove the tool
    actively strips them rather than depending solely on the pinned
    `api-version` (PRINCIPLES §5 defence in depth)."""
    body = {
        "value": [
            {
                "id": f"{RESOURCE_ID}/subscriptions/sub-1",
                "name": "sub-1",
                "properties": {
                    "displayName": "Test subscription",
                    "scope": f"{RESOURCE_ID}/products/starter",
                    "state": "active",
                    "createdDate": "2026-01-01T00:00:00Z",
                    "ownerId": f"{RESOURCE_ID}/users/1",
                    "primaryKey": "should-never-appear-primary",
                    "secondaryKey": "should-never-appear-secondary",
                },
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    app, _registry, _settings_obj = _build_app(monkeypatch, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = _call_tool(
            client, "apim_list_subscriptions", {"service": "prod", "response_format": "json"}
        )
    text = _result_text(response)
    assert "should-never-appear-primary" not in text
    assert "should-never-appear-secondary" not in text
    payload = json.loads(text)
    assert payload["items"][0]["displayName"] == "Test subscription"


def test_subscriptions_fixture_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _registry, _settings_obj = _build_app(monkeypatch, transport=_fixture_transport())
    with TestClient(app) as client:
        response = _call_tool(
            client, "apim_list_subscriptions", {"service": "prod", "response_format": "json"}
        )
    payload = json.loads(_result_text(response))
    assert payload["total"] == 3
    names = {item["id"].rsplit("/", 1)[-1] for item in payload["items"]}
    assert "master" in names


def test_all_config_tools_return_within_latency_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _registry, _settings_obj = _build_app(monkeypatch, transport=_fixture_transport())
    with TestClient(app) as client:
        started = time.monotonic()
        _call_tool(client, "apim_list_apis", {"service": "prod"})
        _call_tool(client, "apim_get_api", {"service": "prod", "api_id": "echo-api"})
        _call_tool(client, "apim_list_products", {"service": "prod"})
        _call_tool(client, "apim_list_backends", {"service": "prod"})
        _call_tool(client, "apim_list_named_values", {"service": "prod"})
        _call_tool(client, "apim_list_subscriptions", {"service": "prod"})
        elapsed = time.monotonic() - started
    assert elapsed < 5.0  # Done-when: every tool returns quickly against fixtures (spec: <60s)


def test_audit_events_emitted_for_all_config_tools(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from apim_mcp.common.telemetry import AUDIT_LOGGER_NAME

    app, registry, _settings_obj = _build_app(monkeypatch, transport=_fixture_transport())
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER_NAME), TestClient(app) as client:
        _call_tool(client, "apim_list_apis", {"service": "prod"})
        _call_tool(client, "apim_get_api", {"service": "prod", "api_id": "echo-api"})
        _call_tool(client, "apim_list_products", {"service": "prod"})
        _call_tool(client, "apim_list_backends", {"service": "prod"})
        _call_tool(client, "apim_list_named_values", {"service": "prod"})
        _call_tool(client, "apim_list_subscriptions", {"service": "prod"})

    audit_records = [r for r in caplog.records if r.name == AUDIT_LOGGER_NAME]
    logged_tools = {json.loads(r.message)["tool"] for r in audit_records}
    assert logged_tools == set(registry) - {"apim_get_policy", "apim_get_api_spec"}


_SAMPLE_SPEC_DOCUMENT: dict[str, Any] = {
    "info": {"title": "Echo API", "version": "1.0"},
    "servers": [{"url": "https://apim-fixture.azure-api.net/echo"}],
    "components": {"securitySchemes": {"apiKeyHeader": {"type": "apiKey"}}},
    "paths": {
        "/inventory/{id}": {
            "get": {
                "operationId": "getInventoryLevels",
                "summary": "Get inventory levels",
                "parameters": [{"name": "id"}],
            }
        }
    },
}


def test_spec_tool_summary_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """`apim_get_api_spec` in `mode="summary"` (T-14): the tool-level
    behaviour (mode branching, rendering) built on top of the two-call
    fetch flow already covered by `tests/test_spec_export.py`."""
    app, _registry, _settings_obj = _build_app(monkeypatch, transport=_fixture_transport())

    calls: list[str] = []

    async def fake_fetch(
        ctx: CallContext,
        resource_id: str,
        *,
        format: str,  # noqa: A002
    ) -> dict[str, Any]:
        calls.append(resource_id)
        return _SAMPLE_SPEC_DOCUMENT

    monkeypatch.setattr("apim_mcp.tools.config.fetch_spec_document", fake_fetch)

    with TestClient(app) as client:
        response = _call_tool(
            client,
            "apim_get_api_spec",
            {"service": "prod", "api_id": "echo-api", "response_format": "json"},
        )
    payload = json.loads(_result_text(response))

    assert payload["mode"] == "summary"
    assert payload["info"]["title"] == "Echo API"
    assert payload["servers"] == _SAMPLE_SPEC_DOCUMENT["servers"]
    assert payload["securitySchemes"] == ["apiKeyHeader"]
    assert payload["paths"] == [
        {
            "path": "/inventory/{id}",
            "method": "GET",
            "operationId": "getInventoryLevels",
            "summary": "Get inventory levels",
            "parameters": ["id"],
        }
    ]
    assert len(calls) == 1
    assert calls[0].endswith("/apis/echo-api")


def test_spec_tool_full_mode_returns_raw_document(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _registry, _settings_obj = _build_app(monkeypatch, transport=_fixture_transport())

    async def fake_fetch(
        ctx: CallContext,
        resource_id: str,
        *,
        format: str,  # noqa: A002
    ) -> dict[str, Any]:
        return _SAMPLE_SPEC_DOCUMENT

    monkeypatch.setattr("apim_mcp.tools.config.fetch_spec_document", fake_fetch)

    with TestClient(app) as client:
        response = _call_tool(
            client,
            "apim_get_api_spec",
            {
                "service": "prod",
                "api_id": "echo-api",
                "mode": "full",
                "response_format": "json",
            },
        )
    payload = json.loads(_result_text(response))

    assert payload["mode"] == "full"
    assert payload["document"] == _SAMPLE_SPEC_DOCUMENT


def test_spec_tool_full_mode_falls_back_to_summary_over_size_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _registry, settings_obj = _build_app(monkeypatch, transport=_fixture_transport())
    settings_obj.max_response_bytes = 200

    huge_document = {
        "info": {"title": "Huge API"},
        "servers": [],
        "paths": {
            f"/op{i}": {"get": {"operationId": f"op{i}", "parameters": []}} for i in range(200)
        },
    }

    async def fake_fetch(
        ctx: CallContext,
        resource_id: str,
        *,
        format: str,  # noqa: A002
    ) -> dict[str, Any]:
        return huge_document

    monkeypatch.setattr("apim_mcp.tools.config.fetch_spec_document", fake_fetch)

    with TestClient(app) as client:
        response = _call_tool(
            client,
            "apim_get_api_spec",
            {
                "service": "prod",
                "api_id": "echo-api",
                "mode": "full",
                "response_format": "json",
            },
        )
    payload = json.loads(_result_text(response))

    assert payload["mode"] == "summary"
    assert payload["truncated"] is True
    assert "hint" in payload


def test_spec_tool_export_failure_returns_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _registry, _settings_obj = _build_app(monkeypatch, transport=_fixture_transport())

    async def failing_fetch(
        ctx: CallContext,
        resource_id: str,
        *,
        format: str,  # noqa: A002
    ) -> ToolError:
        return not_found("api spec", "echo-api", "prod")

    monkeypatch.setattr("apim_mcp.tools.config.fetch_spec_document", failing_fetch)

    with TestClient(app) as client:
        response = _call_tool(
            client, "apim_get_api_spec", {"service": "prod", "api_id": "echo-api"}
        )
    body = response.json()
    assert body["result"]["content"][0]["text"]
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["error"]["kind"] == "not_found"


def test_spec_tool_cache_is_scoped_by_oid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cache key includes `oid` (docs/development/PRINCIPLES.md §3): two calls with the
    same caller identity hit the cache; a different caller re-fetches."""
    app, _registry, _settings_obj = _build_app(monkeypatch, transport=_fixture_transport())

    calls: list[str] = []

    async def fake_fetch(
        ctx: CallContext,
        resource_id: str,
        *,
        format: str,  # noqa: A002
    ) -> dict[str, Any]:
        calls.append(ctx.oid)
        return _SAMPLE_SPEC_DOCUMENT

    monkeypatch.setattr("apim_mcp.tools.config.fetch_spec_document", fake_fetch)

    arguments = {"service": "prod", "api_id": "echo-api"}
    with TestClient(app) as client:
        for req_id, oid in enumerate(("caller-oid", "caller-oid", "other-oid"), start=1):
            client.post(
                "/mcp",
                json=_rpc(
                    "tools/call",
                    {"name": "apim_get_api_spec", "arguments": arguments},
                    req_id=req_id,
                ),
                headers={**REQUEST_HEADERS, "Authorization": f"Bearer {_token(oid=oid)}"},
            )

    assert calls == ["caller-oid", "other-oid"]
