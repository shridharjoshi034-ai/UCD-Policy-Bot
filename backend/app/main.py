import contextlib
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api import chat, health, sources
from app.api.chat import pipeline


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """Warms up the embedding model and Ollama LLM at backend startup,
    so the first real user query doesn't pay the cold-load cost."""
    pipeline.warmup()
    yield
    pipeline.close()


app = FastAPI(title="UCD PolicyBot Backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(chat.router)
app.include_router(sources.router)