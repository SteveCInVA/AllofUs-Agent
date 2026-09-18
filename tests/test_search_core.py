"""Unit tests for search_core: text helpers, artifact build, filtering, search."""
import pickle

import pytest

import search_core as sc


# ---------------------------------------------------------------- text helpers

def test_clean_strips_html_and_decodes_and_collapses():
    assert sc.clean("<b>air</b>   pollution %28kids%29") == "air pollution (kids)"


def test_clean_empty():
    assert sc.clean("") == ""
    assert sc.clean(None) == ""


def test_tokenize_lowercases_and_splits():
    assert sc.tokenize("Air, Pollution! 123") == ["air", "pollution", "123"]


def test_trim_truncates_with_ellipsis():
    assert sc.trim("abcdef", 3) == "abc…"
    assert sc.trim("abc", 10) == "abc"
    assert sc.trim("", 10) == ""


# ---------------------------------------------------------------- artifact build

def test_build_artifact_drops_body_and_parallels_tokens(sample_docs):
    raw, n = sc.build_artifact_bytes(sample_docs)
    data = pickle.loads(raw)
    assert n == len(sample_docs) == len(data["docs"]) == len(data["tokens"])
    assert all("_body" not in d for d in data["docs"]), "_body must not be stored"
    # tokens derived from _body
    assert "asthma" in data["tokens"][0]


def test_build_artifact_appends_carried(sample_docs):
    carried_docs = [{"id": "z", "source": "ccdi", "title": "carried"}]
    carried_tokens = [["carried", "tokens"]]
    raw, n = sc.build_artifact_bytes(sample_docs, carried_docs, carried_tokens)
    data = pickle.loads(raw)
    assert n == len(sample_docs) + 1
    assert data["docs"][-1]["id"] == "z"
    assert data["tokens"][-1] == ["carried", "tokens"]


def test_carry_source_filters_by_source(sample_docs):
    raw, _ = sc.build_artifact_bytes(sample_docs)
    docs, toks = sc.carry_source(raw, "ihcc")
    assert len(docs) == 1 and docs[0]["id"] == "c1"
    assert len(toks) == 1


def test_carry_source_absent_returns_empty(sample_docs):
    raw, _ = sc.build_artifact_bytes(sample_docs)
    docs, toks = sc.carry_source(raw, "ccdi")
    assert docs == [] and toks == []


def test_build_artifact_to_disk(tmp_path, sample_docs):
    out = tmp_path / "corpus.pkl"
    n = sc.build_artifact(sample_docs, str(out))
    assert n == len(sample_docs) and out.exists()


def test_load_artifact_returns_docs_and_tokens(sample_docs):
    raw, _ = sc.build_artifact_bytes(sample_docs)
    docs, tokens = sc.load_artifact(raw)
    assert len(docs) == len(tokens) == len(sample_docs)
    assert all("_body" not in d for d in docs)


def test_build_engine_merges_parts(sample_docs):
    raw, _ = sc.build_artifact_bytes(sample_docs)
    docs, tokens = sc.load_artifact(raw)
    # split into two parts and merge
    part1 = (docs[:1], tokens[:1])
    part2 = (docs[1:], tokens[1:])
    merged_docs, bm25 = sc.build_engine([part1, part2])
    assert len(merged_docs) == len(sample_docs) and bm25 is not None


def test_build_engine_empty_yields_none_bm25():
    docs, bm25 = sc.build_engine([])
    assert docs == [] and bm25 is None


def test_search_empty_engine_returns_empty():
    assert sc.search([], None, "anything", "all", 8) == []


# ---------------------------------------------------------------- filter resolve

@pytest.mark.parametrize("value,expected", [
    ("both", None), ("all", None), ("", None), (None, None),
    ("publications", "publication"), ("projects", "project"),
    ("publication", "publication"), ("ihcc", "ihcc"), ("CCDI", "ccdi"),
])
def test_resolve_filter(value, expected):
    assert sc.resolve_filter(value) == expected


# ---------------------------------------------------------------- search

def _engine(sample_docs):
    raw, _ = sc.build_artifact_bytes(sample_docs)
    return sc.load_engine_from_bytes(raw)


def test_search_ranks_and_returns_score(sample_docs):
    docs, bm25 = _engine(sample_docs)
    res = sc.search(docs, bm25, "childhood asthma", "all", 8)
    assert res and res[0]["source"] == "publication"
    assert "score" in res[0] and res[0]["score"] > 0
    assert "_body" not in res[0]


def test_search_source_filter(sample_docs):
    docs, bm25 = _engine(sample_docs)
    res = sc.search(docs, bm25, "asthma", "ihcc", 8)
    assert res and all(r["source"] == "ihcc" for r in res)


def test_search_top_cap(sample_docs):
    docs, bm25 = _engine(sample_docs)
    res = sc.search(docs, bm25, "asthma", "all", 1)
    assert len(res) == 1


def test_search_no_match_returns_empty(sample_docs):
    docs, bm25 = _engine(sample_docs)
    assert sc.search(docs, bm25, "quantum chromodynamics zzzz", "all", 8) == []


def test_search_drops_empty_fields(sample_docs):
    docs = list(sample_docs)
    docs[0] = {**docs[0], "journal": "", "authors": []}
    e_docs, bm25 = _engine(docs)
    res = sc.search(e_docs, bm25, "childhood asthma", "publication", 8)
    assert "journal" not in res[0] and "authors" not in res[0]
