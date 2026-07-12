# Backend for the UCD PolicyBot

This is a FastAPI backend that handles retrieval and generation for the RAG pipeline.

## Requirements
The backend relies on the following key technologies:
- **FastAPI**: For the API layer.
- **Qdrant**: For vector storage and search.
- **Ollama**: For local LLM text generation.
- **BGE-M3**: For dense and sparse embeddings.

## Setup and Running Locally

1. **Virtual Environment**: Ensure you are in the `backend` directory and activate the virtual environment:
   ```bash
   source .venv/bin/activate
   ```

2. **Dependencies**: Install the required Python packages:
   ```bash
   pip install -r requirements.txt
   ```

3. **Environment Variables**: Ensure you have a `.env` file configured in the `backend` directory. This needs to include configurations for Qdrant and Ollama (e.g., `QDRANT_URL`, `QDRANT_API_KEY`, `OLLAMA_MODEL_NAME`).

4. **Run the Server**: Start the FastAPI server using `uvicorn`:
   ```bash
   uvicorn app.main:app --reload
   ```

The server will start locally, and you can test the endpoints (like the live chat endpoint which handles Server-Sent Events).
