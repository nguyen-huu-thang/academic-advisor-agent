"""Split a markdown document into retrievable chunks.

Cat mot tai lieu markdown thanh cac doan de tim kiem.
"""

import re

# Bank documents are strongly sectioned, so splitting on headings keeps each chunk
# about exactly one topic (one fee, one product) instead of cutting mid-sentence.
# Tai lieu ngan hang chia muc rat ro, nen cat theo tieu de giup moi doan noi ve dung
# mot chu de (mot loai phi, mot san pham) thay vi cat giua cau.
HEADING_PATTERN = re.compile(r"^#{2,3}\s+(.+)$", re.MULTILINE)

MAX_CHUNK_CHARS = 1200


def chunk_markdown(text: str, *, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split on level 2 and 3 headings, then hard-split anything still too long.

    Cat theo tieu de cap 2 va 3, sau do cat cung nhung doan van con qua dai.
    """
    matches = list(HEADING_PATTERN.finditer(text))

    sections: list[str] = []
    if not matches:
        sections = [text]
    else:
        preamble = text[: matches[0].start()].strip()
        if preamble:
            sections.append(preamble)
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

    Gom cac doan van thanh cac manh khong vuot qua max_chars.
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
        # Mot doan van dai hon gioi han se duoc giu nguyen thay vi cat giua cau,
        # vi cat giua cau se lam hong y nghia cua no.
        current = paragraph

    if current:
        pieces.append(current)
    return pieces
