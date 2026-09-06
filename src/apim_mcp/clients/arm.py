"""ArmClient: the only way this server talks to Azure Resource Manager.

See docs/SPEC.md §5.2. Read-only by construction - `docs/PRINCIPLES.md` §4
is enforced by the simple fact that this class exposes no mutating method,
not by a runtime check. `get()` and `list_all()` never raise on an
upstream failure; they return a `ToolError` so callers can render it as a
result rather than crash the tool handler (`docs/PRINCIPLES.md` §8).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx
from tenacity import AsyncRetrying, RetryError, retry_if_exception_type, stop_after_attempt

from apim_mcp.auth.context import CallContext
from apim_mcp.auth.credentials import ARM_SCOPE, credential_for
from apim_mcp.common.errors import (
    ToolError,
    access_denied,
    invalid_input,
    not_found,
    throttled,
    upstream_error,
)

ARM_BASE_URL = "https://management.azure.com"
DEFAULT_API_VERSION = "2024-05-01"
_MAX_ATTEMPTS = 5
_BASE_BACKOFF_SECONDS = 0.5
_MAX_BACKOFF_SECONDS = 8.0

SleepFn = Callable[[float], Awaitable[None]]


async def _default_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


class ArmListResult:
    """Result of :meth:`ArmClient.list_all`."""

    __slots__ = ("error", "items", "truncated")

    def __init__(
        self,
        items: list[dict[str, Any]],
        *,
        truncated: bool = False,
        error: ToolError | None = None,
    ) -> None:
        self.items = items
        self.truncated = truncated
        self.error = error


class _RetryableResponseError(Exception):
    """Internal signal that a response is retryable (429 or 5xx)."""

    def __init__(self, response: httpx.Response, retry_after: float | None) -> None:
        self.response = response
        self.retry_after = retry_after
        super().__init__(f"retryable status {response.status_code}")


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _map_error(response: httpx.Response, *, resource_id: str) -> ToolError:
    status = response.status_code
    if status == 400:
        return invalid_input("resource_id or params", "a well-formed ARM resource ID")
    if status == 403:
        return access_denied(resource_id)
    if status == 404:
        return not_found("resource", resource_id, "unknown")
    if status == 429:
        retry_after = _parse_retry_after(response.headers.get("Retry-After")) or 0
        return throttled(int(retry_after))
    return upstream_error(log_detail=f"{status} from ARM for {resource_id}: {response.text[:500]}")


def _build_auth_header(access_token: str) -> str:
    parts = ["Bearer", access_token]
    return " ".join(parts)


class ArmClient:
    """Read-only ARM wrapper. Construct one per request - see `docs/PRINCIPLES.md` §7."""

    def __init__(
        self,
        ctx: CallContext,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: SleepFn = _default_sleep,
    ) -> None:
        self._ctx = ctx
        self._transport = transport
        self._sleep = sleep

    async def get(
        self,
        resource_id: str,
        *,
        api_version: str = DEFAULT_API_VERSION,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, Any] | list[Any] | ToolError:
        """GET one resource. Most ARM endpoints return a JSON object, but a
        few (e.g. `/networkstatus`) return a bare JSON array - callers must
        check the shape themselves rather than assume a dict."""
        response = await self._send(resource_id, api_version=api_version, params=params)
        if isinstance(response, ToolError):
            return response
        parsed: dict[str, Any] | list[Any] = response.json()
        return parsed

    async def list_all(
        self,
        resource_id: str,
        *,
        api_version: str = DEFAULT_API_VERSION,
        params: Mapping[str, str] | None = None,
        max_pages: int = 20,
    ) -> ArmListResult:
        items: list[dict[str, Any]] = []
        next_url: str | None = None
        page = 0
        while True:
            if next_url is None:
                response = await self._send(resource_id, api_version=api_version, params=params)
            else:
                response = await self._send(next_url, absolute=True)
            if isinstance(response, ToolError):
                return ArmListResult(items, truncated=False, error=response)

            body = response.json()
            items.extend(body.get("value", []))
            page += 1
            next_url = body.get("nextLink")
            if not next_url:
                return ArmListResult(items, truncated=False)
            if page >= max_pages:
                return ArmListResult(items, truncated=True)

    async def _send(
        self,
        resource_id_or_url: str,
        *,
        api_version: str | None = None,
        params: Mapping[str, str] | None = None,
        absolute: bool = False,
    ) -> httpx.Response | ToolError:
        if absolute:
            url = resource_id_or_url
            query: dict[str, str] = {}
        else:
            url = f"{ARM_BASE_URL}{resource_id_or_url}"
            query = dict(params or {})
            if api_version is not None:
                query["api-version"] = api_version

        credential = credential_for(self._ctx, ARM_SCOPE)
        access_token_response = await credential.get_token(ARM_SCOPE)
        headers = {"Authorization": _build_auth_header(access_token_response.token)}

        async def _attempt() -> httpx.Response:
            async with httpx.AsyncClient(transport=self._transport, timeout=30.0) as client:
                response = await client.get(url, params=query, headers=headers)
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                raise _RetryableResponseError(response, retry_after)
            return response

        def _wait(retry_state: Any) -> float:
            outcome = retry_state.outcome
            exc = outcome.exception() if outcome is not None else None
            if isinstance(exc, _RetryableResponseError) and exc.retry_after is not None:
                return float(exc.retry_after)
            attempt = int(retry_state.attempt_number)
            return float(min(_BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)), _MAX_BACKOFF_SECONDS))

        try:
            async for attempt in AsyncRetrying(
                sleep=self._sleep,
                retry=retry_if_exception_type(_RetryableResponseError),
                stop=stop_after_attempt(_MAX_ATTEMPTS),
                wait=_wait,
                reraise=True,
            ):
                with attempt:
                    response = await _attempt()
        except _RetryableResponseError as exc:
            return _map_error(exc.response, resource_id=resource_id_or_url)
        except RetryError as exc:  # pragma: no cover - reraise=True makes this unreachable
            raise exc

        if response.status_code >= 400:
            return _map_error(response, resource_id=resource_id_or_url)
        return response
