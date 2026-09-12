"""Acquire a token for optional manual MCP protocol testing.

Authenticates via MSAL (browser-based, bypasses the macOS platform broker)
and writes the token into .vscode/mcp.json. Normal local and hosted VS Code
connections use automatic OAuth discovery and do not need this helper.
Tokens expire after approximately one hour; re-run when they do.

Usage:
    make token
    # or: uv run --no-sync python scripts/get_token.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import msal

from apim_mcp.settings import get_settings

MCP_JSON_PATH = Path(__file__).resolve().parent.parent / ".vscode" / "mcp.json"


def main() -> None:
    settings = get_settings()
    tenant_id = settings.azure_tenant_id
    audience = settings.mcp_server_audience

    # The client app ID — must match the pre-authorized client on the server
    # app registration. Hardcoded here because it's project-specific and
    # doesn't belong in Settings (which is server config, not client config).
    client_id = "4c5ab830-b291-4308-8ae2-70d3f978e4a0"

    app = msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
    )

    scopes = [f"{audience}/Mcp.Tools.Read"]

    # Try silent first (cached from a previous run)
    accounts = app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent(scopes, account=accounts[0])

    if not result or "access_token" not in result:
        print("Opening browser for sign-in...")
        result = app.acquire_token_interactive(scopes=scopes)

    if "access_token" not in result:
        print(
            f"Token acquisition failed: {result.get('error')}: {result.get('error_description')}",
            file=sys.stderr,
        )
        sys.exit(1)

    token = result["access_token"]

    # Write .vscode/mcp.json
    MCP_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    config = {
        "servers": {
            "apim": {
                "type": "http",
                "url": (
                    f"{audience.rstrip('/')}"
                    if audience.startswith("http")
                    else "http://localhost:8000/mcp"
                ),
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }
    MCP_JSON_PATH.write_text(json.dumps(config, indent=2) + "\n")
    print(f"Token written to {MCP_JSON_PATH}")
    print("Restart the MCP server in VS Code to pick it up.")


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    main()
