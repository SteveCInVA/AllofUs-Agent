"""
CCDI Federation Resource API source (Childhood Cancer Data Initiative).

We ingest at STUDY/NAMESPACE level (redistribution of study-level metadata is
approved), not line-level Subject/Sample/File records. The aggregation API
(federation.ccdi.cancer.gov) returns one array per federated node, concatenated
into a top-level list, with an error object for any node that timed out — so all
responses are flattened defensively.

Each namespace becomes one "dataset" record: study name/id + its organization +
that node's descriptive metadata, linking to the node's about/documentation URL
(there is no per-study human page). Note: study descriptions are generic per
organization, so per-study search signal is limited to the study identifier.
"""
from feeds import fetch
from search_core import clean, trim
from source_base import Source

CCDI_BASE = "https://federation.ccdi.cancer.gov/api/v1"
CCDI_HUB = "https://www.cancer.gov/research/key-initiatives/ccdi"
# The aggregator is slow (~2 min/endpoint); bound the wait and let the resilient
# cache cover failures rather than retrying for many minutes.
_TIMEOUT = 200
_RETRIES = 1


def _flatten(payload):
    """Flatten the aggregator's list-of-per-node-arrays, skipping error objects."""
    out = []
    if not isinstance(payload, list):
        return out
    for el in payload:
        if isinstance(el, list):
            out.extend(x for x in el if isinstance(x, dict))
        elif isinstance(el, dict) and "errors" not in el:
            out.append(el)
    return out


def _mv(field):
    """Unwrap a CCDI metadata field {'value': X, ...} to X."""
    if isinstance(field, dict):
        return field.get("value")
    return field


def fetch_ccdi():
    """Return {info, orgs, namespaces}. Namespaces are required; info/orgs best-effort."""
    namespaces = fetch(f"{CCDI_BASE}/namespace", retries=_RETRIES, timeout=_TIMEOUT)
    info, orgs = [], []
    try:
        info = fetch(f"{CCDI_BASE}/info", retries=_RETRIES, timeout=_TIMEOUT)
    except Exception:  # noqa: BLE001  (enrichment only)
        info = []
    try:
        orgs = fetch(f"{CCDI_BASE}/organization", retries=_RETRIES, timeout=_TIMEOUT)
    except Exception:  # noqa: BLE001
        orgs = []
    return {"info": info, "orgs": orgs, "namespaces": namespaces}


def norm_ccdi(payload):
    info = _flatten(payload.get("info"))
    orgs = _flatten(payload.get("orgs"))
    namespaces = _flatten(payload.get("namespaces"))

    # org identifier (lowercased) -> display name, institution
    org_name, org_inst = {}, {}
    for o in orgs:
        ident = (o.get("identifier") or "").lower()
        if not ident:
            continue
        org_name[ident] = o.get("name", "")
        meta = o.get("metadata") or {}
        insts = meta.get("institution") or []
        org_inst[ident] = ", ".join(_mv(i) or "" for i in insts if _mv(i))

    # resolve a human link + node description per org, keyed by source/owner
    url_by_source, desc_by_source = {}, {}
    for n in info:
        server = n.get("server") or {}
        api = n.get("api") or {}
        url = server.get("about_url") or api.get("documentation_url")
        owner = (server.get("owner") or "").lower()
        source = (n.get("source") or "").strip().lower()
        desc = server.get("name") or ""
        for k in (source, owner):
            if k:
                if url:
                    url_by_source[k] = url
                desc_by_source[k] = desc

    out = []
    for ns in namespaces:
        nid = ns.get("id") or {}
        org_id = (nid.get("organization") or "").lower()
        name = clean(str(nid.get("name") or ""))
        if not (org_id or name):
            continue
        meta = ns.get("metadata") or {}
        study_name = clean(str(_mv(meta.get("study_name")) or name))
        short = clean(str(_mv(meta.get("study_short_title")) or ""))
        study_id = clean(str(_mv(meta.get("study_id")) or ""))
        oname = org_name.get(org_id, org_id.upper())
        inst = org_inst.get(org_id, "")
        desc = clean(ns.get("description", ""))
        node_desc = desc_by_source.get(org_id) or desc_by_source.get(oname.lower(), "")
        url = (url_by_source.get(org_id)
               or url_by_source.get(oname.lower())
               or CCDI_HUB)

        title = short or study_name or name
        snippet = trim(f"Pediatric cancer dataset from {oname}"
                       + (f" (study {study_name})" if study_name else "")
                       + (f". {desc}" if desc else ""))

        out.append({
            "id": f"ccdi-{org_id}-{name}".lower().replace(" ", "-"),
            "source": "ccdi",
            "record_type": "Dataset",
            "title": title,
            "organization": oname,
            "institutions": inst,
            "study_id": study_id or study_name,
            "url": url,
            "snippet": snippet,
            "_body": " ".join([study_name, short, name, oname, org_id, inst,
                               desc, node_desc, "pediatric cancer childhood"]),
        })
    return out


SOURCES = [
    Source(key="ccdi", label="Dataset",
           fetch=fetch_ccdi, normalize=norm_ccdi),
]
