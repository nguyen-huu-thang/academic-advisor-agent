"""The FastAPI dependency that turns a bearer token into a student id.

Dependency cua FastAPI, bien mot bearer token thanh mot ma sinh vien.

Every route that touches a student's data depends on this, and this is the only way a student id
enters the service. That is the whole point: `student_id` used to arrive in the request body,
where anyone could type anyone else's, and the tools then read the grade table with it.
Moi route co dung toi du lieu cua sinh vien deu phu thuoc vao day, va day la con duong duy nhat
mot ma sinh vien di vao duoc dich vu. Do chinh la muc dich: truoc kia `student_id` di vao qua
body cua request, noi ai cung go duoc ma cua nguoi khac, roi cac tool cu the ma doc bang diem.
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.tokens import InvalidToken, decode_access_token
from app.config import load_settings

# auto_error=False so that a missing header reaches us as None and we answer it ourselves, with
# the same shape of message as an invalid one.
# auto_error=False de mot request thieu header di toi day duoi dang None va chinh ta tra loi no,
# voi thong bao cung dang voi truong hop token khong hop le.
bearer_scheme = HTTPBearer(auto_error=False)


def get_current_student(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str:
    """The student this request is authorised to act as. Nothing else may name a student.

    Sinh vien ma request nay duoc phep dong vai. Khong con thu gi khac duoc quyen neu ra mot
    sinh vien.
    """
    if credentials is None:
        raise _unauthorised("Thieu access token. Hay dang nhap tai POST /auth/login.")

    try:
        claims = decode_access_token(credentials.credentials, load_settings())
    except InvalidToken as error:
        raise _unauthorised(f"Access token khong hop le: {error}") from error

    return claims["sub"]


def _unauthorised(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )
