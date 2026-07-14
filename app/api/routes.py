"""HTTP API of the academic advisor assistant.

API HTTP cua tro ly co van hoc tap.
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.agent.loop import AdvisorAgent, StudentNotFound
from app.llm.gemini import UpstreamUnavailable
from app.observability.metrics import metrics

logger = logging.getLogger(__name__)
router = APIRouter()


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    # Who the assistant is serving. In a deployed system this would be read from the
    # authenticated session, not from the request body; the service assumes an authentication
    # layer has already established it and does not treat it as user input.
    # Sinh vien ma tro ly dang phuc vu. Trong he thong that, gia tri nay se duoc lay tu phien
    # da xac thuc chu khong lay tu body cua request; dich vu nay gia dinh da co mot tang xac
    # thuc xac lap no tu truoc, va khong coi no la dau vao cua nguoi dung.
    student_id: str = Field(min_length=1, max_length=32)
    message: str = Field(min_length=1, max_length=2000)


class ToolCallView(BaseModel):
    name: str
    allowed: bool
    note: str | None = None


class ChatResponse(BaseModel):
    answer: str
    tool_calls: list[ToolCallView]
    iterations: int
    latency_ms: float
    input_tokens: int
    output_tokens: int
    cost_usd: float


@router.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    agent: AdvisorAgent = request.app.state.agent

    try:
        result = agent.run(payload.session_id, payload.student_id, payload.message)
    except StudentNotFound as error:
        raise HTTPException(status_code=404, detail=str(error))
    except UpstreamUnavailable as error:
        # Being rate limited by the model provider is not a bug in this service, so it is
        # reported as 429 with a Retry-After rather than as an internal error.
        # Bi nha cung cap model chan vi qua han muc khong phai loi cua dich vu nay, nen duoc bao
        # ve dang 429 kem Retry-After thay vi bao la loi noi bo.
        metrics.record_error()
        logger.warning("Gemini khong phuc vu duoc: %s", error)
        headers = {}
        if error.retry_after_seconds is not None:
            headers["Retry-After"] = str(int(error.retry_after_seconds) + 1)
        raise HTTPException(status_code=429, detail=str(error), headers=headers)
    except Exception:
        metrics.record_error()
        logger.exception("Agent that bai voi session %s", payload.session_id)
        raise HTTPException(status_code=500, detail="Tro ly gap su co, vui long thu lai.")

    metrics.record_request(
        latency_ms=result.latency_ms,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        cost_usd=result.usage.cost_usd,
    )
    for call in result.tool_calls:
        metrics.record_tool_call(call.name, allowed=call.allowed)

    return ChatResponse(
        answer=result.answer,
        tool_calls=[
            ToolCallView(name=c.name, allowed=c.allowed, note=c.note) for c in result.tool_calls
        ],
        iterations=result.iterations,
        latency_ms=round(result.latency_ms, 1),
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        cost_usd=round(result.usage.cost_usd, 6),
    )


@router.get("/health")
def health(request: Request) -> dict:
    return {"status": "ok", "chunks_loaded": request.app.state.chunks_loaded}


@router.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics() -> str:
    return metrics.render_prometheus()


@router.get("/stats")
def stats() -> dict:
    return metrics.snapshot()
