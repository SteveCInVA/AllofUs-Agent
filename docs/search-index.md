# Azure AI Search Index Structure

The initial index definition is [index.json](../index.json), with index name
`allofus-content`. It uses one shared index with one document per publication,
project, IHCC cohort, or CCDI study/namespace. Common fields have consistent types;
source-specific fields are optional.

This is the target Azure AI Search structure, not a description of the current
retrieval implementation. Application retrieval remains Blob-backed BM25.
Creating the index does not migrate ingestion, load documents, or enable
application security filtering.

## Deployment

The [deployment script](../deployment/deploy_azure_infrastructure.ps1) validates
that [index.json](../index.json) exists and contains an index name before invoking
Azure. After provisioning Search and its private endpoint, it grants the current
Azure CLI user or service principal `Search Service Contributor` at Search-service
scope, sends the definition using an authenticated create-or-update request, and
verifies the index name with a read-back request.

Run the script from a host with private VNet connectivity and private DNS
resolution for the Search endpoint. The deployer needs permission to assign roles
and resolve its Entra object ID. Index requests use the selected cloud's Search
audience and REST API version `2024-07-01`. Failed writes are logged and retried up
to 20 times with 30-second waits; exhausted retries or failed verification stop
deployment. Public access and API keys remain disabled.

The operation does not delete an existing index or load documents. Compatible
schema updates can be reapplied; incompatible changes require a separate migration
and are not handled by deleting and recreating the index.

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
| `snippet` | `Edm.String` | None | Short display summary |
| `content` | `Edm.String` | S | Full normalized searchable text, mapped from `_body` |
| `authors` | `Collection(Edm.String)` | S | Authors or project team members |
| `institutions` | `Collection(Edm.String)` | S, F, A | Normalized institution names |
| `focus` | `Collection(Edm.String)` | S, F, A | Research focus categories |
| `security_type` | `Edm.String` | F, A | Required classification: `public` or `restricted` |
| `allowed_group_ids` | `Collection(Edm.String)` | F | Entra groups permitted to access restricted records; not retrievable |
| `ingested_at` | `Edm.DateTimeOffset` | F, O | Last successful ingestion time |

The ingestion contract should require `id`, `source`, `record_type`, `title`,
`content`, `security_type`, and `ingested_at`. A URL or snippet may be empty when
unavailable. Apart from the document key, these requirements and the classification
values must be validated by ingestion code; the index definition does not enforce
them.

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
  `content` before the [BM25 artifact builder](../code/search_core.py) drops
  underscore-prefixed fields. Validate or encode identifiers so they are valid
  Azure AI Search keys while preserving stable identity across refreshes.
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

The target ingestion path should reject or quarantine unknown classifications and
restricted records without allowed groups. Apply authorization to every retrieval
path, including document lookups and cached results; never trust caller-supplied
authorization groups. Non-retrievable group IDs do not themselves enforce access.
See Microsoft's [security-filter pattern](https://learn.microsoft.com/azure/search/search-security-trimming-for-azure-search).

Changes to classification or entitlement configuration require updates to affected
indexed documents. Coordinate restrictive changes with query authorization so
stale index permissions cannot continue granting access. Creating this schema
alone does not change the current application's classification defaults or
authorization behavior.

## Semantic ranking and future vector search

The `default` semantic configuration uses:

- Title: `title`.
- Content: `content`.
- Keywords, in priority order: `focus`, `diseases`, `data_types`, `institutions`,
  `organization`.

Semantic ranking requires service support and a query requesting semantic ranking;
the configuration alone does not enable it for every query.

The initial index has no vector fields or vector search configuration. Add those
after selecting an embedding model, with dimensions matching that model's output.
Start with record-level documents for the current compact metadata. If longer
full-text content later requires chunking, copy security fields onto every chunk.
