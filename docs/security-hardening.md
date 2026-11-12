# Security Hardening (as-built controls and remaining gaps)

**Reviewed: 2026-11-07, implementation commit `23f9658`.** The controls below are
implemented in the repository. This is not a claim of production readiness or a
live-environment security certification. Access-model detail is in
[`auth-design.md`](auth-design.md).

## Implemented access controls

- **Platform authentication:** ARM `authsettingsV2` enables Easy Auth, validates
  the tenant's v2 issuer and both API audience forms, returns 401 for missing
  authentication, and allowlists the connector client and Azure CLI. Only
  `/api/health` is excluded. Function routes themselves are `ANONYMOUS`.
- **Search entitlement:** `auth.has_base()` requires the configured base group.
  `auth.entitled_keys()` selects public sources plus restricted sources whose
  group IDs are held; `function_app._engine()` merges only that set.
- **Refresh administration:** HTTP refresh requires `ADMIN_ROLE` (default
  `Agent.Admin`). The role alone suffices; base/dataset memberships are not
  required. The internal timer is not an HTTP caller.
- **Group assignments:** the deployment creates `Agent.Access` and `Agent.Admin`;
  all three `AoU-*` groups are assigned to non-admin `Agent.Access` on the API
  enterprise application. Individual administrators get `Agent.Admin` separately.
- **Classification:** `DATASET_CLASSIFICATION` is configuration-driven. The
  deployed map restricts IHCC and CCDI independently and leaves publications and
  projects public to base-entitled users.

`Agent.Access` establishes application assignments for filtered group claims;
the Python authorization logic does not itself check that role. Groups must
remain group-object-ID claims, not be emitted as role claims.

## Index isolation and packaging

Refresh creates per-dataset `index/<key>.pkl` blobs and
`index/manifest.json`. Index records omit underscore-prefixed fields such as
`_body`, while normalized-doc caches (`<key>.docs.json`) retain searchable text.
Both cached documents and tokens can contain restricted information; they
remain internal data, not a sanitized public export.

The request-specific BM25 engine includes only entitled dataset keys. A
restricted index is not loaded on behalf of an unentitled search request, but
can remain cached in a process after an entitled user has loaded it. Refresh
intentionally processes all enabled datasets.

`code/.funcignore` excludes **all of `index/`** and the obsolete `corpus.pkl`.
No indexes, including public ones, ship in the deployment package.
`build_index.py` writes local development artifacts, not a deployed first-run
fallback. Its `--public` option depends on classification supplied to that
process; absent classification makes all source defaults public. Blob Storage
must be populated by refresh before a fresh deployment has searchable data.

The two worker-local caches are per-index documents/tokens/ETag and per-entitlement
merged engines/component ETags. They are not a distributed cache. See
[`architecture.md`](architecture.md) for the shared ETag-throttle limitation.

## Infrastructure controls actually provisioned

| Area | Current deployment behavior |
|---|---|
| Function | Flex Consumption, Python 3.12, VNet integration, system-assigned managed identity |
| Storage | Standard LRS, minimum TLS 1.2, Blob/Queue/Table private endpoints plus linked private DNS zones |
| Storage permissions | Function identity receives Blob, Queue, and Table Data Contributor at account scope |
| Runtime storage connection | Removes `AzureWebJobsStorage` connection string; injects managed-identity account/credential and per-cloud service URIs |
| Key Vault | RBAC authorization, explicit public network access enabled; no vault private endpoint |
| Vault permissions | Deploying signed-in user gets Secrets Officer if resolvable; Function identity gets Secrets User |
| Connector credential | Client secret stored as `connector-client-secret` when the data-plane write succeeds |
| CORS | Selected Azure portal origins; CORS is not an authentication boundary |
| Telemetry | Application Insights disabled; no Log Analytics, diagnostic settings, or alerts provisioned |

Private endpoints **do not by themselves disable public storage access**. The
script does not set storage public-network denial or disable shared-key access.
Connection-string authentication remains a supported Python storage-helper
fallback even though the deployed settings use managed identity.

The Function has a Key Vault read role, but no code or app setting retrieves
the connector secret at runtime. The operator reads it and enters it into the
Power Platform connector. Other operators need their own vault data-plane
permission. The script waits 30s for role propagation and attempts one secret
write; on failure it logs a warning and **prints the secret** for manual use
while continuing. This is not a private-link or policy-compliant fallback.
Protect deployment console/transcript output and do not assume the secret was
persisted merely because the final message says deployment completed.

## Configuration reference

| Setting | Purpose and current default |
|---|---|
| `AUTH_ENFORCED` | Deploy sets `true`; code defaults false and local settings use false |
| `BASE_ENTITLEMENT_GROUP_ID` | Object ID of `AoU-Agent-Users`; missing/empty fails the base-group check |
| `ADMIN_ROLE` | Literal role value for HTTP refresh; default `Agent.Admin` |
| `DATASET_CLASSIFICATION` | JSON map of classification and entitlement-group IDs, read at import |
| `CORPUS_CHECK_SECONDS` | Worker-wide ETag check interval; default 300, not a per-dataset freshness SLA |
| `CORPUS_CONTAINER` | Internal cache container; default `cache` |
| `INDEX_PREFIX`, `MANIFEST_BLOB` | Default `index/` and `index/manifest.json` |

Changing classification is an app-settings update/restart, not code redeployment.
Run refresh afterward to align the manifest metadata. **Invalid JSON is currently
warned about and ignored; missing entries default public.** The system's
missing-principal/base-group/admin-role checks fail closed, but its
classification-input handling does not. Group-ID uniqueness and schema/value
validation are also not enforced. Validate the persisted setting before allowing
users to search; the deployment's JSON-file write protects quoting, not semantics.

## Health disclosure and reliability boundaries

Health intentionally returns restricted dataset names, classification, counts,
source status, and manifest-entry timestamps without a token. It never returns
records. The connector spec still inherits global OAuth for its health operation;
anonymous API access and connector connection requirements are distinct.

Health returns `status: ok` even when a dataset's stored status is `failed` or
`carried`, or when the manifest is absent/unreadable and its dataset list is
empty. It does not probe upstream APIs or validate storage health. Its timestamp
is the manifest-entry write time, not last successful live ingest. It therefore
does not yet meet an unconditional "enumerate every registered dataset" or
readiness-check guarantee.

Source-exception carry-forward protects a previous index only when fetch and
cache recovery cannot return documents. Successful partial/empty responses can
replace an existing index and reduce counts. Upload failures can leave partially
updated indexes with an old manifest. No refresh lock, atomic snapshot, durable
job, or last-successful-refresh tracking is implemented.

## Tests and review findings

The offline suite covers normalizers, the shared source contract, HTTP fallback,
storage helpers, artifacts/BM25, per-index and entitlement caches, manifest
responses, and the Python authorization matrix. At this review it reports
**105 passed, 4 live tests deselected, 94.63% line coverage**. CI runs Python 3.11
and 3.12 with an 85% coverage gate.

From the repository root:

```powershell
python -m pytest -m "not live" --cov=code --cov-fail-under=85
```

These are mocked/offline tests, not validation of live Entra consent, group/role
token issuance, Easy Auth rejection rules, government availability, networking,
or Copilot Studio end-user connection configuration.

Outstanding code/deployment issues found during the as-built review:

| Issue | Impact / follow-up needed |
|---|---|
| Missing/invalid classification defaults public | Strict startup validation and an explicit fail-closed classification policy are needed; current JSON-file deployment handling alone is insufficient. |
| Shared ETag throttle checks only request keys | A cached restricted index can stay stale while public-only requests consume every check window; per-index check scheduling is needed for a bounded freshness guarantee. |
| Synchronous HTTP refresh with slow CCDI | Work can exceed Azure's 230s HTTP response limit; an asynchronous job pattern is not implemented. |
| Missing-manifest health says OK with no datasets | Does not distinguish empty/uninitialized state from storage failure or enumerate registered sources; explicit readiness/error reporting is needed. |
| Native CLI errors are generally not fail-fast | Except for the unresolved-group guard, deployment can continue after failed commands and report completion for partial setup. |
| Cleanup is name-based across the tenant | It can delete apps/groups used by another deployment; it is not limited by the selected resource-group suffix. |

Storage download/ETag helpers broadly catch exceptions and return missing-data
values without logging. Authentication parsing also has silent failure paths.
Structured operational metrics, denial reasons, and dependency traces are not
implemented. [`../plans/observability-plan.md`](../plans/observability-plan.md)
is a **proposal**, not deployed telemetry.

The Power Platform solution export includes Invoker-mode connector actions, but
also enabled web browsing and a generative Search fallback. Its stale result-type
and health-freshness instructions need alignment with the four-source API.
Consequently, "only answer from the search tool" is currently an instruction,
not a structural restriction in that export. See
[`architecture.md`](architecture.md#6-checked-in-power-platform-export).

## Operator handoff

Follow [`../readme.md`](../readme.md) and the caveats in
[`multi-cloud-deployment.md`](multi-cloud-deployment.md): verify cloud/tenant,
pre-authorize delegated clients, configure the connector callback URI, assign
users/groups/admins, publish, obtain a fresh API token, load data, and test the
entitlement matrix. Confirm each command's success; script completion alone is
not evidence that the controls above are active.

Code and infrastructure were reviewed but not modified in this documentation
update. No live resources were created, refreshed, or deleted.
