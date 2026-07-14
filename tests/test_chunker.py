"""Tests for splitting bank documents into retrievable chunks.

Kiem thu viec cat tai lieu ngan hang thanh cac doan de tim kiem.
"""

from app.rag.chunker import chunk_markdown


def test_splits_on_headings():
    text = "# Tieu de\n\nMo dau.\n\n## Phi A\n\nNoi dung A.\n\n## Phi B\n\nNoi dung B.\n"
    chunks = chunk_markdown(text)

    assert len(chunks) == 3
    assert chunks[0].startswith("# Tieu de")
    assert "Phi A" in chunks[1]
    assert "Phi B" in chunks[2]


def test_each_chunk_keeps_its_heading():
    """A chunk without its heading loses the context that makes it findable.

    Mot doan bi cat mat tieu de se mat luon ngu canh giup no duoc tim thay.
    """
    text = "## Phi chuyen tien\n\nMien phi hoan toan.\n\n## Phi the\n\n66.000 VND mot nam.\n"
    chunks = chunk_markdown(text)

    assert all(chunk.startswith("##") for chunk in chunks)


def test_long_section_is_split_further():
    long_paragraph = "Noi dung rat dai. " * 60
    text = f"## Muc dai\n\n{long_paragraph}\n\n{long_paragraph}\n"
    chunks = chunk_markdown(text, max_chars=500)

    assert len(chunks) > 1
    assert all(len(chunk) <= 1200 for chunk in chunks)


def test_document_without_headings_returns_one_chunk():
    chunks = chunk_markdown("Chi la mot doan van ngan, khong co tieu de nao.")
    assert len(chunks) == 1
