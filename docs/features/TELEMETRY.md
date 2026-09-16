# Telemetry tools

The telemetry feature provides aggregate APIM platform metrics and bounded,
read-only gateway-log analysis:

The complete fixed-query inventory and maintenance workflow are documented in
[`QUERY_CATALOG.md`](../development/QUERY_CATALOG.md).

| Tool | Data source |
|---|---|
| `apim_get_metrics` | `AzureMetrics` |
| `apim_query_gateway_logs` | `ApiManagementGatewayLogs` and legacy APIM `GatewayLogs` records in `AzureDiagnostics` |
| `apim_summarize_errors` | `ApiManagementGatewayLogs` and legacy APIM `GatewayLogs` records in `AzureDiagnostics` |

The tools query Log Analytics with the server's managed identity. They do not
use Azure Monitor's direct metrics API because the available built-in
APIM-scoped monitoring roles also grant broader read access than this server's
secret-blind model permits.

## Service configuration

Each telemetry-enabled APIM entry in the `APIM_SERVICES` setting needs the
APIM resource ID and the resource ID of the Log Analytics workspace receiving
its telemetry:

```json
[
  {
    "alias": "prod",
    "resourceId": "/subscriptions/<subscription-id>/resourceGroups/<apim-resource-group>/providers/Microsoft.ApiManagement/service/<apim-name>",
    "logAnalyticsWorkspaceId": "/subscriptions/<subscription-id>/resourceGroups/<workspace-resource-group>/providers/Microsoft.OperationalInsights/workspaces/<workspace-name>",
    "gatewayLogTableMode": "azureDiagnostics"
  }
]
```

For Bicep deployments, provide the same values through `apimServices` in the
environment-specific parameter file:

```bicep
param apimServices = [
  {
    alias: 'prod'
    resourceId: '/subscriptions/<subscription-id>/resourceGroups/<apim-resource-group>/providers/Microsoft.ApiManagement/service/<apim-name>'
    logAnalyticsWorkspaceId: '/subscriptions/<subscription-id>/resourceGroups/<workspace-resource-group>/providers/Microsoft.OperationalInsights/workspaces/<workspace-name>'
    gatewayLogTableMode: 'azureDiagnostics'
  }
]
```

The alias is the `service` value passed to the tools. The workspace can be in a
different resource group or subscription as long as the deploying identity can
create the required read-only role assignment at that scope.

Omitting `logAnalyticsWorkspaceId` leaves the APIM configuration and search
tools available, but telemetry tools return a configuration error for that
service and `apim_get_service_health` reports its capacity section as
unavailable. The server does not auto-discover the workspace from the APIM
diagnostic setting; the explicit mapping keeps telemetry access within the
configured allowlist and drives the workspace-scoped read-only role assignment.

`gatewayLogTableMode` controls the gateway-log table queried for that service:

- `azureDiagnostics` queries legacy `GatewayLogs` records in
  `AzureDiagnostics`.
- `resourceSpecific` queries `ApiManagementGatewayLogs`.
- `auto` (the default) supports either destination and safely returns no rows
  when neither table has been created yet. Missing-table warnings from the
  fuzzy union are treated as empty only when no other query error is present.

Prefer an explicit mode when the diagnostic destination is known. It avoids
the additional table-resolution and scan work of automatic mode.

## Metrics configuration

Each existing APIM service must already have diagnostics configured to send
`AllMetrics` to its configured Log Analytics workspace. The repository's
deployment does not create or update that diagnostic setting; it only grants
the server identity the built-in **Log Analytics Reader** role at the
workspace scope. No custom role or APIM-scoped **Monitoring Reader**
assignment is required.

Metrics are exported asynchronously and can take several minutes to appear in
`AzureMetrics`. Diagnostic export also flattens APIM metric dimensions.
Therefore `apim_get_metrics` returns aggregate values only. Use the gateway-log
tools for API, operation, response-code, and error breakdowns.

## Gateway-log configuration

The gateway-log tools support both Log Analytics destination-table modes:

- **Resource specific** (`Dedicated`) writes to
  `ApiManagementGatewayLogs`.
- **Azure diagnostics** writes APIM `GatewayLogs` records to the shared
  `AzureDiagnostics` table.

In `auto` mode, the server queries both fixed tables and normalizes the legacy
APIM columns to the resource-specific output shape. It does not heuristically
deduplicate across tables because incomplete gateway fields cannot provide a
lossless request identity. If diagnostics route the same event to both tables,
both records can therefore appear. Explicit modes query only the configured
table. Resource-specific mode remains recommended for new diagnostic settings
because it has a stable service-specific schema and avoids the shared
`AzureDiagnostics` column limit.

An existing workspace generally starts receiving gateway logs within about
15 minutes. A new workspace can take up to two hours. The table is created
after APIM processes traffic and sends the first records.

## Permissions

The deployment grants the server identity:

- **API Management Service Reader Role** on each configured APIM service; and
- **Log Analytics Reader** on each configured workspace.

Keep both assignments resource-scoped. Do not substitute **Reader** or
**Monitoring Reader** at APIM scope: their broad `*/read` permission can
restore access to APIM user-key operations that this server deliberately
excludes.

For local development, the signed-in Azure CLI identity needs equivalent read
access to the APIM service and workspace.

## Validate the configuration

Repository tests validate the fixed table names, resource scoping, and pinned
column mappings without calling Azure. They cannot prove the live workspace
schema or diagnostic routing, and this repository does not include a
fabricated executable Kusto fixture. After deployment, an operator with
workspace access must perform the following live validation.

After generating traffic through the APIM gateway, run these queries in the
configured workspace:

```kusto
AzureMetrics
| where _ResourceId =~ "<apim-resource-id>"
| summarize count() by MetricName
```

```kusto
ApiManagementGatewayLogs
| where _ResourceId =~ "<apim-resource-id>"
| project TimeGenerated, ApiId, OperationId, ResponseCode, TotalTime
| take 10
```

For an **Azure diagnostics** destination, validate the legacy table instead:

```kusto
AzureDiagnostics
| where _ResourceId =~ "<apim-resource-id>"
| where Category == "GatewayLogs"
| project TimeGenerated, apiId_s, operationId_s, responseCode_d, DurationMs
| take 10
```

If `AzureMetrics` is empty, confirm the `AllMetrics` diagnostic setting and
allow for ingestion delay. If both gateway-log queries are missing or empty,
confirm `GatewayLogs`, the destination mode, workspace selection, and recent
gateway traffic. If a query fails because expected columns differ, run
`getschema` for the affected table and record the verified APIM gateway-log
mapping in `docs/operations/RUNBOOK.md` before changing the fixed query.

## Data handling

Gateway-log queries use fixed tables and parameterized filters, enforce a
seven-day timespan and 200-row maximum, and scope every query to the selected
APIM resource. They never project request or response bodies or headers.
URLs are omitted by default; when requested, query strings are stripped in
the final KQL projection before results leave Log Analytics, then stripped
again in Python as defense in depth.
Error text is redacted and labeled as untrusted content before being returned.
