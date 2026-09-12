# Authentication and Access Control

This document explains how authentication and authorization work in the APIM MCP server — both how callers authenticate to the server, and how the server authenticates to Azure on the way out.

---

## Overview

There are two independent authorization decisions for every request:

1. **Can this caller talk to the MCP server?** — enforced per-user by Entra app-role assignment.
2. **What APIM data can the server read?** — enforced by Azure RBAC on the server's managed identity. Not per-user. Every authorized caller sees the same data.

This is a deliberate v1 tradeoff. The managed identity's RBAC scope is the security boundary for content. See [The v1 tradeoff](#the-v1-tradeoff) for what changes in a future On-Behalf-Of upgrade.

**Deployed (Container Apps):**
```
User (VS Code / Foundry agent)
        │
        │  Entra JWT (bearer token)
        ▼
  apim_mcp server (Container App)  ──── validates token, checks Apim.Read role
        │
        │  User-assigned managed identity (UAMI) attached to Container App
        ▼
  Azure Resource Manager / Azure Monitor / Log Analytics
```

**Local development:**
```
User (VS Code / Foundry agent)
        │
        │  Entra JWT (bearer token)
        ▼
  apim_mcp server (local)
        │
        │  AzureCliCredential  (your az login session)
        ▼
  Azure Resource Manager / Azure Monitor / Log Analytics
```

---

## Inbound Authentication (Caller → MCP Server)

### How it works

Every request to the MCP server must carry a valid Entra bearer token. The server validates the token as ASGI middleware before any tool handler runs. Validation order:

1. `Authorization: Bearer <jwt>` header present — else `401`
2. Signature valid against Entra's JWKS (`https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys`)
3. `iss` == `https://login.microsoftonline.com/{TENANT_ID}/v2.0`
4. `aud` == the server app's Application (client) ID (`MCP_SERVER_APP_ID` — a GUID, not the Application ID URI URL)
5. `exp` / `nbf` within 60 seconds of server clock
6. `roles` claim contains `Apim.Read` — else `403`

On success, the validated claims (`oid`, `preferred_username`, `roles`) are attached to request state for audit logging.

### Entra app registration setup

Only the server app registration is required for normal VS Code use. VS Code
uses Microsoft's existing public client and acquires tokens automatically for
both local and deployed MCP endpoints. The repository's separate public-client
registration is optional and exists only for manual token testing.

This section defines the complete authentication configuration and invariants.
For the ordered portal and deployment procedure, see
[`DEPLOYMENT.md`](DEPLOYMENT.md).

**Server app (`apim-mcp-server`)**

- Set `"requestedAccessTokenVersion": 2` in the manifest under the `api` object.
- Set the **Application ID URI** to the server's canonical URL — e.g. `http://localhost:8000/mcp` locally and `https://<app>.<region>.azurecontainerapps.io/mcp` deployed. This must match the URL clients pass as the `resource` parameter exactly (scheme, host, path, no trailing slash). Add both URLs to `identifierUris` so one app registration works in both environments.
- Expose a delegated scope: `Mcp.Tools.Read` with consent type "Admins and users".
- Define an app role: `Apim.Read`, allowed member types `Users/Groups`.
- On the enterprise application, set **Assignment required = Yes**.

**OAuth clients**

- On `apim-mcp-server` → Expose an API → Authorized client applications,
  preauthorize the official Visual Studio Code client ID
  `aebc6443-996d-45c2-90f0-388ff96faa56` for `Mcp.Tools.Read`.
- This preauthorization lets VS Code request the delegated scope without a
  per-user consent prompt. Users still require an `Apim.Read` app-role
  assignment on the server enterprise application.
- If the optional project MSAL client used by `make token` is retained, also
  preauthorize its client ID `4c5ab830-b291-4308-8ae2-70d3f978e4a0` for
  `Mcp.Tools.Read`.
- On that optional `apim-mcp-client` registration, add the delegated
  `Mcp.Tools.Read` API permission and these redirect URIs:
  - `http://localhost` (any-port wildcard for MSAL's loopback flow)
  - `http://127.0.0.1:33418`
  - `https://vscode.dev/redirect`

`make token` is not part of the VS Code connection flow. It remains available
for manual protocol testing or diagnosing a client independently of VS Code.

### Environment variables

| Variable | Deployed | Local dev | Notes |
|---|---|---|---|
| `AZURE_TENANT_ID` | yes | yes | Your Entra tenant ID |
| `MCP_SERVER_AUDIENCE` | yes | yes | Application ID URI URL (e.g. `https://<app>.<region>.azurecontainerapps.io/mcp`; use `http://localhost:8000/mcp` locally) |
| `MCP_SERVER_APP_ID` | yes | yes | Server app's Application (client) ID — a GUID |
| `MCP_REQUIRED_ROLE` | yes | yes | `Apim.Read` (default) |
| `AZURE_CLIENT_ID` | yes | placeholder | UAMI client ID in deployed environments; set to all-zeros locally |
| `APIM_MCP_LOCAL_DEV_CREDENTIAL` | **never** | `1` | Swaps managed identity for `az login` credential. Must not be set in Container Apps. |

### OAuth discovery endpoints

The server exposes RFC 9728 protected-resource metadata so MCP clients can discover the Entra tenant automatically:

- `GET /.well-known/oauth-protected-resource` — returns the resource URI,
  authorization server, supported scopes, and bearer method.
- `GET /.well-known/oauth-protected-resource/mcp` — same document at the path-suffixed route.
- `GET /.well-known/oauth-authorization-server` and `GET /.well-known/openid-configuration` — mirrors Entra's real OIDC discovery document verbatim for compatibility with VS Code clients that drop the path component of the issuer URL.

`scopes_supported` advertises the fully-qualified delegated scope derived from
`MCP_SERVER_AUDIENCE`, for example
`https://host.example/mcp/Mcp.Tools.Read` or
`http://localhost:8000/mcp/Mcp.Tools.Read`. It must never advertise only
`Mcp.Tools.Read`; Entra interprets that short scope as belonging to Microsoft
Graph rather than this MCP resource.

All discovery routes are unauthenticated. Every `401` response includes a `WWW-Authenticate` header with a `resource_metadata` hint pointing at the discovery document.

With this discovery configuration and the official VS Code client
preauthorized, both local and deployed servers use the same automatic sign-in
flow. No static bearer header or `make token` step is required.

---

## Outbound Authentication (MCP Server → Azure)

### How it works

All downstream calls to Azure Resource Manager, Azure Monitor, and Log Analytics go through the `credential_for(ctx, scope)` function in `src/apim_mcp/auth/credentials.py`. No other module constructs credentials directly — this single function is what both the deployed and local development paths go through, and it is the only point that changes in a future OBO upgrade.

The credential used depends on the environment:

| Environment | Credential | Set by |
|---|---|---|
| Container Apps (deployed) | `ManagedIdentityCredential` bound to the UAMI | `AZURE_CLIENT_ID` env var |
| Local development | `AzureCliCredential` (`az login` session) | `APIM_MCP_LOCAL_DEV_CREDENTIAL=1` in `.env` |

`APIM_MCP_LOCAL_DEV_CREDENTIAL=1` is an opt-in dev flag. It is never set in Container Apps environments. `ManagedIdentityCredential` cannot acquire tokens outside of Azure-hosted compute, so this flag exists solely to allow `make run` to hit real APIM from a developer laptop.

### Deployed: user-assigned managed identity (UAMI)

The UAMI is attached directly to the Container App that hosts the MCP server. When the server process calls `credential_for()`, `ManagedIdentityCredential` talks to the Container Apps metadata endpoint to obtain a token for that identity — no secrets or credentials are stored anywhere.

The deployment must create a UAMI, assign the built-in **API Management
Service Reader Role** on each allowed APIM resource, attach it to the
Container App, and set `AZURE_CLIENT_ID` to its client ID. The Bicep templates
and ordered commands are documented in [`DEPLOYMENT.md`](DEPLOYMENT.md).

A Container App may have multiple user-assigned identities attached. Without an explicit `AZURE_CLIENT_ID`, `ManagedIdentityCredential` picks nondeterministically. Always set it.

```
Container App (apim-mcp)
  └── attached identity: id-apim-mcp (UAMI)
        └── RBAC: API Management Service Reader Role → APIM resource
```

### Local development: az login credential

When `APIM_MCP_LOCAL_DEV_CREDENTIAL=1` is set, `credential_for()` returns `AzureCliCredential()`, which uses the token from your active `az login` session. Your personal account needs API Management Service Reader access on the APIM resource for the currently implemented tools.

**What you need in `.env` for local development:**

```
APIM_MCP_LOCAL_DEV_CREDENTIAL=1
AZURE_CLIENT_ID=00000000-0000-0000-0000-000000000000   # placeholder, not used in local mode
```

The `AZURE_CLIENT_ID` placeholder is still required by the settings validator but is ignored when the dev flag is set.

> **Important:** do not set `APIM_MCP_LOCAL_DEV_CREDENTIAL` in any deployed environment. It bypasses the managed identity entirely and would cause the server to try to use a developer's cached `az` token, which will not be present on Container Apps and will fail.

### Built-in RBAC role

The UAMI receives this built-in role on each individual APIM resource:

| Role | ID | Purpose |
|---|---|---|
| **API Management Service Reader Role** | `71522526-b88f-4d52-b57f-d31fc3546d0d` | APIM configuration reads, Resource Health, and permission-canary access |

**Why this role cannot retrieve APIM secrets by construction:** it explicitly excludes
`Microsoft.ApiManagement/service/users/keys/read`, and it does not grant the
APIM `*/action` operations used by `namedValues/listValue`,
`subscriptions/listSecrets`, `gateways/listKeys`, `tenant/listSecrets`, or
`users/token`. Do not add **Reader** or **Monitoring Reader** at the APIM
scope; their `*/read` permission would grant user-key reads despite the APIM
role's `NotActions`, because `NotActions` is not a deny rule.

Metrics are not yet implemented. Before adding them, the infrastructure must
select a separate built-in role that grants the required Azure Monitor metric
actions without restoring APIM user-key access.

### Log Analytics

Assign the built-in **Log Analytics Reader** role on the Log Analytics workspace. This role already excludes `workspaces/sharedKeys/read`.

### Permission canary

At startup, the server calls `Microsoft.Authorization/permissions` for each configured scope and logs the resolved effective permissions. A warning is emitted if any `listSecrets`-family action appears. Treat this warning as a misconfiguration incident — it means someone has assigned a broader role (e.g. Contributor) to the UAMI, probably to "fix a permissions issue."

### Credential scopes

Every `credential_for` call explicitly names its audience:

| Constant | Value | Used for |
|---|---|---|
| `ARM_SCOPE` | `https://management.azure.com/.default` | APIM control plane, Resource Health, Metrics |
| `LOGS_SCOPE` | `https://api.loganalytics.io/.default` | Log Analytics gateway logs |

---

## Managing Access

### Granting a user access to the MCP server

Add the user to the Entra security group that is assigned the `Apim.Read` app role on the `apim-mcp-server` enterprise application.

> **Note:** assigning a group to an app role requires **Entra ID P1 or P2**. Individual user assignment works on the free tier. If P1/P2 is unavailable, assign users individually via the enterprise application's Users and groups blade.

### Revoking a user's access

Remove the user from the Entra security group (or remove their individual assignment). Their existing tokens remain valid until expiry (typically 1 hour); there is no immediate revocation.

### Adding a new APIM instance

Two steps are required:

1. Add the instance to the `APIM_SERVICES` environment variable (JSON array entry with `alias`, `resourceId`, and optionally `logAnalyticsWorkspaceId`).
2. Assign the UAMI the **API Management Service Reader Role** on the new APIM
   resource itself and, when configured, **Log Analytics Reader** on its
   workspace.

`APIM_SERVICES` is the authoritative allowlist. The server will not query an APIM instance that is not listed there, regardless of what the UAMI's RBAC would permit.

### Removing an APIM instance

Remove its entry from `APIM_SERVICES`. You may also remove the RBAC assignment to clean up, but the allowlist check alone is sufficient to prevent access.

---

## What All Authorized Callers Can See

In v1, every user who passes the `Apim.Read` role check sees the same data — whatever the UAMI's RBAC permits. There is no per-user data scoping. This means:

- If the UAMI has reader access to `apim-prod`, all authorized users can query `apim-prod`.
- The server never returns subscription keys, named-value secret contents, gateway keys, or certificates.
- Policies, API configurations, metrics, and gateway logs are readable (subject to what the UAMI can reach).
- The audit log records `caller_oid` and `caller_upn` per tool call — this is the only record of which user asked what, since the Azure activity log records the managed identity's actions, not the human's identity.

---

## The v1 Tradeoff

v1 uses a shared managed identity. This means authorization decision #2 (what data is visible) is not per-user. The entire codebase is structured to make upgrading to **On-Behalf-Of (OBO)** a small, contained change:

- Every downstream call goes through `credential_for(ctx, scope)` — a single function to change.
- `scope` is always passed explicitly, even though managed identity ignores it.
- Every cache key includes the caller's `oid`, even though under managed identity all callers get the same data.
- The raw inbound bearer token is available inside every tool handler via a `ContextVar`.

Under OBO, each tool call would exchange the user's inbound token for a downstream token scoped to ARM or Log Analytics, and Azure RBAC would enforce per-user visibility across APIM instances. A user without ARM reader access to `apim-prod` would get a 403 from ARM, which the server returns as a structured `access_denied` result rather than an exception.

The `access_denied` error message wording is version-specific and carries a `# OBO:` comment in the source. Under v1, a 403 means the server's identity is misconfigured. Under OBO, it is a routine user-permission result. These require different user-facing messages.
