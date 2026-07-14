"""Checking a student's password against the record.

Doi chieu mat khau cua sinh vien voi ho so.
"""

from app.auth.passwords import dummy_hash, verify_password
from app.db import get_connection


def authenticate(student_id: str, password: str) -> str | None:
    """Return the student id if the password is right, otherwise None.

    Tra ve ma sinh vien neu mat khau dung, nguoc lai tra ve None.

    An unknown student and a wrong password are answered the same way, and are made to cost the
    same time. Telling the two apart - by a different message, or merely by a faster answer -
    would turn this endpoint into a way to enumerate which student ids exist, which is a piece
    of the university's data that nobody outside it should be able to read off a login form.
    Mot sinh vien khong ton tai va mot mat khau sai duoc tra loi giong het nhau, va duoc lam cho
    ton thoi gian nhu nhau. Neu phan biet hai truong hop nay - bang mot thong bao khac di, hay
    chi don gian bang mot cau tra loi nhanh hon - thi endpoint nay se tro thanh mot cach de do
    xem nhung ma sinh vien nao co that, von la mot phan du lieu cua nha truong ma khong ai ben
    ngoai duoc phep doc ra tu mot o dang nhap.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT student_id, password_hash FROM students WHERE student_id = %s",
            (student_id,),
        ).fetchone()

    if row is None:
        verify_password(password, dummy_hash())
        return None

    if not verify_password(password, row["password_hash"]):
        return None

    return row["student_id"]
