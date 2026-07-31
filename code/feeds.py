"""
Fetching the two public All of Us JSON feeds.

Shared by build_index.py (local build) and the Azure Function refresh jobs.
Includes a retry loop and an optional local-file fallback used only when a
local data directory is supplied (handy for offline local builds).
"""
import logging
import json
import os
import urllib.request
import storage

PUBLICATIONS = "https://www.researchallofus.org/wp-json/rh-data-caching/publications-report"
PROJECTS = "https://www.researchallofus.org/wp-json/rh-data-caching/projects"


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



# def load_feed(url, local_name=None, local_dir=None):
#     """Fetch a feed; if it fails and a local copy exists, use that."""
#     try:
#         return fetch(url)
#     except Exception as exc:  # noqa: BLE001
#         if local_dir and local_name:
#             local = os.path.join(local_dir, local_name)
#             if os.path.exists(local):
#                 logging.warning("  live fetch failed (%s); using local %s", exc, local)
#                 with open(local, "r", encoding="utf-8") as fh:
#                     return json.load(fh)
#         raise

def load_feed(url, blob_name=None):
    try:
        logging.info("Fetching feed %s…", url)
        data = fetch(url)
        if blob_name:
            storage.upload_blob(blob_name, json.dumps(data).encode("utf-8"))
        return data
    except Exception as exc:
        if blob_name:
            raw = storage.download_blob(blob_name)
            if raw:
                logging.warning("  live fetch failed (%s); using blob %s", exc, blob_name)
                return json.loads(raw)
        raise


def fetch_all(local_dir=None):
    """Return (publications, projects) as parsed JSON lists."""
    pubs = load_feed(PUBLICATIONS, "publications.json")
    projs = load_feed(PROJECTS, "projects.json")
    return pubs, projs

