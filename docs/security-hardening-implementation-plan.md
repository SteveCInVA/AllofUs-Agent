# Security-Hardening Implementation Plan

Implements the approved design in [`auth-design.md`](auth-design.md): delegated
Entra ID auth, Entra security groups as per-dataset entitlements, **per-dataset
indexes** with security-trimming, `Agent.Admin` app role for HTTP refresh, filtered
group claims, anonymous health. **This is the implementation plan — no code has
been changed yet.**

## 1. Current-state review (delta to close)

| Area | Today (reviewed) | Target |
|---|---|---|
| Corpus | **single merged `corpus.pkl`** blob; `storage.upload_corpus/download_corpus/get_corpus_etag` | **one index blob per dataset** (`index/<key>.pkl`) |
| Engine | `function_app._engine` = one in-memory `(docs, bm25)` with ETag hot-reload | **merged engine cache keyed by the caller's entitlement set** |
| `Source` model | `key,label,fetch,normalize,enabled,cache_blob,record_type` | + `classification`, `entitlement_group_id`, `index_blob` |
| `/search` auth | `FUNCTION` key; filters by `directory` only | `ANONYMOUS` + Easy Auth; require base group; results limited to **entitled** datasets |
| `/refresh` auth | `FUNCTION` key | require **`Agent.Admin`** app role |
| `/health` | returns per-source `counts` | anonymous, **generic status only** |
| refresh_job | builds one merged corpus | builds/uploads **per-dataset** indexes |
| Auth/claims code | **none** | new `auth.py` (parse `X-MS-CLIENT-PRINCIPAL`, resolve entitlements) |
| Connector | apiKey `x-functions-key` | OAuth 2.0 (Entra, delegated) |
| Deploy | MI for storage; VNet/PE; **no Entra/Easy Auth/Key Vault** | app registrations, groups, app role, Easy Auth, Key Vault |
| Packaged fallback | ships `corpus.pkl` | ship **public** per-dataset indexes only; **restricted data never in the package** |

## 2. Key design decisions (new detail beyond auth-design.md)

- **Search across entitled indexes — merged engine cache.** BM25 scores are not
  comparable across separately-built indexes, so we build a **combined engine from
  the union of the caller's entitled indexes** and **cache it keyed by the sorted
  entitlement set** (a frozenset "signature") plus a composite ETag. Most users are
  public-only → they share one cached merged engine; each distinct restricted
  combination gets its own cached engine, hot-reloaded when any constituent index
  ETag changes. Correct scoring + caching, bounded memory.
- **Restricted data never ships in the deployment package.** Only `public`
  per-dataset indexes are packaged for the first-run fallback; restricted indexes
  exist only in Blob (behind MI + private endpoints) and are loaded lazily for
  entitled callers.
- **Claims source.** Easy Auth injects `X-MS-CLIENT-PRINCIPAL` (base64 JSON) with
  `roles` (app roles) and group object IDs. `auth.py` parses it. Set Easy Auth
  **Require authentication = Return 401** with `/api/health` in **excludedPaths** so
  health stays anonymous while everything else is gated by the platform.
- **Group IDs are config, not code.** Base group id, admin role name, and each
  restricted dataset's `entitlement_group_id` come from **app settings / a config
  blob** (they are GCC-tenant-specific), e.g. `BASE_ENTITLEMENT_GROUP_ID`,
  `ADMIN_ROLE=Agent.Admin`, and a `DATASET_ENTITLEMENTS` JSON map `{key: group_id}`.
- **`AUTH_ENFORCED` feature flag.** Lets us deploy the code first (flag off = current
  behavior) and flip enforcement on after the Entra objects + connector cutover, so
  there's no big-bang break. Keep function keys enabled during transition.

## 3. Phased implementation

### Phase 1 — Per-dataset index refactor (foundation, no auth yet)
Behavior stays identical (all datasets are `public` → everyone still sees all).
- `storage.py`: generalize corpus helpers to per-blob — `upload_index(key, bytes)`,
  `download_index(key) -> (bytes, etag)`, `get_index_etag(key)` (blob `index/<key>.pkl`).
  Keep the old corpus functions temporarily for rollback.
- `refresh_job.py`: build **one artifact per dataset** and upload each; return
  per-dataset counts/etags/statuses (extends the existing per-source loop).
- `search_core.py`: add a helper to **merge multiple `(docs, tokens)` artifacts** into
  one engine (union docs, rebuild BM25).
- `function_app.py`: replace the single-engine globals with a **per-entitlement
  merged-engine cache** (`{signature: (docs, bm25, composite_etag, last_check)}`);
  `_engine(keys)` loads/merges the given dataset indexes with hot-reload.
- `build_index.py`: write per-dataset packaged indexes (public only).
- **Tests:** per-dataset upload/download; merged-engine equivalence to today; refresh
  produces N indexes.

### Phase 2 — Classification config on the Source model
- `source_base.py`: add `classification` (`public`/`restricted`), `entitlement_group_id`,
  `index_blob` (default `index/<key>.pkl`).
- `source_allofus/ihcc/ccdi`: mark `classification="public"`.
- `sources.py`: helpers `public_keys()`, `restricted_keys()`, `key_for_group(gid)`;
  load restricted `entitlement_group_id`s from the `DATASET_ENTITLEMENTS` config.
- **Tests:** registry classification + group→key resolution.

### Phase 3 — Authorization (behind `AUTH_ENFORCED`)
- New `auth.py`:
  - `principal(req)` → parse `X-MS-CLIENT-PRINCIPAL` (roles, group ids, name/oid).
  - `has_base(p)`, `is_admin(p)`, `entitled_keys(p)` = `public_keys()` +
    restricted keys whose group id is in the caller's groups.
- `function_app.py`:
  - routes `search`/`refresh` → `AuthLevel.ANONYMOUS` (Easy Auth gates).
  - `/search`: if enforced → require `has_base` else `403`; compute `entitled_keys`,
    intersect with any requested `directory`, load merged engine for those, trim
    results + counts. Flag off → today's behavior.
  - `/refresh`: if enforced → require `is_admin` else `403`.
  - `/health`: return generic `{"status":"ok"}` (no per-dataset counts).
- **Tests:** unauthenticated/missing-base → 401/403; entitled sets drive results
  (apples-user sees apples not bananas); admin gate; health generic; flag-off parity.

### Phase 4 — Connector + Azure/Entra config
- `custom_connector/openapi-swagger.yaml`: apiKey → **OAuth 2.0 (Azure AD, delegated)**
  (client id, tenant, auth/token URLs, scope `api://<api-app-id>/.default`); per-cloud
  authorities (GCC = `login.microsoftonline.us`).
- `deployment/deploy_azure_infrastructure.txt`: create/reference the **API + client
  app registrations**, **groups** (`AoU-Agent-Users`, `AoU-DS-<name>`), **App Role
  `Agent.Admin`**, filtered **group-claims** token config, **Easy Auth** (Require auth
  = 401, `excludedPaths=[/api/health]`, audiences=`api://<api-app-id>`), a **Key Vault**
  for the client secret, and the new app settings (`AUTH_ENFORCED`,
  `BASE_ENTITLEMENT_GROUP_ID`, `ADMIN_ROLE`, `DATASET_ENTITLEMENTS`). Parameterized by
  `$cloud`.

### Phase 5 — Cutover & docs
- Flip `AUTH_ENFORCED=true`, verify, then **disable function keys**.
- Update `readme.md` connector steps (OAuth2 connection instead of App key), testing
  script (bearer token instead of `x-functions-key`), and add an ops note on managing
  dataset groups. Update `tests/README.md`.

## 4. New/changed files summary
| File | Phase | Change |
|---|---|---|
| `storage.py` | 1 | per-dataset blob helpers |
| `refresh_job.py` | 1 | build/upload per-dataset indexes |
| `search_core.py` | 1 | merge-engines helper |
| `function_app.py` | 1,3 | per-entitlement engine cache; authz; health trim; auth levels |
| `build_index.py` | 1 | packaged public indexes |
| `source_base.py`, `sources.py`, `source_*` | 2 | classification + entitlement config |
| `auth.py` (new) | 3 | principal parsing + entitlement resolution |
| `openapi-swagger.yaml` | 4 | OAuth2 |
| `deploy_*.txt`, `testing_*.txt` | 4,5 | Entra/Easy Auth/Key Vault; bearer-token tests |
| `tests/*` | 1-3 | per-dataset, merged engine, authorization/trimming |
| `readme.md`, `tests/README.md` | 5 | connector + ops docs |

## 5. Rollout & backward-compat
- Ship Phases 1–3 with `AUTH_ENFORCED=false` → no behavior change, fully tested.
- Stand up Entra objects + connector (Phase 4); keep function keys live.
- Flip `AUTH_ENFORCED=true` (Phase 5), validate entitlements, disable keys.
- **Rollback** at any point: set `AUTH_ENFORCED=false` and re-enable keys.

## 6. Open decisions
1. **Group-id config channel:** app settings (`DATASET_ENTITLEMENTS` JSON) vs a small
   config blob the admin edits. Recommend app settings for now (few datasets).
2. **Packaged fallback:** ship public per-dataset indexes (instant-after-deploy) vs
   require an initial `/api/refresh`. Recommend ship public only.
3. **Health:** fully generic `{"status":"ok"}` vs include a **public** total only.
   Recommend fully generic.
4. Confirm Easy Auth `excludedPaths` for `/api/health` is acceptable vs enforcing per-route in code.

## 7. Effort (rough)
| Phase | Est. |
|---|---|
| 1 — per-dataset indexes + merged engine | ~6h |
| 2 — classification config | ~1.5h |
| 3 — authorization + tests | ~4h |
| 4 — connector + Entra/Easy Auth/Key Vault (deploy) | ~4h |
| 5 — cutover + docs | ~2h |
| **Total** | **~17.5h** |
