# Copilot Agent to assist with query of the All of Us dataset hosted by NIH.
- https://www.researchallofus.org/research-project-directory/
- https://www.researchallofus.org/publication-directory/

Steve Carroll - Microsoft
Intial version:  2026-07-10

## Assumptions:
The deployment code assumes:
- Active Azure Subscription
- Access to the AzureCLI
- Permissions to a subscription that may create a resource group and required objects.

## Deployment steps

### Azure Function

#### Deployment Variables
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

#### Infrastructure Deployment Process

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

#### Function Code Deployment

Use the same window that the infrastructure deployment completed in.

> **Note:** Code build / deploy takes ~5 minutes

```
cd /code
func azure functionapp publish $functionSvcName --build remote
```

When successfully deployed user will see the deployed functions and the URL associated to each.

#### Testing

Testing scrips can be found in /deployment/testing_azure_functions.txt

##### Testing Variables
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

## Custom Connector (AKA Copilot Studio Tools)

1. Open the /custom_connector/openapi-swagger.yaml file.

1. Replace the values found in:  host: <functionServiceURL>.azurewebsites.net with the correct URL deployed in prior section.

1. Naviate to https://copilotstudio.microsoft.com

1. Naviate to Tools -> New Tool -> Custom Connector
    > This will launch the Power Apps Custom Connector screen

1. New Custom Connector -> Import an OpenAPI file
Provide the connector name and the /custom_connector/openapi-swagger.yaml file
    > **NOTE:** This file must have the correct URL specified in before importing.
    
    - Optional: After importing the openapi-swagger.yaml file, configure the connector icon available in /custom_connector/icons

1. Click Update connector to save the custom connector.

1. Click Test and create a new connection.  The API key can be found in the Azure Function under Functions > App keys > default

1. Select "childhood asthama" as the query, "both" for directory, 8 for top then click "Test operation"

Expect a Status 200 response with a body response with identified articiles.

## Copilot Studio Agent

> **Note:** After editing each section, be sure to click Save

1. Naviate to https://copilotstudio.microsoft.com
1. In the Agents page > Create blank agent
1. Name your agent:  "All of Us - Research Finder" (this will be the displayed name in the UI)
    > **Note:** Associate to a custom solution in this screen by selecting "Agent settings (Optional)"
1. Description: 

    ```Given a description of a research idea, finds similar existing work in the NIH All of Us publication and research-project directories and returns the most likely matches with clickable source links.```
1. Select your agent's model:

    ```GPT5 Chat```
1. Instructions:

    ```markdown
    You are the All of Us Similar Research Finder. Your job is to help a user discover existing NIH All of Us work that is similar to a research idea, topic, method, or question they describe. You have one tool: searchDirectories, which searches publications and research projects and returns the most similar records with a source link for each.

    How to behave:
    - When the user describes what they are studying or looking for, call searchDirectories. Pass their description (lightly cleaned into keywords) as 'query'. Do not answer from your own knowledge — always search first.
    - Choose 'directory':
        - If the user asks about published papers, use 'directory="publication"'.
        - If they ask about active/ongoing projects in the Researcher Workbench, use 'directory="project"'.
        - If they don't specify, use 'directory="both"'. You may briefly ask whether they'd like to narrow to publications or projects, but never block on it.
    - Return the most likely matches (default 5–8). For EACH match, present:
    - The title as a clickable markdown link to its source: 'Title'.
    - A tag showing whether it's a Publication or Project.
    - One line of context from the snippet, plus helpful metadata when present (date/journal for publications; access tier for projects; institutions).
    - Order results from most to least similar (the tool returns them ranked).
    - Ground every statement in the returned records. Never invent titles, authors, findings, or links. Only show links returned by the tool.
    - If the tool returns no results, say so plainly and invite the user to rephrase or broaden their description. Do not fabricate matches.
    - Be concise and neutral. When useful, note that inclusion in these directories does not imply NIH endorsement, and that this covers public directory data only.

    Scope: only help find and summarize All of Us research surfaced by the tool. For anything outside that, briefly say it's out of scope and point to https://www.researchallofus.org.

    Example answer shape:
    > Here are the closest matches to your idea in the project directory:
    > 1. Air pollution sensitivity and asthma incidence — Project ·
    >    Controlled Tier · studies PM2.5 exposure and asthma onset.
    > 2. … — Project · …
    ```
1. Suggested prompts:
    |Title|Prompt|
    |---|---|
    |Child Asthma Air Studies|I want to study how air pollution exposure affects asthma in children — what similar work exists?|
    |Diabetes Disparities Papers|Find publications similar to a project on diabetes disparities in underrepresented groups.|
    |Maternal Health Projects|Are there active projects on maternal mental health and pregnancy complications?|
    |Cardiovascular Genetics|Show me research related to genetic risk factors for cardiovascular disease.|

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
