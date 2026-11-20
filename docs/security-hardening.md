# Security Hardening (as-built controls and remaining gaps)

**Original review: 2026-11-07; updated for Azure AI Search ingestion.** The controls below are
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
  group IDs are held. Search filters combine that set with indexed classification
  and configured-group checks before returning results.
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

Refresh uploads source metadata into the shared Azure AI Search index, including
full searchable `content`, `security_type`, and `allowed_group_ids`. The content
can include restricted information; the index is not a sanitized public export.
Permissions are supplied by source configuration, not upstream document values.

The backend builds a source-specific Search filter using the caller's entitled
keys and requiring indexed classification/group values to match current
configuration. Restrictive configuration changes exclude stale records until
they are reimported. There are no in-process corpus/result caches or Blob
fallbacks. Refresh intentionally processes all enabled datasets. Disabling
`AUTH_ENFORCED` also bypasses indexed permission checks for development.

`code/.funcignore` excludes **all of `index/`** and the obsolete `corpus.pkl`.
No indexes, including public ones, ship in the deployment package.
`build_index.py` now imports directly into Search. Its `--public` option depends
on classification supplied to that process; absent classification makes all
source defaults public. A new Search index must be populated by refresh before
it has searchable records. Legacy Blob corpus files are not read or deleted
automatically. See [Search Index Structure](search-index.md) for migration details.

## Infrastructure controls actually provisioned

| Area | Current deployment behavior |
|---|---|
| Function | Flex Consumption, Python 3.12, VNet integration, system-assigned managed identity |
| Storage | Standard LRS, minimum TLS 1.2, Blob/Queue/Table private endpoints plus linked private DNS zones |
| Storage permissions | Function identity receives Blob, Queue, and Table Data Contributor at account scope |
| Runtime storage connection | Removes `AzureWebJobsStorage` connection string; injects managed-identity account/credential and per-cloud service URIs |
| Search | Private endpoint/DNS; public access enabled during deployment and must be disabled manually afterward; API keys remain disabled; Function receives Search document read/write and index-management roles |
| Search deployer | Resolves Azure CLI user/service principal, grants Search Service Contributor, and creates/verifies the index with temporary public access |
| Key Vault | RBAC authorization, explicit public network access enabled; no vault private endpoint |
| Vault permissions | Deploying signed-in user gets Secrets Officer if resolvable; Function identity gets Secrets User |
| Connector credential | Client secret stored as `connector-client-secret` when the data-plane write succeeds |
| CORS | Selected Azure portal origins; CORS is not an authentication boundary |
| Telemetry | Application Insights disabled; no Log Analytics, diagnostic settings, or alerts provisioned |

Private endpoints **do not by themselves disable public storage access**. The
script does not set storage public-network denial or disable shared-key access.
The application no longer uses connection-string corpus storage helpers; the
storage account remains required by the Functions host.

**Search deployment exposure:** public network access is not disabled
automatically, even on failure. The script warns and prints
`az search service update --resource-group <resource-group> --name <search-service> --public-network-access disabled`.
Run it after deployment or troubleshooting and verify private Function access.
While public access is enabled, Search is network-reachable publicly but still
requires Entra authentication and appropriate Search RBAC roles.

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
| `AZURE_SEARCH_ENDPOINT` | Required Search endpoint with private connectivity |
| `AZURE_SEARCH_INDEX_NAME` | Defaults to `allofus-content`; deploy sets the name from `index.json` |
| `AZURE_SEARCH_AUDIENCE` | Commercial default `https://search.azure.com`; Government `https://search.azure.us` |
| `AZURE_AUTHORITY_HOST` | Entra authority for the selected cloud |

Changing classification is an app-settings update/restart, not code redeployment.
Run refresh afterward to align indexed security metadata. **Invalid JSON is currently
warned about and ignored; missing entries default public.** The system's
missing-principal/base-group/admin-role checks fail closed, but its
classification-input handling does not. Search mapping rejects unknown
classifications and restricted sources without groups, but group-ID uniqueness
and configuration map-shape validation remain incomplete. Validate the persisted setting before allowing
users to search; the deployment's JSON-file write protects quoting, not semantics.

## Health disclosure and reliability boundaries

Health intentionally returns restricted dataset names, classification, counts,
index status, and latest document-ingestion timestamps without a token. It never returns
records. The connector spec still inherits global OAuth for its health operation;
anonymous API access and connector connection requirements are distinct.

Health queries Search for every enabled source, including empty sources.
Source status is `indexed` or `empty`, not the outcome of the last refresh.
Search errors return HTTP 503 rather than empty success. The latest indexed
timestamp is unchanged when a source fetch fails; it is `null` for empty sources.
Health does not probe upstream APIs, certify a complete refresh, or provide a
transactional snapshot of all counts.

Source-exception carry-forward leaves existing Search documents unchanged.
Successful partial/empty responses can remove old records and reduce counts.
Uploads are validated and acknowledged before stale IDs are deleted, but upload
or delete failures can leave partial changes. Search updates are eventually
consistent. No refresh lock, atomic snapshot, durable job, or persisted
last-successful-refresh tracking is implemented; run one refresh at a time.

## Tests and review findings

The offline suite covers normalizers, the shared source contract, HTTP fallback,
Search schema mapping, batching, synchronization failures, real SDK serialization
against an offline transport, HTTP/health responses, legacy offline BM25 helpers,
and the Python authorization matrix. CI runs Python 3.11
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
| Non-atomic Search refresh and overlapping runs | Partial failures can leave mixed data; rerun after remediation and avoid overlapping refreshes. A distributed single-writer/job design is not implemented. |
| Synchronous HTTP refresh with slow CCDI | Work can exceed Azure's 230s HTTP response limit; an asynchronous job pattern is not implemented. |
| Native CLI errors are generally not fail-fast | Except for the unresolved-group guard, deployment can continue after failed commands and report completion for partial setup. |
| Cleanup is name-based across the tenant | It can delete apps/groups used by another deployment; it is not limited by the selected resource-group suffix. |

Search SDK errors propagate and HTTP handlers log failures; health reports 503
for failed dependency queries. Authentication parsing still has silent failure paths.
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
