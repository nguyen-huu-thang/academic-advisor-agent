"""Conversation memory backed by PostgreSQL.

Bộ nhớ hội thoại, lưu trong PostgreSQL.
"""

from google.genai import types

from app.db import get_connection

# Only the recent turns are replayed to the model. Sending the whole history would grow the
# prompt without bound, and input tokens are what the bill is made of.
# Chỉ phát lại các lượt gần đây cho model. Gửi toàn bộ lịch sử sẽ làm prompt phình ra vô hạn,
# mà input token chính là thứ tạo nên hóa đơn.
DEFAULT_HISTORY_TURNS = 10


class ConversationMemory:
    """History scoped by student as well as by session.

    Lịch sử được giới hạn theo cả sinh viên lẫn phiên làm việc.

    Both keys are used on every read. A session id alone would be enough to fetch a
    conversation, so guessing or reusing someone else's session id would replay their
    conversation into this student's prompt. Requiring the student id as well means a session
    id belonging to another student simply returns nothing.
    Cả hai khóa đều được dùng ở mọi lần đọc. Nếu chỉ cần session id là lấy được hội thoại, thì
    đoán trúng hoặc dùng lại session id của người khác sẽ kéo hội thoại của họ vào prompt của
    sinh viên này. Bắt buộc phải có cả mã sinh viên nghĩa là một session id của sinh viên khác
    sẽ chỉ trả về rỗng.
    """

    def __init__(self, *, history_turns: int = DEFAULT_HISTORY_TURNS) -> None:
        self._history_turns = history_turns

    def load_history(self, session_id: str, student_id: str) -> list[types.Content]:
        """Load recent conversation turns for one student's session, in chronological order.

        Nạp các lượt hội thoại gần đây của một phiên của một sinh viên, theo đúng thứ tự thời gian.

        Truy vấn lồng hai tầng, và thứ tự sắp xếp hai tầng là ngược nhau - có chủ đích:
          - Tầng trong: ORDER BY id DESC ... LIMIT N -> lấy N dòng MỚI NHẤT (id lớn nhất trước).
          - Tầng ngoài: ORDER BY id ASC             -> đảo lại về thứ tự cũ -> mới, đúng thứ tự
            hội thoại để nạp cho model.
        Nếu chỉ có một tầng ASC + LIMIT thì sẽ lấy nhầm N dòng ĐẦU TIÊN (cũ nhất), không phải
        gần đây nhất. Còn nếu để nguyên thứ tự DESC thì model đọc hội thoại ngược dòng thời gian.
        Nhân đôi (history_turns * 2) vì mỗi lượt gồm hai dòng: một của sinh viên, một của model.
        Lọc theo cả session_id lẫn student_id (xem docstring của class) để không lỡ đọc nhầm
        hội thoại của người khác.
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

        # Chuyển mỗi dòng thành một Content của Gemini: role là "user" (sinh viên) hoặc "model".
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
