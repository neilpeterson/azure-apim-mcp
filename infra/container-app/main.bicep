// Infrastructure for the APIM Knowledge MCP server (docs/SPEC.md §10.1).
//
// Provisions: Container App (minReplicas 1) + environment, user-assigned
// managed identity, Log Analytics workspace + Application Insights for the
// server's own telemetry, and built-in role assignments granting that identity
// access to the configured APIM instances and workspaces at the narrowest
// scope possible, never the resource group or subscription.
//
// Deliberately does NOT provision the container registry — that is
// `../container-registry/main.bicep`, deployed first and separately so an
// image can be built/pushed ("hydrated") into it before this template ever
// runs. This template only references that registry as an `existing`
// resource.
//
// This template provisions infrastructure and RBAC only. It does not create
// the Entra app registrations from §4.3 (app registration, app role, group
// assignment) — those are one-time tenant operations a human performs, and
// are out of scope for `az deployment group validate` / `azd up` here.
targetScope = 'resourceGroup'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Name of the existing Azure Container Registry provisioned by ../container-registry/main.bicep (its `registryName` output). Deploy that template first, hydrate it with an image, then pass that name here.')
param containerRegistryName string

@description('Name of the Log Analytics workspace for the server\'s own telemetry.')
param logAnalyticsWorkspaceName string

@description('Name of the Application Insights component for the server\'s own telemetry.')
param appInsightsName string

@description('Name of the user-assigned managed identity attached to the Container App.')
param uamiName string

@description('Name of the Container Apps managed environment.')
param containerAppEnvironmentName string

@description('Name of the Container App running the MCP server.')
param containerAppName string

@description('Container image to deploy, e.g. <containerRegistryName>.azurecr.io/apim-mcp:latest. Leave the default placeholder until a real image has been pushed to the registry above; the Container App can be updated in place afterwards.')
param containerImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Azure AD tenant ID. Maps to the AZURE_TENANT_ID app setting (docs/SPEC.md §5.3).')
param azureTenantId string = subscription().tenantId

@description('The server app registration\'s Application (client) ID, a GUID (docs/SPEC.md §4.3). Maps to MCP_SERVER_APP_ID.')
param mcpServerAppId string

@description('Entra app role required to call the server. Maps to MCP_REQUIRED_ROLE.')
param mcpRequiredRole string = 'Apim.Read'

@description('The APIM_SERVICES allowlist (docs/SPEC.md §5.3): the alias->resourceId mapping the server is permitted to query, and the resource group(s) RBAC will be scoped to. Each entry\'s resourceGroup is derived automatically from resourceId; logAnalyticsWorkspaceId is optional.')
param apimServices array

@description('CPU cores allocated to the container.')
param containerCpu string = '0.5'

@description('Memory allocated to the container.')
param containerMemory string = '1Gi'

// ---------------------------------------------------------------------------
// Observability for the server's own telemetry (App Insights connection
// string, §5.3) — distinct from the APIM instances' Log Analytics
// workspaces, which are read, never written to.
// ---------------------------------------------------------------------------

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsWorkspaceName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalyticsWorkspace.id
  }
}

// ---------------------------------------------------------------------------
// Identity
// ---------------------------------------------------------------------------

resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: uamiName
  location: location
}

// ---------------------------------------------------------------------------
// Container registry — provisioned separately by
// ../container-registry/main.bicep and referenced here as `existing` so it
// can be hydrated before this template and its Container App run.
// ---------------------------------------------------------------------------

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: containerRegistryName
}

// Built-in "AcrPull" role definition ID (same in every tenant).
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource acrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(containerRegistry.id, uami.id, acrPullRoleId)
  scope: containerRegistry
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

// ---------------------------------------------------------------------------
// Built-in API Management Service Reader Role (docs/SPEC.md §4.2), assigned
// only at each individual APIM resource. It covers the currently implemented
// configuration, Resource Health, and permission-canary calls while excluding
// user-key reads and APIM secret-retrieval actions.
// ---------------------------------------------------------------------------

var apimServiceReaderRoleId = '71522526-b88f-4d52-b57f-d31fc3546d0d'
var apimServiceReaderRoleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', apimServiceReaderRoleId)

module apimServiceReaderRoleAssignments 'bicep-modules/role-assignment-apim.bicep' = [
  for svc in apimServices: {
    name: 'rbac-apim-reader-${svc.alias}'
    scope: resourceGroup(split(svc.resourceId, '/')[2], split(svc.resourceId, '/')[4])
    params: {
      apimServiceName: last(split(svc.resourceId, '/'))
      principalId: uami.properties.principalId
      roleDefinitionId: apimServiceReaderRoleId
    }
  }
]

// Built-in Log Analytics Reader on each configured workspace, scoped to
// that workspace only (already excludes workspaces/sharedKeys/read).
module lawRoleAssignments 'bicep-modules/role-assignment-law.bicep' = [
  for svc in apimServices: if (contains(svc, 'logAnalyticsWorkspaceId') && svc.logAnalyticsWorkspaceId != null) {
    name: 'rbac-law-${svc.alias}'
    scope: resourceGroup(split(svc.logAnalyticsWorkspaceId, '/')[2], split(svc.logAnalyticsWorkspaceId, '/')[4])
    params: {
      workspaceName: last(split(svc.logAnalyticsWorkspaceId, '/'))
      principalId: uami.properties.principalId
    }
  }
]

// ---------------------------------------------------------------------------
// Container Apps (docs/SPEC.md §10.1)
// ---------------------------------------------------------------------------

resource containerAppEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: containerAppEnvironmentName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'azure-monitor'
    }
  }
}

// Route environment logs through Azure Monitor diagnostic settings instead
// of retrieving and embedding a Log Analytics workspace shared key.
resource containerAppEnvDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'container-app-environment-logs'
  scope: containerAppEnv
  properties: {
    workspaceId: logAnalyticsWorkspace.id
    logs: [
      {
        categoryGroup: 'allLogs'
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

// The environment's generated default domain is available during deployment,
// so the audience does not need to be supplied as a parameter.
var mcpServerAudience = 'https://${containerAppName}.${containerAppEnv.properties.defaultDomain}/mcp'

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  tags: {
    'azd-service-name': 'api'
  }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uami.id}': {}
    }
  }
  // Ensures the AcrPull grant exists before the platform attempts to pull
  // the image using it.
  dependsOn: [
    acrPullAssignment
  ]
  properties: {
    managedEnvironmentId: containerAppEnv.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'http'
        allowInsecure: false
      }
      // Identity-based pull from the ACR above — no admin user, no
      // username/password secret. Requires the AcrPull assignment on the
      // UAMI (acrPullAssignment) and the same identity attached above.
      registries: [
        {
          server: containerRegistry.properties.loginServer
          identity: uami.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'apim-mcp'
          image: containerImage
          resources: {
            cpu: json(containerCpu)
            memory: containerMemory
          }
          env: [
            { name: 'AZURE_TENANT_ID', value: azureTenantId }
            { name: 'AZURE_CLIENT_ID', value: uami.properties.clientId }
            { name: 'MCP_SERVER_AUDIENCE', value: mcpServerAudience }
            { name: 'MCP_SERVER_APP_ID', value: mcpServerAppId }
            { name: 'MCP_REQUIRED_ROLE', value: mcpRequiredRole }
            { name: 'APIM_SERVICES', value: string(apimServices) }
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: appInsights.properties.ConnectionString
            }
          ]
        }
      ]
      // minReplicas: 1, not 0 — scale-to-zero adds cold-start latency to an
      // interactive tool and will make Copilot feel broken (§10.1).
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
}

output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
output mcpServerAudience string = mcpServerAudience
output SERVICE_API_NAME string = containerApp.name
output uamiClientId string = uami.properties.clientId
output uamiPrincipalId string = uami.properties.principalId
output apimServiceReaderRoleDefinitionId string = apimServiceReaderRoleDefinitionId
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = containerRegistry.properties.loginServer
