"""Модели данных агента. Все этапы пайплайна обмениваются этими объектами."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Характеристики качества из ISO/IEC/IEEE 29148:2018 (5.2.5 — отдельное требование,
# 5.2.6 — набор требований) и IEEE Std 830-1998 (4.3).
# Ключ -> (название по-русски, источник)
CRITERIA: dict[str, tuple[str, str]] = {
    # 29148, 5.2.5 — отдельное требование
    "necessary": ("Необходимость", "ISO 29148 5.2.5 a"),
    "appropriate": ("Уместность уровня (нет лишних проектных решений)", "ISO 29148 5.2.5 b"),
    "unambiguous": ("Однозначность", "ISO 29148 5.2.5 c; IEEE 830 4.3.2"),
    "complete": ("Полнота", "ISO 29148 5.2.5 d; IEEE 830 4.3.3"),
    "singular": ("Атомарность", "ISO 29148 5.2.5 e"),
    "feasible": ("Реализуемость", "ISO 29148 5.2.5 f"),
    "verifiable": ("Проверяемость", "ISO 29148 5.2.5 g; IEEE 830 4.3.6"),
    "correct": ("Корректность", "ISO 29148 5.2.5 h; IEEE 830 4.3.1"),
    "conforming": ("Соответствие шаблону/стандарту", "ISO 29148 5.2.5 i"),
    # 29148, 5.2.6 — набор требований
    "set_complete": ("Полнота набора", "ISO 29148 5.2.6 a; IEEE 830 4.3.3"),
    "consistent": ("Непротиворечивость", "ISO 29148 5.2.6 b; IEEE 830 4.3.4"),
    "set_feasible": ("Реализуемость набора", "ISO 29148 5.2.6 c"),
    "comprehensible": ("Понятность", "ISO 29148 5.2.6 d"),
    "validatable": ("Возможность валидации", "ISO 29148 5.2.6 e"),
    # IEEE 830, 4.3
    "ranked": ("Ранжирование по важности/стабильности", "IEEE 830 4.3.5"),
    "modifiable": ("Модифицируемость (версия, структура)", "IEEE 830 4.3.7"),
    "traceable": ("Трассируемость", "IEEE 830 4.3.8"),
}

REQUIREMENT_CRITERIA = [
    "necessary", "appropriate", "unambiguous", "complete", "singular",
    "feasible", "verifiable", "correct", "conforming",
]
SET_CRITERIA = [
    "set_complete", "consistent", "set_feasible", "comprehensible",
    "validatable", "ranked", "modifiable", "traceable",
]

SEVERITIES = ["critical", "major", "minor"]
SEVERITY_RU = {"critical": "Критично", "major": "Существенно", "minor": "Незначительно"}
SEVERITY_ICON = {"critical": "🔴", "major": "🟠", "minor": "🟡"}


def criterion_name(key: str) -> str:
    return CRITERIA.get(key, (key, ""))[0]


def criterion_source(key: str) -> str:
    return CRITERIA.get(key, ("", ""))[1]


@dataclass
class Document:
    """Прочитанный документ: текст, разбитый на строки (нумерация с 1)."""
    path: str
    name: str
    fmt: str
    text: str
    title: str = ""

    @property
    def lines(self) -> list[str]:
        return self.text.split("\n")


@dataclass
class Requirement:
    rid: str                 # идентификатор в отчёте: R-01
    doc_id: str              # идентификатор в документе: Ф-1 или "—"
    text: str                # полный текст требования (как в документе, без markdown-разметки)
    section: str             # путь разделов: "Часть A > Функциональные требования"
    line_start: int
    line_end: int
    kind: str = "requirement"  # requirement | acceptance | api | context
    category: str = "Прочее"   # функциональное, нефункциональное, данные, ...

    @property
    def short(self) -> str:
        t = " ".join(self.text.split())
        return t if len(t) <= 110 else t[:107].rstrip() + "…"

    @property
    def location(self) -> str:
        if self.line_start == self.line_end:
            return f"строка {self.line_start}"
        return f"строки {self.line_start}–{self.line_end}"


@dataclass
class Quote:
    text: str                 # дословная цитата из документа
    line: Optional[int] = None  # номер строки (заполняет верификатор)
    verified: bool = False


@dataclass
class Issue:
    criterion: str
    severity: str
    title: str
    explanation: str
    quotes: list[Quote]
    suggestion: str = ""
    question: str = ""
    req_ids: list[str] = field(default_factory=list)  # R-xx, к которым относится
    scope: str = "requirement"  # requirement | set
    source: str = "rule"        # rule | llm | rule+llm
    rule_id: str = ""
    iid: str = ""               # D-01, присваивается в конце

    @property
    def is_critical(self) -> bool:
        return self.severity == "critical"


@dataclass
class Question:
    qid: str
    text: str
    why: str
    issue_ids: list[str] = field(default_factory=list)
    req_ids: list[str] = field(default_factory=list)
    options: list[str] = field(default_factory=list)
    addressee: str = "Аналитик"
    blocking: bool = False
    answer: str = ""


@dataclass
class Resolution:
    """Что агент сделал с ответом автора на уточняющий вопрос."""
    qid: str
    answer: str
    resolved_issue_ids: list[str] = field(default_factory=list)
    proposed_text: str = ""
    req_id: str = ""
    comment: str = ""


@dataclass
class RunInfo:
    doc_name: str
    doc_path: str
    started_at: str
    model: str
    agent_version: str
    mode: str                    # "LLM + правила" | "только правила"
    llm_calls: int = 0
    llm_errors: list[str] = field(default_factory=list)
    dropped_unverified: int = 0  # ответы LLM, отброшенные из-за неподтверждённой цитаты
    critic_used: bool = False
    rejected_by_critic: int = 0
    merged_duplicates: int = 0
    overall: str = ""            # общая оценка от LLM (если есть)
    duration_sec: float = 0.0


@dataclass
class Verdict:
    ready_for_dev: bool
    ready_for_test: bool
    summary: str
    top_fixes: list[str] = field(default_factory=list)


@dataclass
class Report:
    run: RunInfo
    document: Document
    requirements: list[Requirement]
    issues: list[Issue]
    questions: list[Question]
    verdict: Verdict
    resolutions: list[Resolution] = field(default_factory=list)
    rejected: list[tuple[Issue, str]] = field(default_factory=list)  # отклонено критиком

    def issues_for(self, rid: str) -> list[Issue]:
        return [i for i in self.issues if i.scope == "requirement" and rid in i.req_ids]

    @property
    def set_issues(self) -> list[Issue]:
        return [i for i in self.issues if i.scope == "set"]
