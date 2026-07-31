# Copilot Agent to assist with query of the All of Us dataset hosted by NIH.
- https://www.researchallofus.org/research-project-directory/
- https://www.researchallofus.org/publication-directory/

## Assumptions:
The deployment code assumes:
- Active Azure Subscription
- Access to the AzureCLI
- Permissions to a subscription that may create a resource group and required objects.

## Deployment steps

### Deployment Variables
The following varaiables are defined in the top of the deploy_azure_infrastructure.txt file.  They represent the following configurations:

|Variable|Default Value|Purpose|
|-----|-----|-----|
|$rg|"rg-allofus-demo01"|Resource group name|
|$loc|"eastus"|Deployment Region|
|$sfx|"aou1234"|Suffix to apply to resources|
|$storageAcctName|"staallofus$sfx"|Resource name of storage account (includes suffix)|
|$functionSvcName|"func-allofus-$sfx"|Resource name of Azure Function (includes suffix)|
|$vnetName|"vnet-allofus"|Resource name of vNet|
|$vnetAddressSpace|"192.168.0.0/24"|CIDR of vNet|
|$funcSubnetName|"pe-functions"|Subnet name used for Azure Function|
|$funcSubnetAddrSpace|"192.168.0.0/25"|Subnet CIDR for Azure Function|
|$peSubnetStorage|"pe-storage"|Subnet name used for storage acct. private endpoints|
|$peSubnetStorageAddrSpace|"192.168.0.128/25"|Subnet CIDR for storage acct.|

### Infrastructure Deployment Process

The code used to deploy the infrastructure can be found in /deployment/deploy_azure_infrastructure.txt

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

### Function Code Deployment

Use the same window that the infrastructure deployment completed in.

> **Note:** Code build / deploy takes ~5 minutes

```
cd /code
func azure functionapp publish $functionSvcName --build remote
```

When successfully deployed user will see the deployed functions and the URL associated to each.

## Testing

Testing scrips can be found in /deployment/testing_azure_functions.txt

### Testing Variables
The following parameters are defined in the /deployment/testing_azure_functions.txt file

|Variable|Default Value|Purpose|
|-----|-----|-----|
|$rg|"rg-allofus-demo01"|Deployment resource group name|
|$sfx|"aou1234"|Suffix defined in deployment steps|
|$app|"func-allofus-$sfx"|Not needed unless changed in deployment|
|$uri|"https://$app.azurewebsites.net/api"|Not needed unless operating in other than commericial Azure subscription|

### Testing functions
- Health - evaluates response from the Health endpoint and returns number of records in cache
- Refresh - causes the cache to become invalidated and forces a refresh
- Search - Executes a basic query and displays results


## Known Issues
- ~~Problem manually running the refresh function.  Throws an error 500.  I believe its due to a permission issue saving back to storage.~~