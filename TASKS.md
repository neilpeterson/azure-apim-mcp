# Tasks

Ordered backlog. **This file is shared state between sessions — tick boxes as you go.**

Rules:
- Work tasks in order. Skip only tasks marked `[PARALLEL-SAFE]`, which touch disjoint files and can be done any time after their dependency.
- A task is done when every box under **Done when** is ticked and `make check` passes.
- If a task is blocked, mark it `BLOCKED:` with the reason and move to the next unblocked task.
- `§N` references are to `docs/SPEC.md`.

Legend: `[H]` = human only, agents must not attempt. `[PARALLEL-SAFE]` = independent, safe to delegate concurrently.

---

## Milestone 0 — Harness

Nothing else starts until this is green. The harness is what lets every later task run unattended.

### T-01 — Repo skeleton and verification gate
**Depends on:** nothing
**Files:** `pyproject.toml`, `Makefile`, `.github/workflows/ci.yml`, `src/apim_mcp/__init__.py`, `tests/conftest.py`

Create the package layout from §12. Configure `ruff`, `mypy --strict`, and `pytest` with `asyncio_mode = "auto"`. Add a trivial passing test so the gate is meaningfully green.

**Done when:**
- [x] `make check` passes on an otherwise empty project
- [x] `make fmt` is idempotent
- [x] CI workflow runs `make check` on push
- [x] `mypy --strict src/` reports zero errors

---

### T-02 — Principle enforcement tests
**Depends on:** T-01
**Files:** `tests/test_principles.py`
**Spec:** `docs/PRINCIPLES.md`

Write the AST/grep-based tests named in `docs/PRINCIPLES.md`. They will pass trivially on an empty `src/` — that is fine and expected. They exist so that the first violation fails loudly rather than being discovered in review.

**Done when:**
- [x] `test_no_direct_credential_construction` implemented (AST scan for credential class instantiation outside `auth/credentials.py`)
- [x] `test_cache_keys_include_oid` implemented (cache decorators must have an `oid`-derived param; index module allowlisted)
- [x] `test_all_tools_are_readonly` implemented (stub against empty registry for now)
- [x] `test_no_secret_actions` implemented (grep `listSecrets`, `listValue`, `listKeys`, `users/token`)
- [x] `test_no_module_level_azure_clients` implemented
- [x] All five pass

---

## Milestone 1 — Foundations

### T-03 — Settings and configuration
**Depends on:** T-01 · **Spec:** §5.3
**Files:** `src/apim_mcp/settings.py`, `tests/test_settings.py`

Pydantic `Settings` covering every variable in the §5.3 table. `APIM_SERVICES` parses to a list of models with `alias`, `resource_id`, `log_analytics_workspace_id`. Fail fast with a message naming the missing variable.

**Done when:**
- [x] Missing required var raises at import with the variable name in the message
- [x] `APIM_SERVICES` JSON parses; malformed JSON gives a readable error
- [x] Resource IDs are validated against the ARM resource-ID shape
- [x] Alias lookup is case-insensitive and unknown alias raises a typed error listing valid aliases

---

### T-04 — Error taxonomy and response formatting
**Depends on:** T-01 · **Spec:** §8.1, §6.0
**Files:** `src/apim_mcp/common/errors.py`, `src/apim_mcp/common/formatting.py`, `tests/test_errors.py`, `tests/test_formatting.py`

Implement the seven error kinds with their message templates. Implement the list envelope, markdown and JSON rendering, and truncation at `MAX_RESPONSE_BYTES` with a `hint` naming the parameter to narrow.

The `access_denied` message must carry a `# OBO:` comment noting it changes meaning under on-behalf-of (`docs/PRINCIPLES.md` §8).

**Done when:**
- [x] Each of the seven `kind` values has a test asserting the message contains an actionable next step
- [x] `test_truncation_sets_hint` — oversized payload truncates, sets `truncated: true`, and names a real parameter
- [x] Markdown and JSON renderers produce equivalent data for the same input
- [x] `# OBO:` comment present on the `access_denied` template

---

### T-05 — The credential seam
**Depends on:** T-03 · **Spec:** §5.1, `docs/PRINCIPLES.md` §1, §2, §7
**Files:** `src/apim_mcp/auth/credentials.py`, `src/apim_mcp/auth/context.py`, `tests/test_credentials.py`

`CallContext` (carrying `oid`, `upn`, `roles`, `bearer_token`) and `credential_for(ctx, scope)`. v1 returns a `ManagedIdentityCredential` bound to `settings.uami_client_id`.

**`ctx` and `scope` are required parameters with no defaults, even though v1 uses neither.** Include the docstring from §5.1 explaining why, verbatim.

**Done when:**
- [x] `credential_for` signature is `(ctx: CallContext, scope: str)`, both positional, no defaults
- [x] `ARM_SCOPE` and `LOGS_SCOPE` constants defined
- [x] `test_credential_for_requires_scope` — calling without scope is a `TypeError`
- [x] `CallContext` is immutable (frozen model)
- [x] `tests/test_principles.py::test_no_direct_credential_construction` still passes
- [x] Docstring explains the OBO migration rationale

---

### T-06 — ArmClient
**Depends on:** T-04, T-05 · **Spec:** §5.2
**Files:** `src/apim_mcp/clients/arm.py`, `tests/test_arm_client.py`

`get()` and `list_all()` only. **No mutating methods** — this is the enforcement point for `docs/PRINCIPLES.md` §4. Default `api-version=2024-05-01`, per-call override. Follow `nextLink` up to `max_pages`. Retry 429/5xx with `tenacity`, honouring `Retry-After`. Map status codes to the T-04 taxonomy.

**Done when:**
- [x] No method issues POST, PUT, PATCH, or DELETE
- [x] `test_follows_next_link` — three-page fixture yields all items
- [x] `test_respects_max_pages` — sets `truncated`, does not loop forever
- [x] `test_honours_retry_after` — 429 with `Retry-After: 2` waits ~2s (use a fake clock)
- [x] `test_403_returns_access_denied` — 403 becomes a result, not an exception
- [x] Client constructed per call, not at module scope

---

### T-07 — Fixture recorder and replay harness
**Depends on:** T-06
**Files:** `scripts/record_fixtures.py`, `tests/conftest.py`, `tests/fixtures/README.md`

The recorder is run **by a human** against a real non-prod instance; it writes sanitised JSON to `tests/fixtures/`. The replay fixture is what every later test uses.

Sanitisation is mandatory: strip subscription GUIDs, tenant IDs, hostnames, and any `Authorization` header from recorded payloads before writing.

**Done when:**
- [x] `scripts/record_fixtures.py` records: service GET, apis list (paged), operations list, policy GET, export link response, resource health, network status
- [x] Recorded payloads are sanitised — `test_fixtures_contain_no_real_identifiers` asserts no GUID matching the real subscription pattern
- [x] `arm_client` pytest fixture replays from `tests/fixtures/` with zero network access
- [x] A test that attempts a real network call fails loudly (block sockets in `conftest.py`)
- [x] `tests/fixtures/README.md` documents how to re-record

> **[H] Human step:** run `make fixtures` against a non-prod APIM instance once T-07 lands. Agents cannot do this.

---

### T-08 — JWT middleware and health endpoints
**Depends on:** T-03, T-04 · **Spec:** §4.4
**Files:** `src/apim_mcp/auth/middleware.py`, `tests/test_jwt_middleware.py`

Validate in order: bearer present, signature against cached JWKS, `iss`, `aud` (exactly one configured value — **never a list** — a URL per §4.3/§5.3, not an `api://` string), `exp`/`nbf` with ≤60s skew, `roles` contains the required role. Stash claims for the audit log. Add `/healthz` and `/readyz` outside auth.

The middleware must make the **raw inbound token** retrievable inside tool handlers via `ContextVar`, even though v1 does not use it — Appendix A and B both need it.

**Done when:**
- [x] Table-driven test covers: no header, malformed, expired, wrong `iss`, wrong `aud`, missing `roles`, unknown `kid`, valid
- [x] `test_aud_is_not_a_list` — configuring multiple audiences is rejected at startup
- [x] JWKS cache refreshes on unknown `kid`, rate-limited to once per 60s
- [x] `test_raw_token_available_in_handler` — a handler can read the inbound token
- [x] `/healthz` and `/readyz` return 200 without a token; `/readyz` is **not** gated on index build

---

### T-08.1 — OAuth protected-resource discovery
**Depends on:** T-08 · **Spec:** §10.2, §4.3
**Files:** `src/apim_mcp/auth/middleware.py`, `src/apim_mcp/server.py`, `tests/test_jwt_middleware.py`, `tests/test_telemetry.py`

Implement RFC 9728 protected-resource metadata so VS Code (and any MCP client following the authorization spec) can discover the Entra tenant without a hand-configured header, per the exact JSON shape and endpoint-path rules in §10.2.

**Done when:**
- [x] `GET /.well-known/oauth-protected-resource` returns `resource` (== `MCP_SERVER_AUDIENCE` exactly), `authorization_servers` (v2.0 issuer), `scopes_supported`, `bearer_methods_supported`
- [x] Same document also served at the path-suffixed route (`/.well-known/oauth-protected-resource/mcp`)
- [x] Both discovery routes are unauthenticated; every other route (including `/authorize`, `/token`, `/register` if a client probes them) is not — no OAuth-proxy endpoints are implemented (§4.3)
- [x] Every `401` response's `WWW-Authenticate` header includes `resource_metadata="<url>"` pointing at the path-suffixed route
- [x] `test_oauth_protected_resource_*` covers the document shape and both paths; a 401 assertion covers the `resource_metadata` hint

---

### T-08.2 — Authorization-server metadata mirror (VS Code discovery-bug workaround)
**Depends on:** T-08.1 · **Spec:** §10.2.1
**Files:** `src/apim_mcp/auth/middleware.py`, `src/apim_mcp/server.py`, `tests/test_jwt_middleware.py`, `tests/test_telemetry.py`

Work around a known VS Code MCP client bug (drops the path component of an authorization-server issuer URL when building its own discovery request, so Entra's `.../<tenant>/v2.0/.well-known/...` never resolves and the client falls back to treating this server as its own authorization server). Mirror Entra's real, unmodified OIDC discovery document at this server's own well-known paths — never a fabricated document, never `/authorize`/`/token`/`/register`.

**Done when:**
- [x] `AuthorizationServerMetadataCache` fetches and caches `https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration` verbatim, with an injectable transport for tests
- [x] `GET /.well-known/oauth-authorization-server` and `GET /.well-known/openid-configuration` both return the cached document unmodified, unauthenticated
- [x] A fetch failure returns `503`, never an unhandled exception (§8)
- [x] No `/authorize`, `/token`, or `/register` handler is added — the mirrored document's own endpoint URLs still point at `login.microsoftonline.com`
- [x] Tests cover: both paths bypass auth, both return the mocked document verbatim (and never include a `registration_endpoint`), and a fetch failure yields `503`

---

### T-09 — Server bootstrap and audit logging
**Depends on:** T-05, T-08 · **Spec:** §9
**Files:** `src/apim_mcp/server.py`, `src/apim_mcp/common/telemetry.py`, `tests/test_telemetry.py`

FastMCP app, streamable HTTP stateless JSON at `/mcp`, middleware wired. Tool registration decorator that emits the §9 audit event and converts stray exceptions to `upstream_error`.

Startup permission canary (§4.2): resolve the UAMI's effective permissions per configured scope and warn if any `listSecrets`-family action appears.

**Done when:**
- [x] Server starts and responds to MCP `initialize`
- [x] `test_every_tool_emits_audit_event` iterates the registry
- [x] Audit event contains all §9 fields; response bodies absent
- [x] `test_stray_exception_becomes_upstream_error` and logs at ERROR
- [x] Permission canary runs at startup and logs its findings

---

## Milestone 2 — First end-to-end

### T-10 — Group A: discovery and health tools
**Depends on:** T-06, T-07, T-09 · **Spec:** §6 Group A
**Files:** `src/apim_mcp/tools/discovery.py`, `tests/test_tools_discovery.py`

`apim_list_services`, `apim_get_service`, `apim_get_service_health`.

`apim_get_service_health` fans out to five sources and each must be independently fault-tolerant — one failure yields `status: "unavailable"` for that section, not a failed tool.

**Done when:**
- [x] Three tools registered with correct annotations
- [x] `test_health_partial_failure` — one sub-call 500s, others still return
- [x] `test_cert_expiry_computed` — `daysUntilExpiry` correct against a fixture
- [x] `encodedCertificate` and certificate passwords never appear in output
- [x] All three return within 60s against fixtures

> **MILESTONE — stop and verify manually.** Deploy, point VS Code at it, confirm the OAuth flow works and the three tools are callable from Copilot. Do not proceed until this is real.

---

## Milestone 3 — Configuration and search

### T-11 — Redaction module `[PARALLEL-SAFE]`
**Depends on:** T-01 · **Spec:** §8.2, §8.3
**Files:** `src/apim_mcp/common/redaction.py`, `tests/test_redaction.py`

Header-name matching, high-entropy patterns (base64 ≥40, hex ≥32, JWT shape, SAS params), `[REDACTED:reason]` markers, untrusted-content wrapping, control-character stripping.

**Done when:**
- [x] One test per pattern in §8.2
- [x] `test_named_value_refs_survive` — `{{my-value}}` passes through untouched
- [x] `test_untrusted_content_is_wrapped`
- [x] `test_redaction_marker_is_visible` — output shows redaction occurred so the model can say so
- [x] Zero-width Unicode and control chars stripped

---

### T-12 — Tokenizer `[PARALLEL-SAFE]`
**Depends on:** T-01 · **Spec:** §7.3
**Files:** `src/apim_mcp/index/tokenize.py`, `tests/test_tokenize.py`

Split camelCase, PascalCase, snake_case, kebab-case, and URL path segments. This one function is most of what makes the search tool work.

**Done when:**
- [x] `getInventoryLevels` → `{get, inventory, levels}`
- [x] `/orders/{orderId}/line-items` → `{orders, order, id, line, items}`
- [x] `SKU_count` → `{sku, count}`
- [x] Acronyms handled: `parseXMLResponse` → `{parse, xml, response}`
- [x] Idempotent on already-tokenized input

---

### T-13 — Group B: API configuration tools
**Depends on:** T-10, T-11 · **Spec:** §6 Group B (excluding `apim_get_api_spec`)
**Files:** `src/apim_mcp/tools/config.py`, `tests/test_tools_config.py`

`apim_list_apis`, `apim_get_api`, `apim_get_policy`, `apim_list_products`, `apim_list_backends`, `apim_list_named_values`, `apim_list_subscriptions`.

**Done when:**
- [ ] `test_named_values_never_returns_values` — secret entries expose name and flag only
- [ ] `test_subscriptions_never_return_keys`
- [ ] `test_backends_omit_credentials`
- [ ] `test_list_apis_excludes_revisions_by_default`
- [ ] Tool descriptions state explicitly what is *not* returned
- [ ] Policy output passes through T-11 redaction

---

### T-14 — Spec export
**Depends on:** T-13 · **Spec:** §6 Group B `apim_get_api_spec`
**Files:** `src/apim_mcp/clients/apim.py`, `tests/test_spec_export.py`

Two-call flow. The export response returns a **link**, not the document. Fetch the blob with a plain `httpx` GET and **no `Authorization` header** — the SAS is the credential. The SAS is valid five minutes: fetch immediately, never cache the link, re-export on retry.

**Done when:**
- [ ] `test_no_auth_header_on_blob_fetch` — asserts the second request carries no bearer token
- [ ] `test_link_is_never_cached` — only the fetched document is cached
- [ ] `test_expired_sas_triggers_reexport`
- [ ] `mode="summary"` returns info, servers, security scheme names, and compact per-path listing
- [ ] `test_export_failure_is_graceful` — a SOAP/GraphQL API that cannot export returns a typed error, not a crash
- [ ] Cache key includes `oid`

---

### T-15 — Index builder
**Depends on:** T-12, T-14 · **Spec:** §7.1–7.3, §7.5
**Files:** `src/apim_mcp/index/builder.py`, `tests/test_index_builder.py`

Build `OperationIndexEntry` per operation. Spec extraction is **best-effort** — on export failure, index from the operations list and set `specIndexed: false`.

`asyncio.Semaphore(INDEX_MAX_CONCURRENCY)`. Honour `Retry-After`. Cap schema extraction at depth 3 / 200 properties. Hard 10-minute build cap with `partial: true` on timeout.

Every entry point carries a `# OBO:` comment per `docs/PRINCIPLES.md` §3.

**Done when:**
- [ ] `test_concurrency_is_bounded` — never exceeds the semaphore limit
- [ ] `test_throttling_is_not_failure` — 429s retried, build completes
- [ ] `test_export_failure_degrades` — `specIndexed: false`, entry still indexed
- [ ] `test_schema_depth_capped`
- [ ] `test_build_timeout_returns_partial`
- [ ] Field weighting from §7.3 implemented as specified
- [ ] `# OBO:` comments present

---

### T-16 — Search tools
**Depends on:** T-15 · **Spec:** §6 Group C, §7.4
**Files:** `src/apim_mcp/index/search.py`, `src/apim_mcp/tools/search.py`, `tests/test_search.py`

BM25 over the tokenized corpus. `apim_search_apis` and `apim_refresh_index`. **No embeddings** — see §7.4.

The tool description must instruct the model to supply synonyms itself, with the inventory example spelled out.

**Done when:**
- [ ] `test_inventory_question` — a fixture API with `getInventoryLevels` is found by query `"inventory"` and ranks first
- [ ] `test_synonym_terms_widen_results` — `terms=["stock"]` surfaces an operation named `getStockLevels`
- [ ] `test_low_confidence_flagged` — nonsense query returns hits with `lowConfidence: true`
- [ ] `matchedFields` and `snippet` populated on every hit
- [ ] `apim_refresh_index` rate-limited to once per service per 60s
- [ ] Stale index served during background rebuild; never blocks a request
- [ ] Tool description contains the synonym instruction

---

## Milestone 4 — Telemetry

### T-17 — Metrics
**Depends on:** T-10 · **Spec:** §6 Group D `apim_get_metrics`
**Files:** `src/apim_mcp/clients/metrics.py`, `src/apim_mcp/tools/telemetry.py`, `tests/test_metrics.py`

`Capacity`, `Requests`, `Duration`, `BackendDuration`, `ClientDuration`. Dimension filter passthrough. **Do not expose** the deprecated `TotalRequests`/`SuccessfulRequests`/`FailedRequests`.

Startup: call `list_metric_definitions` per service and log which metrics are actually available.

**Done when:**
- [ ] Deprecated metric names rejected with a message naming the replacement
- [ ] `test_dimension_filter_passthrough` — `GatewayResponseCodeCategory eq '5xx'` reaches the client
- [ ] Startup availability probe implemented and logged
- [ ] `timespan` accepts both ISO duration and `start/end`

---

### T-18 — KQL builder
**Depends on:** T-04 · **Spec:** §6 Group D, `docs/PRINCIPLES.md` §6
**Files:** `src/apim_mcp/clients/logs.py`, `tests/test_kql_builder.py`

Query construction only, no tool yet. Every user value goes through `declare query_parameters`. Fixed table name. Hard caps: `timespan` ≤ `P7D`, `limit` ≤ 200.

**Done when:**
- [ ] `test_no_interpolation` — AST/string check that no parameter value appears in the query body
- [ ] `test_injection_attempt_is_inert` — `api_id = "'; SigninLogs | take 100 //"` produces an unchanged query shape and binds the value as a parameter
- [ ] `test_timespan_capped` — `P30D` rejected with a message naming the limit
- [ ] `test_limit_capped` — >200 clamped, `truncated` set
- [ ] Only `ApiManagementGatewayLogs` is ever referenced

---

### T-19 — Log tools
**Depends on:** T-18 · **Spec:** §6 Group D
**Files:** `src/apim_mcp/tools/telemetry.py`, `tests/test_tools_logs.py`

`apim_query_gateway_logs` and `apim_summarize_errors`.

**Done when:**
- [ ] `Url` omitted unless `include_urls=True`; query string stripped even then
- [ ] `test_summarize_errors_groups_correctly` — grouped by ApiId × LastErrorReason × ResponseCode with a representative CorrelationId
- [ ] Both return within 60s against fixtures
- [ ] `Data.Read` scope requested via `credential_for(ctx, LOGS_SCOPE)` — not `ARM_SCOPE`

> **[H] Human step:** run `ApiManagementGatewayLogs | getschema` against a real workspace and pin the column list before this task. Recorded in `docs/SPEC.md` §14 Q4.

---

## Milestone 5 — Ship

### T-20 — Infrastructure `[PARALLEL-SAFE]`
**Depends on:** T-03 · **Spec:** §10.1
**Files:** `infra/main.bicep`, `infra/*.bicep`, `azure.yaml`

Container App (`minReplicas: 1`), UAMI, custom role definition and assignment, App Insights, log analytics. Egress must permit `management.azure.com`, `login.microsoftonline.com`, `api.loganalytics.io`, `*.blob.core.windows.net`.

**Done when:**
- [ ] `az deployment group validate` passes
- [ ] `minReplicas` is 1, not 0
- [ ] Custom role matches §4.2 exactly, including the empty `NotActions`
- [ ] Role assigned at narrowest configured scope, never subscription root
- [ ] `AZURE_CLIENT_ID` set to the UAMI client ID
- [ ] `azd up` completes from clean

---

### T-21 — Evaluations `[PARALLEL-SAFE]`
**Depends on:** T-16, T-19 · **Spec:** §11.3
**Files:** `evals/questions.xml`, `evals/README.md`

The ten seeded questions from §11.3, in the `<evaluation><qa_pair>` format, with verified answers against your non-prod instance.

**Done when:**
- [ ] Ten questions, each requiring ≥2 tool calls
- [ ] Answers verified by hand, not generated
- [ ] Question 9 (secret named values) verified to produce a correct refusal, not a fabrication
- [ ] `evals/README.md` documents how to run them

---

### T-22 — Foundry wiring
**Depends on:** T-10 · **Spec:** §10.3
**Files:** `docs/RUNBOOK.md`

Create the project connection with `--auth-type user-entra-token --audience https://<app>.<region>.azurecontainerapps.io/mcp` (the server's Application ID URI, §4.3 — same value as `--target`, not an `api://` string). Attach as an `mcp` tool with `require_approval: "never"`.

**Done when:**
- [ ] Connection created and an agent successfully calls a tool
- [ ] **Agent-type finding recorded in `docs/RUNBOOK.md`:** does the MCP server see the end user's `oid`, or the agent's identity? Test with the agent type your platform actually hosts.
- [ ] `allowed_tools` subset documented

---

### T-23 — Hardening pass
**Depends on:** all
**Files:** across

**Done when:**
- [ ] Every tool description reviewed against actual behaviour, including what is *not* returned
- [ ] All five principle tests pass
- [ ] `grep -r "# OBO:" src/` returns every site listed in `docs/SPEC.md` Appendix A
- [ ] No response can exceed `MAX_RESPONSE_BYTES`
- [ ] Audit events present for every tool
- [ ] `docs/RUNBOOK.md` covers: blob egress dependency, index rebuild, permission canary alerts, JWKS failures

---

## Human-only tasks

Agents must not attempt these. Do them early — both are blocking and both take about ten minutes.

- [x] **[H] H-01** — Confirm Entra ID P1/P2 availability. Group-to-app-role assignment requires it; individual user assignment does not. Record in `docs/RUNBOOK.md`. (§4.3)
- [x] **[H] H-02** — On a throwaway app registration, set "assignment required = Yes" and confirm whether it forces admin consent in your tenant. Record the result. (§4.3)
- [ ] **[H] H-03** — Run the fixture recorder against non-prod once T-07 lands.
- [ ] **[H] H-04** — `ApiManagementGatewayLogs | getschema`; pin columns before T-19.
- [ ] **[H] H-05** — `az monitor metrics list-definitions` per instance; confirm metric availability before T-17.

---

## Parallel work

Safe to run concurrently once their dependency is met, because they touch disjoint files and share no abstractions:

- **T-11** (redaction) and **T-12** (tokenizer) — after T-01
- **T-20** (infra) — after T-03
- **T-21** (evals) — after T-16 and T-19

Everything else is sequential. Most tasks touch the credential seam or the error taxonomy, and parallel agents will invent competing abstractions for both.
