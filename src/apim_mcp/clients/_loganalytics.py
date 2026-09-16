"""Shared internals for the two Log Analytics-backed telemetry clients.

`clients/logs.py` (the fixed `ApiManagementGatewayLogs` shapes) and
`clients/metrics.py` (the fixed `AzureMetrics` shapes) read different tables
but reach them the same way: parse an ISO 8601 timespan, encode values into a
`declare query_parameters` preamble, resolve the workspace `customerId`
through ARM, then run one query through a per-request `LogsQueryClient` built
on the credential seam.

This module owns only that shared path. What stays with the callers is what
differs between them and must keep differing:

* the query text, so each table's shape is still fixed and auditable in one
  place (`docs/development/PRINCIPLES.md` §6);
* the HTTP-status-to-`ToolError` mapping, passed in as `on_status`, so each
  error still names the data the caller was actually reading (§8);
* the `scope` argument, named explicitly at each call site rather than
  defaulted here, so the OBO retrofit has one audience per call site to find
  (§2).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from azure.core.exceptions import HttpResponseError, ServiceRequestError, ServiceResponseError
from azure.monitor.query.aio import LogsQueryClient

from apim_mcp.auth.context import CallContext
from apim_mcp.auth.credentials import credential_for
from apim_mcp.clients.arm import ArmClient
from apim_mcp.common.errors import ToolError, timeout, upstream_error
from apim_mcp.queries.catalog import QUERY_BY_ID, QueryDefinition

Timespan = timedelta | tuple[datetime, datetime]
"""An Azure Monitor query window: a duration, or an explicit start/end pair."""

StatusErrorMapper = Callable[[int, int], ToolError]
"""Maps ``(status_code, retry_after_seconds)`` to a domain-specific error."""

WORKSPACE_API_VERSION = "2023-09-01"
logger = logging.getLogger("apim_mcp.loganalytics")

# The client budget sits above the server budget so Log Analytics gets the
# chance to return a *partial* result before the client gives up entirely.
_CLIENT_TIMEOUT_SECONDS = 55
_SERVER_TIMEOUT_SECONDS = 50

_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a UTC offset")
    return parsed.astimezone(UTC)


def _parse_duration(value: str) -> timedelta:
    match = _DURATION_RE.fullmatch(value)
    if match is None or not any(match.groupdict().values()):
        raise ValueError("invalid ISO 8601 duration")
    duration = timedelta(
        days=int(match.group("days") or 0),
        hours=int(match.group("hours") or 0),
        minutes=int(match.group("minutes") or 0),
        seconds=float(match.group("seconds") or 0),
    )
    if duration <= timedelta(0):
        raise ValueError("duration must be positive")
    return duration


def parse_timespan(value: str) -> Timespan:
    """Parse an ISO 8601 duration or an RFC3339 ``start/end`` pair."""
    if "/" not in value:
        return _parse_duration(value)
    start_text, end_text = value.split("/", 1)
    start = _parse_datetime(start_text)
    end = _parse_datetime(end_text)
    if end <= start:
        raise ValueError("timespan end must be after start")
    return start, end


def parse_interval(value: str) -> timedelta:
    """Parse a query granularity as a positive ISO 8601 duration."""
    duration = _parse_duration(value)
    if duration < timedelta(seconds=1) or not duration.total_seconds().is_integer():
        raise ValueError("interval must be at least one second and use whole seconds")
    return duration


def kql_string(value: str) -> str:
    """Encode a KQL string literal for a ``declare query_parameters`` preamble.

    `json.dumps` handles quoting and escaping; the extra `;` escape removes
    the one character that could end the declaration statement early even if
    the surrounding quoting were ever wrong.
    """
    return json.dumps(value).replace(";", "\\u003b")


def kql_timespan(value: timedelta) -> str:
    """Render a `timedelta` as a KQL `time(d.hh:mm:ss)` literal."""
    total_seconds = int(value.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"time({days}.{hours:02d}:{minutes:02d}:{seconds:02d})"


def retry_after_seconds(headers: Any) -> int:
    """Read a `Retry-After` header, falling back to 1s when absent or junk."""
    try:
        return int(headers.get("Retry-After", "1"))
    except (TypeError, ValueError, AttributeError):
        return 1


def table_rows(result: Any) -> list[dict[str, Any]]:
    """Flatten an Azure Monitor result into column-keyed dicts.

    Reads `partial_data` when `tables` is empty so a partially successful
    query still yields the rows it did manage to return.
    """
    tables = getattr(result, "tables", None) or getattr(result, "partial_data", None) or []
    rows: list[dict[str, Any]] = []
    for table in tables:
        columns = [
            str(getattr(column, "name", column))
            for column in (getattr(table, "columns", None) or [])
        ]
        rows.extend(
            dict(zip(columns, row, strict=False)) for row in (getattr(table, "rows", None) or [])
        )
    return rows


def has_partial_error(result: Any) -> bool:
    return getattr(result, "partial_error", None) is not None


@dataclass(frozen=True)
class WorkspaceRows:
    """Decoded rows from one workspace query, plus whether they are partial."""

    rows: list[dict[str, Any]]
    partial: bool


def _query_body(query: str) -> str:
    """Return the fixed KQL body without parameter values."""
    declaration, separator, body = query.partition("\n")
    if (
        not separator
        or not declaration.startswith("declare query_parameters(")
        or not declaration.endswith(");")
        or "declare query_parameters(" in body
    ):
        return "<query body unavailable: unrecognized declaration shape>"
    return body


def _query_fingerprint(query: str) -> str:
    return hashlib.sha256(_query_body(query).encode()).hexdigest()[:16]


def _error_codes(error: Any) -> set[str]:
    """Collect machine-readable error codes without reading messages."""
    codes: set[str] = set()
    pending = [error]
    while pending:
        current = pending.pop()
        if current is None:
            continue
        if isinstance(current, Mapping):
            code = current.get("code")
            details = current.get("details")
            inner_error = current.get("innererror") or current.get("innerError")
        else:
            code = getattr(current, "code", None)
            details = getattr(current, "details", None)
            inner_error = getattr(current, "innererror", None) or getattr(
                current, "inner_error", None
            )
        if code is not None:
            codes.add(str(code))
        if isinstance(details, list):
            pending.extend(details)
        if inner_error is not None:
            pending.append(inner_error)
    return codes


def _empty_partial_is_tolerated(
    partial_error: Any,
    tolerated_codes: frozenset[str],
) -> bool:
    """Return whether an empty partial result contains only an allowed warning."""
    codes = _error_codes(partial_error)
    specific_codes = codes - {"PartialError", "PartialQueryFailure"}
    return bool(specific_codes) and specific_codes <= tolerated_codes


def _log_http_error(
    error: HttpResponseError,
    query: str,
    *,
    query_id: str,
) -> None:
    """Log safe correlation metadata and, at DEBUG, the fixed query shape."""
    headers = getattr(error.response, "headers", {}) if error.response else {}
    error_code = getattr(error.error, "code", None)
    fingerprint = _query_fingerprint(query)
    logger.warning(
        (
            "Log Analytics query failed: query_id=%s status=%s code=%s "
            "request_id=%s correlation_request_id=%s query_sha256=%s"
        ),
        query_id,
        error.status_code,
        error_code,
        headers.get("x-ms-request-id"),
        headers.get("x-ms-correlation-request-id"),
        fingerprint,
    )
    logger.debug("Log Analytics fixed query body [%s]:\n%s", fingerprint, _query_body(query))


def _log_partial_error(
    partial_error: Any,
    query: str,
    *,
    query_id: str,
) -> None:
    """Log only the partial error code; messages and details may contain data."""
    fingerprint = _query_fingerprint(query)
    logger.warning(
        "Log Analytics query returned partial error: query_id=%s code=%s query_sha256=%s",
        query_id,
        getattr(partial_error, "code", None),
        fingerprint,
    )
    logger.debug("Log Analytics fixed query body [%s]:\n%s", fingerprint, _query_body(query))


async def _resolve_workspace_id(
    ctx: CallContext,
    workspace_resource_id: str,
) -> str | ToolError:
    """Resolve an ARM workspace resource ID to its Log Analytics `customerId`.

    `LogsQueryClient.query_workspace` addresses workspaces by `customerId`,
    which is not derivable from the resource ID, so it costs one ARM read.
    """
    body = await ArmClient(ctx).get(workspace_resource_id, api_version=WORKSPACE_API_VERSION)
    if isinstance(body, ToolError):
        return body
    if not isinstance(body, dict):
        return upstream_error(log_detail="workspace response had an unexpected shape")
    customer_id = (body.get("properties") or {}).get("customerId")
    if not customer_id:
        return upstream_error(log_detail="workspace response omitted properties.customerId")
    return str(customer_id)


async def run_workspace_query(
    ctx: CallContext,
    *,
    scope: str,
    workspace_resource_id: str,
    definition: QueryDefinition,
    query: str,
    timespan: Timespan,
    on_status: StatusErrorMapper,
    tolerated_empty_partial_codes: frozenset[str] = frozenset(),
) -> WorkspaceRows | ToolError:
    """Run one query against a workspace and decode its rows.

    Constructs the `LogsQueryClient` per call rather than reusing a module
    singleton, because the credential varies per caller under OBO
    (`docs/development/PRINCIPLES.md` §7). `scope` is required, not defaulted,
    for the same reason (§2).
    """
    if QUERY_BY_ID.get(definition.id) is not definition:
        return upstream_error(
            log_detail=f"Unregistered Log Analytics query definition: {definition.id}"
        )
    try:
        async with asyncio.timeout(_CLIENT_TIMEOUT_SECONDS):
            workspace_id = await _resolve_workspace_id(ctx, workspace_resource_id)
            if isinstance(workspace_id, ToolError):
                return workspace_id
            credential = credential_for(ctx, scope)
            async with LogsQueryClient(credential) as client:
                result = await client.query_workspace(
                    workspace_id,
                    query,
                    timespan=timespan,
                    server_timeout=_SERVER_TIMEOUT_SECONDS,
                )
    except HttpResponseError as exc:
        _log_http_error(exc, query, query_id=definition.id)
        headers = getattr(exc.response, "headers", {}) if exc.response else {}
        return on_status(exc.status_code or 500, retry_after_seconds(headers))
    except (TimeoutError, httpx.TimeoutException, ServiceRequestError, ServiceResponseError):
        return timeout()

    rows = table_rows(result)
    partial = has_partial_error(result)
    partial_error = getattr(result, "partial_error", None)
    if partial_error is not None:
        _log_partial_error(partial_error, query, query_id=definition.id)
    if partial and not rows:
        if _empty_partial_is_tolerated(partial_error, tolerated_empty_partial_codes):
            return WorkspaceRows(rows=[], partial=False)
        error_codes = _error_codes(partial_error)
        if any("timeout" in code.lower() for code in error_codes):
            return timeout()
        return upstream_error(
            log_detail=(
                "Log Analytics partial query failed: "
                f"codes={sorted(error_codes)} "
                f"query_sha256={_query_fingerprint(query)}"
            )
        )
    return WorkspaceRows(rows=rows, partial=partial)
