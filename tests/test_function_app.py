"""Tests for function_app: HTTP handlers, per-entitlement engine, manifest health."""
import json

import azure.functions as func
import pytest

import function_app as fa
import search_core


HANDLERS = {f.get_function_name(): f.get_user_function()
            for f in fa.app.get_functions()}


@pytest.fixture(autouse=True)
def reset_caches():
    fa._index_cache.clear()
    fa._engine_cache.clear()
    fa._LAST_CHECK = 0.0
    yield
    fa._index_cache.clear()
    fa._engine_cache.clear()
    fa._LAST_CHECK = 0.0


def _req(method="GET", params=None, body=b""):
    return func.HttpRequest(method=method, url="/api/search",
                            params=params or {}, body=body)


def _mock_engine(monkeypatch, sample_docs):
    docs, bm25 = search_core.load_engine_from_bytes(
        search_core.build_artifact_bytes(sample_docs)[0])
    monkeypatch.setattr(fa, "_engine", lambda keys: (docs, bm25))
    return docs


# ------------------------------------------------------------------- search

def test_search_missing_query_400(monkeypatch, sample_docs):
    _mock_engine(monkeypatch, sample_docs)
    assert HANDLERS["search_directories"](_req(params={})).status_code == 400


def test_search_happy(monkeypatch, sample_docs):
    _mock_engine(monkeypatch, sample_docs)
    resp = HANDLERS["search_directories"](_req(params={"query": "childhood asthma"}))
    body = json.loads(resp.get_body())
    assert resp.status_code == 200 and body["count"] >= 1
    assert body["results"][0]["url"]


def test_search_source_filter(monkeypatch, sample_docs):
    _mock_engine(monkeypatch, sample_docs)
    resp = HANDLERS["search_directories"](
        _req(method="POST", body=json.dumps({"query": "asthma", "directory": "ihcc"}).encode()))
    body = json.loads(resp.get_body())
    assert all(r["source"] == "ihcc" for r in body["results"])


def test_search_top_clamped(monkeypatch, sample_docs):
    _mock_engine(monkeypatch, sample_docs)
    resp = HANDLERS["search_directories"](_req(params={"query": "asthma", "top": "0"}))
    assert len(json.loads(resp.get_body())["results"]) == 1


def test_search_engine_error_500(monkeypatch):
    def boom(keys):
        raise RuntimeError("engine down")
    monkeypatch.setattr(fa, "_engine", boom)
    assert HANDLERS["search_directories"](_req(params={"query": "asthma"})).status_code == 500


# ------------------------------------------------------------------- health

def test_health_enumerates_all_datasets(monkeypatch):
    manifest = {"total": 396, "datasets": [
        {"key": "publication", "name": "Publication", "classification": "public", "count": 87},
        {"key": "ihcc", "name": "Cohort", "classification": "restricted", "count": 309}]}
    monkeypatch.setattr(fa.storage, "download_manifest",
                        lambda: json.dumps(manifest).encode())
    resp = HANDLERS["health"](_req())
    body = json.loads(resp.get_body())
    assert body["status"] == "ok" and body["total"] == 396
    keys = {d["key"] for d in body["datasets"]}
    assert keys == {"publication", "ihcc"}  # restricted dataset IS enumerated
    ihcc = [d for d in body["datasets"] if d["key"] == "ihcc"][0]
    assert ihcc["classification"] == "restricted" and ihcc["count"] == 309


def test_health_empty_when_no_manifest(monkeypatch):
    monkeypatch.setattr(fa.storage, "download_manifest", lambda: None)
    monkeypatch.setattr(fa, "_pkg_path", lambda name: "/nonexistent/" + name)
    body = json.loads(HANDLERS["health"](_req()).get_body())
    assert body["status"] == "ok" and body["datasets"] == []


# ------------------------------------------------------------------- refresh

def test_refresh_now_success(monkeypatch):
    monkeypatch.setattr(fa, "rebuild_and_upload", lambda: {"records": 5})
    resp = HANDLERS["refresh_now"](_req(method="POST"))
    body = json.loads(resp.get_body())
    assert body["status"] == "refreshed" and body["records"] == 5
    assert fa._index_cache == {} and fa._engine_cache == {}  # invalidated


def test_refresh_now_error_500(monkeypatch):
    def boom():
        raise RuntimeError("refresh failed")
    monkeypatch.setattr(fa, "rebuild_and_upload", boom)
    assert HANDLERS["refresh_now"](_req(method="POST")).status_code == 500


def test_refresh_timer_runs_and_swallows_errors(monkeypatch):
    import types as _t
    called = {}
    monkeypatch.setattr(fa, "rebuild_and_upload", lambda: called.setdefault("ok", True) or {})
    HANDLERS["refresh_timer"](_t.SimpleNamespace(past_due=True))
    assert called.get("ok")

    def boom():
        raise RuntimeError("scheduled fail")
    monkeypatch.setattr(fa, "rebuild_and_upload", boom)
    HANDLERS["refresh_timer"](_t.SimpleNamespace(past_due=False))  # must not raise


def test_search_malformed_post_body_400(monkeypatch, sample_docs):
    _mock_engine(monkeypatch, sample_docs)
    resp = HANDLERS["search_directories"](_req(method="POST", body=b"not json"))
    assert resp.status_code == 400  # unparseable body -> no query -> 400


# ------------------------------------------------------------------- _engine

def _artifacts_by_key(sample_docs):
    by_key = {}
    for d in sample_docs:
        by_key.setdefault(d["source"], []).append(d)
    return {k: (search_core.build_artifact_bytes(v)[0], f"etag-{k}")
            for k, v in by_key.items()}


def test_engine_merges_entitled_indexes(monkeypatch, sample_docs):
    arts = _artifacts_by_key(sample_docs)
    monkeypatch.setattr(fa.storage, "download_index", lambda k: arts.get(k, (None, None)))
    docs, bm25 = fa._engine(["publication", "ihcc"])
    sources = {d["source"] for d in docs}
    assert sources == {"publication", "ihcc"}  # project excluded (not requested)
    assert bm25 is not None


def test_engine_caches_by_signature(monkeypatch, sample_docs):
    arts = _artifacts_by_key(sample_docs)
    monkeypatch.setattr(fa.storage, "download_index", lambda k: arts.get(k, (None, None)))
    fa._engine(["publication", "ihcc"])
    assert ("ihcc", "publication") in fa._engine_cache


def test_engine_hot_reloads_on_etag_change(monkeypatch, sample_docs):
    arts = _artifacts_by_key(sample_docs)
    monkeypatch.setattr(fa.storage, "download_index", lambda k: arts.get(k, (None, None)))
    fa._engine(["publication"])
    # a new refresh changes the etag; force the throttled check to run
    arts["publication"] = (arts["publication"][0], "etag-NEW")
    monkeypatch.setattr(fa.storage, "get_index_etag", lambda k: arts[k][1])
    fa._LAST_CHECK = 0.0
    fa._engine(["publication"])
    assert fa._index_cache["publication"]["etag"] == "etag-NEW"


def test_engine_packaged_fallback(monkeypatch, tmp_path, sample_docs):
    art = search_core.build_artifact_bytes([sample_docs[0]])[0]
    p = tmp_path / "publication.pkl"
    p.write_bytes(art)
    monkeypatch.setattr(fa.storage, "download_index", lambda k: (None, None))
    monkeypatch.setattr(fa, "_pkg_path", lambda name: str(tmp_path / name))
    docs, bm25 = fa._engine(["publication"])
    assert len(docs) == 1 and fa._index_cache["publication"]["etag"] is None


# ------------------------------------------------------- authorization (enforced)

import base64 as _b64  # noqa: E402


def _principal(groups=(), roles=()):
    claims = [{"typ": "groups", "val": g} for g in groups]
    claims += [{"typ": "roles", "val": r} for r in roles]
    return _b64.b64encode(json.dumps({"claims": claims}).encode()).decode()


@pytest.fixture
def enforced(monkeypatch):
    """Turn on enforcement with ihcc restricted; restore classification after."""
    import sources
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    monkeypatch.setenv("BASE_ENTITLEMENT_GROUP_ID", "BASE")
    monkeypatch.setenv("ADMIN_ROLE", "Agent.Admin")
    snap = [(s, s.classification, s.entitlement_group_id) for s in sources.ALL_SOURCES]
    sources.apply_classification({
        "ihcc": {"classification": "restricted", "entitlement_group_id": "G-IHCC"},
        "ccdi": {"classification": "restricted", "entitlement_group_id": "G-CCDI"},
    })
    yield
    for s, c, g in snap:
        s.classification, s.entitlement_group_id = c, g


def _search_req(principal_hdr=None, query="asthma"):
    headers = {"x-ms-client-principal": principal_hdr} if principal_hdr else {}
    return func.HttpRequest(method="GET", url="/api/search",
                            headers=headers, params={"query": query}, body=b"")


def test_search_unauthenticated_401(enforced, monkeypatch, sample_docs):
    _mock_engine(monkeypatch, sample_docs)
    assert HANDLERS["search_directories"](_search_req()).status_code == 401


def test_search_missing_base_group_403(enforced, monkeypatch, sample_docs):
    _mock_engine(monkeypatch, sample_docs)
    resp = HANDLERS["search_directories"](_search_req(_principal(groups=["OTHER"])))
    assert resp.status_code == 403


def test_search_base_user_sees_public_only(enforced, monkeypatch, sample_docs):
    captured = {}
    docs, bm25 = search_core.load_engine_from_bytes(
        search_core.build_artifact_bytes(sample_docs)[0])
    monkeypatch.setattr(fa, "_engine",
                        lambda keys: captured.update(keys=set(keys)) or (docs, bm25))
    resp = HANDLERS["search_directories"](_search_req(_principal(groups=["BASE"])))
    assert resp.status_code == 200
    assert captured["keys"] == {"publication", "project"}  # no restricted


def test_search_ihcc_entitled_gets_ihcc(enforced, monkeypatch, sample_docs):
    captured = {}
    docs, bm25 = search_core.load_engine_from_bytes(
        search_core.build_artifact_bytes(sample_docs)[0])
    monkeypatch.setattr(fa, "_engine",
                        lambda keys: captured.update(keys=set(keys)) or (docs, bm25))
    HANDLERS["search_directories"](_search_req(_principal(groups=["BASE", "G-IHCC"])))
    assert "ihcc" in captured["keys"] and "ccdi" not in captured["keys"]


def test_refresh_requires_admin(enforced, monkeypatch):
    monkeypatch.setattr(fa, "rebuild_and_upload", lambda: {"records": 1})

    def _refresh(principal_hdr=None):
        headers = {"x-ms-client-principal": principal_hdr} if principal_hdr else {}
        return HANDLERS["refresh_now"](func.HttpRequest(
            method="POST", url="/api/refresh", headers=headers, params={}, body=b""))

    assert _refresh().status_code == 401                                  # no principal
    assert _refresh(_principal(groups=["BASE"])).status_code == 403       # not admin
    assert _refresh(_principal(roles=["Agent.Admin"])).status_code == 200 # admin ok
