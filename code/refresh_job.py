"""
Refresh job: ingest every registered source, rebuild the corpus snapshot, and
upload it to Blob Storage. Called by the timer trigger and the on-demand HTTP
endpoint in function_app.py.

Resilience: each source is ingested independently. A source that fails live
falls back to its cached normalized docs; if even that is missing, its records
are carried over from the previous corpus snapshot. The refresh only raises when
NO source yields any data anywhere.
"""
import logging
import os
import time

import feeds
import search_core
import storage
from sources import get_enabled_sources


def _previous_artifact():
    """Bytes of the current corpus snapshot: blob first, else the packaged file."""
    data, _ = storage.download_corpus()
    if data:
        return data
    path = os.path.join(os.path.dirname(__file__), "corpus.pkl")
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read()
    return None


def rebuild_and_upload():
    """Ingest all sources → build snapshot → upload to blob. Returns a summary."""
    started = time.time()
    sources = get_enabled_sources()
    logging.info("Refresh: ingesting %d sources…", len(sources))

    all_docs = []
    carried_docs, carried_tokens = [], []
    statuses = {}
    prev = None

    for src in sources:
        docs, status = feeds.load_source_resilient(src)
        if docs is None:
            # Last resort: carry this source's records from the previous snapshot.
            if prev is None:
                prev = _previous_artifact()
            if prev:
                d, t = search_core.carry_source(prev, src.key)
                if d:
                    carried_docs += d
                    carried_tokens += t
                    status = "carried"
            statuses[src.key] = status
            continue
        all_docs += docs
        statuses[src.key] = status

    if not (all_docs or carried_docs):
        raise RuntimeError(f"No data available from any source: {statuses}")

    logging.info("Refresh: building snapshot (%d live/cache docs, %d carried)…",
                 len(all_docs), len(carried_docs))
    data, n = search_core.build_artifact_bytes(all_docs, carried_docs, carried_tokens)

    logging.info("Refresh: uploading %d bytes to blob…", len(data))
    etag = storage.upload_corpus(data)

    counts = {}
    for d in all_docs:
        counts[d.get("source")] = counts.get(d.get("source"), 0) + 1
    for d in carried_docs:
        counts[d.get("source")] = counts.get(d.get("source"), 0) + 1

    warnings = [f"{key} used {status} data"
                for key, status in statuses.items() if status != "live"]

    summary = {
        "records": n,
        "counts": counts,
        "sources": statuses,
        "bytes": len(data),
        "etag": etag,
        "seconds": round(time.time() - started, 1),
    }
    if warnings:
        summary["status"] = "degraded"
        summary["warnings"] = warnings
    logging.info("Refresh complete: %s", summary)
    return summary
