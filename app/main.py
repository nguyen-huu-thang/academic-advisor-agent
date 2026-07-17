"""FastAPI entry point.

Điểm khởi động ứng dụng FastAPI.
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
    # Everything before `yield` runs once at startup; everything after runs once at shutdown.
    # Mọi thứ trước `yield` chạy một lần lúc khởi động; mọi thứ sau chạy một lần lúc tắt.
    settings = load_settings()
    client = GeminiClient(settings)

    # Chunk embeddings are loaded once at startup, so the first student request does not pay
    # for reading the whole knowledge base out of the database.
    # Embedding của các đoạn tài liệu được nạp một lần lúc khởi động, để request đầu tiên của
    # sinh viên không phải gánh chi phí đọc toàn bộ kho tri thức từ database.
    retriever = Retriever(client)
    chunks_loaded = retriever.load()
    logger.info("Da nap %d doan tai lieu vao bo nho.", chunks_loaded)

    executor = ToolExecutor(retriever, settings)
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
