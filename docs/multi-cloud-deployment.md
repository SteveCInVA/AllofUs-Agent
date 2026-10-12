# Multi-Cloud Deployment (Azure Commercial + Azure Government / GCC)

**Status: Implemented.** One codebase deploys to **Azure Commercial** or an **Azure
Government (GCC)** subscription, with the cloud chosen entirely by the `$cloud` deploy
parameter and runtime-injected endpoint settings — there is no cloud-specific
branching in the Python.

## Cloud value matrix (driven by one `$cloud` parameter)

| Setting | Commercial (`AzureCloud`) | Government (`AzureUSGovernment`) |
|---|---|---|
| Storage suffix | `core.windows.net` | `core.usgovcloudapi.net` |
| Private-link zones | `privatelink.*.core.windows.net` | `privatelink.*.core.usgovcloudapi.net` |
| Function hostname | `*.azurewebsites.net` | `*.azurewebsites.us` |
| Portal (CORS) | `portal.azure.com`, `ms.portal.azure.com` | `portal.azure.us` |
| Entra authority | `login.microsoftonline.com` | `login.microsoftonline.us` |
| Region example | `eastus` | `usgovvirginia` |

## How it works (as-built)

### Code
- `code/storage.py` — `_account_url()` prefers the runtime-injected `*__blobServiceUri`
  (set correctly per cloud by the deploy) and otherwise builds the URL from
  `STORAGE_ENDPOINT_SUFFIX` (default `core.windows.net`), so it works in both
  `core.windows.net` and `core.usgovcloudapi.net`.
- Managed identity uses `AZURE_AUTHORITY_HOST` (set per cloud) so
  `DefaultAzureCredential` targets the right authority. Connection-string auth
  auto-handles the suffix via `EndpointSuffix=`.

### Deployment script (`deployment/deploy_azure_infrastructure.ps1`)
- A `$cloud` `switch` derives `$endpointSuffix`, `$privateLinkSuffix`,
  `$functionHostSuffix`, `$authorityHost`, `$portalOrigins`, and default `$loc`.
- `az cloud set --name $cloud` runs before `az login`.
- Private-DNS zones and CORS origins are parameterized.
- Sets app settings `AzureWebJobsStorage__{blob,queue,table}ServiceUri`,
  `AZURE_AUTHORITY_HOST`, and `STORAGE_ENDPOINT_SUFFIX`.
- Includes a note: if Flex Consumption is unavailable in the target Gov region, fall
  back to an Elastic Premium / Consumption plan.

### Connector + testing
- The connector `host:` suffix is set per cloud on import (`.azurewebsites.net` /
  `.azurewebsites.us`).
- `deployment/testing_azure_functions.ps1` parameterizes the host suffix and runs
  after `az cloud set`.

## Deploy to a cloud
Set `$cloud` at the top of `deploy_azure_infrastructure.ps1`
(`AzureCloud` or `AzureUSGovernment`); everything else derives automatically.

## Operational confirmations for Azure Government / GCC
- **Outbound internet egress.** The refresh job pulls from public internet data
  sources (`www.researchallofus.org`, `federation.ccdi.cancer.gov`,
  `raw.githubusercontent.com`), which are identical in both clouds. Commercial
  Functions egress freely; a locked-down Gov subscription may block outbound — ensure
  egress is allowed, or route it through Azure Firewall/NAT with an allow-list of
  those three domains, or ingestion (`/api/refresh`) fails.
- **Flex Consumption region availability** in the chosen Gov region (or use a Premium
  / Consumption plan).
- **Power Platform GCC parity** — build the connector/agent in the matching GCC
  environment.
- **Observability (when added):** use `APPLICATIONINSIGHTS_CONNECTION_STRING` (embeds
  the correct per-cloud ingestion endpoint), never a hardcoded endpoint/key.

## Validation (run in each target cloud)
1. `az cloud set` → deploy → confirm resources use the right suffixes.
2. `/api/health` reachable; `/api/refresh` builds indexes (proves egress + storage).
3. `/api/search` returns results; connector import + test in that cloud's Power
   Platform environment.
