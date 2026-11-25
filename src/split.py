"""Этап 2. Разбиение документа на отдельные требования.

Работает без LLM, поэтому нарезка воспроизводима и у каждого требования
есть точное место в документе (номера строк). Логика общая для любых
документов: разделы по заголовкам, пункты списков, абзацы и строки таблиц.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Document, Requirement

# Явные идентификаторы требований: Ф-1, FR-01, НФТ-2, REQ-12, BR.3, US-7 ...
ID_RE = re.compile(
    r"^\s*(?:[*\-+]\s+)?(?:\*\*|__)?\s*"
    r"(?P<id>[A-Za-zА-Яа-яЁё]{1,8}[-‑–._]?\d+(?:\.\d+)*)"
    r"\s*[.):]?\s*(?:\*\*|__)?[.):]?\s+"
)
NUM_ITEM_RE = re.compile(r"^\s*(\d+)[.)]\s+")
BULLET_RE = re.compile(r"^\s*[*\-+•]\s+")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")

# Разделы, которые описывают документ, а не систему (их не проверяем как требования,
# но они участвуют в проверке набора — версия, согласования и т.п.)
CONTEXT_SECTIONS = [
    "общие сведения", "general information", "ревью требований", "чек-лист", "checklist",
    "согласующ", "approv", "лист согласования", "история изменений", "revision history",
    "change log", "содержание", "оглавление", "table of contents", "глоссарий", "glossary",
    "термины", "сокращения",
]

CATEGORY_RULES: list[tuple[str, str, str]] = [
    # (ключевое слово в пути раздела, категория, вид)
    ("критери", "Критерий приёмки", "acceptance"),
    ("acceptance", "Критерий приёмки", "acceptance"),
    ("нефункц", "Нефункциональное", "requirement"),
    ("non-functional", "Нефункциональное", "requirement"),
    ("функциональн", "Функциональное", "requirement"),
    ("functional", "Функциональное", "requirement"),
    ("данн", "Данные", "requirement"),
    ("data", "Данные", "requirement"),
    ("интеграц", "Интеграция", "requirement"),
    ("integration", "Интеграция", "requirement"),
    ("тестир", "Тестирование", "requirement"),
    ("sequence", "Взаимодействие систем", "requirement"),
    ("дизайн", "UI/UX", "requirement"),
    ("интерфейс", "UI/UX", "requirement"),
    ("ui", "UI/UX", "requirement"),
    ("bpmn", "Бизнес-процесс", "requirement"),
    ("процесс", "Бизнес-процесс", "requirement"),
    ("архитектур", "Архитектура", "requirement"),
    ("цел", "Бизнес-цель", "requirement"),
    ("goal", "Бизнес-цель", "requirement"),
    ("безопасн", "Безопасность", "requirement"),
    ("security", "Безопасность", "requirement"),
    ("api", "API", "api"),
    ("rest", "API", "api"),
    ("метод", "API", "api"),
    ("запрос", "API", "api"),
    ("ответ", "API", "api"),
    ("статус", "API", "api"),
    ("параметр", "API", "api"),
    ("правил", "Бизнес-правило", "requirement"),
]


@dataclass
class Block:
    kind: str                 # para | item | table | code
    lines: list[str]
    start: int                # номер первой строки (с 1)
    end: int
    section: list[str] = field(default_factory=list)
    num: str = ""             # номер пункта нумерованного списка
    table_header: list[str] = field(default_factory=list)


def strip_md(text: str) -> str:
    t = re.sub(r"\*\*|__|`", "", text)
    t = re.sub(r"^\s*[*\-+•]\s+", "", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    return " ".join(t.split())


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [strip_md(c) for c in s.split("|")]


def parse_blocks(doc: Document) -> list[Block]:
    """Разбирает markdown/текст на блоки с номерами строк и путём разделов."""
    lines = doc.lines
    is_md = doc.fmt in ("markdown", "docx") or any(HEADING_RE.match(l) for l in lines)
    blocks: list[Block] = []
    section: list[tuple[int, str]] = []  # (уровень, заголовок)
    i = 0
    n = len(lines)

    def sec() -> list[str]:
        return [t for _, t in section]

    while i < n:
        line = lines[i]
        s = line.strip()
        if not s or s == "---" or s == "***":
            i += 1
            continue

        m = HEADING_RE.match(s) if is_md else None
        if m:
            level = len(m.group(1))
            title = strip_md(m.group(2))
            section = [(lv, t) for lv, t in section if lv < level]
            section.append((level, title))
            i += 1
            continue

        if s.startswith("```"):
            start = i
            i += 1
            body = []
            while i < n and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            blocks.append(Block("code", body, start + 1, min(i + 1, n), sec()))
            i += 1
            continue

        if s.startswith("|"):
            start = i
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append((i + 1, lines[i]))
                i += 1
            header = _cells(rows[0][1]) if rows else []
            data = [(ln, r) for ln, r in rows[1:] if not TABLE_SEP_RE.match(r)]
            if len(header) <= 2:
                # таблица «поле — значение»: весь блок как одна единица
                blocks.append(Block("table", [r for _, r in rows], start + 1, i, sec(), table_header=header))
            else:
                for ln, r in data:
                    blocks.append(Block("table", [r], ln, ln, sec(), table_header=header))
            continue

        if BULLET_RE.match(line) or NUM_ITEM_RE.match(line) or ID_RE.match(line):
            start = i
            num_m = NUM_ITEM_RE.match(line)
            item = [line]
            i += 1
            # продолжение пункта: строки с отступом, не начинающие новый пункт
            while i < n and lines[i].strip() and not (
                BULLET_RE.match(lines[i]) or NUM_ITEM_RE.match(lines[i])
                or lines[i].strip().startswith(("|", "#", "```"))
            ) and (lines[i].startswith((" ", "\t")) or not is_md):
                if not is_md and _looks_like_new_unit(lines[i], item[-1]):
                    break
                item.append(lines[i])
                i += 1
            blocks.append(Block("item", item, start + 1, i, sec(), num=num_m.group(1) if num_m else ""))
            continue

        # обычный абзац; жёсткий перенос markdown (два пробела в конце) делит на части
        start = i
        para = [line]
        i += 1
        while i < n and lines[i].strip() and not (
            lines[i].strip().startswith(("|", "```", "#", "**")) or BULLET_RE.match(lines[i])
            or NUM_ITEM_RE.match(lines[i]) or ID_RE.match(lines[i])
        ):
            if not is_md and _looks_like_new_unit(lines[i], para[-1]):
                break
            para.append(lines[i])
            i += 1
        _emit_paragraph(blocks, para, start, sec(), doc)
    return blocks


def _looks_like_new_unit(line: str, prev: str) -> bool:
    """Для текста из PDF: новая единица, если предыдущая строка закончила предложение."""
    return prev.rstrip().endswith((".", ":", ";", "!", "?")) and line[:1].isupper()


def _emit_paragraph(blocks: list[Block], para: list[str], start: int, section: list[str], doc: Document) -> None:
    raw = doc.lines
    chunk: list[str] = []
    chunk_start = start
    for k, l in enumerate(para):
        chunk.append(l)
        hard_break = raw[start + k].endswith("  ") if start + k < len(raw) else False
        if hard_break:
            blocks.append(Block("para", chunk, chunk_start + 1, start + k + 1, section))
            chunk, chunk_start = [], start + k + 1
    if chunk:
        blocks.append(Block("para", chunk, chunk_start + 1, start + len(para), section))


def _classify(path: list[str]) -> tuple[str, str]:
    if len(path) <= 1:
        return "Контекст", "context"  # вводный текст под заголовком документа
    joined = " > ".join(path).lower()
    for kw in CONTEXT_SECTIONS:
        if kw in joined.split(" > ")[-1] if path else False:
            return "Контекст", "context"
    # ищем от самого вложенного раздела к корню
    for title in reversed(path):
        t = title.lower()
        for kw, cat, kind in CATEGORY_RULES:
            if re.search(r"(?<![a-zа-яё])" + re.escape(kw), t):
                return cat, kind
    return "Прочее", "requirement"


def _block_text(b: Block) -> str:
    if b.kind == "code":
        return "\n".join(b.lines).strip()
    if b.kind == "table":
        if len(b.table_header) <= 2:
            out = []
            for r in b.lines[1:]:
                if TABLE_SEP_RE.match(r):
                    continue
                c = _cells(r)
                out.append(": ".join(x for x in c if x))
            return "; ".join(out)
        c = _cells(b.lines[0])
        pairs = [f"{h}: {v}" for h, v in zip(b.table_header, c) if v]
        return "; ".join(pairs)
    return strip_md(" ".join(l.strip() for l in b.lines))


def split_requirements(doc: Document) -> list[Requirement]:
    blocks = parse_blocks(doc)

    # 1) Склейка: код и очень короткие фрагменты («или», «**404**») присоединяем
    #    к предыдущей единице того же раздела.
    merged: list[Block] = []
    for b in blocks:
        text = _block_text(b)
        short = b.kind in ("para",) and len(text.split()) <= 2
        if (b.kind == "code" or short) and merged and merged[-1].section == b.section:
            prev = merged[-1]
            prev.lines = prev.lines + (["```"] + b.lines + ["```"] if b.kind == "code" else b.lines)
            prev.end = max(prev.end, b.end)
            if prev.kind == "table" and len(prev.table_header) > 2:
                prev.kind = "para"
            continue
        if b.kind == "code":
            title = b.section[-1] if b.section else "Фрагмент"
            start = b.start
            for j in range(b.start - 1, 0, -1):  # захватываем строку заголовка раздела
                lj = doc.lines[j - 1].strip()
                if lj.startswith("#"):
                    start = j
                    break
                if lj:
                    break
            b = Block("para", [title + ":", "```"] + b.lines + ["```"], start, b.end, b.section)
        merged.append(b)

    # Раздел вида «200. Successful» без собственного текста: заголовок — часть требования
    seen_sections: set[tuple] = set()
    for b in merged:
        key = tuple(b.section)
        if key in seen_sections:
            continue
        seen_sections.add(key)
        if b.section and re.match(r"[1-5]\d\d\b", b.section[-1]):
            for j in range(b.start - 1, 0, -1):
                lj = doc.lines[j - 1].strip()
                if lj.startswith("#"):
                    if not b.lines or not b.lines[0].startswith(b.section[-1]):
                        b.start = j
                        b.lines = [b.section[-1] + ":"] + b.lines
                    break
                if lj and j < b.start - 1:
                    break

    reqs: list[Requirement] = []
    counters: dict[str, int] = {}
    for b in merged:
        text = _block_text(b) if b.kind != "para" or "```" not in "\n".join(b.lines) else _para_with_code(b)
        if not text or not re.search(r"[A-Za-zА-Яа-яЁё0-9]", text):
            continue
        category, kind = _classify(b.section)
        first = b.lines[0] if b.lines else ""
        m = ID_RE.match(first) if b.kind in ("item", "para") else None
        if m and not NUM_ITEM_RE.match(first):
            doc_id = m.group("id")
            text = strip_md(ID_RE.sub("", first, count=1) + " " + " ".join(l.strip() for l in b.lines[1:]))
            if kind == "context":
                kind = "requirement"
        elif b.num and kind == "api" and re.fullmatch(r"[1-5]\d\d", b.num):
            doc_id = f"статус {b.num}"
        elif b.num:
            doc_id = f"п. {b.num} «{_short_section(b.section)}»"
            text = re.sub(r"^\d+[.)]\s*", "", text)
        elif b.kind == "table" and len(b.table_header) > 2:
            key = _cells(b.lines[0])[0] if b.lines else ""
            counters[_short_section(b.section)] = counters.get(_short_section(b.section), 0) + 1
            if kind == "acceptance":
                doc_id = f"строка {counters[_short_section(b.section)]} «{_short_section(b.section)}»"
            else:
                doc_id = f"«{key}» в «{_short_section(b.section)}»" if key else "—"
        else:
            doc_id = "—"
            code = re.match(r"\s*(?:#{1,6}\s*|\*\*)?\s*([1-5]\d\d)\.", first)
            if kind == "api" and code:
                doc_id = f"статус {code.group(1)}"
        reqs.append(Requirement(
            rid="", doc_id=doc_id, text=text, section=" > ".join(b.section) or "(без раздела)",
            line_start=b.start, line_end=b.end, kind=kind, category=category,
        ))

    n = 0
    for r in reqs:
        if r.kind != "context":
            n += 1
            r.rid = f"R-{n:02d}"
    c = 0
    for r in reqs:
        if r.kind == "context":
            c += 1
            r.rid = f"C-{c:02d}"
    return reqs


def _para_with_code(b: Block) -> str:
    out, in_code = [], False
    for l in b.lines:
        if l.strip().startswith("```"):
            in_code = not in_code
            continue
        out.append(l.strip() if in_code else strip_md(l))
    return " ".join(x for x in out if x)


def _short_section(section: list[str]) -> str:
    return section[-1] if section else "документ"
