from __future__ import annotations

import json
import re
from pathlib import Path


TEXT_EXTENSIONS = {".txt", ".md", ".rst", ".log", ".json", ".py", ".csv"}


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in TEXT_EXTENSIONS:
        return _read_text_file(path, suffix)
    if suffix == ".pdf":
        return _read_pdf(path)
    if suffix == ".docx":
        return _read_docx(path)
    raise ValueError(f"Unsupported file type: {path.suffix or '<none>'}")


def build_chunks(
    source_name: str,
    text: str,
    chunk_size: int = 900,
    overlap: int = 120,
) -> list[dict[str, object]]:
    clean = _normalize_text(text)
    if not clean:
        return []

    chunks: list[dict[str, object]] = []
    start = 0
    index = 0

    while start < len(clean):
        end = min(len(clean), start + chunk_size)
        chunk_text = clean[start:end].strip()
        if chunk_text:
            chunks.append(
                {
                    "id": f"{source_name}:{index}",
                    "source": source_name,
                    "text": chunk_text,
                }
            )
            index += 1

        if end >= len(clean):
            break
        start = max(end - overlap, start + 1)

    return chunks


def _read_text_file(path: Path, suffix: str) -> str:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    if suffix != ".json":
        return raw

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return json.dumps(parsed, indent=2, ensure_ascii=False)


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF support requires the optional 'pypdf' dependency.") from exc

    reader = PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages)


def _read_docx(path: Path) -> str:
    try:
        from docx import Document
    except ImportError as exc:
        raise RuntimeError("DOCX support requires the optional 'python-docx' dependency.") from exc

    document = Document(str(path))
    paragraphs = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()]
    return "\n\n".join(paragraphs)


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()
