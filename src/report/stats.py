"""Показатели качества документа. Общие для CLI (JSON-отчёт) и веб-платформы."""
from __future__ import annotations

from collections import Counter

from ..models import (CRITERIA, REQUIREMENT_CRITERIA, SET_CRITERIA, SEVERITY_RU, Report, criterion_name,
                      criterion_source)

# Противоречием считаем нарушение непротиворечивости набора или явное «противоречие» в заголовке.
CONTRADICTION_CRITERIA = {"consistent"}


def is_contradiction(issue) -> bool:
    return issue.criterion in CONTRADICTION_CRITERIA or "противоречи" in issue.title.lower()


def touches_requirement(issue) -> bool:
    """Замечание относится к конкретным требованиям, а не ко всему документу (нет ID, нет приоритетов)."""
    return not (issue.scope == "set" and len(issue.req_ids) > 3)


def requirement_status(requirements, issues) -> dict[str, str]:
    """Статус каждого требования — по самому тяжёлому открытому дефекту; «ok», если дефектов нет.
    Одно правило для вердикта, MD-сводки и дашборда."""
    worst: dict[str, str] = {}
    order = {"critical": 0, "major": 1, "minor": 2}
    for i in issues:
        if not touches_requirement(i):
            continue
        for rid in i.req_ids:
            if rid not in worst or order[i.severity] < order[worst[rid]]:
                worst[rid] = i.severity
    return {r.rid: worst.get(r.rid, "ok") for r in requirements}


def compute_stats(report: Report) -> dict:
    """Сводные цифры для дашборда: всё считается из отчёта, без обращений к LLM."""
    resolved = {x for r in report.resolutions for x in r.resolved_issue_ids}
    real = [r for r in report.requirements if r.kind != "context"]
    open_issues = [i for i in report.issues if i.iid not in resolved]

    req_status = requirement_status(real, open_issues)
    status_count = Counter(req_status.values())

    total = len(real)
    good = status_count.get("ok", 0) + status_count.get("minor", 0)
    # Индекс качества: доля требований без критичных и существенных замечаний (0–100).
    quality = round(100 * good / total) if total else 0

    by_sev = Counter(i.severity for i in open_issues)
    by_crit = Counter(i.criterion for i in open_issues)
    by_source = Counter("rule" if i.source == "rule" else "llm" if i.source == "llm" else "both"
                        for i in open_issues)

    return {
        "requirements_total": total,
        "requirements_by_status": {k: status_count.get(k, 0) for k in ("ok", "minor", "major", "critical")},
        "requirement_status": req_status,
        "quality_index": quality,
        "issues_total": len(open_issues),
        "issues_resolved": len(resolved),
        "issues_by_severity": {k: by_sev.get(k, 0) for k in ("critical", "major", "minor")},
        "issues_by_criterion": dict(by_crit.most_common()),
        "issues_by_source": {k: by_source.get(k, 0) for k in ("rule", "llm", "both")},
        "contradictions": sum(is_contradiction(i) for i in open_issues),
        "questions_total": len(report.questions),
        "questions_blocking": sum(q.blocking for q in report.questions),
        "questions_answered": sum(bool(q.answer) for q in report.questions),
        "criteria": {k: {"name": criterion_name(k), "source": criterion_source(k),
                         "level": "requirement" if k in REQUIREMENT_CRITERIA else "set"}
                     for k in CRITERIA if k in REQUIREMENT_CRITERIA or k in SET_CRITERIA},
        "severity_names": SEVERITY_RU,
    }
