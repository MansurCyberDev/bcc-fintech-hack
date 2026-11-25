"""Этап 4. Критик: второй взгляд LLM на замечания LLM, отсев ложных срабатываний.

Правила (код) критику не отдаются: они детерминированы и проверяемы.
Если критик недоступен — замечания остаются как есть (и это отмечается в паспорте).
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field, ValidationError

from ..config import Settings
from ..llm.client import LLMClient, LLMError
from ..models import Document, Issue
from .llm_checks import _norm_severity, load_prompt, numbered_document

log = logging.getLogger(__name__)


class _Verdict(BaseModel):
    id: str
    keep: bool = True
    severity: str = ""
    reason: str = ""


class _Verdicts(BaseModel):
    verdicts: list[_Verdict] = Field(default_factory=list)


def run_critic(client: LLMClient, settings: Settings, doc: Document, issues: list[Issue],
               errors: list[str]) -> tuple[list[Issue], list[tuple[Issue, str]]]:
    """Возвращает (оставленные замечания, [(отклонённое, причина)])."""
    llm_issues = [i for i in issues if i.source == "llm"]
    if not llm_issues:
        return issues, []
    tmp = {f"L-{n:02d}": i for n, i in enumerate(llm_issues, 1)}
    listing = "\n".join(
        f"- {k} [{i.severity}] {', '.join(i.req_ids) or 'набор'} / {i.criterion}: {i.title}. {i.explanation} "
        f"Цитаты: " + " | ".join(f"«{q.text[:150]}» (стр. {q.line})" for q in i.quotes)
        for k, i in tmp.items()
    )
    user = load_prompt("critic.md").format(issues=listing, document=numbered_document(doc, settings.max_doc_chars))
    try:
        raw = client.generate_json(load_prompt("system.md"), user, purpose="критик")
        parsed = _Verdicts.model_validate(raw if isinstance(raw, dict) else {"verdicts": raw})
    except (LLMError, ValueError, ValidationError) as e:
        log.error("LLM: критик: %s", e)
        errors.append(f"Критик: {e}")
        return issues, []

    rejected: list[tuple[Issue, str]] = []
    drop_ids = set()
    for v in parsed.verdicts:
        i = tmp.get(v.id)
        if i is None:
            continue
        if not v.keep:
            drop_ids.add(id(i))
            rejected.append((i, v.reason.strip()))
        elif v.severity:
            i.severity = _norm_severity(v.severity)
    kept = [i for i in issues if id(i) not in drop_ids]
    log.info("Критик: оставлено %d, отклонено %d", len(kept), len(rejected))
    return kept, rejected
