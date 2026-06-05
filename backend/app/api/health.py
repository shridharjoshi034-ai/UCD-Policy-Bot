
import os

from fastapi import APIRouter
from qdrant_client import QdrantClient 
from app.schemas import HealthResponse

router = APIRouter(prefix = "/health", tags = ["Health"])

@router.get("", response_model = HealthResponse)
async def health():
    return {"status" : "ok", "service" : "ucd-policybot-backend"}


@router.get("/qdrant",response_model = HealthResponse)
async def health_qdrant():
    qdrant_url = os.getenv("QDRANT_URL")
    qdrant_api_key = os.getenv("QDRANT_API_KEY")

    if not qdrant_url:
        return {"status" : "not configured", "service" : "qdrant", "message" : "Qdrant_url is not set"}
    
    try : 
            
        client = QdrantClient(url = qdrant_url, api_key = qdrant_api_key)
        client.get_collections()

        return {"status" : "ok", "service" : "qdrant"}
    except Exception as e : 
        return {"status" : "unavailable", "service" : "qdrant", "message" : str(e)}
    

@router.get("/rag", response_model = HealthResponse)
async def health_rag():
    try :  
        return {"status" : "ok", "service" : "rag-service"}
    except Exception as e : 
        return {"status" : "unavailable", "service" : "rag-service", "message" : str(e)}