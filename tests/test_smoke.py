"""Trivial smoke test proving the verification gate is meaningfully green."""

from __future__ import annotations

import socket

import pytest

import apim_mcp


def test_package_importable() -> None:
    assert apim_mcp.__version__ == "0.1.0"


def test_network_is_blocked_in_tests() -> None:
    """Guards the invariant documented in tests/fixtures/README.md."""
    with pytest.raises(RuntimeError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("example.com", 80))
