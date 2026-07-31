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
    """Fetch feeds → build snapshot → upload to blob. Returns a summary dict.

    A single failing feed no longer sinks the whole refresh: each feed falls
    back to its last cached snapshot, and the corpus is rebuilt from whatever
    data is available. Only when *both* feeds are unavailable (live fetch
    failed and no cache exists) does this raise.
    """
    started = time.time()
    logging.info("Refresh: fetching feeds…")
    pubs, pub_status = feeds.load_feed_resilient(feeds.PUBLICATIONS, "publications.json")
    projs, proj_status = feeds.load_feed_resilient(feeds.PROJECTS, "projects.json")

    if pubs is None and projs is None:
        raise RuntimeError(
            "Both feeds unavailable: live fetch failed and no cached snapshot "
            f"exists (publications={pub_status}, projects={proj_status}).")

    pubs = pubs or []
    projs = projs or []

    logging.info("Refresh: building snapshot (%d pubs, %d projects)…",
                 len(pubs), len(projs))
    data, n = search_core.build_artifact_bytes(pubs, projs)

    logging.info("Refresh: uploading %d bytes to blob…", len(data))
    etag = storage.upload_corpus(data)

    warnings = [f"{name} feed used {status} data"
                for name, status in (("publications", pub_status),
                                     ("projects", proj_status))
                if status != "live"]

    summary = {
        "records": n,
        "publications": len(pubs),
        "projects": len(projs),
        "publications_source": pub_status,
        "projects_source": proj_status,
        "bytes": len(data),
        "etag": etag,
        "seconds": round(time.time() - started, 1),
    }
    if warnings:
        summary["status"] = "degraded"
        summary["warnings"] = warnings
    logging.info("Refresh complete: %s", summary)
    return summary
