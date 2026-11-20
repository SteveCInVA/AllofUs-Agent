"""Azure AI Search persistence, source synchronization, and authorized retrieval."""
import base64
import datetime
import json
import os
import re
from functools import lru_cache

from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient

from search_core import resolve_filter, tokenize
from source_base import Source
from sources import get_enabled_sources

STRING_FIELDS = (
    "title", "url", "snippet", "date", "journal", "access_tier",
    "pi_lead", "organization", "study_id",
)
COLLECTION_FIELDS = ("authors", "institutions", "focus", "countries", "diseases", "data_types")
RESULT_FIELDS = (
    "id", "source", "record_type", *STRING_FIELDS, *COLLECTION_FIELDS,
    "citations", "enrollment",
)
MAX_BATCH_DOCUMENTS = 1000
MAX_BATCH_BYTES = 15 * 1024 * 1024  # Leave room below Search's 16 MB request limit.


@lru_cache(maxsize=1)
def _client() -> SearchClient:
    endpoint = os.environ.get("AZURE_SEARCH_ENDPOINT", "").strip().rstrip("/")
    if not endpoint:
        raise RuntimeError("AZURE_SEARCH_ENDPOINT is required.")
    return SearchClient(
        endpoint=endpoint,
        index_name=os.environ.get("AZURE_SEARCH_INDEX_NAME", "allofus-content"),
        credential=DefaultAzureCredential(),
        audience=os.environ.get("AZURE_SEARCH_AUDIENCE", "https://search.azure.com"),
        api_version="2024-07-01",
    )


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _source_filter(key: str) -> str:
    return f"source eq {_literal(key)}"


def _security_fields(source: Source) -> dict:
    if source.classification not in ("public", "restricted"):
        raise ValueError(f"Unknown classification for source '{source.key}'.")
    group = source.entitlement_group_id.strip()
    if source.classification == "restricted" and not group:
        raise ValueError(f"Restricted source '{source.key}' has no entitlement group.")
    return {
        "security_type": source.classification,
        "allowed_group_ids": [group] if source.classification == "restricted" else [],
    }


def _key(source: str, record_id: str) -> str:
    value = base64.urlsafe_b64encode(f"{source}:{record_id}".encode("utf-8")).decode("ascii")
    value = "v1_" + value.rstrip("=")
    if len(value) > 1024:
        raise ValueError(f"Record ID for source '{source}' exceeds the Search key limit.")
    return value


def _record_id(key: str) -> str:
    if not key.startswith("v1_"):
        return key
    encoded = key[3:]
    return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8").split(":", 1)[1]


def _integer(value, field: str):
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not re.fullmatch(r"[+-]?\d+", str(value)):
        raise ValueError(f"Invalid integer for '{field}': {value!r}.")
    number = int(value)
    bits = 32 if field == "publication_year" else 64
    if not -(2 ** (bits - 1)) <= number < 2 ** (bits - 1):
        raise ValueError(f"Integer out of range for '{field}'.")
    return number


def _strings(value, field: str) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"'{field}' must be a string collection.")
    return value


def search_document(source: Source, record: dict, ingested_at: str) -> dict:
    """Map a normalized source record to exactly the fields in index.json."""
    security = _security_fields(source)
    if record.get("source") != source.key:
        raise ValueError(f"Record source does not match '{source.key}'.")
    for field in ("id", "title", "_body"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            raise ValueError(f"Source '{source.key}' record requires '{field}'.")

    document = {
        "id": _key(source.key, record["id"]),
        "source": source.key,
        "record_type": record.get("record_type") or source.record_type,
        "content": record["_body"],
        "ingested_at": ingested_at,
        **security,
    }
    for field in STRING_FIELDS:
        value = record.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"'{field}' must be a string.")
        document[field] = value
    for field in COLLECTION_FIELDS:
        value = record.get("_institutions", record.get(field)) if field == "institutions" else record.get(field)
        document[field] = _strings(value, field)
    for field in ("citations", "enrollment"):
        document[field] = _integer(record.get(field), field)
    year = re.match(r"^(\d{4})(?:-|$)", record.get("date") or "")
    document["publication_year"] = _integer(
        record.get("publication_year", year.group(1) if year else None), "publication_year")
    return document


def _batches(documents: list, action: str) -> list:
    batches, batch, size = [], [], 32
    for document in documents:
        item_size = len(json.dumps(
            {"@search.action": action, **document}, allow_nan=False).encode("utf-8")) + 2
        if item_size + 32 > MAX_BATCH_BYTES:
            raise ValueError(f"Document '{document['id']}' exceeds the Search batch size limit.")
        if batch and (len(batch) == MAX_BATCH_DOCUMENTS or size + item_size > MAX_BATCH_BYTES):
            batches.append(batch)
            batch, size = [], 32
        batch.append(document)
        size += item_size
    if batch:
        batches.append(batch)
    return batches


def _write(client: SearchClient, batches: list, action: str) -> None:
    operation = client.upload_documents if action == "upload" else client.delete_documents
    for batch in batches:
        results = operation(documents=batch)
        expected = {document["id"] for document in batch}
        returned = {result.key for result in results}
        failures = [
            f"{result.key}: {result.status_code} {result.error_message}"
            for result in results if not result.succeeded
        ]
        if returned != expected or len(results) != len(batch) or failures:
            detail = "; ".join(failures) or "missing or duplicate document acknowledgements"
            raise RuntimeError(f"Search {action} failed: {detail}")


def replace_dataset(source: Source, records: list) -> int:
    """Upload a complete source, then delete removed IDs only after every upload succeeds."""
    _security_fields(source)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    documents = [search_document(source, record, now) for record in records]
    current = {document["id"] for document in documents}
    if len(current) != len(documents):
        raise ValueError(f"Duplicate document IDs for source '{source.key}'.")
    batches = _batches(documents, "upload")
    client = _client()
    previous = {
        row["id"] for row in client.search(
            search_text="*", filter=_source_filter(source.key), select=["id"])
    }
    _write(client, batches, "upload")
    removed = [{"id": key} for key in sorted(previous - current)]
    _write(client, _batches(removed, "delete"), "delete")
    return len(documents)


def dataset_info(source: Source) -> dict:
    """Count all source records while retrieving only the latest ingestion timestamp."""
    results = _client().search(
        search_text="*", filter=_source_filter(source.key),
        select=["ingested_at"], order_by=["ingested_at desc"],
        top=1, include_total_count=True,
    )
    count = results.get_count()
    if count is None:
        raise RuntimeError(f"Search did not return a count for '{source.key}'.")
    latest = next(iter(results), {})
    updated_at = latest.get("ingested_at")
    if isinstance(updated_at, datetime.datetime):
        updated_at = updated_at.isoformat()
    return {
        "key": source.key, "name": source.label, "classification": source.classification,
        "count": count, "status": "indexed" if count else "empty", "updated_at": updated_at,
    }


def load_manifest() -> dict:
    """Return enabled-source summaries from Search counts, without document content."""
    datasets = [dataset_info(source) for source in get_enabled_sources()]
    return {"total": sum(dataset["count"] for dataset in datasets), "datasets": datasets}


def search_records(query: str, keys: list, directory: str = "both", top: int = 8,
                   enforce_security: bool = True) -> list:
    """Query only entitled sources, checking indexed permissions against current configuration."""
    wanted = resolve_filter(directory)
    clauses = []
    for source in get_enabled_sources():
        if source.key not in keys or (wanted and wanted != source.key):
            continue
        clause = _source_filter(source.key)
        if enforce_security:
            security = _security_fields(source)
            clause += f" and security_type eq {_literal(security['security_type'])}"
            if source.classification == "restricted":
                clause += f" and allowed_group_ids/any(g: g eq {_literal(security['allowed_group_ids'][0])})"
        clauses.append(f"({clause})")
    text = " ".join(tokenize(query))
    if not clauses or not text:
        return []
    rows = _client().search(
        search_text=text, query_type="simple", search_mode="any",
        filter=" or ".join(clauses), select=list(RESULT_FIELDS), top=top,
    )
    results = []
    for row in rows:
        item = {field: row[field] for field in RESULT_FIELDS
                if field in row and row[field] not in (None, "", [])}
        item["id"] = _record_id(row["id"])
        if "institutions" in item:
            item["institutions"] = ", ".join(item["institutions"])
        if "citations" in item:
            item["citations"] = str(item["citations"])
        item["score"] = round(float(row["@search.score"]), 3)
        results.append(item)
    return results
