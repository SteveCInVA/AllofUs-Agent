# Testing Azure Functions:

##################################
# Test function after deployment
##################################

$cloud = "AzureCloud"          # or "AzureUSGovernment" for Azure Government / GCC
$rg  = "rg-allofus-demo21"
$sfx = "aou0921"
$app = "func-allofus-$sfx"
$apiAppId = "<API-APP-ID>"     # AllOfUs-Function-API appId from the deployment output

# Function hostname suffix per cloud (.azurewebsites.net commercial / .us government)
$functionHostSuffix = if ($cloud -eq "AzureUSGovernment") { "azurewebsites.us" } else { "azurewebsites.net" }
$uri = "https://$app.$functionHostSuffix/api"

# Target the correct Azure cloud before running az commands below
az cloud set --name $cloud

##########################################
# Auth: acquire a Microsoft Entra bearer token for the API app's scope.
# The signed-in user must be in AoU-Agent-Users (base) for /search, and hold the
# Agent.Admin role for /refresh. /api/health is anonymous (no header needed).
##########################################

$token = az account get-access-token --resource "api://$apiAppId" --query accessToken -o tsv
$headers = @{
    "Authorization" = "Bearer $token"
}

##########################################
# Health (anonymous) - enumerates every dataset with name + record count
##########################################

$health_uri = "$uri/health"

$response = Invoke-RestMethod `
  -Method Get `
  -Uri "$health_uri"

$response | ConvertTo-Json -Depth 10
clear-variable -Name response

##########################################
# Refresh (requires Agent.Admin)
##########################################

$refresh_uri = "$uri/refresh"

$response = Invoke-RestMethod `
    -Method Post `
    -Uri "$refresh_uri" `
    -Headers $headers

$response | ConvertTo-Json -Depth 10
clear-variable -Name response

##########################################
# Search (requires AoU-Agent-Users; results limited to the caller's entitled datasets)
##########################################

$search_uri = "$uri/search"

$query = [System.Uri]::EscapeDataString(
    "childhood asthma air pollution"
)

$response = Invoke-RestMethod `
    -Method Get `
    -Uri "$search_uri`?query=$query&directory=all&top=5" `
    -Headers $headers

$response | ConvertTo-Json -Depth 10
clear-variable -Name response
