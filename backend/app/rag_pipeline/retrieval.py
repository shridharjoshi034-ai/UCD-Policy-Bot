import os
import re
import hashlib
from urllib import response
import requests
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
from FlagEmbedding import BGEM3FlagModel
from qdrant_client import QdrantClient, models

# Load environment variables
load_dotenv()


class PolicyRAGPipeline:
    def __init__(self, collection_name: str = "ucd_policies"):
        # Qdrant Config
        self.connection_url = os.getenv("QDRANT_URL")
        self.api_key = os.getenv("QDRANT_API_KEY")
        self.collection_name = collection_name

        # Model Configs from .env
        self.embedding_model_name = os.getenv("EMBEDDING_MODEL_NAME", "BAAI/bge-m3")
        self.ollama_model_name = os.getenv("OLLAMA_MODEL_NAME", "gemma4:e2b")
        self.ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

        print(f"[Init] Embedding Model: {self.embedding_model_name}")
        print(f"[Init] Generation Model: {self.ollama_model_name}")

        # Initialize BGE-M3
        self.model = BGEM3FlagModel(self.embedding_model_name, use_fp16=True)

        # Initialize Qdrant Client
        self.client = QdrantClient(
            url=self.connection_url,
            api_key=self.api_key,
        )

    def close(self):
        """Cleanly closes the underlying Qdrant client network connections."""
        if hasattr(self, 'client'):
            print("[Clean up] Closing Qdrant HTTP connection client...")
            self.client.close()

    def _process_query_vectors(self, query_text: str) -> tuple:
        """Transforms raw query text into synchronized dense and sparse vector spaces."""
        embeddings = self.model.encode(query_text, return_dense=True, return_sparse=True)

        dense_vector = embeddings['dense_vecs']
        lexical_weights = embeddings['lexical_weights']

        sparse_indices = []
        sparse_values = []

        # Exact deterministic hashing strategy from ingestion logic
        for word, weight in lexical_weights.items():
            hash_object = hashlib.sha256(word.encode('utf-8'))
            hash_int = int(hash_object.hexdigest(), 16)
            consistent_index = hash_int % (10 ** 9)
            sparse_indices.append(consistent_index)
            sparse_values.append(weight)

        return dense_vector, sparse_indices, sparse_values

    def retrieve(
            self,
            query_text: str,
            limit: int = 5,
            file_id_filter: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Executes concurrent Dense + Sparse hybrid retrieval utilizing native RRF inside Qdrant."""
        dense_vec, sparse_idx, sparse_vals = self._process_query_vectors(query_text)

        query_filter = None
        if file_id_filter:
            query_filter = models.Filter(
                must=[
                    models.FieldCondition(
                        key="source_file_id",
                        match=models.MatchValue(value=file_id_filter)
                    )
                ]
            )

        response = self.client.query_points(
            collection_name=self.collection_name,
            prefetch=[
                models.Prefetch(
                    query=dense_vec,
                    using="",
                    limit=limit * 3,
                    filter=query_filter
                ),
                models.Prefetch(
                    query=models.SparseVector(
                        indices=sparse_idx,
                        values=sparse_vals
                    ),
                    using="sparse",
                    limit=limit * 3,
                    filter=query_filter
                )
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=limit
        )

        results = []
        for point in response.points:
            results.append({
                "text": point.payload.get("text"),
                "source_file_name": point.payload.get("source_file_name"),
                "h1": point.payload.get("Header 1"),
                "h2": point.payload.get("Header 2"),
            })

        return results

    def generate_answer(self, query_text: str, retrieved_chunks: List[Dict[str, Any]]):
        """Constructs context template and handles generation stream via Ollama."""

        # Compile retrieved chunks into structural context block
        context_blocks = []
        for chunk in retrieved_chunks:
            header_context = f"File: {chunk['source_file_name']}"
            if chunk['h1']: header_context += f" > {chunk['h1']}"
            if chunk['h2']: header_context += f" >> {chunk['h2']}"

            context_blocks.append(f"[{header_context}]\nContent: {chunk['text']}\n")

        context_str = "\n---\n".join(context_blocks)

        # Structure strict context system constraint prompt
        system_prompt = (
            "You are an advanced assistant answering queries based strictly on the provided policy documents.\n"
            "If the answer cannot be confidently derived from the context, state that explicitly.\n"
            "Your response MUST be entirely formatted in strict, professional Markdown. Follow these rules:\n"
            "1. Use `#` for the main title and `##` or `###` for any sub-sections.\n"
            "2. Use bullet points (`-`) or numbered lists for any sequence of facts, steps, or rules.\n"
            "3. Emphasize important terms using **bold** text.\n"
            "4. If presenting comparative data or multiple attributes, use Markdown tables.\n"
            "5. Never output a wall of plain text; always structure your paragraphs clearly."
        )

        user_prompt = f"Context:\n{context_str}\n\nQuery: {query_text}\n\nAnswer:"

        # API payloads for local Ollama endpoint
        url = f"{self.ollama_base_url}/api/chat"
        payload = {
            "model": self.ollama_model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "stream": True
        }

        print(f"\n--- Generating Answer via {self.ollama_model_name} ---")
        try:
            response = requests.post(url, json=payload, stream=True)
            if not response.ok:
                print("Ollama error:", response.text)
            response.raise_for_status()

            for line in response.iter_lines():
                if line:
                    chunk_json = line.decode('utf-8')
                    # Parse continuous JSON streaming frames out safely
                    import json
                    data = json.loads(chunk_json)
                    content = data.get("message", {}).get("content", "")
                    # print(content, end="", flush=True)
                    if content:
                        yield content
            print("\n")

        except requests.exceptions.RequestException as e:
            print(f"\nFailed to connect or communicate with Ollama instance: {e}")
        

    def stream_answer(self, query_text : str):
        """Streams answer from Ollama to the frontend"""

        chunks = self.retrieve(query_text=query_text, limit=3)

        # Frontend can show count of chunks found for user feedback
        yield{"type" : "chunks_found", "count" : len(chunks)}

        for token in self.generate_answer(query_text=query_text, retrieved_chunks=chunks):
            yield {"type": "token", "text": token}
        
       

    def check_greeting(self, query_text: str) -> Optional[str]:
        """Checks if the query is a standard greeting and returns a pre-written response if so."""
        pattern = r'^\s*(hi|hello|hey|greetings|how are you|good morning|good afternoon|good evening)(?:\s+there|(?:\s*,\s*)?how are you(?:\s+doing)?)?[\s?.!]*$'
        if re.match(pattern, query_text, re.IGNORECASE):
            return (
                "# Hello! 👋\n\n"
                "I am the UCD Policy Assistant. I can help answer your questions regarding UCD policies. "
                "How can I help you today?"
            )
        return None

if __name__ == "__main__":
    # Instantiate the unified system pipeline
    pipeline = PolicyRAGPipeline()

    try:
        query = "Hello, how are you?"

        # 1. Check for basic greeting first
        greeting_response = pipeline.check_greeting(query)
        
        if greeting_response:
            print(f"\n--- Generating Pre-written Greeting Response ---")
            print(greeting_response)
            print("\n")
        else:
            # 2. Execute Retrieval
            chunks = pipeline.retrieve(query_text=query, limit=10)
    
            # 3. Execute Local Text Generation
            pipeline.generate_answer(query_text=query, retrieved_chunks=chunks)

    finally:
        # 3. Always close connections cleanly before exit
        pipeline.close()