# apim-mcp

Read-only MCP server that answers questions about Azure API Management: API inventory
and configuration, OpenAPI specs, semantic search across the API surface, service health,
metrics, and gateway logs. Works with any MCP-compatible client.

Azure already provides a broad MCP server, so the natural first question is whether a
dedicated APIM server is necessary. The two are complementary: Azure MCP Server provides
general Azure coverage, while `apim-mcp` provides a constrained, APIM-specific surface.

## Why not just use the Azure MCP Server?

[Azure MCP Server](https://learn.microsoft.com/en-us/azure/developer/azure-mcp-server/tools)
covers 60+ Azure service namespaces. **API Management is not one of them** (verified
against the tool catalogue, 2026-08-11). It covers part of the telemetry and health
surface, and none of the APIM control plane.

| Capability | Azure MCP Server | apim-mcp |
|---|---|---|
| Metrics | ✅ `monitor` | ✅ Curated, APIM-specific |
| Gateway logs | ⚠️ Generic KQL — you write the query | ✅ Parameterized, injection-safe |
| Service health | ⚠️ `resourcehealth` only | ✅ Plus `/networkstatus`, cert expiry |
| List APIM instances | ⚠️ Via `group` / `subscription` | ✅ |
| APIs, operations, products, backends | ❌ | ✅ |
| OpenAPI / Swagger export | ❌ | ✅ |
| Semantic search across API surface | ❌ | ✅ |
| Policy XML inspection | ❌ | ✅ |
| Error summarization | ❌ | ✅ |


## Available tools

All tools are read-only, take a `response_format: "markdown" | "json"` parameter, and
never return secrets (subscription keys, named-value secrets, certificates, backend
credentials) — see `docs/PRINCIPLES.md` §4/§5. This list reflects what's implemented
today; the full catalogue (Group D — metrics, gateway logs) is tracked in `TASKS.md`.

### Group A — Service discovery

| Tool | Returns |
|---|---|
| `apim_list_services` | Every APIM instance configured in `APIM_SERVICES`: alias, name, resource group, location, SKU, provisioning state, platform version, whether Log Analytics is wired up. |
| `apim_get_service` | Full configuration of one instance: SKU/capacity, provisioning state, platform version, VNet type, public IPs, additional locations, portal/gateway URLs, per-hostname certificate details (never the certificate itself). |
| `apim_get_service_health` | Consolidated health: provisioning state, Azure Resource Health, certificate-expiry warnings (<30 days), capacity, and dependency network status. Each section degrades independently rather than failing the whole call. |

### Group B — API configuration

| Tool | Returns |
|---|---|
| `apim_list_apis` | APIs on one instance (current revisions by default): id, name, path, protocols, revision info, subscription requirement. Supports `filter`, `include_revisions`, `limit`/`offset`. |
| `apim_get_api` | One API's full entity plus its operations (method, URL template, description). Truncates at 100 operations with a hint to use `apim_get_api_spec`. |
| `apim_get_policy` | Policy XML at `global`/`api`/`operation`/`product` scope, with sensitive header values and high-entropy secrets redacted (`docs/SPEC.md` §8.2). `{{named-value}}` references are preserved. |
| `apim_get_api_spec` | An API's OpenAPI/Swagger definition. `mode="summary"` (default) returns info, servers, security scheme names, and a compact path listing; `mode="full"` returns the parsed document (JSON or YAML), falling back to a truncated summary if it exceeds the response size ceiling. |
| `apim_list_products` | Products: id, name, description, subscription/approval requirements, state. |
| `apim_list_backends` | Backends: id, name, url, protocol, title, TLS settings. Never `credentials`. |
| `apim_list_named_values` | Named values: name, displayName, tags, `secret` flag. Returns `value` only when `secret` is `false`. |
| `apim_list_subscriptions` | Subscriptions: id, displayName, scope, state, owner. Never `primaryKey`/`secondaryKey`. |

### Group C — Search

| Tool | Returns |
|---|---|
| `apim_search_apis` | Lexical (BM25) search over an in-memory index of API/operation names, descriptions, URL paths, parameter names, and OpenAPI schema property names, across all configured instances. Not semantic — the tool description instructs the model to supply its own synonyms via `terms`. Ranked hits include `matchedFields` and a `snippet`; a weak match is still returned but flagged `lowConfidence: true`. See `docs/SEARCH_INDEX.md`. |
| `apim_refresh_index` | Forces an immediate rebuild of the search index for one or all services, rather than waiting out the TTL-based background refresh. Rate-limited to one call per service per 60 seconds. |


## Quick start — connect to a deployed server

**VS Code (GitHub Copilot)**

Add to `.vscode/mcp.json`:

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

Start the server via **MCP: List Servers**. VS Code discovers auth via `/.well-known/oauth-protected-resource`, prompts for the client app ID once, and refreshes tokens automatically.

> On corporate-managed Macs, VS Code's OAuth flow may fail with `platform_broker_error`. Ask the server operator to run `make token` and supply you with a static token, or to add your redirect URI to the client app registration.

---

## Quick start — run locally

**Prerequisites:** Python 3.12, `uv`, an `az login` session with Reader on your APIM resource, and the Entra app registrations described below.

```bash
# 1. Install dependencies
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

# 5. Get a token and wire VS Code
make token    # opens browser, writes token to .vscode/mcp.json
```

Reload VS Code after step 5. Token expires in ~1 hour — re-run `make token` when it does. Full walkthrough: `docs/DEPLOYMENT.md`.

---

## Configuration

### Environment variables

| Variable | Required | Notes |
|---|---|---|
| `AZURE_TENANT_ID` | yes | Entra tenant GUID. |
| `AZURE_CLIENT_ID` | yes | UAMI client ID. Use a placeholder GUID locally. |
| `MCP_SERVER_AUDIENCE` | yes | Application ID URI — a URL, not an `api://` string. |
| `MCP_SERVER_APP_ID` | yes | Server app registration's Application (client) ID (GUID). |
| `MCP_REQUIRED_ROLE` | no | Default `Apim.Read`. |
| `APIM_SERVICES` | yes | JSON array — see below. |
| `APIM_MCP_LOCAL_DEV_CREDENTIAL` | no | Set to `1` locally to use `az login`. Never set in deployed environments. |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | yes | Real connection string or an `InstrumentationKey=00000000-...` placeholder locally. |

### APIM_SERVICES

JSON array listing the APIM instances the server should expose:

```json
[
  {
    "alias": "prod",
    "resourceId": "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.ApiManagement/service/<name>"
  }
]
```

`alias` is the short name used in tool calls: `apim_get_service(service="prod")`. Add one entry per instance.

---

## Authentication setup

Two Entra app registrations, created once, shared between local and deployed. Full walkthrough with screenshots: `docs/DEPLOYMENT.md`.

**Server app (`apim-mcp-server`)** — validates inbound tokens:

1. App registrations → New → name it.
2. Expose an API → set Application ID URI to the server's URL (e.g. `https://<app>.<region>.azurecontainerapps.io/mcp`). Add both local and deployed URLs so one registration works everywhere.
3. Add scope `Mcp.Tools.Read` — **Who can consent**: "Admins and users".
4. Add app role `Apim.Read`, member type `Users/Groups`.
5. Enterprise applications → the app → Properties → **Assignment required = Yes**.
6. Users and groups → assign yourself `Apim.Read`.
7. Expose an API → Authorized client applications → add the client app ID with `Mcp.Tools.Read`.

**Client app (`apim-mcp-client`)** — what VS Code uses to acquire tokens:

1. App registrations → New → name it.
2. Authentication → Mobile and desktop → add redirect URIs: `http://localhost`, `http://127.0.0.1:33418`, `https://vscode.dev/redirect`.
3. API permissions → Add → APIs my organization uses → `apim-mcp-server` → Delegated → `Mcp.Tools.Read`.

**Granting access to others:** Enterprise applications → `apim-mcp-server` → Users and groups → assign `Apim.Read`.

---

## Deploying to Container Apps

Infrastructure automation (`infra/main.bicep`) is in progress. Manual steps until then:

1. Create a UAMI (e.g. `id-apim-mcp`).
2. Assign the `APIM Knowledge Reader` custom role at the narrowest scope (see `docs/SPEC.md` §4.2).
3. Attach the UAMI to the Container App; set `AZURE_CLIENT_ID` to its client ID.
4. Set env vars from the table above. Leave `APIM_MCP_LOCAL_DEV_CREDENTIAL` unset.
5. Container App: external ingress, port `8000`, `minReplicas: 1`.

Required egress: `management.azure.com`, `login.microsoftonline.com`, `api.loganalytics.io`, `*.blob.core.windows.net`.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| 401 `missing Authorization header` | Client didn't send a token. | Locally: run `make token`. Deployed: check VS Code's MCP auth flow. |
| 401 `wrong audience` | `aud` in token doesn't match `MCP_SERVER_APP_ID`. | Verify `MCP_SERVER_APP_ID` matches the server app's Application (client) ID in Entra. |
| 403 `missing required role` | User lacks the `Apim.Read` app role. | Enterprise apps → server app → Users and groups → assign the role. |
| Tools return `access_denied` | Outbound 403 from ARM. | Assign Reader or `APIM Knowledge Reader` on the APIM resource to your account (local) or the UAMI (deployed). |
| `platform_broker_error` (VS Code, macOS) | macOS SSO Extension intercepts OAuth. | Use `make token` instead of VS Code's built-in auth. |
| `AADSTS9010010` | `resource` doesn't match Application ID URI. | Verify `MCP_SERVER_AUDIENCE` matches the URI in Entra exactly — scheme, host, path, no trailing slash. |
| `AADSTS50011` | Redirect URI not registered. | Add `http://localhost` (no port) to the client app's redirect URIs. |

---

## Development quick start

No framework — five files plus a Makefile. The mechanism that makes unattended work possible is `make check` plus recorded fixtures: the agent can determine for itself whether it is finished, without you and without Azure.

```bash
uv sync
make check    # lint + types + tests — no Azure connection needed
make fmt      # auto-fix formatting and lint
make run      # local server on :8000
make inspect  # MCP Inspector against local server
```

Tests run against recorded fixtures in `tests/fixtures/` — no live Azure calls. To record or refresh fixtures against a real APIM instance, see `docs/LOCAL_TESTING.md`.

### Repository map

| File | Role |
|---|---|
| `AGENTS.md` | Agent routing table and principles summary. Loaded on every request. |
| `docs/PRINCIPLES.md` | Non-negotiables with rationale and enforcement. |
| `docs/SPEC.md` | Full technical requirement. |
| `TASKS.md` | Ordered backlog and shared state. |
| `docs/RUNBOOK.md` | Tenant quirks, environment findings, operational facts. |
| `Makefile` | `make check` is the gate. |

`.github/agents/*.agent.md` defines three subagents: `azure-auth`, `kql-safety`, and `spec-auditor`.
