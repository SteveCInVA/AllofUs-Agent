<#
.SYNOPSIS
  Tears down the NIH All of Us greenfield deployment for a clean-slate rebuild.

.DESCRIPTION
  Deletes everything deploy_azure_infrastructure.ps1 creates:
    - the Azure resource group (VNet, private endpoints, private DNS zones, storage,
      Function App) and, by default, purges the soft-deleted Key Vault so its name is
      not left reserved;
    - the Entra app registrations (AllOfUs-Function-API / AllOfUs-Function-Client, and
      their service principals) and the three security groups - including any DUPLICATES
      left behind by earlier deploys (az ad *create allows duplicate display names).

  It does NOT touch the Power Platform custom connector or the Copilot Studio agent -
  delete those by hand in the environment you built them in.

  Set $cloud / $rg / $sfx below to match the deployment you are removing.

.EXAMPLE
  .\cleanup_agent.ps1
  # interactive: prints the target subscription/tenant and asks you to confirm.

.EXAMPLE
  .\cleanup_agent.ps1 -Force -PurgeDeletedApps
  # no prompt; also hard-purges soft-deleted AllOfUs-Function* app registrations.

.EXAMPLE
  .\cleanup_agent.ps1 -SkipAzure
  # remove only the Entra objects (leave the resource group in place).
#>
param(
  [switch]$Force,            # skip the interactive confirmation
  [switch]$SkipAzure,        # leave the Azure resource group in place
  [switch]$SkipEntra,        # leave the Entra apps / groups in place
  [switch]$KeepKeyVault,     # do NOT purge the soft-deleted Key Vault (name stays reserved)
  [switch]$PurgeDeletedApps  # also hard-purge soft-deleted AllOfUs-Function* app registrations
)

$ErrorActionPreference = "Continue"

##################################################
# Config - MUST match the deployment you are tearing down
# (mirror deploy_azure_infrastructure.ps1)
$cloud = "AzureCloud"          # or "AzureUSGovernment" for Azure Government / GCC
$rg    = "rg-allofus-demo21"
$sfx   = "aou0921"
$storageAcctName = "staallofus$sfx"
$functionSvcName = "func-allofus-$sfx"
$kvName          = "kv-allofus-$sfx"

# Entra objects created by the deploy
$apiAppName    = "AllOfUs-Function-API"
$clientAppName = "AllOfUs-Function-Client"
$groupNames    = @("AoU-Agent-Users", "AoU-DS-IHCC", "AoU-DS-CCDI")

# Cloud-specific endpoints derived from $cloud (do not edit unless adding a cloud).
switch ($cloud) {
  "AzureUSGovernment" { $loc = "usgovvirginia"; $graphHost = "https://graph.microsoft.us" }
  default             { $loc = "eastus";        $graphHost = "https://graph.microsoft.com" }
}
##################################################

function Remove-EntraAppsByName($name) {
  $ids = az ad app list --display-name $name --query "[].id" -o tsv 2>$null
  if (-not $ids) { Write-Host "  (none) no app registration named '$name'"; return }
  foreach ($id in $ids) {
    Write-Host "  deleting app registration '$name' ($id)"
    az ad app delete --id $id 2>$null
  }
}

function Remove-EntraGroupsByName($name) {
  $ids = az ad group list --display-name $name --query "[].id" -o tsv 2>$null
  if (-not $ids) { Write-Host "  (none) no group named '$name'"; return }
  foreach ($id in $ids) {
    Write-Host "  deleting group '$name' ($id)"
    az ad group delete --group $id 2>$null
  }
}

# ============================================================ preflight
$acct = az account show -o json 2>$null | ConvertFrom-Json
if (-not $acct) {
  Write-Error "Not logged in. Run 'az login' (and target the right cloud) first."
  exit 1
}
az cloud set --name $cloud | Out-Null

Write-Host ""
Write-Host "Target context:" -ForegroundColor Cyan
Write-Host "  Cloud        : $cloud"
Write-Host "  Subscription : $($acct.name)  ($($acct.id))"
Write-Host "  Tenant       : $($acct.tenantId)"
Write-Host "  Resource grp : $rg"
Write-Host "  Suffix (sfx) : $sfx  -> $storageAcctName / $functionSvcName / $kvName"

# ============================================================ confirmation
if (-not $Force) {
  Write-Host ""
  Write-Host "This will DELETE:" -ForegroundColor Yellow
  if (-not $SkipAzure) {
    Write-Host "  - Resource group '$rg' and ALL resources in it"
    if (-not $KeepKeyVault) { Write-Host "  - Purge the soft-deleted Key Vault '$kvName'" }
  }
  if (-not $SkipEntra) {
    Write-Host "  - Entra app registrations '$apiAppName', '$clientAppName' (all duplicates + their SPs)"
    Write-Host "  - Entra security groups: $($groupNames -join ', ') (all duplicates)"
    if ($PurgeDeletedApps) { Write-Host "  - Hard-purge soft-deleted 'AllOfUs-Function*' app registrations" }
  }
  Write-Host ""
  $ans = Read-Host "Type the resource group name '$rg' to confirm"
  if ($ans -ne $rg) { Write-Host "Confirmation did not match; aborting." -ForegroundColor Red; exit 1 }
}

# ============================================================ 1. Azure resources
if (-not $SkipAzure) {
  Write-Host ""
  Write-Host "== Azure ==" -ForegroundColor Cyan

  # Key Vault first: delete + purge so its (soft-deleted) name is not left reserved,
  # then the resource-group delete can run asynchronously.
  if (-not $KeepKeyVault) {
    az keyvault show --name $kvName -o none 2>$null
    if ($LASTEXITCODE -eq 0) {
      Write-Host "Deleting Key Vault '$kvName' (soft-delete)..."
      az keyvault delete --name $kvName -o none 2>$null
    }
    $deleted = az keyvault list-deleted --query "[?name=='$kvName'].name" -o tsv 2>$null
    if ($deleted) {
      Write-Host "Purging soft-deleted Key Vault '$kvName'..."
      az keyvault purge --name $kvName --location $loc -o none 2>$null
    } else {
      Write-Host "  (none) no soft-deleted Key Vault '$kvName' to purge"
    }
  }

  if ((az group exists --name $rg) -eq "true") {
    Write-Host "Deleting resource group '$rg' (runs in the background)..."
    az group delete --name $rg --yes --no-wait
  } else {
    Write-Host "  (none) resource group '$rg' not found"
  }
}

# ============================================================ 2. Entra objects
if (-not $SkipEntra) {
  Write-Host ""
  Write-Host "== Entra app registrations ==" -ForegroundColor Cyan
  Remove-EntraAppsByName $apiAppName
  Remove-EntraAppsByName $clientAppName

  Write-Host ""
  Write-Host "== Entra security groups ==" -ForegroundColor Cyan
  foreach ($g in $groupNames) { Remove-EntraGroupsByName $g }

  if ($PurgeDeletedApps) {
    Write-Host ""
    Write-Host "== Hard-purge soft-deleted app registrations ==" -ForegroundColor Cyan
    $delIds = az rest --method GET `
      --uri "$graphHost/v1.0/directory/deletedItems/microsoft.graph.application?`$select=id,displayName" `
      --query "value[?starts_with(displayName,'AllOfUs-Function')].id" -o tsv 2>$null
    if (-not $delIds) {
      Write-Host "  (none) no soft-deleted AllOfUs-Function* app registrations"
    } else {
      foreach ($d in $delIds) {
        Write-Host "  purging deleted app $d"
        az rest --method DELETE --uri "$graphHost/v1.0/directory/deletedItems/$d" 2>$null
      }
    }
  }
}

# ============================================================ summary
Write-Host ""
Write-Host "Cleanup dispatched." -ForegroundColor Green
if (-not $SkipAzure) {
  Write-Host "  - Resource-group deletion runs in the background. Check with:"
  Write-Host "      az group exists --name $rg"
}
Write-Host "  - NOT removed (manual): the Power Platform custom connector and the"
Write-Host "    Copilot Studio agent, if you built them."
Write-Host "  - Tip: for a fully clean redeploy, bump `$sfx` / `$rg` to fresh values in"
Write-Host "    deploy_azure_infrastructure.ps1 (avoids Key Vault / storage name reuse)."
