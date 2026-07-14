"""FastAPI entry point.

Diem khoi dong ung dung FastAPI.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.agent.loop import AdvisorAgent
from app.agent.tools import ToolExecutor
from app.api.routes import router
from app.config import load_settings
from app.db import close_pool
from app.llm.gemini import GeminiClient
from app.memory.conversation import ConversationMemory
from app.rag.retriever import Retriever

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    client = GeminiClient(settings)

    # Chunk embeddings are loaded once at startup, so the first student request does not pay
    # for reading the whole knowledge base out of the database.
    # Embedding cua cac doan tai lieu duoc nap mot lan luc khoi dong, de request dau tien cua
    # sinh vien khong phai ganh chi phi doc toan bo kho tri thuc tu database.
    retriever = Retriever(client)
    chunks_loaded = retriever.load()
    logger.info("Da nap %d doan tai lieu vao bo nho.", chunks_loaded)

    executor = ToolExecutor(
        retriever,
        retrieval_top_k=settings.retrieval_top_k,
        semester=settings.current_semester,
    )
    app.state.agent = AdvisorAgent(client, executor, ConversationMemory(), settings)
    app.state.chunks_loaded = chunks_loaded

    yield

    close_pool()


app = FastAPI(
    title="Academic Advisor Agent",
    description="Tro ly co van hoc tap dung RAG va tool-use agent tren Gemini.",
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(router)
