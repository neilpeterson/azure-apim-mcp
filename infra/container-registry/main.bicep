// Standalone Azure Container Registry deployment (../../docs/DEPLOYMENT.md).
//
// Deployed separately from `../container-app/main.bicep` on purpose: this lets you
// provision the registry, build/push an image into it ("hydrate"), and
// only then deploy the Container App template, which references this
// registry as an `existing` resource and needs a real image tag to run.
targetScope = 'resourceGroup'

@description('Azure region for the registry.')
param location string = resourceGroup().location

@description('Name of the Azure Container Registry. Must be 5-50 alphanumeric characters only — no hyphens. Pass the same value as the `containerRegistryName` param when deploying ../container-app/main.bicep.')
@minLength(5)
@maxLength(50)
param registryName string

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
}

output registryName string = containerRegistry.name
output loginServer string = containerRegistry.properties.loginServer
// Recognized by `azd` as the registry to build/push the `api` service's
// image to (docker.path in azure.yaml).
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = containerRegistry.properties.loginServer
