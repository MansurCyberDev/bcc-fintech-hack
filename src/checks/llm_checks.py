"""Этап 3б. Проверки с помощью LLM: каждое требование (пачками) и весь набор.

Модель получает документ с номерами строк и список требований. Ответ — строгий
JSON, который валидируется pydantic-схемой. Цитаты потом проверяет код (verify.py).
"""
from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from ..config import PROMPTS_DIR, Settings
from ..llm.client import LLMClient, LLMError
from ..models import CRITERIA, SEVERITIES, Document, Issue, Quote, Requirement

log = logging.getLogger(__name__)


# ----------------------------- схемы ответа LLM -----------------------------

def _norm_criterion(v: str) -> str:
    v = (v or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "ambiguous": "unambiguous", "unambiguity": "unambiguous", "completeness": "complete",
        "incomplete": "complete", "consistency": "consistent", "inconsistent": "consistent",
        "verifiability": "verifiable", "testable": "verifiable", "correctness": "correct",
        "atomic": "singular", "singularity": "singular", "feasibility": "feasible",
        "traceability": "traceable", "modifiability": "modifiable", "completeness_of_set": "set_complete",
        "set_consistent": "consistent", "understandable": "comprehensible", "implementation_free": "appropriate",
    }
    v = aliases.get(v, v)
    return v if v in CRITERIA else "correct"


def _norm_severity(v: str) -> str:
    v = (v or "").strip().lower()
    mapping = {"high": "critical", "blocker": "critical", "критично": "critical", "medium": "major",
               "существенно": "major", "low": "minor", "незначительно": "minor"}
    v = mapping.get(v, v)
    return v if v in SEVERITIES else "major"


class LLMIssue(BaseModel):
    criterion: str = "correct"
    severity: str = "major"
    title: str = ""
    explanation: str = ""
    quote: Optional[str] = None
    quotes: list[str] = Field(default_factory=list)
    req_ids: list[str] = Field(default_factory=list)
    suggestion: str = ""
    question: str = ""

    @field_validator("criterion", mode="before")
    @classmethod
    def _check_criterion(cls, v):
        return _norm_criterion(str(v))

    @field_validator("severity", mode="before")
    @classmethod
    def _check_severity(cls, v):
        return _norm_severity(str(v))

    @field_validator("quotes", "req_ids", mode="before")
    @classmethod
    def _as_list(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return [str(x) for x in v if x]

    def all_quotes(self) -> list[str]:
        qs = list(self.quotes)
        if self.quote:
            qs.insert(0, self.quote)
        return [q for q in qs if isinstance(q, str) and q.strip()]


class LLMReqResult(BaseModel):
    rid: str
    issues: list[LLMIssue] = Field(default_factory=list)


class LLMReqResponse(BaseModel):
    requirements: list[LLMReqResult] = Field(default_factory=list)


class LLMSetResponse(BaseModel):
    issues: list[LLMIssue] = Field(default_factory=list)
    overall: str = ""


# ------------------------------- форматирование -------------------------------

def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def numbered_document(doc: Document, max_chars: int) -> str:
    """Документ с номерами строк. Закрывающий тег экранируется (защита от prompt injection)."""
    out, total = [], 0
    for i, line in enumerate(doc.lines, 1):
        safe = line.replace("</document>", "</ document>").replace("<document>", "< document>")
        row = f"{i}: {safe}"
        total += len(row) + 1
        if total > max_chars:
            out.append(f"... [документ обрезан: показаны строки 1–{i - 1} из {len(doc.lines)}]")
            break
        out.append(row)
    return "\n".join(out)


def format_requirements(reqs: list[Requirement]) -> str:
    return "\n".join(
        f"- {r.rid} | в документе: {r.doc_id} | {r.location} | раздел: {r.section} | {r.text}"
        for r in reqs
    )


def format_findings(issues: list[Issue]) -> str:
    if not issues:
        return "(ничего)"
    return "\n".join(
        f"- {', '.join(i.req_ids) or 'набор'}: {i.title} (стр. {', '.join(str(q.line) for q in i.quotes)})"
        for i in issues
    )


# --------------------------------- проверки ---------------------------------

def check_requirements_llm(client: LLMClient, settings: Settings, doc: Document,
                           reqs: list[Requirement], rule_issues: list[Issue],
                           errors: list[str]) -> list[Issue]:
    system = load_prompt("system.md")
    template = load_prompt("check_requirements.md")
    document = numbered_document(doc, settings.max_doc_chars)
    known = {r.rid for r in reqs}
    result: list[Issue] = []

    batches = [reqs[i:i + settings.batch_size] for i in range(0, len(reqs), settings.batch_size)]
    for n, batch in enumerate(batches, 1):
        ids = {r.rid for r in batch}
        findings = [i for i in rule_issues if set(i.req_ids) & ids]
        user = template.format(
            rule_findings=format_findings(findings),
            requirements=format_requirements(batch),
            document=document,
        )
        try:
            raw = client.generate_json(system, user, purpose=f"проверка требований {n}/{len(batches)}")
            parsed = LLMReqResponse.model_validate(raw if isinstance(raw, dict) else {"requirements": raw})
        except (LLMError, ValueError, ValidationError) as e:
            msg = f"Пачка {n}/{len(batches)} ({batch[0].rid}–{batch[-1].rid}): {e}"
            log.error("LLM: %s", msg)
            errors.append(msg)
            continue
        for rr in parsed.requirements:
            if rr.rid not in known:
                continue
            for li in rr.issues:
                result.append(_to_issue(li, scope="requirement", req_ids=[rr.rid]))
    return result


def check_set_llm(client: LLMClient, settings: Settings, doc: Document, reqs: list[Requirement],
                  rule_issues: list[Issue], errors: list[str]) -> tuple[list[Issue], str]:
    system = load_prompt("system.md")
    user = load_prompt("check_set.md").format(
        rule_findings=format_findings([i for i in rule_issues if i.scope == "set"]),
        requirements=format_requirements(reqs),
        document=numbered_document(doc, settings.max_doc_chars),
    )
    try:
        raw = client.generate_json(system, user, purpose="проверка набора")
        parsed = LLMSetResponse.model_validate(raw if isinstance(raw, dict) else {"issues": raw})
    except (LLMError, ValueError, ValidationError) as e:
        log.error("LLM: проверка набора: %s", e)
        errors.append(f"Проверка набора: {e}")
        return [], ""
    known = {r.rid for r in reqs}
    out = []
    for li in parsed.issues:
        out.append(_to_issue(li, scope="set", req_ids=[r for r in li.req_ids if r in known]))
    return out, parsed.overall.strip()


def _to_issue(li: LLMIssue, scope: str, req_ids: list[str]) -> Issue:
    return Issue(
        criterion=li.criterion,
        severity=li.severity,
        title=(li.title or "Замечание").strip()[:160],
        explanation=li.explanation.strip(),
        quotes=[Quote(q) for q in li.all_quotes()],
        suggestion=li.suggestion.strip(),
        question=li.question.strip(),
        req_ids=req_ids,
        scope=scope,
        source="llm",
    )
