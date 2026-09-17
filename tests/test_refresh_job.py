"""Tests for refresh_job.rebuild_and_upload (feeds + storage mocked)."""
import types

import pytest

import refresh_job
import search_core


def _doc(source, i):
    return {"id": f"{source}-{i}", "source": source, "record_type": source.title(),
            "title": f"{source} {i}", "url": f"http://{source}/{i}",
            "snippet": "s", "_body": f"{source} body text {i}"}


def _patch(monkeypatch, sources, resilient, upload=None, prev=None):
    monkeypatch.setattr(refresh_job, "get_enabled_sources",
                        lambda: [types.SimpleNamespace(key=k) for k in sources])
    monkeypatch.setattr(refresh_job.feeds, "load_source_resilient",
                        lambda src: resilient[src.key])
    up = upload if upload is not None else {}
    monkeypatch.setattr(refresh_job.storage, "upload_corpus",
                        lambda data: up.setdefault("etag", "etag123") or "etag123")
    monkeypatch.setattr(refresh_job, "_previous_artifact", lambda: prev)
    return up


def test_all_live(monkeypatch):
    resilient = {"a": ([_doc("a", 1)], "live"),
                 "b": ([_doc("b", 1), _doc("b", 2)], "live")}
    _patch(monkeypatch, ["a", "b"], resilient)
    summary = refresh_job.rebuild_and_upload()
    assert summary["records"] == 3
    assert summary["counts"] == {"a": 1, "b": 2}
    assert summary["sources"] == {"a": "live", "b": "live"}
    assert "status" not in summary  # not degraded


def test_degraded_when_source_uses_cache(monkeypatch):
    resilient = {"a": ([_doc("a", 1)], "live"),
                 "b": ([_doc("b", 1)], "cache")}
    _patch(monkeypatch, ["a", "b"], resilient)
    summary = refresh_job.rebuild_and_upload()
    assert summary["status"] == "degraded"
    assert any("b used cache" in w for w in summary["warnings"])


def test_carry_forward_when_failed(monkeypatch):
    # 'b' fails with no cache; previous corpus still has a 'b' record to carry.
    prev, _ = search_core.build_artifact_bytes([_doc("b", 99)])
    resilient = {"a": ([_doc("a", 1)], "live"),
                 "b": (None, "failed")}
    _patch(monkeypatch, ["a", "b"], resilient, prev=prev)
    summary = refresh_job.rebuild_and_upload()
    assert summary["counts"]["b"] == 1, "carried record should count"
    assert summary["sources"]["b"] == "carried"
    assert summary["status"] == "degraded"


def test_raises_when_no_data_anywhere(monkeypatch):
    resilient = {"a": (None, "failed")}
    _patch(monkeypatch, ["a"], resilient, prev=None)
    with pytest.raises(RuntimeError):
        refresh_job.rebuild_and_upload()
