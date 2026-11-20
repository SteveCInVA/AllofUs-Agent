"""Tests for the shared HTTP/timer/CLI Azure Search import process."""
from types import SimpleNamespace

import pytest

import refresh_job


def _patch(monkeypatch, results, existing=None):
    sources = [SimpleNamespace(key=key) for key in results]
    monkeypatch.setattr(refresh_job, "get_enabled_sources", lambda: sources)
    monkeypatch.setattr(refresh_job.feeds, "load_source_resilient", lambda src: results[src.key])
    uploaded = {}
    monkeypatch.setattr(refresh_job.storage, "replace_dataset",
                        lambda source, docs: uploaded.setdefault(source.key, len(docs)))
    monkeypatch.setattr(refresh_job.storage, "dataset_info",
                        lambda source: {"count": (existing or {}).get(source.key, 0)})
    return uploaded


def test_live_sources_import_directly_and_preserve_summary(monkeypatch):
    uploaded = _patch(monkeypatch, {"publication": ([{}, {}], "live"), "ihcc": ([{}], "live")})
    summary = refresh_job.rebuild_and_upload()
    assert uploaded == summary["datasets"] == {"publication": 2, "ihcc": 1}
    assert summary["records"] == 3
    assert summary["sources"] == {"publication": "live", "ihcc": "live"}
    assert "status" not in summary and "warnings" not in summary


def test_failed_source_keeps_indexed_records_while_other_sources_refresh(monkeypatch):
    uploaded = _patch(monkeypatch, {"publication": ([{}], "live"), "ihcc": (None, "failed")},
                      existing={"ihcc": 7})
    summary = refresh_job.rebuild_and_upload()
    assert uploaded == {"publication": 1}
    assert summary["records"] == 8
    assert summary["datasets"]["ihcc"] == 7
    assert summary["sources"]["ihcc"] == "carried"
    assert summary["status"] == "degraded"
    assert summary["warnings"] == ["ihcc used carried data"]


def test_failed_first_import_reports_failed_and_zero_count(monkeypatch):
    uploaded = _patch(monkeypatch, {"ihcc": (None, "failed")})
    summary = refresh_job.rebuild_and_upload()
    assert not uploaded and summary["datasets"] == {"ihcc": 0}
    assert summary["sources"] == {"ihcc": "failed"} and summary["status"] == "degraded"


def test_successful_empty_source_is_synchronized(monkeypatch):
    uploaded = _patch(monkeypatch, {"ihcc": ([], "live")})
    assert refresh_job.rebuild_and_upload()["records"] == 0
    assert uploaded == {"ihcc": 0}


def test_indexing_failure_propagates_instead_of_reporting_success(monkeypatch):
    _patch(monkeypatch, {"publication": ([{}], "live")})

    def fail(*args):
        raise RuntimeError("Search rejected a document")

    monkeypatch.setattr(refresh_job.storage, "replace_dataset", fail)
    with pytest.raises(RuntimeError, match="Search rejected"):
        refresh_job.rebuild_and_upload()


def test_cli_public_option_imports_instead_of_writing_files(monkeypatch, capsys):
    import build_index
    public = SimpleNamespace(key="publication", classification="public")
    restricted = SimpleNamespace(key="ihcc", classification="restricted")
    monkeypatch.setattr(build_index, "get_enabled_sources", lambda: [public, restricted])
    monkeypatch.setattr("sys.argv", ["build_index.py", "--public"])
    captured = []
    monkeypatch.setattr(build_index, "rebuild_and_upload",
                        lambda sources: captured.extend(sources) or {"records": 1})
    build_index.main()
    assert captured == [public]
    assert '"records": 1' in capsys.readouterr().out
