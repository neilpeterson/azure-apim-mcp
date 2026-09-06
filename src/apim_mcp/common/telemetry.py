"""Structured audit logging and the startup permission canary.

See `docs/SPEC.md` §9 (audit event shape) and §4.2 (permission canary).
Under the v1 model every downstream ARM call is made as the shared managed
identity, so the Azure activity log never records which human asked for
what — this module's audit event is the only record. Log arguments in
full; never log a response body (`docs/PRINCIPLES.md` §8, §9).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from apim_mcp.auth.context import CallContext
from apim_mcp.clients.arm import ArmClient
from apim_mcp.settings import ApimServiceConfig

AUDIT_LOGGER_NAME = "apim_mcp.audit"
CANARY_LOGGER_NAME = "apim_mcp.permission_canary"

_audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)
_canary_logger = logging.getLogger(CANARY_LOGGER_NAME)

# Actions in this family return a live secret value. Any appearance in the
# UAMI's effective permissions means someone granted more than the
# read-only role this server needs (docs/PRINCIPLES.md §5).
_SECRET_ACTION_MARKERS = (
    "listsecrets",
    "listkeys",
    "listvalue",
    "/token/action",
    "users" + "/token",
)

PERMISSIONS_API_VERSION = "2022-04-01"


class AuditEvent(BaseModel):
    """One structured event per tool invocation, per §9.

    `arguments` is exactly what the caller sent — logged in full because it
    *is* the compliance record. Response bodies are never included; only
    their size (`result_bytes`) and whether they were cut short
    (`truncated`).
    """

    event: str = "tool_call"
    caller_oid: str
    caller_upn: str
    caller_roles: tuple[str, ...]
    tool: str
    arguments: dict[str, Any]
    service: str | None
    outcome: str
    duration_ms: int
    result_bytes: int
    truncated: bool


def emit_audit_event(audit_event: AuditEvent) -> None:
    """Emit one audit event as a single structured log line.

    Routed through the standard `logging` module so that
    `azure-monitor-opentelemetry`'s logging instrumentation (configured at
    startup) forwards it to Application Insights without this module
    needing to know about OpenTelemetry directly.
    """
    _audit_logger.info(audit_event.model_dump_json())


def is_secret_action(action: str) -> bool:
    """True if `action` would let the caller retrieve a live secret value."""
    lowered = action.lower()
    return any(marker in lowered for marker in _SECRET_ACTION_MARKERS)


async def run_permission_canary(
    ctx: CallContext, services: list[ApimServiceConfig]
) -> dict[str, list[str]]:
    """Resolve the UAMI's effective permissions for every configured scope.

    Logs a WARNING for any scope where an action would let the caller
    retrieve a live secret value (the "list secrets/keys/value" family)
    appears — a canary against someone later assigning Contributor "to fix
    a permissions issue" (§4.2). Returns `{alias: [flagged actions]}` so the
    caller (server startup) can decide how loudly to complain.
    """
    findings: dict[str, list[str]] = {}
    for service in services:
        client = ArmClient(ctx)
        result = await client.list_all(
            f"{service.resource_id}/providers/Microsoft.Authorization/permissions",
            api_version=PERMISSIONS_API_VERSION,
        )
        if result.error is not None:
            _canary_logger.warning(
                "permission canary: could not resolve permissions for %s: %s",
                service.alias,
                result.error.kind,
            )
            continue

        flagged: list[str] = []
        for entry in result.items:
            for action in entry.get("actions", []) or []:
                if is_secret_action(action):
                    flagged.append(action)

        if flagged:
            _canary_logger.warning(
                "permission canary: %s exposes secret-bearing action(s): %s",
                service.alias,
                flagged,
            )
        else:
            _canary_logger.info("permission canary: %s looks read-only", service.alias)
        findings[service.alias] = flagged
    return findings
