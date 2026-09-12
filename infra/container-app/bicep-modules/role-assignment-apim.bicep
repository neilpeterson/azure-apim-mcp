// Assigns a supplied built-in role to the server's managed identity at the
// narrowest possible scope: the individual APIM resource, never the resource
// group and never the subscription (docs/SPEC.md §4.2).
//
// Deployed as a module scoped to the resource group that actually contains
// the target APIM instance, which may differ from the resource group the
// rest of this deployment lives in.
targetScope = 'resourceGroup'

@description('Name of the existing API Management service to grant access to.')
param apimServiceName string

@description('Principal ID of the server user-assigned managed identity.')
param principalId string

@description('GUID of the built-in role definition to assign.')
param roleDefinitionId string

resource apimService 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimServiceName
}

// Resolve the built-in role in this module's target subscription so APIM
// instances in subscriptions other than the Container App's are supported.
var roleDefinitionResourceId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  roleDefinitionId
)

// Deterministic name so re-running the deployment is idempotent instead of
// creating duplicate assignments.
resource roleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(apimService.id, principalId, roleDefinitionResourceId)
  scope: apimService
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: roleDefinitionResourceId
  }
}
