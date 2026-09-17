"""Tests for storage helpers (BlobServiceClient mocked via _service)."""
import pytest

import storage


# --------------------------------------------------- connection/url selection

def test_conn_prefers_corpus_storage(monkeypatch):
    monkeypatch.setenv("CORPUS_STORAGE", "corpus-conn")
    monkeypatch.setenv("AzureWebJobsStorage", "jobs-conn")
    assert storage._conn() == "corpus-conn"


def test_conn_falls_back_to_webjobs(monkeypatch):
    monkeypatch.delenv("CORPUS_STORAGE", raising=False)
    monkeypatch.setenv("AzureWebJobsStorage", "jobs-conn")
    assert storage._conn() == "jobs-conn"


def test_account_url_from_service_uri(monkeypatch):
    monkeypatch.setenv("AzureWebJobsStorage__blobServiceUri", "https://acct.blob.core.windows.net")
    assert storage._account_url() == "https://acct.blob.core.windows.net"


def test_account_url_from_account_name(monkeypatch):
    monkeypatch.delenv("CORPUS_STORAGE__blobServiceUri", raising=False)
    monkeypatch.delenv("AzureWebJobsStorage__blobServiceUri", raising=False)
    monkeypatch.delenv("CORPUS_STORAGE__accountName", raising=False)
    monkeypatch.setenv("AzureWebJobsStorage__accountName", "acct")
    assert storage._account_url() == "https://acct.blob.core.windows.net"


def test_service_raises_without_config(monkeypatch):
    for k in ("CORPUS_STORAGE", "AzureWebJobsStorage",
              "CORPUS_STORAGE__blobServiceUri", "AzureWebJobsStorage__blobServiceUri",
              "CORPUS_STORAGE__accountName", "AzureWebJobsStorage__accountName"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError):
        storage._service()


# --------------------------------------------------- blob ops via fake service

class _FakeBlob:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def upload_blob(self, data, overwrite=False):
        self.store[self.name] = data

    def download_blob(self):
        if self.name not in self.store:
            raise KeyError(self.name)
        store, name = self.store, self.name

        class _Stream:
            def readall(self_inner):
                return store[name]
            properties = type("P", (), {"etag": "etag-xyz"})()
        return _Stream()

    def get_blob_properties(self):
        return type("P", (), {"etag": "etag-xyz"})()


class _FakeContainer:
    def __init__(self, store):
        self.store = store

    def get_blob_client(self, name):
        return _FakeBlob(self.store, name)


class _FakeService:
    def __init__(self, store):
        self.store = store

    def create_container(self, name):
        pass

    def get_container_client(self, name):
        return _FakeContainer(self.store)


@pytest.fixture
def fake_store(monkeypatch):
    store = {}
    monkeypatch.setattr(storage, "_service", lambda: _FakeService(store))
    return store


def test_upload_and_download_corpus(fake_store):
    etag = storage.upload_corpus(b"corpusbytes")
    assert etag == "etag-xyz"
    data, etag2 = storage.download_corpus()
    assert data == b"corpusbytes" and etag2 == "etag-xyz"


def test_download_corpus_missing_returns_none(fake_store):
    assert storage.download_corpus() == (None, None)


def test_get_corpus_etag(fake_store):
    storage.upload_corpus(b"x")
    assert storage.get_corpus_etag() == "etag-xyz"


def test_upload_download_named_blob(fake_store):
    storage.upload_blob("demo.docs.json", b"[1,2,3]")
    assert storage.download_blob("demo.docs.json") == b"[1,2,3]"


def test_download_blob_missing_returns_none(fake_store):
    assert storage.download_blob("nope.json") is None
