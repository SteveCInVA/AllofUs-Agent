# NIH All of Us — Greenfield deployment (infrastructure + identity)
#
# Stands up the whole solution from scratch in one run: resource group, Entra app
# registrations + groups + admin role, VNet + private endpoints, storage, Function
# App (system-assigned managed identity), Key Vault, role assignments, Easy Auth,
# and all app settings (including entitlement-based authorization, enforced from
# day one). After this runs, publish the code and assign users to the groups.
#
# Assumes an existing subscription and a caller with rights to create these objects
# (including Entra Application Administrator / Cloud App Admin for the app
# registrations, groups, and role).

##################################################
# update the following variables as required
$cloud = "AzureCloud"          # or "AzureUSGovernment" for Azure Government / GCC
$rg  = "rg-allofus-demo18"
$sfx = "aou0918"
$storageAcctName = "staallofus$sfx"
$functionSvcName = "func-allofus-$sfx"
$kvName = "kv-allofus-$sfx"
$vnetName = "vnet-allofus"
$vnetAddressSpace = "192.168.0.0/24"
$funcSubnetName = "pe-functions"
$funcSubnetAddrSpace = "192.168.0.0/25"
$peSubnetStorage = "pe-storage"
$peSubnetStorageAddrSpace = "192.168.0.128/25"
$adminRole = "Agent.Admin"

# Cloud-specific endpoints derived from $cloud (do not edit unless adding a cloud).
# Override $loc below if you want a different region within the selected cloud.
switch ($cloud) {
  "AzureUSGovernment" {
    $loc                = "usgovvirginia"
    $endpointSuffix     = "core.usgovcloudapi.net"
    $privateLinkSuffix  = "core.usgovcloudapi.net"
    $functionHostSuffix = "azurewebsites.us"
    $authorityHost      = "https://login.microsoftonline.us"
    $portalOrigins      = @("https://portal.azure.us")
  }
  default {
    $loc                = "eastus"
    $endpointSuffix     = "core.windows.net"
    $privateLinkSuffix  = "core.windows.net"
    $functionHostSuffix = "azurewebsites.net"
    $authorityHost      = "https://login.microsoftonline.com"
    $portalOrigins      = @("https://portal.azure.com", "https://ms.portal.azure.com")
  }
}
##################################################

# Target the correct Azure cloud BEFORE login (commercial vs. government)
az cloud set --name $cloud

# Log in only if not already authenticated to the selected cloud
az account show -o none 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Output "Not logged in; launching az login..."
    az login
} else {
    Write-Output "Already logged in as $(az account show --query user.name -o tsv)"
}
$tenantId = az account show --query tenantId -o tsv

# Resource Group
az group create --name $rg --location $loc

# ============================================================ 1. Entra identity
# API app registration (the Function's audience) + client app (the connector) + groups.
$apiApp = az ad app create --display-name "AllOfUs-Function-API" --query appId -o tsv
az ad app update --id $apiApp --identifier-uris "api://$apiApp"
# Add the delegated scope `access_as_user` (Expose an API) and the `$adminRole` app
# role (allowedMemberTypes ["User"]). These use the app manifest — easiest in the
# portal (App registrations > Expose an API / App roles) or via `az ad app update
# --set` with a JSON manifest.

$clientApp = az ad app create --display-name "AllOfUs-Function-Client" --query appId -o tsv
$clientSecret = az ad app credential reset --id $clientApp --append --query password -o tsv
# Pre-authorize the client to the API's access_as_user scope (portal: API
# permissions > Add a permission > My APIs > AllOfUs-Function-API > Grant consent).

$baseGroup = az ad group create --display-name "AoU-Agent-Users" --mail-nickname "AoU-Agent-Users" --query id -o tsv
$ihccGroup = az ad group create --display-name "AoU-DS-IHCC" --mail-nickname "AoU-DS-IHCC" --query id -o tsv
$ccdiGroup = az ad group create --display-name "AoU-DS-CCDI" --mail-nickname "AoU-DS-CCDI" --query id -o tsv
# Emit FILTERED group claims (only groups assigned to the app) to keep tokens lean:
# portal: API app > Token configuration > Add groups claim > Groups assigned to the
# application; assign AoU-Agent-Users / AoU-DS-* to the API app's enterprise app.

# ============================================================ 2. Networking
az network vnet create `
  --resource-group $rg `
  --name $vnetName `
  --address-prefix $vnetAddressSpace `
  --subnet-name $funcSubnetName `
  --subnet-prefix $funcSubnetAddrSpace

az network vnet subnet create `
  --resource-group $rg `
  --vnet-name $vnetName `
  --name $peSubnetStorage `
  --address-prefixes $peSubnetStorageAddrSpace

# ============================================================ 3. Storage + Function
az storage account create `
  --name $storageAcctName `
  --resource-group $rg `
  --location $loc `
  --min-tls-version TLS1_2 `
  --sku Standard_LRS

# NOTE: Flex Consumption is not available in every Azure Government region. If
# --flexconsumption-location fails for the selected $loc, either pick a Gov region
# that offers Flex Consumption or switch to an Elastic Premium / Consumption plan.
az functionapp create `
  --name $functionSvcName `
  --resource-group $rg `
  --storage-account $storageAcctName `
  --flexconsumption-location $loc `
  --runtime python `
  --runtime-version 3.12 `
  --vnet $vnetName `
  --subnet $funcSubnetName `
  --disable-app-insights `
  --deployment-storage-auth-type SystemAssignedIdentity `
  --assign-identity [system]

# principal id of the Function's system-assigned managed identity
$sami = az functionapp identity show --resource-group $rg --name $functionSvcName --query principalId --output tsv
$storageId = az storage account show --resource-group $rg --name $storageAcctName --query id --output tsv

# ============================================================ 4. Role assignments (storage)
az role assignment create --assignee-object-id $sami --assignee-principal-type ServicePrincipal `
  --role "Storage Blob Data Contributor" --scope $storageId
az role assignment create --assignee-object-id $sami --assignee-principal-type ServicePrincipal `
  --role "Storage Queue Data Contributor" --scope $storageId
az role assignment create --assignee-object-id $sami --assignee-principal-type ServicePrincipal `
  --role "Storage Table Data Contributor" --scope $storageId

# ============================================================ 5. Private endpoints
$zones = @(
    "privatelink.blob.$privateLinkSuffix",
    "privatelink.queue.$privateLinkSuffix",
    "privatelink.table.$privateLinkSuffix"
)
foreach ($zone in $zones) {
    az network private-dns zone create --resource-group $rg --name $zone
    $linkName = "link-" + $zone.Split(".")[1]
    az network private-dns link vnet create `
        --resource-group $rg --zone-name $zone --name $linkName `
        --virtual-network $vnetName --registration-enabled false
}

az network private-endpoint create `
	--resource-group $rg --name "pe-$storageAcctName-blob" `
	--vnet-name $vnetName --subnet $peSubnetStorage `
	--private-connection-resource-id $storageId --group-id blob `
	--connection-name "pl-$storageAcctName-blob"
az network private-endpoint dns-zone-group create `
    --resource-group $rg --endpoint-name "pe-$storageAcctName-blob" `
    --name "storage-dns" --private-dns-zone "privatelink.blob.$privateLinkSuffix" --zone-name blob

foreach ($service in @("queue", "table")) {
    az network private-endpoint create `
        --resource-group $rg --name "pe-$storageAcctName-$service" `
        --vnet-name $vnetName --subnet $peSubnetStorage `
        --private-connection-resource-id $storageId --group-id $service `
        --connection-name "pl-$storageAcctName-$service"
    az network private-endpoint dns-zone-group create `
        --resource-group $rg --endpoint-name "pe-$storageAcctName-$service" `
        --name "storage-dns" --private-dns-zone "privatelink.$service.$privateLinkSuffix" --zone-name $service
}

# ============================================================ 6. Key Vault (connector secret)
az keyvault create --name $kvName --resource-group $rg --enable-rbac-authorization true
az keyvault secret set --vault-name $kvName --name "connector-client-secret" --value $clientSecret
$kvId = az keyvault show --name $kvName --query id -o tsv
az role assignment create --assignee-object-id $sami --assignee-principal-type ServicePrincipal `
  --role "Key Vault Secrets User" --scope $kvId

# ============================================================ 7. App settings
# Use managed identity for AzureWebJobsStorage (identity-based, per-cloud service URIs)
az functionapp config appsettings delete --resource-group $rg --name $functionSvcName `
  --setting-names AzureWebJobsStorage
az functionapp config appsettings set --resource-group $rg --name $functionSvcName `
  --settings `
      "AzureWebJobsStorage__accountName=$storageAcctName" `
      "AzureWebJobsStorage__credential=managedidentity" `
      "AzureWebJobsStorage__blobServiceUri=https://$storageAcctName.blob.$endpointSuffix" `
      "AzureWebJobsStorage__queueServiceUri=https://$storageAcctName.queue.$endpointSuffix" `
      "AzureWebJobsStorage__tableServiceUri=https://$storageAcctName.table.$endpointSuffix"

# Classification + entitlements. Authorization is ENFORCED from day one (greenfield).
$datasetClassification = @{
  ihcc = @{ classification = "restricted"; entitlement_group_id = $ihccGroup }
  ccdi = @{ classification = "restricted"; entitlement_group_id = $ccdiGroup }
} | ConvertTo-Json -Compress

az functionapp config appsettings set --resource-group $rg --name $functionSvcName `
  --settings `
      "AzureWebJobsFeatureFlags=EnableWorkerIndexing" `
      "AZURE_AUTHORITY_HOST=$authorityHost" `
      "STORAGE_ENDPOINT_SUFFIX=$endpointSuffix" `
      "AUTH_ENFORCED=true" `
      "ADMIN_ROLE=$adminRole" `
      "BASE_ENTITLEMENT_GROUP_ID=$baseGroup" `
      "DATASET_CLASSIFICATION=$datasetClassification"

# ============================================================ 8. Easy Auth (Entra)
# Require a valid Entra token for all routes; the app declares /api/health anonymous.
az webapp auth update --resource-group $rg --name $functionSvcName `
  --enabled true `
  --action Return401 `
  --aad-allowed-token-audiences "api://$apiApp" `
  --aad-token-issuer-url "$authorityHost/$tenantId/v2.0" `
  --aad-client-id $apiApp
# Exclude the health endpoint from platform authentication (portal: Authentication >
# Edit > Excluded paths = /api/health, OR set globalValidation.excludedPaths via az rest).

# ============================================================ 9. CORS
foreach ($origin in $portalOrigins) {
  az functionapp cors add --resource-group $rg --name $functionSvcName --allowed-origins $origin
}

# ============================================================ Summary
Write-Output ""
Write-Output "Deployment complete."
Write-Output "  Function:     https://$functionSvcName.$functionHostSuffix"
Write-Output "  API appId:    $apiApp"
Write-Output "  Client appId: $clientApp   (secret in Key Vault '$kvName' / connector-client-secret)"
Write-Output "  Base group:   $baseGroup (AoU-Agent-Users)"
Write-Output "  IHCC group:   $ihccGroup (AoU-DS-IHCC)"
Write-Output "  CCDI group:   $ccdiGroup (AoU-DS-CCDI)"

##################################
# NEXT STEPS
#  1. Publish the code:   cd /code ; func azure functionapp publish $functionSvcName --build remote
#  2. Assign users to AoU-Agent-Users (base) + AoU-DS-IHCC / AoU-DS-CCDI as needed,
#     and grant the $adminRole app role to admins (for POST /api/refresh).
#  3. Trigger the first data load: POST /api/refresh with an Entra bearer token
#     (see /deployment/testing_azure_functions.ps1). The daily timer also refreshes.
#  4. Import the OAuth2 connector (custom_connector/openapi-swagger.yaml) in Copilot Studio.
##################################
