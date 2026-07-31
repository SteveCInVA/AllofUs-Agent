"""
Azure Functions (Python v2) API for the NIH All of Us "find similar work" agent.

Endpoints:
  GET|POST /api/search    Copilot Studio custom action — find similar records.
  POST     /api/refresh   On-demand: rebuild the corpus snapshot now.
  GET      /api/health    Record count + snapshot source.
Timer:
  refresh_timer           Daily (03:00 UTC) — rebuild the corpus snapshot.

The searchable corpus is cached in three layers:
  1. In-memory per worker (fast path for every request).
  2. Azure Blob Storage snapshot (refreshable without redeploying).
  3. Packaged corpus.pkl (first-run fallback if the blob doesn't exist yet).
Workers re-check the blob ETag every CORPUS_CHECK_SECONDS and hot-reload when a
refresh has produced a new snapshot.
"""
import json
import logging
import os
import time

import azure.functions as func

from search_core import load_engine, load_engine_from_bytes, search
import storage
from refresh_job import rebuild_and_upload

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

_DOCS = None
_BM25 = None
_ETAG = None          # ETag of the loaded blob snapshot (None if packaged file)
_SOURCE = None        # "blob" | "package" — for diagnostics
_LAST_CHECK = 0.0
CHECK_INTERVAL = int(os.environ.get("CORPUS_CHECK_SECONDS", "300"))


def _invalidate():
    """Force the next _engine() call to reload the snapshot."""
    global _DOCS, _BM25, _ETAG, _SOURCE, _LAST_CHECK
    _DOCS = _BM25 = _ETAG = _SOURCE = None
    _LAST_CHECK = 0.0


def _load_from_blob():
    global _DOCS, _BM25, _ETAG, _SOURCE
    data, etag = storage.download_corpus()
    if data is None:
        return False
    _DOCS, _BM25 = load_engine_from_bytes(data)
    _ETAG, _SOURCE = etag, "blob"
    logging.info("Loaded corpus from blob: %d records (etag=%s)", len(_DOCS), etag)
    return True


def _load_from_package():
    global _DOCS, _BM25, _ETAG, _SOURCE
    path = os.path.join(os.path.dirname(__file__), "corpus.pkl")
    _DOCS, _BM25 = load_engine(path)
    _ETAG, _SOURCE = None, "package"
    logging.info("Loaded corpus from packaged file: %d records", len(_DOCS))


def _engine():
    """Return (docs, bm25), loading and hot-reloading the snapshot as needed."""
    global _LAST_CHECK
    now = time.time()
    if _DOCS is None:
        if not _load_from_blob():
            _load_from_package()
        _LAST_CHECK = now
    elif now - _LAST_CHECK > CHECK_INTERVAL:
        _LAST_CHECK = now
        try:
            etag = storage.get_corpus_etag()
            if etag and etag != _ETAG:
                logging.info("New snapshot detected (etag %s -> %s); reloading.", _ETAG, etag)
                _load_from_blob()
        except Exception:  # noqa: BLE001
            logging.exception("Snapshot staleness check failed; keeping current data.")
    return _DOCS, _BM25


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
    directory = (req.params.get("directory") or body.get("directory") or "both").lower()
    try:
        top = int(req.params.get("top") or body.get("top") or 8)
    except (TypeError, ValueError):
        top = 8
    top = max(1, min(top, 25))

    if not query:
        return func.HttpResponse(
            json.dumps({"error": "The 'query' parameter is required."}),
            status_code=400, mimetype="application/json")

    if directory not in ("publication", "project", "both"):
        directory = "both"

    try:
        docs, bm25 = _engine()
        results = search(docs, bm25, query, directory, top)
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
    """Scheduled rebuild of the corpus snapshot — runs daily at 03:00 UTC."""
    if getattr(timer, "past_due", False):
        logging.warning("Timer past due; running refresh now.")
    try:
        rebuild_and_upload()
        _invalidate()  # this worker reloads on next search; others via ETag check
    except Exception:  # noqa: BLE001
        logging.exception("Scheduled refresh failed.")


@app.route(route="refresh", methods=["POST"])
def refresh_now(req: func.HttpRequest) -> func.HttpResponse:
    """On-demand rebuild of the corpus snapshot (function-key protected)."""
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
    docs, _ = _engine()
    return func.HttpResponse(
        json.dumps({"status": "ok", "records": len(docs), "source": _SOURCE}),
        mimetype="application/json")
