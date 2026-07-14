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
