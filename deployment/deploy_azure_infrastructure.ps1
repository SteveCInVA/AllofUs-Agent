# NIH - Provision Azure function resources - Changes required from original plan:
# Assumes existing subscription and calling user has permissions to deploy objects

##################################################
# update the folloing variables as required

$cloud = "AzureCloud"          # or "AzureUSGovernment" for Azure Government / GCC
$rg  = "rg-allofus-demo18"
$sfx = "aou0918"
$storageAcctName = "staallofus$sfx"
$functionSvcName = "func-allofus-$sfx"
$vnetName = "vnet-allofus"
$vnetAddressSpace = "192.168.0.0/24"
$funcSubnetName = "pe-functions"
$funcSubnetAddrSpace = "192.168.0.0/25"
$peSubnetStorage = "pe-storage"
$peSubnetStorageAddrSpace = "192.168.0.128/25"

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

# Resource Group
az group create `
  --name $rg `
  --location $loc

# Create Networking
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

# Storage Account Creation
az storage account create `
  --name $storageAcctName `
  --resource-group $rg `
  --location $loc `
  --min-tls-version TLS1_2 `
  --sku Standard_LRS

# Azure Function Creation
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

# Required for the Python v2 programming model:
az functionapp config appsettings set `
  --resource-group $rg `
  --name $functionSvcName `
  --settings AzureWebJobsFeatureFlags=EnableWorkerIndexing

# retrieve the principal id from azure function
$sami = az functionapp identity show `
    --resource-group $rg `
    --name $functionSvcName `
    --query principalId `
    --output tsv

# Get storage account resource id
$storageId = az storage account show `
	--resource-group $rg `
	--name $storageAcctName `
	--query id `
	--output tsv

# Assign Storage Blob Data Contributor
az role assignment create `
  --assignee-object-id $sami `
  --assignee-principal-type ServicePrincipal `
  --role "Storage Blob Data Contributor" `
  --scope $storageId

az role assignment create `
  --assignee-object-id $sami `
  --assignee-principal-type ServicePrincipal `
  --role "Storage Queue Data Contributor" `
  --scope $storageId

az role assignment create `
  --assignee-object-id $sami `
  --assignee-principal-type ServicePrincipal `
  --role "Storage Table Data Contributor" `
  --scope $storageId

$zones = @(
    "privatelink.blob.$privateLinkSuffix",
    "privatelink.queue.$privateLinkSuffix",
    "privatelink.table.$privateLinkSuffix"
)

foreach ($zone in $zones) {
    az network private-dns zone create `
        --resource-group $rg `
        --name $zone

    $linkName = "link-" + $zone.Split(".")[1]

    az network private-dns link vnet create `
        --resource-group $rg `
        --zone-name $zone `
        --name $linkName `
        --virtual-network $vnetName `
        --registration-enabled false
}

az network private-endpoint create `
	--resource-group $rg `
	--name "pe-$storageAcctName-blob" `
	--vnet-name $vnetName `
	--subnet $peSubnetStorage `
	--private-connection-resource-id $storageId `
	--group-id blob `
	--connection-name "pl-$storageAcctName-blob"

az network private-endpoint dns-zone-group create `
    --resource-group $rg `
    --endpoint-name "pe-$storageAcctName-blob" `
    --name "storage-dns" `
    --private-dns-zone "privatelink.blob.$privateLinkSuffix" `
    --zone-name blob

foreach ($service in @("queue", "table")) {
    az network private-endpoint create `
        --resource-group $rg `
        --name "pe-$storageAcctName-$service" `
        --vnet-name $vnetName `
        --subnet $peSubnetStorage `
        --private-connection-resource-id $storageId `
        --group-id $service `
        --connection-name "pl-$storageAcctName-$service"

    az network private-endpoint dns-zone-group create `
        --resource-group $rg `
        --endpoint-name "pe-$storageAcctName-$service" `
        --name "storage-dns" `
        --private-dns-zone "privatelink.$service.$privateLinkSuffix" `
        --zone-name $service
}

# update AzureWebJobsStorage to use System Managed Identity
az functionapp config appsettings delete `
  --resource-group $rg `
  --name $functionSvcName `
  --setting-names AzureWebJobsStorage

az functionapp config appsettings set `
  --resource-group $rg `
  --name $functionSvcName `
  --settings `
      "AzureWebJobsStorage__accountName=$storageAcctName" `
      "AzureWebJobsStorage__credential=managedidentity" `
      "AzureWebJobsStorage__blobServiceUri=https://$storageAcctName.blob.$endpointSuffix" `
      "AzureWebJobsStorage__queueServiceUri=https://$storageAcctName.queue.$endpointSuffix" `
      "AzureWebJobsStorage__tableServiceUri=https://$storageAcctName.table.$endpointSuffix"

# Cloud-neutral endpoint settings the app code reads (managed-identity authority +
# storage suffix used only if the *ServiceUri settings above are ever absent).
az functionapp config appsettings set `
  --resource-group $rg `
  --name $functionSvcName `
  --settings `
      "AZURE_AUTHORITY_HOST=$authorityHost" `
      "STORAGE_ENDPOINT_SUFFIX=$endpointSuffix"


az functionapp config appsettings set `
    --resource-group $rg `
    --name $functionSvcName `
    --settings `
        "AzureWebJobsFeatureFlags=EnableWorkerIndexing"

# Allow testing from the correct portal for the selected cloud
foreach ($origin in $portalOrigins) {
  az functionapp cors add `
    --resource-group $rg `
    --name $functionSvcName `
    --allowed-origins $origin
}

##################################
# Infrastructure deployment complete
##################################


