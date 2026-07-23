"""
Shared Document Services
========================
Cross-cutting operations that touch both Supabase Storage AND Qdrant.
- delete_document: removes Supabase file(s) + their Qdrant chunks
- ingest_file: download from Supabase → chunk → embed → upsert into Qdrant
"""

import hashlib
import logging
import os
import uuid
from typing import Optional

from dotenv import load_dotenv
from qdrant_client import QdrantClient, models

from app.services import storage_service

load_dotenv()

log = logging.getLogger("document_services")
log.setLevel(logging.INFO)
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)

POLICY_NAMESPACE = uuid.UUID("12345678-1234-5678-1234-567812345678")
COLLECTION_NAME = "ucd_policies"


def _get_qdrant_client() -> QdrantClient:
    """Return a Qdrant client using env vars."""
    return QdrantClient(
        url=os.getenv("QDRANT_URL"),
        api_key=os.getenv("QDRANT_API_KEY"),
    )


# ── Delete (cascading: Supabase + Qdrant) ─────────────────────────────────────

def delete_document(file_path: str) -> dict:
    """
    Delete a document from BOTH Supabase storage AND Qdrant.
    `file_path` is the full path in the Supabase bucket (e.g. 'pdfs/doc.pdf').

    Returns a dict with counts of what was deleted.
    """
    result = {"supabase_deleted": 0, "qdrant_chunks_deleted": 0}

    # 1. Delete from Supabase
    try:
        storage_service.delete_file(file_path)
        result["supabase_deleted"] = 1
    except Exception as e:
        log.warning(f"⚠️ Supabase delete failed for {file_path}: {e}")

    # 2. Delete Qdrant chunks by source_file_id
    # The source_file_id is a UUID v5 derived from the filename
    filename = file_path.split("/")[-1]
    source_file_id = str(uuid.uuid5(POLICY_NAMESPACE, filename))

    try:
        qdrant = _get_qdrant_client()
        # Find how many points match before deleting
        count_result = qdrant.count(
            COLLECTION_NAME,
            count_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="source_file_id",
                        match=models.MatchValue(value=source_file_id),
                    )
                ]
            ),
        )
        result["qdrant_chunks_deleted"] = count_result.count if count_result else 0

        qdrant.delete(
            collection_name=COLLECTION_NAME,
            points_selector=models.Filter(
                must=[
                    models.FieldCondition(
                        key="source_file_id",
                        match=models.MatchValue(value=source_file_id),
                    )
                ]
            ),
        )
    except Exception as e:
        log.warning(f"⚠️ Qdrant delete failed for {file_path} (source_file_id={source_file_id}): {e}")

    return result


def delete_documents(file_paths: list[str]) -> dict:
    """Batch-delete multiple documents from Supabase + Qdrant."""
    total = {"supabase_deleted": 0, "qdrant_chunks_deleted": 0}
    for path in file_paths:
        res = delete_document(path)
        total["supabase_deleted"] += res["supabase_deleted"]
        total["qdrant_chunks_deleted"] += res["qdrant_chunks_deleted"]
    return total


def delete_all_documents() -> dict:
    """Delete ALL files from Supabase and ALL vectors from Qdrant."""
    result = {"supabase_deleted": 0, "qdrant_cleared": False}

    # Delete all Supabase files
    try:
        files = storage_service.list_files()
        storage_service.delete_files(files)
        result["supabase_deleted"] = len(files)
    except Exception as e:
        log.warning(f"⚠️ Supabase delete-all failed: {e}")

    # Clear Qdrant collection
    try:
        qdrant = _get_qdrant_client()
        qdrant.delete_collection(COLLECTION_NAME)
        qdrant.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=models.VectorParams(size=1024, distance=models.Distance.COSINE),
            sparse_vectors_config={"sparse": models.SparseVectorParams()},
        )
        qdrant.create_payload_index(
            collection_name=COLLECTION_NAME,
            field_name="source_file_id",
            field_schema=models.PayloadSchemaType.KEYWORD,
        )
        result["qdrant_cleared"] = True
    except Exception as e:
        log.warning(f"⚠️ Qdrant clear failed: {e}")

    return result


# ── Ingest single file ────────────────────────────────────────────────────────

def ingest_file_from_supabase(file_path: str) -> dict:
    """
    Download a single file from Supabase, chunk it, embed, and upsert into Qdrant.
    `file_path` is the full path in the bucket (e.g. 'markdown/doc.md').

    Returns a dict with counts.
    """
    from FlagEmbedding import BGEM3FlagModel
    from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

    model = BGEM3FlagModel(os.getenv("EMBEDDING_MODEL_NAME", "BAAI/bge-m3"), use_fp16=True)

    # Download from Supabase
    try:
        raw = storage_service.download_file(file_path)
        md_content = raw.decode("utf-8")
    except Exception as e:
        raise RuntimeError(f"Failed to download {file_path}: {e}")

    filename = file_path.split("/")[-1]
    source_file_id = str(uuid.uuid5(POLICY_NAMESPACE, filename))

    # Delete existing chunks for this file first (idempotent re-ingest)
    qdrant = _get_qdrant_client()
    try:
        qdrant.delete(
            collection_name=COLLECTION_NAME,
            points_selector=models.Filter(
                must=[
                    models.FieldCondition(
                        key="source_file_id",
                        match=models.MatchValue(value=source_file_id),
                    )
                ]
            ),
        )
    except Exception:
        pass

    # Split
    headers_to_split_on = [("#", "Header 1"), ("##", "Header 2"), ("###", "Header 3")]
    markdown_splitter = MarkdownHeaderTextSplitter(headers_to_split_on)
    char_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)

    md_splits = markdown_splitter.split_text(md_content)
    char_splits = char_splitter.split_documents(md_splits)

    if not char_splits:
        return {"chunks": 0, "filename": filename}

    # Embed and upsert
    points_to_upload = []
    for idx, chunk in enumerate(char_splits):
        chunk_id = str(uuid.uuid5(uuid.UUID(source_file_id), f"chunk_{idx}"))
        text_content = chunk.page_content

        embeddings = model.encode(text_content, return_dense=True, return_sparse=True)
        dense_vector = embeddings["dense_vecs"]
        lexical_weights = embeddings["lexical_weights"]

        sparse_indices = []
        sparse_values = []
        for word, weight in lexical_weights.items():
            hash_int = int(hashlib.sha256(word.encode("utf-8")).hexdigest(), 16)
            sparse_indices.append(hash_int % (10**9))
            sparse_values.append(weight)

        # Build metadata
        metadata = {
            "source_file_name": filename,
            "source_file_id": source_file_id,
        }
        for key, value in chunk.metadata.items():
            metadata[key] = value

        points_to_upload.append(
            models.PointStruct(
                id=chunk_id,
                payload={"text": text_content, **metadata},
                vector={
                    "": dense_vector,
                    "sparse": models.SparseVector(
                        indices=sparse_indices, values=sparse_values
                    ),
                },
            )
        )

    if points_to_upload:
        qdrant.upsert(collection_name=COLLECTION_NAME, points=points_to_upload)

    return {"chunks": len(points_to_upload), "filename": filename, "chars": len(md_content)}


def ingest_all_from_supabase(markdown_prefix: str = "markdown/") -> dict:
    """
    Download ALL markdown files from Supabase, chunk, embed, and upsert into Qdrant.
    This replaces the old local-directory-based ingestion.
    """
    from FlagEmbedding import BGEM3FlagModel
    from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

    model = BGEM3FlagModel(os.getenv("EMBEDDING_MODEL_NAME", "BAAI/bge-m3"), use_fp16=True)

    # List all markdown files in the bucket
    all_files = storage_service.list_files()
    md_files = [f for f in all_files if f.endswith(".md") and f.startswith(markdown_prefix)]

    if not md_files:
        log.warning("No markdown files found in Supabase bucket.")
        return {"files_processed": 0, "total_chunks": 0, "files_succeeded": 0, "total_vectors": 0, "unique_files": 0}

    total_chunks = 0
    files_succeeded = 0
    total_files = len(md_files)
    qdrant = _get_qdrant_client()

    headers_to_split_on = [("#", "Header 1"), ("##", "Header 2"), ("###", "Header 3")]
    markdown_splitter = MarkdownHeaderTextSplitter(headers_to_split_on)
    char_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)

    for file_idx, md_path in enumerate(md_files, start=1):
        try:
            raw = storage_service.download_file(md_path)
            md_content = raw.decode("utf-8")
        except Exception as e:
            log.warning(f"⚠️ Failed to download {md_path}: {e}")
            continue

        filename = md_path.split("/")[-1]
        source_file_id = str(uuid.uuid5(POLICY_NAMESPACE, filename))

        # Delete existing chunks
        try:
            qdrant.delete(
                collection_name=COLLECTION_NAME,
                points_selector=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="source_file_id",
                            match=models.MatchValue(value=source_file_id),
                        )
                    ]
                ),
            )
        except Exception:
            pass

        md_splits = markdown_splitter.split_text(md_content)
        char_splits = char_splitter.split_documents(md_splits)

        if not char_splits:
            continue

        points_to_upload = []
        for idx, chunk in enumerate(char_splits):
            chunk_id = str(uuid.uuid5(uuid.UUID(source_file_id), f"chunk_{idx}"))
            embeddings = model.encode(chunk.page_content, return_dense=True, return_sparse=True)

            sparse_indices = []
            sparse_values = []
            for word, weight in embeddings["lexical_weights"].items():
                hash_int = int(hashlib.sha256(word.encode("utf-8")).hexdigest(), 16)
                sparse_indices.append(hash_int % (10**9))
                sparse_values.append(weight)

            metadata = {"source_file_name": filename, "source_file_id": source_file_id}
            for key, value in chunk.metadata.items():
                metadata[key] = value

            points_to_upload.append(
                models.PointStruct(
                    id=chunk_id,
                    payload={"text": chunk.page_content, **metadata},
                    vector={
                        "": embeddings["dense_vecs"],
                        "sparse": models.SparseVector(indices=sparse_indices, values=sparse_values),
                    },
                )
            )

        if points_to_upload:
            qdrant.upsert(collection_name=COLLECTION_NAME, points=points_to_upload)
            total_chunks += len(points_to_upload)
            files_succeeded += 1
            pct = file_idx * 100 // total_files
            log.info(f"  📝 File {file_idx}/{total_files} ({pct}%) — {filename}: {len(points_to_upload)} chunks ({len(md_content):,} chars)")

    # ── Query Qdrant collection info for summary ───────────────────────────
    collection_info = {"total_vectors": 0, "unique_files": 0}
    try:
        info = qdrant.get_collection(COLLECTION_NAME)
        collection_info["total_vectors"] = info.points_count if info else 0

        # Count distinct source_file_name values in Qdrant (actual ingested files)
        unique_names = set()
        scroll_result = qdrant.scroll(
            COLLECTION_NAME,
            with_payload=["source_file_name"],
            limit=10000,
        )
        for point in scroll_result[0]:
            if point.payload and point.payload.get("source_file_name"):
                unique_names.add(point.payload["source_file_name"])
        collection_info["unique_files"] = len(unique_names)
    except Exception:
        pass

    return {"files_processed": len(md_files), "total_chunks": total_chunks,
            "files_succeeded": files_succeeded, **collection_info}


def ensure_qdrant_collection() -> None:
    """Ensure the Qdrant collection exists, creating it if needed."""
    qdrant = _get_qdrant_client()
    if not qdrant.collection_exists(collection_name=COLLECTION_NAME):
        qdrant.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=models.VectorParams(size=1024, distance=models.Distance.COSINE),
            sparse_vectors_config={"sparse": models.SparseVectorParams()},
        )
        qdrant.create_payload_index(
            collection_name=COLLECTION_NAME,
            field_name="source_file_id",
            field_schema=models.PayloadSchemaType.KEYWORD,
        )
        log.info(f"Created Qdrant collection '{COLLECTION_NAME}'")
