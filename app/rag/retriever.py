"""Vector search over the chunks stored in PostgreSQL.

Tim kiem vector tren cac doan tai lieu luu trong PostgreSQL.
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

    Nap toan bo embedding vao bo nho mot lan, sau do cham diem truy van bang numpy.

    The knowledge base here is a few hundred chunks, so a full scan costs well under a
    millisecond and an approximate index (pgvector, Milvus) would add operational cost
    without buying any latency. That trade-off changes above roughly 100k chunks.
    Kho tri thuc o day chi vai tram doan, nen quet toan bo mat chua toi mot phan nghin
    giay; dung chi muc xap xi (pgvector, Milvus) chi them chi phi van hanh ma khong
    giam duoc do tre. Danh doi nay se khac di khi vuot khoang 100 nghin doan.
    """

    def __init__(self, client: GeminiClient) -> None:
        self._client = client
        self._matrix: np.ndarray | None = None
        self._rows: list[dict] = []

    def load(self) -> int:
        """Read all chunks from the database into memory. Returns how many were loaded.

        Doc toan bo doan tai lieu tu database vao bo nho. Tra ve so doan da nap.
        """
        with get_connection() as conn:
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
            self._matrix = np.asarray([row["embedding"] for row in rows], dtype=np.float64)
        else:
            self._matrix = None
        return len(rows)

    def search(self, query: str, *, top_k: int) -> list[RetrievedChunk]:
        """Return the top_k chunks most similar to the query.

        Tra ve top_k doan tai lieu giong truy van nhat.
        """
        if self._matrix is None:
            self.load()
        if self._matrix is None or not self._rows:
            return []

        query_vector = self._client.embed([query], is_query=True)[0]
        # Both sides are L2-normalised, so the dot product is the cosine similarity.
        # Ca hai ve deu da chuan hoa L2, nen tich vo huong chinh la do tuong dong cosine.
        scores = self._matrix @ query_vector

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
