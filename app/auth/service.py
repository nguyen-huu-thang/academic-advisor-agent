"""Checking a student's password against the record.

Đối chiếu mật khẩu của sinh viên với hồ sơ.
"""

from app.auth.passwords import dummy_hash, verify_password
from app.db import get_connection


def authenticate(student_id: str, password: str) -> str | None:
    """Return the student id if the password is right, otherwise None.

    Trả về mã sinh viên nếu mật khẩu đúng, ngược lại trả về None.

    An unknown student and a wrong password are answered the same way, and are made to cost the
    same time. Telling the two apart - by a different message, or merely by a faster answer -
    would turn this endpoint into a way to enumerate which student ids exist, which is a piece
    of the university's data that nobody outside it should be able to read off a login form.
    Một sinh viên không tồn tại và một mật khẩu sai được trả lời giống hệt nhau, và được làm cho
    tốn thời gian như nhau. Nếu phân biệt hai trường hợp này - bằng một thông báo khác đi, hay
    chỉ đơn giản bằng một câu trả lời nhanh hơn - thì endpoint này sẽ trở thành một cách để dò
    xem những mã sinh viên nào có thật, vốn là một phần dữ liệu của nhà trường mà không ai bên
    ngoài được phép đọc ra từ một ô đăng nhập.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT student_id, password_hash FROM students WHERE student_id = %s",
            (student_id,),
        ).fetchone()

    # No such student: still burn a full scrypt verification against a dummy hash, so this
    # branch takes as long as a real wrong-password check (see dummy_hash's docstring).
    # Không có sinh viên này: vẫn đốt trọn một lần kiểm tra scrypt trên bản băm giả, để nhánh
    # này tốn thời gian y như một lần kiểm tra sai mật khẩu thật (xem docstring của dummy_hash).
    if row is None:
        verify_password(password, dummy_hash())
        return None

    if not verify_password(password, row["password_hash"]):
        return None

    return row["student_id"]
