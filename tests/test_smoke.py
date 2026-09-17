"""
Smoke tests.

- Offline smoke (default): builds a corpus from the bundled fixtures via the real
  registry normalizers and runs an end-to-end search. No network.
- Live smoke (opt-in): `pytest -m live` hits each source's real endpoint and
  asserts it normalizes to at least one record. CCDI is also marked `slow`.
"""
import pytest

import sources
import search_core
from conftest import load_fixture


FIXTURE_BY_KEY = {
    "publication": "publications.json",
    "project": "projects.json",
    "ihcc": "ihcc_cohorts.json",
    "ccdi": "ccdi_payload.json",
}


@pytest.mark.smoke
def test_offline_pipeline_end_to_end():
    all_docs = []
    for key, fixture in FIXTURE_BY_KEY.items():
        all_docs += sources.get_source(key).normalize(load_fixture(fixture))
    raw, n = search_core.build_artifact_bytes(all_docs)
    docs, bm25 = search_core.load_engine_from_bytes(raw)

    # every source is represented
    present = {d["source"] for d in docs}
    assert present == set(FIXTURE_BY_KEY)

    # a cross-source query returns cited results
    res = search_core.search(docs, bm25, "childhood asthma cancer cohort", "all", 8)
    assert res and all(r.get("url") for r in res)

    # source filtering works end-to-end
    only_ihcc = search_core.search(docs, bm25, "cohort", "ihcc", 8)
    assert only_ihcc and all(r["source"] == "ihcc" for r in only_ihcc)


@pytest.mark.live
@pytest.mark.parametrize("key", ["publication", "project", "ihcc"])
def test_live_source_fetches(key):
    src = sources.get_source(key)
    docs = src.normalize(src.fetch())
    assert len(docs) > 0
    assert all(d["source"] == key and d["url"] for d in docs[:5])


@pytest.mark.live
@pytest.mark.slow
def test_live_ccdi_fetches():
    src = sources.get_source("ccdi")
    docs = src.normalize(src.fetch())
    assert len(docs) > 0
