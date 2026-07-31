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
    """Blob endpoint for identity-based connections (managed identity)."""
    uri = (os.environ.get("CORPUS_STORAGE__blobServiceUri")
           or os.environ.get("AzureWebJobsStorage__blobServiceUri"))
    if uri:
        return uri
    account = (os.environ.get("CORPUS_STORAGE__accountName")
               or os.environ.get("AzureWebJobsStorage__accountName"))
    if account:
        return f"https://{account}.blob.core.windows.net"
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