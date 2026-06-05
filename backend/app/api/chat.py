import time
from urllib import request
import json
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from app.schemas import HealthResponse
from app.schemas import ChatRequest, ChatResponse
from app.services.rag_services import answer_question

router = APIRouter(prefix = "/chat", tags = ["chat"])

@router.post("/query", response_model = ChatResponse)
async def chat_query(request : ChatRequest):
    return answer_question(request.question);    

@router.post("/stream")
async def chat_stream(request : ChatRequest):
    def event_stream():
        events = [
            ("retrieval_started", {
                "message": "Searching indexed UCD policy documents"
            }),
            ("chunks_found", {
                "count": 0
            }),
            ("token", {
                "text": "Mock "
            }),
            ("token", {
                "text": "streaming "
            }),
            ("token", {
                "text": "answer "
            }),
            ("token", {
                "text": f"for: {request.question}"
            }),
            ("final", {
                "answer": f"Mock streaming answer for: {request.question}",
                "citations": [],
                "verification_status": "not_run",
                "latency_ms": 1000
            }),
        ]
        for event_name, payload in events:
            yield f"event: {event_name}\n"
            yield f"data: {json.dumps(payload)}\n\n"
            time.sleep(0.2)

    return StreamingResponse(event_stream(), media_type="text/event-stream")