# Observability Plan — Application Insights + Log Analytics

**Status: Proposed (plan only — no code or infrastructure changes made).**

A design for adding logging, metrics, and alerting to the All of Us agent's Azure
Function. Grounded in a review of the current code: logging is thin (`feeds.py` 7,
`function_app.py` 7, `refresh_job.py` 3, `sources.py` 1; **none** in `auth.py`,
`storage.py`, or the source normalizers), there are ~21 `except` blocks that
**swallow errors silently** (6 in `storage.py`), `host.json` already has an App
Insights sampling stanza, and the deploy currently passes `--disable-app-insights`.

## 1. Architecture
- **Workspace-based Application Insights** — an App Insights component wired to a
  **Log Analytics (LA) workspace** so telemetry lands in queryable LA tables with
  configurable retention and KQL-based alerts.
- Provision an **LA workspace** + **App Insights component**; **remove
  `--disable-app-insights`** from the deploy and set
  **`APPLICATIONINSIGHTS_CONNECTION_STRING`** (the connection string embeds the
  correct per-cloud ingestion endpoint → works in Commercial and GCC).
- **Diagnostic settings** route platform logs to the same workspace: Function App
  (`FunctionAppLogs`, metrics), Storage account (blob transactions/audit), and
  **Key Vault audit** (secret-access events).
- Parameterize by `$cloud` like the rest of the deploy (App Insights + LA exist in
  Azure Government).

## 2. What WILL be captured automatically (no code needed, once enabled)
| Telemetry | LA table | Notes |
|---|---|---|
| Every function invocation (search, refresh, health, timer) | `requests` | name, duration, result code, success, `operation_Id` |
| All `logging.*` output | `traces` | existing info/warning logs flow in, auto-correlated to the parent request |
| Unhandled + `logging.exception` errors | `exceptions` | stack traces (e.g., the search/refresh `logging.exception` calls) |
| Host/platform performance, live metrics | `performanceCounters`, live stream | CPU/memory, cold starts |
| Platform logs (via diagnostic settings) | `FunctionAppLogs`, storage, Key Vault audit | infra-level |

## 3. What will NOT be captured (gaps)
- **Outbound dependencies.** Calls to Blob Storage (`azure-storage-blob`) and the
  data-source APIs (`urllib` in `feeds.fetch`) are **not** auto-collected as
  `dependencies` in Python Functions. You won't see "Blob download failed" or "CCDI
  API took 240s" as dependency telemetry by default.
- **Silent failures.** The ~21 swallowed `except` blocks (esp. `storage.py`
  download/etag, `auth.py` malformed principal, `feeds` fallbacks) never log — so a
  storage permission error currently surfaces only as "empty results," invisible in
  telemetry.
- **Business/operational metrics.** Nothing captures: search volume, result counts,
  which datasets were searched, entitled-dataset counts, refresh record counts per
  source, degraded refreshes, cache hit/miss, or **auth denials (401/403) and
  reasons**. These need custom emission.
- **Per-request context for filtering.** Traces land as plain strings without
  **custom dimensions**, so you can't easily slice by directory, source, outcome, or
  latency in KQL.
- **Deliberately excluded (by policy, see §6):** raw **search query text**, result
  **titles/snippets**, and **user identity (oid/name)**.

## 4. Code changes required for effective logging
1. **Fix silent excepts (highest value).** Add `logging.warning`/`logging.exception`
   in the swallowing blocks — `storage.py` (download_blob, get_blob_etag,
   download_blob_with_etag, container create), `auth.py` (malformed principal →
   warning), `feeds` fallbacks. Without this, storage/auth failures are invisible.
2. **Structured custom dimensions.** Pass `extra={"custom_dimensions": {...}}` on key
   logs so they populate `customDimensions` for KQL. Minimal, no new dependency (the
   Functions App Insights logging handler maps it).
3. **Instrument the two hot paths:**
   - `/search`: one summary log per request with `directory`, `top`, `result_count`,
     `entitled_count`, `latency_ms`, `outcome` (200/401/403/500) — **not** the query
     text.
   - `refresh_job`: log the summary dict as dimensions (per-source counts/statuses,
     duration, `degraded`). It already returns this — just log it with dimensions.
4. **Auth outcomes:** log allow/deny with a reason (`no_principal`, `no_base_group`,
   `not_admin`) — counts only, no PII.
5. **(Optional) real dependency tracking.** To see Blob + data-source calls as
   `dependencies` (with duration/success), adopt the **Azure Monitor OpenTelemetry
   Distro** (`configure_azure_monitor`) or wrap calls in manual `track_dependency`.
   This is the one change that adds a dependency and warrants a scope decision.
6. **Sampling/level tuning** in `host.json` (keep exceptions/custom events unsampled;
   confirm info-level traces flow).

## 5. Alerts, dashboards, sample KQL
- **Alerts:** refresh failure/degraded; `/search` 5xx rate; p95 latency; **no
  successful refresh in 24h**; record-count drop per source; 403 spike (possible
  misconfig/abuse); exception rate.
- **Workbook:** search volume + latency, per-source record counts over time, refresh
  success timeline, entitlement usage.
- **Example KQL:**
  ```kusto
  // refreshes that ran degraded in the last 7d
  traces
  | where customDimensions.event == "refresh_summary"
  | where customDimensions.status == "degraded"

  // p95 search latency by day
  requests
  | where name == "search_directories"
  | summarize percentile(duration, 95) by bin(timestamp, 1d)
  ```

## 6. Privacy & security (important for this solution)
- **Never log restricted content or PII.** Result titles/snippets could be
  restricted-dataset content, and query text may describe sensitive research; user
  `oid`/`name` is PII. Log **counts, lengths, directory, outcome** instead. App
  Insights data lives in the customer tenant/workspace and should inherit its access
  controls + retention.
- Key Vault audit logs give **secret-access visibility** (who/what read the connector
  secret) — worth including.

## 7. Cost, retention, multi-cloud
- LA workspace retention (e.g., 30–90 days) + App Insights adaptive **sampling**
  manage cost; the `requests` exclusion already in `host.json` keeps invocation
  telemetry complete.
- All per-cloud via the connection string; LA/App Insights in the same cloud + region.

## 8. Decisions to confirm before implementing
1. **Dependency tracking:** adopt the OpenTelemetry distro (richer, one new dep) or
   stay with logging + KQL only (no new deps)? *(Recommend start without it.)*
2. **Retention** target (30/60/90 days) and any budget cap.
3. Confirm the **no-PII / no-content logging policy** (strongly recommended).
4. Scope of **diagnostic settings** (include Storage + Key Vault audit, or Function
   App only?).
