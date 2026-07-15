"""Conversation memory backed by PostgreSQL.

Bo nho hoi thoai, luu trong PostgreSQL.
"""

from google.genai import types

from app.db import get_connection

# Only the recent turns are replayed to the model. Sending the whole history would grow the
# prompt without bound, and input tokens are what the bill is made of.
# Chi phat lai cac luot gan day cho model. Gui toan bo lich su se lam prompt phinh ra vo han,
# ma input token chinh la thu tao nen hoa don.
DEFAULT_HISTORY_TURNS = 10


class ConversationMemory:
    """History scoped by student as well as by session.

    Lich su duoc gioi han theo ca sinh vien lan phien lam viec.

    Both keys are used on every read. A session id alone would be enough to fetch a
    conversation, so guessing or reusing someone else's session id would replay their
    conversation into this student's prompt. Requiring the student id as well means a session
    id belonging to another student simply returns nothing.
    Ca hai khoa deu duoc dung o moi lan doc. Neu chi can session id la lay duoc hoi thoai, thi
    doan trung hoac dung lai session id cua nguoi khac se keo hoi thoai cua ho vao prompt cua
    sinh vien nay. Bat buoc phai co ca ma sinh vien nghia la mot session id cua sinh vien khac
    se chi tra ve rong.
    """

    def __init__(self, *, history_turns: int = DEFAULT_HISTORY_TURNS) -> None:
        self._history_turns = history_turns

    def load_history(self, session_id: str, student_id: str) -> list[types.Content]:
        """Load recent conversation turns for one student's session, in chronological order.

        Nap cac luot hoi thoai gan day cua mot phien cua mot sinh vien, theo dung thu tu thoi gian.

        Truy van long hai tang, va thu tu sap xep hai tang la nguoc nhau - co chu dich:
          - Tang trong: ORDER BY id DESC ... LIMIT N -> lay N dong MOI NHAT (id lon nhat truoc).
          - Tang ngoai: ORDER BY id ASC             -> dao lai ve thu tu cu -> moi, dung thu tu
            hoi thoai de nap cho model.
        Neu chi co mot tang ASC + LIMIT thi se lay nham N dong DAU TIEN (cu nhat), khong phai
        gan day nhat. Con neu de nguyen thu tu DESC thi model doc hoi thoai nguoc dong thoi gian.
        Nhan doi (history_turns * 2) vi moi luot gom hai dong: mot cua sinh vien, mot cua model.
        Loc theo ca session_id lan student_id (xem docstring cua class) de khong lo doc nham
        hoi thoai cua nguoi khac.
        """
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT role, content FROM (
                    SELECT role, content, id
                    FROM messages
                    WHERE session_id = %s AND student_id = %s
                    ORDER BY id DESC
                    LIMIT %s
                ) recent
                ORDER BY id ASC
                """,
                (session_id, student_id, self._history_turns * 2),
            ).fetchall()

        # Chuyen moi dong thanh mot Content cua Gemini: role la "user" (sinh vien) hoac "model".
        return [
            types.Content(role=row["role"], parts=[types.Part.from_text(text=row["content"])])
            for row in rows
        ]

    def append(self, session_id: str, student_id: str, role: str, content: str) -> None:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO messages (session_id, student_id, role, content)
                VALUES (%s, %s, %s, %s)
                """,
                (session_id, student_id, role, content),
            )

    def clear(self, session_id: str, student_id: str) -> None:
        with get_connection() as conn:
            conn.execute(
                "DELETE FROM messages WHERE session_id = %s AND student_id = %s",
                (session_id, student_id),
            )
