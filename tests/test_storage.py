"""Offline tests for Search schema mapping, synchronization, and authorization."""
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import sources
import storage
from source_base import Source


def _source(key="publication", classification="public", group=""):
    return Source(key, key.title(), lambda: [], lambda raw: raw,
                  classification=classification, entitlement_group_id=group)


def _record(key="publication", record_id="pub-1", **extra):
    return {
        "id": record_id, "source": key, "title": "Asthma research",
        "url": "https://example.org/study", "snippet": "An asthma study",
        "_body": "Full asthma research text", **extra,
    }


class SearchResults(list):
    def __init__(self, rows=(), count=0):
        super().__init__(rows)
        self.count = count

    def get_count(self):
        return self.count


class FakeSearch:
    def __init__(self):
        self.documents = {}
        self.calls = []
        self.fail_upload = False
        self.fail_delete = False

    def search(self, **kwargs):
        self.calls.append(("search", kwargs))
        clauses = re.findall(r"source eq '([^']+)'", kwargs.get("filter", ""))
        rows = [dict(doc) for doc in self.documents.values() if doc["source"] in clauses]
        if "security_type" in kwargs.get("filter", ""):
            permitted = []
            for row in rows:
                clause = next(piece for piece in kwargs["filter"].split(" or ")
                              if f"source eq '{row['source']}'" in piece)
                if f"security_type eq '{row['security_type']}'" not in clause:
                    continue
                if row["security_type"] == "restricted" and not any(
                    f"g eq {storage._literal(group)}" in clause for group in row["allowed_group_ids"]
                ):
                    continue
                permitted.append(row)
            rows = permitted
        if kwargs["search_text"] != "*":
            words = kwargs["search_text"].split()
            rows = [row for row in rows if any(word in row["content"].lower() for word in words)]
        if kwargs.get("order_by"):
            rows.sort(key=lambda row: row["ingested_at"], reverse=True)
        count = len(rows)
        rows = rows[:kwargs.get("top", len(rows))]
        for row in rows:
            row["@search.score"] = 1.23456
        return SearchResults(rows, count=count)

    def _write(self, action, documents, fail):
        self.calls.append((action, documents))
        results = []
        for document in documents:
            if not fail:
                if action == "upload":
                    self.documents[document["id"]] = dict(document)
                else:
                    self.documents.pop(document["id"], None)
            results.append(SimpleNamespace(
                key=document["id"], succeeded=not fail,
                status_code=503 if fail else 200, error_message="busy" if fail else None))
        return results

    def upload_documents(self, *, documents):
        return self._write("upload", documents, self.fail_upload)

    def delete_documents(self, *, documents):
        return self._write("delete", documents, self.fail_delete)


@pytest.fixture
def client(monkeypatch):
    fake = FakeSearch()
    monkeypatch.setattr(storage, "_client", lambda: fake)
    return fake


def test_client_requires_endpoint_and_uses_cloud_identity(monkeypatch):
    storage._client.cache_clear()
    monkeypatch.delenv("AZURE_SEARCH_ENDPOINT", raising=False)
    with pytest.raises(RuntimeError, match="AZURE_SEARCH_ENDPOINT"):
        storage._client()
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://service.search.azure.us/")
    monkeypatch.setenv("AZURE_SEARCH_INDEX_NAME", "test-index")
    monkeypatch.setenv("AZURE_SEARCH_AUDIENCE", "https://search.azure.us")
    credential = object()
    monkeypatch.setattr(storage, "DefaultAzureCredential", lambda: credential)
    constructor = Mock()
    monkeypatch.setattr(storage, "SearchClient", constructor)
    try:
        storage._client()
        storage._client()
        constructor.assert_called_once_with(
            endpoint="https://service.search.azure.us", index_name="test-index",
            credential=credential, audience="https://search.azure.us", api_version="2024-07-01")
    finally:
        storage._client.cache_clear()


@pytest.mark.parametrize("key,fixture", [
    ("publication", "publications_raw"), ("project", "projects_raw"),
    ("ihcc", "ihcc_raw"), ("ccdi", "ccdi_payload"),
])
def test_real_source_records_match_every_index_field(request, key, fixture):
    source = sources.get_source(key)
    records = source.normalize(request.getfixturevalue(fixture))
    schema = json.loads((Path(__file__).parent.parent / "index.json").read_text())
    fields = {field["name"] for field in schema["fields"]}
    for record in records:
        document = storage.search_document(source, record, "2026-11-13T20:00:00Z")
        assert set(document) == fields
        assert document["content"] == record["_body"]
        assert document["security_type"] == source.classification
        assert document["allowed_group_ids"] == []
        assert re.fullmatch(r"[a-zA-Z0-9_-]+", document["id"])
        assert storage._record_id(document["id"]) == record["id"]


def test_mapping_preserves_institution_boundaries_and_partial_dates():
    record = _record(
        institutions="University, School of Medicine, Other University",
        _institutions=["University, School of Medicine", "Other University"],
        date="2022-10", citations="0", enrollment="", unknown="not indexed")
    document = storage.search_document(_source(), record, "2026-11-13T20:00:00Z")
    assert document["institutions"] == ["University, School of Medicine", "Other University"]
    assert document["date"] == "2022-10"
    assert document["publication_year"] == 2022
    assert document["citations"] == 0 and document["enrollment"] is None
    assert "unknown" not in document and "_body" not in document


def test_restricted_mapping_uses_configuration_not_untrusted_record_permissions():
    document = storage.search_document(
        _source("ihcc", "restricted", "GROUP"),
        _record("ihcc", "cohort/with spaces", security_type="public", allowed_group_ids=["OTHER"]),
        "2026-11-13T20:00:00Z")
    assert document["security_type"] == "restricted"
    assert document["allowed_group_ids"] == ["GROUP"]
    assert storage._record_id(document["id"]) == "cohort/with spaces"
    assert storage._key("ihcc", "same") != storage._key("ccdi", "same")


@pytest.mark.parametrize("source,record,error", [
    (_source(classification="unknown"), _record(), "Unknown classification"),
    (_source(classification="restricted"), _record(), "no entitlement group"),
    (_source(), _record(title=""), "requires 'title'"),
    (_source(), _record(source="ihcc"), "does not match"),
    (_source(), _record(citations="bad"), "Invalid integer"),
    (_source(), _record(enrollment=1.5), "Invalid integer"),
    (_source(), _record(citations=2 ** 63), "out of range"),
    (_source(), _record(authors=[1]), "string collection"),
])
def test_invalid_records_are_rejected_before_network(client, source, record, error):
    with pytest.raises(ValueError, match=error):
        storage.replace_dataset(source, [record])
    assert client.calls == []


def test_upload_then_remove_stale_records_and_clear_old_optional_fields(client):
    source = _source()
    storage.replace_dataset(source, [_record(record_id="keep", journal="Old"),
                                     _record(record_id="remove")])
    other = storage.search_document(_source("ihcc"), _record("ihcc"), "2026-11-13T20:00:00Z")
    client.documents[other["id"]] = other
    client.calls.clear()
    assert storage.replace_dataset(source, [_record(record_id="keep")]) == 1
    assert set(client.documents) == {storage._key("publication", "keep"), other["id"]}
    assert client.documents[storage._key("publication", "keep")]["journal"] is None
    assert [action for action, _ in client.calls] == ["search", "upload", "delete"]
    assert "ingested_at" in client.documents[storage._key("publication", "keep")]


def test_partial_upload_failure_does_not_delete_previous_documents(client):
    storage.replace_dataset(_source(), [_record(record_id="old")])
    client.calls.clear()
    client.fail_upload = True
    with pytest.raises(RuntimeError, match="Search upload failed"):
        storage.replace_dataset(_source(), [_record(record_id="new")])
    assert storage._key("publication", "old") in client.documents
    assert not any(action == "delete" for action, _ in client.calls)


def test_delete_failure_is_reported(client):
    storage.replace_dataset(_source(), [_record()])
    client.fail_delete = True
    with pytest.raises(RuntimeError, match="Search delete failed"):
        storage.replace_dataset(_source(), [])


def test_duplicate_ids_and_oversized_documents_do_not_mutate_index(client, monkeypatch):
    with pytest.raises(ValueError, match="Duplicate"):
        storage.replace_dataset(_source(), [_record(), _record()])
    monkeypatch.setattr(storage, "MAX_BATCH_BYTES", 100)
    with pytest.raises(ValueError, match="exceeds"):
        storage.replace_dataset(_source(), [_record()])
    assert not client.calls


def test_batches_obey_document_count_and_encoded_byte_limits(monkeypatch):
    documents = [{"id": str(i), "content": "x"} for i in range(1001)]
    assert [len(batch) for batch in storage._batches(documents, "upload")] == [1000, 1]
    monkeypatch.setattr(storage, "MAX_BATCH_BYTES", 200)
    documents = [{"id": str(i), "content": "\u00e9" * 10} for i in range(3)]
    batches = storage._batches(documents, "upload")
    assert len(batches) == 3
    for batch in batches:
        payload = {"value": [{"@search.action": "upload", **doc} for doc in batch]}
        assert len(json.dumps(payload).encode("utf-8")) <= 200


def test_missing_acknowledgements_stop_cleanup(client, monkeypatch):
    monkeypatch.setattr(client, "upload_documents", lambda **kwargs: [])
    with pytest.raises(RuntimeError, match="acknowledgements"):
        storage.replace_dataset(_source(), [_record()])
    assert not any(action == "delete" for action, _ in client.calls)


def test_failed_later_upload_batch_prevents_all_cleanup(client, monkeypatch):
    storage.replace_dataset(_source(), [_record(record_id="old")])
    monkeypatch.setattr(storage, "MAX_BATCH_DOCUMENTS", 1)
    upload = client.upload_documents
    calls = []

    def fail_second(**kwargs):
        calls.append(kwargs)
        client.fail_upload = len(calls) == 2
        return upload(**kwargs)

    monkeypatch.setattr(client, "upload_documents", fail_second)
    with pytest.raises(RuntimeError, match="Search upload failed"):
        storage.replace_dataset(_source(), [_record(record_id="new1"), _record(record_id="new2")])
    assert storage._key("publication", "old") in client.documents
    assert storage._key("publication", "new1") in client.documents


def test_health_counts_indexed_sources_without_a_manifest_blob(client, monkeypatch):
    source = _source()
    monkeypatch.setattr(storage, "get_enabled_sources", lambda: [source, _source("ihcc")])
    storage.replace_dataset(source, [_record()])
    manifest = storage.load_manifest()
    assert manifest["total"] == 1
    assert manifest["datasets"][0]["status"] == "indexed"
    assert manifest["datasets"][0]["updated_at"]
    assert manifest["datasets"][1]["count"] == 0
    assert manifest["datasets"][1]["status"] == "empty"
    assert manifest["datasets"][1]["updated_at"] is None


def test_search_filters_entitlements_and_returns_legacy_field_types(client, monkeypatch):
    public = _source()
    restricted = _source("ihcc", "restricted", "G'1")
    monkeypatch.setattr(storage, "get_enabled_sources", lambda: [public, restricted, _source("ccdi")])
    storage.replace_dataset(public, [_record(institutions=["University, Medical School"], citations="9")])
    results = storage.search_records("asthma + research", ["publication", "ihcc"], top=3)
    request = client.calls[-1][1]
    assert request["search_text"] == "asthma research" and request["top"] == 3
    assert "(source eq 'publication' and security_type eq 'public')" in request["filter"]
    assert "source eq 'ihcc' and security_type eq 'restricted'" in request["filter"]
    assert "allowed_group_ids/any(g: g eq 'G''1')" in request["filter"]
    assert "ccdi" not in request["filter"]
    assert "content" not in request["select"] and "allowed_group_ids" not in request["select"]
    assert results[0]["id"] == "pub-1"
    assert results[0]["institutions"] == "University, Medical School"
    assert results[0]["citations"] == "9" and results[0]["score"] == 1.235
    assert "content" not in results[0] and "security_type" not in results[0]


def test_unentitled_source_and_empty_query_never_query_search(client):
    assert storage.search_records("asthma", ["publication"], "ihcc") == []
    assert storage.search_records("***", ["publication"]) == []
    assert not client.calls


def test_source_alias_and_auth_bypass_still_limit_enabled_sources(client, monkeypatch):
    monkeypatch.setattr(storage, "get_enabled_sources", lambda: [_source()])
    storage.search_records("asthma", ["publication", "disabled"], "publications", enforce_security=False)
    assert client.calls[-1][1]["filter"] == "(source eq 'publication')"


def test_stale_classification_and_group_are_excluded_until_reimport(client, monkeypatch):
    source = _source("ihcc")
    monkeypatch.setattr(storage, "get_enabled_sources", lambda: [source])
    storage.replace_dataset(source, [_record("ihcc")])
    source.classification, source.entitlement_group_id = "restricted", "NEW"
    assert storage.search_records("asthma", ["ihcc"]) == []
    storage.replace_dataset(source, [_record("ihcc")])
    assert storage.search_records("asthma", ["ihcc"])
    source.entitlement_group_id = "CHANGED"
    assert storage.search_records("asthma", ["ihcc"]) == []


@pytest.mark.smoke
def test_real_sdk_import_search_and_health_round_trip(monkeypatch):
    """Run real SDK serialization/paging against an in-memory HTTP transport."""
    import time
    from azure.core.credentials import AccessToken
    from azure.core.pipeline.transport import HttpResponse, HttpTransport
    from azure.search.documents import SearchClient
    import refresh_job
    from conftest import load_fixture

    backend = FakeSearch()
    requests = []

    class Response(HttpResponse):
        def __init__(self, request, payload):
            super().__init__(request, None)
            self.status_code = 200
            self.headers = {"content-type": "application/json"}
            self.content_type = "application/json"
            self.payload = json.dumps(payload).encode()

        def body(self):
            return self.payload

        def json(self):
            return json.loads(self.payload)

    class Transport(HttpTransport):
        def open(self):
            pass

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def send(self, request, **kwargs):
            body = json.loads(request.body)
            requests.append((request.url, body))
            if "value" in body:
                action = body["value"][0]["@search.action"]
                documents = [{key: value for key, value in doc.items() if key != "@search.action"}
                             for doc in body["value"]]
                results = backend._write(action, documents, False)
                return Response(request, {"value": [
                    {"key": result.key, "status": True, "statusCode": 200, "errorMessage": None}
                    for result in results]})
            rows = backend.search(
                search_text=body["search"], filter=body.get("filter", ""),
                top=body.get("top", 100000), order_by=body.get("orderby"))
            return Response(request, {"@odata.count": rows.count, "value": list(rows)})

    credential = SimpleNamespace(get_token=lambda *args, **kwargs: AccessToken(
        "offline-test-token", int(time.time()) + 3600))
    sdk = SearchClient(
        "https://offline.search.windows.net", "allofus-content",
        credential, transport=Transport(), api_version="2024-07-01")
    monkeypatch.setattr(storage, "_client", lambda: sdk)
    fixtures = {
        "publication": "publications.json", "project": "projects.json",
        "ihcc": "ihcc_cohorts.json", "ccdi": "ccdi_payload.json",
    }
    for source in sources.get_enabled_sources():
        monkeypatch.setattr(source, "fetch", lambda key=source.key: load_fixture(fixtures[key]))
    try:
        summary = refresh_job.rebuild_and_upload()
        assert summary["records"] == len(backend.documents) > 0
        assert set(summary["datasets"]) == set(fixtures)
        assert all(status == "live" for status in summary["sources"].values())
        first_keys = set(backend.documents)
        refresh_job.rebuild_and_upload()
        assert set(backend.documents) == first_keys
        results = storage.search_records("childhood asthma cancer cohort", list(fixtures))
        assert results and all(row["url"] for row in results)
        assert storage.load_manifest()["total"] == summary["records"]
        assert all("api-version=2024-07-01" in url for url, _ in requests)
    finally:
        sdk.close()
