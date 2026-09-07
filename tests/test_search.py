"""Tests for `apim_mcp.index.search` and `apim_mcp.tools.search` (T-16).

See docs/SPEC.md §6 Group C and §7.4.
"""

from __future__ import annotations

import asyncio

import pytest

from apim_mcp.auth.context import CallContext
from apim_mcp.common.errors import ToolError
from apim_mcp.index.builder import OperationIndexEntry, ServiceIndexResult
from apim_mcp.index.search import IndexManager
from apim_mcp.settings import ApimServiceConfig, Settings

TENANT_ID = "11111111-1111-1111-1111-111111111111"


def _ctx() -> CallContext:
    return CallContext(oid="user-oid", upn="user@example.com", roles=(), bearer_token="tok")


def _settings(*, ttl_seconds: int = 900, aliases: tuple[str, ...] = ("prod",)) -> Settings:
    return Settings(
        azure_tenant_id=TENANT_ID,
        azure_client_id="22222222-2222-2222-2222-222222222222",
        mcp_server_audience="api://apim-mcp",
        mcp_server_app_id="api://apim-mcp",
        apim_services=[
            ApimServiceConfig(
                alias=alias,
                resource_id=(
                    "/subscriptions/00000000-0000-0000-0000-000000000000"
                    "/resourceGroups/rg-fixture"
                    f"/providers/Microsoft.ApiManagement/service/{alias}"
                ),
            )
            for alias in aliases
        ],
        index_ttl_seconds=ttl_seconds,
        applicationinsights_connection_string=(
            "InstrumentationKey=00000000-0000-0000-0000-000000000000"
        ),
    )


def _entry(
    *,
    service: str = "prod",
    api_id: str = "inventory-api",
    api_display_name: str = "Inventory API",
    operation_id: str = "getInventoryLevels",
    operation_display_name: str = "Get Inventory Levels",
    method: str = "GET",
    url_template: str = "/inventory/{id}",
    operation_description: str = "Returns current inventory levels for a SKU.",
    api_description: str | None = "Warehouse inventory operations.",
    parameter_names: list[str] | None = None,
    schema_property_names: list[str] | None = None,
    api_tags: list[str] | None = None,
) -> OperationIndexEntry:
    from apim_mcp.index.builder import _compose_search_text

    parameter_names = parameter_names if parameter_names is not None else ["id"]
    schema_property_names = schema_property_names if schema_property_names is not None else []
    api_tags = api_tags if api_tags is not None else []
    search_text = _compose_search_text(
        operation_display_name=operation_display_name,
        api_display_name=api_display_name,
        url_template=url_template,
        operation_description=operation_description,
        api_description=api_description,
        parameter_names=parameter_names,
        schema_property_names=schema_property_names,
        api_tags=api_tags,
    )
    return OperationIndexEntry(
        service=service,
        api_id=api_id,
        api_display_name=api_display_name,
        api_description=api_description,
        api_path=api_id,
        api_tags=api_tags,
        operation_id=operation_id,
        operation_display_name=operation_display_name,
        operation_description=operation_description,
        method=method,
        url_template=url_template,
        parameter_names=parameter_names,
        schema_property_names=schema_property_names,
        search_text=search_text,
        spec_indexed=True,
    )


def _result(entries: list[OperationIndexEntry], *, service: str = "prod") -> ServiceIndexResult:
    return ServiceIndexResult(
        service=service,
        entries=entries,
        partial=False,
        api_count=len({e.api_id for e in entries}) or 1,
        operation_count=len(entries),
        spec_failures=0,
        duration_seconds=0.01,
    )


def _corpus() -> list[OperationIndexEntry]:
    return [
        _entry(),
        _entry(
            api_id="orders-api",
            api_display_name="Orders API",
            operation_id="createOrder",
            operation_display_name="Create Order",
            method="POST",
            url_template="/orders",
            operation_description="Places a new customer order.",
            api_description="Order placement and tracking.",
            parameter_names=[],
        ),
        _entry(
            api_id="users-api",
            api_display_name="Users API",
            operation_id="getUserProfile",
            operation_display_name="Get User Profile",
            method="GET",
            url_template="/users/{id}/profile",
            operation_description="Returns a user's profile information.",
            api_description="User account management.",
        ),
    ]


def _manager_with_fixed_corpus(
    entries: list[OperationIndexEntry], *, settings: Settings | None = None, clock: object = None
) -> IndexManager:
    settings = settings or _settings()

    async def build_fn(
        ctx: CallContext, config: ApimServiceConfig, **kwargs: object
    ) -> ServiceIndexResult:
        return _result([e for e in entries if e.service == config.alias], service=config.alias)

    kwargs: dict[str, object] = {"build_fn": build_fn}
    if clock is not None:
        kwargs["clock"] = clock
    return IndexManager(settings, **kwargs)  # type: ignore[arg-type]


async def test_inventory_question() -> None:
    """T-16's headline scenario: `getInventoryLevels` ranks first for the
    query "inventory" among unrelated operations."""
    manager = _manager_with_fixed_corpus(_corpus())
    await manager.build_all(_ctx())

    result = await manager.search(
        _ctx(), query="inventory", terms=None, service=None, scope="both", limit=15
    )
    assert not isinstance(result, ToolError)
    assert result["hits"]
    assert result["hits"][0]["operationId"] == "getInventoryLevels"
    assert result["lowConfidence"] is False


async def test_synonym_terms_widen_results() -> None:
    """`terms=["stock"]` must surface an operation named `getStockLevels`
    even though the bare query wouldn't lexically match it."""
    entries = [
        *_corpus(),
        _entry(
            api_id="stock-api",
            api_display_name="Stock API",
            operation_id="getStockLevels",
            operation_display_name="Get Stock Levels",
            method="GET",
            url_template="/stock/{id}",
            operation_description="Returns current stock counts for a SKU.",
            api_description="Stock tracking.",
        ),
    ]
    manager = _manager_with_fixed_corpus(entries)
    await manager.build_all(_ctx())

    result = await manager.search(
        _ctx(),
        query="widgets availability",
        terms=["stock"],
        service=None,
        scope="both",
        limit=15,
    )
    assert not isinstance(result, ToolError)
    operation_ids = {hit["operationId"] for hit in result["hits"]}
    assert "getStockLevels" in operation_ids


async def test_low_confidence_flagged() -> None:
    manager = _manager_with_fixed_corpus(_corpus())
    await manager.build_all(_ctx())

    result = await manager.search(
        _ctx(),
        query="xyzzyplughquux nonsense gibberish",
        terms=None,
        service=None,
        scope="both",
        limit=15,
    )
    assert not isinstance(result, ToolError)
    assert result["hits"]  # still returned - never hide a possible answer
    assert result["lowConfidence"] is True


async def test_matched_fields_and_snippet_populated() -> None:
    manager = _manager_with_fixed_corpus(_corpus())
    await manager.build_all(_ctx())

    result = await manager.search(
        _ctx(), query="inventory", terms=None, service=None, scope="both", limit=15
    )
    assert not isinstance(result, ToolError)
    top_hit = result["hits"][0]
    assert top_hit["matchedFields"]
    assert isinstance(top_hit["snippet"], str)
    assert top_hit["snippet"]


async def test_search_returns_index_unavailable_before_first_build() -> None:
    manager = _manager_with_fixed_corpus(_corpus())
    # No build_all() call - the index has never been built for "prod".
    result = await manager.search(
        _ctx(), query="inventory", terms=None, service=None, scope="both", limit=15
    )
    assert isinstance(result, ToolError)
    assert result.kind == "index_unavailable"


async def test_stale_index_served_during_background_rebuild() -> None:
    """§7.5: "serve the stale index while rebuilding; never block a
    request on a rebuild." """
    clock_value = [0.0]

    def clock() -> float:
        return clock_value[0]

    settings = _settings(ttl_seconds=10)
    entries = _corpus()
    build_started = asyncio.Event()
    release_build = asyncio.Event()
    build_count = [0]

    async def slow_build_fn(
        ctx: CallContext, config: ApimServiceConfig, **kwargs: object
    ) -> ServiceIndexResult:
        build_count[0] += 1
        if build_count[0] > 1:
            build_started.set()
            await release_build.wait()
        return _result(entries, service=config.alias)

    manager = IndexManager(settings, build_fn=slow_build_fn, clock=clock)
    await manager.build_all(_ctx())
    assert build_count[0] == 1

    # Advance the clock past the TTL so the next search sees a stale index.
    clock_value[0] = 100.0

    result = await manager.search(
        _ctx(), query="inventory", terms=None, service=None, scope="both", limit=15
    )
    # The request is served immediately from the stale (but present) index
    # - it must not block on the in-flight background rebuild.
    assert not isinstance(result, ToolError)
    assert result["hits"]
    assert result["hits"][0]["operationId"] == "getInventoryLevels"

    await asyncio.wait_for(build_started.wait(), timeout=1)
    assert build_count[0] == 2  # the background refresh really was kicked off
    release_build.set()
    await asyncio.sleep(0)  # let the background task finish so it doesn't leak into other tests


async def test_refresh_rate_limited_to_once_per_60s() -> None:
    clock_value = [0.0]

    def clock() -> float:
        return clock_value[0]

    manager = _manager_with_fixed_corpus(_corpus(), clock=clock)

    first = await manager.refresh(_ctx(), service="prod")
    assert not isinstance(first, ToolError)

    clock_value[0] = 30.0  # within the 60s window
    second = await manager.refresh(_ctx(), service="prod")
    assert isinstance(second, ToolError)
    assert second.kind == "throttled"

    clock_value[0] = 61.0  # past the window
    third = await manager.refresh(_ctx(), service="prod")
    assert not isinstance(third, ToolError)


async def test_refresh_counts_are_reported() -> None:
    manager = _manager_with_fixed_corpus(_corpus())
    result = await manager.refresh(_ctx(), service="prod")
    assert not isinstance(result, ToolError)
    assert result["refreshed"] == ["prod"]
    assert result["counts"]["prod"]["operationCount"] == len(_corpus())


async def test_search_scope_apis_dedupes_by_api() -> None:
    entries = [
        *_corpus(),
        _entry(
            api_id="inventory-api",
            api_display_name="Inventory API",
            operation_id="getInventoryHistory",
            operation_display_name="Get Inventory History",
            method="GET",
            url_template="/inventory/{id}/history",
            operation_description="Returns inventory history for a SKU.",
        ),
    ]
    manager = _manager_with_fixed_corpus(entries)
    await manager.build_all(_ctx())

    result = await manager.search(
        _ctx(), query="inventory", terms=None, service=None, scope="apis", limit=15
    )
    assert not isinstance(result, ToolError)
    api_ids = [hit["apiId"] for hit in result["hits"]]
    assert api_ids.count("inventory-api") == 1


def test_search_tool_description_instructs_synonym_expansion() -> None:
    """§6 Group C: the tool description must instruct the model to supply
    its own synonyms, with the inventory example spelled out."""
    import asyncio as _asyncio

    from mcp.server.fastmcp import FastMCP

    from apim_mcp.server import ToolRegistration
    from apim_mcp.tools.search import register_search_tools

    settings = _settings()
    mcp: FastMCP[object] = FastMCP("test")
    registry: list[ToolRegistration] = []
    manager = _manager_with_fixed_corpus(_corpus(), settings=settings)
    register_search_tools(mcp, registry, settings, manager)

    tools = _asyncio.run(mcp.list_tools())
    search_tool = next(t for t in tools if t.name == "apim_search_apis")
    description = search_tool.description or ""
    assert "synonym" in description.lower()
    assert "inventory" in description.lower()
    assert "terms" in description.lower()


@pytest.mark.parametrize("service", ["prod", None])
async def test_search_service_filter(service: str | None) -> None:
    settings = _settings(aliases=("prod", "staging"))
    entries = [
        *_corpus(),
        _entry(service="staging", api_id="staging-inventory", operation_id="stagingInventory"),
    ]
    manager = _manager_with_fixed_corpus(entries, settings=settings)
    await manager.build_all(_ctx())

    result = await manager.search(
        _ctx(), query="inventory", terms=None, service=service, scope="both", limit=15
    )
    assert not isinstance(result, ToolError)
    services_seen = {hit["service"] for hit in result["hits"]}
    if service == "prod":
        assert services_seen == {"prod"}
    else:
        assert "staging" in services_seen
