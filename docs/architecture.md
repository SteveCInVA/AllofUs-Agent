# Architecture — All of Us Research Finder

A Copilot Studio agent that finds similar research and datasets across four public
sources (All of Us publications & projects, IHCC cohorts, CCDI datasets) and
returns ranked matches with clickable source links. The backend is an Azure
Functions (Python v2) app that keeps a BM25 search index over a normalized,
refreshable corpus.

---

## 1. End-to-end context

```mermaid
flowchart LR
    U["User"] --> AG["Copilot Studio agent"]
    AG --> CC["Power Platform custom connector<br/>operation: searchDirectories"]
    CC -->|"GET /api/search<br/>x-functions-key"| FN["Azure Function"]
    FN --> RES["Ranked matches<br/>title + snippet + source URL"]
    RES --> AG --> U

    subgraph SRC["Public data sources"]
      P1["All of Us Publications<br/>WordPress JSON"]
      P2["All of Us Projects<br/>WordPress JSON"]
      P3["IHCC Cohort Atlas<br/>Apache-2.0 GitHub JSON"]
      P4["CCDI Federation<br/>REST API - study level"]
    end
    SRC -.ingested on refresh.-> FN
```

---

## 2. Module map (what depends on what)

```mermaid
flowchart TD
    subgraph HOST["Azure Functions host"]
      FA["function_app.py<br/>HTTP: /search /refresh /health<br/>timer: daily refresh<br/>in-memory cache + hot-reload"]
    end

    FA --> RJ["refresh_job.py<br/>orchestrates a rebuild"]
    FA --> SC["search_core.py<br/>build artifact + BM25 search"]
    FA --> ST["storage.py<br/>Blob: corpus + per-source cache"]
    FA --> SRCS["sources.py<br/>source registry"]

    RJ --> SRCS
    RJ --> FEED["feeds.py<br/>HTTP fetch + resilient per-source ingest"]
    RJ --> SC
    RJ --> ST

    SRCS --> SA["source_allofus.py"]
    SRCS --> SI["source_ihcc.py"]
    SRCS --> SD["source_ccdi.py"]

    SA --> SB["source_base.py<br/>Source dataclass"]
    SI --> SB
    SD --> SB
    SA --> FEED
    SI --> FEED
    SD --> FEED
    SA --> SC
    SI --> SC
    SD --> SC

    FEED --> ST

    BI["build_index.py<br/>local first-run corpus.pkl"] --> SRCS
    BI --> SC
```

**Roles**
- **function_app.py** — the only Functions entry point: 3 HTTP routes + 1 timer; owns the in-memory engine and hot-reload.
- **sources.py / source_base.py** — the registry and the `Source` abstraction (`key`, `label`, `fetch()`, `normalize()`). Adding a source = new `source_*.py` + one registry line.
- **source_allofus / source_ihcc / source_ccdi** — per-source fetch + normalize into the common record schema.
- **feeds.py** — HTTP `fetch()` and `load_source_resilient()` (cache to blob, fall back to cache).
- **search_core.py** — source-agnostic: builds the artifact (tokenize + drop `_body`) and runs BM25 search/filter.
- **storage.py** — Blob helpers for the corpus snapshot and per-source caches (managed identity or connection string).
- **refresh_job.py** — iterates the registry and assembles a new corpus with per-source resilience.
- **build_index.py** — builds the packaged `corpus.pkl` first-run fallback.

---

## 3. Search request flow (three-layer cache)

```mermaid
flowchart TD
    Q["GET /api/search?query=...&directory=all&top=8"] --> ENG{"in-memory<br/>corpus loaded?"}
    ENG -- "no" --> L1{"blob snapshot<br/>exists?"}
    L1 -- "yes" --> LB["load from Blob<br/>build BM25"]
    L1 -- "no" --> LP["load packaged corpus.pkl<br/>build BM25"]
    ENG -- "yes" --> STALE{"ETag changed<br/>since last check?"}
    STALE -- "yes" --> LB
    STALE -- "no" --> USE["use in-memory engine"]
    LB --> USE
    LP --> USE
    USE --> SRCH["search_core.search<br/>BM25 score + rank"]
    SRCH --> FLT["filter by source<br/>publication|project|ihcc|ccdi|all"]
    FLT --> TOP["take top N, add score,<br/>drop empty fields"]
    TOP --> J["JSON: results with title + snippet + url"]
```

Layers: **(1)** in-memory per worker (fast path), **(2)** Blob snapshot (refreshable
without redeploy; workers re-check the ETag every `CORPUS_CHECK_SECONDS`), **(3)**
packaged `corpus.pkl` (first-run fallback). `_body` is never returned.

---

## 4. Refresh flow (timer + on-demand, per-source resilience)

```mermaid
flowchart TD
    T["Timer 03:00 UTC"] --> RB["rebuild_and_upload"]
    H["POST /api/refresh"] --> RB
    RB --> LOOP["for each registered source"]

    LOOP --> F{"live fetch + normalize"}
    F -- "ok" --> C1["cache normalized docs to blob<br/>status = live"]
    F -- "fail" --> CACHE{"per-source<br/>blob cache?"}
    CACHE -- "yes" --> C2["use cached docs<br/>status = cache"]
    CACHE -- "no" --> CARRY{"records in<br/>previous corpus?"}
    CARRY -- "yes" --> C3["carry forward<br/>status = carried"]
    CARRY -- "no" --> C4["status = failed<br/>source omitted"]

    C1 --> AGG["collect docs"]
    C2 --> AGG
    C3 --> AGG
    AGG --> BUILD["build_artifact_bytes<br/>tokenize + drop _body"]
    BUILD --> UP["upload corpus to Blob"]
    UP --> INV["invalidate in-memory<br/>workers hot-reload via ETag"]
    UP --> SUM["summary: per-source counts + statuses<br/>degraded if any not live"]
```

**Resilience guarantee:** a single source being down (e.g. the IHCC atlas in
maintenance, or a CCDI node timing out) never blanks the corpus — it falls back to
its last cache, then to carry-forward from the previous snapshot.

---

## 5. Record lifecycle (data shape)

```mermaid
flowchart LR
    RAW["raw source data<br/>JSON / REST"] -->|"Source.normalize"| NORM["normalized record<br/>id, source, record_type,<br/>title, url, snippet, _body,<br/>+ source-specific fields"]
    NORM -->|"build_artifact_bytes"| STORE["stored record<br/>same, minus _body"]
    NORM -->|"tokenize _body"| TOK["BM25 tokens<br/>parallel list"]
    STORE --> CORP["corpus.pkl<br/>docs + tokens"]
    TOK --> CORP
    CORP -->|"search + rank"| HIT["search result<br/>stored record + score"]
```

Every record shares the required keys `id, source, record_type, title, url,
snippet, _body`; `_body` is the searchable blob (tokenized then dropped). Source
modules add their own fields (authors, countries, diseases, organization, ...).
