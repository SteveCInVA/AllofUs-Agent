"""
Azure Functions (Python v2) API for the NIH All of Us "find similar work" agent.

Endpoints:
  GET|POST /api/search    Copilot Studio custom action — find similar records.
  POST     /api/refresh   On-demand: import source records into Azure AI Search.
  GET      /api/health    Enumerate every dataset with name + record count.
Timer:
  refresh_timer           Daily (03:00 UTC) — refresh the shared Search index.
"""
import json
import logging

import azure.functions as func

import storage
import auth
from refresh_job import rebuild_and_upload
from search_core import resolve_filter
from sources import source_keys

# Routes are ANONYMOUS at the Functions layer; App Service Authentication ("Easy
# Auth") gates access with an Entra token and the code enforces entitlements from
# the caller's claims (see auth.py). /api/health is intentionally anonymous.
app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

def _err(status, message):
    return func.HttpResponse(json.dumps({"error": message}),
                             status_code=status, mimetype="application/json")


def _authorize_search(req):
    """Return (error_response or None, entitled_keys). Enforced only when AUTH_ENFORCED."""
    if not auth.enforced():
        return None, source_keys()
    principal = auth.get_principal(req)
    if principal is None:
        return _err(401, "Authentication required."), None
    if not auth.has_base(principal):
        return _err(403, "Not entitled to use this service."), None
    return None, auth.entitled_keys(principal)


# --------------------------------------------------------------------- search

@app.route(route="search", methods=["GET", "POST"])
def search_directories(req: func.HttpRequest) -> func.HttpResponse:
    """Search the shared Azure index, mapping directory selection to entitled source keys."""
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

    err, keys = _authorize_search(req)
    if err is not None:
        return err

    requested_source = resolve_filter(directory)
    selected_keys = [key for key in keys if requested_source is None or key == requested_source]
    try:
        results = storage.search_records(
            query, selected_keys, top=top, enforce_security=auth.enforced())
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
    """Scheduled Search import, daily at 03:00 UTC."""
    if getattr(timer, "past_due", False):
        logging.warning("Timer past due; running refresh now.")
    try:
        rebuild_and_upload()
    except Exception:  # noqa: BLE001
        logging.exception("Scheduled refresh failed.")


@app.route(route="refresh", methods=["POST"])
def refresh_now(req: func.HttpRequest) -> func.HttpResponse:
    """On-demand Search import. Requires the Admin role when enforced."""
    if auth.enforced():
        principal = auth.get_principal(req)
        if principal is None:
            return _err(401, "Authentication required.")
        if not auth.is_admin(principal):
            return _err(403, "Admin role required to refresh.")
    try:
        summary = rebuild_and_upload()
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
    """Return anonymous source summaries from Search, never indexed document content."""
    try:
        manifest = storage.load_manifest()
    except Exception:  # noqa: BLE001
        logging.exception("Health check could not query Azure AI Search.")
        return _err(503, "Azure AI Search is unavailable.")
    return func.HttpResponse(
        json.dumps({"status": "ok", **manifest}),
        mimetype="application/json")
