"""Vector search over the chunks stored in PostgreSQL.

Tìm kiếm vector trên các đoạn tài liệu lưu trong PostgreSQL.
"""

from dataclasses import dataclass

import numpy as np

from app.db import get_connection
from app.llm.gemini import GeminiClient


@dataclass
class RetrievedChunk:
    content: str
    title: str
    source: str
    score: float


class Retriever:
    """Loads every chunk embedding into memory once, then scores queries with numpy.

    Nạp toàn bộ embedding vào bộ nhớ một lần, sau đó chấm điểm truy vấn bằng numpy.

    The knowledge base here is a few hundred chunks, so a full scan costs well under a
    millisecond and an approximate index (pgvector, Milvus) would add operational cost
    without buying any latency. That trade-off changes above roughly 100k chunks.
    Kho tri thức ở đây chỉ vài trăm đoạn, nên quét toàn bộ mất chưa tới một phần nghìn
    giây; dùng chỉ mục xấp xỉ (pgvector, Milvus) chỉ thêm chi phí vận hành mà không
    giảm được độ trễ. Đánh đổi này sẽ khác đi khi vượt khoảng 100 nghìn đoạn.
    """

    def __init__(self, client: GeminiClient) -> None:
        self._client = client
        self._matrix: np.ndarray | None = None
        self._rows: list[dict] = []

    def load(self) -> int:
        """Read all chunks from the database into memory. Returns how many were loaded.

        Đọc toàn bộ đoạn tài liệu từ database vào bộ nhớ. Trả về số đoạn đã nạp.
        """
        with get_connection() as conn:
            # Lấy tất cả các đoạn (chunks) kèm vector embedding của chúng, JOIN sang documents
            # để biết mỗi đoạn thuộc tài liệu nào (tiêu đề, nguồn) - phục vụ việc trích nguồn.
            rows = conn.execute(
                """
                SELECT c.content, c.embedding, d.title, d.source
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                ORDER BY c.id
                """
            ).fetchall()

        self._rows = rows
        if rows:
            # Xếp chồng tất cả embedding thành một ma trận (số_đoạn x số_chiều). Nhờ vậy lúc tìm
            # kiếm chỉ cần một phép nhân ma trận là chấm điểm được với toàn bộ kho cùng một lúc.
            self._matrix = np.asarray([row["embedding"] for row in rows], dtype=np.float64)
        else:
            self._matrix = None
        return len(rows)

    def search(self, query: str, *, top_k: int) -> list[RetrievedChunk]:
        """Return the top_k chunks most similar to the query.

        Trả về top_k đoạn tài liệu giống truy vấn nhất.
        """
        if self._matrix is None:
            self.load()
        if self._matrix is None or not self._rows:
            return []

        query_vector = self._client.embed([query], is_query=True)[0]
        # Both sides are L2-normalised, so the dot product is the cosine similarity.
        # Cả hai vế đều đã chuẩn hóa L2, nên tích vô hướng chính là độ tương đồng cosine.
        # scores là mảng 1 chiều: mỗi phần tử là điểm giống nhau giữa truy vấn và một đoạn.
        scores = self._matrix @ query_vector

        # Chọn top_k đoạn điểm cao nhất bằng HAI bước, tránh sắp xếp toàn bộ mảng cho tốn kém:
        #   1. argpartition(-scores, top_k - 1)[:top_k]: đưa top_k phần tử lớn nhất lên đầu mảng
        #      (chỉ trả về CHỈ SỐ của chúng), nhưng chưa sắp xếp nội bộ. Dùng -scores vì argpartition
        #      làm việc theo thứ tự tăng dần, đảo dấu để "lớn nhất" thành "nhỏ nhất".
        #   2. argsort(-scores[best]): sắp xếp đúng top_k chỉ số đó theo điểm giảm dần, để kết quả
        #      trả về đúng thứ tự từ giống nhất tới ít giống hơn.
        # Cách này nhanh hơn np.argsort cả mảng khi kho lớn: chỉ sắp xếp top_k thay vì tất cả.
        top_k = min(top_k, len(self._rows))
        best = np.argpartition(-scores, top_k - 1)[:top_k]
        best = best[np.argsort(-scores[best])]

        return [
            RetrievedChunk(
                content=self._rows[i]["content"],
                title=self._rows[i]["title"],
                source=self._rows[i]["source"],
                score=float(scores[i]),
            )
            for i in best
        ]
