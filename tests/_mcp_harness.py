"""Shared authenticated MCP test helpers."""

from __future__ import annotations

import time
from typing import Any, cast

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from apim_mcp.auth.middleware import JWKSCache
from apim_mcp.settings import ApimServiceConfig, Settings

TENANT_ID = "11111111-1111-1111-1111-111111111111"
AUDIENCE = "http://testserver/mcp"
REQUIRED_ROLE = "Apim.Read"
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
KID = "mcp-test-kid"
REQUEST_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_JWK = jwt.algorithms.RSAAlgorithm.to_jwk(_PRIVATE_KEY.public_key(), as_dict=True)
_PUBLIC_JWK["kid"] = KID
_PUBLIC_JWK["use"] = "sig"
_JWKS_BODY: dict[str, Any] = {"keys": [_PUBLIC_JWK]}


def settings(
    *,
    services: list[ApimServiceConfig] | None = None,
    audience: str = AUDIENCE,
) -> Settings:
    return Settings(
        azure_tenant_id=TENANT_ID,
        azure_client_id="22222222-2222-2222-2222-222222222222",
        mcp_server_audience=audience,
        mcp_server_app_id=audience,
        mcp_required_role=REQUIRED_ROLE,
        apim_services=services or [],
        applicationinsights_connection_string=(
            "InstrumentationKey=00000000-0000-0000-0000-000000000000"
        ),
    )


def token(
    *,
    roles: list[str] | None = None,
    oid: str = "caller-oid",
    audience: str = AUDIENCE,
) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": audience,
        "exp": now + 3600,
        "nbf": now - 10,
        "iat": now,
        "oid": oid,
        "preferred_username": "caller@example.com",
        "roles": roles if roles is not None else [REQUIRED_ROLE],
    }
    return jwt.encode(claims, _PRIVATE_KEY, algorithm="RS256", headers={"kid": KID})


def jwks_cache() -> JWKSCache:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_JWKS_BODY)

    return JWKSCache(TENANT_ID, transport=httpx.MockTransport(handler))


def rpc(method: str, params: dict[str, Any], *, req_id: int = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}


def authorization_header(*, roles: list[str] | None = None, oid: str = "caller-oid") -> str:
    return "Bearer " + token(roles=roles, oid=oid)


def call_tool(client: TestClient, name: str, arguments: dict[str, Any]) -> httpx.Response:
    return cast(
        httpx.Response,
        client.post(
            "/mcp",
            json=rpc("tools/call", {"name": name, "arguments": arguments}),
            headers={**REQUEST_HEADERS, "Authorization": authorization_header()},
        ),
    )


def result_text(response: httpx.Response) -> str:
    body = response.json()
    text: str = body["result"]["content"][0]["text"]
    return text
