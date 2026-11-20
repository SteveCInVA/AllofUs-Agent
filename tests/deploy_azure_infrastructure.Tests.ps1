Describe "Azure AI Search infrastructure (offline)" {
  BeforeAll {
    $deploymentPath = Join-Path $PSScriptRoot "..\deployment\deploy_azure_infrastructure.ps1"
    $deploymentSource = Get-Content -Raw $deploymentPath
    $configuration = $deploymentSource.Substring(0, $deploymentSource.IndexOf("# Target the correct Azure cloud"))
    $indexPathAssignment = '$searchIndexFile = "' + (Join-Path $PSScriptRoot "..\index.json") + '"'
    $configuration = $configuration.Replace(
      '$searchIndexFile = Join-Path (Split-Path $PSScriptRoot -Parent) "index.json"',
      $indexPathAssignment
    )
    $searchStart = $deploymentSource.IndexOf("# ============================================================ 5a.")
    $searchEnd = $deploymentSource.IndexOf("# ============================================================ 5b.")
    $searchSetup = [scriptblock]::Create($deploymentSource.Substring($searchStart, $searchEnd - $searchStart))
    $indexEnd = $deploymentSource.IndexOf('write-output "Setting up Key Vault')
    $indexSetup = [scriptblock]::Create($deploymentSource.Substring($searchEnd, $indexEnd - $searchEnd))
    $summaryStart = $deploymentSource.IndexOf("# ============================================================ Summary")
    $summarySetup = [scriptblock]::Create($deploymentSource.Substring($summaryStart))

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
      if (($args[0..1] -join " ") -eq "account show") {
        return (@{ type = $script:callerType; name = "deployer-app-id" } | ConvertTo-Json -Compress)
      }
      if (($args[0..2] -join " ") -in @("ad signed-in-user show", "ad sp show")) {
        return "deployer-object-id"
      }
      if (($args[0..2] -join " ") -eq "rest --method put" -and $script:putFailures -gt 0) {
        $script:putFailures--
        $global:LASTEXITCODE = 1
        return "Search endpoint not ready"
      }
      if (($args[0..2] -join " ") -eq "rest --method get") {
        return $script:verifiedName
      }
    }
  }

  BeforeEach {
    $script:commands = New-Object System.Collections.Generic.List[object]
    $script:failAt = 0
    $script:emptySearchId = $false
    $script:callerType = "user"
    $script:putFailures = 0
    $script:verifiedName = "allofus-content"
    Mock Start-Sleep {}
    $sami = "function-principal-id"
    $script:searchResourceId = "/subscriptions/test/resourceGroups/test/providers/Microsoft.Search/searchServices/test"
  }

  It "allows temporary public Search access with Entra auth and private endpoints in <Cloud>" -TestCases @(
    @{ Cloud = "AzureCloud"; Suffix = "search.windows.net"; Audience = "https://search.azure.com" },
    @{ Cloud = "AzureUSGovernment"; Suffix = "search.azure.us"; Audience = "https://search.azure.us" }
  ) {
    param($Cloud, $Suffix, $Audience)
    . ([scriptblock]::Create($configuration.Replace('$cloud = "AzureCloud"', ('$cloud = "' + $Cloud + '"'))))
    . $searchSetup

    $create = $script:commands[0] -join " "
    $create | Should -Match "--sku basic --replica-count 1 --partition-count 1"
    $create | Should -Match "--semantic-search free --disable-local-auth true --public-network-access enabled"
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
    ($settings -contains "AZURE_SEARCH_INDEX_NAME=allofus-content") | Should -Be $true
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

  It "creates and verifies the JSON index in <Cloud> as <CallerType>" -TestCases @(
    @{ Cloud = "AzureCloud"; CallerType = "user"; PrincipalType = "User"; Audience = "https://search.azure.com"; Suffix = "search.windows.net" },
    @{ Cloud = "AzureUSGovernment"; CallerType = "servicePrincipal"; PrincipalType = "ServicePrincipal"; Audience = "https://search.azure.us"; Suffix = "search.azure.us" }
  ) {
    param($Cloud, $CallerType, $PrincipalType, $Audience, $Suffix)
    . ([scriptblock]::Create($configuration.Replace('$cloud = "AzureCloud"', ('$cloud = "' + $Cloud + '"'))))
    . $searchSetup
    $script:callerType = $CallerType
    . $indexSetup

    $assignment = @($script:commands | Where-Object { $_ -contains "deployer-object-id" -and $_ -contains "Search Service Contributor" })
    $assignment.Count | Should -Be 1
    ($assignment[0] -join " ") | Should -Match "--assignee-principal-type $PrincipalType"
    ($assignment[0] -join " ") | Should -Match ([regex]::Escape("--scope $script:searchResourceId"))
    $requests = @($script:commands | Where-Object { $_[0] -eq "rest" })
    $requests.Count | Should -Be 2
    $expectedUri = "https://$searchSvcName.$Suffix/indexes/allofus-content?api-version=2024-07-01"
    $requests[0] | Should -Contain $expectedUri
    $requests[0] | Should -Contain $Audience
    $requests[0] | Should -Contain "@$searchIndexFile"
    ($requests[0] -join " ") | Should -Match "--method put"
    ($requests[1] -join " ") | Should -Match "--method get"
    Should -Invoke Start-Sleep -Times 0 -Exactly
  }

  It "retries index creation after a transient failure" {
    . ([scriptblock]::Create($configuration))
    . $searchSetup
    $script:putFailures = 1
    . $indexSetup
    @($script:commands | Where-Object { ($_[0..2] -join " ") -eq "rest --method put" }).Count | Should -Be 2
    Should -Invoke Start-Sleep -Times 1 -Exactly -ParameterFilter { $Seconds -eq 30 }
  }

  It "fails after bounded index retries without reporting verification" {
    . ([scriptblock]::Create($configuration))
    . $searchSetup
    $script:putFailures = 20
    { . $indexSetup } | Should -Throw "*Disable public access after troubleshooting*"
    @($script:commands | Where-Object { ($_[0..2] -join " ") -eq "rest --method put" }).Count | Should -Be 20
    @($script:commands | Where-Object { ($_[0..2] -join " ") -eq "rest --method get" }).Count | Should -Be 0
    Should -Invoke Start-Sleep -Times 19 -Exactly
  }

  It "stops before index requests when deployer operation <Operation> fails" -TestCases @(
    1..3 | ForEach-Object { @{ Operation = $_ } }
  ) {
    param($Operation)
    . ([scriptblock]::Create($configuration))
    . $searchSetup
    $script:failAt = $script:commands.Count + $Operation
    { . $indexSetup } | Should -Throw
    $script:commands.Count | Should -Be $script:failAt
  }

  It "rejects a mismatched index verification response" {
    . ([scriptblock]::Create($configuration))
    . $searchSetup
    $script:verifiedName = "wrong-index"
    { . $indexSetup } | Should -Throw "*Could not verify Search index*"
  }

  It "rejects a missing index file before invoking Azure" {
    $missingPath = Join-Path $TestDrive "missing-index.json"
    $invalidConfiguration = $configuration.Replace(
      $indexPathAssignment,
      ('$searchIndexFile = "' + $missingPath + '"')
    )
    { . ([scriptblock]::Create($invalidConfiguration)) } | Should -Throw "*Search index definition not found*"
    $script:commands.Count | Should -Be 0
  }

  It "warns the operator and prints the disable command after deployment in <Cloud>" -TestCases @(
    @{ Cloud = "AzureCloud" },
    @{ Cloud = "AzureUSGovernment" }
  ) {
    param($Cloud)
    . ([scriptblock]::Create($configuration.Replace('$cloud = "AzureCloud"', ('$cloud = "' + $Cloud + '"'))))
    $kvSecretStored = $true
    Mock Write-Warning {}
    $output = @(. $summarySetup)
    Should -Invoke Write-Warning -Times 1 -Exactly -ParameterFilter {
      $Message -like "*public network access is still enabled*Disable it after deployment*"
    }
    ($output -join "`n") | Should -Match ([regex]::Escape(
      "az search service update --resource-group $rg --name $searchSvcName --public-network-access disabled"))
    $script:commands.Count | Should -Be 0
  }
}
