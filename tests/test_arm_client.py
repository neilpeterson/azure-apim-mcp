"""Tests for ArmClient (T-06). See docs/SPEC.md §5.2."""

from __future__ import annotations

import inspect
from collections.abc import Callable

import httpx
import pytest

import apim_mcp.clients.arm as arm_module
from apim_mcp.auth.context import CallContext
from apim_mcp.clients.arm import ArmClient
from apim_mcp.common.errors import ToolError

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


@pytest.fixture(autouse=True)
def _patch_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(arm_module, "credential_for", lambda ctx, scope: _FakeCredential())


def _ctx() -> CallContext:
    return CallContext(oid="user-oid", upn="user@example.com", roles=(), bearer_token="tok")


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    sleep: arm_module.SleepFn | None = None,
) -> ArmClient:
    transport = httpx.MockTransport(handler)
    if sleep is not None:
        return ArmClient(_ctx(), transport=transport, sleep=sleep)
    return ArmClient(_ctx(), transport=transport)


async def test_get_returns_parsed_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer fake-token"
        assert "api-version" in request.url.params
        return httpx.Response(200, json={"name": "apim-fixture"})

    client = _client(handler)
    result = await client.get(RESOURCE_ID)
    assert result == {"name": "apim-fixture"}


async def test_follows_next_link() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                200,
                json={
                    "value": [{"id": "a"}],
                    "nextLink": "https://management.azure.com/fake/next2",
                },
            )
        if call_count == 2:
            return httpx.Response(
                200,
                json={
                    "value": [{"id": "b"}],
                    "nextLink": "https://management.azure.com/fake/next3",
                },
            )
        return httpx.Response(200, json={"value": [{"id": "c"}]})

    client = _client(handler)
    result = await client.list_all(RESOURCE_ID)

    assert [item["id"] for item in result.items] == ["a", "b", "c"]
    assert result.truncated is False
    assert result.error is None
    assert call_count == 3


async def test_respects_max_pages() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        body: dict[str, object] = {"value": [{"id": f"item-{call_count}"}]}
        if call_count < 5:
            body["nextLink"] = f"https://management.azure.com/fake/next{call_count + 1}"
        return httpx.Response(200, json=body)

    client = _client(handler)
    result = await client.list_all(RESOURCE_ID, max_pages=2)

    assert result.truncated is True
    assert len(result.items) == 2
    assert call_count == 2  # stopped after max_pages instead of looping forever


async def test_honours_retry_after() -> None:
    call_count = 0
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": "throttled"})
        return httpx.Response(200, json={"name": "apim-fixture"})

    client = _client(handler, sleep=fake_sleep)
    result = await client.get(RESOURCE_ID)

    assert result == {"name": "apim-fixture"}
    assert call_count == 2
    assert sleeps == [2.0]


async def test_403_returns_access_denied() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    client = _client(handler)
    result = await client.get(RESOURCE_ID)

    assert isinstance(result, ToolError)
    assert result.kind == "access_denied"


async def test_5xx_becomes_upstream_error_after_retries_exhausted() -> None:
    call_count = 0

    async def fake_sleep(seconds: float) -> None:
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(503, json={"error": "unavailable"})

    client = _client(handler, sleep=fake_sleep)
    result = await client.get(RESOURCE_ID)

    assert isinstance(result, ToolError)
    assert result.kind == "upstream_error"
    assert call_count == arm_module._MAX_ATTEMPTS


async def test_transport_error_is_retried_and_recovers() -> None:
    """A connection-level blip (not an HTTP status) must be retried the
    same as a 5xx - otherwise it looks identical to a real bug: the tool
    fails on the first flaky attempt while a fresh manual retry, made
    moments later from outside this process, succeeds."""
    call_count = 0
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ReadTimeout("simulated network blip", request=request)
        return httpx.Response(200, json={"name": "apim-fixture"})

    client = _client(handler, sleep=fake_sleep)
    result = await client.get(RESOURCE_ID)

    assert result == {"name": "apim-fixture"}
    assert call_count == 2
    assert sleeps == [arm_module._BASE_BACKOFF_SECONDS]


async def test_transport_error_becomes_upstream_error_after_retries_exhausted() -> None:
    call_count = 0

    async def fake_sleep(seconds: float) -> None:
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        raise httpx.ConnectError("simulated connection failure", request=request)

    client = _client(handler, sleep=fake_sleep)
    result = await client.get(RESOURCE_ID)

    assert isinstance(result, ToolError)
    assert result.kind == "upstream_error"
    assert call_count == arm_module._MAX_ATTEMPTS


def test_no_mutating_http_methods_are_issued() -> None:
    source = inspect.getsource(arm_module)
    for forbidden in (".post(", ".put(", ".patch(", ".delete("):
        assert forbidden not in source


def test_public_api_surface_is_read_only() -> None:
    public_methods = {name for name in dir(ArmClient) if not name.startswith("_")}
    assert public_methods == {"get", "list_all"}


def test_arm_client_does_not_store_a_persistent_http_client() -> None:
    client = ArmClient(_ctx())
    for value in vars(client).values():
        assert not isinstance(value, httpx.AsyncClient)
