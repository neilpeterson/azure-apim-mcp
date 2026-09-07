"""Tests for `apim_mcp.clients.apim` (T-14). See docs/SPEC.md §6 Group B
`apim_get_api_spec`.
"""

from __future__ import annotations

import json

import httpx
import pytest

import apim_mcp.clients.arm as arm_module
from apim_mcp.auth.context import CallContext
from apim_mcp.clients.apim import (
    SpecCacheKey,
    SpecDocumentCache,
    fetch_spec_document,
    spec_summary,
)
from apim_mcp.common.errors import ToolError

RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000"
    "/resourceGroups/rg-fixture"
    "/providers/Microsoft.ApiManagement/service/apim-fixture"
    "/apis/echo-api"
)

BLOB_LINK = "https://apimfixture.blob.core.windows.net/api-export/echo-api.json?sv=fake&sig=fake"

SAMPLE_SPEC: dict[str, object] = {
    "info": {"title": "Echo API", "version": "1.0"},
    "servers": [{"url": "https://apim-fixture.azure-api.net/echo"}],
    "components": {"securitySchemes": {"apiKeyHeader": {"type": "apiKey"}}},
    "paths": {
        "/inventory/{id}": {
            "get": {
                "operationId": "getInventoryLevels",
                "summary": "Get inventory levels",
                "parameters": [{"name": "id"}, {"name": "limit"}],
            }
        }
    },
}


class _FakeToken:
    def __init__(self, token: str) -> None:
        self.token = token


class _FakeCredential:
    async def get_token(self, *scopes: str) -> _FakeToken:
        return _FakeToken("fake-token")


@pytest.fixture(autouse=True)
def _patch_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(arm_module, "credential_for", lambda ctx, scope: _FakeCredential())


def _ctx() -> CallContext:
    return CallContext(oid="user-oid", upn="user@example.com", roles=(), bearer_token="tok")


def _export_handler(*, link: str = BLOB_LINK, shape: str = "bare") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("export") == "true"
        if shape == "bare":
            return httpx.Response(200, json={"link": link})
        return httpx.Response(200, json={"format": "openapi+json-link", "value": {"link": link}})

    return httpx.MockTransport(handler)


def _blob_handler(
    *, body: str | None = None, status: int = 200, calls: list[httpx.Request] | None = None
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(status, text=body if body is not None else json.dumps(SAMPLE_SPEC))

    return httpx.MockTransport(handler)


async def test_no_auth_header_on_blob_fetch() -> None:
    """The blob fetch is a plain unauthenticated GET - the SAS query
    string is the credential, and a bearer token would make it fail."""
    calls: list[httpx.Request] = []
    result = await fetch_spec_document(
        _ctx(),
        RESOURCE_ID,
        format="openapi_json",
        arm_transport=_export_handler(),
        blob_transport=_blob_handler(calls=calls),
    )

    assert not isinstance(result, ToolError)
    assert len(calls) == 1
    header_names = {name.lower() for name in calls[0].headers}
    assert "authorization" not in header_names


async def test_export_response_shape_both_documented_and_actual() -> None:
    """A real APIM instance returns a bare `{"link": ...}`, not the
    documented `{"value": {"link": ...}}` - both must work."""
    for shape in ("bare", "documented"):
        result = await fetch_spec_document(
            _ctx(),
            RESOURCE_ID,
            format="openapi_json",
            arm_transport=_export_handler(shape=shape),
            blob_transport=_blob_handler(),
        )
        assert result == SAMPLE_SPEC


async def test_link_is_never_cached() -> None:
    """`SpecDocumentCache` only ever stores what `fetch_spec_document`
    returns (the parsed document) - it has no way to store a link even by
    accident, because `fetch_spec_document` never returns one."""
    result = await fetch_spec_document(
        _ctx(),
        RESOURCE_ID,
        format="openapi_json",
        arm_transport=_export_handler(),
        blob_transport=_blob_handler(),
    )
    assert isinstance(result, dict)
    assert "link" not in result
    assert result == SAMPLE_SPEC

    cache = SpecDocumentCache(ttl_seconds=900)
    key = SpecCacheKey(oid="user-oid", service="prod", api_id="echo-api", format="openapi_json")
    cache.put(key, result)
    cached = cache.get(key)
    assert cached is not None
    assert "link" not in cached


async def test_expired_sas_triggers_reexport() -> None:
    """A blob-fetch failure (an expired SAS) triggers exactly one
    re-export and retry, not an immediate failure."""
    export_calls = 0

    def export_handler(request: httpx.Request) -> httpx.Response:
        nonlocal export_calls
        export_calls += 1
        return httpx.Response(200, json={"link": f"{BLOB_LINK}&attempt={export_calls}"})

    blob_calls: list[httpx.Request] = []

    def blob_handler(request: httpx.Request) -> httpx.Response:
        blob_calls.append(request)
        if len(blob_calls) == 1:
            # Simulate an expired SAS: blob storage returns 403.
            return httpx.Response(403, text="AuthenticationFailed")
        return httpx.Response(200, text=json.dumps(SAMPLE_SPEC))

    result = await fetch_spec_document(
        _ctx(),
        RESOURCE_ID,
        format="openapi_json",
        arm_transport=httpx.MockTransport(export_handler),
        blob_transport=httpx.MockTransport(blob_handler),
    )

    assert result == SAMPLE_SPEC
    assert export_calls == 2
    assert len(blob_calls) == 2


async def test_expired_sas_gives_up_after_max_attempts() -> None:
    def blob_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="AuthenticationFailed")

    result = await fetch_spec_document(
        _ctx(),
        RESOURCE_ID,
        format="openapi_json",
        arm_transport=_export_handler(),
        blob_transport=httpx.MockTransport(blob_handler),
    )

    assert isinstance(result, ToolError)
    assert result.kind == "upstream_error"


async def test_export_failure_is_graceful() -> None:
    """A SOAP/GraphQL API that cannot export must return a typed error,
    not raise - ARM reports this as an error response on the export call
    itself, before any blob fetch is attempted."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": "ExportNotSupported"}})

    result = await fetch_spec_document(
        _ctx(),
        RESOURCE_ID,
        format="openapi_json",
        arm_transport=httpx.MockTransport(handler),
        blob_transport=_blob_handler(),
    )

    assert isinstance(result, ToolError)
    assert result.kind == "invalid_input"


async def test_unparseable_document_is_graceful() -> None:
    result = await fetch_spec_document(
        _ctx(),
        RESOURCE_ID,
        format="openapi_json",
        arm_transport=_export_handler(),
        blob_transport=_blob_handler(body="not json at all"),
    )
    assert isinstance(result, ToolError)
    assert result.kind == "upstream_error"


async def test_openapi_yaml_format_is_parsed() -> None:
    yaml_body = "info:\n  title: Echo API\npaths: {}\n"
    result = await fetch_spec_document(
        _ctx(),
        RESOURCE_ID,
        format="openapi_yaml",
        arm_transport=_export_handler(),
        blob_transport=_blob_handler(body=yaml_body),
    )
    assert result == {"info": {"title": "Echo API"}, "paths": {}}


def test_spec_cache_key_includes_oid() -> None:
    fields = set(SpecCacheKey.__dataclass_fields__)
    assert "oid" in fields


def test_spec_cache_scoped_by_key() -> None:
    clock_value = [0.0]
    cache = SpecDocumentCache(ttl_seconds=10, clock=lambda: clock_value[0])
    key_a = SpecCacheKey(oid="user-a", service="prod", api_id="echo-api", format="openapi_json")
    key_b = SpecCacheKey(oid="user-b", service="prod", api_id="echo-api", format="openapi_json")

    cache.put(key_a, {"doc": "a"})
    assert cache.get(key_a) == {"doc": "a"}
    assert cache.get(key_b) is None

    clock_value[0] = 11.0
    assert cache.get(key_a) is None


def test_spec_summary_extracts_info_servers_security_and_paths() -> None:
    summary = spec_summary(SAMPLE_SPEC)

    assert summary["info"] == SAMPLE_SPEC["info"]
    assert summary["servers"] == SAMPLE_SPEC["servers"]
    assert summary["securitySchemes"] == ["apiKeyHeader"]
    assert summary["paths"] == [
        {
            "path": "/inventory/{id}",
            "method": "GET",
            "operationId": "getInventoryLevels",
            "summary": "Get inventory levels",
            "parameters": ["id", "limit"],
        }
    ]


def test_spec_summary_reads_swagger_security_definitions() -> None:
    swagger_doc = {
        "info": {"title": "Legacy"},
        "securityDefinitions": {"apiKey": {"type": "apiKey"}},
        "paths": {},
    }
    summary = spec_summary(swagger_doc)
    assert summary["securitySchemes"] == ["apiKey"]
