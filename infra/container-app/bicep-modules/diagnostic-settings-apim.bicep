// Routes APIM metrics and gateway logs to the configured Log Analytics
// workspace so the runtime can use the narrow Log Analytics Reader role.
targetScope = 'resourceGroup'

@description('Name of the existing API Management service.')
param apimServiceName string

@description('Resource ID of the Log Analytics workspace receiving metrics.')
param workspaceResourceId string

resource apimService 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimServiceName
}

resource telemetryDiagnosticSetting 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'apim-mcp-telemetry'
  scope: apimService
  properties: {
    workspaceId: workspaceResourceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        category: 'GatewayLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}
