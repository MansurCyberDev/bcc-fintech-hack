"""Этап 1. Чтение документа: .md / .txt / .pdf / .docx -> Document (текст по строкам)."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from .models import Document

log = logging.getLogger(__name__)

SUPPORTED = {".md", ".markdown", ".txt", ".pdf", ".docx"}
MAX_BYTES = 20 * 1024 * 1024  # 20 МБ — защита от случайно огромных файлов


class IngestError(Exception):
    """Понятная пользователю ошибка чтения документа."""


def load_document(path: str | Path) -> Document:
    p = Path(path)
    if not p.exists():
        raise IngestError(f"Файл не найден: {p}")
    if not p.is_file():
        raise IngestError(f"Это не файл: {p}")
    ext = p.suffix.lower()
    if ext not in SUPPORTED:
        raise IngestError(
            f"Формат {ext or '(без расширения)'} не поддерживается. "
            f"Поддерживаются: {', '.join(sorted(SUPPORTED))}"
        )
    if p.stat().st_size > MAX_BYTES:
        raise IngestError(f"Файл слишком большой (> {MAX_BYTES // 1024 // 1024} МБ)")

    if ext in {".md", ".markdown", ".txt"}:
        text = _read_text(p)
        fmt = "markdown" if ext != ".txt" else "text"
    elif ext == ".pdf":
        text = _read_pdf(p)
        fmt = "pdf"
    else:
        text = _read_docx(p)
        fmt = "docx"

    text = _normalize(text)
    if not text.strip():
        raise IngestError(
            "В документе не найден текст. Если это скан PDF, его нужно сначала распознать (OCR)."
        )
    doc = Document(path=str(p), name=p.name, fmt=fmt, text=text, title=_guess_title(text, p))
    log.info("Прочитан документ %s (%s): %d строк", p.name, fmt, len(doc.lines))
    return doc


def _read_text(p: Path) -> str:
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _read_pdf(p: Path) -> str:
    try:
        import pdfplumber
    except ImportError as e:  # pragma: no cover
        raise IngestError("Для PDF нужен пакет pdfplumber: pip install -r requirements.txt") from e
    parts: list[str] = []
    try:
        with pdfplumber.open(str(p)) as pdf:
            for page in pdf.pages:
                parts.append(page.extract_text() or "")
    except Exception as e:  # битый или зашифрованный PDF
        raise IngestError(f"Не удалось прочитать PDF: {e}") from e
    return "\n".join(parts)


def _read_docx(p: Path) -> str:
    try:
        import docx  # python-docx
    except ImportError as e:  # pragma: no cover
        raise IngestError("Для DOCX нужен пакет python-docx: pip install -r requirements.txt") from e
    try:
        d = docx.Document(str(p))
    except Exception as e:
        raise IngestError(f"Не удалось прочитать DOCX: {e}") from e

    # Идём по телу документа по порядку: абзацы и таблицы.
    out: list[str] = []
    body = d.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            para = docx.text.paragraph.Paragraph(child, d)
            text = para.text.strip()
            style = (para.style.name or "").lower() if para.style is not None else ""
            if not text:
                out.append("")
                continue
            m = re.search(r"heading\s*(\d)|заголовок\s*(\d)", style)
            if m:
                level = int(m.group(1) or m.group(2))
                out.append("#" * min(level, 6) + " " + text)
            elif "list" in style or "спис" in style:
                out.append("* " + text)
            else:
                out.append(text)
            out.append("")
        elif tag == "tbl":
            table = docx.table.Table(child, d)
            rows = []
            for r in table.rows:
                cells = [c.text.replace("\n", " ").replace("|", "/").strip() for c in r.cells]
                rows.append("| " + " | ".join(cells) + " |")
            if rows:
                ncols = rows[0].count("|") - 1
                rows.insert(1, "|" + " --- |" * ncols)
                out.extend(rows)
                out.append("")
    return "\n".join(out)


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = text.replace("\t", "    ")
    # убираем хвостовые пробелы, но сохраняем нумерацию строк
    return "\n".join(line.rstrip() for line in text.split("\n"))


def _guess_title(text: str, p: Path) -> str:
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    for line in text.split("\n"):
        if line.strip():
            return line.strip()[:120]
    return p.stem
