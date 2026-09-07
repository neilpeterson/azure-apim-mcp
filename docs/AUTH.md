# Authentication and Access Control

This document explains how authentication and authorization work in the APIM MCP server — both how callers authenticate to the server, and how the server authenticates to Azure on the way out.

---

## Overview

There are two independent authorization decisions for every request:

1. **Can this caller talk to the MCP server?** — enforced per-user by Entra app-role assignment.
2. **What APIM data can the server read?** — enforced by Azure RBAC on the server's managed identity. Not per-user. Every authorized caller sees the same data.

This is a deliberate v1 tradeoff. The managed identity's RBAC scope is the security boundary for content. See [The v1 tradeoff](#the-v1-tradeoff) for what changes in a future On-Behalf-Of upgrade.

```
User (VS Code / Foundry agent)
        │
        │  Entra JWT (bearer token)
        ▼
  apim_mcp server  ──── validates token, checks Apim.Read role
        │
        │  User-assigned managed identity (UAMI)
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

Two app registrations are required.

**Server app (`apim-mcp-server`)**

- Set `"requestedAccessTokenVersion": 2` in the manifest under the `api` object.
- Set the **Application ID URI** to the server's canonical URL — e.g. `http://localhost:8000/mcp` locally and `https://<app>.<region>.azurecontainerapps.io/mcp` deployed. This must match the URL clients pass as the `resource` parameter exactly (scheme, host, path, no trailing slash). Add both URLs to `identifierUris` so one app registration works in both environments.
- Expose a delegated scope: `Mcp.Tools.Read` with consent type "Admins and users".
- Define an app role: `Apim.Read`, allowed member types `Users/Groups`.
- On the enterprise application, set **Assignment required = Yes**.

**Client app (`apim-mcp-client`)**

- On `apim-mcp-server` → Expose an API → Authorized client applications, add `apim-mcp-client`'s client ID with the `Mcp.Tools.Read` scope. This suppresses the per-user consent prompt for known clients.
- On `apim-mcp-client` → Authentication, add redirect URIs:
  - `http://localhost` (any-port wildcard for MSAL's loopback flow)
  - `http://127.0.0.1:33418`
  - `https://vscode.dev/redirect`

### Environment variables

| Variable | Value |
|---|---|
| `AZURE_TENANT_ID` | Your Entra tenant ID |
| `MCP_SERVER_AUDIENCE` | The Application ID URI URL (e.g. `https://<app>.<region>.azurecontainerapps.io/mcp`) |
| `MCP_SERVER_APP_ID` | The server app's Application (client) ID — a GUID |
| `MCP_REQUIRED_ROLE` | `Apim.Read` (default) |

### OAuth discovery endpoints

The server exposes RFC 9728 protected-resource metadata so MCP clients can discover the Entra tenant automatically:

- `GET /.well-known/oauth-protected-resource` — returns the resource URI, authorization server, supported scopes, and bearer method.
- `GET /.well-known/oauth-protected-resource/mcp` — same document at the path-suffixed route.
- `GET /.well-known/oauth-authorization-server` and `GET /.well-known/openid-configuration` — mirrors Entra's real OIDC discovery document verbatim (workaround for a VS Code client bug that drops the path component of the issuer URL).

All discovery routes are unauthenticated. Every `401` response includes a `WWW-Authenticate` header with a `resource_metadata` hint pointing at the discovery document.

---

## Outbound Authentication (MCP Server → Azure)

### How it works

The server authenticates to Azure using a **user-assigned managed identity (UAMI)**. All downstream calls to Azure Resource Manager, Azure Monitor, and Log Analytics go through the `credential_for(ctx, scope)` function in `src/apim_mcp/auth/credentials.py`. No module constructs credentials directly.

Set `AZURE_CLIENT_ID` to the UAMI's client ID. A Container App may have multiple identities attached; the explicit client ID prevents nondeterministic resolution.

### Custom RBAC role

The UAMI is assigned a custom role — **APIM Knowledge Reader** — at the narrowest applicable scope (individual APIM resource ID if possible, resource group if necessary, never subscription root).

```json
{
  "Name": "APIM Knowledge Reader",
  "IsCustom": true,
  "Description": "Read-only access to API Management configuration and telemetry. Cannot retrieve secrets.",
  "Actions": [
    "Microsoft.ApiManagement/service/read",
    "Microsoft.ApiManagement/service/*/read",
    "Microsoft.ResourceHealth/availabilityStatuses/read",
    "Microsoft.Insights/metrics/read",
    "Microsoft.Insights/metricDefinitions/read",
    "Microsoft.Resources/subscriptions/resourceGroups/read"
  ],
  "NotActions": [],
  "DataActions": [],
  "NotDataActions": [],
  "AssignableScopes": [
    "/subscriptions/{sub}/resourceGroups/{rg}"
  ]
}
```

**Why this role cannot retrieve secrets by construction:** every secret-retrieval action in the APIM resource provider (`namedValues/listValue/action`, `subscriptions/listSecrets/action`, `gateways/listKeys/action`, `tenant/listSecrets/action`, `users/token/action`) is a POST `*/action`, not a `*/read`. This role grants only `*/read`. No amount of code change can cause the server to return secrets — Azure RBAC is the enforcement layer, not application-level filtering.

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
2. Assign the UAMI the **APIM Knowledge Reader** role on the new APIM resource or its resource group.

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
