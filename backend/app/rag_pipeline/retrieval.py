import os
import json
import time
import requests
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from fastembed import TextEmbedding, SparseTextEmbedding
from qdrant_client import QdrantClient, models

# Load environment variables
load_dotenv()

# Enable ONNX Runtime graph-level optimizations for faster embedding inference.
# Level 2 (EXTENDED) applies operator fusion, constant folding, and memory
# planning — yielding 15-25% faster encode times with zero model changes.
os.environ.setdefault("ORT_GRAPH_OPTIMIZATION_LEVEL", "2")

# Simple in-memory LRU cache for query embeddings (max 128 entries).
# Same query text → same embedding, so repeated/similar questions skip
# the dense+sparse encode entirely.
_EMBEDDING_CACHE = {}
_EMBEDDING_CACHE_MAX = 128

# ============================================================
# Fixed schema config -- must exactly match ingestion.py.
# Only dense model name, Ollama model, and Ollama base URL come from .env.
# ============================================================
DENSE_MODEL_DIM = 768
SPARSE_MODEL_NAME = "Qdrant/bm25"
DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"

# Default timeout for Ollama requests (seconds)
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "120"))


class PolicyRAGPipeline:
    def __init__(self, collection_name: str = "ucd_policies"):
        # Qdrant Config
        self.connection_url = os.getenv("QDRANT_URL")
        self.api_key = os.getenv("QDRANT_API_KEY")
        self.collection_name = collection_name

        # Model Configs -- dense model, Ollama model, and Ollama base URL from .env
        self.dense_model_name = os.getenv("EMBEDDING_MODEL_NAME")
        self.ollama_model_name = os.getenv("OLLAMA_MODEL_NAME")
        self.ollama_base_url = os.getenv("OLLAMA_BASE_URL")

        print(f"[Init] Dense model: {self.dense_model_name}")
        print(f"[Init] Sparse model: {SPARSE_MODEL_NAME}")
        print(f"[Init] Generation model: {self.ollama_model_name}")

        # fastembed for both -- ONNX runtime, no torch, much lighter/faster to load
        # and run per-query than FlagEmbedding (BGE-M3) or sentence-transformers.
        self.dense_model = TextEmbedding(model_name=self.dense_model_name)
        self.sparse_model = SparseTextEmbedding(model_name=SPARSE_MODEL_NAME)

        # Thread pool reused across every query for parallel dense+sparse encoding
        self._executor = ThreadPoolExecutor(max_workers=2)

        # Initialize Qdrant Client
        self.client = QdrantClient(
            url=self.connection_url,
            api_key=self.api_key,
        )

        self.has_sparse_vectors = self._check_collection_schema()

    def _check_collection_schema(self) -> bool:
        """Fetches actual vector names from Qdrant collection -> warns loudly if config doesn't match
        what retrieve() expects. Returns True if the expected sparse vector exists, False otherwise
        (retrieve() then falls back to dense-only instead of crashing)."""
        try:
            info = self.client.get_collection(self.collection_name)
            dense_names = list(info.config.params.vectors.keys())
            sparse_names = list(info.config.params.sparse_vectors.keys()) if info.config.params.sparse_vectors else []

            print(f"[Schema Check] Collection '{self.collection_name}' dense vectors: {dense_names}")
            print(f"[Schema Check] Collection '{self.collection_name}' sparse vectors: {sparse_names}")

            if DENSE_VECTOR_NAME not in dense_names:
                print(f"[Schema Check] WARNING: expected dense vector {DENSE_VECTOR_NAME!r} not found in "
                      f"collection (found {dense_names}). Retrieval will likely fail -- re-ingest required.")

            if SPARSE_VECTOR_NAME not in sparse_names:
                print(f"[Schema Check] WARNING: sparse vector {SPARSE_VECTOR_NAME!r} not found -> "
                      f"falling back to DENSE-ONLY search. Recreate collection w/ sparse config "
                      f"(Modifier.IDF) + re-ingest to enable BM25 hybrid search.")
                return False

            return True

        except Exception as e:
            print(f"[Schema Check] Could not verify collection schema: {e}")
            return False

    def close(self):
        """Cleanly closes the underlying Qdrant client network connections."""
        if hasattr(self, 'client'):
            print("[Clean up] Closing Qdrant HTTP connection client...")
            self.client.close()
        if hasattr(self, '_executor'):
            self._executor.shutdown(wait=False)

    def warmup(self, keep_alive: str = "30m"):
        """Forces every lazy-loaded piece (dense model, sparse model, Ollama weights) into memory
        now, at app startup, instead of on the first real user request. Call this once from your
        FastAPI startup/lifespan hook -- NOT per-request."""
        print("[Warmup] Loading dense + sparse model weights into memory...")
        list(self.dense_model.query_embed(["warmup"]))
        list(self.sparse_model.query_embed(["warmup"]))
        print("[Warmup] Embedding models ready.")
        self._warmup_ollama(keep_alive=keep_alive)

    def _warmup_ollama(self, keep_alive: str = "30m"):
        """Pings Ollama once at startup -> forces model weights into memory before first real request."""
        url = f"{self.ollama_base_url}/api/generate"
        payload = {
            "model": self.ollama_model_name,
            "prompt": "",
            "keep_alive": keep_alive
        }
        try:
            print(f"[Warmup] Loading {self.ollama_model_name} into Ollama memory...")
            r = requests.post(url, json=payload, timeout=OLLAMA_TIMEOUT)
            r.raise_for_status()
            print("[Warmup] Ollama model ready.")
        except requests.exceptions.RequestException as e:
            print(f"[Warmup] Failed -> {self.ollama_model_name} not preloaded (will load on first real request): {e}")

    def _process_query_vectors(self, query_text: str) -> tuple:
        """Encodes query into dense + BM25 sparse vectors, using query-side (asymmetric) encoding
        that matches ingestion's passage-side encoding. Dense and sparse encode run in parallel
        threads -- independent CPU work, so this costs roughly max(dense_time, sparse_time)
        instead of dense_time + sparse_time on every single query.

        Results are cached by query text to skip re-encoding for repeated questions."""

        # Check cache first (same query = same embedding, deterministic)
        cache_key = query_text.strip().lower()
        if cache_key in _EMBEDDING_CACHE:
            return _EMBEDDING_CACHE[cache_key]

        def encode_dense():
            return list(self.dense_model.query_embed([query_text]))[0].tolist()

        def encode_sparse():
            vec = list(self.sparse_model.query_embed([query_text]))[0]
            return vec.indices.tolist(), vec.values.tolist()

        dense_future = self._executor.submit(encode_dense)
        sparse_future = self._executor.submit(encode_sparse)

        dense_vec = dense_future.result()
        sparse_idx, sparse_vals = sparse_future.result()

        result = (dense_vec, sparse_idx, sparse_vals)

        # Store in cache (simple FIFO eviction via dict insertion order)
        if len(_EMBEDDING_CACHE) >= _EMBEDDING_CACHE_MAX:
            _EMBEDDING_CACHE.pop(next(iter(_EMBEDDING_CACHE)))
        _EMBEDDING_CACHE[cache_key] = result

        return result

    def retrieve(
            self,
            query_text: str,
            limit: int = 5,
            file_id_filter: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Executes Dense + BM25 Sparse hybrid retrieval via RRF when the collection has sparse
        vectors, else falls back to dense-only."""
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

        if self.has_sparse_vectors:
            dense_vec, sparse_idx, sparse_vals = self._process_query_vectors(query_text)
            response = self.client.query_points(
                collection_name=self.collection_name,
                prefetch=[
                    models.Prefetch(
                        query=dense_vec,
                        using=DENSE_VECTOR_NAME,
                        limit=limit * 2,
                        filter=query_filter
                    ),
                    models.Prefetch(
                        query=models.SparseVector(
                            indices=sparse_idx,
                            values=sparse_vals
                        ),
                        using=SPARSE_VECTOR_NAME,
                        limit=limit * 2,
                        filter=query_filter
                    )
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=limit
            )
        else:
            # Dense-only fallback — collection has no sparse field, skip BM25 encode + fusion entirely.
            # Still check the embedding cache for dense-only lookups.
            cache_key = query_text.strip().lower()
            if cache_key in _EMBEDDING_CACHE:
                dense_vec = _EMBEDDING_CACHE[cache_key][0]  # (dense, sparse_idx, sparse_vals)
            else:
                dense_vec = list(self.dense_model.query_embed([query_text]))[0].tolist()
                # Cache the dense vector even without sparse (store None placeholders)
                if len(_EMBEDDING_CACHE) >= _EMBEDDING_CACHE_MAX:
                    _EMBEDDING_CACHE.pop(next(iter(_EMBEDDING_CACHE)))
                _EMBEDDING_CACHE[cache_key] = (dense_vec, None, None)
            response = self.client.query_points(
                collection_name=self.collection_name,
                query=dense_vec,
                using=DENSE_VECTOR_NAME,
                filter=query_filter,
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
        """Constructs context template and handles generation stream via Ollama.
        Yields raw token strings."""

        context_blocks = []
        for chunk in retrieved_chunks:
            header_context = f"File: {chunk['source_file_name']}"
            if chunk['h1']: header_context += f" > {chunk['h1']}"
            if chunk['h2']: header_context += f" >> {chunk['h2']}"

            context_blocks.append(f"[{header_context}]\nContent: {chunk['text']}\n")

        context_str = "\n---\n".join(context_blocks)

        system_prompt = (
            "You answer queries strictly from the provided policy documents.\n"
            "If the answer is not in the context, say so clearly.\n"
            "Format responses in professional Markdown: use headings, bullet lists,"
            " **bold** for key terms, and tables where helpful.\n"
            "Never output unstructured plain text."
        )

        user_prompt = f"Context:\n{context_str}\n\nQuery: {query_text}\n\nAnswer:"

        url = f"{self.ollama_base_url}/api/chat"
        payload = {
            "model": self.ollama_model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "stream": True,
            # Keeps the model resident in Ollama's memory between requests so it
            # doesn't get unloaded and reloaded from disk on every single query.
            "keep_alive": "30m"
        }

        print(f"\n--- Generating Answer via {self.ollama_model_name} ---")
        try:
            response = requests.post(url, json=payload, stream=True, timeout=OLLAMA_TIMEOUT)
            if not response.ok:
                print("Ollama error:", response.text)
            response.raise_for_status()

            for line in response.iter_lines():
                if line:
                    chunk_json = line.decode('utf-8')
                    data = json.loads(chunk_json)
                    content = data.get("message", {}).get("content", "")
                    if content:
                        yield content
            print("\n")

        except requests.exceptions.RequestException as e:
            print(f"\nFailed to connect or communicate with Ollama instance: {e}")

    def stream_answer(self, query_text: str):
        """Streams answer from Ollama to the frontend.
        Measures generation latency (wall-clock from first token request to stream end)
        per-request, safely even under concurrent load."""

        greeting_response = self.check_greeting(query_text)
        if greeting_response:
            yield {"type": "token", "text": greeting_response}
            yield {"type": "final", "citations": [], "latency_seconds": 0.0}
            return

        chunks = self.retrieve(query_text=query_text, limit=3)
        yield {"type": "chunks_found", "count": len(chunks)}

        # Stream individual chunk metadata as they're retrieved so the frontend
        # can show real-time source discovery before the LLM response begins.
        seen_files = set()
        for chunk in chunks:
            file_name = chunk.get("source_file_name", "")
            if file_name and file_name not in seen_files:
                seen_files.add(file_name)
                yield {
                    "type": "chunk_found",
                    "title": file_name,
                    "section": chunk.get("h1") or chunk.get("h2") or "",
                }

        # Per-request latency measurement: starts when we begin consuming tokens,
        # stops when the generator is exhausted. No instance variables = thread-safe.
        start_time = time.time()
        token_count = 0
        first_token_time = None

        try:
            for token in self.generate_answer(query_text=query_text, retrieved_chunks=chunks):
                if token_count == 0:
                    first_token_time = time.time()
                    print(f"[Latency] Time to first token: {first_token_time - start_time:.3f}s")
                token_count += 1
                yield {"type": "token", "text": token}
        finally:
            generation_latency = time.time() - start_time
            ttft = (first_token_time - start_time) if first_token_time else 0.0
            print(f"[Latency] TTFT: {ttft:.3f}s  Total generation ({token_count} tokens): {generation_latency:.3f}s")

        citations = []
        for file_name in seen_files:
            citations.append({
                "title": file_name,
                "source_url": "#"
            })

        final_event = {
            "type": "final",
            "citations": citations,
            "latency_seconds": round(generation_latency, 3),
        }
        # Only include TTFT if at least one token was actually generated
        if first_token_time is not None:
            final_event["ttft_seconds"] = round(ttft, 3)
        yield final_event

    def check_greeting(self, query_text: str) -> Optional[str]:
        """Checks if the query is a standard greeting and returns a pre-written response if so."""
        cleaned = query_text.strip().lower()
        # Simple, robust greeting detection without regex complexity
        greetings = {
            "hi", "hello", "hey", "greetings",
            "how are you", "how are you doing",
            "good morning", "good afternoon", "good evening",
            "hi there", "hello there", "hey there",
        }
        # Check if the cleaned query is exactly a greeting or starts with one and has no substantive content
        for greeting in greetings:
            if cleaned == greeting or cleaned.startswith(greeting + " ") or cleaned.startswith(greeting + ","):
                # Ensure there's no substantive question after the greeting
                remainder = cleaned[len(greeting):].strip(",.!? ")
                if not remainder or remainder in {"there", "doing", "today", "now"}:
                    return (
                        "Hello! 👋\n\n"
                        "I am the UCD Policy Assistant. I can help answer your questions regarding UCD policies. "
                        "How can I help you today?"
                    )
        return None


if __name__ == "__main__":
    pipeline = PolicyRAGPipeline()
    pipeline.warmup()

    try:
        query = "What is the policy on academic misconduct?"
        print(f"\n--- Test Query: {query!r} ---\n")

        for event in pipeline.stream_answer(query):
            if event["type"] == "chunks_found":
                print(f"[chunks_found] {event['count']} chunks retrieved")
            elif event["type"] == "token":
                print(event["text"], end="", flush=True)
            elif event["type"] == "final":
                print("\n\n--- Citations ---")
                for c in event["citations"]:
                    print(f"- {c['title']}")
                print(f"\n--- Latency: {event['latency_seconds']:.3f}s ---")

    finally:
        pipeline.close()
