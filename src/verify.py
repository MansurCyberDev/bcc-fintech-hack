"""Этап проверки цитат. Каждое замечание должно ссылаться на реальное место документа.

LLM может ошибиться или «додумать» цитату. Здесь код (не модель) ищет цитату
в тексте документа. Найдена — ставим номер строки. Не найдена даже приблизительно —
замечание отбрасывается и учитывается в паспорте прогона.
"""
from __future__ import annotations

import bisect
import difflib
import re

from .models import Document, Issue, Quote

_TRANS = str.maketrans({
    "«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "'": '"', "’": '"',
    "–": "-", "—": "-", "‑": "-", "−": "-", "ё": "е", "Ё": "Е", " ": " ",
})


def normalize(text: str) -> str:
    t = text.translate(_TRANS)
    t = re.sub(r"\*\*|__|`|\|", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip().lower()


class QuoteIndex:
    """Нормализованный текст документа + отображение позиции в номер строки."""

    def __init__(self, doc: Document):
        self.doc = doc
        self.lines = doc.lines
        parts, starts, pos = [], [], 0
        for line in self.lines:
            n = normalize(line)
            starts.append(pos)
            parts.append(n)
            pos += len(n) + 1
        self.starts = starts
        self.norm_lines = parts
        self.text = " ".join(parts)

    def line_at(self, pos: int) -> int:
        return bisect.bisect_right(self.starts, pos)  # номера строк с 1

    def find(self, quote: str) -> tuple[int | None, str]:
        """Возвращает (номер строки, дословный текст) или (None, quote)."""
        q = normalize(quote).strip(' ."')
        if len(q) < 3:
            return None, quote
        idx = self.text.find(q)
        if idx >= 0:
            line = self.line_at(idx)
            return line, quote.strip()
        # LLM часто сокращает цитату через «...» — проверяем части по отдельности
        if "..." in q or "…" in q:
            pieces = [p.strip() for p in re.split(r"\.\.\.|…", q) if len(p.strip()) >= 6]
            if pieces and all(p in self.text for p in pieces):
                return self.line_at(self.text.find(pieces[0])), quote.strip()
        return self._fuzzy(q, quote)

    def _fuzzy(self, q: str, original: str) -> tuple[int | None, str]:
        best, best_line, best_text = 0.0, None, ""
        for i, nl in enumerate(self.norm_lines):
            if not nl:
                continue
            # окно: строка и строка+следующая (цитата может переходить на новую строку)
            cands = [nl]
            if i + 1 < len(self.norm_lines) and self.norm_lines[i + 1]:
                cands.append(nl + " " + self.norm_lines[i + 1])
            for c in cands:
                if len(c) >= len(q):
                    sm = difflib.SequenceMatcher(None, q, c, autojunk=False)
                    m = sm.find_longest_match(0, len(q), 0, len(c))
                    # насколько цитата целиком похожа на фрагмент строки той же длины
                    a = max(0, m.b - m.a)
                    frag = c[a:a + len(q)]
                    r = difflib.SequenceMatcher(None, q, frag, autojunk=False).ratio()
                else:
                    r = difflib.SequenceMatcher(None, q, c, autojunk=False).ratio()
                if r > best:
                    best, best_line, best_text = r, i + 1, self.lines[i].strip()
        if best >= 0.86:
            return best_line, _clean(best_text)
        return None, original


def _clean(line: str) -> str:
    t = re.sub(r"\*\*|__|`", "", line)
    t = re.sub(r"^\s*[*\-+]\s+", "", t)
    return " ".join(t.split())


def verify_issues(issues: list[Issue], doc: Document) -> tuple[list[Issue], int]:
    """Оставляет только замечания, у которых подтверждена хотя бы одна цитата.

    Неподтверждённые цитаты внутри замечания удаляются. Возвращает
    (подтверждённые замечания, число отброшенных замечаний).
    """
    index = QuoteIndex(doc)
    kept: list[Issue] = []
    dropped = 0
    for issue in issues:
        good: list[Quote] = []
        for q in issue.quotes:
            if not q.text or not q.text.strip():
                continue
            line, text = index.find(q.text)
            if line is not None:
                good.append(Quote(text=text, line=line, verified=True))
        # дубликаты цитат убираем
        uniq, seen = [], set()
        for q in good:
            key = (q.line, normalize(q.text))
            if key not in seen:
                seen.add(key)
                uniq.append(q)
        if uniq:
            issue.quotes = uniq
            kept.append(issue)
        else:
            dropped += 1
    return kept, dropped
