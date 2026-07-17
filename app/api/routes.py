"""HTTP API of the academic advisor assistant.

API HTTP của trợ lý cố vấn học tập.
"""

import logging
import time
from functools import lru_cache

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.agent.guardrail import mask_student_id
from app.agent.loop import AdvisorAgent, StudentNotFound
from app.auth import refresh as refresh_tokens
from app.auth.dependencies import get_current_student, require_ops_token
from app.auth.refresh import InvalidRefreshToken, RefreshTokenReused
from app.auth.service import authenticate
from app.auth.throttle import LoginThrottle
from app.auth.tokens import issue_access_token
from app.config import Settings, load_settings
from app.llm.gemini import UpstreamUnavailable
from app.observability.metrics import metrics

logger = logging.getLogger(__name__)
router = APIRouter()

# The refresh token travels in an HttpOnly cookie, and the access token travels in the response
# body. That split is the whole point, and each half of it is doing a job.
#
# HttpOnly means JavaScript cannot read the cookie. So a cross-site scripting bug - the single
# most common way a browser token is stolen - cannot reach the long-lived credential at all. The
# access token, which script CAN reach, is worth fifteen minutes and nothing more.
#
# The access token goes in the body precisely so that the frontend keeps it in memory and never
# in localStorage. Anything in localStorage survives a page reload, which sounds convenient until
# one remembers that it also survives long enough for any injected script to walk off with it.
#
# Refresh token đi trong một cookie HttpOnly, còn access token đi trong body của câu trả lời. Sự
# tách đôi đó chính là mục đích, và mỗi nửa của nó đều đang làm một việc.
#
# HttpOnly nghĩa là JavaScript không đọc được cookie. Nên một lỗ hổng XSS - cách phổ biến nhất để
# một token bị đánh cắp trên trình duyệt - không thể chạm tới cái chứng chỉ sống lâu kia. Còn access
# token, thứ mà script CÓ thể chạm tới, thì chỉ đáng giá mười lăm phút chứ không hơn.
#
# Access token đi trong body chính là để frontend giữ nó trong bộ nhớ chứ tuyệt đối không bỏ vào
# localStorage. Thứ gì nằm trong localStorage thì sống sót qua một lần tải lại trang, nghe thì tiện,
# cho tới khi nhớ ra rằng nó cũng sống sót đủ lâu để bất kỳ đoạn script nào được chèn vào cũng kịp
# ung dung mang đi.
REFRESH_COOKIE_NAME = "refresh_token"

# The cookie is scoped to the exact prefix of the only two endpoints that read it, and nothing
# else. `Path=/` would attach it to every request the browser makes, including /chat - which is
# the endpoint that carries student messages and calls out to a third-party model, and is
# therefore the last place a fortnight-long credential should be riding along.
#
# Even `/auth` would be too wide, and that is why the two endpoints were moved under their own
# prefix: /auth/login is the one endpoint that handles a password, and it has no use for the
# refresh cookie at all. There is nothing to gain from sending it there and something to lose, so
# it is not sent there.
#
# A credential that is never sent is a credential that cannot be leaked by whatever handles the
# request it was never sent to.
#
# Cookie được giới hạn vào đúng tiền tố của chỉ hai endpoint có đọc nó, và không gì khác. `Path=/`
# sẽ đính kèm nó vào mọi request trình duyệt gửi đi, kể cả /chat - vốn là endpoint mang tin nhắn
# của sinh viên và gọi ra một model của bên thứ ba, tức là nơi cuối cùng mà một chứng chỉ sống hai
# tuần nên đi theo.
#
# Ngay cả `/auth` cũng đã quá rộng, và đó là lý do hai endpoint này được đưa xuống một tiền tố
# riêng: /auth/login là endpoint duy nhất xử lý mật khẩu, và nó không dùng tới cookie refresh chút
# nào. Gửi cookie tới đó thì không được gì mà lại có thứ để mất, nên nó không được gửi tới đó.
#
# Một chứng chỉ không bao giờ được gửi đi là một chứng chỉ không thể bị lộ bởi bất cứ thứ gì xử lý
# cái request nó không bao giờ được gửi tới.
REFRESH_COOKIE_PATH = "/auth/session"


@lru_cache(maxsize=1)
def _login_throttle() -> LoginThrottle:
    """One throttle for the whole process, built on first use rather than at import.

    Một bộ đếm khóa tài khoản dùng chung cho cả tiến trình, dựng lên ở lần gọi đầu tiên chứ
    không phải lúc import.

    Building it at import time would mean merely importing this module required a full, valid
    environment - which would make the module impossible to import in a test that has no
    business needing a database URL or an API key.
    Nếu dựng lên ngay lúc import thì chỉ riêng việc import module này đã đòi một môi trường đầy
    đủ và hợp lệ - khiến module không thể import được trong một bài test vốn chẳng liên quan gì
    tới database hay API key.
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
    """Only the access token. The refresh token is not in here, and must not be.

    Chỉ có access token. Refresh token không nằm trong đây, và không được phép nằm trong đây.

    Putting the refresh token in the body would hand it to JavaScript, and the frontend would then
    have to keep it somewhere - which in practice means localStorage, which in practice means the
    first XSS bug walks away with a credential good for a fortnight. It goes in an HttpOnly cookie
    instead, where script cannot reach it at all.
    Nếu đặt refresh token vào body thì tức là trao nó cho JavaScript, và frontend sẽ phải cất nó ở
    đâu đó - mà trong thực tế nghĩa là localStorage, mà trong thực tế nghĩa là lỗ hổng XSS đầu tiên
    sẽ mang đi một chứng chỉ còn giá trị hai tuần. Thay vào đó nó đi trong một cookie HttpOnly, nơi
    mà script không thể chạm tới.
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int


def _set_refresh_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token,
        max_age=settings.refresh_token_ttl_days * 24 * 3600,
        # Script cannot read it.
        # Script không đọc được.
        httponly=True,
        # The browser will not send it over plain HTTP.
        # Trình duyệt sẽ không gửi nó qua HTTP trần.
        secure=settings.cookie_secure,
        # A cookie is attached by the browser automatically, which is exactly what makes a
        # cookie-borne credential vulnerable to CSRF: some other site can cause the browser to
        # POST to /auth/refresh and the cookie rides along. SameSite=strict is what stops that -
        # the cookie is simply not attached to a request that originated anywhere else.
        # Cookie được trình duyệt tự động đính kèm, và đó đúng là thứ làm cho một chứng chỉ nằm
        # trong cookie dễ bị CSRF: một trang web khác có thể khiến trình duyệt POST tới
        # /auth/refresh và cookie đi theo luôn. SameSite=strict chặn điều đó - cookie đơn giản là
        # không được đính kèm vào một request xuất phát từ bất kỳ nơi nào khác.
        samesite="strict",
        path=REFRESH_COOKIE_PATH,
    )


def _clear_refresh_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
    )


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
    # Ở đây cố ý không còn `student_id`. Trước kia nó nằm trong body này, nghĩa là ai cũng nêu ra
    # được bất kỳ ai: các tool sẽ đọc bảng điểm của sinh viên đó, còn guardrail sẽ kiểm tra môn
    # tiên quyết của sinh viên đó. Bây giờ nó đến từ token đã ký, và không đến từ đâu khác.
    #
    # Đây chính là nước đi đã làm với schema của các tool, áp dụng ra thêm một lớp nữa: cách chắc
    # chắn nhất để người gọi không nêu ra được mã sinh viên của người khác là không chừa một ô
    # trống nào cho nó. Một `student_id` gửi kèm trong body hôm nay sẽ bị bỏ qua - nó không còn
    # là một trường của model này nữa.


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
def login(payload: LoginRequest, response: Response) -> TokenResponse:
    """Exchange a student id and password for an access token, plus a refresh cookie.

    Đổi mã sinh viên và mật khẩu lấy một access token, kèm một cookie refresh.
    """
    settings = load_settings()
    now = time.monotonic()

    throttle = _login_throttle()

    waiting = throttle.seconds_until_unlocked(payload.student_id, now)
    if waiting is not None:
        # 423 would be more precise, but 429 with Retry-After is what clients already know how
        # to obey, and it is the same answer this service gives when Gemini rate limits it.
        # Mã 423 thì chính xác hơn, nhưng 429 kèm Retry-After mới là thứ client vốn đã biết cách
        # tuân theo, và cũng là câu trả lời dịch vụ này đưa ra khi bị Gemini chặn vì quá hạn mức.
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
        # Một thông báo chung cho cả "không có sinh viên này" lẫn "sai mật khẩu". Nếu nói rõ là
        # trường hợp nào thì ai cũng dò được danh sách mã sinh viên từ chính endpoint này.
        logger.warning("Dang nhap that bai cho %s", mask_student_id(payload.student_id))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ma sinh vien hoac mat khau khong dung.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    throttle.record_success(student_id)

    # A fresh family per login, so signing in on a phone does not disturb the laptop, and
    # revoking one does not revoke the other.
    # Mỗi lần đăng nhập mở một họ riêng, nên đăng nhập trên điện thoại không làm phiền máy tính,
    # và thu hồi cái này không thu hồi cái kia.
    refresh_token = refresh_tokens.issue_for_new_login(student_id, settings)
    _set_refresh_cookie(response, refresh_token, settings)

    token, expires_in = issue_access_token(student_id, settings)
    logger.info("Dang nhap thanh cong cho %s", mask_student_id(student_id))

    return TokenResponse(access_token=token, expires_in=expires_in)


@router.post("/auth/session/refresh", response_model=TokenResponse)
def refresh(
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
) -> TokenResponse:
    """Spend the refresh cookie, get a new access token and a new refresh cookie.

    Tiêu cookie refresh, nhận một access token mới và một cookie refresh mới.

    The old refresh token is dead the instant this succeeds. That is what rotation buys: a token
    copied off the wire is only good until its rightful owner next refreshes, instead of being
    good for the next fortnight.
    Refresh token cũ chết ngay khi lệnh này thành công. Đó là thứ mà việc xoay vòng mua về: một
    token bị sao chép trên đường truyền chỉ còn giá trị cho tới lần refresh tiếp theo của chủ thật
    sự của nó, thay vì còn giá trị suốt hai tuần tới.
    """
    settings = load_settings()

    if refresh_token is None:
        raise _unauthorised_refresh("Thieu refresh token. Hay dang nhap lai.")

    try:
        rotated = refresh_tokens.rotate(refresh_token, settings)
    except RefreshTokenReused as reuse:
        # The alarm, not the noise. The family is already revoked by the time we get here; all
        # that is left is to clear the cookie so the browser stops presenting a dead token, and to
        # say so loudly enough that someone watching the metrics can see it.
        # Đây là báo động, không phải nhiễu. Cả họ token đã bị thu hồi trước khi đến được đây; việc
        # còn lại chỉ là xóa cookie để trình duyệt thôi trình ra một token đã chết, và nói to đủ để
        # người đang nhìn vào metrics thấy được.
        metrics.record_refresh_reuse()
        logger.warning("PHAT HIEN TAI SU DUNG REFRESH TOKEN. Da thu hoi ca ho token.")
        _clear_refresh_cookie(response, settings)
        raise _unauthorised_refresh(str(reuse)) from reuse
    except InvalidRefreshToken as error:
        _clear_refresh_cookie(response, settings)
        raise _unauthorised_refresh(str(error)) from error

    _set_refresh_cookie(response, rotated.refresh_token, settings)
    token, expires_in = issue_access_token(rotated.student_id, settings)

    return TokenResponse(access_token=token, expires_in=expires_in)


@router.post("/auth/session/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
) -> None:
    """Revoke the whole family this token belongs to, and clear the cookie.

    Thu hồi toàn bộ họ token mà token này thuộc về, và xóa cookie.

    The family, not just the token in hand. Killing only the token presented would leave its
    parent - already rotated, but still alive in the table - and anyone holding that parent could
    carry on refreshing as if nothing had happened.
    Cả họ, chứ không chỉ token đang cầm trên tay. Nếu chỉ giết token được trình ra thì token cha
    của nó - đã bị xoay vòng, nhưng vẫn còn sống trong bảng - sẽ sót lại, và bất kỳ ai đang giữ
    token cha đó vẫn cứ thế mà refresh tiếp như chưa hề có chuyện gì.

    Logging out with no cookie, or with a token nobody recognises, still succeeds: the caller ends
    up logged out either way, which is exactly what they asked for.
    Đăng xuất mà không có cookie, hoặc với một token không ai nhận ra, vẫn thành công: dù sao người
    gọi cũng kết thúc ở trạng thái đã đăng xuất, đúng như họ muốn.
    """
    settings = load_settings()

    if refresh_token is not None:
        refresh_tokens.revoke_family_of(refresh_token)

    _clear_refresh_cookie(response, settings)


def _unauthorised_refresh(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


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
        # Bị nhà cung cấp model chặn vì quá hạn mức không phải lỗi của dịch vụ này, nên được báo
        # về dạng 429 kèm Retry-After thay vì báo là lỗi nội bộ.
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


@router.get(
    "/metrics",
    response_class=PlainTextResponse,
    dependencies=[Depends(require_ops_token)],
)
def prometheus_metrics() -> str:
    return metrics.render_prometheus()


@router.get("/stats", dependencies=[Depends(require_ops_token)])
def stats() -> dict:
    return metrics.snapshot()
