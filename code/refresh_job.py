"""Refresh registered source documents in Azure AI Search without corpus Blob files."""
import logging
import time

import feeds
import storage
from sources import get_enabled_sources


def rebuild_and_upload(sources=None):
    """Preserve the refresh summary while importing directly into the shared Search index."""
    started = time.time()
    if sources is None:
        sources = get_enabled_sources()
    logging.info("Refresh: ingesting %d sources into Azure AI Search.", len(sources))
    counts, statuses = {}, {}
    for source in sources:
        docs, status = feeds.load_source_resilient(source)
        if docs is None:
            count = storage.dataset_info(source)["count"]
            status = "carried" if count else "failed"
        else:
            count = storage.replace_dataset(source, docs)
        counts[source.key] = count
        statuses[source.key] = status

    summary = {
        "records": sum(counts.values()), "datasets": counts, "sources": statuses,
        "seconds": round(time.time() - started, 1),
    }
    warnings = [f"{key} used {status} data" for key, status in statuses.items() if status != "live"]
    if warnings:
        summary.update(status="degraded", warnings=warnings)
    logging.info("Refresh complete: %s", summary)
    return summary
