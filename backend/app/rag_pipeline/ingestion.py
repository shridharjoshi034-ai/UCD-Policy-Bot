import os
import uuid
from dotenv import load_dotenv
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict

# Load env vars
load_dotenv()

from fastembed import TextEmbedding, SparseTextEmbedding
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from qdrant_client import QdrantClient, models

# ============================================================
# Model config
# Dense model name comes from .env (must match retrieval.py's EMBEDDING_MODEL_NAME).
# Everything else -- sparse model, vector dim, vector names -- is fixed here since
# it defines the collection schema and must be identical on the retrieval side.
# ============================================================
DENSE_MODEL_NAME = os.getenv("EMBEDDING_MODEL_NAME")
DENSE_MODEL_DIM = 768
SPARSE_MODEL_NAME = "Qdrant/bm25"
DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"

print(f"[Init] Dense model: {DENSE_MODEL_NAME} (dim={DENSE_MODEL_DIM})")
print(f"[Init] Sparse model: {SPARSE_MODEL_NAME}")

# fastembed = ONNX runtime under the hood, no torch -- much lighter/faster to load
# and run than FlagEmbedding (BGE-M3) or sentence-transformers, with no accuracy
# hit for this use case (short policy chunks, not massive multilingual documents).
dense_model = TextEmbedding(model_name=DENSE_MODEL_NAME)
sparse_model = SparseTextEmbedding(model_name=SPARSE_MODEL_NAME)


# Pydantic Model
class PolicyChunkMetadata(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    source_file_name: str = Field(
        ...,
        description="The original filename (e.g., Academic Centres Policy.md)"
    )
    source_file_id: str = Field(
        ...,
        description="Deterministic UUID v5 representing the file generated from the filename"
    )
    header_1: Optional[str] = Field(
        None,
        alias="Header 1",
        description="Main section heading extracted from Markdown (#)"
    )
    header_2: Optional[str] = Field(
        None,
        alias="Header 2",
        description="Subsection heading extracted from Markdown (##)"
    )
    header_3: Optional[str] = Field(
        None,
        alias="Header 3",
        description="Sub-subsection heading extracted from Markdown (###)"
    )


def ingest():

    connection_url = os.getenv("QDRANT_URL")
    qdrant_api_key = os.getenv("QDRANT_API_KEY")

    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    MDS_DIR = os.path.join(SCRIPT_DIR, "policies_mds")
    POLICY_NAMESPACE = uuid.UUID("12345678-1234-5678-1234-567812345678")

    client = QdrantClient(
        url=connection_url,
        api_key=qdrant_api_key,
    )

    collection_name = "ucd_policies"

    if not os.path.exists(MDS_DIR) or not any(f.endswith('.md') for f in os.listdir(MDS_DIR)):
        print(f" No markdown files found in {MDS_DIR}. Staging queue is clear.")
        return

    if not client.collection_exists(collection_name=collection_name):
        print(f"Collection '{collection_name}' not found. Creating it now...")
        client.create_collection(
            collection_name=collection_name,
            vectors_config={
                DENSE_VECTOR_NAME: models.VectorParams(
                    size=DENSE_MODEL_DIM,
                    distance=models.Distance.COSINE
                )
            },
            sparse_vectors_config={
                SPARSE_VECTOR_NAME: models.SparseVectorParams(
                    modifier=models.Modifier.IDF
                )
            }
        )
        client.create_payload_index(
            collection_name=collection_name,
            field_name="source_file_id",
            field_schema=models.PayloadSchemaType.KEYWORD,
        )
    else:
        print(f"Collection '{collection_name}' already exists. Proceeding...")

    headers_to_split_on = [
        ("#", "Header 1"),
        ("##", "Header 2"),
        ("###", "Header 3"),
    ]

    markdown_splitter = MarkdownHeaderTextSplitter(headers_to_split_on)
    char_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)

    for md_file in os.listdir(MDS_DIR):
        if not md_file.endswith(".md"):
            continue

        source_file_id = str(uuid.uuid5(POLICY_NAMESPACE, md_file))

        file_path = os.path.join(MDS_DIR, md_file)
        with open(file_path, "r", encoding="utf-8") as f:
            md_content = f.read()

        md_header_splits = markdown_splitter.split_text(md_content)
        char_splits = char_splitter.split_documents(md_header_splits)
        print(len(char_splits), " + ", file_path)

        # Only delete old chunks after confirming we have new ones to replace them.
        # Prevents data loss if splitting produces zero chunks.
        if char_splits:
            client.delete(
                collection_name=collection_name,
                points_selector=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="source_file_id",
                            match=models.MatchValue(value=source_file_id)
                        )
                    ]
                )
            )
            embed_and_ingest(char_splits, client, source_file_id, md_file, collection_name)
        else:
            print(f"Warning: {md_file} produced zero chunks after splitting. Old index retained.")


def embed_and_ingest(char_splits, client, source_file_id, source_file_name, collection_name):
    if not char_splits:
        print("No valid chunks were generated for upload.")
        return

    texts = [chunk.page_content for chunk in char_splits]

    # Batch-encode all chunks in one pass -- far fewer model calls than per-chunk encoding.
    # passage_embed = doc-side asymmetric encoding (correct instruction prefix baked in for
    # models like BGE that were trained with different query vs. passage prefixes).
    # BM25 embed() = doc-side encode, uses real term-frequency + document-length normalization.
    dense_vectors = list(dense_model.passage_embed(texts))
    sparse_vectors = list(sparse_model.embed(texts))

    points_to_upload = []
    for index, chunk in enumerate(char_splits):

        chunk_id = str(uuid.uuid5(uuid.UUID(source_file_id), f"chunk_{index}"))

        try:
            validated_metaData = PolicyChunkMetadata(
                source_file_name=source_file_name,
                source_file_id=source_file_id,
                **chunk.metadata
            )
        except Exception as e:
            print("Something went wrong parsing chunk metadata: ", e)
            continue

        sparse_vec = sparse_vectors[index]

        point = models.PointStruct(
            id=chunk_id,
            payload={
                "text": texts[index],
                **validated_metaData.model_dump(by_alias=True, exclude_none=True)
            },
            vector={
                DENSE_VECTOR_NAME: dense_vectors[index].tolist(),
                SPARSE_VECTOR_NAME: models.SparseVector(
                    indices=sparse_vec.indices.tolist(),
                    values=sparse_vec.values.tolist()
                )
            }
        )
        points_to_upload.append(point)

    if points_to_upload:
        client.upsert(
            collection_name=collection_name,
            points=points_to_upload
        )
        print(f"Uploaded {len(points_to_upload)} chunks to '{collection_name}'.")
    else:
        print("No valid chunks were generated for upload.")


if __name__ == "__main__":
    ingest()
