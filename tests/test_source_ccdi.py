"""Tests for the CCDI federation normalizer."""
import source_ccdi as sd


def test_flatten_skips_error_objects(ccdi_payload):
    ns = sd._flatten(ccdi_payload["namespaces"])
    assert len(ns) == 2, "per-node error object must be skipped"
    orgs = sd._flatten(ccdi_payload["orgs"])
    assert len(orgs) == 1


def test_mv_unwraps_value():
    assert sd._mv({"value": "X"}) == "X"
    assert sd._mv("plain") == "plain"
    assert sd._mv({"value": None}) is None


def test_norm_ccdi_records(ccdi_payload):
    docs = sd.norm_ccdi(ccdi_payload)
    assert len(docs) == 2
    by_id = {d["id"]: d for d in docs}

    b953 = by_id["ccdi-pcdc-b953"]
    assert b953["source"] == "ccdi" and b953["record_type"] == "Dataset"
    assert b953["title"] == "B953"          # short title null -> study_name
    assert b953["organization"] == "Pediatric Cancer Data Common"
    assert b953["url"] == "https://commons.cri.uchicago.edu/pcdc/"
    assert "pediatric" in b953["_body"].lower()

    aml = by_id["ccdi-pcdc-aaml0531"]
    assert aml["title"] == "AML Study"      # short title preferred when present


def test_norm_ccdi_url_fallback_to_hub():
    payload = {"info": [], "orgs": [], "namespaces": [[
        {"id": {"organization": "unknownorg", "name": "S1"},
         "metadata": {"study_name": {"value": "S1"}}}]]}
    docs = sd.norm_ccdi(payload)
    assert docs[0]["url"] == sd.CCDI_HUB


def test_ccdi_source_registered():
    assert [s.key for s in sd.SOURCES] == ["ccdi"]


def test_fetch_ccdi_assembles_payload(monkeypatch):
    def fake_fetch(url, retries=1, timeout=200):
        if url.endswith("/namespace"):
            return [[{"id": {"organization": "o", "name": "N1"},
                      "metadata": {"study_name": {"value": "N1"}}}]]
        if url.endswith("/info"):
            return [{"source": "o", "server": {"owner": "Org"}}]
        if url.endswith("/organization"):
            return [[{"identifier": "o", "name": "Org"}]]
        raise AssertionError(url)
    monkeypatch.setattr(sd, "fetch", fake_fetch)
    payload = sd.fetch_ccdi()
    assert payload["namespaces"] and payload["info"] and payload["orgs"]
    docs = sd.norm_ccdi(payload)
    assert docs and docs[0]["source"] == "ccdi"


def test_fetch_ccdi_tolerates_info_org_failure(monkeypatch):
    def fake_fetch(url, retries=1, timeout=200):
        if url.endswith("/namespace"):
            return [[{"id": {"organization": "o", "name": "N1"},
                      "metadata": {"study_name": {"value": "N1"}}}]]
        raise RuntimeError("node down")
    monkeypatch.setattr(sd, "fetch", fake_fetch)
    payload = sd.fetch_ccdi()
    assert payload["info"] == [] and payload["orgs"] == []
    assert sd.norm_ccdi(payload)  # still builds from namespaces
