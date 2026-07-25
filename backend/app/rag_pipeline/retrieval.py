import os
import re
import hashlib
import json
import requests
from typing import List, Dict, Any, Optional, Generator
from dotenv import load_dotenv
# Load environment variables
load_dotenv()

from app.observability.tracer import get_tracer
from app.services import storage_service
from FlagEmbedding import BGEM3FlagModel
from qdrant_client import QdrantClient, models




class PolicyRAGPipeline:
    def __init__(self, collection_name: str = "ucd_policies"):
        # Qdrant Config
        self.connection_url = os.getenv("QDRANT_URL")
        self.api_key = os.getenv("QDRANT_API_KEY")
        self.collection_name = collection_name

        # Model Configs from .env
        self.embedding_model_name = os.getenv("EMBEDDING_MODEL_NAME", "BAAI/bge-m3")
        self.ollama_model_name = os.getenv("OLLAMA_MODEL_NAME", "qwen2.5:1.5b")
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

        #Initialize langfuse client
        self.langfuse = get_tracer()

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
            file_id_filter: Optional[str] = None,
            trace = None
    ) -> List[Dict[str, Any]]:
        span = trace.span(name="retrieve-chunks", input={"query":query_text,"limit":limit}) if trace else None
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

        if span:
            span.update(output={
                "chunk_count": len(results),
                "sources": [r["source_file_name"] for r in results if r["source_file_name"]],
                "contexts": [r["text"] for r in results]
            })
            span.end()

        return results

    def generate_answer(self, query_text: str, retrieved_chunks: List[Dict[str, Any]], trace=None) -> Generator[str, None, None]:
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
            "stream": True,
            "options": {
                "temperature": 0.1
            }
        }

        generation = trace.generation(name="ollama-generate", model=self.ollama_model_name, input=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]) if trace else None
        
        full_text = []
        usage = {}
        print(f"\n--- Generating Answer via {self.ollama_model_name} ---")
        try:
            response = requests.post(url, json=payload, stream=True)
            if not response.ok:
                print("Ollama error:", response.text)
            response.raise_for_status()

            for line in response.iter_lines():
                if line:
                    chunk_json = line.decode('utf-8')
                    data = json.loads(chunk_json)
                    content = data.get("message", {}).get("content", "")
                    if content:
                        full_text.append(content)
                        yield content
                    if data.get("done"):
                        usage.update({
                            "input": data.get("prompt_eval_count", 0),
                            "output": data.get("eval_count", 0),
                        })
            print("\n")

        except requests.exceptions.RequestException as e:
            print(f"\nFailed to connect or communicate with Ollama instance: {e}")
            if generation:
                generation.update(level="ERROR", status_message=str(e))
        finally:
            if generation:
                generation.end(output="".join(full_text), usage=usage)

    def stream_answer(self, query_text : str):
        """Streams answer from Ollama to the frontend"""
        try:
            trace = self.langfuse.trace(name="chat_response", input={"question":query_text})
        except Exception:
            trace = None
        
        greeting_response = self.check_greeting(query_text)
        if greeting_response:
            if trace:
                trace.update(output={"answer": greeting_response})
            yield {"type": "token", "text": greeting_response}
            yield {"type": "final", "citations": []}
            return

        chunks = self.retrieve(query_text=query_text, limit=5, trace=trace)

        # Frontend can show count of chunks found for user feedback
        yield{"type" : "chunks_found", "count" : len(chunks)}

        answer_parts = []

        for token in self.generate_answer(query_text=query_text, retrieved_chunks=chunks, trace=trace):
            answer_parts.append(token)
            yield {"type": "token", "text": token}

        # Format citations from unique source files with Supabase signed URLs
        # Prefer PDF versions over markdown — users want to see the original document
        citations = []
        seen_files = set()
        for chunk in chunks:
            file_name = chunk.get("source_file_name")
            if file_name and file_name not in seen_files:
                seen_files.add(file_name)
                # Reconstruct the Supabase storage path from the filename
                ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
                if ext == "pdf":
                    storage_path = f"pdfs/{file_name}"
                elif ext == "md":
                    # Try to find the corresponding PDF version first
                    base_name = file_name.rsplit(".", 1)[0]
                    pdf_path = f"pdfs/{base_name}.pdf"
                    if storage_service.file_exists(pdf_path):
                        storage_path = pdf_path
                        file_name = f"{base_name}.pdf"  # Show PDF name in citation
                    else:
                        storage_path = f"markdown/{file_name}"
                else:
                    storage_path = file_name
                # Use signed URL (works with private buckets) with 1-hour expiry
                try:
                    open_url = storage_service.create_signed_url(storage_path, expires_in=3600)
                except Exception:
                    open_url = storage_service.get_public_url(storage_path)
                citations.append({
                    "title": file_name,
                    "source_url": open_url,
                    "open_url": open_url,
                })
        if trace:
            trace.update(output={
                "answer": "".join(answer_parts),
                "citations": citations,
                "contexts": [chunk["text"] for chunk in chunks]
            })
        yield {"type": "final", "citations": citations}

    def check_greeting(self, query_text: str) -> Optional[str]:
        """Checks if the query is a standard greeting and returns a pre-written response if so."""
        pattern = r'^\s*(hi|hello|hey|greetings|how are you|good morning|good afternoon|good evening)(?:\s+there|(?:\s*,\s*)?how are you(?:\s+doing)?)?[\s?.!]*$'
        if re.match(pattern, query_text, re.IGNORECASE):
            return (
                "Hello! 👋\n\n"
                "I am the UCD Policy Assistant. I can help answer your questions regarding UCD policies. "
                "How can I help you today?"
            )
        return None

async def main():
    """Async entry point for testing the pipeline locally."""
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

            # 3. Execute Local Text Generation (now async)
            for token in pipeline.generate_answer(query_text=query, retrieved_chunks=chunks):
                print(token, end="", flush=True)

    finally:
        # Always close connections cleanly before exit
        pipeline.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())