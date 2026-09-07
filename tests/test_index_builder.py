"""Tests for `apim_mcp.index.builder` (T-15). See docs/SPEC.md §7.1-7.3, §7.5."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import apim_mcp.clients.arm as arm_module
from apim_mcp.auth.context import CallContext
from apim_mcp.common.errors import ToolError, upstream_error
from apim_mcp.index.builder import (
    _MAX_SCHEMA_PROPERTIES,
    _compose_search_text,
    build_service_index,
)
from apim_mcp.settings import ApimServiceConfig

RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000"
    "/resourceGroups/rg-fixture"
    "/providers/Microsoft.ApiManagement/service/apim-fixture"
)
API_VERSION = "2024-05-01"


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


def _config() -> ApimServiceConfig:
    return ApimServiceConfig(alias="prod", resource_id=RESOURCE_ID)


def _api(name: str, *, is_current: bool = True) -> dict[str, object]:
    return {
        "id": f"{RESOURCE_ID}/apis/{name}",
        "name": name,
        "properties": {
            "displayName": name.replace("-", " ").title(),
            "description": f"{name} description",
            "path": name,
            "tags": ["catalog"],
            "isCurrent": is_current,
        },
    }


def _operation(
    name: str,
    *,
    method: str = "GET",
    url_template: str = "/resource",
    display_name: str | None = None,
) -> dict[str, object]:
    return {
        "id": f"{RESOURCE_ID}/apis/x/operations/{name}",
        "name": name,
        "properties": {
            "displayName": display_name or name.replace("-", " ").title(),
            "method": method,
            "urlTemplate": url_template,
            "description": f"{name} description",
            "templateParameters": [],
            "request": {"queryParameters": [], "headers": []},
        },
    }


def _apis_response(*apis: dict[str, object]) -> dict[str, object]:
    return {"value": list(apis)}


def _operations_response(*operations: dict[str, object]) -> dict[str, object]:
    return {"value": list(operations)}


def _router(handlers: dict[str, object]) -> httpx.MockTransport:
    """Route by the request path (ignoring query string) to a fixed
    response dict, a list of responses (consumed in order - for testing
    retries), or a callable(request) -> httpx.Response."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        entry = handlers.get(path)
        if entry is None:
            raise AssertionError(f"no handler registered for {path!r}")
        if callable(entry):
            response = entry(request)
            assert isinstance(response, httpx.Response)
            return response
        if isinstance(entry, list):
            popped = entry.pop(0)
            assert isinstance(popped, httpx.Response)
            return popped
        return httpx.Response(200, json=entry)

    return httpx.MockTransport(handler)


async def _no_spec(*args: object, **kwargs: object) -> ToolError:
    return upstream_error(log_detail="no spec in this test")


async def test_export_failure_degrades() -> None:
    """§7.2: a per-API export failure indexes that API from its operations
    list alone and sets `spec_indexed: false` - it never fails the build."""
    transport = _router(
        {
            f"{RESOURCE_ID}/apis": _apis_response(_api("echo-api")),
            f"{RESOURCE_ID}/apis/echo-api/operations": _operations_response(
                _operation("retrieve-resource")
            ),
        }
    )

    result = await build_service_index(
        _ctx(),
        _config(),
        max_concurrency=4,
        fetch_spec=_no_spec,
        arm_transport=transport,
    )

    assert result.error is None
    assert result.api_count == 1
    assert result.operation_count == 1
    assert result.spec_failures == 1
    assert result.partial is False
    entry = result.entries[0]
    assert entry.spec_indexed is False
    assert entry.schema_property_names == []
    assert entry.operation_id == "retrieve-resource"


async def test_schema_depth_capped() -> None:
    """§7.2: schema property extraction stops at depth 3 and 200 properties."""
    # Build a schema nested 6 levels deep, each level adding one property -
    # only the first 3 levels' properties should be collected.
    schema: dict[str, object] = {"type": "object", "properties": {"level5": {"type": "string"}}}
    for level in range(4, -1, -1):
        schema = {"type": "object", "properties": {f"level{level}": schema}}

    spec_document: dict[str, object] = {
        "paths": {
            "/resource": {
                "get": {
                    "operationId": "retrieve-resource",
                    "responses": {"200": {"content": {"application/json": {"schema": schema}}}},
                }
            }
        }
    }

    async def fake_fetch(*args: object, **kwargs: object) -> dict[str, object]:
        return spec_document

    transport = _router(
        {
            f"{RESOURCE_ID}/apis": _apis_response(_api("echo-api")),
            f"{RESOURCE_ID}/apis/echo-api/operations": _operations_response(
                _operation("retrieve-resource")
            ),
        }
    )

    result = await build_service_index(
        _ctx(), _config(), max_concurrency=4, fetch_spec=fake_fetch, arm_transport=transport
    )

    entry = result.entries[0]
    assert entry.spec_indexed is True
    # depth 0 -> level0, depth 1 -> level1, depth 2 -> level2, depth 3 ->
    # level3 : the check is `depth > _MAX_SCHEMA_DEPTH` (3), so depths
    # 0..3 inclusive (4 levels) are collected; level4/level5 must not be.
    assert entry.schema_property_names == ["level0", "level1", "level2", "level3"]
    assert "level4" not in entry.schema_property_names
    assert "level5" not in entry.schema_property_names


async def test_schema_property_count_capped() -> None:
    many_properties = {f"prop{i}": {"type": "string"} for i in range(_MAX_SCHEMA_PROPERTIES + 50)}
    spec_document: dict[str, object] = {
        "paths": {
            "/resource": {
                "get": {
                    "operationId": "retrieve-resource",
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"type": "object", "properties": many_properties}
                                }
                            }
                        }
                    },
                }
            }
        }
    }

    async def fake_fetch(*args: object, **kwargs: object) -> dict[str, object]:
        return spec_document

    transport = _router(
        {
            f"{RESOURCE_ID}/apis": _apis_response(_api("echo-api")),
            f"{RESOURCE_ID}/apis/echo-api/operations": _operations_response(
                _operation("retrieve-resource")
            ),
        }
    )

    result = await build_service_index(
        _ctx(), _config(), max_concurrency=4, fetch_spec=fake_fetch, arm_transport=transport
    )

    assert len(result.entries[0].schema_property_names) == _MAX_SCHEMA_PROPERTIES


async def test_concurrency_is_bounded() -> None:
    """§7.2: `asyncio.Semaphore(index_max_concurrency)` - never more than
    `max_concurrency` spec exports in flight at once."""
    api_names = [f"api-{i}" for i in range(6)]
    max_concurrency = 2

    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def fake_fetch(*args: object, **kwargs: object) -> ToolError:
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        async with lock:
            in_flight -= 1
        return upstream_error(log_detail="no spec in this test")

    handlers: dict[str, object] = {
        f"{RESOURCE_ID}/apis": _apis_response(*[_api(n) for n in api_names])
    }
    for name in api_names:
        handlers[f"{RESOURCE_ID}/apis/{name}/operations"] = _operations_response(
            _operation("retrieve-resource")
        )
    transport = _router(handlers)

    result = await build_service_index(
        _ctx(),
        _config(),
        max_concurrency=max_concurrency,
        fetch_spec=fake_fetch,
        arm_transport=transport,
    )

    assert result.api_count == len(api_names)
    assert max_in_flight <= max_concurrency


async def test_throttling_is_not_failure() -> None:
    """§7.2: throttling (429, honouring `Retry-After`) is transparently
    retried by `ArmClient` - it must not surface as a build failure."""
    call_count = {"operations": 0}

    def operations_handler(request: httpx.Request) -> httpx.Response:
        call_count["operations"] += 1
        if call_count["operations"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "throttled"})
        return httpx.Response(200, json=_operations_response(_operation("retrieve-resource")))

    transport = _router(
        {
            f"{RESOURCE_ID}/apis": _apis_response(_api("echo-api")),
            f"{RESOURCE_ID}/apis/echo-api/operations": operations_handler,
        }
    )

    result = await build_service_index(
        _ctx(), _config(), max_concurrency=4, fetch_spec=_no_spec, arm_transport=transport
    )

    assert result.error is None
    assert call_count["operations"] == 2
    assert result.operation_count == 1
    assert result.entries[0].operation_id == "retrieve-resource"


async def test_build_timeout_returns_partial() -> None:
    """§7.2: a hard build-time cap keeps whatever finished and marks
    `partial=True` rather than hanging or raising."""
    api_names = ["fast-api", "slow-api"]

    async def fake_fetch(
        ctx: CallContext,
        resource_id: str,
        *,
        format: str,  # noqa: A002
        **kwargs: object,
    ) -> ToolError:
        if "slow-api" in resource_id:
            await asyncio.sleep(5)
        return upstream_error(log_detail="no spec in this test")

    handlers: dict[str, object] = {
        f"{RESOURCE_ID}/apis": _apis_response(*[_api(n) for n in api_names])
    }
    for name in api_names:
        handlers[f"{RESOURCE_ID}/apis/{name}/operations"] = _operations_response(
            _operation("retrieve-resource")
        )
    transport = _router(handlers)

    result = await build_service_index(
        _ctx(),
        _config(),
        max_concurrency=4,
        build_timeout_seconds=0.2,
        fetch_spec=fake_fetch,
        arm_transport=transport,
    )

    assert result.partial is True
    # fast-api finished within the budget; slow-api's task got cancelled.
    assert any(e.api_id == "fast-api" for e in result.entries)
    assert not any(e.api_id == "slow-api" for e in result.entries)


async def test_non_current_revisions_excluded() -> None:
    transport = _router(
        {
            f"{RESOURCE_ID}/apis": _apis_response(
                _api("echo-api", is_current=True), _api("echo-api-v1", is_current=False)
            ),
            f"{RESOURCE_ID}/apis/echo-api/operations": _operations_response(
                _operation("retrieve-resource")
            ),
        }
    )

    result = await build_service_index(
        _ctx(), _config(), max_concurrency=4, fetch_spec=_no_spec, arm_transport=transport
    )

    assert result.api_count == 1
    assert all(e.api_id == "echo-api" for e in result.entries)


def test_search_text_weights_fields_per_spec() -> None:
    """§7.3's weighting table: operation name x3, API name/tags x2,
    url_template x2, description/params/schema properties x1."""
    text = _compose_search_text(
        operation_display_name="Get Inventory Levels",
        api_display_name="Warehouse API",
        url_template="/inventory/{id}",
        operation_description="Returns inventory levels",
        api_description="Warehouse operations",
        parameter_names=["id"],
        schema_property_names=["quantity"],
        api_tags=["catalog"],
    )
    tokens = text.split()
    # inventory: operation name x3 + url_template x2 + description x1 = 6
    assert tokens.count("inventory") == 6
    # warehouse: api name x2 + api description x1 = 3
    assert tokens.count("warehouse") == 3
    assert tokens.count("catalog") == 2  # tags x2
    assert tokens.count("quantity") == 1  # schema property names x1
    # id: url_template x2 + parameter_names x1 = 3
    assert tokens.count("id") == 3


def test_api_index_entry_serializes_expected_fields() -> None:
    """A basic sanity check that the model matches docs/SPEC.md §7.1's shape."""
    from apim_mcp.index.builder import OperationIndexEntry

    entry = OperationIndexEntry(
        service="prod",
        api_id="echo-api",
        api_display_name="Echo API",
        api_description=None,
        api_path="echo",
        api_tags=[],
        operation_id="retrieve-resource",
        operation_display_name="Retrieve resource",
        operation_description=None,
        method="GET",
        url_template="/resource",
        parameter_names=[],
        schema_property_names=[],
        search_text="retrieve resource",
        spec_indexed=True,
    )
    dumped = json.loads(entry.model_dump_json())
    assert dumped["service"] == "prod"
    assert dumped["spec_indexed"] is True
