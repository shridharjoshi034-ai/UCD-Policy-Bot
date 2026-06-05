from fastapi import FastAPI
from pydantic import BaseModel
from app.api import chat, health, sources

# from services.rag_services import answer_question

app = FastAPI()

# class ChatRequest(BaseModel):  
#     question : str;


app = FastAPI(title="UCD PolicyBot Backend")

app.include_router(health.router)
app.include_router(chat.router)
app.include_router(sources.router)