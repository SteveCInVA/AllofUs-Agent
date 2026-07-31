"""
Shared refresh job: fetch the live feeds, rebuild the corpus snapshot, and
upload it to Blob Storage. Called by both the timer trigger and the on-demand
HTTP endpoint in function_app.py.
"""
import logging
import os
import time

import feeds
import search_core
import storage


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
    """Fetch feeds → build snapshot → upload to blob. Returns a summary dict.

    A single failing feed no longer sinks the whole refresh, nor does it shrink
    the corpus: each feed falls back to its cached raw snapshot, and if even
    that is missing its already-normalized records are carried over from the
    previous corpus snapshot. Only when no data is available anywhere does this
    raise.
    """
    started = time.time()
    logging.info("Refresh: fetching feeds…")
    pubs, pub_status = feeds.load_feed_resilient(feeds.PUBLICATIONS, "publications.json")
    projs, proj_status = feeds.load_feed_resilient(feeds.PROJECTS, "projects.json")

    # Carry any fully-unavailable feed's records over from the previous snapshot
    # so one failing feed can't drop records (and the health count) from the corpus.
    carried_docs, carried_tokens = [], []
    if pubs is None or projs is None:
        prev = _previous_artifact()
        if prev:
            if pubs is None:
                d, t = search_core.carry_source(prev, "publication")
                carried_docs += d
                carried_tokens += t
                pub_status = "carried" if d else pub_status
            if projs is None:
                d, t = search_core.carry_source(prev, "project")
                carried_docs += d
                carried_tokens += t
                proj_status = "carried" if d else proj_status

    if not (pubs or projs or carried_docs):
        raise RuntimeError(
            "No data available: live fetch failed, no cached feed, and no "
            f"previous snapshot (publications={pub_status}, projects={proj_status}).")

    pubs = pubs or []
    projs = projs or []
    logging.info("Refresh: building snapshot (%d live pubs, %d live projects, %d carried)…",
                 len(pubs), len(projs), len(carried_docs))
    data, n = search_core.build_artifact_bytes(pubs, projs, carried_docs, carried_tokens)

    logging.info("Refresh: uploading %d bytes to blob…", len(data))
    etag = storage.upload_corpus(data)

    pub_count = len(pubs) + sum(1 for d in carried_docs if d.get("source") == "publication")
    proj_count = len(projs) + sum(1 for d in carried_docs if d.get("source") == "project")
    warnings = [f"{name} feed used {status} data"
                for name, status in (("publications", pub_status),
                                     ("projects", proj_status))
                if status != "live"]

    summary = {
        "records": n,
        "publications": pub_count,
        "projects": proj_count,
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
