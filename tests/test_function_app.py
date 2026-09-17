"""Tests for function_app HTTP handlers and the cache engine."""
import json

import azure.functions as func
import pytest

import function_app as fa
import search_core


HANDLERS = {f.get_function_name(): f.get_user_function()
            for f in fa.app.get_functions()}


@pytest.fixture(autouse=True)
def reset_engine():
    fa._DOCS = fa._BM25 = fa._ETAG = fa._SOURCE = None
    fa._LAST_CHECK = 0.0
    yield
    fa._DOCS = fa._BM25 = fa._ETAG = fa._SOURCE = None
    fa._LAST_CHECK = 0.0


def _req(method="GET", params=None, body=b""):
    return func.HttpRequest(method=method, url="/api/search",
                            params=params or {}, body=body)


def _mock_engine(monkeypatch, sample_docs):
    docs, bm25 = search_core.load_engine_from_bytes(
        search_core.build_artifact_bytes(sample_docs)[0])
    monkeypatch.setattr(fa, "_engine", lambda: (docs, bm25))
    return docs


# ------------------------------------------------------------------- search

def test_search_missing_query_400(monkeypatch, sample_docs):
    _mock_engine(monkeypatch, sample_docs)
    resp = HANDLERS["search_directories"](_req(params={}))
    assert resp.status_code == 400


def test_search_happy(monkeypatch, sample_docs):
    _mock_engine(monkeypatch, sample_docs)
    resp = HANDLERS["search_directories"](_req(params={"query": "childhood asthma"}))
    assert resp.status_code == 200
    body = json.loads(resp.get_body())
    assert body["count"] >= 1
    assert body["results"][0]["url"]


def test_search_invalid_directory_defaults(monkeypatch, sample_docs):
    _mock_engine(monkeypatch, sample_docs)
    resp = HANDLERS["search_directories"](
        _req(params={"query": "asthma", "directory": "bogus"}))
    body = json.loads(resp.get_body())
    assert body["directory"] == "both"  # normalized default


def test_search_top_clamped(monkeypatch, sample_docs):
    _mock_engine(monkeypatch, sample_docs)
    resp = HANDLERS["search_directories"](
        _req(params={"query": "asthma", "top": "0"}))
    body = json.loads(resp.get_body())
    assert len(body["results"]) == 1  # 0 clamped up to 1


def test_search_post_body(monkeypatch, sample_docs):
    _mock_engine(monkeypatch, sample_docs)
    resp = HANDLERS["search_directories"](
        _req(method="POST", body=json.dumps({"query": "maternal", "directory": "project"}).encode()))
    body = json.loads(resp.get_body())
    assert all(r["source"] == "project" for r in body["results"])


def test_search_engine_error_500(monkeypatch, sample_docs):
    def boom():
        raise RuntimeError("engine down")
    monkeypatch.setattr(fa, "_engine", boom)
    resp = HANDLERS["search_directories"](_req(params={"query": "asthma"}))
    assert resp.status_code == 500


# ------------------------------------------------------------------- health

def test_health_counts(monkeypatch, sample_docs):
    _mock_engine(monkeypatch, sample_docs)
    fa._SOURCE = "blob"
    resp = HANDLERS["health"](_req())
    body = json.loads(resp.get_body())
    assert body["status"] == "ok"
    assert body["records"] == 3
    assert body["counts"]["publication"] == 1


# ------------------------------------------------------------------- refresh

def test_refresh_now_success(monkeypatch):
    monkeypatch.setattr(fa, "rebuild_and_upload",
                        lambda: {"records": 5, "counts": {}})
    resp = HANDLERS["refresh_now"](_req(method="POST"))
    body = json.loads(resp.get_body())
    assert body["status"] == "refreshed" and body["records"] == 5
    assert fa._DOCS is None  # cache invalidated


def test_refresh_now_error_500(monkeypatch):
    def boom():
        raise RuntimeError("refresh failed")
    monkeypatch.setattr(fa, "rebuild_and_upload", boom)
    resp = HANDLERS["refresh_now"](_req(method="POST"))
    assert resp.status_code == 500


# ------------------------------------------------------------------- _engine

def test_engine_loads_from_blob(monkeypatch, sample_docs):
    raw = search_core.build_artifact_bytes(sample_docs)[0]
    monkeypatch.setattr(fa.storage, "download_corpus", lambda: (raw, "etag1"))
    docs, _ = fa._engine()
    assert len(docs) == 3 and fa._SOURCE == "blob" and fa._ETAG == "etag1"


def test_engine_falls_back_to_package(monkeypatch, sample_docs):
    monkeypatch.setattr(fa.storage, "download_corpus", lambda: (None, None))
    monkeypatch.setattr(fa, "load_engine", lambda path: (["x"], object()))
    docs, _ = fa._engine()
    assert docs == ["x"] and fa._SOURCE == "package"


def test_engine_hot_reloads_on_etag_change(monkeypatch, sample_docs):
    raw = search_core.build_artifact_bytes(sample_docs)[0]
    fa._DOCS, fa._BM25 = ["old"], object()
    fa._ETAG, fa._SOURCE, fa._LAST_CHECK = "old", "blob", 0.0
    monkeypatch.setattr(fa.storage, "get_corpus_etag", lambda: "new")
    monkeypatch.setattr(fa.storage, "download_corpus", lambda: (raw, "new"))
    docs, _ = fa._engine()
    assert fa._ETAG == "new" and len(docs) == 3


def test_engine_staleness_check_tolerates_errors(monkeypatch):
    fa._DOCS, fa._BM25 = ["keep"], object()
    fa._ETAG, fa._SOURCE, fa._LAST_CHECK = "e", "blob", 0.0

    def boom():
        raise RuntimeError("etag check down")
    monkeypatch.setattr(fa.storage, "get_corpus_etag", boom)
    docs, _ = fa._engine()
    assert docs == ["keep"]  # kept current data, no crash
