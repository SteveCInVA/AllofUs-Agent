# Copilot Agent to assist with finding similar research and datasets across NIH-affiliated sources.

The agent searches four public sources for work similar to a description the user provides:
- All of Us Research Project Directory — https://www.researchallofus.org/research-project-directory/
- All of Us Publication Directory — https://www.researchallofus.org/publication-directory/
- IHCC Cohort Atlas (health cohorts) — https://ihccglobal.org/ (ingested from the Apache-2.0 `IHCC-cohorts/data-harmonization` GitHub source)
- CCDI Federation (pediatric cancer datasets, study level) — https://federation.ccdi.cancer.gov/api/v1

---
- Steve Carroll - Microsoft
- Intial version:  2026-07-10
  - Updated:       2026-07-31 - Support cache refresh / expiration
  - Updated:       2026-09-15 - Improved handling of new datasets + added in IHCC and CCDI datasets.
                              - Incorporated Security Model to handle public / restricted data handling
                              - Updated documentation / deployment for configuration in Azure Commericial / GCC


## Assumptions:
The deployment code assumes:
- Active Azure Subscription
- Access to the AzureCLI
- Permissions to a subscription that may create a resource group and required objects.
- **Azure Functions Core Tools (`func`)** and **Python 3.12** installed locally (for code publish / optional local index build).
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
> the first refresh is run.

### Deployment overview

1. **Prerequisites** — tooling and Entra permissions (see *Assumptions* above).
2. **Set deployment variables** in `deploy_azure_infrastructure.ps1`.
3. **Deploy infrastructure + identity** — run the script.
4. **Finish the API app registration** — expose scope, create the `Agent.Admin` app role, add the groups claim, authorize the groups/clients, exclude `/api/health` from Easy Auth. *(manual — the script stubs these)*
5. **Publish the function code.**
6. **Assign users & entitlements** — groups + the `Agent.Admin` role.
7. **Load data** — run the first `/api/refresh` (or wait for the daily 03:00 UTC timer).
8. **Test** the `/health`, `/refresh`, and `/search` endpoints.
9. **Import the custom connector** in Copilot Studio.
10. **Build the Copilot Studio agent.**

---

### Azure Function

#### Deployment Variables
The following varaiables are defined in the top of the deploy_azure_infrastructure.ps1 file.  They represent the following configurations:

|Variable|Default Value|Purpose|
|-----|-----|-----|
|$cloud|"AzureCloud"|Target Azure cloud: `AzureCloud` (Commercial) or `AzureUSGovernment` (GCC). Derives all cloud-specific endpoints.|
|$rg|"rg-allofus-demo01"|Resource group name|
|$loc|derived from $cloud|Deployment Region (defaults: `eastus` commercial / `usgovvirginia` government; override after the switch if needed)|
|$sfx|"aou1234"|Suffix to apply to resources|
|$storageAcctName|"staallofus$sfx"|Resource name of storage account (includes suffix)|
|$functionSvcName|"func-allofus-$sfx"|Resource name of Azure Function (includes suffix)|
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
- Enable CORS to allow testing from https://portal.azure.com and https://ms.portal.azure.com

#### Configure the Entra identity (required before publish/test)

`deploy_azure_infrastructure.ps1` creates the two app registrations, the three security
groups, the `Agent.Admin` role **name**, Key Vault, Easy Auth, and all app settings — but
a few identity items are left to finish by hand (the script marks each one inline). Do
these **once per environment**, using the `API appId` / `Client appId` printed in the
deploy summary.

> **App registration vs. enterprise application.** Each app is two Entra objects: the
> **app registration** (the global *definition* — exposed scopes, app roles, the groups
> claim) and the **enterprise application** / *service principal* (the local instance in
> your tenant, where **user/group assignments** live). Steps 1–4 below edit the app
> registration; step 5 uses the enterprise application. Both show the name
> `AllOfUs-Function-API` and are linked by the app ID.

**First, create the service principal.** The deploy script provisions the API with
`az ad app create`, which creates only the app-registration object — **not** the service
principal — so `AllOfUs-Function-API` does **not** yet appear under *Enterprise
applications*. Create it now (this also unblocks step 5 and the *Assign users &
entitlements* phase, both of which need the service principal to exist):

```powershell
az ad sp create --id "<API-APP-ID>"     # materializes the enterprise application
```

In **Entra admin center → App registrations → `AllOfUs-Function-API`**:

1. **Expose an API** — confirm the Application ID URI is `api://<API-APP-ID>` (set by the script), then **Add a scope**: name `access_as_user`, *Who can consent* **Admins and users**, and enable it.
2. **App roles → Create app role** — display name `Agent.Admin`, *Allowed member types* **Users/Groups**, value **`Agent.Admin`**, enabled. *(this app role gates `POST /api/refresh`)*
3. **Token configuration → Add groups claim** — choose **Groups assigned to the application** (filtered — keeps tokens small) and include it in the **Access** token.
4. **Expose an API → Authorized client applications → Add a client application** — authorize each of these for the `access_as_user` scope:
   - the connector client `<CLIENT-APP-ID>` (`AllOfUs-Function-Client`), and
   - *(only if you'll call `/api/refresh` from the Azure CLI as shown later)* the **Azure CLI**, appId `04b07795-8ddb-461a-bbee-02f9e1bf7b46`.

In **Entra admin center → Enterprise applications → `AllOfUs-Function-API` → Users and groups**:

5. **Add** all three groups — `AoU-Agent-Users`, `AoU-DS-IHCC`, `AoU-DS-CCDI` — with the **Default Access** role (not `Agent.Admin`). This creates an app-role assignment linking each group to the service principal, and the *filtered* groups claim from step 3 (**Groups assigned to the application**) emits **only** groups assigned here. Skip it and members' tokens carry no group IDs, so `/search` returns 403 for everyone. *(CLI equivalent: `POST /groups/<group-id>/appRoleAssignments` with `resourceId` = the API service principal's object id and `appRoleId` = the all-zeros Default Access role `00000000-0000-0000-0000-000000000000`.)*

On the **Function app → Settings → Authentication** blade (the deploy script already
configures this via `config/authsettingsV2` — **verify only**):

6. Confirm the Microsoft identity provider shows **Unauthenticated requests → HTTP 401 Unauthorized**, allowed token audience `api://<API-APP-ID>`, and **Excluded paths** containing `/api/health`. No manual edit is normally needed.

> The scope, app role, and groups claim can also be scripted with `az rest` PATCH calls
> against the app manifest, but the portal path above is the reliable default and only
> runs once per environment.

#### Function Code Deployment

Use the same window that the infrastructure deployment completed in, then publish the
code (the build runs remotely; ~5 minutes):

```
cd /code
func azure functionapp publish $functionSvcName --build remote
```

> Deployed packages intentionally ship **no** search indexes — not even public ones — so
> restricted data can never be packaged. The app loads every dataset from Blob Storage on
> the first refresh (step 7). Running `python build_index.py --public` only populates a
> **local** `code/index/` for offline development; that folder is excluded from the
> deployment package.

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
$apiSp  = az ad sp show --id $apiApp --query id -o tsv   # API service principal (created in phase 4) object id

# Groups -> `groups` claim
az ad group member add --group "AoU-Agent-Users" --member-id $me   # base: required for /search
az ad group member add --group "AoU-DS-IHCC"     --member-id $me   # optional: unlock IHCC data
az ad group member add --group "AoU-DS-CCDI"     --member-id $me   # optional: unlock CCDI data

# Agent.Admin app role -> `roles` claim (required for /refresh)
$roleId = az ad app show --id $apiApp --query "appRoles[?value=='Agent.Admin'].id | [0]" -o tsv
az rest --method POST `
  --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$apiSp/appRoleAssignedTo" `
  --body (@{ principalId = $me; resourceId = $apiSp; appRoleId = $roleId } | ConvertTo-Json)
```

> **GCC:** use `https://graph.microsoft.us` for the `az rest` URI. To entitle many users,
> add them to the groups (and assign the role to a group instead of each user).

#### Load data (first refresh)

Deployed packages ship no index data, so populate Blob Storage by running one refresh (or
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

A source that is temporarily unavailable keeps its previous index (never blanked), so a
partial refresh won't wipe good data.

#### Testing

Testing scrips can be found in /deployment/testing_azure_functions.ps1

##### Testing Variables
The following parameters are defined in the /deployment/testing_azure_functions.ps1 file

|Variable|Default Value|Purpose|
|-----|-----|-----|
|$rg|"rg-allofus-demo01"|Deployment resource group name|
|$sfx|"aou1234"|Suffix defined in deployment steps|
|$app|"func-allofus-$sfx"|Not needed unless changed in deployment|
|$uri|"https://$app.azurewebsites.net/api"|Not needed unless operating in other than commericial Azure subscription|

### Testing functions
- Health - anonymous; returns every dataset (including restricted ones) with its name, classification, and record count, read from the manifest (`index/manifest.json`)
- Refresh - rebuilds each dataset's index and the manifest; a source that is temporarily unavailable keeps its previous index (never blanked). Requires the Admin role when `AUTH_ENFORCED=true`
- Search - Executes a basic query and displays results (trimmed to the caller's entitled datasets when `AUTH_ENFORCED=true`)

**Expected results after a successful first refresh**

- `/health` (anonymous) enumerates every dataset with name, classification, and record count — roughly `publication ~1432`, `project ~25898`, `ihcc 87`, `ccdi ~309` (CCDI varies as member nodes come and go).
- `/search` returns public (`publication`/`project`) results for any `AoU-Agent-Users` member, and adds `ihcc`/`ccdi` rows only for members of those groups.
- `/refresh` returns a per-dataset summary with status `200`.

**Troubleshooting**

| Symptom | Cause / fix |
|---|---|
| `az account get-access-token` → `AADSTS65001` (consent required) | Azure CLI not authorized on the API scope — do step 4.4, or run `az login --scope api://<API-APP-ID>/access_as_user` once. |
| `/search` → `403` base group required | Groups claim not emitting (steps 4.3–4.5) or the token predates the group assignment. Get a fresh token. |
| `/refresh` → `403` Admin role required | `Agent.Admin` not assigned (step 6) or a stale token. |
| `/refresh` → `500` on IHCC/CCDI | Outbound egress to the three public data domains is blocked (see the GCC egress caveat above). |
| `/health` → `401` | `/api/health` is not in Easy Auth **Excluded paths** (step 4.6). |

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
registrations, the groups (`AoU-Agent-Users`, `AoU-DS-IHCC`, `AoU-DS-CCDI`), the
`Agent.Admin` role name, Easy Auth, a Key Vault for the connector secret, and the auth
app settings. A few app-registration manifest items and **all** user/entitlement
assignments are finished after the run — the full, ordered procedure is in
**Deployment steps** above:

1. *Configure the Entra identity* — expose the `access_as_user` scope, create the
   `Agent.Admin` app role, add the groups claim, authorize the groups/clients, and
   exclude `/api/health` from Easy Auth.
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