# Runbook

Operational facts, environment findings, and things discovered the hard way.

**This file is append-only in spirit.** When you learn something about the tenant, the APIM instances, or the deployed service that isn't in `docs/SPEC.md`, it goes here with a date. The spec describes what to build; this describes the environment you're building it in.

---

## Entra ID

### Tenant

**MSIT.** Custom app management policies apply. Findings below were established 2026-09-05.

### `serviceManagementReference` is mandatory

Every app registration in this tenant requires it. Without it:

```
ERROR: ServiceManagementReference field is required for Create, but is missing
in the request. Refer to the TSG https://aka.ms/service-management-reference-error
```

**Value:** `a0656ef8-1e55-4dc7-8a13-a716b686a773`

Needed for:
- `apim-mcp-server` app registration (`docs/SPEC.md` §4.3)
- optional `apim-mcp-client` registration, only if the manual token helper is
  retained (§4.3)
- T-20 Bicep, if it creates any app registrations — needs a parameter for this

```powershell
az ad app create --display-name <name> --service-management-reference $SmRef
# on an existing app:
az ad app update --id <appId> --set serviceManagementReference=$SmRef
```

### User consent policy

```
ManagePermissionGrantsForSelf.microsoft-user-default-low
ManagePermissionGrantsForSelf.msit-low-permission-from-unverified
ManagePermissionGrantsForOwnedResource.microsoft-pre-approval-apps-for-chat
ManagePermissionGrantsForOwnedResource.microsoft-pre-approval-apps-for-team
```

Users may consent to **low-risk permissions only**. A custom delegated scope with `type: "User"` on your own app should qualify. Broad permissions like ARM `user_impersonation` almost certainly do not.

To re-check:

```powershell
az rest --method GET `
  --url 'https://graph.microsoft.com/v1.0/policies/authorizationPolicy' `
  --query 'defaultUserRolePermissions.permissionGrantPoliciesAssigned'
```

### H-01 — Entra ID P1/P2 availability

**Status:** _not yet run_

Determines whether the Entra **group** can be assigned to the `Apim.Read` app role (requires P1/P2) or whether **individual users** must be assigned (works on Free). Affects operations, not architecture.

```powershell
az rest --method GET `
  --url "https://graph.microsoft.com/v1.0/subscribedSkus?\$select=skuPartNumber,prepaidUnits,consumedUnits" `
  --query "value[].{sku:skuPartNumber, enabled:prepaidUnits.enabled, used:consumedUnits}" -o table
```

Look for `AAD_PREMIUM`, `AAD_PREMIUM_P2`, or a bundle (`SPE_E3`, `SPE_E5`, `SPB`, `EMS`, `EMSPREMIUM`). Then confirm it's assigned to the actual users, not just present in the tenant — a common failure is holding P1 on your own account only and still hitting *"Groups are not available for assignment due to your Active Directory plan level."*

**Result:** _pending_

### H-02 — "assignment required" vs admin consent

**Status: DEFERRED. Not blocking phase 1.**

Phase 1 enforces access through the `roles` claim check in the JWT middleware (T-08). The "Assignment required = Yes" toggle on the enterprise application is a second gate in front of that — defence in depth, not load-bearing.

Given the `-low` consent policy and a custom delegated scope with `type: "User"`, the toggle is **expected to work without admin consent**. Confirm when building the real server app registration. If it forces admin consent at that point, the claim check alone is sufficient for phase 1 — do not block on it.

**Because of this deferral, T-08's test coverage matters more than usual.** The table-driven test must include a token with no `roles` claim and assert a 403. That test is the access control.

---

## OBO — blocked, revisit

`docs/SPEC.md` Appendix A is the retrofit procedure. This section records why it's parked and how to check whether it's become viable.

OBO requires delegated permissions that the `-low` consent policy almost certainly excludes:

| API | App ID | Permission |
|---|---|---|
| Azure Service Management | `797f4846-ba00-4fd7-ba43-dac1f8f63013` | `user_impersonation` |
| Log Analytics | `ca7f3f0b-7d91-482c-8e09-c5d840d0eac5` | `Data.Read` |

**The check is empirical, not a query.** Reading `permissionGrantPolicies` requires a directory role we do not have:

```
Forbidden: Authorization_RequestDenied — Insufficient privileges to complete the operation.
```

So test it by attempting consent. Create a throwaway public-client app and open the authorize URL requesting the scope directly — no permission wiring needed, requesting it in the URL triggers consent evaluation on its own.

```powershell
$SmRef = "<service-management-reference>"
$AppId = az ad app create --display-name obo-consent-test `
  --public-client-redirect-uris 'http://localhost:8400' `
  --service-management-reference $SmRef --query appId -o tsv
$TenantId = az account show --query tenantId -o tsv

$Url = "https://login.microsoftonline.com/$TenantId/oauth2/v2.0/authorize" +
       "?client_id=$AppId&response_type=code" +
       "&redirect_uri=http://localhost:8400" +
       "&scope=https://management.azure.com/user_impersonation" +
       "&response_mode=query"
Start-Process msedge "-inprivate $Url"

# then clean up
az ad app delete --id $AppId
```

| Result | Meaning |
|---|---|
| Consent prompt, then `code=...` | User-consentable. OBO unblocked for ARM. |
| `AADSTS90094` | Admin approval required. Stay on phase 1. |
| `AADSTS65001` | Also admin-gated in practice. |

**Repeat with `scope=https://api.loganalytics.io/Data.Read`. OBO needs both.**

**Status as of 2026-09-05:** not yet run. Expected result is `AADSTS90094`.

### If OBO becomes viable

Follow `docs/SPEC.md` Appendix A. The §5.1 credential seam means this is a change to `credential_for()` plus the Appendix A checklist, not a rewrite.

Find the migration surface with:

```bash
grep -rn "# OBO:" src/
```

Every site whose behaviour or meaning changes under OBO carries that marker. T-23 asserts they match Appendix A.

---

## APIM instances

### Configured services

| Alias | Resource ID | Log Analytics workspace |
|---|---|---|
| _tbd_ | | |

### Gateway log schema (H-04)

**Status:** _not yet run._ Blocking for T-19.

Column names in `ApiManagementGatewayLogs` have changed across APIM versions. Pin the real list before building log tools:

```kql
ApiManagementGatewayLogs | getschema
```

**Pinned columns:** _pending_

### Metric availability (H-05)

**Status:** _not yet run._ Blocking for T-17.

Availability varies by SKU and platform version:

```powershell
az monitor metrics list-definitions --resource <apim-resource-id> `
  --query "[].{name:name.value, unit:unit}" -o table
```

**Available metrics:** _pending_

### `format=rawxml` returns bare XML, not a JSON-wrapped `PolicyContract`

**Confirmed 2026-09-06** against `apim-api-gateway-msft-lab` (rg `rg-sc-api-gateway-msft-lab`).

The ARM REST reference for `Policy - Get` / `ApiPolicy - Get` documents `.../policies/policy?format=rawxml&api-version=2024-05-01` as returning `200 OK` with a JSON `PolicyContract` body (`properties.value` holding the XML, `properties.format: "rawxml"`). In practice this instance returns the policy as a **bare XML document** with no JSON envelope at all — `response.json()` fails immediately with `JSONDecodeError: Expecting value: line 1 column 1 (char 0)` because the body starts with `<`.

`apim_get_policy` (T-13, `src/apim_mcp/tools/config.py`) now uses `ArmClient.get_text()` and a shape-tolerant `_extract_policy_xml()` helper that accepts either the documented JSON wrapper or bare XML. If a future `apim_*` tool needs another endpoint whose body might not reliably be JSON, use `get_text()` there too rather than assuming the REST reference's sample response is what you'll actually get back.

### ARM transport-level failures were not retried

**Confirmed 2026-09-06.** `ArmClient`'s retry policy (`docs/SPEC.md` §5.2: "Retry on 429 and 5xx") only matched HTTP status codes. A connection-level blip (`httpx.ConnectError`, `ReadTimeout`, etc. — no HTTP response at all) skipped every retry attempt and surfaced as a bare `upstream_error` after a single failed attempt, which looked identical to a real bug: the tool failed immediately while a fresh manual request (e.g. `az rest`) made moments later succeeded, because it simply landed after the blip passed.

Fixed by also retrying `httpx.TransportError` with the same exponential backoff, up to the existing attempt cap (`src/apim_mcp/clients/arm.py`).

---

## Deployment

The Container App needs outbound HTTPS to:

- `management.azure.com` — ARM control plane
- `login.microsoftonline.com` — token acquisition, JWKS
- `api.loganalytics.io` — log queries
- `*.blob.core.windows.net` — **OpenAPI spec export**
- App Insights ingestion endpoint

**The blob dependency is the non-obvious one.** The APIM export API returns a link to a blob with a five-minute SAS, not the document itself. If the app is ever network-isolated, every spec request fails while everything else keeps working. Symptom: `apim_get_api_spec` times out or 403s while `apim_list_apis` is fine.

### Permission canary

The server logs the UAMI's effective permissions at startup and warns if any `listSecrets`-family action appears (`docs/SPEC.md` §4.2). **Treat that warning as an incident** — it means someone widened the role, and the platform-enforced redaction that the design depends on is no longer in place.

### JWKS failures

If token validation starts failing tenant-wide, check `login.microsoftonline.com` reachability first. The JWKS cache refreshes on unknown `kid`, rate-limited to once per 60s, so a key rollover during an egress outage produces a delayed rather than immediate failure.

---

## Foundry

### Agent type finding (T-22)

**Status:** _not yet run._

The `user-entra-token` passthrough is well-supported for **prompt agents**. For **hosted agents** (code-based, running in a Foundry container) the MCP server has historically seen only the agent's managed identity, never the end user's.

Under phase 1 this does not affect data scoping — every caller sees the same thing regardless. It **does** affect audit attribution in `docs/SPEC.md` §9, and it is a hard blocker for OBO.

Test with the agent type the internal SPI platform actually hosts, and record which `oid` the MCP server receives.

**Result:** _pending_

---

## Change log

| Date | Finding |
|---|---|
| 2026-09-05 | Tenant identified as MSIT; `serviceManagementReference` mandatory on all app registrations |
| 2026-09-05 | User consent policy is `-low`; OBO delegated permissions likely require admin consent |
| 2026-09-05 | H-02 deferred as non-blocking; phase 1 relies on the `roles` claim check |
| 2026-09-06 | `apim-api-gateway-msft-lab`'s `.../policies/policy?format=rawxml` returns bare XML, not the documented JSON-wrapped `PolicyContract`; `apim_get_policy` now handles both shapes |
| 2026-09-06 | `ArmClient` did not retry `httpx.TransportError` (connection/timeout blips), only HTTP 429/5xx; now retries both |
