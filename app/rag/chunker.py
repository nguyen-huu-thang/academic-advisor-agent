"""Split a markdown document into retrievable chunks.

Cắt một tài liệu markdown thành các đoạn để tìm kiếm.

Role in the RAG pipeline - INDEXING stage, step 2/7: Chunking.
It sits between the raw documents (step 1, data/documents/*.md) and embedding
(step 3, GeminiClient.embed). It turns a long .md file into short passages, each about
one topic, which is the unit that later gets embedded, stored and retrieved.
Called by scripts/ingest.py.
Vai trò trong luồng RAG - giai đoạn INDEXING (nạp kho, chạy offline), bước 2/7: Cắt đoạn.
Đứng giữa tài liệu thô (bước 1, data/documents/*.md) và sinh embedding (bước 3,
GeminiClient.embed). Nó biến một file .md dài thành các đoạn ngắn, mỗi đoạn một chủ đề;
đoạn chính là đơn vị sau đó được đem đi embedding, lưu trữ rồi truy hồi.
Được gọi bởi scripts/ingest.py.
"""

import re

# The source documents are strongly sectioned, so splitting on headings keeps each chunk
# about exactly one topic (one rule, one procedure) instead of cutting mid-sentence.
# Tài liệu nguồn chia mục rất rõ, nên cắt theo tiêu đề giúp mỗi đoạn nói về đúng
# một chủ đề (một quy định, một quy trình) thay vì cắt giữa câu.
HEADING_PATTERN = re.compile(r"^#{2,3}\s+(.+)$", re.MULTILINE)

MAX_CHUNK_CHARS = 1200


def chunk_markdown(text: str, *, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split on level 2 and 3 headings, then hard-split anything still too long.

    Cắt theo tiêu đề cấp 2 và 3, sau đó cắt cứng những đoạn văn còn quá dài.
    """
    matches = list(HEADING_PATTERN.finditer(text))

    sections: list[str] = []
    if not matches:
        sections = [text]
    else:
        # Text before the first heading (title, introduction) is kept as its own section.
        # Phần văn bản trước tiêu đề đầu tiên (tựa đề, mở đầu) được giữ thành một mục riêng.
        preamble = text[: matches[0].start()].strip()
        if preamble:
            sections.append(preamble)
        # Each section runs from its heading to the start of the next heading (or end of file).
        # Mỗi mục chạy từ tiêu đề của nó tới đầu tiêu đề kế tiếp (hoặc hết file).
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            sections.append(text[match.start() : end].strip())

    chunks: list[str] = []
    for section in sections:
        if not section:
            continue
        if len(section) <= max_chars:
            chunks.append(section)
        else:
            chunks.extend(_split_by_paragraph(section, max_chars))
    return chunks


def _split_by_paragraph(section: str, max_chars: int) -> list[str]:
    """Pack paragraphs into pieces no larger than max_chars.

    Gom các đoạn văn thành các mảnh không vượt quá max_chars.
    """
    paragraphs = [p.strip() for p in section.split("\n\n") if p.strip()]
    pieces: list[str] = []
    current = ""

    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            pieces.append(current)
        # A single paragraph longer than the limit is emitted on its own rather than
        # being cut mid-sentence, which would damage its meaning.
        # Một đoạn văn dài hơn giới hạn sẽ được giữ nguyên thay vì cắt giữa câu,
        # vì cắt giữa câu sẽ làm hỏng ý nghĩa của nó.
        current = paragraph

    if current:
        pieces.append(current)
    return pieces
