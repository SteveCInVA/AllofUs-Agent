"""Tests for the All of Us normalizers (publications + projects)."""
import source_allofus as sa


def test_publications_skips_hidden_and_formats(publications_raw):
    docs = sa.norm_publications(publications_raw)
    assert len(docs) == 1, "show_on_site=0 record must be skipped"
    d = docs[0]
    assert d["id"] == "pub-1"
    assert d["source"] == "publication" and d["record_type"] == "Publication"
    assert d["authors"] == ["Jane Doe", "John Smith"]
    assert d["institutions"] == "Brown University, Rutgers"
    assert d["date"] == "2022-10-19"
    assert d["focus"] == ["Common", "Population Health"]
    assert d["citations"] == "9"
    assert d["url"] == "https://pubmed.ncbi.nlm.nih.gov/1"
    # abstract cleaned of HTML + %-encoding, and present in body
    assert "air pollution" in d["_body"]
    assert "(kids)" in d["snippet"]


def test_projects_flatten_team_and_snippet(projects_raw):
    docs = sa.norm_projects(projects_raw)
    assert len(docs) == 1
    d = docs[0]
    assert d["id"] == "proj-101"
    assert d["source"] == "project" and d["record_type"] == "Project"
    assert d["authors"] == ["Nina Cesare", "Minhyuk Choi", "Xavier Test"]
    # institutions unique + sorted
    assert d["institutions"] == "Boston University, Rush University, UCSF"
    assert d["access_tier"] == "Controlled Tier"
    assert d["snippet"].startswith("Study population")
    assert "logistic regression" in d["_body"].lower()


def test_allofus_sources_registered():
    keys = [s.key for s in sa.SOURCES]
    assert keys == ["publication", "project"]
