# NIH All of Us — Security hardening: Entra ID auth, Easy Auth, Key Vault, app settings
#
# Run AFTER deploy_azure_infrastructure.ps1 (the Function must already exist).
# Some Entra operations may require Application Administrator / Cloud App Admin
# rights and a Graph-enabled az login. Values in <ANGLE BRACKETS> are filled in
# as you go. Parameterize $cloud the same way as the infrastructure script.

##################################################
$cloud = "AzureCloud"          # or "AzureUSGovernment" for GCC
$rg  = "rg-allofus-demo31"
$sfx = "aou0731"
$functionSvcName = "func-allofus-$sfx"
$kvName = "kv-allofus-$sfx"

switch ($cloud) {
  "AzureUSGovernment" { $authorityHost = "https://login.microsoftonline.us" }
  default             { $authorityHost = "https://login.microsoftonline.com" }
}
$tenantId = az account show --query tenantId -o tsv
##################################################

az cloud set --name $cloud
az login

# ------------------------------------------------------------------ 1. Entra apps
# API app registration (the Function's audience). Expose a scope + an Admin app role.
$apiApp = az ad app create --display-name "AllOfUs-Function-API" --query appId -o tsv
az ad app update --id $apiApp --identifier-uris "api://$apiApp"

# NOTE: define the access_as_user scope (oauth2PermissionScopes) and the Agent.Admin
# app role (appRoles) via the app manifest. Easiest in the portal (App registrations
# > Expose an API / App roles), or with `az ad app update --set` using a JSON manifest.
# Agent.Admin: value "Agent.Admin", allowedMemberTypes ["User"].

# Client app registration (used by the Power Platform connector, delegated).
$clientApp = az ad app create --display-name "AllOfUs-Function-Client" `
  --query appId -o tsv
$clientSecret = az ad app credential reset --id $clientApp --append `
  --query password -o tsv
# Pre-authorize the client to the API's access_as_user scope (portal: API permissions
# > Add a permission > My APIs > AllOfUs-Function-API > access_as_user > Grant consent).

# ------------------------------------------------------------------ 2. Groups
$baseGroup = az ad group create --display-name "AoU-Agent-Users" `
  --mail-nickname "AoU-Agent-Users" --query id -o tsv
$ihccGroup = az ad group create --display-name "AoU-DS-IHCC" `
  --mail-nickname "AoU-DS-IHCC" --query id -o tsv
$ccdiGroup = az ad group create --display-name "AoU-DS-CCDI" `
  --mail-nickname "AoU-DS-CCDI" --query id -o tsv

# Emit FILTERED group claims (only groups assigned to the app) to keep tokens lean:
# portal: API app > Token configuration > Add groups claim > Groups assigned to the
# application. Assign AoU-Agent-Users / AoU-DS-* to the API app's enterprise app.

# ------------------------------------------------------------------ 3. Key Vault
az keyvault create --name $kvName --resource-group $rg --enable-rbac-authorization true
az keyvault secret set --vault-name $kvName --name "connector-client-secret" `
  --value $clientSecret

# Grant the Function's managed identity read access to the secret
$sami = az functionapp identity show --resource-group $rg --name $functionSvcName `
  --query principalId -o tsv
$kvId = az keyvault show --name $kvName --query id -o tsv
az role assignment create --assignee-object-id $sami --assignee-principal-type ServicePrincipal `
  --role "Key Vault Secrets User" --scope $kvId

# ------------------------------------------------------------------ 4. Easy Auth
# Require a valid Entra token for all routes EXCEPT /api/health.
az webapp auth update --resource-group $rg --name $functionSvcName `
  --enabled true `
  --action Return401 `
  --aad-allowed-token-audiences "api://$apiApp" `
  --aad-token-issuer-url "$authorityHost/$tenantId/v2.0" `
  --aad-client-id $apiApp
# Exclude the health endpoint from authentication (portal: Authentication >
# Edit > Excluded paths = /api/health, OR set globalValidation.excludedPaths).

# ------------------------------------------------------------------ 5. App settings
# Classification + entitlements (group object IDs from step 2). AUTH_ENFORCED stays
# false until the cutover (see readme.md) so keys keep working during transition.
$datasetClassification = @{
  ihcc = @{ classification = "restricted"; entitlement_group_id = $ihccGroup }
  ccdi = @{ classification = "restricted"; entitlement_group_id = $ccdiGroup }
} | ConvertTo-Json -Compress

az functionapp config appsettings set --resource-group $rg --name $functionSvcName `
  --settings `
    "AUTH_ENFORCED=false" `
    "ADMIN_ROLE=Agent.Admin" `
    "BASE_ENTITLEMENT_GROUP_ID=$baseGroup" `
    "DATASET_CLASSIFICATION=$datasetClassification"

Write-Output "API appId:    $apiApp"
Write-Output "Client appId: $clientApp   (secret stored in Key Vault '$kvName')"
Write-Output "Base group:   $baseGroup"
Write-Output "IHCC group:   $ihccGroup"
Write-Output "CCDI group:   $ccdiGroup"

##################################
# Auth infrastructure staged. Cutover (readme.md): import the OAuth2 connector,
# set AUTH_ENFORCED=true, verify entitlements, set the /search + /refresh routes'
# auth level to ANONYMOUS (Easy Auth now gates), then disable function keys.
##################################
