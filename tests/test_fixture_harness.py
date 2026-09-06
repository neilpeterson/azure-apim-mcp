"""Tests for the fixture recorder/replay harness (T-07).

See docs/PRINCIPLES.md and tests/fixtures/README.md. No test in this file
(or anywhere else in the suite) is allowed to touch the network - that is
enforced globally by the `_block_network` autouse fixture in conftest.py.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import httpx
import pytest
from scripts.record_fixtures import FAKE_RG, FAKE_SUB, FAKE_TENANT, GUID, sanitise

import apim_mcp.clients.arm as arm_module
from _fixture_transport import FixtureNotFoundError, FixtureTransport, fixture_name_for
from apim_mcp.auth.context import CallContext
from apim_mcp.clients.arm import ArmClient
from conftest import NetworkBlockedError

RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000"
    "/resourceGroups/rg-fixture"
    "/providers/Microsoft.ApiManagement/service/apim-fixture"
)


class _FakeToken:
    def __init__(self, token: str) -> None:
        self.token = token


class _FakeCredential:
    async def get_token(self, *scopes: str) -> _FakeToken:
        return _FakeToken("fixture-token")


def _request(suffix: str, params: dict[str, str] | None = None) -> httpx.Request:
    return httpx.Request(
        "GET", f"https://management.azure.com{RESOURCE_ID}{suffix}", params=params or {}
    )


def test_sanitise_strips_real_identifiers() -> None:
    """The sanitiser is what `record_fixtures.py` relies on to keep real
    subscription/tenant/hostname data out of committed fixtures."""
    real_sub = "12345678-aaaa-bbbb-cccc-1234567890ab"
    real_tenant = "87654321-dddd-eeee-ffff-ba0987654321"
    real_service = "apim-prod-eastus"
    real_rg = "rg-prod-eastus"
    raw = {
        "id": (
            f"/subscriptions/{real_sub}/resourceGroups/{real_rg}"
            f"/providers/Microsoft.ApiManagement/service/{real_service}"
        ),
        "name": real_service,
        "properties": {
            "resourceGroup": real_rg,
            "tenantId": real_tenant,
            "encodedCertificate": "super-secret-base64",
            "primaryKey": "abc123",
            "clientSecret": "shh",
        },
        "nested": [{"link": "https://storage.blob.core.windows.net/x?sig=abcd&se=2024&sv=1"}],
    }

    clean = sanitise(raw, real_sub=real_sub, real_service=real_service, real_rg=real_rg)
    dumped = json.dumps(clean)

    assert real_sub not in dumped
    assert real_service not in dumped
    assert real_rg not in dumped
    assert real_tenant not in dumped
    assert clean["properties"]["encodedCertificate"] == "[SCRUBBED]"
    assert clean["properties"]["primaryKey"] == "[SCRUBBED]"
    assert clean["properties"]["clientSecret"] == "[SCRUBBED]"
    assert "sig=[SCRUBBED]" in dumped
    assert FAKE_SUB in dumped
    assert FAKE_RG in dumped


def test_fixtures_contain_no_real_identifiers() -> None:
    """Every committed fixture may only contain the documented placeholder
    GUIDs - never anything recorded from a real subscription or tenant.

    Passes vacuously until a human runs `make fixtures`; once fixtures land
    this is the check that stops a real identifier from being committed.
    """
    allowed = {FAKE_SUB, FAKE_TENANT}
    fixtures_dir = Path(__file__).resolve().parent / "fixtures"
    for path in sorted(fixtures_dir.glob("*.json")):
        text = path.read_text()
        for match in GUID.findall(text):
            assert match in allowed, f"{path.name} contains an unrecognised GUID: {match}"


def test_real_network_call_fails_loudly() -> None:
    """conftest.py blocks sockets for every test; prove it actually fires
    rather than silently no-op'ing."""
    with pytest.raises(NetworkBlockedError):
        socket.create_connection(("example.com", 80), timeout=1)


@pytest.mark.parametrize(
    ("suffix", "params", "expected"),
    [
        ("", None, "service_get"),
        ("/apis", None, "apis_list"),
        ("/products", None, "products_list"),
        ("/backends", None, "backends_list"),
        ("/namedValues", None, "named_values_list"),
        ("/subscriptions", None, "subscriptions_list"),
        ("/policies/policy", {"format": "rawxml"}, "policy_global"),
        ("/networkstatus", None, "network_status"),
        (
            "/providers/Microsoft.ResourceHealth/availabilityStatuses/current",
            None,
            "resource_health",
        ),
        ("/apis/echo-api/operations", None, "operations_echo-api"),
        ("/apis/echo-api", {"export": "true"}, "api_export_echo-api"),
    ],
)
def test_fixture_name_for_maps_known_requests(
    suffix: str, params: dict[str, str] | None, expected: str
) -> None:
    assert fixture_name_for(_request(suffix, params)) == expected


def test_fixture_name_for_raises_on_unmapped_request() -> None:
    with pytest.raises(FixtureNotFoundError):
        fixture_name_for(_request("/somethingUnknown"))


async def test_fixture_transport_replays_without_network(tmp_path: Path) -> None:
    """Proven with synthetic fixtures in a throwaway directory - never
    tests/fixtures/, which holds only real recordings (see its README)."""
    (tmp_path / "service_get.json").write_text(json.dumps({"name": "apim-fixture"}))
    (tmp_path / "apis_list.json").write_text(json.dumps({"value": [{"name": "echo-api"}]}))

    transport = FixtureTransport(tmp_path)
    service_response = await transport.handle_async_request(_request(""))
    assert service_response.json() == {"name": "apim-fixture"}

    apis_response = await transport.handle_async_request(_request("/apis"))
    assert apis_response.json() == {"value": [{"name": "echo-api"}]}


async def test_fixture_transport_raises_on_missing_file(tmp_path: Path) -> None:
    transport = FixtureTransport(tmp_path)
    with pytest.raises(FixtureNotFoundError):
        await transport.handle_async_request(_request(""))


async def test_arm_client_replays_fixtures_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercises the harness through the real `ArmClient`, not just the
    transport, matching how later tasks will consume the `arm_client`
    fixture."""
    (tmp_path / "service_get.json").write_text(json.dumps({"name": "apim-fixture"}))
    (tmp_path / "apis_list.json").write_text(json.dumps({"value": [{"name": "echo-api"}]}))

    monkeypatch.setattr(arm_module, "credential_for", lambda ctx, scope: _FakeCredential())
    ctx = CallContext(oid="oid", upn="u@example.com", roles=(), bearer_token="tok")
    client = ArmClient(ctx, transport=FixtureTransport(tmp_path))

    service = await client.get(RESOURCE_ID)
    assert service == {"name": "apim-fixture"}

    apis = await client.list_all(f"{RESOURCE_ID}/apis")
    assert [item["name"] for item in apis.items] == ["echo-api"]
    assert apis.error is None


async def test_conftest_arm_client_fixture_is_wired_for_replay(arm_client: ArmClient) -> None:
    """The shared `arm_client` fixture uses the same replay transport and a
    stubbed credential - no real token endpoint or network call is reachable
    through it. An unmapped request proves it never falls through to a real
    transport; it raises instead of silently succeeding."""
    assert isinstance(arm_client, ArmClient)
    with pytest.raises(FixtureNotFoundError):
        await arm_client.get("/some/unmapped/resource")
