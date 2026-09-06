"""Tests for src/apim_mcp/auth/middleware.py (T-08). See docs/SPEC.md §4.4."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.types import Receive, Scope, Send

from apim_mcp.auth.middleware import (
    HEALTHZ_PATH,
    OAUTH_AUTHORIZATION_SERVER_PATH,
    OAUTH_PROTECTED_RESOURCE_MCP_PATH,
    OAUTH_PROTECTED_RESOURCE_PATH,
    OPENID_CONFIGURATION_PATH,
    READYZ_PATH,
    JWKSCache,
    TokenValidationMiddleware,
    entra_issuer,
    get_raw_token,
)
from apim_mcp.settings import Settings

TENANT_ID = "11111111-1111-1111-1111-111111111111"
# Real audiences are URL-shaped since §4.3's RFC 8707 `resource`-matching
# requirement (`AADSTS9010010`) - keep the fixture realistic.
AUDIENCE = "http://testserver/mcp"
REQUIRED_ROLE = "Apim.Read"
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
KID = "test-kid-1"

_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(_private_key.public_key(), as_dict=True)
_public_jwk["kid"] = KID
_public_jwk["use"] = "sig"
JWKS_BODY: dict[str, Any] = {"keys": [_public_jwk]}


def _settings(**overrides: Any) -> Settings:
    kwargs: dict[str, Any] = {
        "azure_tenant_id": TENANT_ID,
        "azure_client_id": "22222222-2222-2222-2222-222222222222",
        "mcp_server_audience": AUDIENCE,
        "mcp_server_app_id": AUDIENCE,
        "mcp_required_role": REQUIRED_ROLE,
        "apim_services": [],
        "applicationinsights_connection_string": (
            "InstrumentationKey=00000000-0000-0000-0000-000000000000"
        ),
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


def _make_token(
    *,
    kid: str | None = KID,
    iss: str = ISSUER,
    aud: str | list[str] = AUDIENCE,
    roles: list[str] | None = None,
    exp_delta: float = 3600,
    nbf_delta: float = -10,
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": iss,
        "aud": aud,
        "exp": now + int(exp_delta),
        "nbf": now + int(nbf_delta),
        "iat": now,
        "oid": "caller-oid",
        "preferred_username": "caller@example.com",
        "roles": roles if roles is not None else [REQUIRED_ROLE],
    }
    headers = {"kid": kid} if kid else {}
    return jwt.encode(claims, _private_key, algorithm="RS256", headers=headers)


def _jwks_transport(body: dict[str, Any] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body if body is not None else JWKS_BODY)

    return httpx.MockTransport(handler)


def _jwks_cache(
    *, body: dict[str, Any] | None = None, clock: Callable[[], float] | None = None
) -> JWKSCache:
    kwargs: dict[str, Any] = {"transport": _jwks_transport(body)}
    if clock is not None:
        kwargs["clock"] = clock
    return JWKSCache(TENANT_ID, **kwargs)


async def _echo_app(scope: Scope, receive: Receive, send: Send) -> None:
    body = (get_raw_token() or "").encode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


def _client(middleware_app: Callable[..., Awaitable[None]]) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=middleware_app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def _call(
    app: TokenValidationMiddleware, path: str = "/mcp", *, token: str | None = None
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with _client(app) as client:
        return await client.get(path, headers=headers)


@pytest.mark.parametrize(
    ("case", "token_kwargs", "expected_status"),
    [
        ("expired", {"exp_delta": -3600}, 401),
        ("wrong_iss", {"iss": "https://login.microsoftonline.com/other-tenant/v2.0"}, 401),
        ("wrong_aud", {"aud": "api://someone-else"}, 401),
        ("missing_roles", {"roles": []}, 403),
        ("valid", {}, 200),
    ],
)
async def test_table_driven_claim_validation(
    case: str, token_kwargs: dict[str, Any], expected_status: int
) -> None:
    app = TokenValidationMiddleware(_echo_app, settings=_settings(), jwks_cache=_jwks_cache())
    token = _make_token(**token_kwargs)
    response = await _call(app, token=token)
    assert response.status_code == expected_status, case


async def test_no_header_returns_401_with_www_authenticate() -> None:
    app = TokenValidationMiddleware(_echo_app, settings=_settings(), jwks_cache=_jwks_cache())
    response = await _call(app, token=None)
    assert response.status_code == 401
    expected_metadata_url = f"http://testserver{OAUTH_PROTECTED_RESOURCE_MCP_PATH}"
    assert (
        response.headers["www-authenticate"]
        == f'Bearer resource_metadata="{expected_metadata_url}"'
    )


async def test_malformed_header_returns_401() -> None:
    app = TokenValidationMiddleware(_echo_app, settings=_settings(), jwks_cache=_jwks_cache())
    async with _client(app) as client:
        response = await client.get("/mcp", headers={"Authorization": "not-a-bearer-token"})
    assert response.status_code == 401


async def test_malformed_token_returns_401() -> None:
    app = TokenValidationMiddleware(_echo_app, settings=_settings(), jwks_cache=_jwks_cache())
    response = await _call(app, token="this.is.not-a-jwt")
    assert response.status_code == 401


async def test_unknown_kid_returns_401() -> None:
    app = TokenValidationMiddleware(_echo_app, settings=_settings(), jwks_cache=_jwks_cache())
    token = _make_token(kid="some-other-kid")
    response = await _call(app, token=token)
    assert response.status_code == 401


async def test_valid_token_reaches_the_app_and_returns_200() -> None:
    app = TokenValidationMiddleware(_echo_app, settings=_settings(), jwks_cache=_jwks_cache())
    token = _make_token()
    response = await _call(app, token=token)
    assert response.status_code == 200


async def test_raw_token_available_in_handler() -> None:
    """A handler downstream of the middleware can read the inbound token."""
    app = TokenValidationMiddleware(_echo_app, settings=_settings(), jwks_cache=_jwks_cache())
    token = _make_token()
    response = await _call(app, token=token)
    assert response.text == token


async def test_raw_token_not_leaked_across_requests() -> None:
    """The ContextVar must not leak a token from one request into the next."""
    assert get_raw_token() is None


async def test_healthz_and_readyz_return_200_without_a_token() -> None:
    app = TokenValidationMiddleware(_echo_app, settings=_settings(), jwks_cache=_jwks_cache())
    async with _client(app) as client:
        for path in (HEALTHZ_PATH, READYZ_PATH):
            response = await client.get(path)
            assert response.status_code == 200, path


async def test_oauth_protected_resource_metadata_bypasses_auth() -> None:
    """§10.2: VS Code fetches this before it has ever authenticated, so it
    must never be gated behind `TokenValidationMiddleware` like `/mcp` is.
    Both the root path and the RFC 9728 path-suffixed variant must work."""
    app = TokenValidationMiddleware(_echo_app, settings=_settings(), jwks_cache=_jwks_cache())
    async with _client(app) as client:
        for path in (OAUTH_PROTECTED_RESOURCE_PATH, OAUTH_PROTECTED_RESOURCE_MCP_PATH):
            response = await client.get(path)
            assert response.status_code == 200, path


async def test_authorization_server_metadata_mirror_bypasses_auth() -> None:
    """§10.2.1: the VS Code discovery-bug workaround paths must be
    reachable before the client has ever authenticated, same as the
    protected-resource metadata itself."""
    app = TokenValidationMiddleware(_echo_app, settings=_settings(), jwks_cache=_jwks_cache())
    async with _client(app) as client:
        for path in (OAUTH_AUTHORIZATION_SERVER_PATH, OPENID_CONFIGURATION_PATH):
            response = await client.get(path)
            assert response.status_code == 200, path


def test_entra_issuer_matches_expected_issuer_format() -> None:
    assert entra_issuer(TENANT_ID) == ISSUER


async def test_jwks_cache_refreshes_on_unknown_kid() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=JWKS_BODY)

    cache = JWKSCache(TENANT_ID, transport=httpx.MockTransport(handler))
    key = await cache.get_signing_key(KID)
    assert key is not None
    assert call_count == 1


async def test_jwks_cache_rate_limits_refresh_to_once_per_60s() -> None:
    call_count = 0
    fake_now = 1000.0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=JWKS_BODY)

    cache = JWKSCache(TENANT_ID, transport=httpx.MockTransport(handler), clock=lambda: fake_now)

    # First lookup of an unknown kid triggers a refresh.
    assert await cache.get_signing_key("still-unknown") is None
    assert call_count == 1

    # A second lookup of a (still) unknown kid within 60s must not refetch.
    assert await cache.get_signing_key("still-unknown") is None
    assert call_count == 1

    # Once 60s have passed, an unknown kid triggers exactly one more refresh.
    fake_now += 61.0
    assert await cache.get_signing_key("still-unknown") is None
    assert call_count == 2


def test_aud_is_not_a_list() -> None:
    """Configuring multiple audiences is rejected at Settings construction time."""
    with pytest.raises(Exception, match="MCP_SERVER_AUDIENCE"):
        _settings(mcp_server_audience='["api://a", "api://b"]')

    with pytest.raises(Exception, match="MCP_SERVER_AUDIENCE"):
        _settings(mcp_server_audience="api://a,api://b")
