"""Index the university documents: chunk, embed, and store into PostgreSQL.

Nạp tài liệu của nhà trường vào kho tri thức: cắt đoạn, sinh embedding, lưu vào PostgreSQL.

Role in the RAG pipeline: this script IS the whole INDEXING stage (offline, run once each
time the documents change). It drives steps 1->4 in order: read the source files (step 1,
data/documents/*.md), chunk them (step 2, app/rag/chunker.py), embed each chunk (step 3,
GeminiClient.embed), and store documents + chunk vectors (step 4, tables in app/schema.sql).
Nothing here runs while serving a student; the online half starts at app/rag/retriever.py.
Vai trò trong luồng RAG: script này CHÍNH LÀ toàn bộ giai đoạn INDEXING (offline, chạy một
lần mỗi khi tài liệu thay đổi). Nó thực hiện tuần tự bước 1->4: đọc file nguồn (bước 1,
data/documents/*.md), cắt đoạn (bước 2, app/rag/chunker.py), sinh embedding cho từng đoạn
(bước 3, GeminiClient.embed), rồi lưu tài liệu + vector các đoạn (bước 4, các bảng trong
app/schema.sql). Không phần nào ở đây chạy lúc phục vụ sinh viên; nửa online bắt đầu ở
app/rag/retriever.py.

Chạy: python -m scripts.ingest
"""

import re
import time
from pathlib import Path

from app.config import load_settings
from app.db import close_pool, get_connection
from app.llm.gemini import GeminiClient
from app.rag.chunker import chunk_markdown

DOCUMENTS_DIR = Path(__file__).resolve().parent.parent / "data" / "documents"
FRONT_MATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split the YAML-style header from the document body.

    Tách phần tiêu đề kiểu YAML ra khỏi nội dung tài liệu.
    """
    match = FRONT_MATTER_PATTERN.match(text)
    if not match:
        return {}, text

    metadata: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            metadata[key.strip()] = value.strip()
    return metadata, text[match.end():]


def main() -> None:
    settings = load_settings()
    client = GeminiClient(settings)

    files = sorted(DOCUMENTS_DIR.glob("*.md"))
    if not files:
        print(f"Khong tim thay tai lieu nao trong {DOCUMENTS_DIR}")
        return

    started = time.perf_counter()
    total_chunks = 0

    with get_connection() as conn:
        # Reindexing replaces the knowledge base wholesale, so a document edited on disk
        # can never leave a stale copy of itself behind for the retriever to find.
        # Nạp lại sẽ thay thế toàn bộ kho tri thức, nên một tài liệu đã sửa trên đĩa
        # không thể để sót lại bản cũ cho bộ tìm kiếm đọc phải.
        # (Xóa documents sẽ kéo theo chunks nhờ ON DELETE CASCADE trong schema.)
        conn.execute("DELETE FROM documents")

        for path in files:
            metadata, body = parse_front_matter(path.read_text(encoding="utf-8"))
            title = metadata.get("title", path.stem)
            category = metadata.get("category", "khac")
            source = metadata.get("source", title)

            chunks = chunk_markdown(body)
            if not chunks:
                print(f"Bo qua {path.name}: khong cat duoc doan nao.")
                continue

            embeddings = client.embed(chunks, is_query=False)

            row = conn.execute(
                "INSERT INTO documents (title, category, source) VALUES (%s, %s, %s) RETURNING id",
                (title, category, source),
            ).fetchone()
            document_id = row["id"]

            # executemany: insert every chunk of this document in one round trip.
            # executemany: chèn tất cả các đoạn của tài liệu này trong một lượt gọi.
            conn.cursor().executemany(
                """
                INSERT INTO chunks (document_id, ordinal, content, embedding)
                VALUES (%s, %s, %s, %s)
                """,
                [
                    (document_id, ordinal, content, embeddings[ordinal].tolist())
                    for ordinal, content in enumerate(chunks)
                ],
            )

            total_chunks += len(chunks)
            print(f"  {path.name}: {len(chunks)} doan")

    elapsed = time.perf_counter() - started
    print(
        f"\nDa nap {len(files)} tai lieu, {total_chunks} doan, "
        f"embedding {settings.embedding_dim} chieu, mat {elapsed:.1f}s."
    )
    close_pool()


if __name__ == "__main__":
    main()
