"""
Refresh job: ingest every registered source, build a per-dataset search index, and
upload each to Blob Storage along with a manifest. Called by the timer trigger and
the on-demand HTTP endpoint in function_app.py.

Per-dataset design: each dataset gets its own index blob (`index/<key>.pkl`) so the
Function can load only the indexes a caller is entitled to. A single source being
unavailable degrades gracefully — its previous index blob is left in place (carried).
A manifest (`index/manifest.json`) lists every dataset with its name and record
count for the /health endpoint.

Resilience order per source: live fetch+normalize (cached to its per-source docs
blob) -> per-source docs cache -> keep the previous per-dataset index blob.
"""
import datetime
import json
import logging
import time

import feeds
import search_core
import storage
from sources import get_enabled_sources


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def rebuild_and_upload():
    """Ingest all sources -> per-dataset indexes + manifest. Returns a summary."""
    started = time.time()
    sources = get_enabled_sources()
    logging.info("Refresh: ingesting %d sources into per-dataset indexes…", len(sources))

    manifest = {"total": 0, "datasets": []}
    statuses = {}

    for src in sources:
        docs, status = feeds.load_source_resilient(src)
        count = 0
        if docs is None:
            # Keep the previous index blob for this dataset (carry it forward).
            prev, _ = storage.download_index(src.key)
            if prev:
                try:
                    slim, _tokens = search_core.load_artifact(prev)
                    count = len(slim)
                    status = "carried"
                except Exception:  # noqa: BLE001
                    logging.exception("Could not read previous index for %s", src.key)
                    status = "failed"
            else:
                status = "failed"
        else:
            data, count = search_core.build_artifact_bytes(docs)
            storage.upload_index(src.key, data)

        statuses[src.key] = status
        manifest["total"] += count
        manifest["datasets"].append({
            "key": src.key,
            "name": src.label,
            "classification": getattr(src, "classification", "public"),
            "count": count,
            "status": status,
            "updated_at": _now_iso(),
        })

    storage.upload_manifest(json.dumps(manifest).encode("utf-8"))

    warnings = [f"{k} used {v} data" for k, v in statuses.items() if v != "live"]
    summary = {
        "records": manifest["total"],
        "datasets": {d["key"]: d["count"] for d in manifest["datasets"]},
        "sources": statuses,
        "seconds": round(time.time() - started, 1),
    }
    if warnings:
        summary["status"] = "degraded"
        summary["warnings"] = warnings
    logging.info("Refresh complete: %s", summary)
    return summary
