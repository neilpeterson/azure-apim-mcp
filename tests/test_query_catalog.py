"""Coverage for the fixed Log Analytics query catalog."""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

from apim_mcp.auth.context import CallContext
from apim_mcp.clients._loganalytics import run_workspace_query
from apim_mcp.common.errors import ToolError, upstream_error
from apim_mcp.queries.catalog import QUERY_CATALOG, QueryDefinition

_REPOSITORY_ROOT = Path(__file__).parents[1]
_QUERY_DOCS = _REPOSITORY_ROOT / "docs" / "development" / "QUERY_CATALOG.md"
_SAFE_TABLES = frozenset(
    {
        "ApiManagementGatewayLogs",
        "AzureDiagnostics",
        "AzureMetrics",
    }
)


def test_query_ids_are_unique_and_stable() -> None:
    query_ids = [definition.id for definition in QUERY_CATALOG]

    assert len(query_ids) == len(set(query_ids))
    assert all(re.fullmatch(r"[a-z][a-z0-9-]*", query_id) for query_id in query_ids)
    assert set(query_ids) == {
        "gateway-error-summary",
        "gateway-log-detail",
        "metric-definitions",
        "metric-timeseries",
    }


def test_every_query_uses_only_approved_tables() -> None:
    for definition in QUERY_CATALOG:
        assert definition.source_tables
        assert set(definition.source_tables) <= _SAFE_TABLES


def test_every_query_has_maintainer_metadata() -> None:
    for definition in QUERY_CATALOG:
        assert definition.title
        assert definition.purpose
        assert definition.implementation.startswith("src/apim_mcp/")
        assert (_REPOSITORY_ROOT / definition.implementation).is_file()
        assert definition.parameters
        assert definition.result_fields


def test_every_catalog_query_is_documented() -> None:
    documentation = _QUERY_DOCS.read_text()

    for definition in QUERY_CATALOG:
        assert f"`{definition.id}`" in documentation


async def test_uncataloged_query_definition_is_rejected_before_execution() -> None:
    definition = QueryDefinition(
        id="uncataloged",
        title="Uncataloged query",
        purpose="Prove that metadata objects cannot bypass the registry.",
        visibility="internal",
        tools=(),
        source_tables=("AzureMetrics",),
        parameters=("resource_id",),
        result_fields=("Value",),
        implementation="src/apim_mcp/clients/metrics.py",
    )
    ctx = CallContext(oid="oid", upn="user@example.com", roles=(), bearer_token="token")

    result = await run_workspace_query(
        ctx,
        scope="https://api.loganalytics.io/.default",
        workspace_resource_id="/subscriptions/unused",
        definition=definition,
        query="declare query_parameters(resource_id:string = 'unused');\nAzureMetrics",
        timespan=timedelta(hours=1),
        on_status=lambda status, retry_after: upstream_error(
            log_detail=f"unexpected status {status}, retry {retry_after}"
        ),
    )

    assert isinstance(result, ToolError)
    assert result.kind == "upstream_error"
