"""Оркестратор: прогоняет документ через все этапы и собирает отчёт.

ingest -> split -> правила -> LLM (требования, набор) -> проверка цитат -> слияние дублей
-> критик -> нумерация дефектов -> уточняющие вопросы -> (ответы автора) -> вердикт
"""
from __future__ import annotations

import difflib
import logging
import subprocess
import time
from datetime import datetime
from typing import Callable, Optional

from . import __version__
from .checks.critic import run_critic
from .checks.llm_checks import check_requirements_llm, check_set_llm
from .checks.rules import check_requirement, check_set
from .clarify import (apply_answers, ask_interactive, build_questions_llm, build_questions_rules,
                      load_answers)
from .config import ROOT, Settings
from .ingest import load_document
from .llm.client import LLMClient
from .report.stats import requirement_status
from .models import Issue, Report, Requirement, RunInfo, Verdict, criterion_name
from .split import split_requirements
from .verify import verify_issues

log = logging.getLogger(__name__)

SEV_ORDER = {"critical": 0, "major": 1, "minor": 2}
# дефекты, из-за которых нельзя начинать тестирование (определение «критичных» из ТЗ)
TEST_BLOCKING = {"verifiable", "consistent", "complete", "set_complete", "validatable"}


def agent_version() -> str:
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "describe", "--always", "--dirty", "--tags"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return f"{__version__} (git {out.stdout.strip()})"
    except (OSError, subprocess.SubprocessError):
        pass
    return __version__


def run(doc_path: str, settings: Settings, *, use_llm: bool = True, use_critic: bool = True,
        interactive: bool = False, answers_path: Optional[str] = None,
        progress: Callable[[str], None] = lambda s: None,
        client: Optional[LLMClient] = None) -> Report:
    t0 = time.monotonic()
    llm_on = use_llm and settings.llm_enabled
    if llm_on and client is None:
        client = LLMClient(settings)
    if not llm_on:
        client = None

    run_info = RunInfo(
        doc_name="", doc_path=doc_path, started_at=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
        model=settings.model_label if llm_on else "не использовалась (режим только правил)",
        agent_version=agent_version(),
        mode="LLM + правила + проверка цитат" + (" + критик" if llm_on and use_critic else "") if llm_on
        else "только правила (без LLM)",
    )

    progress("1/7 Читаю документ")
    doc = load_document(doc_path)
    run_info.doc_name = doc.title or doc.name

    progress("2/7 Нарезаю на требования")
    reqs = split_requirements(doc)
    real = [r for r in reqs if r.kind != "context"]
    log.info("Требований: %d (контекстных блоков: %d)", len(real), len(reqs) - len(real))

    progress("3/7 Проверяю правилами")
    rule_issues: list[Issue] = []
    for r in real:
        rule_issues.extend(check_requirement(doc, r))
    rule_issues.extend(check_set(doc, reqs))

    llm_issues: list[Issue] = []
    if client is not None:
        progress(f"4/7 Проверяю требования через LLM ({settings.model})")
        llm_issues += check_requirements_llm(client, settings, doc, real, rule_issues, run_info.llm_errors)
        progress("4/7 Проверяю набор требований целиком через LLM")
        set_issues, overall = check_set_llm(client, settings, doc, real, rule_issues, run_info.llm_errors)
        llm_issues += set_issues
        run_info.overall = overall
    else:
        progress("4/7 LLM не подключена — пропускаю смысловые проверки")

    progress("5/7 Проверяю цитаты и убираю дубли")
    rule_ok, _ = verify_issues(rule_issues, doc)
    llm_ok, dropped = verify_issues(llm_issues, doc)
    run_info.dropped_unverified = dropped
    issues, merged = merge_duplicates(rule_ok + llm_ok)
    run_info.merged_duplicates = merged

    rejected = []
    if client is not None and use_critic:
        progress("5/7 Критик перепроверяет замечания LLM")
        issues, rejected = run_critic(client, settings, doc, issues, run_info.llm_errors)
        run_info.critic_used = True
        run_info.rejected_by_critic = len(rejected)

    number_issues(issues, reqs)

    progress("6/7 Готовлю уточняющие вопросы автору")
    questions = None
    if client is not None:
        questions = build_questions_llm(client, issues, run_info.llm_errors)
    if questions is None:
        questions = build_questions_rules(issues)

    report = Report(run=run_info, document=doc, requirements=reqs, issues=issues, questions=questions,
                    verdict=make_verdict(issues, real, set()), rejected=rejected)

    if answers_path:
        load_answers(answers_path, questions)
    if interactive and questions:
        ask_interactive(questions)
    if any(q.answer for q in questions):
        progress("6/7 Применяю ответы автора")
        apply_report_answers(report, client)

    progress("7/7 Формирую вердикт")
    run_info.llm_calls = client.calls if client else 0
    if client is not None:
        run_info.model = f"{settings.provider}:{client.model}"
        if client.ok_calls == 0:
            run_info.mode = "только правила — LLM не ответила ни на один запрос (причины ниже)"
            run_info.model += " (не ответила)"
            run_info.critic_used = False
        elif run_info.llm_errors:
            run_info.mode += f" (частично: {len(run_info.llm_errors)} шагов LLM не выполнены)"
    run_info.duration_sec = round(time.monotonic() - t0, 1)
    return report


def apply_report_answers(report: Report, client: Optional[LLMClient]) -> None:
    """Применяет ответы автора (q.answer) к отчёту: закрытые дефекты, новые формулировки, вердикт."""
    real = [r for r in report.requirements if r.kind != "context"]
    report.resolutions = apply_answers(client, report.questions, report.issues, real, report.run.llm_errors)
    resolved = {x for r in report.resolutions for x in r.resolved_issue_ids}
    report.verdict = make_verdict(report.issues, real, resolved)
    if client is not None:
        report.run.llm_calls = client.calls


# ------------------------------------------------------------------ helpers

def _lines(i: Issue) -> set[int]:
    return {q.line for q in i.quotes if q.line}


_STOP = {"требование", "требования", "документ", "документе", "нет", "не", "для", "при", "или", "что", "как",
         "указан", "указано", "описан", "описано", "отсутствует", "неполное", "неясно"}


def _stems(text: str) -> set[str]:
    import re
    words = re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9]+", text.lower())
    return {w[:5] for w in words if (len(w) >= 4 or w.isascii() and len(w) >= 3) and w not in _STOP}


def _topic_overlap(a: Issue, b: Issue) -> bool:
    """Общие значимые основы слов в заголовках (GET, «заполн», «единиц», «201»…)."""
    return bool(_stems(a.title) & _stems(b.title)) or bool(_stems(a.title + " " + a.explanation[:120])
                                                           & _stems(b.title) & {"get", "post", "put", "200", "201", "400", "403", "404"})


def _similar(a: Issue, b: Issue) -> bool:
    if a.scope == "requirement" and b.scope == "requirement" and not set(a.req_ids) & set(b.req_ids):
        return False
    if not _lines(a) & _lines(b):
        return False
    if a.source != b.source and _topic_overlap(a, b):
        return True  # правило и LLM про одно и то же место и одну тему
    if a.criterion == b.criterion and (a.source != b.source or
                                       difflib.SequenceMatcher(None, a.title.lower(), b.title.lower()).ratio() > 0.5):
        return True
    # одна и та же проблема по HTTP/единицам часто приходит под разными критериями
    return difflib.SequenceMatcher(None, a.title.lower(), b.title.lower()).ratio() > 0.75


def merge_duplicates(issues: list[Issue]) -> tuple[list[Issue], int]:
    """Сливает одинаковые замечания (правило + LLM, требование + набор)."""
    # правила первыми: они детерминированы, их формулировки стабильнее
    ordered = sorted(issues, key=lambda i: (i.source != "rule", SEV_ORDER[i.severity]))
    out: list[Issue] = []
    merged = 0
    for i in ordered:
        dup = next((o for o in out if _similar(o, i)), None)
        if dup is None:
            out.append(i)
            continue
        merged += 1
        if dup.source != i.source and "+" not in dup.source:
            dup.source = "rule+llm"
        if SEV_ORDER[i.severity] < SEV_ORDER[dup.severity]:
            dup.severity = i.severity
        if not dup.question and i.question:
            dup.question = i.question
        if not dup.suggestion and i.suggestion:
            dup.suggestion = i.suggestion
        # пояснение не склеиваем: правило объясняет точнее, а склейка дублирует мысль или подмешивает чужую тему
        for r in i.req_ids:
            if r not in dup.req_ids:
                dup.req_ids.append(r)
        known = _lines(dup)
        dup.quotes += [q for q in i.quotes if q.line not in known]
    return out, merged


def number_issues(issues: list[Issue], reqs: list[Requirement]) -> None:
    order = {r.rid: n for n, r in enumerate(reqs)}

    def key(i: Issue):
        first = min((order.get(r, 999) for r in i.req_ids), default=999)
        return (SEV_ORDER[i.severity], i.scope != "requirement", first, min(_lines(i) or {0}))
    issues.sort(key=key)
    for n, i in enumerate(issues, 1):
        i.iid = f"D-{n:02d}"


def plural(n: int, one: str, few: str, many: str) -> str:
    """Склонение: 1 дефект, 2 дефекта, 5 дефектов."""
    if n % 10 == 1 and n % 100 != 11:
        w = one
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        w = few
    else:
        w = many
    return f"{n} {w}"


def make_verdict(issues: list[Issue], real: list[Requirement], resolved: set[str]) -> Verdict:
    open_ = [i for i in issues if i.iid not in resolved]
    crit = [i for i in open_ if i.severity == "critical"]
    major = [i for i in open_ if i.severity == "major"]
    test_block = [i for i in crit if i.criterion in TEST_BLOCKING]
    ready_dev = not crit
    ready_test = not test_block and not any(i.rule_id == "R-PLACEHOLDER" for i in crit)
    status = requirement_status(real, open_)
    clean = [r for r in real if status[r.rid] == "ok"]

    if ready_dev and not major:
        summary = "Документ можно передавать в разработку и тестирование. Замечания носят редакционный характер."
    elif ready_dev:
        summary = (f"Критичных дефектов нет, но есть {plural(len(major), 'существенное замечание', 'существенных замечания', 'существенных замечаний')}: "
                   "разработку можно начинать после их согласования с автором.")
    else:
        summary = (f"Начинать разработку по документу нельзя: найдено {plural(len(crit), 'критичный дефект', 'критичных дефекта', 'критичных дефектов')} "
                   f"(противоречия, непроверяемые или неполные требования, ошибки контракта). "
                   + ("Тестирование также невозможно: тест-дизайн упрётся в противоречия и пробелы. "
                      if not ready_test else "Тест-дизайн можно начинать параллельно с исправлениями. ")
                   + f"Без замечаний — {len(clean)} из {len(real)} требований.")

    fixes, seen = [], set()
    for i in crit + major:
        t = i.title.lower()
        if t in seen:
            continue
        seen.add(t)
        where = ", ".join(i.req_ids[:3]) if i.req_ids else "весь документ"
        fixes.append(f"{i.iid} ({where}): {i.title} — {i.suggestion or criterion_name(i.criterion)}")
        if len(fixes) >= 7:
            break
    return Verdict(ready_for_dev=ready_dev, ready_for_test=ready_test, summary=summary, top_fixes=fixes)
