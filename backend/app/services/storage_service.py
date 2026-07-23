"""
Shared Supabase Storage Service
================================
Single source of truth for all Supabase Storage operations.
Used by admin.py, scraper.py, and ingestion.py.
All credentials read from environment variables (.env).
"""

import json
import logging
import os
import random
import time
from typing import Optional

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

_supabase_client: Optional[Client] = None

log = logging.getLogger("storage_service")
log.setLevel(logging.INFO)
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)


def _reset_supabase_client() -> None:
    """Force-create a fresh Supabase client (e.g. after connection errors)."""
    global _supabase_client
    _supabase_client = None


def get_supabase_client() -> Client:
    """Return the singleton Supabase client (service_role key)."""
    global _supabase_client
    if _supabase_client is None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env"
            )
        _supabase_client = create_client(url, key)
    return _supabase_client


def get_bucket_name() -> str:
    """Return the configured bucket name from env. Defaults to 'Bucket' if not set."""
    bucket = os.getenv("SUPABASE_BUCKET")
    if not bucket:
        bucket = "Bucket"
    return bucket


def ensure_bucket_exists() -> str:
    """Ensure the configured bucket exists in Supabase, creating it if necessary.
    Returns the bucket name."""
    client = get_supabase_client()
    bucket = get_bucket_name()
    try:
        buckets = client.storage.list_buckets()
        bucket_names = {b.name for b in buckets if hasattr(b, "name")}
    except Exception:
        # Fallback: try listing the bucket directly to check existence
        try:
            client.storage.from_(bucket).list()
            return bucket
        except Exception:
            bucket_names = set()

    if bucket not in bucket_names:
        try:
            client.storage.create_bucket(bucket, options={"public": False})
        except Exception as e:
            raise RuntimeError(f"Could not create bucket '{bucket}' in Supabase: {e}") from e

    return bucket


def upload_file(
    remote_path: str,
    data: bytes,
    content_type: str = "application/octet-stream",
    upsert: bool = True,
    max_retries: int = 3,
) -> str:
    """Upload raw bytes to a path in the Supabase bucket (with exponential backoff retry). Returns the storage path."""
    client = get_supabase_client()
    bucket = get_bucket_name()
    options = {"content-type": content_type}
    if upsert:
        options["upsert"] = "true"

    for attempt in range(1, max_retries + 1):
        try:
            client.storage.from_(bucket).upload(
                path=remote_path,
                file=data,
                file_options=options,
            )
            return remote_path
        except Exception as e:
            # Detect connection-level errors and reset the client for a fresh connection
            exc_str = str(e).lower()
            is_connection_error = any(kw in exc_str for kw in (
                "server disconnected", "remote protocol error",
                "connection", "timeout", "reset", "broken pipe"
            ))
            if is_connection_error and attempt < max_retries:
                _reset_supabase_client()
                client = get_supabase_client()

            if attempt < max_retries:
                jitter = random.uniform(0.5, 2.0)
                wait = (2 ** attempt) * jitter
                log.warning(f"    Upload attempt {attempt}/{max_retries} failed for {remote_path} — retrying in {wait:.1f}s: {e}")
                time.sleep(wait)
            else:
                log.error(f"    Upload FAILED after {max_retries} attempts for {remote_path}: {e}", exc_info=True)
                raise


def download_file(remote_path: str) -> bytes:
    """Download a file from Supabase as raw bytes."""
    client = get_supabase_client()
    bucket = get_bucket_name()
    return client.storage.from_(bucket).download(remote_path)


def delete_file(remote_path: str) -> None:
    """Delete a single file from Supabase storage."""
    client = get_supabase_client()
    bucket = get_bucket_name()
    client.storage.from_(bucket).remove([remote_path])


def delete_files(remote_paths: list[str]) -> None:
    """Batch-delete multiple files from Supabase storage."""
    if not remote_paths:
        return
    client = get_supabase_client()
    bucket = get_bucket_name()
    # Supabase remove supports up to 1000 files per call
    batch_size = 500
    for i in range(0, len(remote_paths), batch_size):
        batch = remote_paths[i : i + batch_size]
        client.storage.from_(bucket).remove(batch)


def list_files(prefix: str = "") -> list[str]:
    """Recursively list all file paths in the Supabase bucket."""
    client = get_supabase_client()
    bucket = get_bucket_name()
    return _list_all_files(client, bucket, prefix)


def _list_all_files(client: Client, bucket_name: str, prefix: str = "") -> list[str]:
    """Internal recursive file listing."""
    all_files: list[str] = []
    limit = 100
    offset = 0

    while True:
        try:
            resp = (
                client.storage.from_(bucket_name)
                .list(
                    prefix,
                    options={
                        "limit": limit,
                        "offset": offset,
                        "sortBy": {"column": "name", "order": "asc"},
                    },
                )
            )
        except Exception:
            break

        if not resp:
            break

        for item in resp:
            if item.get("id") is None:  # folder
                all_files.extend(
                    _list_all_files(client, bucket_name, prefix + item["name"] + "/")
                )
            else:
                all_files.append(prefix + item["name"])

        if len(resp) < limit:
            break
        offset += limit

    return all_files


def create_signed_url(remote_path: str, expires_in: int = 300) -> str:
    """Generate a signed download URL valid for `expires_in` seconds."""
    client = get_supabase_client()
    bucket = get_bucket_name()
    result = client.storage.from_(bucket).create_signed_url(remote_path, expires_in)
    if isinstance(result, dict):
        return result.get("signedURL", result.get("signed_url", str(result)))
    return str(result)


def file_exists(remote_path: str) -> bool:
    """Check whether a file exists in the bucket."""
    client = get_supabase_client()
    bucket = get_bucket_name()
    try:
        # Try listing with the exact path prefix to see if it exists
        parent = "/".join(remote_path.split("/")[:-1])
        filename = remote_path.split("/")[-1]
        files = _list_all_files(client, bucket, parent + "/" if parent else "")
        return any(f.endswith(filename) for f in files)
    except Exception:
        return False


# ── index.json management (stored in Supabase bucket root) ────────────────────

INDEX_JSON_PATH = "index.json"


def upload_index_json(data: dict) -> str:
    """Upload the scraper's index.json to the Supabase bucket root."""
    content = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    return upload_file(INDEX_JSON_PATH, content, "application/json")


def download_index_json() -> dict:
    """Download index.json from the Supabase bucket root. Returns empty dict if not found."""
    try:
        raw = download_file(INDEX_JSON_PATH)
        parsed = json.loads(raw.decode("utf-8"))
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {}
        return {}
    except Exception:
        return {}
