"""
Shared retrieval core for the NIH All of Us "find similar work" agent.

Used at build time (build_index.py) to normalize the two public directory feeds
into a compact, version-portable artifact (corpus.pkl), and at runtime
(function_app.py) to load that artifact and run similarity search.

The artifact stores ONLY builtin Python types (lists/dicts/strings) so it loads
cleanly on any Python 3.8-3.12 runtime. The BM25 model is (re)built at cold
start from the stored token lists — this avoids pickling numpy/BM25 objects
across Python versions.
"""
import pickle
import re
from urllib.parse import unquote

# ---------------------------------------------------------------- normalization

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


def _trim(text, n=320):
    return (text[:n] + "…") if text and len(text) > n else (text or "")


def norm_publications(rows):
    out = []
    for r in rows:
        if str(r.get("show_on_site", "1")) == "0":
            continue
        names = []
        for a in (r.get("calc_author_list") or []):
            if isinstance(a, dict):
                names.append(f"{a.get('ForeName','')} {a.get('LastName','')}".strip())
        insts = clean(r.get("calc_institution_list", "")).replace("|", ", ")
        y = r.get("calc_date_year", "")
        m = str(r.get("calc_date_month", "")).zfill(2)
        d = str(r.get("calc_date_day", "")).zfill(2)
        date = "-".join(p for p in [y, m, d] if p and p != "00")
        focus = [k.replace("_focus", "").replace("_", " ").title()
                 for k in ("common_focus", "rare_focus", "maternal_focus",
                           "aging_focus", "results_focus", "behavioral_focus",
                           "environmental_focus", "population_health_focus",
                           "genetic_focus")
                 if str(r.get(k, "0")) == "1"]
        title = clean(r.get("calc_title", ""))
        abstract = clean(r.get("calc_abstract", ""))
        lay = clean(r.get("lay_summary", ""))
        out.append({
            "id": f"pub-{r.get('record_id')}",
            "source": "publication",
            "title": title,
            "authors": names[:6],
            "institutions": insts,
            "date": date,
            "journal": clean(r.get("calc_journal_title", "")),
            "focus": focus,
            "citations": str(r.get("icite_count", "")),
            "url": r.get("link", ""),
            "snippet": _trim(abstract or lay),
            "_body": " ".join([title, abstract, lay, " ".join(names),
                               insts, " ".join(focus)]),
        })
    return out


def norm_projects(rows):
    out = []
    for r in rows:
        team = r.get("team", {}) or {}
        people, insts = [], set()
        for grp in ("owner", "members"):
            for p in (team.get(grp, []) or []):
                if p.get("name"):
                    people.append(p["name"])
                if p.get("institution"):
                    insts.add(p["institution"])
        purposes = r.get("purposes", []) or []
        focus = r.get("focusCategories", []) or []
        title = clean(r.get("title", ""))
        questions = clean(r.get("questions", ""))
        approaches = clean(r.get("approaches", ""))
        findings = clean(r.get("findings", ""))
        out.append({
            "id": f"proj-{r.get('workspaceId')}",
            "source": "project",
            "title": title,
            "authors": people[:6],
            "institutions": ", ".join(sorted(insts)),
            "date": "",
            "journal": "",
            "focus": focus,
            "access_tier": r.get("accessTier", ""),
            "citations": "",
            "url": r.get("reviewUrl", ""),
            "snippet": _trim(questions or approaches or findings),
            "_body": " ".join([title, " ".join(purposes), questions,
                               approaches, findings, " ".join(people),
                               ", ".join(insts), " ".join(focus)]),
        })
    return out


DISPLAY_FIELDS = ("id", "source", "title", "authors", "institutions", "date",
                  "journal", "focus", "access_tier", "citations", "url", "snippet")


def build_artifact_bytes(pub_rows, proj_rows):
    """Normalize both feeds and return (pickle_bytes, record_count)."""
    docs = norm_publications(pub_rows) + norm_projects(proj_rows)
    tokens = [tokenize(d["_body"]) for d in docs]
    slim = [{k: d.get(k) for k in DISPLAY_FIELDS} for d in docs]
    return pickle.dumps({"docs": slim, "tokens": tokens}, protocol=4), len(slim)


def build_artifact(pub_rows, proj_rows, out_path):
    """Normalize both feeds and write a portable {docs, tokens} artifact."""
    data, n = build_artifact_bytes(pub_rows, proj_rows)
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


def load_engine(path):
    """Load the artifact from disk and (re)build the BM25 model at cold start."""
    with open(path, "rb") as fh:
        return load_engine_from_bytes(fh.read())


def search(docs, bm25, query, directory="both", top=8):
    """Return the most similar records to a free-text description."""
    directory = (directory or "both").lower()
    scores = bm25.get_scores(tokenize(query))
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    want = None if directory in ("both", "all", "") else directory.rstrip("s")
    results = []
    for i in order:
        if scores[i] <= 0:
            break
        d = docs[i]
        if want and d["source"] != want:
            continue
        item = {k: v for k, v in d.items() if v not in (None, "", [])}
        item["score"] = round(float(scores[i]), 3)
        results.append(item)
        if len(results) >= top:
            break
    return results
