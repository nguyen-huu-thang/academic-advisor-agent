"""The FastAPI dependency that turns a bearer token into a student id.

Dependency của FastAPI, biến một bearer token thành một mã sinh viên.

Every route that touches a student's data depends on this, and this is the only way a student id
enters the service. That is the whole point: `student_id` used to arrive in the request body,
where anyone could type anyone else's, and the tools then read the grade table with it.
Mọi route có đụng tới dữ liệu của sinh viên đều phụ thuộc vào đây, và đây là con đường duy nhất
một mã sinh viên đi vào được dịch vụ. Đó chính là mục đích: trước kia `student_id` đi vào qua
body của request, nơi ai cũng gõ được mã của người khác, rồi các tool cứ thế mà đọc bảng điểm.
"""

import hmac

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.tokens import InvalidToken, decode_access_token
from app.config import load_settings

# auto_error=False so that a missing header reaches us as None and we answer it ourselves, with
# the same shape of message as an invalid one.
# auto_error=False để một request thiếu header đi tới đây dưới dạng None và chính ta trả lời nó,
# với thông báo cùng dạng với trường hợp token không hợp lệ.
bearer_scheme = HTTPBearer(auto_error=False)


def get_current_student(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str:
    """The student this request is authorised to act as. Nothing else may name a student.

    Sinh viên mà request này được phép đóng vai. Không còn thứ gì khác được quyền nêu ra một
    sinh viên.
    """
    if credentials is None:
        raise _unauthorised("Thieu access token. Hay dang nhap tai POST /auth/login.")

    try:
        claims = decode_access_token(credentials.credentials, load_settings())
    except InvalidToken as error:
        raise _unauthorised(f"Access token khong hop le: {error}") from error

    return claims["sub"]


def require_ops_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> None:
    """Guard the operational endpoints, which are not for students.

    Canh các endpoint vận hành, vốn không dành cho sinh viên.

    A student's access token deliberately does not open these. /metrics and /stats say what the
    service costs to run and how often the guardrail fires; that is the operator's business, and
    handing it to everyone who can log in would be handing out the bill.
    Access token của sinh viên cố ý không mở được các endpoint này. /metrics và /stats nói lên chi
    phí vận hành dịch vụ và số lần guardrail chặn; đó là việc của người vận hành, và đưa nó cho mọi
    người đăng nhập được thì khác nào đưa cả hóa đơn ra.
    """
    if credentials is None:
        raise _unauthorised("Thieu token van hanh.")

    expected = load_settings().metrics_token
    # Constant-time, for the same reason the password check is: a plain `==` leaks how many
    # leading characters were right through how long it took to say no.
    # So sánh trong thời gian hằng định, cùng lý do với phép kiểm tra mật khẩu: một phép `==`
    # thông thường lộ ra có bao nhiêu ký tự đầu đã đúng, qua chính thời gian nó trả lời không.
    if not hmac.compare_digest(credentials.credentials, expected):
        raise _unauthorised("Token van hanh khong hop le.")


def _unauthorised(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )
