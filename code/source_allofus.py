"""
All of Us data sources: the Publication Directory and the Research Project
Directory, served as public WordPress JSON feeds on researchallofus.org.
"""
from feeds import fetch
from search_core import clean, trim
from source_base import Source

PUBLICATIONS = "https://www.researchallofus.org/wp-json/rh-data-caching/publications-report"
PROJECTS = "https://www.researchallofus.org/wp-json/rh-data-caching/projects"


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
            "record_type": "Publication",
            "title": title,
            "authors": names[:6],
            "institutions": insts,
            "date": date,
            "journal": clean(r.get("calc_journal_title", "")),
            "focus": focus,
            "citations": str(r.get("icite_count", "")),
            "url": r.get("link", ""),
            "snippet": trim(abstract or lay),
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
            "record_type": "Project",
            "title": title,
            "authors": people[:6],
            "institutions": ", ".join(sorted(insts)),
            "focus": focus,
            "access_tier": r.get("accessTier", ""),
            "url": r.get("reviewUrl", ""),
            "snippet": trim(questions or approaches or findings),
            "_body": " ".join([title, " ".join(purposes), questions,
                               approaches, findings, " ".join(people),
                               ", ".join(insts), " ".join(focus)]),
        })
    return out


SOURCES = [
    Source(key="publication", label="Publication",
           fetch=lambda: fetch(PUBLICATIONS), normalize=norm_publications),
    Source(key="project", label="Project",
           fetch=lambda: fetch(PROJECTS), normalize=norm_projects),
]
