# Architecture — All of Us Research Finder

A Copilot Studio agent that finds similar research and datasets across four public
sources (All of Us publications & projects, IHCC cohorts, CCDI datasets) and
returns ranked matches with clickable source links. The backend is an Azure
Functions (Python v2) app that keeps a BM25 search index over a normalized,
refreshable corpus.

> **PNG exports** of every diagram (for non-GitHub viewers) are in
> [`docs/diagrams/`](diagrams/); the `.mmd` sources there can be re-rendered with
> `mmdc -i <file>.mmd -o <file>.png -b white -s 2`.

---

## 1. End-to-end context

```mermaid
flowchart LR
    U["User"] --> AG["Copilot Studio agent"]
    AG --> CC["Custom connector<br/>searchDirectories"]
    CC -->|"GET /api/search"| FN["Azure Function"]
    FN --> RES["Ranked matches<br/>title + snippet + source URL"]

    FN -->|"pulls on refresh"| SRC

    subgraph SRC["Public data sources"]
      direction TB
      P1["All of Us Publications"]
      P2["All of Us Projects"]
      P3["IHCC Cohorts (GitHub JSON)"]
      P4["CCDI Federation (REST)"]
    end
```

*Left-to-right request pipeline; the data sources hang off the Function on a
single ingest edge so nothing crosses the request path.*

---

## 2. Module map (dependencies)

Layered top-to-bottom: entry point → orchestration → registry → source modules →
shared libraries. Each source module (`source_allofus`, `source_ihcc`,
`source_ccdi`) uses the same shared libraries, so those are drawn as one edge from
the group instead of nine crossing edges.

```mermaid
flowchart TB
    FA["function_app.py<br/>routes /search /refresh /health + timer"]

    subgraph ORCH["Orchestration"]
      direction LR
      RJ["refresh_job.py"]
      BI["build_index.py"]
    end

    REG["sources.py<br/>registry"]

    subgraph SRCMODS["Source modules"]
      direction LR
      SA["source_allofus"]
      SI["source_ihcc"]
      SD["source_ccdi"]
    end

    subgraph SHARED["Shared libraries"]
      direction LR
      SC["search_core.py"]
      FEED["feeds.py"]
      ST["storage.py"]
      SB["source_base.py"]
    end

    FA --> ORCH
    FA --> REG
    RJ --> REG
    REG --> SRCMODS

    SRCMODS --> SB
    SRCMODS --> FEED
    SRCMODS --> SC

    ORCH --> SHARED
    FA --> SHARED
    FEED --> ST
```

---

## 3. Search request flow (three-layer cache)

```mermaid
flowchart TB
    Q["GET /api/search"] --> ENG{"engine cached for<br/>caller's entitlements?"}

    ENG -- "no, first call" --> L1{"entitled indexes<br/>in Blob?"}
    L1 -- "yes" --> LB["load index/&lt;key&gt;.pkl<br/>for entitled datasets"]
    L1 -- "no" --> LP["no data yet<br/>(await /api/refresh)"]

    ENG -- "yes" --> STALE{"any component<br/>ETag changed?"}
    STALE -- "yes" --> RB["reload changed index"]
    STALE -- "no" --> USE["use merged engine"]

    LB --> USE
    LP --> USE
    RB --> USE

    USE --> SRCH["BM25 score + rank"]
    SRCH --> FLT["filter by source"]
    FLT --> TOP["top N + score,<br/>drop empty fields"]
    TOP --> J["JSON results<br/>title + snippet + url"]
```

Layers: **(1)** in-memory per-entitlement merged engine per worker, **(2)** per-dataset
Blob indexes (`index/<key>.pkl`, refreshable without redeploy; ETags re-checked every
`CORPUS_CHECK_SECONDS`). Deployed packages ship no indexes — Blob is populated by the
first `/api/refresh` or the daily timer (a local `index/<key>.pkl` is a dev-only
fallback). `_body` is never returned.

---

## 4. Refresh flow (per-source resilience)

```mermaid
flowchart TB
    T["Timer 03:00 UTC"] --> RB["rebuild_and_upload"]
    H["POST /api/refresh"] --> RB
    RB --> LOOP["for each registered source"]

    LOOP --> F{"live fetch<br/>+ normalize"}
    F -- "ok" --> C1["cache to blob<br/>status = live"]
    F -- "fail" --> CACHE{"blob cache?"}
    CACHE -- "yes" --> C2["use cache<br/>status = cache"]
    CACHE -- "no" --> CARRY{"in previous<br/>corpus?"}
    CARRY -- "yes" --> C3["carry forward<br/>status = carried"]
    CARRY -- "no" --> C4["omitted<br/>status = failed"]

    C1 --> AGG["collect docs"]
    C2 --> AGG
    C3 --> AGG

    AGG --> BUILD["build artifact<br/>tokenize + drop _body"]
    BUILD --> UP["upload corpus to Blob"]
    UP --> INV["invalidate in-memory<br/>+ hot-reload via ETag"]
```

**Resilience guarantee:** a single source being down (IHCC atlas in maintenance,
a CCDI node timing out) never blanks the corpus — it falls back to its last cache,
then to carry-forward from the previous snapshot.

---

## 5. Record lifecycle (data shape)

```mermaid
flowchart LR
    RAW["raw source data<br/>JSON / REST"] --> NORM["normalized record<br/>id, source, record_type,<br/>title, url, snippet, _body,<br/>+ source fields"]
    NORM --> STORE["stored record<br/>(minus _body)"]
    NORM --> TOK["BM25 tokens<br/>from _body"]
    STORE --> CORP["index/&lt;key&gt;.pkl<br/>docs + tokens"]
    TOK --> CORP
    CORP --> HIT["search result<br/>record + score"]
```

Every record shares the required keys `id, source, record_type, title, url,
snippet, _body`; `_body` is the searchable blob (tokenized then dropped). Source
modules add their own fields (authors, countries, diseases, organization, ...).
