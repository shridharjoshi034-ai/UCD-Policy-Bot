"""
Ingestion Module
================
Reads ALL markdown files from the Supabase bucket's `markdown/` folder,
chunks them, embeds them, and upserts into Qdrant.

No local file fallback — everything flows through Supabase Storage.
The scraper uploads MD files to `markdown/`, and ingestion reads them back.
"""

import logging

from app.services.document_services import (
    ingest_all_from_supabase,
    ingest_file_from_supabase,
    ensure_qdrant_collection,
)

log = logging.getLogger("ingestion")
log.setLevel(logging.INFO)
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)


def ingest() -> None:
    """
    Download ALL markdown files from Supabase bucket's `markdown/` folder,
    chunk, embed, and upsert into Qdrant.
    """
    ensure_qdrant_collection()
    result = ingest_all_from_supabase("markdown/")

    total_vecs = result.get("total_vectors", 0)
    unique_files = result.get("unique_files", result.get("files_succeeded", result.get("files_processed", 0)))

    log.info("=" * 60)
    log.info(f"Qdrant: ucd_policies · {total_vecs:,} vectors across {unique_files} files")
    log.info(f"Ingestion complete — {result['files_processed']} files processed, {result['total_chunks']} chunks upserted")
    log.info("=" * 60)


def ingest_file(file_path: str) -> dict:
    """
    Ingest a SINGLE file from Supabase into Qdrant.
    `file_path` is the full path in the bucket (e.g. 'markdown/doc.md').

    Returns a dict with {chunks, filename, chars}.
    """
    ensure_qdrant_collection()
    return ingest_file_from_supabase(file_path)


if __name__ == "__main__":
    ingest()
