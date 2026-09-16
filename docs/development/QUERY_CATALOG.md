# Log Analytics query catalog

Every Log Analytics request made by this server uses a registered,
fixed-shape query. The registry is
`src/apim_mcp/queries/catalog.py`; query implementations remain beside their
domain clients in `src/apim_mcp/clients/`.

The catalog gives each query a stable ID used in safe diagnostic logs. It also
records its purpose, source tables, parameters, output fields, owning tool,
implementation file, and server-enforced limits. It does not load KQL from
environment variables or accept free-form KQL from a tool caller.

## Queries

### `gateway-log-detail`

- **Tool:** `apim_query_gateway_logs`
- **Purpose:** Return recent APIM gateway events matching bounded filters.
- **Tables:** `ApiManagementGatewayLogs`, `AzureDiagnostics`, or both,
  according to the service's `gatewayLogTableMode`.
- **Parameters:** APIM resource ID, table mode, timespan, API ID, operation ID,
  response-code category, minimum duration, correlation ID, result limit, and
  whether sanitized URLs are requested.
- **Result:** Timestamp, API and operation IDs, HTTP method and response code,
  timing, success state, allowlisted error fields, correlation ID, region, and
  optionally a URL with its query string removed.
- **Limits:** Seven-day maximum timespan; 200 newest matching rows.
- **Implementation:** `src/apim_mcp/clients/logs.py`.

### `gateway-error-summary`

- **Tool:** `apim_summarize_errors`
- **Purpose:** Aggregate gateway failures by API, last-error reason, and
  response code.
- **Tables:** `ApiManagementGatewayLogs`, `AzureDiagnostics`, or both,
  according to the service's `gatewayLogTableMode`.
- **Parameters:** APIM resource ID, table mode, timespan, and number of groups
  to return.
- **Result:** API ID, error reason, response code, count, first and last
  occurrence, and a representative correlation ID.
- **Limits:** Seven-day maximum timespan; 100 groups.
- **Implementation:** `src/apim_mcp/clients/logs.py`.

### `metric-timeseries`

- **Tool:** `apim_get_metrics`
- **Purpose:** Aggregate an approved APIM metric into fixed time intervals.
- **Table:** `AzureMetrics`.
- **Parameters:** APIM resource ID, approved metric name, timespan, interval,
  and aggregation. Intervals use whole-second ISO 8601 durations.
- **Result:** Interval timestamp, aggregate value, sample count, and unit.
- **Implementation:** `src/apim_mcp/clients/metrics.py`.

### `metric-definitions`

- **Visibility:** Internal startup probe; it is not directly exposed as a
  tool.
- **Purpose:** List APIM metric names observed during the startup lookback.
- **Table:** `AzureMetrics`.
- **Parameters:** APIM resource ID.
- **Result:** Metric name.
- **Limits:** One-day lookback.
- **Implementation:** `src/apim_mcp/clients/metrics.py`.

## Adding a query

1. Add one `QueryDefinition` to `src/apim_mcp/queries/catalog.py`. Use a stable
   lowercase kebab-case ID and only approved APIM telemetry tables.
2. Implement a fixed query builder in the appropriate client module. Keep
   table names and operators in source code; encode every variable value in a
   `declare query_parameters` preamble.
3. Pass the registered definition to `run_workspace_query`. This makes the
   query ID visible in safe diagnostics and prevents uncataloged execution.
4. Add builder tests proving that malicious parameter values cannot alter the
   query body, all tables are resource-scoped, output fields are allowlisted,
   and limits are enforced.
5. Add the query to this document and update the relevant tool documentation
   and acceptance criteria.
6. Run `make check`.

## Updating a query

Keep the query ID stable when its purpose and result contract remain the same.
Update the catalog metadata, implementation, tests, and this document in the
same change. Create a new query ID when the purpose or output contract changes
substantially so diagnostics remain understandable across deployments.

## Removing a query

Remove or replace every tool and internal call site first, then remove its
builder, catalog entry, tests, and documentation. Because
`run_workspace_query` requires a registered `QueryDefinition`, remaining call
sites fail type checking instead of silently becoming uncataloged queries.

## Security boundary

The catalog is an inventory, not a free-form query feature. Do not add a tool
that accepts KQL, a table name, a query file path, or a catalog ID directly
from callers. Adding or removing query capabilities remains a reviewed source
change because the server's Log Analytics Reader role can read more workspace
data than the APIM-specific tools are permitted to expose.
