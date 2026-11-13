# Azure AI Search Index Structure

The initial index definition is [index.json](../index.json), with index name
`allofus-content`. It uses one shared index with one document per publication,
project, IHCC cohort, or CCDI study/namespace. Common fields have consistent types;
source-specific fields are optional.

This is the structure used by application ingestion, search, and health.
The deployment script creates the index; the HTTP refresh, daily timer, or
command-line import loads source documents. Corpus data is no longer written
to Azure Blob Storage.

## Deployment

The [deployment script](../deployment/deploy_azure_infrastructure.ps1) validates
that [index.json](../index.json) exists and contains an index name before invoking
Azure. After provisioning Search and its private endpoint, it grants the current
Azure CLI user or service principal `Search Service Contributor` at Search-service
scope, sends the definition using an authenticated create-or-update request, and
verifies the index name with a read-back request.

Search public network access is temporarily enabled so deployment can create
the index without private VNet connectivity. Entra authentication remains
required and API keys remain disabled. The deployer needs permission to assign roles
and resolve its Entra object ID. Index requests use the selected cloud's Search
audience and REST API version `2024-07-01`. Failed writes are logged and retried up
to 20 times with 30-second waits; exhausted retries or failed verification stop
deployment. Public access remains enabled until manually disabled, including
after a failed deployment. The script warns before provisioning and at completion
and prints a command populated with the deployment's resource names:

```powershell
az search service update --resource-group <resource-group> --name <search-service> --public-network-access disabled
```

**Disable public access after deployment is complete**, or after troubleshooting
a failure. Then verify Function search/health over the provisioned private
endpoint. Subsequent direct index administration and imports require private
connectivity and private DNS resolution.

The infrastructure operation does not delete an existing index or load documents. Compatible
schema updates can be reapplied; incompatible changes require a separate migration
and are not handled by deleting and recreating the index.

## Import and refresh

The [refresh process](../code/refresh_job.py) is shared by:

- `POST /api/refresh`, with the existing admin-role requirement.
- The daily `03:00 UTC` timer.
- `python build_index.py`, run from the `code` directory.
  `--public` limits the import to sources currently classified as public.

The command-line importer writes to Search, not to local files. It does not
automatically read `local.settings.json`. Set the following environment variables
before running it:

| Setting | Purpose |
|---|---|
| `AZURE_SEARCH_ENDPOINT` | Required Search endpoint |
| `AZURE_SEARCH_INDEX_NAME` | Index name; defaults to `allofus-content`; set by deployment from the definition |
| `AZURE_SEARCH_AUDIENCE` | Defaults to `https://search.azure.com`; use `https://search.azure.us` for Government |
| `AZURE_AUTHORITY_HOST` | Entra authority for the selected cloud, supplied by deployment |
| `DATASET_CLASSIFICATION` | Existing classification/group JSON; use the same policy as the deployed API |

Authentication uses `DefaultAzureCredential`: the Function uses managed identity,
while local imports can use an authenticated Azure CLI identity. Query operations
need Search Index Data Reader; imports need Search Index Data Contributor as well
as read access to enumerate existing records. Infrastructure deployment's
deployer Search Service Contributor role manages index definitions but does not
itself grant document read/write access. Grant the appropriate data roles to a
local importer at Search-service scope. Private connectivity is required.

For each enabled source, the importer:

1. Fetches and normalizes live source metadata.
2. Validates classification, required record values, numeric types, and duplicate
   keys before writing. It maps only the 25 declared schema fields.
3. Enumerates the source's existing document IDs and uploads complete replacement
   documents in batches of at most 1,000 documents and 15 MiB of estimated JSON
   payload, leaving room below Search's 16 MB request limit.
4. Checks every document acknowledgement. Only after every upload succeeds does
   it delete previously indexed IDs absent from the new source response.

Uploads replace complete documents, so removed optional values do not survive a
refresh. A fetch or normalization exception does not write or delete that source:
the summary reports `carried` with its existing count, or `failed` if none exists.
Other sources still refresh. Successful empty or partial responses are
authoritative; an empty response deletes that source's old records. CCDI node
errors can produce a partial successful source response.

There are no normalized-data cache blobs, pickle uploads, or manifest writes.
Old Blob files are not read, migrated, or deleted automatically. Azure Functions
still uses its storage account for host/timer infrastructure.

Refresh keeps the existing response keys: `records`, `datasets`, `sources`, and
`seconds`. HTTP refresh adds `status: refreshed` for an all-live run; unavailable
sources produce `status: degraded` and `warnings` with HTTP 200. Validation,
Search request, and individual upload/delete errors propagate as HTTP 500 and are
logged; the timer logs failures. The SDK handles request-level retries, but a
failed per-document acknowledgement fails the refresh rather than being ignored.

**Operational limits:** writes are not an atomic dataset snapshot. A failed
later upload batch can leave some records updated, but no stale IDs are deleted
after an upload failure. Delete failures can leave some stale records. Re-run
refresh after correcting the error; stable keys make uploads idempotent.
Run one refresh at a time: no distributed refresh lock is implemented.
Index changes and query counts are eventually consistent. HTTP refresh remains
synchronous and can exceed Azure's HTTP response timeout for slow sources; check
logs before starting another run.

## Search and health interfaces

`GET/POST /api/search` retains `query`, `directory`/`source`, and `top`, including
the plural source aliases, default limit of 8, and clamp to 1-25. The application
tokenizes the description into plain search terms and uses Search's simple
full-text query with any-term matching. Ranking now comes from Azure AI Search,
not the old in-process BM25 engine; relevance scores/order can differ.

The `directory` input selects the shared index's `source` field, not a separate
index. The handler intersects this selection with the caller's entitled source
keys before issuing the Search request:

| `directory` input | Indexed source selection |
|---|---|
| `publication`, `publications` | `source eq 'publication'` |
| `project`, `projects` | `source eq 'project'` |
| `ihcc` | `source eq 'ihcc'` |
| `ccdi` | `source eq 'ccdi'` |
| `all`, `both`, or omitted | All enabled sources the caller may access |
| Unrecognized value | Existing fallback to `both` |

These source clauses are combined with indexed security checks when authorization
is enforced. Selecting a source the caller cannot access returns no matches; it
never broadens access. The legacy `source` request parameter remains an alias for
`directory`. GET query-string and POST JSON inputs retain the same response
shape and `directory` echo; plural aliases are mapped internally, not rewritten
in the response.

Results retain their previous API shapes: `id` is the original source-record ID,
`institutions` is a comma-separated display string, `citations` is a string,
and `score` is numeric. Full `content`, indexed permission metadata, and internal
ingestion timestamps are not returned in search results. The index's actual key
is `v1_` followed by unpadded URL-safe Base64 of `source:record_id`, preserving
stable, collision-free source identity and allowing otherwise invalid key
characters. Keys longer than Search's 1,024-character limit are rejected.

`GET /api/health` remains anonymous and returns `status`, `total`, and `datasets`,
including restricted dataset metadata but never its content. It queries Search
for every enabled source, including empty ones, and uses current configuration for
classification. Dataset status is `indexed` or `empty`, not the outcome of the
last refresh; `updated_at` is the latest document's `ingested_at`, or `null` for
an empty dataset. Failed source fetches do not advance that timestamp.
Search errors return HTTP 503 rather than an empty success.
Health does not probe upstream sources or certify a complete successful refresh.
Counts come from Search's total match count for each `source`, not the number
of documents returned in a page: only one document's ingestion timestamp is
retrieved per source. Only configured, enabled sources contribute to `total`.
The anonymous response never includes document IDs, titles, snippets, full
`content`, or `allowed_group_ids`; use the authorized search endpoint for records.

## Field attributes

The tables below match the attributes in [index.json](../index.json):

- **S**: searchable through full-text queries.
- **F**: filterable through exact-match or range filters, as appropriate to the type.
- **A**: facetable for grouping result counts.
- **O**: sortable.
- **Key**: the unique document key.
- **None**: none of these attributes is enabled.

Attributes not listed are disabled. All fields are retrievable except
`allowed_group_ids`. Collection fields are not sortable.

## Common fields

| Field | Azure Search type | Attributes | Purpose |
|---|---|---|---|
| `id` | `Edm.String` | Key, F | Stable, globally unique, Search-safe identifier |
| `source` | `Edm.String` | F, A | `publication`, `project`, `ihcc`, or `ccdi` |
| `record_type` | `Edm.String` | F, A | `Publication`, `Project`, `Cohort`, or `Dataset` |
| `title` | `Edm.String` | S, O | Display title |
| `url` | `Edm.String` | None | Link to the source |
| `snippet` | `Edm.String` | S | Short display summary |
| `content` | `Edm.String` | S | Full normalized searchable text, mapped from `_body` |
| `authors` | `Collection(Edm.String)` | S | Authors or project team members |
| `institutions` | `Collection(Edm.String)` | S, F, A | Normalized institution names |
| `focus` | `Collection(Edm.String)` | S, F, A | Research focus categories |
| `security_type` | `Edm.String` | F, A | Required classification: `public` or `restricted` |
| `allowed_group_ids` | `Collection(Edm.String)` | F | Entra groups permitted to access restricted records; not retrievable |
| `ingested_at` | `Edm.DateTimeOffset` | F, O | Last successful ingestion time |

Ingestion requires a non-empty source record ID, title, and `_body`, plus a source
value matching the source being imported. It supplies `record_type` from the
source when absent, maps `_body` to `content`, and stamps security metadata and
`ingested_at`. A URL or snippet may be empty when unavailable. These requirements
are enforced by ingestion code, not by the index definition alone.

## Source-specific fields

These are top-level fields, populated only where applicable.

| Field | Azure Search type | Attributes | Applies to |
|---|---|---|---|
| `date` | `Edm.String` | None | Publication date as supplied, including partial dates |
| `publication_year` | `Edm.Int32` | F, A, O | Publications |
| `journal` | `Edm.String` | S, F, A | Publications |
| `citations` | `Edm.Int64` | F, O | Publications |
| `access_tier` | `Edm.String` | F, A | Projects |
| `countries` | `Collection(Edm.String)` | S, F, A | IHCC cohorts |
| `pi_lead` | `Edm.String` | S | IHCC cohorts |
| `enrollment` | `Edm.Int64` | F, O | IHCC cohorts |
| `diseases` | `Collection(Edm.String)` | S, F, A | IHCC cohorts |
| `data_types` | `Collection(Edm.String)` | S, F, A | IHCC cohorts |
| `organization` | `Edm.String` | S, F, A | CCDI |
| `study_id` | `Edm.String` | S, F | CCDI |

Use `null` for missing scalar values and `[]` for missing collections. Do not send
empty strings for numeric fields or invent a day/month for a partial publication
date.

## Mapping source records

- [Shared source contract](../code/source_base.py): map `_body` to persistent
  `content` and encode identifiers as described above.
- [All of Us sources](../code/source_allofus.py): retain publication and project
  metadata; convert citation counts to integers and derive `publication_year`
  when available. Extract institution collections from raw source data rather
  than splitting comma-separated display strings, since names can contain commas.
- [IHCC source](../code/source_ihcc.py): retain cohort metadata and convert missing
  enrollment to `null`.
- [CCDI source](../code/source_ccdi.py): retain study/namespace metadata only, not
  subject, sample, or file records. The URL can point to node documentation rather
  than an individual study page.
- [Source configuration](../code/sources.py): map `Source.classification` to
  `security_type`. For restricted sources, map `Source.entitlement_group_id` to a
  one-element `allowed_group_ids` collection; public records use `[]`.
- Set `ingested_at` to the successful ingestion timestamp in UTC.

## Security filtering

`security_type` describes application visibility, not access to underlying
research data. A project's `access_tier` must not determine who can see its
metadata.

Preserve the [application authorization model](auth-design.md):

- `public`: visible to callers with the base entitlement, not necessarily
  anonymous users.
- `restricted`: additionally requires membership in at least one group listed in
  `allowed_group_ids`.

After authenticating the caller and validating the base entitlement, the backend
must construct a filter from validated group claims:

```text
security_type eq 'public'
or (
  security_type eq 'restricted'
  and allowed_group_ids/any(g: search.in(g, '<group-id-1>,<group-id-2>', ','))
)
```

For callers with no dataset groups, use only `security_type eq 'public'`.
Combine any user-requested source or metadata filters with the authorization
filter using `and`, with each expression parenthesized.

The ingestion path rejects unknown classifications and restricted sources without
an entitlement group. The application uses the existing caller-entitlement mapping
to select source keys, then builds per-source clauses requiring indexed
`security_type` and, for restricted sources, the configured group in
`allowed_group_ids`. An explicit directory filter can only narrow those clauses.
Caller-supplied groups are not accepted. With `AUTH_ENFORCED=false`, claims and
indexed-permission checks are bypassed for development; enabled-source filtering
still applies. Non-retrievable group IDs do not themselves enforce access.
See Microsoft's [security-filter pattern](https://learn.microsoft.com/azure/search/search-security-trimming-for-azure-search).

Changes to classification or entitlement configuration require refresh of affected
documents. Query filters require the indexed values to match current
configuration, so stale classification/group values exclude records until they
are reimported. Classification configuration still defaults omitted entries to
public and ignores malformed JSON with a warning; validate the policy before
refreshing or enabling access. See [authorization configuration](auth-design.md#6-configuration-source-of-truth).

## Semantic ranking and future vector search

The `default` semantic configuration uses:

- Title: `title`.
- Content, in priority order: `content`, `snippet`.
- Keywords, in priority order: `focus`, `diseases`, `data_types`, `institutions`,
  `organization`.

Semantic ranking requires service support and a query requesting semantic ranking;
the current API uses lexical full-text queries, not semantic ranking.

The initial index has no vector fields or vector search configuration. Add those
after selecting an embedding model, with dimensions matching that model's output.
Start with record-level documents for the current compact metadata. If longer
full-text content later requires chunking, copy security fields onto every chunk.
