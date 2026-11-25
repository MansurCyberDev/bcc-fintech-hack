"""Этап 5. Уточняющие вопросы автору документа и обработка ответов.

1) Вопросы строятся из найденных дефектов: LLM объединяет похожие, расставляет
   приоритет и предлагает варианты ответа. Без LLM работает запасной алгоритм.
2) В интерактивном режиме агент задаёт вопросы в терминале, а ответы автора
   применяет: отмечает закрытые дефекты и предлагает новую формулировку.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from .checks.llm_checks import format_requirements, load_prompt
from .llm.client import LLMClient, LLMError
from .models import Issue, Question, Requirement, Resolution

log = logging.getLogger(__name__)

MAX_QUESTIONS = 12

ADDRESSEE_BY_CRITERION = {
    "correct": "Разработчик API",
    "consistent": "Аналитик",
    "complete": "Аналитик",
    "set_complete": "Аналитик",
    "verifiable": "Тестировщик",
    "validatable": "Product owner",
    "modifiable": "Аналитик",
    "ranked": "Product owner",
    "necessary": "Product owner",
    "appropriate": "Архитектор",
}


class _LLMQuestion(BaseModel):
    text: str
    why: str = ""
    options: list[str] = Field(default_factory=list)
    addressee: str = "Аналитик"
    blocking: bool = False
    issue_ids: list[str] = Field(default_factory=list)

    @field_validator("options", "issue_ids", mode="before")
    @classmethod
    def _as_list(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return [str(x) for x in v if x]


class _LLMQuestions(BaseModel):
    questions: list[_LLMQuestion] = Field(default_factory=list)


class _LLMResolution(BaseModel):
    qid: str
    resolved_issue_ids: list[str] = Field(default_factory=list)
    req_id: str = ""
    proposed_text: str = ""
    comment: str = ""


class _LLMResolutions(BaseModel):
    resolutions: list[_LLMResolution] = Field(default_factory=list)


def _sort_key(i: Issue) -> tuple:
    return ({"critical": 0, "major": 1, "minor": 2}[i.severity], i.iid)


def build_questions_rules(issues: list[Issue]) -> list[Question]:
    """Запасной вариант без LLM: вопрос из каждого дефекта, похожие объединяются."""
    out: list[Question] = []
    by_text: dict[str, Question] = {}
    for i in sorted(issues, key=_sort_key):
        if not i.question or i.severity == "minor":
            continue
        key = i.question.strip().lower()
        if key in by_text:
            q = by_text[key]
            q.issue_ids.append(i.iid)
            q.req_ids.extend(r for r in i.req_ids if r not in q.req_ids)
            continue
        q = Question(
            qid="", text=i.question.strip(), why=f"{i.title}: без ответа требование нельзя однозначно реализовать.",
            issue_ids=[i.iid], req_ids=list(i.req_ids),
            addressee=ADDRESSEE_BY_CRITERION.get(i.criterion, "Аналитик"),
            blocking=i.severity == "critical",
        )
        by_text[key] = q
        out.append(q)
    out = out[:MAX_QUESTIONS]
    for n, q in enumerate(out, 1):
        q.qid = f"Q-{n:02d}"
    return out


def build_questions_llm(client: LLMClient, issues: list[Issue], errors: list[str]) -> Optional[list[Question]]:
    if not issues:
        return []
    listing = "\n".join(
        f"- {i.iid} [{i.severity}] {', '.join(i.req_ids) or 'набор'}: {i.title}. "
        f"Цитата: «{i.quotes[0].text[:160]}» (стр. {i.quotes[0].line}). "
        + (f"Черновой вопрос: {i.question}" if i.question else "")
        for i in sorted(issues, key=_sort_key)
    )
    user = load_prompt("clarify.md").format(max_questions=MAX_QUESTIONS, issues=listing)
    try:
        raw = client.generate_json(load_prompt("system.md"), user, purpose="уточняющие вопросы")
        parsed = _LLMQuestions.model_validate(raw if isinstance(raw, dict) else {"questions": raw})
    except (LLMError, ValueError, ValidationError) as e:
        log.error("LLM: уточняющие вопросы: %s", e)
        errors.append(f"Уточняющие вопросы: {e}")
        return None

    by_id = {i.iid: i for i in issues}
    out: list[Question] = []
    for q in parsed.questions[:MAX_QUESTIONS]:
        ids = [x for x in q.issue_ids if x in by_id]
        if not q.text.strip():
            continue
        reqs: list[str] = []
        for x in ids:
            reqs.extend(r for r in by_id[x].req_ids if r not in reqs)
        out.append(Question(
            qid="", text=q.text.strip(), why=q.why.strip(), issue_ids=ids, req_ids=reqs,
            options=[o.strip() for o in q.options if o.strip()][:4],
            addressee=q.addressee.strip() or "Аналитик",
            blocking=bool(q.blocking) or any(by_id[x].severity == "critical" for x in ids),
        ))
    out.sort(key=lambda q: (not q.blocking))
    for n, q in enumerate(out, 1):
        q.qid = f"Q-{n:02d}"
    return out


# ------------------------------ ответы автора ------------------------------

def ask_interactive(questions: list[Question], input_fn: Callable[[str], str] = input,
                    print_fn: Callable[[str], None] = print) -> None:
    """Задаёт вопросы в терминале. Пустой ответ — пропустить, 'q' — закончить."""
    print_fn("\nАгент: у меня есть уточняющие вопросы к автору документа.")
    print_fn("Ответьте текстом или номером варианта. Enter — пропустить, q — закончить.\n")
    for q in questions:
        mark = " [блокирующий]" if q.blocking else ""
        print_fn(f"{q.qid}{mark} ({q.addressee}): {q.text}")
        if q.why:
            print_fn(f"   Зачем: {q.why}")
        for n, o in enumerate(q.options, 1):
            print_fn(f"   {n}) {o}")
        try:
            ans = input_fn("   Ответ: ").strip()
        except EOFError:
            break
        if ans.lower() in ("q", "й", "quit", "exit"):
            break
        if ans.isdigit() and 1 <= int(ans) <= len(q.options):
            ans = q.options[int(ans) - 1]
        q.answer = ans


def load_answers(path: str | Path, questions: list[Question]) -> None:
    """Ответы из JSON-файла: {"Q-01": "ответ", ...}."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for q in questions:
        a = data.get(q.qid)
        if isinstance(a, str):
            q.answer = a.strip()


def apply_answers(client: Optional[LLMClient], questions: list[Question], issues: list[Issue],
                  reqs: list[Requirement], errors: list[str]) -> list[Resolution]:
    answered = [q for q in questions if q.answer]
    if not answered:
        return []
    if client is None:
        return [Resolution(qid=q.qid, answer=q.answer, comment="Ответ записан; LLM не подключена, "
                           "переформулировку нужно сделать вручную.") for q in answered]
    by_id = {i.iid: i for i in issues}
    qa = "\n\n".join(
        f"{q.qid}: {q.text}\nОтвет автора: {q.answer}\nСвязанные дефекты: "
        + "; ".join(f"{x} ({by_id[x].title}, {', '.join(by_id[x].req_ids)})" for x in q.issue_ids if x in by_id)
        for q in answered
    )
    rel = {r for q in answered for r in q.req_ids}
    user = load_prompt("apply_answers.md").format(
        qa=qa, requirements=format_requirements([r for r in reqs if r.rid in rel] or reqs[:20]))
    try:
        raw = client.generate_json(load_prompt("system.md"), user, purpose="применение ответов")
        parsed = _LLMResolutions.model_validate(raw if isinstance(raw, dict) else {"resolutions": raw})
    except (LLMError, ValueError, ValidationError) as e:
        errors.append(f"Применение ответов: {e}")
        return [Resolution(qid=q.qid, answer=q.answer, comment="Не удалось обработать ответ через LLM.") for q in answered]

    qmap = {q.qid: q for q in answered}
    out = []
    for r in parsed.resolutions:
        if r.qid not in qmap:
            continue
        allowed = set(qmap[r.qid].issue_ids)
        out.append(Resolution(
            qid=r.qid, answer=qmap[r.qid].answer,
            resolved_issue_ids=[x for x in r.resolved_issue_ids if x in allowed],
            proposed_text=r.proposed_text.strip(), req_id=r.req_id.strip(), comment=r.comment.strip(),
        ))
    done = {r.qid for r in out}
    out += [Resolution(qid=q.qid, answer=q.answer) for q in answered if q.qid not in done]
    return out
