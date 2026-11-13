Describe "Azure AI Search infrastructure (offline)" {
  BeforeAll {
    $deploymentPath = Join-Path $PSScriptRoot "..\deployment\deploy_azure_infrastructure.ps1"
    $deploymentSource = Get-Content -Raw $deploymentPath
    $configuration = $deploymentSource.Substring(0, $deploymentSource.IndexOf("# Target the correct Azure cloud"))
    $searchStart = $deploymentSource.IndexOf("# ============================================================ 5a.")
    $searchEnd = $deploymentSource.IndexOf('write-output "Setting up Key Vault')
    $searchSetup = [scriptblock]::Create($deploymentSource.Substring($searchStart, $searchEnd - $searchStart))

    function az {
      $script:commands.Add(@($args))
      $global:LASTEXITCODE = 0
      if ($script:commands.Count -eq $script:failAt) {
        $global:LASTEXITCODE = 1
        return
      }
      if (($args[0..2] -join " ") -eq "search service create" -and -not $script:emptySearchId) {
        return $script:searchResourceId
      }
    }
  }

  BeforeEach {
    $script:commands = New-Object System.Collections.Generic.List[object]
    $script:failAt = 0
    $script:emptySearchId = $false
    $sami = "function-principal-id"
    $script:searchResourceId = "/subscriptions/test/resourceGroups/test/providers/Microsoft.Search/searchServices/test"
  }

  It "configures private identity-only Search in <Cloud>" -TestCases @(
    @{ Cloud = "AzureCloud"; Suffix = "search.windows.net"; Audience = "https://search.azure.com" },
    @{ Cloud = "AzureUSGovernment"; Suffix = "search.azure.us"; Audience = "https://search.azure.us" }
  ) {
    param($Cloud, $Suffix, $Audience)
    . ([scriptblock]::Create($configuration.Replace('$cloud = "AzureCloud"', ('$cloud = "' + $Cloud + '"'))))
    . $searchSetup

    $create = $script:commands[0] -join " "
    $create | Should -Match "--sku basic --replica-count 1 --partition-count 1"
    $create | Should -Match "--semantic-search free --disable-local-auth true --public-network-access disabled"
    foreach ($role in @("Search Index Data Reader", "Search Index Data Contributor", "Search Service Contributor")) {
      $assignment = @($script:commands | Where-Object { $_ -contains $role })
      $assignment.Count | Should -Be 1
      ($assignment[0] -join " ") | Should -Match "--assignee-object-id function-principal-id"
      ($assignment[0] -join " ") | Should -Match ([regex]::Escape("--scope $script:searchResourceId"))
    }
    $dns = @($script:commands | Where-Object { ($_[0..3] -join " ") -eq "network private-dns zone create" })
    ($dns[0] -join " ") | Should -Match ([regex]::Escape("--name privatelink.$Suffix"))
    $endpoint = @($script:commands | Where-Object { ($_[0..2] -join " ") -eq "network private-endpoint create" })
    ($endpoint[0] -join " ") | Should -Match "--subnet pe-storage"
    ($endpoint[0] -join " ") | Should -Match ([regex]::Escape("--private-connection-resource-id $script:searchResourceId --group-id searchService"))
    $link = @($script:commands | Where-Object { ($_[0..4] -join " ") -eq "network private-dns link vnet create" })
    ($link[0] -join " ") | Should -Match ([regex]::Escape("--zone-name privatelink.$Suffix"))
    ($link[0] -join " ") | Should -Match "--virtual-network vnet-allofus --registration-enabled false"
    $zoneGroup = @($script:commands | Where-Object { ($_[0..3] -join " ") -eq "network private-endpoint dns-zone-group create" })
    ($zoneGroup[0] -join " ") | Should -Match ([regex]::Escape("--private-dns-zone privatelink.$Suffix --zone-name searchService"))
    $settings = $script:commands[$script:commands.Count - 1]
    ($settings -contains "AZURE_SEARCH_ENDPOINT=https://$searchSvcName.$Suffix") | Should -Be $true
    ($settings -contains "AZURE_SEARCH_AUDIENCE=$Audience") | Should -Be $true
  }

  It "stops immediately when Search operation <Operation> fails" -TestCases @(
    1..10 | ForEach-Object { @{ Operation = $_ } }
  ) {
    param($Operation)
    . ([scriptblock]::Create($configuration))
    $script:failAt = $Operation
    { . $searchSetup } | Should -Throw
    $script:commands.Count | Should -Be $Operation
  }

  It "rejects a missing Search resource ID before granting access" {
    . ([scriptblock]::Create($configuration))
    $script:emptySearchId = $true
    { . $searchSetup } | Should -Throw
    $script:commands.Count | Should -Be 1
  }

  It "rejects a missing Function identity before creating Search" {
    . ([scriptblock]::Create($configuration))
    $sami = ""
    { . $searchSetup } | Should -Throw
    $script:commands.Count | Should -Be 0
  }
}
