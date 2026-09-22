"""Tests for feeds.fetch and feeds.load_source_resilient (storage mocked)."""
import io
import json
import types

import pytest

import feeds


class _FakeResp:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return self._data


def test_fetch_success(monkeypatch):
    monkeypatch.setattr(feeds.urllib.request, "urlopen",
                        lambda req, timeout=0: _FakeResp([{"ok": 1}]))
    assert feeds.fetch("http://x") == [{"ok": 1}]


def test_fetch_retries_then_raises(monkeypatch):
    calls = {"n": 0}

    def boom(req, timeout=0):
        calls["n"] += 1
        raise OSError("net down")

    monkeypatch.setattr(feeds.urllib.request, "urlopen", boom)
    monkeypatch.setattr(feeds.httpx, "Client", lambda **kwargs: _FailingHttpxClient())
    with pytest.raises(OSError):
        feeds.fetch("http://x", retries=3)
    assert calls["n"] == 3


class _FailingHttpxClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url):
        raise OSError("HTTP/2 down")


class _HttpxResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return [{"ok": "http2"}]


class _HttpxClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url):
        return _HttpxResponse()


def test_fetch_falls_back_to_http2(monkeypatch):
    monkeypatch.setattr(feeds.urllib.request, "urlopen",
                        lambda req, timeout=0: (_ for _ in ()).throw(OSError("HTTP/1.1 down")))
    monkeypatch.setattr(feeds.httpx, "Client", lambda **kwargs: _HttpxClient())
    assert feeds.fetch("http://x", retries=1) == [{"ok": "http2"}]


def _fake_source(fetch):
    return types.SimpleNamespace(
        key="demo", cache_blob="demo.docs.json",
        fetch=fetch, normalize=lambda raw: raw)


def test_live_caches_and_returns_live(monkeypatch):
    uploaded = {}
    monkeypatch.setattr(feeds.storage, "upload_blob",
                        lambda name, data: uploaded.update({name: data}))
    src = _fake_source(lambda: [{"id": "1"}])
    docs, status = feeds.load_source_resilient(src)
    assert status == "live" and docs == [{"id": "1"}]
    assert "demo.docs.json" in uploaded


def test_fetch_fails_falls_back_to_cache(monkeypatch):
    cached = json.dumps([{"id": "cached"}]).encode()
    recached = {}
    monkeypatch.setattr(feeds.storage, "download_blob", lambda name: cached)
    monkeypatch.setattr(feeds.storage, "upload_blob",
                        lambda name, data: recached.update({name: data}))

    def boom():
        raise RuntimeError("down")

    docs, status = feeds.load_source_resilient(_fake_source(boom))
    assert status == "cache" and docs == [{"id": "cached"}]
    assert recached, "cache should be carried forward"


def test_fetch_fails_no_cache_returns_failed(monkeypatch):
    monkeypatch.setattr(feeds.storage, "download_blob", lambda name: None)

    def boom():
        raise RuntimeError("down")

    docs, status = feeds.load_source_resilient(_fake_source(boom))
    assert docs is None and status == "failed"


def test_fetch_fails_bad_cache_returns_failed(monkeypatch):
    monkeypatch.setattr(feeds.storage, "download_blob", lambda name: b"not json")

    def boom():
        raise RuntimeError("down")

    docs, status = feeds.load_source_resilient(_fake_source(boom))
    assert docs is None and status == "failed"
