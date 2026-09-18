# Security Hardening (as-built)

**Status: Implemented** on branch `feature/multi-source`. This describes how the
authorization and per-dataset access controls work in the code today. The design
rationale is in [`auth-design.md`](auth-design.md); the remaining work is operator
setup (Entra objects) and the cutover, both at the end of this document.

## Overview

Access is per-user and entitlement-based. Delegated Microsoft Entra sign-in flows
the caller's identity to the Function (via App Service Authentication / "Easy Auth"),
which the code maps to entitlements from the caller's group/role claims:

- `publication`, `project` — **public** (gated by the base group `AoU-Agent-Users`).
- `ihcc`, `ccdi` — **restricted**, each requiring its own Entra group
  (`AoU-DS-IHCC`, `AoU-DS-CCDI`). Compartments are independent (IHCC ≠ CCDI).
- `/refresh` requires the **`Agent.Admin`** app role. `/health` is anonymous.

Enforcement is gated by the **`AUTH_ENFORCED`** app setting. While `false` (the
current default) the app keeps its prior behavior (function-key auth, all datasets
visible), so the code shipped safely before Entra/Easy Auth were stood up. Setting
it `true` turns on the claims-based checks below.

## How it works (as-built)

### Per-dataset indexes
Each dataset has its own index blob `index/<key>.pkl`. The refresh job builds and
uploads one index per dataset plus a manifest `index/manifest.json` (per-dataset
name, classification, count, updated_at). This lets the Function load **only** the
datasets a caller is entitled to — restricted content is never even loaded for an
unentitled caller.

- `storage.py` — `upload_index/download_index/get_index_etag` (blob `index/<key>.pkl`),
  `upload_manifest/download_manifest`, `download_blob_with_etag/get_blob_etag`.
- `refresh_job.py` — builds/uploads per-dataset indexes; a source that is unavailable
  keeps its previous index (carried) so nothing is blanked; writes the manifest.
- `search_core.py` — `load_artifact` (docs+tokens, no BM25) and `build_engine`
  (merge parts into one BM25); `search` guards an empty engine.
- `build_index.py` — writes packaged per-dataset indexes; `--public` skips restricted
  datasets so **restricted data is never in the deployment package**.

### Two-level cache (function_app.py)
1. **Per-index cache** — `{key: (docs, tokens, etag)}` loaded from Blob (or the
   packaged `code/index/<key>.pkl` fallback), hot-reloaded on ETag change (throttled).
2. **Per-entitlement engine cache** — `{sorted(keys): (docs, bm25, component etags)}`.
   BM25 scores aren't comparable across separately-built indexes, so a **merged
   engine is built over exactly the caller's entitled datasets** and cached by the
   entitlement signature. Public-only callers share one merged engine.

### Classification config (config-driven, no redeploy)
`source_base.Source` carries `classification`, `entitlement_group_id`, and
`index_blob`. These are applied from the **`DATASET_CLASSIFICATION`** app-settings
JSON map by `sources.apply_classification()` — the source modules do **not** hardcode
classification. A key absent from the config defaults to `public`. Helpers:
`public_keys()`, `restricted_keys()`, `key_for_group(gid)`. Changing classification
is an app-settings change (app restart), never a redeploy.

```jsonc
DATASET_CLASSIFICATION = {
  "ihcc": { "classification": "restricted", "entitlement_group_id": "<AoU-DS-IHCC guid>" },
  "ccdi": { "classification": "restricted", "entitlement_group_id": "<AoU-DS-CCDI guid>" }
}
```

### Authorization (auth.py + function_app.py)
`auth.py` parses `X-MS-CLIENT-PRINCIPAL` (groups, roles, oid) and resolves
entitlements. Checks fail closed and only apply when `AUTH_ENFORCED=true`.

| Endpoint | Behavior when enforced |
|---|---|
| `GET/POST /api/search` | Require the base group (`BASE_ENTITLEMENT_GROUP_ID`) → else `401`/`403`. Load a merged engine over the caller's entitled datasets only (public + restricted groups they hold). |
| `POST /api/refresh` | Require the `Agent.Admin` role (`ADMIN_ROLE`) → else `401`/`403`. The internal daily timer is unaffected (runs in-process, no token). |
| `GET /api/health` | Anonymous; enumerates **every** dataset (incl. restricted) with name + count from the manifest. Intentionally discloses restricted dataset names + counts (not content). |

### App settings
| Setting | Purpose |
|---|---|
| `AUTH_ENFORCED` | `true` enables claims checks (default `false`) |
| `BASE_ENTITLEMENT_GROUP_ID` | Entra object id of `AoU-Agent-Users` |
| `ADMIN_ROLE` | app role for refresh (default `Agent.Admin`) |
| `DATASET_CLASSIFICATION` | JSON map of per-dataset classification + group ids |

## Tests
`tests/` covers per-dataset storage, the merged engine + hot-reload, manifest health,
config-driven classification, and the full authorization matrix (401 unauth, 403 no
base group, base-user = public only, IHCC-entitled adds IHCC not CCDI, admin-gated
refresh). Suite: 107 tests, ~94% coverage.

## Remaining operator setup + cutover
The application code is complete; going live requires tenant/deploy actions:

1. Run [`../deployment/deploy_entra_auth.txt`](../deployment/deploy_entra_auth.txt) to
   create the API + client app registrations, the groups (`AoU-Agent-Users`,
   `AoU-DS-IHCC`, `AoU-DS-CCDI`), the `Agent.Admin` role, filtered group-claims token
   config, **Easy Auth** (Return 401 with `/api/health` excluded), a **Key Vault** for
   the connector secret, and the auth app settings. It leaves `AUTH_ENFORCED=false`.
2. **Cutover:** import the OAuth2 connector
   (`../custom_connector/openapi-swagger-oauth2.yaml`), set `AUTH_ENFORCED=true`,
   verify entitlements, switch the `/search` and `/refresh` route auth levels to
   `ANONYMOUS` (Easy Auth now gates), then disable the function keys.
3. **Rollback:** set `AUTH_ENFORCED=false` and re-enable function keys.
