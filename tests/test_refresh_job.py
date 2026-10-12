"""Tests for refresh_job.rebuild_and_upload (per-dataset indexes, mocked storage)."""
import json
import types

import refresh_job
import search_core


def _doc(source, i):
    return {"id": f"{source}-{i}", "source": source, "record_type": source.title(),
            "title": f"{source} {i}", "url": f"http://{source}/{i}",
            "snippet": "s", "_body": f"{source} body text {i}"}


def _src(key, classification="public"):
    return types.SimpleNamespace(key=key, label=key.title(), classification=classification)


def _patch(monkeypatch, sources, resilient, uploaded_idx, uploaded_manifest,
           prev_index=None):
    monkeypatch.setattr(refresh_job, "get_enabled_sources", lambda: sources)
    monkeypatch.setattr(refresh_job.feeds, "load_source_resilient",
                        lambda src: resilient[src.key])
    monkeypatch.setattr(refresh_job.storage, "upload_index",
                        lambda key, data: uploaded_idx.update({key: data}) or "etag")
    monkeypatch.setattr(refresh_job.storage, "download_index",
                        lambda key: ((prev_index or {}).get(key), "etag")
                        if (prev_index or {}).get(key) else (None, None))
    monkeypatch.setattr(refresh_job.storage, "upload_manifest",
                        lambda data: uploaded_manifest.append(json.loads(data)) or "etag")


def test_all_live_builds_per_dataset_indexes(monkeypatch):
    idx, man = {}, []
    resilient = {"publication": ([_doc("publication", 1)], "live"),
                 "ihcc": ([_doc("ihcc", 1), _doc("ihcc", 2)], "live")}
    _patch(monkeypatch, [_src("publication"), _src("ihcc", "restricted")],
           resilient, idx, man)
    summary = refresh_job.rebuild_and_upload()
    assert set(idx) == {"publication", "ihcc"}      # one index blob per dataset
    assert summary["records"] == 3
    assert summary["datasets"] == {"publication": 1, "ihcc": 2}
    assert summary["sources"] == {"publication": "live", "ihcc": "live"}
    # manifest enumerates datasets with name + count + classification
    m = man[-1]
    assert m["total"] == 3
    ihcc = [d for d in m["datasets"] if d["key"] == "ihcc"][0]
    assert ihcc["name"] == "Ihcc" and ihcc["count"] == 2 and ihcc["classification"] == "restricted"


def test_degraded_on_cache(monkeypatch):
    idx, man = {}, []
    resilient = {"publication": ([_doc("publication", 1)], "live"),
                 "ihcc": ([_doc("ihcc", 1)], "cache")}
    _patch(monkeypatch, [_src("publication"), _src("ihcc")], resilient, idx, man)
    summary = refresh_job.rebuild_and_upload()
    assert summary["status"] == "degraded"
    assert any("ihcc used cache" in w for w in summary["warnings"])


def test_carry_previous_index_when_failed(monkeypatch):
    idx, man = {}, []
    prev_bytes, _ = search_core.build_artifact_bytes([_doc("ihcc", 9)])
    resilient = {"publication": ([_doc("publication", 1)], "live"),
                 "ihcc": (None, "failed")}
    _patch(monkeypatch, [_src("publication"), _src("ihcc")], resilient, idx, man,
           prev_index={"ihcc": prev_bytes})
    summary = refresh_job.rebuild_and_upload()
    assert summary["datasets"]["ihcc"] == 1        # carried count preserved
    assert summary["sources"]["ihcc"] == "carried"
    assert "ihcc" not in idx                        # not re-uploaded


def test_failed_with_no_previous(monkeypatch):
    idx, man = {}, []
    resilient = {"ihcc": (None, "failed")}
    _patch(monkeypatch, [_src("ihcc")], resilient, idx, man)
    summary = refresh_job.rebuild_and_upload()
    assert summary["datasets"]["ihcc"] == 0
    assert summary["sources"]["ihcc"] == "failed"
    assert man[-1]["total"] == 0                    # manifest still written
