# Multi-Cloud Deployment Plan (Azure Commercial + Azure Government / GCC)

**Goal:** one codebase deployable to **Azure Commercial** or an **Azure Government
(GCC)** subscription, with the cloud chosen entirely by deploy-time parameters and
runtime-injected endpoint settings — no cloud-specific branching in the Python.

## Cloud value matrix (driven by one `$cloud` parameter)

| Setting | Commercial (`AzureCloud`) | Government (`AzureUSGovernment`) |
|---|---|---|
| Storage suffix | `core.windows.net` | `core.usgovcloudapi.net` |
| Private-link zones | `privatelink.*.core.windows.net` | `privatelink.*.core.usgovcloudapi.net` |
| Function hostname | `*.azurewebsites.net` | `*.azurewebsites.us` |
| Portal (CORS) | `portal.azure.com`, `ms.portal.azure.com` | `portal.azure.us` |
| Entra authority (auth work) | `login.microsoftonline.com` | `login.microsoftonline.us` |
| Region example | `eastus` | `usgovvirginia` |

## Findings (hardcoded values today)

| File / line | Value | Fix |
|---|---|---|
| `code/storage.py` `_account_url` | `…blob.core.windows.net` fallback | prefer injected `*__blobServiceUri`; make suffix configurable via `STORAGE_ENDPOINT_SUFFIX` |
| `deployment/deploy_azure_infrastructure.txt` | `privatelink.*.core.windows.net`, `portal.azure.com`, region, `az login` | parameterize from `$cloud`; `az cloud set` first |
| `custom_connector/openapi-swagger.yaml` | `host: *.azurewebsites.net` | use `.azurewebsites.us` for Gov when importing |
| `deployment/testing_azure_functions.txt` | `*.azurewebsites.net` | parameterize the host suffix |

Outbound data sources (`www.researchallofus.org`, `federation.ccdi.cancer.gov`,
`raw.githubusercontent.com`) are **public commercial internet** and identical in
both clouds — see the egress risk below.

## Changes

### A. Code (small, defensive)
- `storage.py`: `_account_url()` builds the URL from `STORAGE_ENDPOINT_SUFFIX`
  (default `core.windows.net`). The deploy also sets `AzureWebJobsStorage__blobServiceUri`
  to the correct per-cloud URL, which the code already prefers — so the constructed
  fallback is only a safety net.
- Managed identity: rely on `AZURE_AUTHORITY_HOST` app setting (set per cloud) so
  `DefaultAzureCredential` uses the right authority. Connection-string auth already
  auto-handles the suffix via `EndpointSuffix=`.

### B. Deployment script
- Add a `$cloud` parameter with a `switch` that sets `$endpointSuffix`,
  `$privateLinkSuffix`, `$functionHostSuffix`, `$authorityHost`, `$portalOrigins`,
  and default `$loc`.
- `az cloud set --name $cloud` before `az login`.
- Parameterize the private-DNS zones and CORS origins.
- Add app settings: `AzureWebJobsStorage__{blob,queue,table}ServiceUri`,
  `AZURE_AUTHORITY_HOST`, `STORAGE_ENDPOINT_SUFFIX`.
- **Flex Consumption availability:** verify Flex is offered in the target Gov region;
  if not, fall back to Elastic Premium / Consumption (documented in the script).

### C. Connector / D. Testing
- Set the connector `host:` suffix per cloud on import.
- Testing script parameterizes the host suffix and runs after `az cloud set`.

### E. Networking / egress (biggest operational risk)
The refresh job must reach the **public internet** data sources. Commercial Functions
egress freely; a locked-down **Gov** subscription may block outbound. Confirm outbound
is allowed, or route egress through **Azure Firewall/NAT with an allowlist** of the
three source domains. Without this, ingestion fails in Gov even though the code is
correct.

### F. Observability (future)
Use `APPLICATIONINSIGHTS_CONNECTION_STRING` (embeds the correct per-cloud ingestion
endpoint) — never a hardcoded endpoint/instrumentation key.

## Validation (run in each target cloud)
1. `az cloud set` → deploy → confirm resources use the right suffixes.
2. `/api/health` reachable; `/api/refresh` builds indexes (proves egress + storage).
3. `/api/search` returns results; connector import + test in that cloud's Power
   Platform environment.

## Risks to confirm
- Flex Consumption region availability in the chosen Gov region.
- Outbound internet egress in the Gov subscription.
- Power Platform GCC parity for the connector/agent.
- Managed-identity token authority in Gov (mitigated by `AZURE_AUTHORITY_HOST`).

## Effort
~3–4h: mostly deploy-script parameterization + two small code/config safety nets +
a validation pass per cloud. Low code risk.
