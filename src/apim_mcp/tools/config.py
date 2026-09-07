"""Group B tools: API configuration (T-13). See docs/SPEC.md §6 Group B.

`apim_get_api_spec` (the OpenAPI/Swagger export) is deliberately excluded —
that is T-14, with its own two-call SAS-link flow. Everything here is a
straightforward ARM read, except `apim_get_policy`, which must pass its
result through the T-11 redaction module before it ever reaches the model:
policy XML is exactly the "content the identity is legitimately allowed to
read but which may embed secrets" case `docs/PRINCIPLES.md` §5 describes.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from apim_mcp.auth.context import CallContext
from apim_mcp.clients.arm import ArmClient
from apim_mcp.common.errors import ToolError, invalid_input, not_found, upstream_error
from apim_mcp.common.formatting import ResponseFormat, apply_truncation, build_list_envelope
from apim_mcp.common.redaction import redact_policy_xml
from apim_mcp.server import ToolRegistration, audited_tool
from apim_mcp.settings import ApimServiceConfig, Settings, UnknownServiceAliasError

_API_VERSION = "2024-05-01"
_MAX_INLINE_OPERATIONS = 100
_UNKNOWN_SERVICE_HINT = "one of the aliases configured in APIM_SERVICES - see apim_list_services"

PolicyScope = Literal["global", "api", "operation", "product"]


def _resolve_service(settings: Settings, alias: str) -> ApimServiceConfig | None:
    try:
        return settings.service(alias)
    except UnknownServiceAliasError:
        return None


def _matches_filter(item: dict[str, Any], *, filter_text: str) -> bool:
    needle = filter_text.lower()
    properties = item.get("properties") or {}
    haystacks = (item.get("name"), properties.get("displayName"), properties.get("path"))
    return any(needle in str(h).lower() for h in haystacks if h is not None)


def _paginate(
    items: list[dict[str, Any]], *, limit: int, offset: int, max_bytes: int, narrow_param: str
) -> dict[str, Any]:
    page = items[offset : offset + limit]
    envelope = build_list_envelope(page, total=len(items), offset=offset, limit=limit)
    return apply_truncation(envelope, max_bytes=max_bytes, narrow_param=narrow_param)


def _api_summary(item: dict[str, Any]) -> dict[str, Any]:
    properties = item.get("properties") or {}
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "displayName": properties.get("displayName"),
        "path": properties.get("path"),
        "protocols": properties.get("protocols"),
        "apiRevision": properties.get("apiRevision"),
        "isCurrent": properties.get("isCurrent"),
        "apiVersion": properties.get("apiVersion"),
        "apiVersionSetId": properties.get("apiVersionSetId"),
        "subscriptionRequired": properties.get("subscriptionRequired"),
        "type": item.get("type"),
        "serviceUrl": properties.get("serviceUrl"),
        # ARM's `GET .../apis` payload carries no operation count and
        # computing it here would mean an extra call per API - defeating
        # the point of a list tool. `apim_get_api` returns the real
        # operation list for one API when that detail is needed.
        "operationCount": None,
    }


def _operation_summary(item: dict[str, Any]) -> dict[str, Any]:
    properties = item.get("properties") or {}
    return {
        "id": item.get("id"),
        "displayName": properties.get("displayName"),
        "method": properties.get("method"),
        "urlTemplate": properties.get("urlTemplate"),
        "description": properties.get("description"),
    }


def _product_summary(item: dict[str, Any]) -> dict[str, Any]:
    properties = item.get("properties") or {}
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "displayName": properties.get("displayName"),
        "description": properties.get("description"),
        "subscriptionRequired": properties.get("subscriptionRequired"),
        "approvalRequired": properties.get("approvalRequired"),
        "subscriptionsLimit": properties.get("subscriptionsLimit"),
        "state": properties.get("state"),
    }


def _backend_summary(item: dict[str, Any]) -> dict[str, Any]:
    """Never includes `credentials` - PRINCIPLES §5, even if the source
    payload carries one (it shouldn't; the RBAC role can't retrieve it, but
    this tool doesn't trust that as the only line of defence)."""
    properties = item.get("properties") or {}
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "url": properties.get("url"),
        "protocol": properties.get("protocol"),
        "title": properties.get("title"),
        "description": properties.get("description"),
        "tls": properties.get("tls"),
    }


def _named_value_summary(item: dict[str, Any]) -> dict[str, Any]:
    """Never calls the named-value secret-listing action (PRINCIPLES §5) and
    never forwards a `value` for an entry marked `secret` - even if one
    somehow appeared on the source payload, which the RBAC role cannot
    produce anyway."""
    properties = item.get("properties") or {}
    is_secret = bool(properties.get("secret"))
    summary = {
        "name": item.get("name"),
        "displayName": properties.get("displayName"),
        "tags": properties.get("tags"),
        "secret": is_secret,
    }
    if not is_secret:
        summary["value"] = properties.get("value")
    return summary


def _subscription_summary(item: dict[str, Any]) -> dict[str, Any]:
    """Never `primaryKey`/`secondaryKey` (PRINCIPLES §5), regardless of
    what the source payload contains."""
    properties = item.get("properties") or {}
    return {
        "id": item.get("id"),
        "displayName": properties.get("displayName"),
        "scope": properties.get("scope"),
        "state": properties.get("state"),
        "createdDate": properties.get("createdDate"),
        "ownerId": properties.get("ownerId"),
    }


def _policy_resource_id(
    config: ApimServiceConfig,
    *,
    scope: PolicyScope,
    api_id: str | None,
    operation_id: str | None,
    product_id: str | None,
) -> ToolError | str:
    base = config.resource_id
    if scope == "global":
        return f"{base}/policies/policy"
    if scope == "api":
        if not api_id:
            return invalid_input("api_id", "'echo-api' (required when scope='api')")
        return f"{base}/apis/{api_id}/policies/policy"
    if scope == "operation":
        if not api_id or not operation_id:
            return invalid_input(
                "api_id, operation_id", "'echo-api', 'retrieve-resource' (both required)"
            )
        return f"{base}/apis/{api_id}/operations/{operation_id}/policies/policy"
    if not product_id:
        return invalid_input("product_id", "'starter' (required when scope='product')")
    return f"{base}/products/{product_id}/policies/policy"


def _extract_policy_xml(raw_text: str) -> str | None:
    """`.../policies/policy?format=rawxml` is documented as returning a
    JSON-wrapped `PolicyContract` (`properties.value` holding the XML), but
    in practice ARM returns the policy as a bare XML document for this
    format - handle both rather than assuming the documented shape.
    Returns `None` if `raw_text` is empty or an unrecognisable JSON shape.
    """
    stripped = raw_text.strip()
    if not stripped:
        return None
    if stripped[0] != "{":
        return raw_text
    try:
        body = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    properties = body.get("properties") or {}
    value = properties.get("value")
    return value if isinstance(value, str) else None


def register_config_tools(
    mcp: FastMCP[Any], registry: list[ToolRegistration], settings: Settings
) -> None:
    """Register the Group B API-configuration tools against `mcp`."""

    @audited_tool(mcp, registry, name="apim_list_apis")
    async def apim_list_apis(
        *,
        ctx: CallContext,
        service: str,
        filter: str | None = None,  # noqa: A002 - matches docs/SPEC.md §6 Group B param name
        include_revisions: bool = False,
        limit: int = 25,
        offset: int = 0,
        response_format: ResponseFormat = "markdown",
    ) -> dict[str, Any] | ToolError:
        """List APIs on one APIM instance.

        Returns per API: `id`, `name`, `displayName`, `path`, `protocols`,
        `apiRevision`, `isCurrent`, `apiVersion`, `apiVersionSetId`,
        `subscriptionRequired`, `type`, `serviceUrl`. `operationCount` is
        always `null` - ARM's list payload doesn't carry it; use
        `apim_get_api` for an API's real operation list. Defaults to
        **current revisions only** (`include_revisions=True` to see all) -
        revision noise otherwise confuses the model badly. `filter` is a
        case-insensitive substring match against name, display name, and
        path.
        """
        config = _resolve_service(settings, service)
        if config is None:
            return invalid_input("service", _UNKNOWN_SERVICE_HINT)
        client = ArmClient(ctx)
        result = await client.list_all(f"{config.resource_id}/apis", api_version=_API_VERSION)
        if result.error is not None:
            return upstream_error(log_detail=f"apim_list_apis: {service}: {result.error.message}")

        items = result.items
        if not include_revisions:
            items = [i for i in items if (i.get("properties") or {}).get("isCurrent", True)]
        if filter:
            items = [i for i in items if _matches_filter(i, filter_text=filter)]

        summaries = [_api_summary(i) for i in items]
        return _paginate(
            summaries,
            limit=limit,
            offset=offset,
            max_bytes=settings.max_response_bytes,
            narrow_param="filter",
        )

    @audited_tool(mcp, registry, name="apim_get_api")
    async def apim_get_api(
        *,
        ctx: CallContext,
        service: str,
        api_id: str,
        include_operations: bool = True,
        response_format: ResponseFormat = "markdown",
    ) -> dict[str, Any] | ToolError:
        """One API's full entity, plus (by default) its operations.

        Each operation: `id`, `displayName`, `method`, `urlTemplate`,
        `description`. If the API has more than 100 operations, returns the
        first 100 with `truncated: true` and a hint to use
        `apim_get_api_spec` for the complete surface.
        """
        config = _resolve_service(settings, service)
        if config is None:
            return invalid_input("service", _UNKNOWN_SERVICE_HINT)
        client = ArmClient(ctx)
        resource_id = f"{config.resource_id}/apis/{api_id}"
        body = await client.get(resource_id, api_version=_API_VERSION)
        if isinstance(body, ToolError):
            if body.kind == "not_found":
                return not_found("api", api_id, service)
            return body
        if not isinstance(body, dict):
            return upstream_error(log_detail=f"apim_get_api: {service}/{api_id}: unexpected shape")

        api = _api_summary(body)
        if not include_operations:
            return api

        ops_result = await client.list_all(f"{resource_id}/operations", api_version=_API_VERSION)
        if ops_result.error is not None:
            return upstream_error(
                log_detail=f"apim_get_api: {service}/{api_id}: {ops_result.error.message}"
            )
        operations = [_operation_summary(o) for o in ops_result.items]
        truncated = len(operations) > _MAX_INLINE_OPERATIONS
        if truncated:
            operations = operations[:_MAX_INLINE_OPERATIONS]
        api["operations"] = operations
        api["truncated"] = truncated
        if truncated:
            api["hint"] = (
                "This API has more than 100 operations. Use `apim_get_api_spec` for the "
                "complete operation surface."
            )
        return api

    @audited_tool(mcp, registry, name="apim_get_policy")
    async def apim_get_policy(
        *,
        ctx: CallContext,
        service: str,
        scope: PolicyScope,
        api_id: str | None = None,
        operation_id: str | None = None,
        product_id: str | None = None,
        response_format: ResponseFormat = "markdown",
    ) -> dict[str, Any] | ToolError:
        """Policy XML at the given scope (`global`, `api`, `operation`, or
        `product`), redacted per docs/SPEC.md §8.2 before being returned:
        sensitive `<set-header>` values and high-entropy substrings (base64,
        hex, JWT, SAS parameters) are replaced with `[REDACTED:reason]`
        markers, so a redacted policy is visibly incomplete rather than
        silently missing content. `{{named-value}}` references are left
        intact and unexpanded - the reference name is useful context and is
        not itself a secret.
        """
        config = _resolve_service(settings, service)
        if config is None:
            return invalid_input("service", _UNKNOWN_SERVICE_HINT)
        resource_id_or_error = _policy_resource_id(
            config, scope=scope, api_id=api_id, operation_id=operation_id, product_id=product_id
        )
        if isinstance(resource_id_or_error, ToolError):
            return resource_id_or_error
        resource_id = resource_id_or_error

        client = ArmClient(ctx)
        raw = await client.get_text(
            resource_id, api_version=_API_VERSION, params={"format": "rawxml"}
        )
        if isinstance(raw, ToolError):
            if raw.kind == "not_found":
                return not_found("policy", scope, service)
            return raw

        policy_xml = _extract_policy_xml(raw)
        if policy_xml is None:
            return upstream_error(
                log_detail=f"apim_get_policy: {service}/{scope}: no policy XML in response"
            )
        return {"scope": scope, "policyXml": redact_policy_xml(policy_xml)}

    @audited_tool(mcp, registry, name="apim_list_products")
    async def apim_list_products(
        *,
        ctx: CallContext,
        service: str,
        limit: int = 25,
        offset: int = 0,
        response_format: ResponseFormat = "markdown",
    ) -> dict[str, Any] | ToolError:
        """List products on one APIM instance: `id`, `name`, `displayName`,
        `description`, `subscriptionRequired`, `approvalRequired`,
        `subscriptionsLimit`, `state`."""
        config = _resolve_service(settings, service)
        if config is None:
            return invalid_input("service", _UNKNOWN_SERVICE_HINT)
        client = ArmClient(ctx)
        result = await client.list_all(f"{config.resource_id}/products", api_version=_API_VERSION)
        if result.error is not None:
            return upstream_error(
                log_detail=f"apim_list_products: {service}: {result.error.message}"
            )
        summaries = [_product_summary(i) for i in result.items]
        return _paginate(
            summaries,
            limit=limit,
            offset=offset,
            max_bytes=settings.max_response_bytes,
            narrow_param="limit",
        )

    @audited_tool(mcp, registry, name="apim_list_backends")
    async def apim_list_backends(
        *,
        ctx: CallContext,
        service: str,
        limit: int = 25,
        offset: int = 0,
        response_format: ResponseFormat = "markdown",
    ) -> dict[str, Any] | ToolError:
        """List backends on one APIM instance: `id`, `name`, `url`,
        `protocol`, `title`, `description`, `tls` settings. Never returns
        `credentials` - use the Azure Portal to inspect backend
        authentication configuration."""
        config = _resolve_service(settings, service)
        if config is None:
            return invalid_input("service", _UNKNOWN_SERVICE_HINT)
        client = ArmClient(ctx)
        result = await client.list_all(f"{config.resource_id}/backends", api_version=_API_VERSION)
        if result.error is not None:
            return upstream_error(
                log_detail=f"apim_list_backends: {service}: {result.error.message}"
            )
        summaries = [_backend_summary(i) for i in result.items]
        return _paginate(
            summaries,
            limit=limit,
            offset=offset,
            max_bytes=settings.max_response_bytes,
            narrow_param="limit",
        )

    @audited_tool(mcp, registry, name="apim_list_named_values")
    async def apim_list_named_values(
        *,
        ctx: CallContext,
        service: str,
        limit: int = 25,
        offset: int = 0,
        response_format: ResponseFormat = "markdown",
    ) -> dict[str, Any] | ToolError:
        """List named values on one APIM instance: `name`, `displayName`,
        `tags`, and `secret` (bool). Returns `value` only for entries where
        `secret` is `false`; **secret values are never returned**, under any
        circumstances - this tool cannot fetch them (the secret-listing
        action is never called) and would not return them even if a source
        payload somehow carried one."""
        config = _resolve_service(settings, service)
        if config is None:
            return invalid_input("service", _UNKNOWN_SERVICE_HINT)
        client = ArmClient(ctx)
        result = await client.list_all(
            f"{config.resource_id}/namedValues", api_version=_API_VERSION
        )
        if result.error is not None:
            return upstream_error(
                log_detail=f"apim_list_named_values: {service}: {result.error.message}"
            )
        summaries = [_named_value_summary(i) for i in result.items]
        return _paginate(
            summaries,
            limit=limit,
            offset=offset,
            max_bytes=settings.max_response_bytes,
            narrow_param="limit",
        )

    @audited_tool(mcp, registry, name="apim_list_subscriptions")
    async def apim_list_subscriptions(
        *,
        ctx: CallContext,
        service: str,
        limit: int = 25,
        offset: int = 0,
        response_format: ResponseFormat = "markdown",
    ) -> dict[str, Any] | ToolError:
        """List subscriptions on one APIM instance: `id`, `displayName`,
        `scope`, `state`, `createdDate`, `ownerId`. Never returns
        `primaryKey` or `secondaryKey`, under any circumstances - this tool
        pins an `api-version` where the read operation does not include
        keys inline, and would strip them even if it did."""
        config = _resolve_service(settings, service)
        if config is None:
            return invalid_input("service", _UNKNOWN_SERVICE_HINT)
        client = ArmClient(ctx)
        result = await client.list_all(
            f"{config.resource_id}/subscriptions", api_version=_API_VERSION
        )
        if result.error is not None:
            return upstream_error(
                log_detail=f"apim_list_subscriptions: {service}: {result.error.message}"
            )
        summaries = [_subscription_summary(i) for i in result.items]
        return _paginate(
            summaries,
            limit=limit,
            offset=offset,
            max_bytes=settings.max_response_bytes,
            narrow_param="limit",
        )
