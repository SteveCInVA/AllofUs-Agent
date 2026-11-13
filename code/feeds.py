"""
HTTP fetching and per-source resilient ingestion.

Failed fetches leave the source's existing Azure AI Search documents untouched.
"""
import json
import logging
import urllib.request

import httpx

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
    logging.info("Retrying %s with an HTTP/2-capable client.", url)
    try:
        with httpx.Client(http2=True, headers={"User-Agent": "Mozilla/5.0"},
                          timeout=timeout) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.json()
    except Exception as exc:  # noqa: BLE001
        logging.warning("  HTTP/2 fallback failed: %s", exc)
    raise last


def load_source_resilient(source):
    """Return fresh normalized documents, or (None, 'failed') without corpus writes."""
    try:
        raw = source.fetch()
        docs = source.normalize(raw)
        if not isinstance(docs, list):
            raise ValueError(f"Source '{source.key}' did not normalize to a list.")
        return docs, "live"
    except Exception as exc:  # noqa: BLE001
        logging.warning("  live ingest failed for %s: %s", source.key, exc)
        return None, "failed"
