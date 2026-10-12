"""Registry + cross-source contract tests."""
import pytest

import sources
import source_base


def test_registry_keys_and_order():
    assert sources.source_keys() == ["publication", "project", "ihcc", "ccdi"]


def test_get_source_hit_and_miss():
    assert sources.get_source("ihcc").key == "ihcc"
    assert sources.get_source("nope") is None


def test_get_enabled_sources_excludes_disabled(monkeypatch):
    src = sources.ALL_SOURCES[0]
    monkeypatch.setattr(src, "enabled", False)
    try:
        assert src.key not in sources.source_keys()
    finally:
        monkeypatch.setattr(src, "enabled", True)


def test_source_base_defaults():
    s = source_base.Source(key="demo", label="Demo",
                           fetch=lambda: None, normalize=lambda r: [])
    assert s.cache_blob == "demo.docs.json"
    assert s.record_type == "Demo"
    assert s.index_blob == "index/demo.pkl"
    assert s.classification == "public" and s.entitlement_group_id == ""


@pytest.fixture
def restore_classification():
    """Snapshot + restore each source's classification/entitlement across a test."""
    snap = [(s, s.classification, s.entitlement_group_id) for s in sources.ALL_SOURCES]
    yield
    for s, c, g in snap:
        s.classification, s.entitlement_group_id = c, g


def test_default_classification_is_public():
    assert set(sources.public_keys()) == set(sources.source_keys())
    assert sources.restricted_keys() == []


def test_apply_classification_marks_ihcc_ccdi_restricted(restore_classification):
    sources.apply_classification({
        "ihcc": {"classification": "restricted", "entitlement_group_id": "G-IHCC"},
        "ccdi": {"classification": "restricted", "entitlement_group_id": "G-CCDI"},
    })
    assert sorted(sources.restricted_keys()) == ["ccdi", "ihcc"]
    assert sorted(sources.public_keys()) == ["project", "publication"]
    assert sources.key_for_group("G-IHCC") == "ihcc"
    assert sources.key_for_group("G-CCDI") == "ccdi"
    assert sources.key_for_group("unknown") is None


def test_unlisted_dataset_stays_public(restore_classification):
    sources.apply_classification({"ihcc": {"classification": "restricted",
                                           "entitlement_group_id": "G1"}})
    assert "ccdi" in sources.public_keys()  # unlisted -> default public
    assert "ihcc" in sources.restricted_keys()


REQUIRED_KEYS = {"id", "source", "record_type", "title", "url", "snippet", "_body"}

# (source key, fixture name, callable to normalize)
CONTRACT = [
    ("publication", "publications.json"),
    ("project", "projects.json"),
    ("ihcc", "ihcc_cohorts.json"),
    ("ccdi", "ccdi_payload.json"),
]


@pytest.mark.parametrize("key,fixture", CONTRACT)
def test_every_record_has_required_keys(key, fixture):
    from conftest import load_fixture
    raw = load_fixture(fixture)
    docs = sources.get_source(key).normalize(raw)
    assert docs, f"{key} produced no records"
    for d in docs:
        missing = REQUIRED_KEYS - set(d)
        assert not missing, f"{key} record missing {missing}"
        assert d["source"] == key
        assert isinstance(d["title"], str) and d["title"]
        assert isinstance(d["url"], str) and d["url"]
