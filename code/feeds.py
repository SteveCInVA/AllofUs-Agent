"""
HTTP fetching and per-source resilient ingestion.

`fetch` is a small retrying JSON GET used by simple sources. `load_source_resilient`
runs a source's fetch+normalize, caches the normalized records to Blob Storage,
and — on any failure — falls back to that cached copy so a single source being
down never blanks its slice of the corpus.
"""
import json
import logging
import urllib.request

import storage


def fetch(url, retries=3, timeout=180):
    last = None
    for attempt in range(retries):
        logging.info("Fetching %s (attempt %d/%d)…", url, attempt + 1, retries)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except Exception as exc:  # noqa: BLE001
            last = exc
            logging.warning("  fetch attempt %d failed: %s", attempt + 1, exc)
    raise last


def load_source_resilient(source):
    """Fetch + normalize one source, caching normalized docs to blob.

    Returns (docs, status) where status is:
      "live"   fresh fetch succeeded (and was cached)
      "cache"  fetch/normalize failed; served the last cached normalized docs
      "failed" no live data and no cache (docs is None)
    """
    try:
        raw = source.fetch()
        docs = source.normalize(raw)
        try:
            storage.upload_blob(source.cache_blob,
                                json.dumps(docs).encode("utf-8"))
        except Exception as exc:  # noqa: BLE001  (caching is best-effort)
            logging.warning("  could not cache %s: %s", source.cache_blob, exc)
        return docs, "live"
    except Exception as exc:  # noqa: BLE001
        logging.warning("  live ingest failed for %s: %s", source.key, exc)
        raw = storage.download_blob(source.cache_blob)
        if raw:
            try:
                docs = json.loads(raw)
                storage.upload_blob(source.cache_blob, raw)  # carry cache forward
                logging.warning("  using cached docs for %s (%d records)",
                                source.key, len(docs))
                return docs, "cache"
            except Exception as exc2:  # noqa: BLE001
                logging.warning("  cached docs for %s unreadable: %s",
                                source.key, exc2)
        logging.error("  no data available for %s", source.key)
        return None, "failed"
