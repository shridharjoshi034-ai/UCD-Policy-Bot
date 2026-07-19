
import os

from dotenv import load_dotenv
from fastapi import APIRouter
import requests
from qdrant_client import QdrantClient 
from app.schemas import HealthResponse

load_dotenv()

router = APIRouter(prefix = "/health", tags = ["Health"])

# Health check for the backend service
@router.get("", response_model = HealthResponse)
async def health():
    return {"status" : "ok", "service" : "ucd-policybot-backend"}


# Health check for Qdrant service
@router.get("/qdrant",response_model = HealthResponse)
async def health_qdrant():
    qdrant_url = os.getenv("QDRANT_URL")
    qdrant_api_key = os.getenv("QDRANT_API_KEY")

    if not qdrant_url:
        return {"status" : "not configured", "service" : "qdrant", "message" : "Qdrant_url is not set"}
    
    try:
        client = QdrantClient(url=qdrant_url, api_key=qdrant_api_key)
        client.get_collections()

        return {"status" : "ok", "service" : "qdrant"}
    except Exception as e : 
        return {"status" : "unavailable", "service" : "qdrant", "message" : str(e)}


# Health check for Ollama LLM service
@router.get("/llm", response_model = HealthResponse)
async def health_llm():
    ollama_base_url = os.getenv("OLLAMA_BASE_URL")
    ollama_model_name = os.getenv("OLLAMA_MODEL_NAME")

    if not ollama_base_url or not ollama_model_name:
        return {"status" : "not configured", "service" : "ollama", "message" : "Ollama configuration is incomplete"}
    
    try:

        # Check if Ollama service is reachable
        response = requests.get(
            f"{ollama_base_url}/api/tags",
            timeout=5
        )
        response.raise_for_status()         # Raise an error for bad responses

        # Check if the specified model is in the list of installed models
        installed_models = {
            model["name"]
            for model in response.json().get("models", [])
        }

        if ollama_model_name not in installed_models:
            return {
                "status": "unavailable",
                "service": "ollama",
                "message": f"{ollama_model_name} is not installed",
            }

        return {
            "status": "ok",
            "service": "ollama",
            "message": f"{ollama_model_name} is available",
        }

    except requests.RequestException as exc:
        return {
            "status": "unavailable",
            "service": "ollama",
            "message": str(exc),
        }
    
# Health check for RAG service (sync def so FastAPI runs it in a thread pool,
# avoiding event-loop blocking from the synchronous Qdrant client).
@router.get("/rag", response_model=HealthResponse)
def health_rag():
    client = None

    try:
        client = QdrantClient(url=os.getenv("QDRANT_URL"), api_key=os.getenv("QDRANT_API_KEY"))
        collection = client.get_collection("ucd_policies")

        # Check the count of indexed chunks in the collection
        if collection.points_count == 0:
            return {
                "status": "degraded",
                "service": "rag-service",
                "message": "Qdrant collection contains no indexed chunks",
            }

        return {
            "status": "ok",
            "service": "rag-service",
            "message": f"{collection.points_count} chunks indexed",
        }

    except Exception as e:
        return {
            "status": "unavailable",
            "service": "rag-service",
            "message": str(e),
        }
    finally:
        if client is not None:
            client.close()