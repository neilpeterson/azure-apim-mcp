using '../main.bicep'

// Copy this file to an environment-specific name such as lab.bicepparam.
// Environment parameter files are ignored by Git.

param location = '<azure-region>'

// Must match registryName in the container-registry parameter file.
param containerRegistryName = '<globally-unique-acr-name>'

param logAnalyticsWorkspaceName = '<server-log-analytics-workspace-name>'
param appInsightsName = '<application-insights-name>'
param uamiName = '<managed-identity-name>'
param containerAppEnvironmentName = '<container-app-environment-name>'
param containerAppName = '<container-app-name>'
param containerImage = '<acr-name>.azurecr.io/apim-mcp:<immutable-tag>'

param azureTenantId = '<entra-tenant-id>'
param mcpServerAppId = '<server-app-client-id>'
param mcpRequiredRole = 'Apim.Read'

param apimServices = [
  {
    alias: '<friendly-service-alias>'
    resourceId: '/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.ApiManagement/service/<apim-name>'
    logAnalyticsWorkspaceId: '/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.OperationalInsights/workspaces/<workspace-name>'
  }
]
