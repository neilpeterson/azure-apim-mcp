// Assigns the built-in "Log Analytics Reader" role to the server's managed
// identity on a single Log Analytics workspace (docs/SPEC.md §4.2). This
// built-in role already excludes workspaces/sharedKeys/read, so no custom
// role is needed for telemetry access.
//
// Deployed as a module scoped to the resource group that actually contains
// the target workspace, which may differ from the resource group the rest
// of this deployment lives in.
targetScope = 'resourceGroup'

@description('Name of the existing Log Analytics workspace to grant read access to.')
param workspaceName string

@description('Principal ID of the server user-assigned managed identity.')
param principalId string

// Built-in "Log Analytics Reader" role definition ID (same in every tenant).
var logAnalyticsReaderRoleId = '73c42c96-874c-492b-b04d-ab87d138a893'

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: workspaceName
}

// Deterministic name so re-running the deployment is idempotent instead of
// creating duplicate assignments.
resource roleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(workspace.id, principalId, logAnalyticsReaderRoleId)
  scope: workspace
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      logAnalyticsReaderRoleId
    )
  }
}
