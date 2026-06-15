import os
import uuid
from dotenv import load_dotenv
from typing import Optional
from pydantic import BaseModel, Field
import hashlib



# Load the environment variables from the .env file
load_dotenv()

from FlagEmbedding import BGEM3FlagModel
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from qdrant_client import QdrantClient, models


model = BGEM3FlagModel('BAAI/bge-m3', use_fp16=True)

# Pydantic Model
class PolicyChunkMetadata(BaseModel):
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

    class Config:
        # Allows populating the model using either the python attribute name (header_1)
        # or the original spacing string alias ("Header 1")
        populate_by_name = True


def ingest():

    connection_url = os.getenv("QDRANT_URL")
    Qdrant_api_key = os.getenv("QDRANT_API_KEY")

    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    MDS_DIR = os.path.join(SCRIPT_DIR, "policies_mds")
    POLICY_NAMESPACE = uuid.UUID("12345678-1234-5678-1234-567812345678")

    client = QdrantClient(
        url=connection_url,
        api_key=Qdrant_api_key,
    )

    collection_name = "ucd_policies"

    if not os.path.exists(MDS_DIR) or not any(f.endswith('.md') for f in os.listdir(MDS_DIR)):
        print(f" No markdown files found in {MDS_DIR}. Staging queue is clear.")
        return

    if not client.collection_exists(collection_name=collection_name):
        print(f"Collection '{collection_name}' not found. Creating it now...")
        client.create_collection(
            collection_name=collection_name,
            vectors_config=models.VectorParams(
                size=1024,
                distance=models.Distance.COSINE
            ),
            sparse_vectors_config={
                "sparse": models.SparseVectorParams()
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

        source_file_id = str(uuid.uuid5(POLICY_NAMESPACE, md_file))
        client.delete(
            collection_name="ucd_policies",  # Replace with your dynamic variable if needed
            points_selector=models.Filter(
                must=[
                    models.FieldCondition(
                        key="source_file_id",
                        match=models.MatchValue(value=source_file_id)
                    )
                ]
            )
        )


        file_path = os.path.join(MDS_DIR, md_file)
        if md_file.endswith(".md"):
            with open(file_path) as f:
                md_content = f.read()

            md_header_splits = markdown_splitter.split_text(md_content)
            char_splits = char_splitter.split_documents(md_header_splits)
            print(len(char_splits)," + " , file_path)
            embed_and_ingest(char_splits, model, client, source_file_id, md_file)






def embed_and_ingest(char_splits, model, client,source_file_id, source_file_name):
    points_to_upload = []

    for index, chunk in enumerate(char_splits):

        chunk_id = str(uuid.uuid5(uuid.UUID(source_file_id),f"chunk_{index}"))

        text_content = chunk.page_content

        try:
            validated_metaData = PolicyChunkMetadata(
                source_file_name=source_file_name,
                source_file_id=source_file_id,
                **chunk.metadata
            )
        except Exception as e:
            print("Something went wrong parsing chunk metadata: ", e)
            continue

        embeddings = model.encode(text_content, return_dense = True, return_sparse = True)
        dense_vector = embeddings['dense_vecs']
        lexical_weights = embeddings['lexical_weights']

        sparse_indices = []
        sparse_values = []
        for word, weight in lexical_weights.items():
            # 1. Encode the string to bytes and generate a SHA-256 hash
            hash_object = hashlib.sha256(word.encode('utf-8'))
            # 2. Convert the hexadecimal hash back into a standard base-16 integer
            hash_int = int(hash_object.hexdigest(), 16)
            consistent_index = hash_int % (10 ** 9)
            sparse_indices.append(consistent_index)
            sparse_values.append(weight)

        point = models.PointStruct(
            id=chunk_id,
            payload={
                "text": text_content,
                **validated_metaData.model_dump(by_alias=True, exclude_none=True)
            },
            vector={
                "": dense_vector,
                "sparse": models.SparseVector(
                    indices=sparse_indices,
                    values=sparse_values
                )
            }
        )
        points_to_upload.append(point)



    if points_to_upload:
        client.upsert(
            collection_name="ucd_policies",
            points=points_to_upload
        )
    else:
        print("No valid chunks were generated for upload.")

if __name__ == "__main__":
    ingest()