# Deployment and authentication

How to run the MCP server and connect a client. See `docs/SPEC.md` §4 for
the authoritative spec; this doc is the practical walkthrough.

---

## Quick start — local dev

**Prerequisites:** `az login` session with Reader on your APIM resource,
the Entra app registrations below already created.

```bash
# 1. Install dependencies (once)
uv sync

# 2. Configure
cp .env.example .env
#    Fill in: AZURE_TENANT_ID, MCP_SERVER_AUDIENCE, MCP_SERVER_APP_ID,
#    APIM_SERVICES, APIM_MCP_LOCAL_DEV_CREDENTIAL=1

# 3. Start the server
make run

# 4. Smoke-test (no token needed)
curl -s http://localhost:8000/healthz
curl -s http://localhost:8000/.well-known/oauth-protected-resource

# 5. Get a token and write it to .vscode/mcp.json
make token       # opens browser for sign-in, writes token to .vscode/mcp.json

# 6. Connect VS Code
#    Reload VS Code window → MCP: List Servers → apim should show "Running"
#    Token expires in ~1 hour — re-run `make token` when it does.
```

---

## How auth works

Two independent questions, two independent mechanisms:

```
              INBOUND                          OUTBOUND
              (who's calling us?)               (what can we call?)

VS Code  ──bearer token──▶  apim-mcp server  ──credential──▶  Azure APIM
          (Entra OAuth)      (validates token,     (AzureCliCredential locally,
                              runs the tool)        ManagedIdentityCredential deployed)
```

- **Inbound** — Entra JWT, validated by the server's middleware. Same flow
  whether the server is local or deployed.
- **Outbound** — `credential_for()` in `src/apim_mcp/auth/credentials.py`.
  Locally uses your `az login`; deployed uses the UAMI.

---

## One-time setup: Entra app registrations

Two app registrations, created once, reused for local and deployed.

### 1. Server app (`apim-mcp-server`)

1. **App registrations** → New → name it.
2. **Manifest** → confirm `"requestedAccessTokenVersion": 2` under `api`.
3. **Expose an API** → set Application ID URI to the server's canonical URL
   (e.g. `http://localhost:8000/mcp`). Add both local and deployed URLs so
   one registration works everywhere. **Not** an `api://` string — Entra
   rejects tokens with `AADSTS9010010` if the URI doesn't match the
   client's `resource` parameter exactly.
   - Add scope `Mcp.Tools.Read` — set **Who can consent** to
     "Admins and users" (not "Admins only").
4. **App roles** → create `Apim.Read`, allowed member type `Users/Groups`.
5. **Enterprise applications** → find the app → **Properties** →
   **Assignment required = Yes**.
6. **Users and groups** → assign yourself the `Apim.Read` role.
7. **Expose an API** → **Authorized client applications** → add the client
   app's ID (below) with `Mcp.Tools.Read` checked.

### 2. Client app (`apim-mcp-client`)

1. **App registrations** → New → name it.
2. **Authentication** → Add platform → Mobile and desktop → add redirect
   URIs: `http://localhost`, `http://127.0.0.1:33418`,
   `https://vscode.dev/redirect`.
3. **API permissions** → Add → APIs my organization uses →
   `apim-mcp-server` → Delegated → `Mcp.Tools.Read` → Add.
   If you have admin rights, click **Grant admin consent**. If not, the
   pre-authorization in step 7 above + "Admins and users" consent type
   handles it.

### Granting access to others

**Enterprise applications** → `apim-mcp-server` → **Users and groups** →
assign the `Apim.Read` role. This is the per-user gate — outbound data
access is the same for everyone (governed by the UAMI's RBAC, not the
caller's Azure permissions).

---

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `AZURE_TENANT_ID` | yes | Entra tenant GUID. |
| `AZURE_CLIENT_ID` | yes | UAMI client ID. Placeholder locally (`00000000-...`). |
| `MCP_SERVER_AUDIENCE` | yes | Application ID URI (a URL). Used in `/.well-known/oauth-protected-resource`. |
| `MCP_SERVER_APP_ID` | yes | Server app registration's Application (client) ID (a GUID). Entra v2 tokens set `aud` to this, not the URI. |
| `MCP_REQUIRED_ROLE` | no | Default `Apim.Read`. |
| `APIM_SERVICES` | yes | JSON: `[{"alias":"prod","resourceId":"/subscriptions/..."}]` |
| `APIM_MCP_LOCAL_DEV_CREDENTIAL` | no | Set to `1` locally for `AzureCliCredential`. Never set deployed. |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | yes | Real string or `InstrumentationKey=00000000-...` placeholder. |

---

## Connecting clients

### VS Code (Copilot Chat)

**Deployed** — VS Code handles OAuth automatically:

```json
{
  "servers": {
    "apim": {
      "type": "http",
      "url": "https://<app>.<region>.azurecontainerapps.io/mcp"
    }
  }
}
```

Start the server via **MCP: List Servers**. VS Code discovers auth via
`/.well-known/oauth-protected-resource`, prompts for the client app ID
once, and refreshes tokens automatically.

**Local** — VS Code's OAuth flow may fail on corporate-managed Macs
(`platform_broker_error` from the macOS SSO Extension). Workaround: use
`make token` to acquire a token via MSAL Python (browser-based, bypasses
the broker) and inject it into `.vscode/mcp.json` as a static header.

```bash
make token    # opens browser, writes token to .vscode/mcp.json
```

Reload VS Code window after running. Token expires in ~1 hour; re-run
`make token` when it does. `.vscode/mcp.json` is gitignored.

### Foundry

```bash
azd ai connection create apim-mcp-conn \
  --kind remote-tool \
  --target "https://<app>.<region>.azurecontainerapps.io/mcp" \
  --auth-type user-entra-token \
  --audience https://<app>.<region>.azurecontainerapps.io/mcp
```

Set `require_approval: "never"` — every tool is read-only.

---

## Deployed (Container Apps)

Not yet automated (`infra/main.bicep` is T-20 in `TASKS.md`). Manual steps:

1. Create a UAMI (e.g. `id-apim-mcp`).
2. Assign the custom `APIM Knowledge Reader` role (`docs/SPEC.md` §4.2) at
   the narrowest scope that works.
3. Attach the UAMI to the Container App. Set `AZURE_CLIENT_ID` to its
   client ID.
4. Set env vars per the table above. `APIM_MCP_LOCAL_DEV_CREDENTIAL` unset.
5. Container App: single container, ingress external, port `8000`,
   `minReplicas: 1`. Egress: `management.azure.com`,
   `login.microsoftonline.com`, `api.loganalytics.io`,
   `*.blob.core.windows.net`.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| 401 `missing Authorization header` | Client didn't send a token. | Locally: run `make token`. Deployed: check VS Code's MCP auth flow. |
| 401 `wrong audience` | `aud` in token doesn't match `MCP_SERVER_APP_ID`. | Verify `MCP_SERVER_APP_ID` matches the server app's Application (client) ID in Entra. |
| 403 `missing required role` | Token is valid but user lacks the `Apim.Read` app role. | Enterprise apps → server app → Users and groups → assign the role. |
| Tools return `access_denied` | Outbound 403 from ARM. | Assign Reader or `APIM Knowledge Reader` role on the APIM resource to your account (local) or the UAMI (deployed). |
| `platform_broker_error` (VS Code, macOS) | macOS SSO Extension intercepts the OAuth flow. | Use `make token` instead of VS Code's built-in auth. |
| `AADSTS9010010` | `resource` parameter doesn't match the Application ID URI. | Verify `MCP_SERVER_AUDIENCE` matches the URI in Entra exactly (scheme, host, path, no trailing slash). |
| `AADSTS50011` redirect URI mismatch | MSAL used a port/host not in the client app's redirect URIs. | Add `http://localhost` (no port) to the client app's redirect URIs. |

## Tools exposed

As of T-10, Group A (discovery and health):

| Tool | What it does |
|---|---|
| `apim_list_services` | Lists configured APIM instances: alias, name, location, SKU, provisioning state. |
| `apim_get_service` | Full config of one instance: SKU, VNet, hostnames, certificate details. |
| `apim_get_service_health` | Health view: provisioning state, Resource Health, cert warnings, network status. |
