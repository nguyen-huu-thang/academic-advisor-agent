"""HTTP API of the academic advisor assistant.

API HTTP cua tro ly co van hoc tap.
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
# Refresh token di trong mot cookie HttpOnly, con access token di trong body cua cau tra loi. Su
# tach doi do chinh la muc dich, va moi nua cua no deu dang lam mot viec.
#
# HttpOnly nghia la JavaScript khong doc duoc cookie. Nen mot lo hong XSS - cach pho bien nhat de
# mot token bi danh cap tren trinh duyet - khong the cham toi cai chung chi song lau kia. Con access
# token, thu ma script CO the cham toi, thi chi dang gia muoi lam phut chu khong hon.
#
# Access token di trong body chinh la de frontend giu no trong bo nho chu tuyet doi khong bo vao
# localStorage. Thu gi nam trong localStorage thi song sot qua mot lan tai lai trang, nghe thi tien,
# cho toi khi nho ra rang no cung song sot du lau de bat ky doan script nao duoc chen vao cung kip
# ung dung mang di.
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
# Cookie duoc gioi han vao dung tien to cua chi hai endpoint co doc no, va khong gi khac. `Path=/`
# se dinh kem no vao moi request trinh duyet gui di, ke ca /chat - von la endpoint mang tin nhan
# cua sinh vien va goi ra mot model cua ben thu ba, tuc la noi cuoi cung ma mot chung chi song hai
# tuan nen di theo.
#
# Ngay ca `/auth` cung da qua rong, va do la ly do hai endpoint nay duoc dua xuong mot tien to
# rieng: /auth/login la endpoint duy nhat xu ly mat khau, va no khong dung toi cookie refresh chut
# nao. Gui cookie toi do thi khong duoc gi ma lai co thu de mat, nen no khong duoc gui toi do.
#
# Mot chung chi khong bao gio duoc gui di la mot chung chi khong the bi lo boi bat cu thu gi xu ly
# cai request no khong bao gio duoc gui toi.
REFRESH_COOKIE_PATH = "/auth/session"


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
    """Only the access token. The refresh token is not in here, and must not be.

    Chi co access token. Refresh token khong nam trong day, va khong duoc phep nam trong day.

    Putting the refresh token in the body would hand it to JavaScript, and the frontend would then
    have to keep it somewhere - which in practice means localStorage, which in practice means the
    first XSS bug walks away with a credential good for a fortnight. It goes in an HttpOnly cookie
    instead, where script cannot reach it at all.
    Neu dat refresh token vao body thi tuc la trao no cho JavaScript, va frontend se phai cat no o
    dau do - ma trong thuc te nghia la localStorage, ma trong thuc te nghia la lo hong XSS dau tien
    se mang di mot chung chi con gia tri hai tuan. Thay vao do no di trong mot cookie HttpOnly, noi
    ma script khong the cham toi.
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
        # Script khong doc duoc.
        httponly=True,
        # The browser will not send it over plain HTTP.
        # Trinh duyet se khong gui no qua HTTP tran.
        secure=settings.cookie_secure,
        # A cookie is attached by the browser automatically, which is exactly what makes a
        # cookie-borne credential vulnerable to CSRF: some other site can cause the browser to
        # POST to /auth/refresh and the cookie rides along. SameSite=strict is what stops that -
        # the cookie is simply not attached to a request that originated anywhere else.
        # Cookie duoc trinh duyet tu dong dinh kem, va do dung la thu lam cho mot chung chi nam
        # trong cookie de bi CSRF: mot trang web khac co the khien trinh duyet POST toi
        # /auth/refresh va cookie di theo luon. SameSite=strict chan dieu do - cookie don gian la
        # khong duoc dinh kem vao mot request xuat phat tu bat ky noi nao khac.
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
def login(payload: LoginRequest, response: Response) -> TokenResponse:
    """Exchange a student id and password for an access token, plus a refresh cookie.

    Doi ma sinh vien va mat khau lay mot access token, kem mot cookie refresh.
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

    # A fresh family per login, so signing in on a phone does not disturb the laptop, and
    # revoking one does not revoke the other.
    # Moi lan dang nhap mo mot ho rieng, nen dang nhap tren dien thoai khong lam phien may tinh,
    # va thu hoi cai nay khong thu hoi cai kia.
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

    Tieu cookie refresh, nhan mot access token moi va mot cookie refresh moi.

    The old refresh token is dead the instant this succeeds. That is what rotation buys: a token
    copied off the wire is only good until its rightful owner next refreshes, instead of being
    good for the next fortnight.
    Refresh token cu chet ngay khi lenh nay thanh cong. Do la thu ma viec xoay vong mua ve: mot
    token bi sao chep tren duong truyen chi con gia tri cho toi lan refresh tiep theo cua chu that
    su cua no, thay vi con gia tri suot hai tuan toi.
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
        # Day la bao dong, khong phai nhieu. Ca ho token da bi thu hoi truoc khi den duoc day; viec
        # con lai chi la xoa cookie de trinh duyet thoi trinh ra mot token da chet, va noi to du de
        # nguoi dang nhin vao metrics thay duoc.
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

    Thu hoi toan bo ho token ma token nay thuoc ve, va xoa cookie.

    The family, not just the token in hand. Killing only the token presented would leave its
    parent - already rotated, but still alive in the table - and anyone holding that parent could
    carry on refreshing as if nothing had happened.
    Ca ho, chu khong chi token dang cam tren tay. Neu chi giet token duoc trinh ra thi token cha
    cua no - da bi xoay vong, nhung van con song trong bang - se sot lai, va bat ky ai dang giu
    token cha do van cu the ma refresh tiep nhu chua he co chuyen gi.

    Logging out with no cookie, or with a token nobody recognises, still succeeds: the caller ends
    up logged out either way, which is exactly what they asked for.
    Dang xuat ma khong co cookie, hoac voi mot token khong ai nhan ra, van thanh cong: du sao nguoi
    goi cung ket thuc o trang thai da dang xuat, dung nhu ho muon.
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
