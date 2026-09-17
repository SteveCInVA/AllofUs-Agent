"""
IHCC Cohort Atlas source (International Health Cohorts Consortium).

The live atlas (ihccglobal.org) is an Overture SPA that is frequently in
maintenance and carries a "mock/demo data" disclaimer, so we ingest the
authoritative harmonized cohort metadata from the consortium's Apache-2.0
GitHub repo instead: IHCC-cohorts/data-harmonization/data/cohort-data.json
(a list of ~87 cohort records). This gives clean redistribution rights and is
unaffected by the atlas outage.

There is no per-cohort deep link in the atlas, so each record links to the
cohort's own `website` (falling back to the atlas home page).
"""
import re

from feeds import fetch
from search_core import clean, trim
from source_base import Source

IHCC_URL = ("https://raw.githubusercontent.com/IHCC-cohorts/"
            "data-harmonization/master/data/cohort-data.json")
ATLAS_HOME = "https://ihccglobal.org/"

# present = the cohort reports having this data type (value is a % band, not "No")
_ABSENT = {"", "no", "none", "0%", "0", "n/a", "false"}
_ACRONYMS = {"Wgs": "WGS", "Wes": "WES", "Ehr": "EHR", "Id": "ID"}


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "cohort"


def _humanize(key):
    words = key.replace("_", " ").title()
    for a, b in _ACRONYMS.items():
        words = re.sub(rf"\b{a}\b", b, words)
    return words


def _present(mapping):
    if not isinstance(mapping, dict):
        return []
    out = []
    for k, v in mapping.items():
        if v in (True,) or (isinstance(v, str) and v.strip().lower() not in _ABSENT):
            out.append(_humanize(k))
    return out


def _as_list(v):
    if isinstance(v, list):
        return [str(x) for x in v if x]
    return [str(v)] if v else []


def norm_ihcc(rows):
    out = []
    seen = set()
    for r in rows:
        name = clean(r.get("cohort_name", ""))
        if not name:
            continue
        cid = f"ihcc-{_slug(name)}"
        if cid in seen:  # guard against dup names
            cid = f"{cid}-{len(seen)}"
        seen.add(cid)

        countries = _as_list(r.get("countries"))
        pi = clean(r.get("pi_lead", ""))
        website = (r.get("website") or "").strip() or ATLAS_HOME
        enroll = r.get("current_enrollment")

        qs = r.get("questionnaire_survey_data") or {}
        diseases = _as_list(qs.get("diseases")) if isinstance(qs, dict) else []
        lifestyle = _as_list(qs.get("lifestyle_and_behaviours")) if isinstance(qs, dict) else []
        data_types = _present(r.get("available_data_types"))
        cohort_kinds = _present(r.get("type_of_cohort"))
        ancestry = _present(r.get("cohort_ancestry"))

        # Human-readable snippet (the raw `description` field is near-useless).
        bits = []
        if countries:
            bits.append("Cohort in " + ", ".join(countries))
        if isinstance(enroll, (int, float)) and enroll:
            bits.append(f"~{int(enroll):,} participants")
        if pi:
            bits.append(f"PI: {pi}")
        if diseases:
            bits.append("Diseases: " + ", ".join(diseases[:5]))
        if data_types:
            bits.append("Data: " + ", ".join(data_types[:5]))
        snippet = trim(". ".join(bits))

        out.append({
            "id": cid,
            "source": "ihcc",
            "record_type": "Cohort",
            "title": name,
            "countries": countries,
            "pi_lead": pi,
            "enrollment": int(enroll) if isinstance(enroll, (int, float)) and enroll else "",
            "diseases": diseases,
            "data_types": data_types,
            "url": website,
            "snippet": snippet,
            "_body": " ".join([name, " ".join(countries), pi,
                               " ".join(diseases), " ".join(lifestyle),
                               " ".join(data_types), " ".join(cohort_kinds),
                               " ".join(ancestry), clean(r.get("description", "")),
                               "health cohort"]),
        })
    return out


SOURCES = [
    Source(key="ihcc", label="Cohort",
           fetch=lambda: fetch(IHCC_URL), normalize=norm_ihcc),
]
