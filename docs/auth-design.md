# Authentication & Authorization Design

**Status:** Approved design (not yet implemented). No application code changes are
made by this document — it is the plan for the security-hardening work.

## 1. Requirement
Access to datasets is entitlement-based and per user:
- **Public** datasets: available to any authenticated user with the base entitlement.
- **Restricted** datasets: each is an **independent compartment**. In this solution
  the **IHCC** and **CCDI** datasets are restricted: a user granted the **IHCC**
  entitlement must **not** see **CCDI** data, and vice‑versa. Users may hold any
  combination (additional restricted datasets follow the same pattern).

This is per-user, need-to-know authorization, so the **end user's identity must
flow to the service** — an app-only service principal cannot satisfy it.

**Access is layered — "public" is not open to the internet.** Every request
requires authentication *and* membership in the base entitlement group
(`AoU-Agent-Users`); that base group is what gates "public" datasets. Restricted
datasets require an *additional* per-dataset group on top of the base entitlement.
So an unauthenticated caller gets `401`, an authenticated caller without the base
group gets `403`, and "public" means "available to all entitled agent users," not
"available to the general public."

## 2. Decisions (confirmed)
| Decision | Choice |
|---|---|
| Identity | **Delegated Entra ID (OAuth 2.0 authorization-code)** on the connector |
| Dataset entitlement | **One Entra security group per restricted dataset** (public datasets need no *dedicated* group — they are gated by the base entitlement below) |
| Tiers | Binary per dataset: `public` or `restricted` (restricted = compartmented by dataset) |
| Base entitlement | An Entra **group** (`AoU-Agent-Users`) required to use `/search` |
| Refresh (HTTP) | Requires the **`Agent.Admin`** app role |
| Health | **Anonymous**, generic status only |
| Index strategy | **Per-dataset indexes**; load/merge only entitled ones |
| Group claims | **Filtered group claims** (groups assigned to the app) to keep tokens lean and avoid the >200-group overage |
| Classification source of truth | Maintained **in the application dataset config** |

## 3. Recommended mechanism (summary)
Delegated Entra OAuth2 for identity → **Easy Auth** validates the JWT on the
Function → the Function reads the caller's **group claims** → computes entitled
datasets (public + restricted groups the user holds) → loads **only those
per-dataset indexes** → BM25 ranks → returns results (and counts) **trimmed** to
the entitled set. `Agent.Admin` app role gates HTTP refresh.

## 4. Entra objects (customer GCC tenant)
- **API app registration** ("AllOfUs-Function-API")
  - Expose an API → scope `access_as_user`; Application ID URI `api://<api-app-id>`.
  - App Role: **`Agent.Admin`** (member type: Users/Groups).
  - Token configuration: emit **group claims filtered to groups assigned to the app**.
- **Client app registration** ("AllOfUs-Function-Client") — delegated; pre-authorized
  to the API's `access_as_user` scope (connector uses this).
- **Groups**
  - `AoU-Agent-Users` — base entitlement (public datasets + agent use).
  - `AoU-DS-<name>` — one per **restricted** dataset (currently `AoU-DS-IHCC` and
    `AoU-DS-CCDI`).
- Admin consent for the API scope; assign `Agent.Admin` to admins.

## 5. Authorization rules (enforced in the Function, after Easy Auth)
| Endpoint | Rule |
|---|---|
| `GET/POST /api/search` | Require `AoU-Agent-Users`; entitled datasets = all `public` + every `restricted` whose group is in the caller's claims. Trim results **and counts** to that set. `403` if base group absent. |
| `POST /api/refresh` | Require **`Agent.Admin`** app role; rebuilds all dataset indexes. `403` otherwise. |
| `GET /api/health` | Anonymous; generic `ok` only (no per-dataset restricted counts). |

Note: the internal **daily timer** refresh runs in-process (no HTTP token) and is
unaffected by the `Agent.Admin` gate — that gate protects only the HTTP endpoint.

## 6. Data classification config (source of truth)
Maintained in the app; each dataset declares its classification and (if restricted)
its entitlement group and index blob:

```jsonc
{
  "publication": { "classification": "public",     "index_blob": "index/publication.pkl" },
  "project":     { "classification": "public",     "index_blob": "index/project.pkl" },
  "ihcc":        { "classification": "restricted", "entitlement_group_id": "<AoU-DS-IHCC guid>",
                   "index_blob": "index/ihcc.pkl" },
  "ccdi":        { "classification": "restricted", "entitlement_group_id": "<AoU-DS-CCDI guid>",
                   "index_blob": "index/ccdi.pkl" }
}
```
Current classification: **publication** and **project** are public (gated by the base
entitlement); **ihcc** and **ccdi** are restricted, each requiring its own group
(`AoU-DS-IHCC`, `AoU-DS-CCDI`).
The `Source` in the registry carries `classification`, `entitlement_group_id`, and
its index blob name — but these are **applied from an app-settings JSON map**
(`DATASET_CLASSIFICATION`), not hardcoded in the source modules. Changing a dataset's
classification or its entitlement group is therefore an **app-settings change (which
restarts the app)** plus the matching Entra group work — **not an application redeploy**.

## 7. Index architecture (extends today's registry)
- Refresh builds **each dataset's index independently** → uploads `index/<dataset>.pkl`
  (natural extension of the current per-source normalization from one merged corpus).
- The Function keeps a **per-index in-memory cache** (dict keyed by dataset) with
  ETag hot-reload; a restricted index is loaded lazily the first time an entitled
  user requests it.
- All blobs remain in the MI + private-endpoint-protected storage account. Restricted
  content is **never loaded** for an unentitled caller, so a trimming bug cannot leak it.

## 8. Authorized request flow

```mermaid
flowchart TB
    U["User (signed in)"] --> AG["Copilot Studio agent"]
    AG --> CC["Custom connector<br/>OAuth2 delegated (Entra)"]
    CC -->|"Bearer token"| EA["Easy Auth<br/>validate audience + issuer"]
    EA -- "invalid / missing" --> R401["401"]
    EA -- "valid" --> BASE{"in base group<br/>AoU-Agent-Users?"}
    BASE -- "no" --> R403["403"]
    BASE -- "yes" --> ENT["entitled datasets =<br/>public + restricted groups in claims"]
    ENT --> LOAD["load + merge entitled<br/>per-dataset indexes"]
    LOAD --> RANK["BM25 rank + trim counts"]
    RANK --> OUT["results: entitled records only"]
```

## 9. Changes required (plan only — no code yet)
| Area | Change |
|---|---|
| `code/function_app.py` | routes → `ANONYMOUS` (Easy Auth gates); parse `X-MS-CLIENT-PRINCIPAL`; base-group check on `/search`; `Agent.Admin` check on `/refresh`; per-request index selection + trimming |
| `code/source_base.py`, `sources.py` | add `classification`, `entitlement_group_id`, per-dataset index name (the classification config) |
| `code/refresh_job.py`, `storage.py` | write/read **per-dataset index blobs** instead of one `corpus.pkl` |
| `code/search_core.py` | search across a **merged set of entitled indexes** |
| `custom_connector/openapi-swagger.yaml` | apiKey → **OAuth 2.0 (Azure AD, delegated)** |
| `deployment/*` | app registrations, groups, Easy Auth, `Agent.Admin` role, Key Vault for the client secret; GCC authorities |
| `tests/` | authorization + trimming tests (entitled vs unentitled datasets; admin vs non-admin refresh; anonymous health) |

## 10. Service principal — where it lands
Not for user access (no user context). Retained only for **non-interactive machine
callers** (e.g., an external scheduler triggering refresh) holding `Agent.Admin`.

## 11. GCC considerations
Use gov-cloud authorities (`login.microsoftonline.us`, `*.azurewebsites.us`), create
the app registrations/groups in the **customer GCC tenant**, and parameterize the
IaC/connector accordingly.

## 12. Effort & sequencing (rough)
1. Entra objects (app regs, groups, role, token config) + admin consent — ~2h
2. Per-dataset index refactor (refresh + storage + search + cache) — ~5h
3. Function authz (claims parse, base/admin checks, trimming) — ~4h
4. Connector OAuth2 + deploy-script + Key Vault — ~4h
5. Authorization tests — ~2h

**Rollback:** keep function keys enabled during transition; cut the connector over
to OAuth2, verify, then disable keys.
