"""Shared tool-layer validation helpers."""

from __future__ import annotations

from apim_mcp.common.errors import ToolError, invalid_input
from apim_mcp.settings import ApimServiceConfig, Settings, UnknownServiceAliasError

UNKNOWN_SERVICE_HINT = "one of the aliases configured in APIM_SERVICES - see apim_list_services"


def resolve_service(settings: Settings, alias: str) -> ApimServiceConfig | ToolError:
    """Resolve a service alias into configuration or an actionable result."""
    try:
        return settings.service(alias)
    except UnknownServiceAliasError:
        return invalid_input("service", UNKNOWN_SERVICE_HINT)


def require_workspace(config: ApimServiceConfig) -> str | ToolError:
    """Return a configured Log Analytics workspace or an actionable result."""
    if config.log_analytics_workspace_id is None:
        return ToolError(
            kind="invalid_input",
            message=(
                f"Log Analytics is not mapped for service '{config.alias}' in the MCP server's "
                "`APIM_SERVICES` configuration. Add `logAnalyticsWorkspaceId` with the "
                "workspace ARM resource ID; Azure diagnostic settings are not auto-discovered."
            ),
        )
    return config.log_analytics_workspace_id
