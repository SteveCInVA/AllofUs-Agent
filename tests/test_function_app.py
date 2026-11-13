"""Tests for unchanged HTTP interfaces backed by Azure AI Search."""
import base64
import json
from types import SimpleNamespace

import azure.functions as func
import pytest

import function_app as fa


HANDLERS = {function.get_function_name(): function.get_user_function()
            for function in fa.app.get_functions()}


@pytest.fixture(autouse=True)
def auth_disabled(monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCED", "false")


def _req(method="GET", params=None, body=b"", headers=None):
    return func.HttpRequest(method=method, url="/api/search",
                            params=params or {}, headers=headers or {}, body=body)


def _mock_search(monkeypatch):
    calls = []

    def search(*args, **kwargs):
        calls.append((args, kwargs))
        return [{"id": "pub-1", "source": "publication", "title": "Asthma",
                 "url": "https://example.org", "score": 1.25}]

    monkeypatch.setattr(fa.storage, "search_records", search)
    return calls


def test_search_missing_query_400(monkeypatch):
    calls = _mock_search(monkeypatch)
    assert HANDLERS["search_directories"](_req()).status_code == 400
    assert not calls


def test_search_happy_preserves_response(monkeypatch):
    calls = _mock_search(monkeypatch)
    response = HANDLERS["search_directories"](_req(params={"query": "childhood asthma"}))
    payload = json.loads(response.get_body())
    assert response.status_code == 200 and payload["count"] == 1
    assert payload["query"] == "childhood asthma" and payload["directory"] == "both"
    assert payload["results"][0]["url"]
    assert calls[0][0][0] == "childhood asthma"
    assert calls[0][1] == {"enforce_security": False}


def test_post_source_filter_and_top_clamp(monkeypatch):
    calls = _mock_search(monkeypatch)
    HANDLERS["search_directories"](_req(
        "POST", body=json.dumps({"query": "asthma", "source": "ihcc", "top": 100}).encode()))
    assert calls[0][0][2:] == ("ihcc", 25)
    HANDLERS["search_directories"](_req(params={"query": "asthma", "top": "-1"}))
    assert calls[1][0][3] == 1


def test_search_backend_error_500(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("Search unavailable")

    monkeypatch.setattr(fa.storage, "search_records", fail)
    assert HANDLERS["search_directories"](_req(params={"query": "asthma"})).status_code == 500


def test_malformed_post_body_400():
    assert HANDLERS["search_directories"](_req("POST", body=b"not json")).status_code == 400


def test_health_enumerates_restricted_metadata_from_search(monkeypatch):
    manifest = {"total": 3, "datasets": [
        {"key": "publication", "count": 2},
        {"key": "ihcc", "classification": "restricted", "count": 1}]}
    monkeypatch.setattr(fa.storage, "load_manifest", lambda: manifest)
    response = HANDLERS["health"](_req())
    payload = json.loads(response.get_body())
    assert response.status_code == 200 and payload["status"] == "ok" and payload["total"] == 3
    assert payload["datasets"][1]["classification"] == "restricted"


def test_health_search_failure_is_not_an_empty_success(monkeypatch):
    def fail():
        raise RuntimeError("permission denied")

    monkeypatch.setattr(fa.storage, "load_manifest", fail)
    response = HANDLERS["health"](_req())
    assert response.status_code == 503
    assert "error" in json.loads(response.get_body())


def test_refresh_preserves_success_and_degraded_summary(monkeypatch):
    monkeypatch.setattr(fa, "rebuild_and_upload", lambda: {"records": 5})
    response = HANDLERS["refresh_now"](_req("POST"))
    assert json.loads(response.get_body()) == {"status": "refreshed", "records": 5}
    monkeypatch.setattr(fa, "rebuild_and_upload",
                        lambda: {"status": "degraded", "records": 5, "warnings": ["carried"]})
    response = HANDLERS["refresh_now"](_req("POST"))
    assert response.status_code == 200 and json.loads(response.get_body())["status"] == "degraded"


def test_refresh_write_failure_returns_500(monkeypatch):
    def fail():
        raise RuntimeError("partial upload")

    monkeypatch.setattr(fa, "rebuild_and_upload", fail)
    assert HANDLERS["refresh_now"](_req("POST")).status_code == 500


def test_timer_calls_same_import_and_logs_errors(monkeypatch, caplog):
    called = []
    monkeypatch.setattr(fa, "rebuild_and_upload", lambda: called.append(True))
    HANDLERS["refresh_timer"](SimpleNamespace(past_due=True))
    assert called == [True]

    def fail():
        raise RuntimeError("scheduled fail")

    monkeypatch.setattr(fa, "rebuild_and_upload", fail)
    HANDLERS["refresh_timer"](SimpleNamespace(past_due=False))
    assert "Scheduled refresh failed" in caplog.text


def _principal(groups=(), roles=()):
    claims = [{"typ": "groups", "val": group} for group in groups]
    claims += [{"typ": "roles", "val": role} for role in roles]
    return {"x-ms-client-principal": base64.b64encode(json.dumps({"claims": claims}).encode()).decode()}


@pytest.fixture
def enforced(monkeypatch):
    import sources
    monkeypatch.setenv("AUTH_ENFORCED", "true")
    monkeypatch.setenv("BASE_ENTITLEMENT_GROUP_ID", "BASE")
    monkeypatch.setenv("ADMIN_ROLE", "Agent.Admin")
    for key, group in (("ihcc", "G-IHCC"), ("ccdi", "G-CCDI")):
        source = sources.get_source(key)
        monkeypatch.setattr(source, "classification", "restricted")
        monkeypatch.setattr(source, "entitlement_group_id", group)


def test_search_denies_before_querying_search(enforced, monkeypatch):
    calls = _mock_search(monkeypatch)
    assert HANDLERS["search_directories"](_req(params={"query": "asthma"})).status_code == 401
    assert HANDLERS["search_directories"](_req(
        params={"query": "asthma"}, headers=_principal(groups=["OTHER"]))).status_code == 403
    assert not calls


def test_entitled_keys_and_security_flag_reach_search(enforced, monkeypatch):
    calls = _mock_search(monkeypatch)
    HANDLERS["search_directories"](_req(params={"query": "asthma"}, headers=_principal(groups=["BASE"])))
    assert set(calls[0][0][1]) == {"publication", "project"}
    assert calls[0][1] == {"enforce_security": True}
    HANDLERS["search_directories"](_req(
        params={"query": "asthma"}, headers=_principal(groups=["BASE", "G-IHCC"])))
    assert set(calls[1][0][1]) == {"publication", "project", "ihcc"}


def test_refresh_requires_admin(enforced, monkeypatch):
    monkeypatch.setattr(fa, "rebuild_and_upload", lambda: {"records": 1})
    assert HANDLERS["refresh_now"](_req("POST")).status_code == 401
    assert HANDLERS["refresh_now"](_req("POST", headers=_principal(groups=["BASE"]))).status_code == 403
    assert HANDLERS["refresh_now"](_req("POST", headers=_principal(roles=["Agent.Admin"]))).status_code == 200
