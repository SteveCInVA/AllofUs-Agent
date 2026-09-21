"""
Retrieval core for the All of Us "find similar work" agent.

Source-agnostic: it turns a flat list of already-normalized record dicts (each
produced by a Source.normalize) into a compact, version-portable per-dataset index
artifact (`index/<key>.pkl`) and runs BM25 similarity search over one or more of them.

The artifact stores ONLY builtin Python types (lists/dicts/strings) so it loads
cleanly on any Python 3.8-3.12 runtime. The BM25 model is (re)built at cold
start from the stored token lists — this avoids pickling numpy/BM25 objects
across Python versions.
"""
import pickle
import re
from urllib.parse import unquote

# ---------------------------------------------------------------- text helpers

def clean(text):
    """Decode %-encoding, strip HTML, collapse whitespace."""
    if not text:
        return ""
    text = unquote(str(text))
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\u00b7", " ").replace("\u00a7", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def tokenize(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def trim(text, n=320):
    return (text[:n] + "…") if text and len(text) > n else (text or "")


# ---------------------------------------------------------------- artifact build

def _slim(doc):
    """Drop private (underscore-prefixed) keys such as _body from a record."""
    return {k: v for k, v in doc.items() if not k.startswith("_")}


def build_artifact_bytes(docs, carried_docs=None, carried_tokens=None):
    """Build (pickle_bytes, record_count) from a flat list of normalized records.

    Each record in `docs` must carry a "_body" searchable text blob; it is
    tokenized for BM25 and then dropped from the stored record. `carried_docs`
    /`carried_tokens` append already-slimmed records + tokens salvaged from a
    previous snapshot (used when a source is fully unavailable), so one failing
    source can't drop records from the corpus.
    """
    slim = [_slim(d) for d in docs]
    tokens = [tokenize(d.get("_body", "")) for d in docs]
    if carried_docs:
        slim += carried_docs
        tokens += carried_tokens or []
    return pickle.dumps({"docs": slim, "tokens": tokens}, protocol=4), len(slim)


def carry_source(artifact_bytes, source):
    """Return (slim_docs, tokens) for one source key from an existing artifact."""
    data = pickle.loads(artifact_bytes)
    docs, toks = [], []
    for d, t in zip(data.get("docs", []), data.get("tokens", [])):
        if d.get("source") == source:
            docs.append(d)
            toks.append(t)
    return docs, toks


def build_artifact(docs, out_path):
    """Normalize a flat doc list into a portable artifact written to disk."""
    data, n = build_artifact_bytes(docs)
    with open(out_path, "wb") as fh:
        fh.write(data)
    return n


# --------------------------------------------------------------------- runtime

def load_engine_from_bytes(data_bytes):
    """Build the search engine from pickled artifact bytes."""
    from rank_bm25 import BM25Okapi
    data = pickle.loads(data_bytes)
    bm25 = BM25Okapi(data["tokens"])
    return data["docs"], bm25


def load_artifact(data_bytes):
    """Return (docs, tokens) from a pickled artifact WITHOUT building BM25.

    Used to hold per-dataset index contents so a merged engine can be built across
    several datasets on demand.
    """
    data = pickle.loads(data_bytes)
    return data["docs"], data["tokens"]


def build_engine(parts):
    """Build a merged (docs, bm25) engine from parts = [(docs, tokens), ...]."""
    from rank_bm25 import BM25Okapi
    docs, tokens = [], []
    for d, t in parts:
        docs += d
        tokens += t
    bm25 = BM25Okapi(tokens) if tokens else None
    return docs, bm25


def load_engine(path):
    """Load the artifact from disk and (re)build the BM25 model at cold start."""
    with open(path, "rb") as fh:
        return load_engine_from_bytes(fh.read())


# Legacy plural aliases so an old connector value keeps working.
_ALIASES = {"publications": "publication", "projects": "project"}


def resolve_filter(directory):
    """Map a directory/source filter value to a source key, or None for all."""
    directory = (directory or "both").lower()
    if directory in ("both", "all", ""):
        return None
    return _ALIASES.get(directory, directory)


def search(docs, bm25, query, directory="both", top=8):
    """Return the most similar records to a free-text description.

    `directory` filters to a single source key (e.g. "publication", "ihcc") or
    "both"/"all" for no filter.
    """
    want = resolve_filter(directory)
    if bm25 is None or not docs:
        return []
    scores = bm25.get_scores(tokenize(query))
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    results = []
    for i in order:
        if scores[i] <= 0:
            break
        d = docs[i]
        if want and d.get("source") != want:
            continue
        item = {k: v for k, v in d.items() if v not in (None, "", [])}
        item["score"] = round(float(scores[i]), 3)
        results.append(item)
        if len(results) >= top:
            break
    return results
