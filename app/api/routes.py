"""HTTP API of the academic advisor assistant.

API HTTP cua tro ly co van hoc tap.
"""

import logging
import time
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.agent.guardrail import mask_student_id
from app.agent.loop import AdvisorAgent, StudentNotFound
from app.auth.dependencies import get_current_student
from app.auth.service import authenticate
from app.auth.throttle import LoginThrottle
from app.auth.tokens import issue_access_token
from app.config import load_settings
from app.llm.gemini import UpstreamUnavailable
from app.observability.metrics import metrics

logger = logging.getLogger(__name__)
router = APIRouter()


@lru_cache(maxsize=1)
def _login_throttle() -> LoginThrottle:
    """One throttle for the whole process, built on first use rather than at import.

    Mot bo dem khoa tai khoan dung chung cho ca tien trinh, dung len o lan goi dau tien chu
    khong phai luc import.

    Building it at import time would mean merely importing this module required a full, valid
    environment - which would make the module impossible to import in a test that has no
    business needing a database URL or an API key.
    Neu dung len ngay luc import thi chi rieng viec import module nay da doi mot moi truong day
    du va hop le - khien module khong the import duoc trong mot bai test von chang lien quan gi
    toi database hay API key.
    """
    settings = load_settings()
    return LoginThrottle(
        max_attempts=settings.login_max_attempts,
        lockout_seconds=settings.login_lockout_minutes * 60,
    )


class LoginRequest(BaseModel):
    student_id: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=2000)
    # There is deliberately no `student_id` here. It used to sit in this body, which meant
    # anyone could name anyone: the tools would then read that student's grades, and the
    # guardrail would check that student's prerequisites. It now comes from the signed token
    # and from nowhere else.
    #
    # This is the same move already made in the tool schemas, applied one layer further out: the
    # surest way to stop a caller naming someone else's student id is to leave no field for one.
    # A `student_id` sent in this body today is simply ignored - it is not a field of this model.
    #
    # O day co y khong con `student_id`. Truoc kia no nam trong body nay, nghia la ai cung neu ra
    # duoc bat ky ai: cac tool se doc bang diem cua sinh vien do, con guardrail se kiem tra mon
    # tien quyet cua sinh vien do. Bay gio no den tu token da ky, va khong den tu dau khac.
    #
    # Day chinh la nuoc di da lam voi schema cua cac tool, ap dung ra them mot lop nua: cach chac
    # chan nhat de nguoi goi khong neu ra duoc ma sinh vien cua nguoi khac la khong chua mot o
    # trong nao cho no. Mot `student_id` gui kem trong body hom nay se bi bo qua - no khong con
    # la mot truong cua model nay nua.


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


@router.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest) -> TokenResponse:
    """Exchange a student id and password for an access token.

    Doi ma sinh vien va mat khau lay mot access token.
    """
    settings = load_settings()
    now = time.monotonic()

    throttle = _login_throttle()

    waiting = throttle.seconds_until_unlocked(payload.student_id, now)
    if waiting is not None:
        # 423 would be more precise, but 429 with Retry-After is what clients already know how
        # to obey, and it is the same answer this service gives when Gemini rate limits it.
        # Ma 423 thi chinh xac hon, nhung 429 kem Retry-After moi la thu client von da biet cach
        # tuan theo, va cung la cau tra loi dich vu nay dua ra khi bi Gemini chan vi qua han muc.
        logger.warning(
            "Dang nhap bi khoa tam thoi cho %s", mask_student_id(payload.student_id)
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Sai mat khau qua nhieu lan. Tai khoan bi tam khoa, vui long thu lai sau.",
            headers={"Retry-After": str(int(waiting) + 1)},
        )

    student_id = authenticate(payload.student_id, payload.password)
    if student_id is None:
        throttle.record_failure(payload.student_id, now)
        # One message for both "no such student" and "wrong password". Saying which one it was
        # would let anyone read the list of student ids off this endpoint.
        # Mot thong bao chung cho ca "khong co sinh vien nay" lan "sai mat khau". Neu noi ro la
        # truong hop nao thi ai cung do duoc danh sach ma sinh vien tu chinh endpoint nay.
        logger.warning("Dang nhap that bai cho %s", mask_student_id(payload.student_id))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ma sinh vien hoac mat khau khong dung.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    throttle.record_success(student_id)
    token, expires_in = issue_access_token(student_id, settings)
    logger.info("Dang nhap thanh cong cho %s", mask_student_id(student_id))

    return TokenResponse(access_token=token, expires_in=expires_in)


@router.post("/chat", response_model=ChatResponse)
def chat(
    payload: ChatRequest,
    request: Request,
    student_id: str = Depends(get_current_student),
) -> ChatResponse:
    agent: AdvisorAgent = request.app.state.agent

    try:
        result = agent.run(payload.session_id, student_id, payload.message)
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
