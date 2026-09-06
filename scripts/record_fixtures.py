"""Record sanitised ARM responses into tests/fixtures/.

HUMAN ONLY. Requires live Azure credentials (`az login`) and read access to a
non-production APIM instance. Agents must not run this — see AGENTS.md.

Everything written here is committed to the repo, so sanitisation is not
optional. Real subscription IDs, tenant IDs, hostnames and any credential
material are scrubbed before a single byte hits disk.

    export APIM_RECORD_RESOURCE_ID="/subscriptions/.../service/apim-nonprod"
    export APIM_RECORD_WORKSPACE_ID="/subscriptions/.../workspaces/law-nonprod"
    make fixtures
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx
from azure.identity.aio import AzureCliCredential

ARM = "https://management.azure.com"
API_VERSION = "2024-05-01"
FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# Placeholders that fixtures are rewritten to use. Tests assert these appear
# and that nothing matching the real values does.
FAKE_SUB = "00000000-0000-0000-0000-000000000000"
FAKE_TENANT = "11111111-1111-1111-1111-111111111111"
FAKE_SERVICE = "apim-fixture"
FAKE_RG = "rg-fixture"

GUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
SAS = re.compile(r"[?&](sig|sv|se|st|sp|sr)=[^&\s\"]*")
BEARER = re.compile(r"Bearer\s+[A-Za-z0-9._\-]+")
CERT_FIELDS = {
    "encodedCertificate",
    "certificatePassword",
    "primaryKey",
    "secondaryKey",
    "value",  # named-value payloads
    "clientSecret",
    "password",
}


def sanitise(obj: Any, *, real_sub: str, real_service: str, real_rg: str) -> Any:
    """Recursively scrub identifiers and credential material.

    Order matters: replace known real values first so the generic GUID sweep
    doesn't map two different real IDs onto the same placeholder.
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k in CERT_FIELDS and isinstance(v, str):
                out[k] = "[SCRUBBED]"
            else:
                out[k] = sanitise(v, real_sub=real_sub, real_service=real_service, real_rg=real_rg)
        return out
    if isinstance(obj, list):
        return [
            sanitise(i, real_sub=real_sub, real_service=real_service, real_rg=real_rg) for i in obj
        ]
    if isinstance(obj, str):
        s = obj.replace(real_sub, FAKE_SUB)
        s = s.replace(real_service, FAKE_SERVICE)
        s = s.replace(real_rg, FAKE_RG)
        s = SAS.sub(r"\1=[SCRUBBED]", s)
        s = BEARER.sub("Bearer [SCRUBBED]", s)
        # FAKE_SUB is itself GUID-shaped, so the generic sweep must leave it
        # alone - otherwise the subscription ID placeholder just inserted
        # above gets immediately overwritten with the generic tenant one.
        s = GUID.sub(lambda m: m.group(0) if m.group(0) == FAKE_SUB else FAKE_TENANT, s)
        return s
    return obj


class FixtureFetchError(RuntimeError):
    """A response was not JSON we can sanitise and record."""


async def fetch(client: httpx.AsyncClient, token: str, url: str, params: dict[str, str]) -> Any:
    r = await client.get(
        url,
        params={"api-version": API_VERSION, **params},
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    )
    r.raise_for_status()
    try:
        return r.json()
    except json.JSONDecodeError as exc:
        raise FixtureFetchError(
            f"non-JSON response (status {r.status_code}, "
            f"content-type {r.headers.get('content-type')!r}, "
            f"body starts {r.text[:120]!r})"
        ) from exc


async def main() -> int:
    resource_id = os.environ.get("APIM_RECORD_RESOURCE_ID")
    if not resource_id:
        print("APIM_RECORD_RESOURCE_ID is not set. See the module docstring.", file=sys.stderr)
        return 1

    parts = resource_id.strip("/").split("/")
    real_sub, real_rg, real_service = parts[1], parts[3], parts[-1]

    FIXTURES.mkdir(parents=True, exist_ok=True)

    cred = AzureCliCredential()
    token = (await cred.get_token(f"{ARM}/.default")).token

    # (fixture name, path suffix, extra query params)
    targets: list[tuple[str, str, dict[str, str]]] = [
        ("service_get", "", {}),
        ("apis_list", "/apis", {}),
        ("products_list", "/products", {}),
        ("backends_list", "/backends", {}),
        ("named_values_list", "/namedValues", {}),
        ("subscriptions_list", "/subscriptions", {}),
        ("policy_global", "/policies/policy", {"format": "rawxml"}),
        ("network_status", "/networkstatus", {}),
        (
            "resource_health",
            "/providers/Microsoft.ResourceHealth/availabilityStatuses/current",
            {"api-version": "2023-07-01-preview"},
        ),
    ]

    async with httpx.AsyncClient() as client:
        for name, suffix, params in targets:
            url = f"{ARM}{resource_id}{suffix}"
            try:
                raw = await fetch(client, token, url, params)
            except httpx.HTTPStatusError as exc:
                print(f"  skip {name}: HTTP {exc.response.status_code}", file=sys.stderr)
                continue
            except FixtureFetchError as exc:
                print(f"  skip {name}: {exc}", file=sys.stderr)
                continue

            clean = sanitise(raw, real_sub=real_sub, real_service=real_service, real_rg=real_rg)
            (FIXTURES / f"{name}.json").write_text(json.dumps(clean, indent=2) + "\n")
            print(f"  wrote {name}.json")

            # Per-API detail for the first three APIs only — enough to exercise
            # paging and operation indexing without committing a huge corpus.
            if name == "apis_list":
                for idx, api in enumerate((clean.get("value") or [])[:3]):
                    api_id = api["name"]
                    try:
                        ops = await fetch(client, token, f"{url}/{api_id}/operations", {})
                    except (httpx.HTTPStatusError, FixtureFetchError) as exc:
                        print(f"  skip operations_{api_id}: {exc}", file=sys.stderr)
                        continue
                    ops_clean = sanitise(
                        ops, real_sub=real_sub, real_service=real_service, real_rg=real_rg
                    )
                    (FIXTURES / f"operations_{api_id}.json").write_text(
                        json.dumps(ops_clean, indent=2) + "\n"
                    )
                    print(f"  wrote operations_{api_id}.json")

                    # Export-link response for the first API only. The link
                    # itself points at a real SAS-secured blob and expires in
                    # five minutes, so there is nothing durable to record
                    # beyond the shape of this envelope — sanitise() scrubs
                    # the SAS query out of the returned link.
                    if idx == 0:
                        try:
                            export = await fetch(
                                client,
                                token,
                                f"{url}/{api_id}",
                                {"export": "true", "format": "openapi+json-link"},
                            )
                        except (httpx.HTTPStatusError, FixtureFetchError) as exc:
                            print(f"  skip api_export_{api_id}: {exc}", file=sys.stderr)
                            continue
                        export_clean = sanitise(
                            export, real_sub=real_sub, real_service=real_service, real_rg=real_rg
                        )
                        (FIXTURES / f"api_export_{api_id}.json").write_text(
                            json.dumps(export_clean, indent=2) + "\n"
                        )
                        print(f"  wrote api_export_{api_id}.json")

    await cred.close()

    # Fail loudly rather than committing a leak.
    leaked = [
        p.name
        for p in FIXTURES.glob("*.json")
        if real_sub in p.read_text() or real_service in p.read_text()
    ]
    if leaked:
        print(f"\nSANITISATION FAILED in: {leaked}", file=sys.stderr)
        return 2

    print(f"\nDone. Fixtures in {FIXTURES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
