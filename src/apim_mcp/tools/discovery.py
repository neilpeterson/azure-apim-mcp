"""Group A tools: discovery and health (T-10). See docs/development/SPEC.md §6 Group A.

`apim_list_services` and `apim_get_service` return the ARM-configuration
view; `apim_get_service_health` is the fan-out workflow tool that answers
"is this instance healthy" in one round trip instead of four, with each
sub-call independently fault-tolerant per §6 Group A.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from mcp.server.fastmcp import FastMCP

from apim_mcp.auth.context import CallContext
from apim_mcp.clients.arm import DEFAULT_API_VERSION, ArmClient
from apim_mcp.clients.metrics import MetricsClient
from apim_mcp.common.errors import ToolError, upstream_error
from apim_mcp.common.formatting import ResponseFormat
from apim_mcp.server import ToolRegistration, audited_tool
from apim_mcp.settings import ApimServiceConfig, Settings
from apim_mcp.tools._common import require_workspace, resolve_service

_RESOURCE_HEALTH_API_VERSION = "2023-07-01-preview"
_CERT_WARNING_DAYS = 30
_HEALTH_TIMEOUT_SECONDS = 60
_HEALTH_TIMEOUT_REASON = "health query exceeded the time budget"


def _resource_group_from_id(resource_id: str) -> str | None:
    """ARM resource IDs are case-insensitive; some responses echo
    `resourceGroups`, others `resourcegroups` - match either casing."""
    parts = resource_id.split("/")
    for index, part in enumerate(parts):
        if part.lower() == "resourcegroups" and index + 1 < len(parts):
            return parts[index + 1]
    return None


def _hostname_entries(properties: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Map `hostnameConfigurations` to the public shape. Never includes
    `encodedCertificate` or `certificatePassword`, whatever the source
    payload contains (docs/development/SPEC.md §6 Group A)."""
    now = datetime.now(UTC)
    entries: list[dict[str, Any]] = []
    for hc in properties.get("hostnameConfigurations") or []:
        certificate = hc.get("certificate") or {}
        expiry = certificate.get("expiry")
        entries.append(
            {
                "hostName": hc.get("hostName"),
                "certificateSource": hc.get("certificateSource"),
                "expiry": expiry,
                "daysUntilExpiry": _days_until_expiry(expiry, now),
            }
        )
    return entries


def _days_until_expiry(expiry: str | None, now: datetime) -> int | None:
    if not expiry:
        return None
    try:
        expiry_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (expiry_dt - now).days


async def _fetch(
    client: ArmClient, resource_id: str, *, api_version: str
) -> tuple[dict[str, Any] | list[Any] | None, str | None]:
    """Fetch one ARM resource. Returns `(body, None)` on success or
    `(None, reason)` on failure - never raises, so callers building a
    fault-tolerant fan-out (§6 Group A) can degrade one section at a time."""
    result = await client.get(resource_id, api_version=api_version)
    if isinstance(result, ToolError):
        return None, result.message
    return result, None


def _certificate_section(properties: Mapping[str, Any]) -> dict[str, Any]:
    hostnames = _hostname_entries(properties)
    warnings = [
        entry
        for entry in hostnames
        if entry["daysUntilExpiry"] is not None and entry["daysUntilExpiry"] <= _CERT_WARNING_DAYS
    ]
    return {"status": "ok", "hostnames": hostnames, "warnings": warnings}


async def _resource_health_section(client: ArmClient, config: ApimServiceConfig) -> dict[str, Any]:
    resource_id = (
        f"{config.resource_id}/providers/Microsoft.ResourceHealth/availabilityStatuses/current"
    )
    body, reason = await _fetch(client, resource_id, api_version=_RESOURCE_HEALTH_API_VERSION)
    if reason is not None or not isinstance(body, dict):
        return {"status": "unavailable", "reason": reason or "unexpected response shape"}
    properties = body.get("properties") or {}
    return {
        "status": "ok",
        "availabilityState": properties.get("availabilityState"),
        "summary": properties.get("summary"),
    }


async def _network_status_section(client: ArmClient, config: ApimServiceConfig) -> dict[str, Any]:
    resource_id = f"{config.resource_id}/networkstatus"
    body, reason = await _fetch(client, resource_id, api_version=DEFAULT_API_VERSION)
    if reason is not None or not isinstance(body, list):
        return {"status": "unavailable", "reason": reason or "unexpected response shape"}
    failing: list[dict[str, Any]] = []
    for location_entry in body:
        if not isinstance(location_entry, dict):
            continue
        connectivity = (location_entry.get("networkStatus") or {}).get("connectivityStatus") or []
        for dependency in connectivity:
            if dependency.get("status") != "success":
                failing.append(
                    {
                        "location": location_entry.get("location"),
                        "name": dependency.get("name"),
                        "resourceType": dependency.get("resourceType"),
                        "error": dependency.get("error"),
                    }
                )
    return {"status": "ok", "failingDependencies": failing}


async def _capacity_section(ctx: CallContext, config: ApimServiceConfig) -> dict[str, Any]:
    workspace_id = require_workspace(config)
    if isinstance(workspace_id, ToolError):
        return {"status": "unavailable", "reason": workspace_id.message}
    result = await MetricsClient(ctx).query(
        config.resource_id,
        workspace_id,
        metric="Capacity",
        timespan="PT1H",
        interval="PT5M",
        aggregation="average",
    )
    if isinstance(result, ToolError):
        return {"status": "unavailable", "reason": result.message}
    weighted_samples = [
        (item["Value"], item["SampleCount"])
        for item in result.get("items", [])
        if isinstance(item.get("Value"), int | float)
        and isinstance(item.get("SampleCount"), int | float)
        and item["SampleCount"] > 0
    ]
    sample_count = sum(sample_count for _, sample_count in weighted_samples)
    if not weighted_samples or sample_count == 0:
        return {
            "status": "unavailable",
            "reason": (
                "No usable Capacity samples were found in Log Analytics for the past hour. "
                "Confirm `AllMetrics` export and allow for ingestion delay; diagnostic settings "
                "do not backfill historical data."
            ),
        }
    weighted_total = sum(value * count for value, count in weighted_samples)
    section: dict[str, Any] = {
        "status": "ok",
        "average": weighted_total / sample_count,
        "sampleCount": sample_count,
        "timespan": "PT1H",
    }
    if result.get("partial"):
        section["partial"] = True
        section["reason"] = "Log Analytics returned partial results; the average may be incomplete."
    return section


def register_discovery_tools(
    mcp: FastMCP[Any], registry: list[ToolRegistration], settings: Settings
) -> None:
    """Register `apim_list_services`, `apim_get_service`, and
    `apim_get_service_health` against `mcp`, auditing every call via
    `audited_tool`."""

    @audited_tool(mcp, registry, name="apim_list_services")
    async def apim_list_services(
        *, ctx: CallContext, response_format: ResponseFormat = "markdown"
    ) -> dict[str, Any] | ToolError:
        """List every APIM instance configured in `APIM_SERVICES`.

        Returns, per instance: `alias`, `name`, `resourceGroup`, `location`,
        `sku`, `skuCapacity`, `provisioningState`, `platformVersion`, and
        `hasLogAnalytics`. Never returns certificates, secrets, or named
        value contents - use `apim_get_service` for full configuration
        detail on one instance.
        """
        client = ArmClient(ctx)
        items: list[dict[str, Any]] = []
        for config in settings.apim_services:
            body, reason = await _fetch(client, config.resource_id, api_version=DEFAULT_API_VERSION)
            if reason is not None:
                return upstream_error(log_detail=f"apim_list_services: {config.alias}: {reason}")
            if not isinstance(body, dict):
                return upstream_error(
                    log_detail=f"apim_list_services: {config.alias}: unexpected response shape"
                )
            properties = body.get("properties") or {}
            sku = body.get("sku") or {}
            items.append(
                {
                    "alias": config.alias,
                    "name": body.get("name"),
                    "resourceGroup": _resource_group_from_id(body.get("id") or config.resource_id),
                    "location": body.get("location"),
                    "sku": sku.get("name"),
                    "skuCapacity": sku.get("capacity"),
                    "provisioningState": properties.get("provisioningState"),
                    "platformVersion": properties.get("platformVersion"),
                    "hasLogAnalytics": config.log_analytics_workspace_id is not None,
                }
            )
        return {"items": items, "count": len(items)}

    @audited_tool(mcp, registry, name="apim_get_service")
    async def apim_get_service(
        *, ctx: CallContext, service: str, response_format: ResponseFormat = "markdown"
    ) -> dict[str, Any] | ToolError:
        """Full configuration of one APIM instance: SKU and capacity,
        provisioning state, platform version, virtual network type, public
        IP addresses, additional locations, developer portal and gateway
        URLs, and per-hostname certificate details (`hostName`,
        `certificateSource`, `expiry`, computed `daysUntilExpiry`). Never
        returns `encodedCertificate` or any certificate password, even if
        present on the underlying ARM resource.
        """
        config = resolve_service(settings, service)
        if isinstance(config, ToolError):
            return config
        client = ArmClient(ctx)
        body, reason = await _fetch(client, config.resource_id, api_version=DEFAULT_API_VERSION)
        if reason is not None:
            return upstream_error(log_detail=f"apim_get_service: {service}: {reason}")
        if not isinstance(body, dict):
            return upstream_error(
                log_detail=f"apim_get_service: {service}: unexpected response shape"
            )
        properties = body.get("properties") or {}
        sku = body.get("sku") or {}
        return {
            "alias": config.alias,
            "name": body.get("name"),
            "resourceGroup": _resource_group_from_id(body.get("id") or config.resource_id),
            "location": body.get("location"),
            "sku": sku.get("name"),
            "skuCapacity": sku.get("capacity"),
            "provisioningState": properties.get("provisioningState"),
            "platformVersion": properties.get("platformVersion"),
            "virtualNetworkType": properties.get("virtualNetworkType"),
            "publicIPAddresses": properties.get("publicIPAddresses"),
            "additionalLocations": properties.get("additionalLocations"),
            "developerPortalUrl": properties.get("developerPortalUrl"),
            "gatewayUrl": properties.get("gatewayUrl"),
            "hostnameConfigurations": _hostname_entries(properties),
        }

    @audited_tool(mcp, registry, name="apim_get_service_health")
    async def apim_get_service_health(
        *, ctx: CallContext, service: str, response_format: ResponseFormat = "markdown"
    ) -> dict[str, Any] | ToolError:
        """Consolidated health view for one APIM instance: provisioning
        state, Azure Resource Health, certificate-expiry warnings (hostnames
        within 30 days of expiry), capacity, and network status of the
        service's dependency connections. Each section is independently
        fault-tolerant - a failing sub-call reports `status: "unavailable"`
        with a `reason` instead of failing the whole tool. Never returns
        certificates or raw metric time series - only current state and,
        for network status, the names of failing dependencies.
        """
        config = resolve_service(settings, service)
        if isinstance(config, ToolError):
            return config
        client = ArmClient(ctx)

        service_task = asyncio.create_task(
            _fetch(client, config.resource_id, api_version=DEFAULT_API_VERSION)
        )
        resource_health_task = asyncio.create_task(_resource_health_section(client, config))
        network_task = asyncio.create_task(_network_status_section(client, config))
        capacity_task = asyncio.create_task(_capacity_section(ctx, config))
        tasks = (service_task, resource_health_task, network_task, capacity_task)
        done, pending = await asyncio.wait(tasks, timeout=_HEALTH_TIMEOUT_SECONDS)
        for pending_task in pending:
            pending_task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        if service_task in done:
            service_body, service_reason = service_task.result()
        else:
            service_body, service_reason = None, _HEALTH_TIMEOUT_REASON
        if service_reason is not None or not isinstance(service_body, dict):
            reason = service_reason or "unexpected response shape"
            provisioning_section: dict[str, Any] = {"status": "unavailable", "reason": reason}
            certificate_section: dict[str, Any] = {"status": "unavailable", "reason": reason}
        else:
            properties = service_body.get("properties") or {}
            provisioning_section = {
                "status": "ok",
                "provisioningState": properties.get("provisioningState"),
            }
            certificate_section = _certificate_section(properties)

        resource_health_section = (
            resource_health_task.result()
            if resource_health_task in done
            else {"status": "unavailable", "reason": _HEALTH_TIMEOUT_REASON}
        )
        network_section = (
            network_task.result()
            if network_task in done
            else {"status": "unavailable", "reason": _HEALTH_TIMEOUT_REASON}
        )
        capacity_section = (
            capacity_task.result()
            if capacity_task in done
            else {"status": "unavailable", "reason": _HEALTH_TIMEOUT_REASON}
        )

        return {
            "alias": config.alias,
            "provisioningState": provisioning_section,
            "resourceHealth": resource_health_section,
            "certificateExpiry": certificate_section,
            "capacityMetric": capacity_section,
            "networkStatus": network_section,
        }
