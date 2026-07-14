"""Issuing and verifying the access token that says who the student is.

Cap va xac minh access token, thu quyet dinh sinh vien la ai.

This is the only place the service learns a student's identity. Everything downstream - the
tools that read the grade table, the guardrail, the audit log - trusts `TurnContext.student_id`
completely, so the whole design rests on that id having been proven here rather than typed in
by whoever sent the request.
Day la noi duy nhat dich vu biet duoc sinh vien la ai. Moi thu o phia sau - cac tool doc bang
diem, guardrail, nhat ky kiem toan - deu tin tuyet doi vao `TurnContext.student_id`, nen toan bo
thiet ke dua tren viec ma so do da duoc chung minh tai day, chu khong phai do nguoi gui request
tu go vao.
"""

import uuid
from datetime import datetime, timedelta, timezone

import jwt

from app.config import Settings

# Pinned, and passed to the decoder as the only algorithm it may accept. Two classic attacks
# live in this one line. A token whose header says `alg: none` carries no signature at all, and
# a decoder that reads the algorithm from the token will happily accept it. A token that says
# `alg: RS256` would have its "signature" checked against our secret as if the secret were a
# public key. Naming the algorithm ourselves, rather than letting the token name it, closes
# both: the token no longer gets a say in how it is verified.
# Duoc ghim cung, va truyen vao ham giai ma nhu thuat toan duy nhat no duoc phep chap nhan. Hai
# don tan cong kinh dien nam gon trong mot dong nay. Mot token co header ghi `alg: none` thi
# khong mang chu ky nao ca, va mot bo giai ma nao doc thuat toan tu chinh token se vui ve chap
# nhan no. Mot token ghi `alg: RS256` thi "chu ky" cua no se bi kiem tra bang secret cua ta nhu
# the secret do la mot khoa cong khai. Tu minh chi dinh thuat toan, thay vi de token tu khai,
# chan duoc ca hai: token khong con duoc quyen quyet dinh no se bi kiem tra ra sao.
ALGORITHM = "HS256"

TOKEN_TYPE = "access"

# Claims that must be present. Requiring `exp` explicitly matters: a token without an expiry is
# valid forever, and PyJWT cannot check an expiry that was never written down.
# Cac claim bat buoc phai co. Doi `exp` mot cach tuong minh la quan trong: mot token khong co han
# dung thi co gia tri vinh vien, ma PyJWT thi khong the kiem tra mot han dung chua he duoc ghi.
REQUIRED_CLAIMS = ["exp", "iat", "sub", "iss", "aud"]


class InvalidToken(Exception):
    """The token is missing, malformed, expired, or not meant for this service.

    Token bi thieu, hong dinh dang, het han, hoac khong danh cho dich vu nay.
    """


def issue_access_token(
    student_id: str, settings: Settings, *, now: datetime | None = None
) -> tuple[str, int]:
    """Mint an access token for a student whose password has just been checked.

    Cap access token cho mot sinh vien vua duoc kiem tra dung mat khau.

    Returns the token and how many seconds it stays valid, so the caller can tell the client
    when to come back rather than leaving it to find out by being rejected.
    Tra ve token va so giay no con hieu luc, de phia goi noi truoc cho client biet khi nao phai
    quay lai, thay vi de client tu phat hien ra bang cach bi tu choi.
    """
    issued_at = now or datetime.now(timezone.utc)
    ttl = timedelta(minutes=settings.access_token_ttl_minutes)

    claims = {
        "sub": student_id,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": issued_at,
        "exp": issued_at + ttl,
        # A unique id per token. Nothing uses it yet, but it is what a revocation list would key
        # on, and it costs nothing to write down now while it costs a migration to add later.
        # Mot ma rieng cho tung token. Hien chua dung toi, nhung do la thu ma mot danh sach thu
        # hoi token se lay lam khoa, va ghi no ra bay gio thi khong ton gi, con them vao sau thi
        # ton mot lan doi luoc do.
        "jti": uuid.uuid4().hex,
        "typ": TOKEN_TYPE,
    }

    token = jwt.encode(claims, settings.jwt_secret, algorithm=ALGORITHM)
    return token, int(ttl.total_seconds())


def decode_access_token(token: str, settings: Settings) -> dict:
    """Verify a token and return its claims, or raise InvalidToken.

    Xac minh token va tra ve cac claim, hoac nem ra InvalidToken.

    The signature is not the only thing checked. `iss` and `aud` are checked as well, because a
    correctly signed token is not automatically a token meant for us: without them, a token
    minted by some other system that happened to share the secret, or minted by this system for
    a different service, would be accepted here.
    Chu ky khong phai thu duy nhat duoc kiem tra. `iss` va `aud` cung duoc kiem tra, boi mot
    token ky dung chua chac la mot token danh cho ta: neu khong co hai claim nay, mot token do
    mot he thong khac lo dung chung secret cap ra, hoac do chinh he thong nay cap cho mot dich
    vu khac, van se duoc chap nhan o day.
    """
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[ALGORITHM],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": REQUIRED_CLAIMS},
        )
    except jwt.PyJWTError as error:
        raise InvalidToken(str(error)) from error

    # A refresh token, or any other token this service might mint later, must not be usable as
    # an access token just because it was signed with the same secret.
    # Mot refresh token, hay bat ky loai token nao dich vu nay co the cap ve sau, khong duoc phep
    # dung thay access token chi vi no cung duoc ky bang mot secret.
    if claims.get("typ") != TOKEN_TYPE:
        raise InvalidToken("Token nay khong phai access token.")

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise InvalidToken("Token khong chua ma sinh vien hop le.")

    return claims
