"""ASGI token-validation middleware. See `docs/SPEC.md` §4.4.

Validates, in order: bearer header present, signature against cached JWKS,
`iss`, `aud` (exactly one configured value — never a list), `exp`/`nbf`
with <=60s clock skew, and the required `roles` entry. `/healthz` and
`/readyz` bypass all of this — they must work before a caller has ever
authenticated.

The raw inbound token is stashed in a `ContextVar` so tool handlers can
retrieve it via `get_raw_token()`, even though v1 has no caller besides the
managed identity — Appendix A (on-behalf-of) and Appendix B (client token)
both need it, and retrofitting request-scoped access later is painful.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from typing import Any

import httpx
import jwt
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from apim_mcp.settings import Settings

HEALTHZ_PATH = "/healthz"
READYZ_PATH = "/readyz"
MCP_PATH = "/mcp"
OAUTH_PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"
# RFC 9728 §3.1: a client may request the path-suffixed variant
# (`<well-known>/mcp`) instead of the root path, since the protected
# resource lives at `/mcp` rather than at the origin's root. See
# `docs/SPEC.md` §10.2 - serve both, identically.
OAUTH_PROTECTED_RESOURCE_MCP_PATH = OAUTH_PROTECTED_RESOURCE_PATH + MCP_PATH
# Workaround for a known VS Code MCP client bug (`docs/SPEC.md` §10.2.1):
# when an authorization server's issuer URL has a path component (Entra's
# always does - `/<tenant>/v2.0`), VS Code drops the path when building
# its own discovery URL and queries these two well-known paths at *our*
# origin's root instead of Entra's. We answer with Entra's own real,
# unmodified discovery document (never a fabricated one, never our own
# `/authorize`/`/token`/`/register`) so that fallback still lands on Entra.
OAUTH_AUTHORIZATION_SERVER_PATH = "/.well-known/oauth-authorization-server"
OPENID_CONFIGURATION_PATH = "/.well-known/openid-configuration"
# The delegated scope this server exposes (`docs/SPEC.md` §4.3) - fixed by
# this project's own design, not deployment-specific, so it lives here as
# a constant rather than a `Settings` field.
MCP_DELEGATED_SCOPE = "Mcp.Tools.Read"
_UNAUTHENTICATED_PATHS = (
    HEALTHZ_PATH,
    READYZ_PATH,
    OAUTH_PROTECTED_RESOURCE_PATH,
    OAUTH_PROTECTED_RESOURCE_MCP_PATH,
    OAUTH_AUTHORIZATION_SERVER_PATH,
    OPENID_CONFIGURATION_PATH,
)


def entra_issuer(tenant_id: str) -> str:
    """The Entra v2 issuer URL for `tenant_id` - the single source of truth
    for both `TokenValidationMiddleware`'s expected `iss` and the
    `authorization_servers` entry in `/.well-known/oauth-protected-resource`
    (`docs/SPEC.md` §10.2), so the two can never drift apart."""
    return f"https://login.microsoftonline.com/{tenant_id}/v2.0"


_RAW_TOKEN_CTX: ContextVar[str | None] = ContextVar("apim_mcp_raw_token", default=None)
_CLAIMS_CTX: ContextVar[Mapping[str, Any] | None] = ContextVar("apim_mcp_claims", default=None)


def get_raw_token() -> str | None:
    """The inbound bearer token for the current request, if any. See §4.4."""
    return _RAW_TOKEN_CTX.get()


def get_claims() -> Mapping[str, Any] | None:
    """The validated claims (`oid`, `preferred_username`, `roles`) for the audit log (§9)."""
    return _CLAIMS_CTX.get()


class JWKSCache:
    """Caches the tenant's signing keys, keyed by `kid`.

    Refreshes on an unknown `kid`, but never more than once per
    `min_refresh_interval` seconds — an attacker (or a misconfigured
    client) replaying a token with a bogus `kid` must not be able to make
    this server hammer Entra's discovery endpoint.
    """

    def __init__(
        self,
        tenant_id: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        min_refresh_interval: float = 60.0,
    ) -> None:
        self._uri = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
        self._transport = transport
        self._clock = clock
        self._min_refresh_interval = min_refresh_interval
        self._keys: dict[str, Any] = {}
        self._last_refresh: float | None = None

    async def get_signing_key(self, kid: str) -> Any | None:
        if kid not in self._keys:
            await self._maybe_refresh()
        return self._keys.get(kid)

    async def _maybe_refresh(self) -> None:
        now = self._clock()
        if self._last_refresh is not None and now - self._last_refresh < self._min_refresh_interval:
            return
        async with httpx.AsyncClient(transport=self._transport, timeout=10.0) as client:
            response = await client.get(self._uri)
            response.raise_for_status()
        body = response.json()
        keys: dict[str, Any] = {}
        for jwk_dict in body.get("keys", []):
            key_id = jwk_dict.get("kid")
            if key_id:
                keys[key_id] = jwt.PyJWK.from_dict(jwk_dict).key
        self._keys = keys
        self._last_refresh = now


class AuthorizationServerMetadataCache:
    """Caches Entra's real, unmodified OIDC discovery document.

    Exists solely to work around a VS Code MCP client bug (see
    `OAUTH_AUTHORIZATION_SERVER_PATH` above and `docs/SPEC.md` §10.2.1):
    we serve this verbatim at our own well-known paths, never a fabricated
    document, and never implement `/authorize`/`/token`/`/register`
    ourselves - the endpoints named inside the cached document still point
    straight at `login.microsoftonline.com`.
    """

    def __init__(
        self,
        tenant_id: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        min_refresh_interval: float = 3600.0,
    ) -> None:
        self._uri = (
            f"https://login.microsoftonline.com/{tenant_id}/v2.0/.well-known/openid-configuration"
        )
        self._transport = transport
        self._clock = clock
        self._min_refresh_interval = min_refresh_interval
        self._document: dict[str, Any] | None = None
        self._last_refresh: float | None = None

    async def get_document(self) -> dict[str, Any] | None:
        if self._document is None:
            await self._maybe_refresh()
        return self._document

    async def _maybe_refresh(self) -> None:
        now = self._clock()
        if self._last_refresh is not None and now - self._last_refresh < self._min_refresh_interval:
            return
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=10.0) as client:
                response = await client.get(self._uri)
                response.raise_for_status()
        except httpx.HTTPError:
            # §8: errors are results, not exceptions - a transient fetch
            # failure must not crash the request; the caller falls back to
            # a 503 rather than propagating.
            return
        self._document = response.json()
        self._last_refresh = now


async def _send_json(
    send: Send, status: int, body: dict[str, Any], *, extra_headers: list[tuple[bytes, bytes]]
) -> None:
    headers = [(b"content-type", b"application/json"), *extra_headers]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": json.dumps(body).encode()})


async def _send_unauthorized(send: Send, reason: str, *, resource_metadata_url: str) -> None:
    # §10.2: without `resource_metadata`, a client has no way to find the
    # protected-resource metadata except guessing well-known paths against
    # this server's own origin - which is exactly the AS-proxy-style
    # probing (`/authorize`, `/register`, ...) this design avoids.
    www_authenticate = f'Bearer resource_metadata="{resource_metadata_url}"'.encode()
    await _send_json(
        send, 401, {"error": reason}, extra_headers=[(b"www-authenticate", www_authenticate)]
    )


async def _send_forbidden(send: Send, reason: str) -> None:
    await _send_json(send, 403, {"error": reason}, extra_headers=[])


class TokenValidationMiddleware:
    """Raw ASGI middleware — wraps any ASGI app, including FastMCP's streamable-HTTP app."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: Settings,
        jwks_cache: JWKSCache | None = None,
    ) -> None:
        self._app = app
        self._settings = settings
        self._jwks_cache = jwks_cache or JWKSCache(settings.azure_tenant_id)
        self._expected_issuer = entra_issuer(settings.azure_tenant_id)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in _UNAUTHENTICATED_PATHS:
            await self._app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        scheme_prefix = scope.get("scheme", "http")
        host = headers.get("host", "")
        resource_metadata_url = f"{scheme_prefix}://{host}{OAUTH_PROTECTED_RESOURCE_MCP_PATH}"
        auth_header = headers.get("authorization")
        if not auth_header:
            await _send_unauthorized(
                send, "missing Authorization header", resource_metadata_url=resource_metadata_url
            )
            return

        scheme, _, token = auth_header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            await _send_unauthorized(
                send, "malformed Authorization header", resource_metadata_url=resource_metadata_url
            )
            return

        try:
            unverified_header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError:
            await _send_unauthorized(
                send, "malformed token", resource_metadata_url=resource_metadata_url
            )
            return

        kid = unverified_header.get("kid")
        if not kid:
            await _send_unauthorized(
                send, "malformed token", resource_metadata_url=resource_metadata_url
            )
            return

        signing_key = await self._jwks_cache.get_signing_key(kid)
        if signing_key is None:
            await _send_unauthorized(
                send, "unknown key id", resource_metadata_url=resource_metadata_url
            )
            return

        try:
            claims = jwt.decode(
                token,
                key=signing_key,
                algorithms=["RS256"],
                options={"verify_aud": False, "verify_iss": False},
                leeway=60,
            )
        except jwt.ExpiredSignatureError:
            await _send_unauthorized(
                send, "expired token", resource_metadata_url=resource_metadata_url
            )
            return
        except jwt.InvalidTokenError:
            await _send_unauthorized(
                send, "invalid token", resource_metadata_url=resource_metadata_url
            )
            return

        if claims.get("iss") != self._expected_issuer:
            await _send_unauthorized(
                send, "wrong issuer", resource_metadata_url=resource_metadata_url
            )
            return

        aud = claims.get("aud")
        if isinstance(aud, list) or aud != self._settings.mcp_server_app_id:
            await _send_unauthorized(
                send, "wrong audience", resource_metadata_url=resource_metadata_url
            )
            return

        roles = claims.get("roles") or []
        if self._settings.mcp_required_role not in roles:
            await _send_forbidden(send, "missing required role")
            return

        raw_token_reset = _RAW_TOKEN_CTX.set(token)
        claims_reset = _CLAIMS_CTX.set(claims)
        try:
            await self._app(scope, receive, send)
        finally:
            _RAW_TOKEN_CTX.reset(raw_token_reset)
            _CLAIMS_CTX.reset(claims_reset)
