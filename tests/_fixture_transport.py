"""Offline HTTP transport that replays recorded ARM fixtures.

See ``tests/fixtures/README.md``. Every request is matched by the part of
its path after ``.../service/<name>`` (e.g. ``""`` for the service itself,
``"/apis"`` for the API list) against the same fixture names that
``scripts/record_fixtures.py`` writes. Nothing here ever opens a socket -
unmapped requests raise instead of falling through to any real transport.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

_SERVICE_SEGMENT = re.compile(r"/service/[^/]+")


class FixtureNotFoundError(RuntimeError):
    """Raised when a request has no matching recorded fixture."""


def fixture_name_for(request: httpx.Request) -> str:
    """Map a request to the fixture file stem that should answer it."""
    path = request.url.path
    match = _SERVICE_SEGMENT.search(path)
    suffix = path[match.end() :].strip("/") if match else path.strip("/")
    parts = suffix.split("/") if suffix else []

    simple_map = {
        "": "service_get",
        "apis": "apis_list",
        "products": "products_list",
        "backends": "backends_list",
        "namedValues": "named_values_list",
        "subscriptions": "subscriptions_list",
        "policies/policy": "policy_global",
        "networkstatus": "network_status",
        "providers/Microsoft.ResourceHealth/availabilityStatuses/current": "resource_health",
    }
    if suffix in simple_map:
        return simple_map[suffix]
    if len(parts) == 3 and parts[0] == "apis" and parts[2] == "operations":
        return f"operations_{parts[1]}"
    if len(parts) == 2 and parts[0] == "apis" and request.url.params.get("export") == "true":
        return f"api_export_{parts[1]}"
    raise FixtureNotFoundError(f"no fixture mapping for path suffix {suffix!r}")


class FixtureTransport(httpx.AsyncBaseTransport):
    """Serves recorded JSON fixtures from ``fixtures_dir`` instead of ARM."""

    def __init__(self, fixtures_dir: Path) -> None:
        self._dir = fixtures_dir

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        name = fixture_name_for(request)
        path = self._dir / f"{name}.json"
        if not path.exists():
            raise FixtureNotFoundError(f"missing fixture file {path.name}")
        body = json.loads(path.read_text())
        return httpx.Response(200, json=body, request=request)
