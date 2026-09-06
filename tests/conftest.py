"""Shared pytest fixtures.

No test in this repository may touch the network — every test runs against
recorded fixtures in ``tests/fixtures/`` (see ``tests/fixtures/README.md``).
This module enforces that by blocking real socket connections for the
duration of every test.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import apim_mcp.clients.arm as arm_module
from _fixture_transport import FixtureTransport
from apim_mcp.auth.context import CallContext
from apim_mcp.clients.arm import ArmClient

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class NetworkBlockedError(RuntimeError):
    """Raised when a test attempts to open a real network connection."""


def _guarded_connect(*_args: Any, **_kwargs: Any) -> None:
    raise NetworkBlockedError(
        "Network access is disabled in tests. Use tests/fixtures/ instead of live calls."
    )


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Prevent any test from opening a real socket connection."""
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect)
    yield


class _FixtureToken:
    def __init__(self, token: str) -> None:
        self.token = token


class _FixtureCredential:
    """Stands in for a real Azure credential during replay - no token endpoint exists."""

    async def get_token(self, *scopes: str) -> _FixtureToken:
        return _FixtureToken("fixture-token")


@pytest.fixture
def arm_client(monkeypatch: pytest.MonkeyPatch) -> ArmClient:
    """An `ArmClient` that replays recorded fixtures - see tests/fixtures/README.md.

    Credential acquisition is stubbed (no token endpoint is ever called) and
    all HTTP traffic is served from tests/fixtures/ by `FixtureTransport`, so
    using this fixture involves zero network access, even though `_block_network`
    would also catch a real attempt.
    """
    monkeypatch.setattr(arm_module, "credential_for", lambda ctx, scope: _FixtureCredential())
    ctx = CallContext(oid="fixture-oid", upn="fixture@example.com", roles=(), bearer_token="tok")
    return ArmClient(ctx, transport=FixtureTransport(FIXTURES_DIR))
