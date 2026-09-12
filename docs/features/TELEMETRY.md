# Telemetry tools

The telemetry feature provides aggregate APIM platform metrics and bounded,
read-only gateway-log analysis:

| Tool | Data source |
|---|---|
| `apim_get_metrics` | `AzureMetrics` |
| `apim_query_gateway_logs` | `ApiManagementGatewayLogs` |
| `apim_summarize_errors` | `ApiManagementGatewayLogs` |

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
    "logAnalyticsWorkspaceId": "/subscriptions/<subscription-id>/resourceGroups/<workspace-resource-group>/providers/Microsoft.OperationalInsights/workspaces/<workspace-name>"
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
  }
]
```

The alias is the `service` value passed to the tools. The workspace can be in a
different resource group or subscription as long as the deploying identity can
create the required read-only role assignment at that scope.

Omitting `logAnalyticsWorkspaceId` leaves the APIM configuration and search
tools available, but telemetry tools return a configuration error for that
service.

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

The gateway-log tools require the existing APIM diagnostic configuration to
send `GatewayLogs` to the resource-specific `ApiManagementGatewayLogs` table.
The diagnostic destination type must be `Dedicated`.

`Dedicated` selects Azure resource-specific tables, which is what creates
`ApiManagementGatewayLogs`; the legacy destination writes to
`AzureDiagnostics` and is not queried by these tools.

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

If `AzureMetrics` is empty, confirm the `AllMetrics` diagnostic setting and
allow for ingestion delay. If `ApiManagementGatewayLogs` is missing or empty,
confirm `GatewayLogs`, the resource-specific destination, workspace selection,
and recent gateway traffic.

## Data handling

Gateway-log queries use a fixed table and parameterized filters, enforce a
seven-day timespan and 200-row maximum, and scope every query to the selected
APIM resource. They never project request or response bodies or headers.
URLs are omitted by default; when requested, query strings are stripped.
Error text is redacted and labeled as untrusted content before being returned.
