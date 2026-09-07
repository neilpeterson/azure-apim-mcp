"""FastMCP app bootstrap: middleware wiring, health endpoints, and the
audited-tool registration decorator every Group A-D tool (T-10+) uses.

See `docs/SPEC.md` §9, §10 and `docs/PRINCIPLES.md` §8 ("errors are
results, not exceptions").
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import time
import typing
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from apim_mcp.auth.context import CallContext
from apim_mcp.auth.middleware import (
    MCP_DELEGATED_SCOPE,
    MCP_PATH,
    OAUTH_AUTHORIZATION_SERVER_PATH,
    OAUTH_PROTECTED_RESOURCE_MCP_PATH,
    OAUTH_PROTECTED_RESOURCE_PATH,
    OPENID_CONFIGURATION_PATH,
    AuthorizationServerMetadataCache,
    JWKSCache,
    TokenValidationMiddleware,
    entra_issuer,
    get_claims,
    get_raw_token,
)
from apim_mcp.common.errors import ToolError, error_envelope, upstream_error
from apim_mcp.common.formatting import ResponseFormat, render
from apim_mcp.common.telemetry import AuditEvent, emit_audit_event, run_permission_canary
from apim_mcp.index.search import IndexManager
from apim_mcp.settings import Settings, get_settings

logger = logging.getLogger(__name__)

SERVER_NAME = "apim-mcp"

# Every tool is read-only by construction (docs/PRINCIPLES.md §4) — these
# annotations are identical for all of them, per docs/SPEC.md §6.0.
_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


class ToolRegistration:
    """One entry in the audited-tool registry.

    `test_every_tool_emits_audit_event` iterates this list rather than
    reaching into FastMCP's internals, so the registry is the contract
    between `audited_tool` and its tests.
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


def current_call_context() -> CallContext:
    """Build the `CallContext` for the in-flight request from the
    claims/token the middleware (T-08) stashed in its `ContextVar`s."""
    claims = get_claims() or {}
    roles_claim = claims.get("roles") or ()
    return CallContext(
        oid=str(claims.get("oid", "")),
        upn=str(claims.get("preferred_username", "")),
        roles=tuple(roles_claim),
        bearer_token=get_raw_token() or "",
    )


def _result_payload(result: dict[str, Any] | ToolError) -> dict[str, Any]:
    """Never hand a `ToolError` object straight to the transport — every
    tool result is a plain, JSON-serialisable dict (`docs/SPEC.md` §6.0)."""
    if isinstance(result, ToolError):
        return error_envelope(result)
    return result


def _outcome_for(result: dict[str, Any] | ToolError) -> str:
    if isinstance(result, ToolError):
        return result.kind
    return "ok"


def audited_tool(
    mcp: FastMCP[Any],
    registry: list[ToolRegistration],
    *,
    name: str,
) -> Callable[
    [Callable[..., Awaitable[dict[str, Any] | ToolError]]],
    Callable[..., Awaitable[dict[str, Any] | str]],
]:
    """Register `fn` as an MCP tool, wrapped so that every call:

    1. Receives a `ctx: CallContext` built from the validated inbound
       token — `ctx` is *not* part of the tool's public JSON schema; the
       decorator strips it from the signature FastMCP inspects.
    2. Never raises out of the handler: a stray exception becomes an
       `upstream_error` result and is logged at ERROR
       (`docs/PRINCIPLES.md` §8).
    3. Emits exactly one §9 audit event, regardless of outcome.
    4. Renders the structured result per the caller's `response_format`
       (`docs/SPEC.md` §6.0) — `fn` itself only ever builds structured
       data; every Group A-D tool gets markdown/JSON rendering for free
       rather than reimplementing it.
    """

    def decorator(
        fn: Callable[..., Awaitable[dict[str, Any] | ToolError]],
    ) -> Callable[..., Awaitable[dict[str, Any] | str]]:
        original_sig = inspect.signature(fn)
        # `fn` is written with `from __future__ import annotations`, so its
        # parameter annotations are unevaluated strings; `wrapper` lives in
        # this module's globals, not `fn`'s, so FastMCP would fail to
        # resolve a name like `ResponseFormat` if we copied them verbatim.
        # Resolve them here, against `fn`'s own module globals, instead.
        resolved_hints = typing.get_type_hints(fn)
        public_params = [
            p.replace(annotation=resolved_hints.get(p.name, p.annotation))
            for p in original_sig.parameters.values()
            if p.name != "ctx"
        ]
        public_sig = original_sig.replace(parameters=public_params)

        @functools.wraps(fn)
        async def wrapper(**kwargs: Any) -> dict[str, Any] | str:
            ctx = current_call_context()
            started = time.monotonic()
            result: dict[str, Any] | ToolError
            try:
                result = await fn(ctx=ctx, **kwargs)
            except Exception:
                logger.exception("tool %s raised an unhandled exception", name)
                result = upstream_error(log_detail=f"unhandled exception in tool {name}")

            payload = _result_payload(result)
            duration_ms = int((time.monotonic() - started) * 1000)
            event = AuditEvent(
                caller_oid=ctx.oid,
                caller_upn=ctx.upn,
                caller_roles=ctx.roles,
                tool=name,
                arguments=dict(kwargs),
                service=kwargs.get("service"),
                outcome=_outcome_for(result),
                duration_ms=duration_ms,
                result_bytes=len(_render_for_size(payload)),
                truncated=bool(payload.get("truncated", False)),
            )
            emit_audit_event(event)

            # Errors always render as structured JSON — the model needs the
            # exact `kind`/`message` fields, not a lossy markdown summary.
            if isinstance(result, ToolError):
                return payload
            response_format: ResponseFormat = kwargs.get("response_format", "markdown")
            return render(payload, response_format)

        wrapper.__signature__ = public_sig  # type: ignore[attr-defined]
        mcp.tool(name=name, annotations=_TOOL_ANNOTATIONS)(wrapper)
        registry.append(ToolRegistration(name))
        return wrapper

    return decorator


def _render_for_size(payload: dict[str, Any]) -> bytes:
    import json

    return json.dumps(payload, default=str).encode("utf-8")


async def _healthz(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def _make_readyz(settings: Settings) -> Callable[[Request], Awaitable[JSONResponse]]:
    async def _readyz(_request: Request) -> JSONResponse:
        # Ready once config validates - deliberately *not* gated on index
        # build (§9), so a slow index doesn't fail readiness probes. The
        # settings this app was built with are already validated Pydantic
        # objects; re-checking here would just re-read the environment.
        try:
            settings.model_dump()
        except Exception as exc:
            return JSONResponse({"status": "not_ready", "reason": str(exc)}, status_code=503)
        return JSONResponse({"status": "ok"})

    return _readyz


def _make_oauth_protected_resource(
    settings: Settings,
) -> Callable[[Request], Awaitable[JSONResponse]]:
    """RFC 9728 protected-resource metadata (`docs/SPEC.md` §10.2) - lets VS
    Code (and any other MCP client implementing the authorization spec)
    discover the Entra tenant to authenticate against without a
    hand-configured header. Unauthenticated, like `/healthz`/`/readyz`
    (`TokenValidationMiddleware` exempts this exact path)."""

    async def _oauth_protected_resource(request: Request) -> JSONResponse:
        # §4.3/§5.3: `resource` must equal `MCP_SERVER_AUDIENCE` exactly
        # (RFC 8707 `resource`-matching, `AADSTS9010010`) - use the
        # configured value directly rather than re-deriving it from the
        # inbound request, so the two can never drift apart.
        return JSONResponse(
            {
                "resource": settings.mcp_server_audience,
                "authorization_servers": [entra_issuer(settings.azure_tenant_id)],
                "scopes_supported": [MCP_DELEGATED_SCOPE],
                "bearer_methods_supported": ["header"],
            }
        )

    return _oauth_protected_resource


def _make_authorization_server_metadata(
    cache: AuthorizationServerMetadataCache,
) -> Callable[[Request], Awaitable[JSONResponse]]:
    """Mirrors Entra's real, unmodified OIDC discovery document at our own
    well-known paths - a workaround for a VS Code MCP client bug, not a
    protocol requirement. See `AuthorizationServerMetadataCache` and
    `docs/SPEC.md` §10.2.1 for the full rationale. Never fabricates a
    document and never adds a `registration_endpoint` - only ever returns
    what Entra itself publishes."""

    async def _authorization_server_metadata(request: Request) -> JSONResponse:
        document = await cache.get_document()
        if document is None:
            return JSONResponse(
                {"error": "authorization server metadata temporarily unavailable"},
                status_code=503,
            )
        return JSONResponse(document)

    return _authorization_server_metadata


def create_mcp(
    settings: Settings,
    *,
    allowed_hosts: list[str] | None = None,
    auth_metadata_transport: httpx.AsyncBaseTransport | None = None,
) -> FastMCP[Any]:
    """Build the FastMCP instance: streamable HTTP, stateless, JSON responses at `/mcp`.

    `allowed_hosts` guards against DNS-rebinding; production passes the
    Container Apps ingress hostname, tests pass `["testserver"]`. Every
    non-health route still requires a valid bearer token via
    `TokenValidationMiddleware`, so this is defence in depth, not the only
    control. `auth_metadata_transport` lets tests inject an offline
    transport for `AuthorizationServerMetadataCache` instead of the
    default, which calls Entra's real discovery endpoint.
    """
    mcp: FastMCP[Any] = FastMCP(
        SERVER_NAME,
        stateless_http=True,
        json_response=True,
        streamable_http_path=MCP_PATH,
        transport_security=TransportSecuritySettings(
            # DNS-rebinding protection needs an explicit, known Host header
            # allowlist to be useful; the real production hostname isn't
            # part of docs/SPEC.md §5.3's configuration surface. Every
            # non-health route already requires a valid bearer token via
            # `TokenValidationMiddleware`, so when no `allowed_hosts` is
            # given (production default) this layer is disabled rather
            # than silently rejecting every request with an invented rule.
            enable_dns_rebinding_protection=bool(allowed_hosts),
            allowed_hosts=allowed_hosts or [],
            allowed_origins=allowed_hosts or [],
        ),
    )
    mcp.custom_route("/healthz", methods=["GET"])(_healthz)
    mcp.custom_route("/readyz", methods=["GET"])(_make_readyz(settings))
    oauth_protected_resource = _make_oauth_protected_resource(settings)
    # RFC 9728 §3.1: serve both the root and the path-suffixed variant
    # identically - VS Code (and other spec-compliant clients) may probe
    # either. See docs/SPEC.md §10.2.
    mcp.custom_route(OAUTH_PROTECTED_RESOURCE_PATH, methods=["GET"])(oauth_protected_resource)
    mcp.custom_route(OAUTH_PROTECTED_RESOURCE_MCP_PATH, methods=["GET"])(oauth_protected_resource)
    auth_metadata_cache = AuthorizationServerMetadataCache(
        settings.azure_tenant_id, transport=auth_metadata_transport
    )
    authorization_server_metadata = _make_authorization_server_metadata(auth_metadata_cache)
    # §10.2.1: both names are workaround targets for the same VS Code bug -
    # some client versions probe one, some the other.
    mcp.custom_route(OAUTH_AUTHORIZATION_SERVER_PATH, methods=["GET"])(
        authorization_server_metadata
    )
    mcp.custom_route(OPENID_CONFIGURATION_PATH, methods=["GET"])(authorization_server_metadata)
    return mcp


def wrap_with_middleware(
    mcp: FastMCP[Any], settings: Settings, *, jwks_cache: JWKSCache | None = None
) -> ASGIApp:
    """Wrap the FastMCP ASGI app with `TokenValidationMiddleware` (T-08).

    `jwks_cache` lets tests inject an offline JWKS source instead of the
    default cache, which calls Entra's real discovery endpoint.
    """
    return TokenValidationMiddleware(
        mcp.streamable_http_app(), settings=settings, jwks_cache=jwks_cache
    )


def create_app(
    *, settings: Settings | None = None, allowed_hosts: list[str] | None = None
) -> ASGIApp:
    """Build the full server: FastMCP app, Group A-D tools, middleware, and
    the startup canary. Imports `apim_mcp.tools` lazily (rather than at
    module scope) since those modules import `audited_tool`/`ToolRegistration`
    from here — a module-level import would be circular."""
    from apim_mcp.index.search import IndexManager
    from apim_mcp.tools.config import register_config_tools
    from apim_mcp.tools.discovery import register_discovery_tools
    from apim_mcp.tools.search import register_search_tools

    resolved_settings = settings or get_settings()
    mcp = create_mcp(resolved_settings, allowed_hosts=allowed_hosts)
    registry: list[ToolRegistration] = []
    register_discovery_tools(mcp, registry, resolved_settings)
    register_config_tools(mcp, registry, resolved_settings)
    # One `IndexManager` per running server, not module-level - see
    # docs/PRINCIPLES.md §7 and `apim_mcp.index.search.IndexManager`.
    index_manager = IndexManager(resolved_settings)
    register_search_tools(mcp, registry, resolved_settings, index_manager)
    app = wrap_with_middleware(mcp, resolved_settings)
    return _StartupCanaryApp(app, resolved_settings, index_manager=index_manager)


async def _run_startup_canary(settings: Settings) -> None:
    """Run the §4.2 permission canary once per process start.

    Uses a synthetic caller identity: `credential_for` (T-05) ignores
    `ctx` in v1 (every call uses the shared managed identity), so this is
    safe, but it must never leak into an audit event as if it were a real
    caller.
    """
    if not settings.apim_services:
        logger.info("permission canary: no APIM_SERVICES configured, skipping")
        return
    ctx = CallContext(oid="startup", upn="startup", roles=(), bearer_token="")
    try:
        await run_permission_canary(ctx, settings.apim_services)
    except Exception:
        logger.exception("permission canary failed to run at startup")


class _StartupCanaryApp:
    """Runs the permission canary exactly once, on the ASGI `lifespan`
    startup event, before delegating the rest of the lifespan protocol
    (including the wrapped app's own startup/shutdown) to `app`.

    Also kicks off the eager first index build (§7.5) on the same
    `lifespan.startup` event, but as a *fire-and-forget* background task
    rather than something this class awaits - "build once eagerly at
    startup so the first query isn't slow. Do not block readiness on it."
    A slow or large-tenant index build must never delay `/readyz` or the
    first MCP request answering.

    A thin ASGI-level wrapper rather than an inner `Callable[[FastMCP],
    AbstractAsyncContextManager]` passed to `FastMCP(lifespan=...)`,
    because that hook is driven by the low-level session lifespan (which
    can run more than once under `stateless_http`), not once per process.
    """

    def __init__(
        self, app: ASGIApp, settings: Settings, *, index_manager: IndexManager | None = None
    ) -> None:
        self._app = app
        self._settings = settings
        self._index_manager = index_manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "lifespan":
            await self._app(scope, receive, send)
            return

        canary_ran = False

        async def _receive() -> Any:
            nonlocal canary_ran
            message = await receive()
            if message["type"] == "lifespan.startup" and not canary_ran:
                canary_ran = True
                await _run_startup_canary(self._settings)
                if self._index_manager is not None and self._settings.apim_services:
                    ctx = CallContext(oid="startup", upn="startup", roles=(), bearer_token="")
                    asyncio.create_task(self._index_manager.build_all(ctx))  # noqa: RUF006
            return message

        await self._app(scope, _receive, send)


def main() -> None:
    """Entry point for `make run` (`python -m apim_mcp.server`).

    Loads `.env` from the current directory first, if present — a local-dev
    convenience so env vars don't need to be exported in every shell. Real
    exported env vars still win (`override=False`); `.env` is gitignored and
    never read by `Settings`/tests directly, only here. See
    `docs/LOCAL_TESTING.md`.
    """
    from dotenv import load_dotenv

    load_dotenv()

    import uvicorn

    settings = get_settings()
    app = create_app(settings=settings)
    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104


if __name__ == "__main__":
    main()
