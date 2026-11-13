# Multi-Cloud Deployment (Azure Commercial and Azure Government)

**As-built review: 2026-11-07, implementation commit `23f9658`.** One Python
codebase and an imperative PowerShell/Azure CLI deployment script contain
commercial and government endpoint configuration. This is not a Bicep deployment
and is not proof that the current script succeeds unchanged in both clouds.

Azure hosting cloud and Power Platform **GCC/GCC High** environment are separate
choices. Do not infer a tenant's Entra authority or Graph endpoint solely from
the label "GCC"; verify the actual hosting cloud, identity tenant, connector
environment, and sovereign-cloud integration support with the customer.

## Cloud value matrix

These are the values selected by the script's `$cloud` variable:

| Setting | `AzureCloud` | `AzureUSGovernment` |
|---|---|---|
| Default region | `eastus` | `usgovvirginia` |
| Storage suffix | `core.windows.net` | `core.usgovcloudapi.net` |
| Private DNS | `privatelink.{blob,queue,table}.core.windows.net` | `privatelink.{blob,queue,table}.core.usgovcloudapi.net` |
| Function hostname suffix | `azurewebsites.net` | `azurewebsites.us` |
| Search hostname suffix | `search.windows.net` | `search.azure.us` |
| Search private DNS | `privatelink.search.windows.net` | `privatelink.search.azure.us` |
| Search token audience | `https://search.azure.com` | `https://search.azure.us` |
| Portal CORS origins | `portal.azure.com`, `ms.portal.azure.com` | `portal.azure.us` |
| Entra authority | `login.microsoftonline.com` | `login.microsoftonline.us` |
| Microsoft Graph | Derived from `az cloud show` (`graph.microsoft.com`) | Derived from `az cloud show` (`graph.microsoft.us`) |
| ARM REST base | Derived from `endpoints.resourceManager` | Derived from `endpoints.resourceManager` |

Endpoint selection is configuration-driven; the Python source connectors use the
same public metadata endpoints in both clouds.

## Deployment script (as implemented)

[`../deployment/deploy_azure_infrastructure.ps1`](../deployment/deploy_azure_infrastructure.ps1)
has editable `$cloud`, `$rg`, `$sfx`, network variables, and derived resource names.
Current checked-in examples are `rg-allofus-demo23` / `aou0923`; do not treat
examples as a discovered live deployment. The script:

1. Selects the Azure CLI cloud and checks for an existing sign-in.
2. Creates the resource group, new API/client app registrations, v2
   `access_as_user` scope, `Agent.Access`/`Agent.Admin` roles, filtered group
   claims, API service principal, and group-to-`Agent.Access` assignments. Groups
   are looked up/reused by display name; missing group IDs cause a throw.
3. Creates the VNet/subnets, Standard LRS storage, and Python 3.12 Flex Consumption
   Function with VNet integration and system-assigned managed identity.
4. Grants Blob/Queue/Table Data Contributor and provisions corresponding storage
   private endpoints, DNS zones, and VNet links. Also creates Basic Azure AI Search
   (one replica/partition, free semantic ranker allowance) with public access and
   API keys disabled, a private endpoint in the same private-endpoint subnet,
   private DNS and VNet link. Grants the Function Search Index Data Reader,
   Search Index Data Contributor, and Search Service Contributor; configures
   `AZURE_SEARCH_ENDPOINT` and `AZURE_SEARCH_AUDIENCE`. Search provisioning errors
   terminate the script. Grants the Azure CLI deployer Search Service Contributor,
   creates or updates the empty index from [index.json](../index.json), and verifies
   it by reading it back. Run from a private-VNet-connected host; index writes
   retry up to 20 times with 30-second waits for RBAC/DNS/service readiness.
   Existing retrieval is unchanged; documents, embeddings, and Azure OpenAI
   resources are not provisioned. See [Search Index Structure](search-index.md).
5. Creates an RBAC Key Vault with public network access enabled, grants deployer
   Secrets Officer and Function Secrets User, and tries to store the connector
   secret after a 30s wait. Failed writes warn and print the secret; no vault
   private endpoint or policy exemption is configured.
6. Removes the runtime storage connection string and sets managed-identity service
   URIs, cloud authority/storage suffix, and authorization settings.
   `DATASET_CLASSIFICATION` is supplied through a JSON settings file, not an
   inline quoted argument.
7. Uses ARM `authsettingsV2` for Easy Auth: v2 tenant issuer, URI and bare-ID
   audiences, connector/Azure CLI client allowlist, Return401, health exclusion,
   and token store. Adds cloud-selected portal CORS origins.

The script does **not** publish code, populate indexes, assign users/admins,
pre-authorize OAuth clients, configure the connector's Web redirect URI, import
the connector, build the agent, or enable observability. These are operator
actions. Application Insights is explicitly disabled. See
[`auth-design.md`](auth-design.md) for the client/consent setup.

### Known deployment limitations

- **First-login cloud mismatch:** the no-sign-in branch currently hardcodes
  `az login --scope https://graph.microsoft.us/.default`, even when `$cloud` is
  `AzureCloud`. This is a code defect, not a cloud-derived scope. Until corrected,
  sign into the chosen cloud/subscription/tenant before running the script so
  the existing-sign-in branch is used.
- **Not generally fail-fast or idempotent:** most native `az` errors do not stop
  PowerShell; app registrations/role IDs are recreated on rerun, while groups
  can be reused. Do not rerun the whole script as an assumed repair operation
  for a partially failed deployment.
- **Network boundaries:** storage private endpoints do not disable its public
  endpoint or shared keys. Key Vault is explicitly public and has no private
  link. Customer policies may block these operations. A console-printed secret
  does not resolve policy compliance or vault connectivity. Search is private-only
  from creation: data-plane administration/testing requires VNet connectivity
  and Search RBAC. Validate private DNS and an authenticated Search request from
  the Function network; script completion does not prove connectivity or RBAC
  propagation. Check Search/semantic ranker availability in the selected region.
  Basic capacity and Private Link are billable; one replica is development
  capacity, not a production availability configuration.
- **Hosting availability:** the script always requests Flex Consumption. There
  is no automatic Premium/other-plan fallback; verify service and Python runtime
  support in the target region/cloud before deploying.
- **Refresh duration:** `POST /api/refresh` is synchronous and can exceed the
  230s HTTP response limit because CCDI can take several minutes. The daily
  03:00 UTC timer is independent of the HTTP response window; no durable
  asynchronous refresh endpoint exists.

Deploy from Windows with Azure CLI, Functions Core Tools, and the required Azure
resource/RBAC and Entra application/group-assignment permissions. Scripts are
ASCII-only for Windows PowerShell 5.1 compatibility. JSON files are used for
complex CLI bodies, with explicit JSON content type on Graph/ARM write requests.
Do not paste deployment secret output into logs or tickets.

## Runtime storage

`code/storage.py` prefers a configured `CORPUS_STORAGE` or
`AzureWebJobsStorage` connection string. Without one, `_account_url()` uses
`CORPUS_STORAGE__blobServiceUri` or `AzureWebJobsStorage__blobServiceUri`, falling
back to account name plus `STORAGE_ENDPOINT_SUFFIX` (default
`core.windows.net`). It then uses `DefaultAzureCredential`; the deploy supplies
`AZURE_AUTHORITY_HOST` and managed-identity storage settings.

Python Functions consumes local settings via Core Tools; running an ordinary
Python script does not automatically load `local.settings.json`.
`build_index.py` is a local-development utility. `code/.funcignore` excludes all
indexes, so **every fresh deployment starts without searchable data**.

## Connector and validation

Set the Swagger `host`, tenant/API placeholders, and **both authorization/token
URLs** for the actual identity cloud; changing only `.azurewebsites.net` to `.us`
does not change OAuth authorities. The connector exposes GET search and GET
health only; health is anonymous at the API but inherits OAuth in the spec.
Register the generated callback URI on the client app and pre-authorize the API
scope before creating a delegated connection.

`testing_azure_functions.ps1` derives the host suffix from its own `$cloud`.
Set its `$sfx`/`$apiAppId` to the deploy output. It acquires a CLI token, calls
health, performs refresh, and tests `all`, `ihcc`, and `ccdi` queries.
It does not grant memberships or admin privileges.

After every deployment, verify:

1. Selected cloud, subscription, tenant, endpoint suffixes, actual persisted
   classification JSON, app-role/group assignments, and Easy Auth configuration.
2. Anonymous health and fresh-token authorization: no base group -> search 403;
   base only -> public results; each restricted group -> only its compartment;
   both groups -> both; non-admin refresh -> 403.
3. Refresh execution and manifest per-source counts/statuses. Health `status: ok`
   is not proof of a successful refresh; absent/unreadable manifests currently
   return an empty dataset list. After HTTP timeout, inspect logs and manifest
   before triggering overlapping work.
4. Connector consent/callback setup and end-user delegated identity in the target
   Power Platform environment.

Outbound access is needed to `www.researchallofus.org`,
`raw.githubusercontent.com`, and `federation.ccdi.cancer.gov`, plus required
platform/identity endpoints. The script does not create an egress firewall/NAT
allowlist. Per-cloud testing is still required; offline pytest does not validate
government deployment or live Entra/Power Platform integration.

The checked-in `NIHAllofUs_1_0_0_2.zip` is a separate Power Platform solution
snapshot containing concrete connector configuration and connection references.
It is not a cloud-parameterized replacement for the deployment script; import,
rebind, and inspect it for the target environment. Its model, browsing, and
instruction configuration differ from the manual README build; details are in
[`architecture.md`](architecture.md#6-checked-in-power-platform-export).

## Teardown

[`../deployment/cleanup_agent.ps1`](../deployment/cleanup_agent.ps1) has independent
`$cloud`, `$rg`, `$sfx`, and location settings. Review them before running.
It deletes/purges the selected vault, dispatches asynchronous resource-group
deletion, and removes API/client apps and the three `AoU-*` groups **by name across
the tenant**, including duplicate-name matches. Those Entra names are not scoped
by `$sfx` and groups may be shared/reused by another deployment.

`-Force` bypasses confirmation; `-SkipAzure`/`-SkipEntra` limit categories.
`-KeepKeyVault` skips the explicit purge, **not vault deletion by resource-group
deletion**. `-PurgeDeletedApps` permanently deletes matching soft-deleted
`AllOfUs-Function*` applications. Purge protection and eventual consistency may
prevent immediate name reuse. Power Platform connectors/agents remain manual.

The script reads account context **before** switching cloud, suppresses many CLI
errors, and has no completion verification. Select and verify the intended cloud
and account first; its displayed context and "Cleanup dispatched" message alone
are not adequate verification. Do not use the name-based Entra cleanup in a
shared tenant without independently confirming every object's ownership.

## Observability

[`../plans/observability-plan.md`](../plans/observability-plan.md) remains proposed.
No Application Insights component, Log Analytics workspace, diagnostic settings,
or alerts are deployed. `host.json` sampling settings alone do not enable them.
If implemented later, use a per-cloud Application Insights connection string
rather than a hardcoded ingestion endpoint.
