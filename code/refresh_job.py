"""
Shared refresh job: fetch the live feeds, rebuild the corpus snapshot, and
upload it to Blob Storage. Called by both the timer trigger and the on-demand
HTTP endpoint in function_app.py.
"""
import logging
import time

import feeds
import search_core
import storage


def rebuild_and_upload():
    """Fetch feeds → build snapshot → upload to blob. Returns a summary dict."""
    started = time.time()
    logging.info("Refresh: fetching feeds…")
    pubs, projs = feeds.fetch_all()

    logging.info("Refresh: building snapshot (%d pubs, %d projects)…",
                 len(pubs), len(projs))
    data, n = search_core.build_artifact_bytes(pubs, projs)

    logging.info("Refresh: uploading %d bytes to blob…", len(data))
    etag = storage.upload_corpus(data)

    summary = {
        "records": n,
        "publications": len(pubs),
        "projects": len(projs),
        "bytes": len(data),
        "etag": etag,
        "seconds": round(time.time() - started, 1),
    }
    logging.info("Refresh complete: %s", summary)
    return summary
