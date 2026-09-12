# Deployment guide

End-to-end instructions for deploying `apim-mcp` to Azure Container Apps
and connecting an MCP client. This is the authoritative operator guide for
both application and infrastructure deployment.

For the authentication model and security rationale, see
[`AUTH.md`](AUTH.md). For the normative requirements, see `docs/SPEC.md`
§4 and §10.

## What gets deployed

Deployment is split into two Bicep templates:

1. `infra/container-registry/main.bicep` creates an Azure Container Registry.
2. `infra/container-app/main.bicep` creates the Container App, Container Apps
   environment, user-assigned managed identity (UAMI), Application Insights,
   Log Analytics workspace, and built-in role assignments.

The split is intentional. The registry must exist and contain the application
image before the Container App tries to start.

The templates do **not** create Entra app registrations. Those are tenant-level,
interactive operations and must be completed separately.

## Quick start

Use this checklist if the Entra registrations and Azure target values are
already understood. The detailed sections below explain each step.

1. Sign in and select the target subscription:

   ```bash
   az login
   az account set --subscription "<subscription-id-or-name>"
   ```

2. Create the `apim-mcp-server` Entra app registration, preauthorize the
   official Visual Studio Code client, and record the server application's
   client ID.

3. Copy both sanitized examples to environment-specific parameter files:

   ```bash
   cp infra/container-registry/bicep-params/example.bicepparam \
     infra/container-registry/bicep-params/my-environment.bicepparam
   cp infra/container-app/bicep-params/example.bicepparam \
     infra/container-app/bicep-params/my-environment.bicepparam
   ```

   Fill in every placeholder in the copied files. Environment-specific
   parameter files are ignored by Git; only the sanitized examples are
   committed.

4. Create the resource group:

   ```bash
   az group create --name "<resource-group>" --location "<region>"
   ```

5. Deploy the registry:

   ```bash
   az deployment group create \
     --name apim-mcp-registry \
     --resource-group "<resource-group>" \
     --template-file infra/container-registry/main.bicep \
     --parameters infra/container-registry/bicep-params/my-environment.bicepparam
   ```

6. Build and push the image:

   ```bash
   az acr build \
     --registry "<registry-name>" \
     --image "apim-mcp:<tag>" \
     .
   ```

7. Set `containerImage` in the Container App parameter file to the image from
   step 6, then deploy:

   ```bash
   az deployment group create \
     --name apim-mcp-app \
     --resource-group "<resource-group>" \
     --template-file infra/container-app/main.bicep \
     --parameters infra/container-app/bicep-params/my-environment.bicepparam
   ```

8. Read the deployed audience:

   ```bash
   az deployment group show \
     --name apim-mcp-app \
     --resource-group "<resource-group>" \
     --query properties.outputs.mcpServerAudience.value \
     --output tsv
   ```

9. Add that exact URL to the **server** app registration's `identifierUris`.
   Keep `http://localhost:8000/mcp` if local development is also required.

10. Verify the deployment and connect VS Code using the URL from step 8.

## Prerequisites

### Local tools

- Azure CLI
- Azure CLI Bicep support (`az bicep version`)
- Docker is optional; `az acr build` builds remotely
- Git, for generating an immutable image tag

### Azure permissions

The deploying identity needs permission to:

- create resources in the deployment resource group;
- create role assignments on the ACR, every configured APIM resource, and
  every configured Log Analytics workspace; and
- push or remotely build images in the registry.

In many tenants this requires a combination such as Contributor plus
User Access Administrator, or an equivalent custom role. Do not broaden the
runtime UAMI's permissions to make deployment succeed; deployment permissions
and runtime permissions are separate.

### Values to collect

- Azure subscription ID
- deployment resource group and region
- Entra tenant ID
- APIM resource ID for every instance the server will expose
- Log Analytics workspace resource ID for each APIM instance, if gateway-log
  tools will be enabled
- globally unique ACR name
- globally unique Container App name

An APIM resource ID has this shape:

```text
/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.ApiManagement/service/<service-name>
```

## 1. Configure Entra authentication

One server app registration is shared by local and deployed use. VS Code uses
Microsoft's existing public client; a project-owned client registration is
optional and needed only for the manual `make token` helper.

### Server app registration

Create `apim-mcp-server`:

1. Under **Manifest**, confirm `requestedAccessTokenVersion` is `2` in the
   `api` object.
2. Under **Expose an API**, initially set the Application ID URI to
   `http://localhost:8000/mcp`. This gives the registration a canonical
   resource URI before the deployed hostname exists.
3. Add the delegated scope `Mcp.Tools.Read`.
   Set **Who can consent** to **Admins and users**.
4. Under **App roles**, create `Apim.Read` for `Users/Groups`.
5. On the corresponding enterprise application, set
   **Assignment required** to **Yes**.
6. Assign the users or groups allowed to call the server to `Apim.Read`.
7. Record the app registration's **Application (client) ID**. This becomes
   `mcpServerAppId` in the Container App parameter file.

The deployed Application ID URI is not known until the Container App has an
FQDN. Add it after deployment in
[Register the deployed audience](#7-register-the-deployed-audience).

### OAuth client preauthorization

On the **server** registration, under **Expose an API → Authorized client
applications**, add the official Visual Studio Code client ID
`aebc6443-996d-45c2-90f0-388ff96faa56` and select `Mcp.Tools.Read`.
This supports automatic VS Code OAuth for both local and deployed endpoints.

The repository also retains an optional project public-client registration,
`apim-mcp-client`, for manual token testing with `make token`. If that helper
is needed:

1. Under **Authentication**, add the Mobile and desktop redirect URIs
   `http://localhost`, `http://127.0.0.1:33418`, and
   `https://vscode.dev/redirect`.
2. Under **API permissions**, add the server application's delegated
   `Mcp.Tools.Read` permission.
3. On the server's **Authorized client applications** list, add
   `4c5ab830-b291-4308-8ae2-70d3f978e4a0` and select `Mcp.Tools.Read`.

The optional client registration does not receive the Container App URL. Only
the server registration's Application ID URIs change after deployment.

See [`AUTH.md`](AUTH.md) for token validation, role enforcement, OAuth
discovery, and the v1 shared-managed-identity authorization boundary.

## 2. Configure deployment parameters

### Container registry

Copy and edit the sanitized registry example:

```bash
cp infra/container-registry/bicep-params/example.bicepparam \
  infra/container-registry/bicep-params/my-environment.bicepparam
```

The resulting environment-specific file should contain values such as:

```bicep
param location = 'eastus'
param registryName = 'acrapimmcpunique'
```

The registry name must be 5-50 alphanumeric characters with no hyphens and
must be globally unique. Registry admin credentials remain disabled.
The registry is deployed separately and its region is independent of the
Container App region; moving the Container App does not require moving ACR.

### Container App and RBAC

Copy and edit the sanitized Container App example:

```bash
cp infra/container-app/bicep-params/example.bicepparam \
  infra/container-app/bicep-params/my-environment.bicepparam
```

The resulting environment-specific file should contain values such as:

```bicep
param location = 'westus3'
param containerRegistryName = 'acrapimmcpunique'
param logAnalyticsWorkspaceName = 'law-apim-mcp'
param appInsightsName = 'appi-apim-mcp'
param uamiName = 'id-apim-mcp'
param containerAppEnvironmentName = 'cae-apim-mcp'
param containerAppName = 'ca-apim-mcp'
param containerImage = 'acrapimmcpunique.azurecr.io/apim-mcp:<tag>'
param azureTenantId = '<tenant-id>'
param mcpServerAppId = '<server-app-client-id>'
param mcpRequiredRole = 'Apim.Read'

param apimServices = [
  {
    alias: 'prod'
    resourceId: '/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.ApiManagement/service/<name>'
    logAnalyticsWorkspaceId: '/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.OperationalInsights/workspaces/<workspace>'
  }
]
```

`containerRegistryName` must exactly match the registry parameter file.
`containerImage` must reference an image that exists before the Container App
deployment starts.

`apimServices` is both:

- the server's authoritative allowlist; and
- the source for the per-resource RBAC assignments.

Each `alias` is the friendly value clients pass to tools, such as
`service="prod"`. Omit `logAnalyticsWorkspaceId` when gateway-log access is
not needed for that instance.

The template assigns:

- the built-in **API Management Service Reader Role** to the UAMI on each
  individual APIM resource; and
- the built-in **Log Analytics Reader** role to the UAMI on each configured
  workspace.

It never grants these runtime roles at resource-group or subscription scope.
Do not replace the APIM role with **Reader** or **Monitoring Reader**, because
their `*/read` grant would include APIM user-key reads.

## 3. Validate the templates

Compile both templates locally before making Azure changes:

```bash
az bicep build \
  --file infra/container-registry/main.bicep \
  --outfile /tmp/apim-mcp-registry.json

az bicep build \
  --file infra/container-app/main.bicep \
  --outfile /tmp/apim-mcp-app.json
```

After the resource group exists, validate the registry deployment:

```bash
az deployment group validate \
  --resource-group "<resource-group>" \
  --template-file infra/container-registry/main.bicep \
  --parameters infra/container-registry/bicep-params/my-environment.bicepparam
```

Validate the Container App template after the registry and image exist, as
shown in [Deploy the Container App](#6-deploy-the-container-app).

## 4. Deploy the registry

Create the target resource group if necessary:

```bash
az group create \
  --name "<resource-group>" \
  --location "<region>"
```

Deploy the standalone registry:

```bash
az deployment group create \
  --name apim-mcp-registry \
  --resource-group "<resource-group>" \
  --template-file infra/container-registry/main.bicep \
  --parameters infra/container-registry/bicep-params/my-environment.bicepparam
```

Confirm its login server:

```bash
az deployment group show \
  --name apim-mcp-registry \
  --resource-group "<resource-group>" \
  --query properties.outputs.loginServer.value \
  --output tsv
```

## 5. Build and push the image

Prefer an immutable tag so updating the parameter creates a new Container App
revision:

```bash
IMAGE_TAG="$(git rev-parse --short HEAD)"

az acr build \
  --registry "<registry-name>" \
  --image "apim-mcp:${IMAGE_TAG}" \
  .
```

Set `containerImage` in
`infra/container-app/bicep-params/my-environment.bicepparam` to:

```text
<registry-name>.azurecr.io/apim-mcp:<tag>
```

The Container App pulls through the UAMI and an identity-based `AcrPull`
assignment. No registry username, password, or admin account is used.

## 6. Deploy the Container App

Validate the deployment against the now-existing registry and target
resources:

```bash
az deployment group validate \
  --resource-group "<resource-group>" \
  --template-file infra/container-app/main.bicep \
  --parameters infra/container-app/bicep-params/my-environment.bicepparam
```

Then deploy:

```bash
az deployment group create \
  --name apim-mcp-app \
  --resource-group "<resource-group>" \
  --template-file infra/container-app/main.bicep \
  --parameters infra/container-app/bicep-params/my-environment.bicepparam
```

The deployment creates:

- a Container Apps environment;
- a Container App with external HTTPS ingress on target port 8000;
- `minReplicas: 1` and `maxReplicas: 3`;
- a UAMI attached to the app;
- identity-based ACR pull;
- built-in APIM and Log Analytics role assignments at narrow scopes;
- a Log Analytics workspace for Container Apps logs; and
- Application Insights for server telemetry.

Container Apps environment logs are routed to the workspace through Azure
Monitor diagnostic settings. The template does not retrieve or embed the
workspace shared key.

The template sets these application environment variables:

| Variable | Source |
|---|---|
| `AZURE_TENANT_ID` | `azureTenantId` parameter |
| `AZURE_CLIENT_ID` | generated UAMI client ID |
| `MCP_SERVER_AUDIENCE` | generated Container App URL plus `/mcp` |
| `MCP_SERVER_APP_ID` | `mcpServerAppId` parameter |
| `MCP_REQUIRED_ROLE` | `mcpRequiredRole` parameter |
| `APIM_SERVICES` | `apimServices` parameter |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | generated Application Insights resource |

`APIM_MCP_LOCAL_DEV_CREDENTIAL` is intentionally absent. Deployed calls use
the UAMI, not a developer's Azure CLI session.

Container Apps environments have open outbound access by default. If the
environment is later VNet-integrated or restricted by a firewall, preserve
egress to:

- `management.azure.com`
- `login.microsoftonline.com`
- `api.loganalytics.io`
- `*.blob.core.windows.net`
- Application Insights ingestion endpoints

## 7. Register the deployed audience

Read the exact URL generated by the template:

```bash
MCP_URL="$(az deployment group show \
  --name apim-mcp-app \
  --resource-group "<resource-group>" \
  --query properties.outputs.mcpServerAudience.value \
  --output tsv)"

printf '%s\n' "$MCP_URL"
```

It will look like:

```text
https://<app>.<region>.azurecontainerapps.io/mcp
```

On the **`apim-mcp-server`** app registration, add this exact value to the
Manifest's `identifierUris` array. Preserve the localhost URI rather than
replacing it. Matching is character-for-character:

- use `https`;
- include `/mcp`;
- do not add a trailing slash; and
- do not replace it with an `api://` URI.

Keep `http://localhost:8000/mcp` in `identifierUris` if the same registration
will support local development.

No client app registration update and no second infrastructure deployment are
required. If the Container App hostname changes later, repeat this step for
the new hostname.

## 8. Verify the deployment

The health and discovery endpoints do not require a token:

```bash
curl --fail --silent --show-error \
  "https://<app>.<region>.azurecontainerapps.io/healthz"

curl --fail --silent --show-error \
  "https://<app>.<region>.azurecontainerapps.io/readyz"

curl --fail --silent --show-error \
  "https://<app>.<region>.azurecontainerapps.io/.well-known/oauth-protected-resource"
```

Confirm the discovery document's `resource` equals the deployed MCP URL
exactly and `scopes_supported` contains the fully-qualified delegated scope:

```text
https://<app>.<region>.azurecontainerapps.io/mcp/Mcp.Tools.Read
```

It must not contain the short value `Mcp.Tools.Read`, which Entra interprets
as a Microsoft Graph scope. An unauthenticated MCP request should return
`401` with a `WWW-Authenticate` header that points to the protected-resource
metadata:

```bash
curl --include "https://<app>.<region>.azurecontainerapps.io/mcp"
```

Inspect application logs if startup or readiness fails:

```bash
az containerapp logs show \
  --name "<container-app-name>" \
  --resource-group "<resource-group>" \
  --follow
```

## 9. Connect a client

### VS Code and GitHub Copilot

Add the deployed endpoint to `.vscode/mcp.json`:

```json
{
  "servers": {
    "apim": {
      "type": "http",
      "url": "https://<app>.<region>.azurecontainerapps.io/mcp"
    }
  }
}
```

Run **MCP: List Servers** and start `apim`. VS Code discovers the Entra
authorization server, prompts you to sign in, and refreshes tokens
automatically. No manually minted token or static `Authorization` header is
needed.

### Foundry

```bash
azd ai connection create apim-mcp-conn \
  --kind remote-tool \
  --target "https://<app>.<region>.azurecontainerapps.io/mcp" \
  --auth-type user-entra-token \
  --audience "https://<app>.<region>.azurecontainerapps.io/mcp"
```

Set `require_approval: "never"` because every tool is read-only.

## Updating the deployment

For an application update:

1. Build a new immutable image tag with `az acr build`.
2. Change `containerImage` in the Container App parameter file.
3. Re-run the `apim-mcp-app` deployment.
4. Verify health and readiness.

For a new APIM instance:

1. Add it to `apimServices`.
2. Re-run the Container App deployment so the allowlist and RBAC assignments
   are updated together.

For a renamed or recreated Container App, add the new generated MCP URL to the
server app registration before reconnecting clients.

## Local development

Local development uses the same inbound Entra validation but swaps the
deployed UAMI for the developer's `az login` session on outbound Azure calls.

```bash
uv sync
cp .env.example .env
az login
```

Set at least:

```dotenv
AZURE_TENANT_ID=<tenant-id>
AZURE_CLIENT_ID=00000000-0000-0000-0000-000000000000
MCP_SERVER_AUDIENCE=http://localhost:8000/mcp
MCP_SERVER_APP_ID=<server-app-client-id>
APIM_SERVICES=[{"alias":"prod","resourceId":"/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.ApiManagement/service/<name>"}]
APPLICATIONINSIGHTS_CONNECTION_STRING=InstrumentationKey=00000000-0000-0000-0000-000000000000
APIM_MCP_LOCAL_DEV_CREDENTIAL=1
```

Then:

```bash
make run
```

Configure VS Code with `http://localhost:8000/mcp` and start the server from
**MCP: List Servers**. The local server uses the same automatic OAuth discovery
and sign-in flow as the deployed server.

`make token` is optional and is not required by VS Code. It acquires a
short-lived token through the project MSAL client and writes a static header to
`.vscode/mcp.json`; use it only for manual protocol testing or to isolate a
third-party client problem. Before using it, confirm that `client_id` in
`scripts/get_token.py` matches the optional client registration.
Never set `APIM_MCP_LOCAL_DEV_CREDENTIAL` in Container Apps.

## Troubleshooting

| Symptom | Cause | Resolution |
|---|---|---|
| Container cannot pull the image | Image/tag does not exist or `AcrPull` is missing | Confirm `containerImage`, the ACR build, the UAMI attachment, and the ACR role assignment |
| Deployment cannot create role assignments | Deploying identity lacks authorization permissions | Grant the deploying identity role-assignment permissions at the ACR, APIM, and workspace scopes; do not broaden the runtime UAMI |
| `401 missing Authorization header` | Client did not begin or complete OAuth | Restart the MCP server from VS Code and inspect the discovery endpoint |
| `401 wrong audience` | `MCP_SERVER_APP_ID` does not match the token's `aud` | Set `mcpServerAppId` to the server app registration's Application (client) ID |
| `403 missing required role` | Caller lacks `Apim.Read` | Assign the user or group to the app role on the server enterprise application |
| `AADSTS9010010` | Deployed URL is not registered exactly | Add the exact `mcpServerAudience` output to the server app's `identifierUris` |
| Tools return `access_denied` | UAMI RBAC is missing or scoped incorrectly | Check the per-APIM and per-workspace role assignments |
| OAuth sign-in or consent fails | Discovery scope or client preauthorization is incorrect | Confirm `scopes_supported` contains `<MCP_SERVER_AUDIENCE>/Mcp.Tools.Read` and preauthorize the official VS Code client |

## About `azd up`

`azure.yaml` maps the `api` service to the Container App through its
`azd-service-name` tag and uses the template outputs for the resource name and
ACR endpoint. Remote builds use the repository's `.dockerignore`, which allows
only the Dockerfile, package manifests, and `src/` into the build context.

The registry and environment-specific Bicep parameters remain explicit
prerequisites, so use the Azure CLI sequence in this guide for the first
deployment. After provisioning, import the deployment outputs into the active
azd environment and use `azd deploy api` for subsequent application image
updates. A clean first-run `azd up` remains pending until the separate registry
and parameter layers are represented as a complete azd provisioning workflow.
