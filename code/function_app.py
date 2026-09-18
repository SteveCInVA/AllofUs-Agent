"""
Azure Functions (Python v2) API for the NIH All of Us "find similar work" agent.

Endpoints:
  GET|POST /api/search    Copilot Studio custom action — find similar records.
  POST     /api/refresh   On-demand: rebuild the per-dataset indexes now.
  GET      /api/health    Enumerate every dataset with name + record count.
Timer:
  refresh_timer           Daily (03:00 UTC) — rebuild the per-dataset indexes.

Per-dataset caching (two levels):
  1. Per-index cache  — {key: (docs, tokens, etag)} loaded from Blob (or the
     packaged code/index/<key>.pkl first-run fallback), hot-reloaded by ETag.
  2. Per-entitlement engine cache — {sorted(keys): (docs, bm25, component etags)} —
     a merged BM25 engine over exactly the datasets a caller may see. Most callers
     share one signature, so the merged engine is built once and reused.
"""
import json
import logging
import os
import time

import azure.functions as func

import search_core
import storage
from refresh_job import rebuild_and_upload
from sources import source_keys

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

CHECK_INTERVAL = int(os.environ.get("CORPUS_CHECK_SECONDS", "300"))

_index_cache = {}   # key -> {"docs": [...], "tokens": [...], "etag": str|None}
_engine_cache = {}  # tuple(sorted keys) -> {"docs": [...], "bm25": obj, "etags": {}}
_LAST_CHECK = 0.0


def _pkg_path(name):
    return os.path.join(os.path.dirname(__file__), "index", name)


def _invalidate():
    """Drop all caches so the next request reloads indexes."""
    global _LAST_CHECK
    _index_cache.clear()
    _engine_cache.clear()
    _LAST_CHECK = 0.0


def _load_index(key):
    """Load one dataset's index into the per-index cache (blob, else packaged)."""
    data, etag = storage.download_index(key)
    if data is None:
        p = _pkg_path(f"{key}.pkl")
        if os.path.exists(p):
            with open(p, "rb") as fh:
                data, etag = fh.read(), None
        else:
            return None
    docs, tokens = search_core.load_artifact(data)
    _index_cache[key] = {"docs": docs, "tokens": tokens, "etag": etag}
    logging.info("Loaded index %s: %d records (etag=%s)", key, len(docs), etag)
    return _index_cache[key]


def _maybe_refresh(keys):
    """Throttled ETag check; reload changed indexes and drop the engine cache."""
    global _LAST_CHECK
    now = time.time()
    if now - _LAST_CHECK <= CHECK_INTERVAL:
        return
    _LAST_CHECK = now
    changed = False
    for key in keys:
        entry = _index_cache.get(key)
        if entry is None:
            continue
        try:
            etag = storage.get_index_etag(key)
        except Exception:  # noqa: BLE001
            logging.exception("ETag check failed for %s; keeping current index.", key)
            continue
        if etag and etag != entry.get("etag"):
            logging.info("Index %s changed (etag %s -> %s); reloading.",
                         key, entry.get("etag"), etag)
            _load_index(key)
            changed = True
    if changed:
        _engine_cache.clear()


def _engine(keys):
    """Return a merged (docs, bm25) engine over the given dataset keys."""
    keys = tuple(sorted(keys))
    _maybe_refresh(keys)
    for key in keys:
        if key not in _index_cache:
            _load_index(key)
    present = [k for k in keys if k in _index_cache]
    etags = {k: _index_cache[k]["etag"] for k in present}
    cached = _engine_cache.get(keys)
    if cached is not None and cached["etags"] == etags:
        return cached["docs"], cached["bm25"]
    parts = [(_index_cache[k]["docs"], _index_cache[k]["tokens"]) for k in present]
    docs, bm25 = search_core.build_engine(parts)
    _engine_cache[keys] = {"docs": docs, "bm25": bm25, "etags": etags}
    return docs, bm25


def _entitled_keys(req):
    """Datasets this caller may search. Phase 1: all datasets (no auth yet)."""
    return source_keys()


def _load_manifest():
    raw = storage.download_manifest()
    if raw is None:
        p = _pkg_path("manifest.json")
        if os.path.exists(p):
            with open(p, "rb") as fh:
                raw = fh.read()
    if raw is None:
        return {"total": 0, "datasets": []}
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return {"total": 0, "datasets": []}


# --------------------------------------------------------------------- search

@app.route(route="search", methods=["GET", "POST"])
def search_directories(req: func.HttpRequest) -> func.HttpResponse:
    body = {}
    if req.method == "POST":
        try:
            body = req.get_json()
        except ValueError:
            body = {}

    query = (req.params.get("query") or body.get("query") or "").strip()
    directory = (req.params.get("directory") or body.get("directory")
                 or req.params.get("source") or body.get("source") or "both").lower()
    try:
        top = int(req.params.get("top") or body.get("top") or 8)
    except (TypeError, ValueError):
        top = 8
    top = max(1, min(top, 25))

    if not query:
        return func.HttpResponse(
            json.dumps({"error": "The 'query' parameter is required."}),
            status_code=400, mimetype="application/json")

    allowed = set(source_keys()) | {"publication", "project", "publications",
                                    "projects", "both", "all"}
    if directory not in allowed:
        directory = "both"

    try:
        keys = _entitled_keys(req)
        docs, bm25 = _engine(keys)
        results = search_core.search(docs, bm25, query, directory, top)
    except Exception as exc:  # noqa: BLE001
        logging.exception("search failed")
        return func.HttpResponse(
            json.dumps({"error": f"search failed: {exc}"}),
            status_code=500, mimetype="application/json")

    payload = {
        "query": query,
        "directory": directory,
        "count": len(results),
        "results": results,
    }
    return func.HttpResponse(json.dumps(payload), mimetype="application/json")


# ---------------------------------------------------------- refresh (2 triggers)

@app.timer_trigger(schedule="0 0 3 * * *", arg_name="timer",
                   run_on_startup=False, use_monitor=True)
def refresh_timer(timer: func.TimerRequest) -> None:
    """Scheduled rebuild of the per-dataset indexes — runs daily at 03:00 UTC."""
    if getattr(timer, "past_due", False):
        logging.warning("Timer past due; running refresh now.")
    try:
        rebuild_and_upload()
        _invalidate()
    except Exception:  # noqa: BLE001
        logging.exception("Scheduled refresh failed.")


@app.route(route="refresh", methods=["POST"])
def refresh_now(req: func.HttpRequest) -> func.HttpResponse:
    """On-demand rebuild of the per-dataset indexes (function-key protected)."""
    try:
        summary = rebuild_and_upload()
        _invalidate()
        return func.HttpResponse(
            json.dumps({"status": "refreshed", **summary}),
            mimetype="application/json")
    except Exception as exc:  # noqa: BLE001
        logging.exception("Manual refresh failed.")
        return func.HttpResponse(
            json.dumps({"status": "error", "error": str(exc)}),
            status_code=500, mimetype="application/json")


# --------------------------------------------------------------------- health

@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
    manifest = _load_manifest()
    return func.HttpResponse(
        json.dumps({"status": "ok", **manifest}),
        mimetype="application/json")
