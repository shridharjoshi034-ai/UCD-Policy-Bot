from typing import List, Optional
from pydantic import BaseModel, Field

class ChatRequest(BaseModel):
    question : str = Field(..., min_length = 1)

class Citation(BaseModel):
    title : str
    source_url : str
    section : Optional[str] = None
    snippet : Optional[str] = None
    open_url : Optional[str] = None



class ChatResponse(BaseModel):
    answer : str
    citations : List[Citation]
    verification_status : str
    latency_ms : int

class HealthResponse(BaseModel):
    status : str
    service : str
    message : Optional[str] = None

class SourceMetaData(BaseModel):
    source_id: str
    title: str
    source_url: str
    source_type: str
    pages_indexed: Optional[int] = None
    last_indexed_at: Optional[str] = None
