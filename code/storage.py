"""
Azure Blob Storage helpers for the refreshable corpus snapshot.

The built corpus (corpus.pkl bytes) is stored in a blob so the timer/on-demand
refresh jobs can update it without redeploying, and every Function instance can
reload the new snapshot. Uses the app's AzureWebJobsStorage account by default;
override with CORPUS_STORAGE (connection string), CORPUS_CONTAINER, CORPUS_BLOB.
"""
import os

CONTAINER = os.environ.get("CORPUS_CONTAINER", "cache")
BLOB = os.environ.get("CORPUS_BLOB", "corpus.pkl")


def _conn():
    return os.environ.get("CORPUS_STORAGE") or os.environ.get("AzureWebJobsStorage")


def _account_url():
    """Blob endpoint for identity-based connections (managed identity).

    Prefers the runtime-injected *__blobServiceUri (set correctly per cloud by the
    deploy). Falls back to constructing the URL from the account name and a
    configurable endpoint suffix (STORAGE_ENDPOINT_SUFFIX) so the same code works
    in Azure Commercial (core.windows.net) and Azure Government (core.usgovcloudapi.net).
    """
    uri = (os.environ.get("CORPUS_STORAGE__blobServiceUri")
           or os.environ.get("AzureWebJobsStorage__blobServiceUri"))
    if uri:
        return uri
    account = (os.environ.get("CORPUS_STORAGE__accountName")
               or os.environ.get("AzureWebJobsStorage__accountName"))
    if account:
        suffix = os.environ.get("STORAGE_ENDPOINT_SUFFIX", "core.windows.net")
        return f"https://{account}.blob.{suffix}"
    return None


def _service():
    from azure.storage.blob import BlobServiceClient
    conn = _conn()
    if conn:
        return BlobServiceClient.from_connection_string(conn)
    account_url = _account_url()
    if account_url:
        from azure.identity import DefaultAzureCredential
        return BlobServiceClient(account_url, credential=DefaultAzureCredential())
    raise RuntimeError(
        "No storage connection configured "
        "(AzureWebJobsStorage/CORPUS_STORAGE connection string or __accountName).")


def _blob_client(create=False):
    svc = _service()
    if create:
        try:
            svc.create_container(CONTAINER)
        except Exception:  # noqa: BLE001  (already exists)
            pass
    return svc.get_container_client(CONTAINER).get_blob_client(BLOB)


def upload_corpus(data: bytes) -> str:
    """Upload the snapshot bytes, overwriting any existing blob. Returns ETag."""
    bc = _blob_client(create=True)
    bc.upload_blob(data, overwrite=True)
    return bc.get_blob_properties().etag


def download_corpus():
    """Return (bytes, etag) for the current snapshot, or (None, None) if absent."""
    try:
        bc = _blob_client()
        stream = bc.download_blob()
        return stream.readall(), stream.properties.etag
    except Exception:  # noqa: BLE001  (missing blob / no storage)
        return None, None


def get_corpus_etag():
    """Return the current blob ETag, or None if it doesn't exist."""
    try:
        return _blob_client().get_blob_properties().etag
    except Exception:  # noqa: BLE001
        return None

def upload_blob(name: str, data: bytes) -> str:
    svc = _service()
    try:
        svc.create_container(CONTAINER)
    except Exception:
        pass
    bc = svc.get_container_client(CONTAINER).get_blob_client(name)
    bc.upload_blob(data, overwrite=True)
    return bc.get_blob_properties().etag

def download_blob(name: str):
    try:
        bc = _service().get_container_client(CONTAINER).get_blob_client(name)
        return bc.download_blob().readall()
    except Exception:
        return None


def download_blob_with_etag(name: str):
    """Return (bytes, etag) for a named blob, or (None, None) if absent."""
    try:
        bc = _service().get_container_client(CONTAINER).get_blob_client(name)
        stream = bc.download_blob()
        return stream.readall(), stream.properties.etag
    except Exception:  # noqa: BLE001
        return None, None


def get_blob_etag(name: str):
    """Return a named blob's ETag, or None if it doesn't exist."""
    try:
        bc = _service().get_container_client(CONTAINER).get_blob_client(name)
        return bc.get_blob_properties().etag
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------- per-dataset indexes + manifest

INDEX_PREFIX = os.environ.get("INDEX_PREFIX", "index/")
MANIFEST_BLOB = os.environ.get("MANIFEST_BLOB", "index/manifest.json")


def index_blob(key: str) -> str:
    return f"{INDEX_PREFIX}{key}.pkl"


def upload_index(key: str, data: bytes) -> str:
    """Upload one dataset's index artifact. Returns ETag."""
    return upload_blob(index_blob(key), data)


def download_index(key: str):
    """Return (bytes, etag) for a dataset's index, or (None, None) if absent."""
    return download_blob_with_etag(index_blob(key))


def get_index_etag(key: str):
    return get_blob_etag(index_blob(key))


def upload_manifest(data: bytes) -> str:
    return upload_blob(MANIFEST_BLOB, data)


def download_manifest():
    return download_blob(MANIFEST_BLOB)