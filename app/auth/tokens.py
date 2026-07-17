"""Issuing and verifying the access token that says who the student is.

Cấp và xác minh access token, thứ quyết định sinh viên là ai.

This is the only place the service learns a student's identity. Everything downstream - the
tools that read the grade table, the guardrail, the audit log - trusts `TurnContext.student_id`
completely, so the whole design rests on that id having been proven here rather than typed in
by whoever sent the request.
Đây là nơi duy nhất dịch vụ biết được sinh viên là ai. Mọi thứ ở phía sau - các tool đọc bảng
điểm, guardrail, nhật ký kiểm toán - đều tin tuyệt đối vào `TurnContext.student_id`, nên toàn bộ
thiết kế dựa trên việc mã số đó đã được chứng minh tại đây, chứ không phải do người gửi request
tự gõ vào.
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
# Được ghim cứng, và truyền vào hàm giải mã như thuật toán duy nhất nó được phép chấp nhận. Hai
# đòn tấn công kinh điển nằm gọn trong một dòng này. Một token có header ghi `alg: none` thì
# không mang chữ ký nào cả, và một bộ giải mã nào đọc thuật toán từ chính token sẽ vui vẻ chấp
# nhận nó. Một token ghi `alg: RS256` thì "chữ ký" của nó sẽ bị kiểm tra bằng secret của ta như
# thể secret đó là một khóa công khai. Tự mình chỉ định thuật toán, thay vì để token tự khai,
# chặn được cả hai: token không còn được quyền quyết định nó sẽ bị kiểm tra ra sao.
ALGORITHM = "HS256"

TOKEN_TYPE = "access"

# Claims that must be present. Requiring `exp` explicitly matters: a token without an expiry is
# valid forever, and PyJWT cannot check an expiry that was never written down.
# Các claim bắt buộc phải có. Đòi `exp` một cách tường minh là quan trọng: một token không có hạn
# dùng thì có giá trị vĩnh viễn, mà PyJWT thì không thể kiểm tra một hạn dùng chưa hề được ghi.
REQUIRED_CLAIMS = ["exp", "iat", "sub", "iss", "aud"]


class InvalidToken(Exception):
    """The token is missing, malformed, expired, or not meant for this service.

    Token bị thiếu, hỏng định dạng, hết hạn, hoặc không dành cho dịch vụ này.
    """


def issue_access_token(
    student_id: str, settings: Settings, *, now: datetime | None = None
) -> tuple[str, int]:
    """Mint an access token for a student whose password has just been checked.

    Cấp access token cho một sinh viên vừa được kiểm tra đúng mật khẩu.

    Returns the token and how many seconds it stays valid, so the caller can tell the client
    when to come back rather than leaving it to find out by being rejected.
    Trả về token và số giây nó còn hiệu lực, để phía gọi nói trước cho client biết khi nào phải
    quay lại, thay vì để client tự phát hiện ra bằng cách bị từ chối.
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
        # Một mã riêng cho từng token. Hiện chưa dùng tới, nhưng đó là thứ mà một danh sách thu
        # hồi token sẽ lấy làm khóa, và ghi nó ra bây giờ thì không tốn gì, còn thêm vào sau thì
        # tốn một lần đổi lược đồ.
        "jti": uuid.uuid4().hex,
        "typ": TOKEN_TYPE,
    }

    token = jwt.encode(claims, settings.jwt_secret, algorithm=ALGORITHM)
    return token, int(ttl.total_seconds())


def decode_access_token(token: str, settings: Settings) -> dict:
    """Verify a token and return its claims, or raise InvalidToken.

    Xác minh token và trả về các claim, hoặc ném ra InvalidToken.

    The signature is not the only thing checked. `iss` and `aud` are checked as well, because a
    correctly signed token is not automatically a token meant for us: without them, a token
    minted by some other system that happened to share the secret, or minted by this system for
    a different service, would be accepted here.
    Chữ ký không phải thứ duy nhất được kiểm tra. `iss` và `aud` cũng được kiểm tra, bởi một
    token ký đúng chưa chắc là một token dành cho ta: nếu không có hai claim này, một token do
    một hệ thống khác lỡ dùng chung secret cấp ra, hoặc do chính hệ thống này cấp cho một dịch
    vụ khác, vẫn sẽ được chấp nhận ở đây.
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
    # Một refresh token, hay bất kỳ loại token nào dịch vụ này có thể cấp về sau, không được phép
    # dùng thay access token chỉ vì nó cũng được ký bằng một secret.
    if claims.get("typ") != TOKEN_TYPE:
        raise InvalidToken("Token nay khong phai access token.")

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise InvalidToken("Token khong chua ma sinh vien hop le.")

    return claims
