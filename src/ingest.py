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
        fmt = "pdf"  # текст восстановлен в markdown-подобную разметку
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
    """PDF -> markdown-подобный текст: заголовки по размеру шрифта, таблицы -> | ... |.

    Структура нужна нарезчику: без заголовков нельзя понять, в каком разделе требование.
    """
    try:
        import pdfplumber
    except ImportError as e:  # pragma: no cover
        raise IngestError("Для PDF нужен пакет pdfplumber: pip install -r requirements.txt") from e
    try:
        with pdfplumber.open(str(p)) as pdf:
            if not pdf.pages:
                return ""
            pages = [_pdf_page_items(page) for page in pdf.pages]
    except IngestError:
        raise
    except Exception as e:  # битый или зашифрованный PDF
        raise IngestError(f"Не удалось прочитать PDF: {e}") from e
    return _pdf_to_markdown(pages)


def _pdf_page_items(page) -> list[dict]:
    """Строки текста (с размером шрифта) и таблицы страницы в порядке сверху вниз."""
    items: list[dict] = []
    try:
        tables = page.find_tables()
    except Exception:
        tables = []
    boxes = []
    for t in tables:
        rows = [[(c or "").replace("\n", " ").replace("|", "/").strip() for c in r] for r in (t.extract() or [])]
        data = [r for r in rows[1:] if any(r)]
        rows = rows[:1] + (data or rows[1:2])  # пустую таблицу (например, согласующих) сохраняем
        if rows and max(len(r) for r in rows) >= 2:
            boxes.append(t.bbox)
            items.append({"top": t.bbox[1], "kind": "table", "rows": rows})

    def inside(w):
        return any(b[0] - 1 <= w["x0"] and w["x1"] <= b[2] + 1 and b[1] - 1 <= w["top"] and w["bottom"] <= b[3] + 1
                   for b in boxes)

    words = [w for w in page.extract_words(extra_attrs=["size", "fontname"], use_text_flow=True) if not inside(w)]
    lines: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= 2.5:
            lines[-1].append(w)
        else:
            lines.append([w])
    for ln in lines:
        ln.sort(key=lambda w: w["x0"])
        text = " ".join(w["text"] for w in ln)
        size = max(w["size"] for w in ln)
        bold = all("bold" in (w.get("fontname") or "").lower() for w in ln)
        items.append({"top": ln[0]["top"], "bottom": max(w["bottom"] for w in ln), "kind": "line",
                      "text": text, "size": round(size, 1), "bold": bold, "x0": ln[0]["x0"]})
    items.sort(key=lambda i: i["top"])
    return items


def _pdf_to_markdown(pages: list[list[dict]]) -> str:
    from collections import Counter
    sizes = Counter()
    for items in pages:
        for i in items:
            if i["kind"] == "line":
                sizes[i["size"]] += len(i["text"])
    if not sizes:
        body = 10.0
    else:
        body = sizes.most_common(1)[0][0]
    bigger = sorted({s for s in sizes if s >= body * 1.15}, reverse=True)
    level_of = {s: min(n + 1, 5) for n, s in enumerate(bigger)}
    bold_level = min(len(bigger) + 1, 6)

    out: list[str] = []
    prev_bottom, prev_size = None, None
    for items in pages:
        prev_bottom = None
        for it in items:
            if it["kind"] == "table":
                rows = it["rows"]
                n = max(len(r) for r in rows)
                rows = [r + [""] * (n - len(r)) for r in rows]
                head = "| " + " | ".join(rows[0]) + " |"
                # продолжение таблицы на следующей странице (тот же заголовок) — склеиваем
                k = len(out) - 1
                while k >= 0 and not out[k]:
                    k -= 1
                if k >= 0 and out[k].startswith("|"):
                    j = k
                    while j >= 0 and out[j].startswith("|"):
                        j -= 1
                    if out[j + 1] == head:
                        del out[k + 1:]
                        out.extend("| " + " | ".join(r) + " |" for r in rows[1:])
                        out.append("")
                        continue
                out.append("")
                out.append("| " + " | ".join(rows[0]) + " |")
                out.append("|" + " --- |" * n)
                for r in rows[1:]:
                    out.append("| " + " | ".join(r) + " |")
                out.append("")
                prev_bottom = None
                continue
            text = it["text"].strip()
            if not text:
                continue
            short = len(text) < 90 and not text.endswith((".", ",", ";", ":"))
            if it["size"] in level_of and short:
                out += ["", "#" * level_of[it["size"]] + " " + text, ""]
                prev_bottom = None
                continue
            if it["bold"] and short and len(text.split()) <= 10:
                out += ["", "#" * bold_level + " " + text, ""]
                prev_bottom = None
                continue
            gap = (it["top"] - prev_bottom) if prev_bottom is not None else 999
            new_unit = (gap > it["size"] * 0.9 or re.match(r"^\s*([•\-*–]|\d+[.)]|[A-Za-zА-Яа-яЁё]{1,8}[-‑–]?\d+\.)\s", text)
                        or prev_size != it["size"])
            if new_unit or not out or not out[-1]:
                if out and out[-1]:
                    out.append("")
                out.append(re.sub(r"^•\s*", "* ", text))
            else:
                out[-1] = out[-1] + " " + text
            prev_bottom, prev_size = it["bottom"], it["size"]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


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
