"""
Source abstraction for the multi-source ingestion pipeline.

Every data source (All of Us publications/projects, IHCC, CCDI, ...) is described
by one `Source`: a key, a human label, a `fetch()` that returns raw data, and a
`normalize(raw)` that turns raw data into a list of normalized record dicts.

A normalized record is a plain dict that MUST contain:
  - "id"      unique string id (prefix with the source key, e.g. "ihcc-123")
  - "source"  the source key (e.g. "publication", "ihcc")
  - "title"   display title
  - "url"     clickable source link
  - "_body"   the searchable text blob (dropped from the stored corpus; used only
              to build the BM25 tokens). Any other keys (snippet, date, focus,
              country, ...) are carried through to search results as-is.
"""
from dataclasses import dataclass, field
from typing import Callable, List, Dict, Any


@dataclass
class Source:
    key: str                                   # unique; also the record "source" value
    label: str                                 # human label, e.g. "Publication"
    fetch: Callable[[], Any]                   # () -> raw data (or raises)
    normalize: Callable[[Any], List[Dict]]     # raw -> [normalized record dicts]
    enabled: bool = True
    cache_blob: str = ""                        # per-source normalized-docs cache blob
    record_type: str = ""                       # optional label for the record kind
    # Applied from the DATASET_CLASSIFICATION app-setting (see sources.py), not
    # hardcoded here. "public" = gated by the base entitlement only; "restricted"
    # = additionally requires the Entra group in entitlement_group_id.
    classification: str = "public"
    entitlement_group_id: str = ""              # Entra group object id (restricted only)
    index_blob: str = ""                        # per-dataset index blob (index/<key>.pkl)

    def __post_init__(self):
        if not self.cache_blob:
            self.cache_blob = f"{self.key}.docs.json"
        if not self.record_type:
            self.record_type = self.label
        if not self.index_blob:
            self.index_blob = f"index/{self.key}.pkl"
