# apim-mcp

Read-only MCP server for exploring Azure API Management from an MCP-compatible
client. It exposes API inventory and configuration, OpenAPI definitions,
search, and service health without exposing APIM secrets.

## Why a purpose-built APIM MCP server

Azure MCP Server provides broad Azure coverage but does not expose a dedicated
API Management (`apim`) namespace. APIM requests therefore rely on generic
Azure Resource Graph, Azure CLI, and ARM REST operations. This server exposes
focused, read-only APIM tools directly, reducing discovery and response
interpretation while returning bounded, redacted results.

In an observed four-prompt comparison using the same model, the purpose-built
server completed the workload in roughly one-third of the session time while
using about 72% fewer tokens and 74% fewer AI credits. Both approaches answered
the core questions accurately, but the focused server needed substantially
fewer tool calls and returned APIM-specific results directly. Azure MCP
produced broader infrastructure detail in some responses, but did so through
additional discovery, CLI, and REST operations.

## Connect to a deployed server

### VS Code and GitHub Copilot

Add the server URL supplied by the operator to `.vscode/mcp.json`:

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

Run **MCP: List Servers** and start `apim`. VS Code discovers the Entra OAuth
configuration, prompts you to sign in, and refreshes tokens automatically.
You do not add an `Authorization` header or mint tokens manually.

Your account must be assigned the `Apim.Read` app role on the server's Entra
enterprise application. All authorized users see the APIM instances configured
by the server operator.

### Connect to a local server

Start the server with `make run`, then use
`http://localhost:8000/mcp` in `.vscode/mcp.json`. The local endpoint uses the
same VS Code OAuth discovery and sign-in flow as the deployed endpoint. The
only difference is outbound Azure authentication: local development uses your
`az login` session, while the deployed server uses its managed identity.

### Foundry

```bash
azd ai connection create apim-mcp-conn \
  --kind remote-tool \
  --target "https://<app>.<region>.azurecontainerapps.io/mcp" \
  --auth-type user-entra-token \
  --audience "https://<app>.<region>.azurecontainerapps.io/mcp"
```

Set `require_approval: "never"` because every exposed tool is read-only.

## Available tools

Every tool accepts `response_format: "markdown" | "json"`. List tools support
pagination, and tools that accept a `service` parameter use the friendly alias
configured by the server operator, such as `prod`.

### Service discovery

| Tool | Returns |
|---|---|
| `apim_list_services` | Configured APIM instances and their basic status |
| `apim_get_service` | Service configuration, networking, hostnames, and certificate metadata |
| `apim_get_service_health` | Provisioning, Resource Health, certificate warnings, capacity, and dependency status |

### API configuration

| Tool | Returns |
|---|---|
| `apim_list_apis` | APIs, revisions, paths, protocols, and subscription requirements |
| `apim_get_api` | One API and its operations |
| `apim_get_policy` | Redacted policy XML at global, API, operation, or product scope |
| `apim_get_api_spec` | OpenAPI/Swagger summary or full definition |
| `apim_list_products` | Products and approval/subscription settings |
| `apim_list_backends` | Backend metadata without credentials |
| `apim_list_named_values` | Named-value metadata; secret values are never returned |
| `apim_list_subscriptions` | Subscription metadata; keys are never returned |

### Search

| Tool | Returns |
|---|---|
| `apim_search_apis` | Ranked lexical search across API and operation metadata |
| `apim_refresh_index` | Rebuilds the API search index for one or all configured services |

### Telemetry

| Tool | Returns |
|---|---|
| `apim_get_metrics` | Aggregate APIM capacity, request, and duration metrics from `AzureMetrics` |
| `apim_query_gateway_logs` | Bounded gateway-log details filtered by API, operation, response category, duration, or correlation ID |
| `apim_summarize_errors` | Gateway failures grouped by API, error reason, and response code |

Telemetry requires each existing APIM service to already route `AllMetrics`
to the configured Log Analytics workspace and populate the resource-specific
`ApiManagementGatewayLogs` table. This repository does not modify APIM
diagnostic settings. Metric dimensions are not available through the
diagnostic export; use the gateway-log tools for API, operation, response-code,
and error breakdowns.

The implemented tool list evolves with `docs/development/TASKS.md`; clients discover the
currently registered set directly from the server.

## Example requests

- “List the APIs on `prod` that do not require a subscription.”
- “Find operations related to inventory or stock levels.”
- “Show the effective policy for the `orders` API.”
- “Which hostname certificate expires first?”
- “Show aggregate request volume and capacity for `prod` over the last hour.”
- “Summarize the most common gateway errors on `prod` in the last 24 hours.”
- “List the named values marked secret.” The server returns names and flags,
  never the secret contents.

## Access and safety

- The server is read-only.
- It never retrieves subscription keys, named-value secrets, gateway keys,
  user tokens, certificate contents, or backend credentials.
- The server operator controls which APIM instances are available.
- In v1, every authorized caller sees the same configured instances because
  downstream Azure access uses the server's managed identity.
- Every tool invocation is audited with caller identity and arguments, not
  response bodies.

## User troubleshooting

| Symptom | Resolution |
|---|---|
| `401 missing Authorization header` | Restart the server from **MCP: List Servers** so VS Code begins OAuth discovery |
| `403 missing required role` | Ask the operator to assign your account to the `Apim.Read` app role |
| OAuth sign-in fails | Confirm the server advertises the fully qualified `Mcp.Tools.Read` scope and that the official VS Code client is preauthorized |
| Tool returns `access_denied` | The server's managed identity lacks access to that configured Azure resource; contact the operator |

## Documentation

### Feature guides

- [Authentication and access control](docs/features/AUTH.md)
- [API and operation search](docs/features/SEARCH_INDEX.md)
- [Metrics and gateway-log telemetry](docs/features/TELEMETRY.md)

### Development

- [Principles](docs/development/PRINCIPLES.md) — non-negotiable security and design rules
- [Technical specification](docs/development/SPEC.md)
- [Implementation tasks](docs/development/TASKS.md)
- [Local testing and fixtures](docs/development/LOCAL_TESTING.md)

### Operations

- [Deployment guide](docs/operations/DEPLOYMENT.md)
- [Runbook](docs/operations/RUNBOOK.md)
