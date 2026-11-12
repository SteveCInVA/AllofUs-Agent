# Azure AI Search migration plan

**Status: Draft for review. Planning only; no application or infrastructure changes.**
**Date: 2026-11-11**

## 1. Goal and agreed scope

Replace application-managed `rank_bm25` retrieval with **Azure AI Search hybrid
keyword + vector search and semantic ranking in the initial release**.

Azure AI Search still uses BM25 for its keyword branch. The replacement is the
local pickle/token artifacts, in-memory engines, and entitlement-specific engine
caches, not the BM25 algorithm inside Azure.

Preserve:

- Copilot Studio -> custom connector -> Easy Auth -> Azure Function.
- Existing source adapters and normalized research/dataset records.
- Base-group and per-dataset entitlements, plus admin-only manual refresh.
- Search request parameters, aliases, top limit, result metadata, and source URLs.
- Commercial and Azure Government deployment support.
- Live -> normalized-doc cache -> last published source data resilience.

No new answer-generating LLM, agent framework migration, upstream source expansion,
or general observability redesign is required.

## 2. Current implementation and change points

| Area | Current behavior | Planned change |
| --- | --- | --- |
| [search_core.py](../code/search_core.py) | Text helpers, pickle artifacts, BM25 build/search | Keep shared text helpers and filter aliases; retire local retrieval after cutover |
| [function_app.py](../code/function_app.py) | Blob/ETag index loading; merged engines per entitlement signature | Call a reusable Azure Search client after authorizing; remove corpus/engine caches |
| [refresh_job.py](../code/refresh_job.py) | Build one pickle per dataset and publish a manifest | Prepare embeddings, upload a candidate index, validate, then publish |
| [feeds.py](../code/feeds.py) | Live ingestion and normalized JSON cache, including `_body` | Reuse; distinguish fetched caches from validated, published snapshots |
| [source_base.py](../code/source_base.py) | `_body` feeds tokenization; optional metadata passes through | Document searchable content, embedding input, and explicit result projection |
| [storage.py](../code/storage.py) | Index blobs, caches, manifest | Keep caches/manifest; add publication pointer, snapshot, and refresh-lock operations |
| [build_index.py](../code/build_index.py) | Local packaged pickle generation | Replace with an authenticated bootstrap/reindex CLI using the refresh pipeline |
| [requirements.txt](../code/requirements.txt) | `rank_bm25`, `numpy`, Azure Identity/Storage | Add Search, Azure OpenAI client, and model-compatible token counting; remove old dependencies after usage checks |
| [deployment script](../deployment/deploy_azure_infrastructure.ps1) | Function, storage, identity, networking | Add Search, embedding deployment, RBAC, network configuration, and settings |
| [connector](../custom_connector/openapi-swagger.yaml) | Stable search action and numeric score | Preserve action binding/schema; document new score semantics and dependency errors |

Add focused `search_service.py` and `embeddings.py` modules under `code/`.
Avoid a permanent multi-provider framework: any BM25/Azure selector is temporary
cutover support.

## 3. Target request flow

1. Validate the existing GET/POST inputs and resolve legacy directory aliases.
2. Use the existing claims-based authorization to compute enabled, entitled
   dataset keys, then intersect them with an explicit directory selection.
3. If the intersection is empty, return the existing empty search envelope
   without contacting either AI service. Never issue an unfiltered query.
4. Read the active publication descriptor, including physical index name,
   embedding model/version/deployment, dimensions, and schema version.
5. Embed the query using the same model and preprocessing as the active corpus.
6. Submit one Search request containing:
   - Keyword text and a vector query, fused by Azure using reciprocal rank fusion.
   - A server-built, escaped `source` allowlist filter on both retrieval branches.
   - Explicit vector `preFilter` mode; never retrieve globally and filter in Python.
   - Semantic configuration over title/content, with vector `k=50` to supply
     candidates and the caller's `top` (1-25) controlling returned results.
   - Explicit selected response fields; no facets, global counts, vectors, or
     internal indexing metadata.
7. Map results to the existing envelope:
   `query`, `directory`, `count`, `results`. `count` is returned-result count.

Reuse SDK clients per worker with bounded timeouts/retries. Cache only the small
publication descriptor, with an explicit refresh interval and maximum stale age;
do not cache user results or recreate per-entitlement engines.

### Authorization invariant

The Function remains the authorization boundary. Its managed identity can access
the corpus; end users receive neither Search credentials nor direct data-plane
access. Every request filter must come from the trusted registry and caller
entitlements, never a caller-provided OData expression.

A shared index does not reproduce the old engine's entitlement-specific corpus
statistics: lexical scoring can depend on all indexed records. Filters restrict
returned documents, but are not independent physical security partitions. Confirm
that application-level security trimming is acceptable before indexing restricted
data; a requirement for physical isolation would change this design.

Validate classification configuration at startup and refresh: malformed
configuration, unknown classifications, or restricted sources without groups
must block service readiness/publication, not silently broaden visibility.
This closes a current default-public hazard directly coupled to the migration.
Deployment must explicitly classify every enabled source.

## 4. Index and embedding design

**Recommendation:** one shared, versioned physical index per published corpus,
with one document per existing research/dataset record. Do not merge independently
scored per-dataset search results.

| Field | Role |
| --- | --- |
| `search_key` | Unique, deterministic Azure-safe key derived from source + original ID; keep original ID separately |
| `id`, `source`, `record_type`, `title`, `url`, `snippet` | Existing display contract; `source` is filterable |
| `content` | Full normalized `_body`, searchable; exclude from API output |
| `content_vector` | HNSW/cosine vector field; dimensions match the deployed model; not returned |
| `payload_json` | Non-searchable serialized slim record preserving optional metadata and current value shapes |
| `content_hash` | Embedding reuse/validation metadata; not returned |

Use explicit display fields for semantic configuration and `payload_json` to avoid
forcing heterogeneous metadata into incompatible Search types. For example, IHCC
currently emits `enrollment` as an integer or empty string. Decode and project the
slim payload while dropping private and empty fields as the API does today.
Validate required fields, source consistency, duplicate IDs, and key encoding
before upload.

**Proposed embedding model:** `text-embedding-3-small`, 1,536 dimensions, subject
to target-cloud deployment availability, quota, and evaluation. Pin model/version,
dimensions, preprocessing version, and deployment in the publication descriptor.
Changing embedding space requires a complete re-embedding and new index, even
when vector dimensions happen to match.

- Generate vectors in Python and push documents through the Search SDK. This
  reuses existing adapters and avoids a second ingestion system of indexers/skills.
- Use a deterministic, token-aware embedding input derived from `_body`, which
  already includes title and source-specific metadata. Proposed policy: retain
  the first 8,000 tokens for this model, leaving full lexical content intact;
  record truncation counts and evaluate affected long records before approval.
- Reject over-budget search queries with a documented 400 response rather than
  silently truncating the user's intent.
- Cache document vectors in protected Blob storage by content hash + complete
  model/preprocessing identity. Reuse unchanged vectors across refreshes.
- Batch embedding work within model token/input quotas and handle throttling with
  bounded retries. Validate response cardinality, dimensions, and finite values.
- Keep one record per result initially. Chunking and parent-result deduplication
  are deferred unless evaluation demonstrates a material long-record recall gap.

### Deliberate relevance changes

`score` will represent semantic reranker relevance, not legacy BM25 or RRF scores.
Keep its numeric shape and descending result ordering, but document that scores
are not comparable across engines, queries, or model changes.

Vector search can find neighbors for unrelated text. The old positive-BM25-score
test no longer establishes relevance. Calibrate a semantic-score cutoff against
labeled relevant and unrelated queries; preserve empty results below the cutoff.
Do not invent a numeric threshold before evaluation or claim it is a probability.
Require semantic ranking to complete; missing/partial semantic results are an
explicit dependency failure, not a silent fallback to RRF.

## 5. Safe refresh and publication

Use **blue/green full-corpus publication** initially. It is simpler to validate
than live upsert/delete reconciliation, preserves deletions, and avoids partial
refresh visibility. Confirm corpus size, index quotas, and full-upload cost first.

1. Acquire a renewable Blob lease shared by timer, CLI, and manual refresh workers.
   Refuse/serialize concurrent refreshes; loss of the lease blocks publication.
2. Run the existing resilient source loading. For an unavailable source, carry
   its last *published normalized snapshot*, not an unvalidated recent fetch cache.
   Preserve `live`, `cache`, `carried`, and `failed` reporting.
3. Validate source completeness. Empty or sharply reduced successful responses
   must be flagged for review rather than silently deleting a source's corpus.
   Establish per-source minimums/drop thresholds from the baseline, not guesses.
4. Create a uniquely named candidate index and build its vectors/documents.
   Upload in batches bounded by both 1,000 documents and 16 MB. Check every
   per-document indexing result, including partial successes; retry only eligible
   transient failures. Never promote a partially indexed corpus.
5. Poll until the expected documents are query-visible, then validate per-source
   counts, representative hybrid queries, semantic configuration, and metadata.
6. Persist immutable source snapshots and the completed manifest. Atomically
   replace one private publication descriptor using the lease and conditional
   ETag update. Readers see either the old or new index/model/manifest bundle.
7. Retain the previous index/snapshots for rollback. Cleanup only after descriptor
   cache expiry plus the in-flight request window and the agreed rollback period.
   Allow capacity for active, candidate, and retained previous indexes.

A source with no usable data and no published snapshot blocks initial production
promotion unless an operator explicitly disables that source. Failed candidate
builds leave the active publication unchanged and expose a failed/degraded job
status. Carry-forward retains data without pretending it was fetched recently:
track both last-successful-fetch and publication timestamps.

Continue the 03:00 UTC timer. **Proposed related API change:** make admin
`POST /api/refresh` enqueue work and return 202 + job ID/status URL; add an
admin-protected status endpoint, with durable completion/failure state and
idempotent queue handling. Existing synchronous ingestion already risks Azure's
HTTP response limit, and embeddings add work. Update its Swagger/runbook/tests;
do not quietly break clients expecting the old synchronous summary. Confirm this
change during plan approval. The search action remains unchanged.

Keep `/api/health` anonymous and metadata-only. Resolve it from the published
manifest, not an unrestricted Search count. Separate liveness from authenticated
readiness checks for the active index, embedding deployment, and configuration.
Never report search-ready based on manifest existence alone.

## 6. Azure resources, identity, and configuration

Extend the existing PowerShell deployment rather than introduce azd or a second
IaC framework.

- Provision an Azure AI Search service with vector support and semantic ranking
  enabled. Price Basic versus Standard using measured active/candidate/rollback
  vector storage and concurrent query/indexing load; select production replicas
  against the required availability SLA. Do not assume the free tier is adequate.
- Provision/reuse an approved Azure OpenAI resource and embedding deployment.
  No chat/completion deployment is needed.
- Runtime needs Search Index Data Reader; refresh needs Search Index Data
  Contributor and Search Service Contributor for document and index management.
  The current app hosts both paths, so its identity needs their combined
  permissions. Do not claim read/write isolation unless the worker is separated.
- Grant the embedding caller Cognitive Services OpenAI User. Keep schema/admin
  rights off connector users; use managed identity, not committed API keys.
- Extend the existing VNet/private-endpoint/private-DNS pattern for Search and
  OpenAI, including Function outbound connectivity and authorized bootstrap access.
  Disable public service access only after private connectivity is verified.
- Configure Search endpoint/audience, Azure authority, OpenAI endpoint/token scope,
  embedding deployment/model/version/dimensions, semantic configuration, and the
  publication descriptor location. Derive cloud-specific values centrally;
  do not infer them only from the Function hostname.
- Azure Government requires its own authority, Search token audience
  (`https://search.azure.us`), resource endpoints, and DNS zones. Validate SDK/API
  compatibility and actual query/embedding calls in each cloud.

**Government deployment gate:** Search semantic ranker is documented in Virginia
and Arizona, but embedding availability also depends on deployment type. Current
documentation lists `text-embedding-3-small` Standard in Arizona, and Data Zone
Standard in both Arizona and Virginia. Do not assume the existing Virginia
default supports the same regional Standard model deployment as Commercial.
Select an approved region/deployment type, check quota, and confirm data-residency
requirements; never route Government workloads to Commercial as a workaround.

Before provisioning, approve a cost estimate covering Search capacity/replicas,
semantic requests, initial and changed-document embeddings, one query embedding
per search, retained snapshots, network endpoints, and refresh overlap. No live
subscription/resource availability or pricing was verified while drafting.

## 7. Implementation sequence and exit gates

| Phase | Deliverable | Exit gate |
| --- | --- | --- |
| 1. Baseline and decisions | Fixed normalized corpus, entitlement matrix, labeled query set, counts/body lengths, cost/region/model choices | Data boundary, model/deployment, refresh API change, and relevance targets approved |
| 2. Service foundation | Resources, networking/RBAC/settings; clients, schema, embedding cache, reindex CLI | Real private, keyless Search and embedding calls succeed in target cloud |
| 3. Indexing pipeline | Snapshot carry-forward, embeddings, candidate validation, locked publication, queued refresh/status | Failure injection proves active corpus survives partial writes, retries, and concurrent runs |
| 4. Retrieval integration | Entitlement prefilters, hybrid + semantic query, response projection, explicit errors | Offline contract/auth tests and live filtered retrieval checks pass |
| 5. Evaluation and cutover | Same-corpus BM25 comparison, canary, dependency/privacy checks, rollback rehearsal | Approved quality/latency/cost gates pass; operator explicitly enables Azure retrieval |
| 6. Retirement and docs | Remove local artifacts/engines and temporary flag; update deployment/runbooks/diagrams | Observation window accepted; no remaining production BM25 dependency |

Temporary rollback is an explicit operator-selected backend or previous Azure
publication, not an automatic per-request fallback. During the migration window,
retain a matching BM25 corpus/configuration and the old deployable package.
Freeze refresh briefly during final comparison or build both artifacts from the
same normalized snapshot. Verify both rollback paths before removing old code.

## 8. Validation and acceptance

Extend existing [tests](../tests/README.md); keep ordinary CI offline with mocked
SDK boundaries, and add an opt-in live Azure integration group.

- **API/auth:** existing GET/POST parsing, aliases, unknown-directory behavior,
  `top` defaults/clamping, metadata/URLs, empty-field omission, 401/403, and admin
  checks remain covered. Public-only, single restricted, both restricted,
  disabled-source, and empty-entitlement cases must produce the correct prefilter.
- **Non-disclosure:** seed a restricted-only exact phrase and vector near-neighbor;
  public-only users receive neither. Verify keyword/vector branches and semantic
  candidates are scoped, and query input cannot override the filter. Inspect
  real SDK requests as well as result behavior.
- **Mapping/embeddings:** invalid keys, duplicate IDs, heterogeneous metadata,
  token limits/truncation, unchanged-vector reuse, model-version invalidation,
  wrong dimensions, and embedding batch response failures.
- **Publication:** 429/transient retries, partial indexing failures, carried and
  absent sources, suspicious drops, deleted records, lease loss, duplicate queue
  messages, query-visibility lag, pointer-write failure, stale readers, and rollback.
- **Failure semantics:** dependency timeout/auth failure returns a sanitized,
  documented 503; internal faults return 500. No successful empty-results response
  for an unavailable Search/embedding service or missing active publication.
- **Relevance:** label at least 40 representative queries across all four sources,
  including exact terms, paraphrases, acronyms, long descriptions, cross-source
  intent, and unrelated queries. Compare BM25, Azure keyword-only, hybrid, and
  hybrid+semantic on the same corpus and entitlement scope. Tune on a development
  split and report results/cutoff behavior on held-out queries.
- **Proposed measurable gates:** zero unauthorized results; aggregate nDCG@8 no
  worse than baseline and at least 10% relative improvement on the paraphrase
  subset; unrelated-query false-positive rate at most 5%; warm end-to-end search
  p95 at most 3 seconds under the agreed representative concurrency. Approve
  targets, labeling, and load profile before using these as release criteria.
- **Operations:** measure first-build and daily-refresh duration, embedding tokens,
  dependency latency/error rates, truncation, per-source counts/freshness, and
  semantic usage/cost. Log counts and outcomes, not raw queries, principal IDs,
  document text, or vectors. Review service diagnostic logging as well as app logs.

Run focused search/refresh/auth tests first, then the existing offline regression
suite and coverage gate. Exercise the actual Copilot Studio action with invoker
identity before rollout; mocks and schema checks do not prove live retrieval.

## 9. Documentation and approval items

Update the [README](../readme.md), [architecture](../docs/architecture.md),
[authorization design](../docs/auth-design.md),
[multi-cloud runbook](../docs/multi-cloud-deployment.md),
[security guidance](../docs/security-hardening.md), diagrams, connector, deployment
test script, and test documentation with the implementation. Preserve current
uncommitted documentation work. The checked-in Power Platform solution export is
a separate snapshot: re-export it only if connector/action changes require it.

Before implementation, approve:

1. Shared-index application-level security trimming versus physical isolation.
2. Target cloud/regions, embedding deployment type, SKU/capacity, and budget.
3. Embedding truncation policy, relevance cutoff methodology, and release targets.
4. Async manual-refresh contract and retained-index/rollback window.

## 10. Microsoft references

- [Hybrid retrieval and semantic candidate sizing](https://learn.microsoft.com/azure/search/hybrid-search-overview)
- [Semantic ranking](https://learn.microsoft.com/azure/search/semantic-search-overview)
- [Search region support](https://learn.microsoft.com/azure/search/search-region-support)
- [Search RBAC](https://learn.microsoft.com/azure/search/search-security-rbac)
- [SDK identity and sovereign-cloud audiences](https://learn.microsoft.com/azure/search/search-security-rbac-client-code)
- [Embedding models/deployment types in Azure Government](https://learn.microsoft.com/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure-gov)
- [Embedding input limits](https://learn.microsoft.com/azure/ai-foundry/openai/how-to/embeddings)
- [Index update/rebuild and batch limits](https://learn.microsoft.com/azure/search/search-howto-reindex)
