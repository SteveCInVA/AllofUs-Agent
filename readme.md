# Copilot Agent to assist with finding similar research and datasets across NIH-affiliated sources.

The agent searches four source families for work similar to a description the user provides:
- All of Us Research Project Directory — https://www.researchallofus.org/research-project-directory/
- All of Us Publication Directory — https://www.researchallofus.org/publication-directory/
- IHCC Cohort Atlas (health cohorts) — https://ihccglobal.org/ (ingested from the Apache-2.0 `IHCC-cohorts/data-harmonization` GitHub source)
- CCDI Federation (pediatric cancer datasets, study level) — https://federation.ccdi.cancer.gov/api/v1

> Current deployment model: the app is deployed as a greenfield Azure Function with Entra-based entitlement checks enabled from day one. The deployment provisions the API + client app registrations, the `access_as_user` delegated scope, the `Agent.Access` and `Agent.Admin` app roles, the filtered `groups` claim, the `AoU-Agent-Users` / `AoU-DS-IHCC` / `AoU-DS-CCDI` groups, Easy Auth with `/api/health` excluded, and the `DATASET_CLASSIFICATION` app setting that governs public vs. restricted dataset visibility.

> **As-built review (2026-11-07):** the [`docs/`](docs/architecture.md) references
> describe the current implementation and its remaining gaps. In particular,
> classification defaults are not fail-closed and HTTP refresh can exceed Azure's
> response timeout. Health queries Search but does not certify upstream freshness. Review the
> [deployment caveats](docs/multi-cloud-deployment.md#known-deployment-limitations)
> before treating this runbook or script completion as deployment verification.

---
- Steve Carroll - Microsoft
- Initial version:  2026-07-10
  - Updated:       2026-07-31 - Support cache refresh / expiration
  - Updated:       2026-09-15 - Improved handling of new datasets + added in IHCC and CCDI datasets.
                              - Incorporated Security Model to handle public / restricted data handling
                              - Updated documentation / deployment for configuration in Azure Commercial / GCC
  - Updated:       2026-09-22 - Finalized entitlement-based greenfield deployment flow, managed-identity storage auth, Easy Auth enforcement, and testing script workflow


## Assumptions:
The deployment code assumes:
- Active Azure Subscription
- Access to the AzureCLI
- Permissions to a subscription that may create a resource group and required objects.
- **Azure Functions Core Tools (`func`)** and **Python 3.12** installed locally (for code publish / optional command-line Search import).
- **Entra directory permissions** to create app registrations, security groups, and app-role assignments — **Application Administrator + Groups Administrator**, or **Global Administrator**.
- For GCC: a matching **GCC Power Platform** environment, and substitute the government endpoints where noted (`graph.microsoft.us`, `portal.azure.us`, `.azurewebsites.us`).

## Cloud selection & pre-deployment confirmations

This solution deploys to **Azure Commercial** or **Azure Government (GCC)**, selected
by the `$cloud` variable at the top of `deploy_azure_infrastructure.ps1`
(`AzureCloud` or `AzureUSGovernment`). All cloud-specific endpoints (storage suffix,
private-link DNS zones, portal/CORS, Entra authority, region) are derived from that
one variable.

> ⚠️ **Confirm these before deploying to Azure Government / GCC:**
> - **Outbound internet egress.** The refresh job pulls from public internet data
>   sources — `www.researchallofus.org`, `federation.ccdi.cancer.gov`, and
>   `raw.githubusercontent.com`. Commercial Functions egress freely, but a
>   locked-down Gov subscription may block outbound traffic. Ensure egress is
>   allowed, or route it through Azure Firewall/NAT with an allow-list of those
>   three domains — otherwise ingestion (`/api/refresh`) will fail.
> - **Flex Consumption availability.** Flex Consumption is not offered in every Gov
>   region. If `--flexconsumption-location` fails for your `$loc`, pick a Gov region
>   that offers Flex Consumption or switch to an Elastic Premium / Consumption plan.
> - **Power Platform environment.** Build the custom connector and agent in the
>   matching **GCC** Power Platform environment, and use the `.azurewebsites.us`
>   host suffix in the connector's `host:` value.
>
> See `docs/multi-cloud-deployment.md` for the full multi-cloud reference.

## Deployment steps

> This is a **greenfield** deployment: it provisions everything from an empty
> subscription — infrastructure, the Entra identity/entitlement model, code, data, and
> the Copilot Studio agent. Perform the phases **in order**; several Entra steps must be
> finished before the app will authenticate, and the app has **no searchable data** until
> the first refresh is run. The live deployment now enables `AUTH_ENFORCED=true`, uses a
> managed-identity storage configuration, and applies dataset visibility from
> `DATASET_CLASSIFICATION` rather than hardcoded source assumptions.

### Deployment overview

1. **Prerequisites** — tooling and Entra permissions (see *Assumptions* above).
2. **Set deployment variables** in `deploy_azure_infrastructure.ps1`.
3. **Deploy infrastructure + identity** — run the script.
4. **Verify identity & authorize the connector** — the script provisions the scope, `Agent.Access` and `Agent.Admin` roles, groups claim, enterprise app, and group-to-access-role assignments; you pre-authorize the connector client (and optionally the Azure CLI) and verify Easy Auth excludes `/api/health`.
5. **Publish the function code.**
6. **Assign users & entitlements** — groups + the `Agent.Admin` role.
7. **Load data** — run the first `/api/refresh` (or wait for the daily 03:00 UTC timer).
8. **Test** the `/health`, `/refresh`, and `/search` endpoints.
9. **Import the custom connector** in Copilot Studio.
10. **Build the Copilot Studio agent.**

---

### Azure Function

#### Deployment Variables
The following variables are defined at the top of the `deploy_azure_infrastructure.ps1` file. They represent the following configurations:

|Variable|Default Value|Purpose|
|-----|-----|-----|
|$cloud|"AzureCloud"|Target Azure cloud: `AzureCloud` (Commercial) or `AzureUSGovernment` (GCC). Derives all cloud-specific endpoints.|
|$rg|"rg-allofus-demo01"|Resource group name|
|$loc|derived from $cloud|Deployment Region (defaults: `eastus` commercial / `usgovvirginia` government; override after the switch if needed)|
|$sfx|"aou1234"|Suffix to apply to resources|
|$storageAcctName|"staallofus$sfx"|Resource name of storage account (includes suffix)|
|$functionSvcName|"func-allofus-$sfx"|Resource name of Azure Function (includes suffix)|
|$searchSvcName|"srch-allofus-$sfx"|Globally unique Azure AI Search service name|
|$searchSku|"basic"|Search tier (billable development capacity)|
|$searchReplicaCount|1|Search replicas; review availability/SLA requirements for production|
|$searchPartitionCount|1|Search partitions|
|$searchSemanticSearch|"free"|Semantic ranker free request allowance; not the free Search service tier|
|$vnetName|"vnet-allofus"|Resource name of vNet|
|$vnetAddressSpace|"192.168.0.0/24"|CIDR of vNet|
|$funcSubnetName|"pe-functions"|Subnet name used for Azure Function|
|$funcSubnetAddrSpace|"192.168.0.0/25"|Subnet CIDR for Azure Function|
|$peSubnetStorage|"pe-storage"|Subnet name used for storage acct. private endpoints|
|$peSubnetStorageAddrSpace|"192.168.0.128/25"|Subnet CIDR for storage acct.|

#### Infrastructure Deployment Process

The code used to deploy the infrastructure can be found in /deployment/deploy_azure_infrastructure.ps1

Deployment will perform the following:
- Create a new resource group
- Create a new vNet
    - Create two separate subnets (Azure Functions + Private Endpoints)
- Create a storage account
    - Standard LRS
- Create Azure Function App
    - Uses newly created storage account
    - Flex Consumption
    - Python Version 3.12
    - Uses Azure Functions subnet
    - Creates System Assigned Managed Identity
- Assign the Function's System Assigned Managed Identity to
    - Storage Blob Data Contributor
    - Storage Queue Data Contributor
    - Storage Table Data Contributor
- Creates Private DNS Zones
    - privatelink.blob.core.windows.net
    - privatelink.queue.core.windows.net
    - privatelink.table.core.windows.net
- Creates the following private endpoints and associates record to correct DNS zone.
    - blob
    - queue
    - table
- Update Azure Function to use system assigned managed identity to access storage account
- Create Azure AI Search (Basic, one replica/partition, free semantic ranker allowance)
    - Enable public network access temporarily for deployment; keep API-key authentication disabled
    - Create a private endpoint in the existing private-endpoint subnet, private DNS zone, and VNet link
    - Grant the Function identity Search Index Data Reader, Search Index Data Contributor, and Search Service Contributor
    - Set `AZURE_SEARCH_ENDPOINT`, `AZURE_SEARCH_INDEX_NAME`, and cloud-specific `AZURE_SEARCH_AUDIENCE`
    - Grant the Azure CLI deployer Search Service Contributor, create/update the index from `index.json`, and verify it with a read-back request
- Enable CORS to allow testing from https://portal.azure.com and https://ms.portal.azure.com

Search provisioning creates the empty index defined in `index.json`. Application
refresh imports documents into that index, and search/health query it directly.
No documents, embeddings, or Azure OpenAI resources are loaded or created by the
infrastructure step. Basic capacity and Private Link incur charges; the free semantic ranker
allowance does not make the service free. Review region/cloud availability before
deployment. Search data-plane administration requires a host with private VNet
connectivity and appropriate Search RBAC; allow time for role propagation.
Public Search network access is enabled during deployment so index creation can
run without private VNet connectivity. Entra authentication and Search RBAC are
still required; API keys remain disabled. The script retries failed index
requests up to 20 times, waiting 30 seconds between attempts for RBAC, DNS, or
service readiness, then fails explicitly if creation or read-back verification
fails. **Public access remains enabled until you disable it manually**, even if
deployment fails. The script prints a warning and the command:

```powershell
az search service update --resource-group $rg --name $searchSvcName --public-network-access disabled
```

Run this after deployment is complete (or after troubleshooting a failure), then
verify Function search/health using the provisioned private endpoint and DNS.
Subsequent direct Search administration/import requires private connectivity.
Existing compatible
indexes are updated without deleting documents; incompatible schema changes fail
and require a separately planned migration.

See [Azure AI Search Index Structure](docs/search-index.md) for the initial
[index definition](index.json), common and source-specific field tables, security
filtering requirements, and semantic ranking configuration.

#### Configure the Entra identity (verify + authorize the connector)

`deploy_azure_infrastructure.ps1` now provisions the **full API identity model**: both app
registrations, the `access_as_user` delegated scope, the **`Agent.Admin`** app role, the
**filtered groups claim** (`groupMembershipClaims = ApplicationGroup`, emitted in the access
token Easy Auth reads), the three security groups, the API **enterprise application**
(service principal), and the assignment of each group to the app's non-admin **`Agent.Access`**
app role. Because the script adds the app roles *before* it creates the service
principal, the initial enterprise application is created with those roles already
defined. Token `roles` should contain assigned app-role values such as `Agent.Admin`;
group object IDs belong in the separate `groups` claim.

> **App registration vs. enterprise application.** Each app is two Entra objects: the
> **app registration** (the global *definition* — scopes, app roles, the groups claim) and
> the **enterprise application** / *service principal* (the tenant-local instance where
> **user/group assignments** live). The script provisions both; you assign *users* to the
> groups under *Assign users & entitlements* below.

Only two identity items remain to finish by hand, **once per environment**, using the
`API appId` / `Client appId` printed in the deploy summary:

1. **Pre-authorize the connector for `access_as_user`.** In **Entra admin center → App
   registrations → `AllOfUs-Function-API` → Expose an API → Authorized client applications
   → Add a client application**, authorize:
   - the connector client `<CLIENT-APP-ID>` (`AllOfUs-Function-Client`), and
   - *(only if you'll call `/api/refresh` from the Azure CLI as shown later)* the **Azure
     CLI**, appId `04b07795-8ddb-461a-bbee-02f9e1bf7b46`.
2. **Verify Easy Auth** on the **Function app → Settings → Authentication** blade (the
   script configures it via `config/authsettingsV2`): Microsoft identity provider with
   **Unauthenticated requests → HTTP 401 Unauthorized**, allowed token audience
    `api://<API-APP-ID>`, an allowlist for the connector client and Azure CLI, and
        **Excluded paths** containing `/api/health`. The API registration must issue v2
        access tokens so their issuer matches this configuration. No edit is normally needed.

**Verify the group assignments** (optional):

```powershell
$apiSp = az ad sp show --id "<API-APP-ID>" --query id -o tsv
az rest --method GET --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$apiSp/appRoleAssignedTo" `
  --query "value[].{who:principalDisplayName, appRoleId:appRoleId}" -o table
```

Expect the three `AoU-*` groups on the non-admin `Agent.Access` role.
If a token has **GUIDs in `roles` but no `groups`**, check the API's optional group
claims for **Emit groups as role claims** (`emit_as_roles`). That option moves groups
into `roles` and suppresses assigned app-role values; it must not be enabled for this
API. Also verify role values/assignments and obtain a fresh API access token.
The symptom alone does not establish a stale service principal; do not delete it as
the default recovery. See [the claims reference](docs/auth-design.md#4-group-and-role-claims).
(For Azure Government Graph calls, use `graph.microsoft.us`.)

#### Function Code Deployment

Use the same window that the infrastructure deployment completed in, then publish the
code (the build runs remotely; ~5 minutes):

```
cd /code
func azure functionapp publish $functionSvcName --build remote
```

> Deployed packages intentionally ship **no** corpus data. The first refresh
> imports source records directly into Azure AI Search, without normalized-data
> cache blobs, pickle files, or a manifest blob. `python build_index.py --public`
> now imports public datasets into Search too; it does not create local indexes.
> See [Search import and refresh](docs/search-index.md#import-and-refresh).

When the publish completes you'll see the deployed functions and their URLs. **The app
has no searchable data yet** — you load it in step 7 below. First assign entitlements
(step 6) so you can authenticate, then run the first refresh.

#### Assign users & entitlements

Entitlements are Entra **group memberships** (the `groups` claim) plus the **`Agent.Admin`
app role** (the `roles` claim). Assign your test user(s). Group/role changes only take
effect on a **new** token, so sign out/in — or request a fresh token — afterward.

```powershell
$apiApp = "<API-APP-ID>"                                # from the deploy summary
$me     = az ad signed-in-user show --query id -o tsv   # or: az ad user show --id <upn> --query id -o tsv
$apiSp  = az ad sp show --id $apiApp --query id -o tsv   # API service principal (created by the deploy script) object id

# Groups -> `groups` claim
az ad group member add --group "AoU-Agent-Users" --member-id $me   # base: required for /search
az ad group member add --group "AoU-DS-IHCC"     --member-id $me   # optional: unlock IHCC data
az ad group member add --group "AoU-DS-CCDI"     --member-id $me   # optional: unlock CCDI data

# Agent.Admin app role -> `roles` claim (required for /refresh)
$roleId = az ad app show --id $apiApp --query "appRoles[?value=='Agent.Admin'].id | [0]" -o tsv

# az rest POST/PATCH to Graph must declare Content-Type, and inline JSON is fragile
# under PowerShell quoting -> write the body to a temp file and pass it with @.
$assignment = @{ principalId = $me; resourceId = $apiSp; appRoleId = $roleId } | ConvertTo-Json -Compress
$bodyFile = New-TemporaryFile
Set-Content -Path $bodyFile.FullName -Value $assignment -Encoding ascii
az rest --method POST `
  --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$apiSp/appRoleAssignedTo" `
  --headers "Content-Type=application/json" `
  --body "@$($bodyFile.FullName)"
Remove-Item $bodyFile.FullName -Force
```

> **GCC:** use `https://graph.microsoft.us` for the `az rest` URI. To entitle many users,
> add them to the groups (and assign the role to a group instead of each user).

#### Get a new token 

The deploying user needs a fresh token to import data and query datasets.

````powershell
az logout
az account clear
az login
````

#### Load data (first refresh)

Deployed packages ship no corpus data, so populate Azure AI Search by running one refresh (or
wait for the daily **03:00 UTC** timer). `/api/refresh` requires the `Agent.Admin` role.

```powershell
$sfx = "aou1234"                                        # same suffix used at deploy
$app = "func-allofus-$sfx"
$uri = "https://$app.azurewebsites.net/api"             # .azurewebsites.us on GCC

$token   = az account get-access-token --resource "api://$apiApp" --query accessToken -o tsv
$headers = @{ Authorization = "Bearer $token" }

# SLOW: the first refresh pulls all four sources; CCDI alone adds ~4 min.
Invoke-RestMethod -Method Post -Uri "$uri/refresh" -Headers $headers | ConvertTo-Json -Depth 6
```

A source fetch/normalization failure leaves its existing Search documents unchanged.
Successful empty or partial source responses are authoritative and can remove old
records. Uploads are acknowledged before stale IDs are deleted; an indexing failure
returns HTTP 500 and can leave partially updated documents. Run only one refresh at
a time. Search changes and counts are eventually consistent.

#### Testing

Testing scrips can be found in /deployment/testing_azure_functions.ps1

##### Testing Variables
The following parameters are defined in the /deployment/testing_azure_functions.ps1 file

|Variable|Default Value|Purpose|
|-----|-----|-----|
|$rg|"rg-allofus-demo01"|Deployment resource group name|
|$sfx|"aou1234"|Suffix defined in deployment steps|
|$app|"func-allofus-$sfx"|Not needed unless changed in deployment|
|$uri|"https://$app.azurewebsites.net/api"|Not needed unless operating in other than commercial Azure subscription|

### Testing functions
- Health - anonymous; queries Search for every enabled dataset's name, current classification, count, and latest indexed timestamp. Returns HTTP 503 when Search cannot be queried.
- Refresh - imports each source directly into Search; failed fetches retain existing documents. Requires the Admin role when `AUTH_ENFORCED=true`.
- Search - executes a full-text Search query filtered to the caller's entitled sources and matching indexed security fields when `AUTH_ENFORCED=true`.

**Expected results after a successful first refresh**

- `/health` (anonymous) enumerates every dataset with name, classification, and record count — roughly `publication ~1432`, `project ~25898`, `ihcc 87`, `ccdi ~309` (CCDI varies as member nodes come and go).
- `/search` returns public (`publication`/`project`) results for any `AoU-Agent-Users` member, and adds `ihcc`/`ccdi` rows only for members of those groups.
- `/refresh` returns a per-dataset summary with status `200`.

**Troubleshooting**

| Symptom | Cause / fix |
|---|---|
| `az account get-access-token` → `AADSTS65001` (consent required) | Azure CLI not pre-authorized on the API scope — do *Configure the Entra identity* step 1, or run `az login --scope api://<API-APP-ID>/access_as_user` once. |
| `/search` → `403` base group required | The token predates the group assignment (the script assigns the groups at deploy time). Get a fresh token; if it persists, verify the assignments (*Configure the Entra identity*). |
| `/refresh` → `403` Admin role required | `Agent.Admin` not granted to the user (*Assign users & entitlements*) or a stale token. |
| `/refresh` → `403` **but the role is assigned**; decoded token `roles` shows **GUIDs** and `groups` is empty | Check the API's group optional claims for `emit_as_roles` (must be off), verify role values and assignments, and acquire a fresh API token. Do not infer a stale service principal or recreate it from this symptom alone. Also inspect the response body: Easy Auth can return 403 before Python authorization runs. |
| `/refresh` reports `degraded` with `carried`/`failed` sources | Check outbound egress to the three public data domains and upstream availability (see the GCC egress caveat above). Existing indexed records are retained on fetch failures. |
| `/health` → `401` | `/api/health` is not in Easy Auth **Excluded paths** (*Configure the Entra identity* step 2). |
| `/health` → `503`, or Search calls fail | Verify `AZURE_SEARCH_ENDPOINT`, `AZURE_SEARCH_INDEX_NAME`, cloud audience/authority, index existence, Function Search RBAC, private DNS, and VNet connectivity. Blob contents are no longer a fallback. |

## Authentication & Authorization

The service uses per-user, entitlement-based access, provisioned by the greenfield
deployment and **enforced from day one**. Design details are in
[`docs/auth-design.md`](docs/auth-design.md) and
[`docs/security-hardening.md`](docs/security-hardening.md).

**Model**
- **Delegated Microsoft Entra sign-in** (the user's identity flows to the Function via
  App Service Authentication / "Easy Auth"). Function routes are anonymous at the
  Functions layer; Easy Auth is the gate and the code enforces entitlements.
- **Datasets are entitlements.** `publication`/`project` are public (gated by the base
  group `AoU-Agent-Users`); `ihcc`/`ccdi` are restricted, each requiring its own Entra
  group (`AoU-DS-IHCC`, `AoU-DS-CCDI`). Classification is driven by the
  `DATASET_CLASSIFICATION` app-setting — changing it is a config change, never a redeploy.
- `/search` requires the base group and returns only the caller's entitled datasets.
  `/refresh` requires the **`Agent.Admin`** app role. `/health` is anonymous and
  enumerates every dataset with name + count.

The deployment sets `AUTH_ENFORCED=true`. (The flag exists so the code can run without
Easy Auth for **local development** — `local.settings.json` sets it `false` — but
deployed environments always enforce.)

**Setup** — `deploy_azure_infrastructure.ps1` provisions the API + client app
registrations, the `access_as_user` scope, the `Agent.Admin` app role, the groups
(`AoU-Agent-Users`, `AoU-DS-IHCC`, `AoU-DS-CCDI`) and their assignment to the API app, the
filtered groups claim, the API enterprise application, Easy Auth, a Key Vault for the
connector secret, and the auth app settings. Only connector pre-authorization and **all**
user/entitlement assignments are finished after the run — the full, ordered procedure is
in **Deployment steps** above:

1. *Configure the Entra identity* — pre-authorize the connector client (and optionally the
   Azure CLI) for `access_as_user`, and verify Easy Auth excludes `/api/health`.
2. *Assign users & entitlements* — add users to `AoU-Agent-Users` (base) and to
   `AoU-DS-IHCC` / `AoU-DS-CCDI` as needed; grant admins the `Agent.Admin` app role.
3. *Load data (first refresh)* — POST `/api/refresh` with an admin bearer token, or wait
   for the daily 03:00 UTC timer.
4. *Import the connector* (`custom_connector/openapi-swagger.yaml`, delegated Entra
   OAuth 2.0) in Copilot Studio and create the connection with the client app's ID + the
   secret from Key Vault.

> Deployed packages ship **no** index data (not even public datasets), so nothing is
> searchable until the first refresh populates Blob Storage.

## Custom Connector (AKA Copilot Studio Tools)


1. Open the /custom_connector/openapi-swagger.yaml file.

1. Replace the values found in:  host: <functionServiceURL>.azurewebsites.net with the correct URL deployed in prior section.

1. Also fill in the OAuth 2.0 placeholders in the file (`<TENANT-ID>`, `<API-APP-ID>`) with the tenant and API app registration id from the deployment output.

1. Naviate to https://copilotstudio.microsoft.com

1. Naviate to Tools -> New Tool -> Custom Connector
    > This will launch the Power Apps Custom Connector screen

1. New Custom Connector -> Import an OpenAPI file
Provide the connector name and the /custom_connector/openapi-swagger.yaml file
    > **NOTE:** This file must have the correct URL specified in before importing.
    
    - Optional: After importing the openapi-swagger.yaml file, configure the connector icon available in /custom_connector/icons

1. On the **Security** tab, confirm **OAuth 2.0 / Azure Active Directory** and enter the **Client ID** of the `AllOfUs-Function-Client` app registration, the **Client Secret** (from Key Vault `connector-client-secret`), the **Tenant ID**, and Resource/Scope `api://<API-APP-ID>/access_as_user`.

1. Click Update connector to save the custom connector.

1. Click Test and create a new connection.  Sign in with a user who is a member of `AoU-Agent-Users` (and any dataset groups you want to test).

1. Select "childhood asthma" as the query, "all" for directory, 8 for top then click "Test operation"

Expect a Status 200 response with a body response with identified articiles.

## Copilot Studio Agent

> **Note:** After editing each section, be sure to click Save

1. Naviate to https://copilotstudio.microsoft.com
1. In the Agents page > Create blank agent
1. Name your agent:  "All of Us - Research Finder" (this will be the displayed name in the UI)
    > **Note:** Associate to a custom solution in this screen by selecting "Agent settings (Optional)"
1. Description: 

    ```Given a description of a research idea, disease, cohort, or dataset, finds similar existing work across four NIH-affiliated sources — All of Us publications and research projects, IHCC health cohorts, and CCDI pediatric-cancer datasets — and returns the most likely matches with clickable source links.```
1. Select your agent's model:

    ```GPT5 Chat```
1. Instructions:

    ```markdown
    You are the All of Us Research Finder. Your job is to help a user discover existing work similar to a research idea, topic, disease, cohort, or dataset they describe. You have one tool: searchDirectories, which searches four public sources and returns the most similar records with a source link for each:
    - publication — All of Us published papers
    - project — All of Us Researcher Workbench projects
    - ihcc — IHCC health cohorts (name, countries, diseases, data types, enrollment)
    - ccdi — CCDI pediatric-cancer datasets (study level)

    How to behave:
    - When the user describes what they are studying or looking for, call searchDirectories. Pass their description (lightly cleaned into keywords) as 'query'. Do not answer from your own knowledge — always search first.
    - Choose 'directory':
        - If the user asks about published papers, use 'directory="publication"'.
        - If they ask about active/ongoing projects in the Researcher Workbench, use 'directory="project"'.
        - If they ask about health cohorts or study populations, use 'directory="ihcc"'.
        - If they ask about pediatric or childhood cancer datasets, use 'directory="ccdi"'.
        - If they don't specify, use 'directory="all"' to search every source. You may briefly ask whether they'd like to narrow to a specific source, but never block on it.
    - Return the most likely matches (default 5–8). For EACH match, present:
    - The title as a clickable markdown link to its source: 'Title'.
    - A tag showing the record type (Publication, Project, Cohort, or Dataset).
    - One line of context from the snippet, plus helpful metadata when present (date/journal for publications; access tier for projects; countries/diseases/enrollment for cohorts; organization for datasets).
    - Order results from most to least similar (the tool returns them ranked).
    - Ground every statement in the returned records. Never invent titles, authors, findings, or links. Only show links returned by the tool.
    - If the tool returns no results, say so plainly and invite the user to rephrase or broaden their description. Do not fabricate matches.
    - Be concise and neutral. When useful, note that inclusion does not imply NIH endorsement, and that this covers public directory/registry data only.

    Scope: only help find and summarize research and datasets surfaced by the tool. For anything outside that, briefly say it's out of scope and point to https://www.researchallofus.org.

    Example answer shape:
    > Here are the closest matches to your idea:
    > 1. Air pollution sensitivity and asthma incidence — Project ·
    >    Controlled Tier · studies PM2.5 exposure and asthma onset.
    > 2. … — Cohort · Canada · ~345,000 participants · …
    ```
1. Suggested prompts:
    |Title|Prompt|
    |---|---|
    |Child Asthma Air Studies|I want to study how air pollution exposure affects asthma in children — what similar work exists?|
    |Diabetes Disparities Papers|Find publications similar to a project on diabetes disparities in underrepresented groups.|
    |Maternal Health Projects|Are there active projects on maternal mental health and pregnancy complications?|
    |Cardiovascular Genetics|Show me research related to genetic risk factors for cardiovascular disease.|
    |Population Health Cohorts|Are there large population health cohorts with genomic and EHR data?|
    |Pediatric Cancer Datasets|What pediatric cancer datasets are available for childhood tumors?|

1. Navigate to Tools from the top menu bar
1. Select + Add a tool
1. In the Add a tool dialog search for the custom connector name created in the prior section (NIH All of Us Directory Search)
1. When found, click "Add and Configure"
1. Under Inputs click "+ Add input" 
    - Add in both directory and top
1. When configured click "Save"
1. Naviate to Channels
    - Ensure the Microsoft 365 and Microsoft Teams channels are configured.

> The All of Us agent is now configured.  You may publish the agent and test prompt completions.

---
## Known Issues

- **IHCC Cohort Atlas live site**: the atlas at ihccglobal.org is frequently in maintenance and its live data carries a "mock/demo data — not appropriate for research" disclaimer. The agent therefore ingests the authoritative harmonized cohort metadata from the consortium's Apache-2.0 GitHub repo (`IHCC-cohorts/data-harmonization/data/cohort-data.json`) instead of the live API. There is no per-cohort deep link, so cohort results link to each cohort's own website.
- **CCDI Federation API is slow**: the aggregation endpoints (`/namespace`, `/info`, `/organization`) can take ~2 minutes each (~4 minutes total) because they fan out to federated member nodes, and an individual node may time out (its records are simply omitted for that refresh). The refresh uses a long timeout and no retry, and caches the result; searches always serve the cached snapshot so end-user latency is unaffected.
- **CCDI study-level search signal is limited**: study descriptions are generic per organization, so per-study matching is driven mainly by the study identifier. Richer disease-level search would require line-level Subject/Sample ingestion, which is out of scope (and governed by controlled-access agreements).
- **Source filter vocabulary**: the `directory` parameter now accepts `all`, `publication`, `project`, `ihcc`, and `ccdi`. The legacy value `both` is still accepted and treated the same as `all`.