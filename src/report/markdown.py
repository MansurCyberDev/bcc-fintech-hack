"""Отчёт в Markdown (основной формат сдачи)."""
from __future__ import annotations

from collections import Counter

from ..models import (CRITERIA, REQUIREMENT_CRITERIA, SET_CRITERIA, SEVERITY_ICON, SEVERITY_RU, Issue, Report,
                      criterion_name, criterion_source)

SHORT = {
    "necessary": "Необх.", "appropriate": "Уместн.", "unambiguous": "Однозн.", "complete": "Полнота",
    "singular": "Атом.", "feasible": "Реализ.", "verifiable": "Провер.", "correct": "Коррект.",
    "conforming": "Шаблон",
}


def esc(text: str) -> str:
    """Экранирование для ячеек таблиц Markdown."""
    return " ".join(str(text).replace("|", "\\|").split())


def status_icon(issues: list[Issue]) -> str:
    if any(i.severity == "critical" for i in issues):
        return "❌"
    if issues:
        return "⚠️"
    return "✅"


def render_issue(i: Issue, level: str = "####") -> list[str]:
    src = {"rule": "правило", "llm": "LLM", "rule+llm": "правило + LLM"}.get(i.source, i.source)
    out = [
        f"{level} {i.iid} {SEVERITY_ICON[i.severity]} {i.title}",
        "",
        f"*{SEVERITY_RU[i.severity]} · {criterion_name(i.criterion)} ({criterion_source(i.criterion)})"
        f"{' · требования: ' + ', '.join(i.req_ids) if i.req_ids else ''} · найдено: {src}*",
        "",
    ]
    for q in i.quotes:
        out.append(f"> «{q.text}» — строка {q.line}")
    out.append("")
    if i.explanation:
        out.append(f"**Почему это дефект.** {i.explanation}")
        out.append("")
    if i.suggestion:
        out.append(f"**Как исправить.** {i.suggestion}")
        out.append("")
    if i.question:
        out.append(f"**Вопрос автору.** {i.question}")
        out.append("")
    return out


def render(report: Report) -> str:
    r = report
    run, doc = r.run, r.document
    real = [q for q in r.requirements if q.kind != "context"]
    context = [q for q in r.requirements if q.kind == "context"]
    sev = Counter(i.severity for i in r.issues)
    L: list[str] = []
    a = L.append

    a(f"# Отчёт о проверке требований: «{doc.title or doc.name}»")
    a("")
    verdict_icon = "✅" if r.verdict.ready_for_dev else "❌"
    a(f"> {verdict_icon} **Итог:** {r.verdict.summary}")
    a(f"> Требований: **{len(real)}**, дефектов: **{len(r.issues)}** "
      f"(🔴 {sev['critical']} · 🟠 {sev['major']} · 🟡 {sev['minor']}), уточняющих вопросов: **{len(r.questions)}**.")
    a("")

    # 1. Паспорт
    a("## 1. Паспорт прогона")
    a("")
    a("| Параметр | Значение |")
    a("| :--- | :--- |")
    rows = [
        ("Документ", f"{doc.title} (`{doc.name}`, формат: {doc.fmt}, строк: {len(doc.lines)})"),
        ("Дата и время прогона", run.started_at),
        ("Модель", run.model),
        ("Версия агента", run.agent_version),
        ("Режим", run.mode),
        ("Вызовов LLM", str(run.llm_calls)),
        ("Длительность", f"{run.duration_sec} с"),
        ("Проверка цитат", f"все цитаты найдены в документе кодом; отброшено замечаний LLM без подтверждённой цитаты: {run.dropped_unverified}"),
        ("Слито дублей", str(run.merged_duplicates)),
    ]
    if run.critic_used:
        rows.append(("Критик", f"отклонено замечаний LLM как ложных: {run.rejected_by_critic}"))
    rows.append(("Стандарты", "ISO/IEC/IEEE 29148:2018 п. 5.2.5–5.2.6; IEEE Std 830-1998 п. 4.3"))
    for k, v in rows:
        a(f"| {k} | {esc(v)} |")
    if run.llm_errors:
        a("")
        a("**Внимание: часть LLM-проверок не выполнена:**")
        for e in run.llm_errors:
            a(f"- {esc(e)}")
    a("")

    # 2. Нарезка
    a("## 2. Как агент нарезал требования")
    a("")
    a("Агент делит документ на разделы по заголовкам, а внутри раздела — на пункты списков, абзацы и строки таблиц. "
      "Если в документе у требования есть номер, он сохраняется; если нет — указано «—» и место в документе (номер строки).")
    a("")
    a("| ID в отчёте | ID в документе | Тип | Краткая формулировка (цитата) | Место |")
    a("| :--- | :--- | :--- | :--- | :--- |")
    for q in real:
        a(f"| {q.rid} | {esc(q.doc_id)} | {esc(q.category)} | {esc(q.short)} | {q.location} |")
    a("")
    if context:
        a(f"<details><summary>Служебные блоки документа, не являющиеся требованиями ({len(context)}) — "
          "использованы как контекст при проверке набора</summary>")
        a("")
        a("| ID | Раздел | Фрагмент | Место |")
        a("| :--- | :--- | :--- | :--- |")
        for q in context:
            a(f"| {q.rid} | {esc(q.section)} | {esc(q.short)} | {q.location} |")
        a("")
        a("</details>")
        a("")

    # 3. Каждое требование
    a("## 3. Проверка каждого требования")
    a("")
    a("Критерии ISO/IEC/IEEE 29148:2018 п. 5.2.5 (IEEE 830 п. 4.3): "
      + ", ".join(f"{SHORT[c]} — {criterion_name(c)}" for c in REQUIREMENT_CRITERIA) + ".")
    a("")
    a("| ID | " + " | ".join(SHORT[c] for c in REQUIREMENT_CRITERIA) + " | Итог |")
    a("| :--- | " + " | ".join(":---:" for _ in REQUIREMENT_CRITERIA) + " | :---: |")
    for q in real:
        iss = r.issues_for(q.rid)
        cells = []
        for c in REQUIREMENT_CRITERIA:
            hit = [i for i in iss if i.criterion == c]
            cells.append(" ".join(f"{SEVERITY_ICON[i.severity]}{i.iid}" for i in hit) if hit else "✓")
        a(f"| {q.rid} | " + " | ".join(cells) + f" | {status_icon(iss)} |")
    a("")
    a("✓ — нарушений по критерию не найдено; D-xx — номер дефекта. Дефекты набора, затрагивающие требование, — в разделе 4.")
    a("")
    for q in real:
        iss = r.issues_for(q.rid)
        set_refs = [i.iid for i in r.set_issues if q.rid in i.req_ids]
        a(f"### {q.rid} {status_icon(iss)} {q.doc_id if q.doc_id != '—' else ''} — {q.category}")
        a("")
        a(f"> {q.text if len(q.text) < 600 else q.text[:600] + '…'}")
        a(f">")
        a(f"> *{q.location}, раздел «{q.section}»*")
        a("")
        if not iss:
            a("Нарушений критериев отдельного требования не найдено." +
              (f" Связанные дефекты набора: {', '.join(set_refs)}." if set_refs else ""))
            a("")
            continue
        if set_refs:
            a(f"Связанные дефекты набора: {', '.join(set_refs)}.")
            a("")
        for i in iss:
            L.extend(render_issue(i))

    # 4. Набор
    a("## 4. Проверка всего набора требований")
    a("")
    a("| Критерий набора | Источник | Результат |")
    a("| :--- | :--- | :--- |")
    for c in SET_CRITERIA:
        hit = [i for i in r.set_issues if i.criterion == c]
        res = "✓ нарушений не найдено" if not hit else \
            f"{status_icon(hit)} " + ", ".join(f"{i.iid}" for i in hit)
        a(f"| {criterion_name(c)} | {criterion_source(c)} | {res} |")
    other = [i for i in r.set_issues if i.criterion not in SET_CRITERIA]
    if other:
        a(f"| Прочие свойства | — | {', '.join(i.iid for i in other)} |")
    a("")
    if run.overall:
        a(f"**Общая оценка (LLM):** {run.overall}")
        a("")
    if not r.set_issues:
        a("Дефектов уровня набора не найдено.")
        a("")
    for i in r.set_issues:
        L.extend(render_issue(i, "###"))

    # 5. Вопросы
    a("## 5. Уточняющие вопросы автору документа")
    a("")
    if not r.questions:
        a("Вопросов нет.")
    else:
        a("Без ответов на блокирующие вопросы (🚫) нельзя однозначно реализовать и протестировать требования.")
        a("")
        for q in r.questions:
            mark = "🚫 " if q.blocking else ""
            a(f"**{q.qid}. {mark}{q.text}**")
            a("")
            meta = [f"Кому: {q.addressee}"]
            if q.issue_ids:
                meta.append(f"дефекты: {', '.join(q.issue_ids)}")
            if q.req_ids:
                meta.append(f"требования: {', '.join(q.req_ids)}")
            a(f"*{' · '.join(meta)}*")
            a("")
            if q.why:
                a(f"Зачем: {q.why}")
                a("")
            for n, o in enumerate(q.options, 1):
                a(f"{n}. {o}")
            if q.options:
                a("")
            if q.answer:
                a(f"**Ответ автора:** {q.answer}")
                a("")
    a("")

    # 6. Сводка
    a("## 6. Сводка")
    a("")
    with_issues = {x for i in r.issues for x in i.req_ids}
    a("| Показатель | Значение |")
    a("| :--- | ---: |")
    a(f"| Требований проверено | {len(real)} |")
    a(f"| Требований без замечаний | {len([q for q in real if q.rid not in with_issues])} |")
    a(f"| Всего нарушений | {len(r.issues)} |")
    a(f"| 🔴 Критичных | {sev['critical']} |")
    a(f"| 🟠 Существенных | {sev['major']} |")
    a(f"| 🟡 Незначительных | {sev['minor']} |")
    a(f"| Дефектов отдельных требований / набора | {len(r.issues) - len(r.set_issues)} / {len(r.set_issues)} |")
    a(f"| Уточняющих вопросов (блокирующих) | {len(r.questions)} ({sum(q.blocking for q in r.questions)}) |")
    a("")
    by_c = Counter(criterion_name(i.criterion) for i in r.issues)
    a("**Нарушения по критериям:** " + "; ".join(f"{k} — {v}" for k, v in by_c.most_common()) + ".")
    a("")
    crit = [i for i in r.issues if i.severity == "critical"]
    a("**Критичные дефекты** (непроверяемые, противоречивые, неполные или ошибочные формулировки):")
    a("")
    if crit:
        for i in crit:
            q = i.quotes[0]
            a(f"- **{i.iid}** {i.title} ({', '.join(i.req_ids) or 'набор'}; {criterion_name(i.criterion)}) — "
              f"«{q.text[:140]}{'…' if len(q.text) > 140 else ''}», строка {q.line}")
    else:
        a("- нет")
    a("")

    # 7. Итог
    a("## 7. Итог")
    a("")
    a(f"- **Можно ли начинать разработку:** {'да' if r.verdict.ready_for_dev else 'нет'}")
    a(f"- **Можно ли начинать тестирование (тест-дизайн):** {'да' if r.verdict.ready_for_test else 'нет'}")
    a("")
    a(r.verdict.summary)
    a("")
    if r.verdict.top_fixes:
        a("**Что исправить в первую очередь:**")
        a("")
        for n, f in enumerate(r.verdict.top_fixes, 1):
            a(f"{n}. {f}")
        a("")
    blocking = [q for q in r.questions if q.blocking and not q.answer]
    if blocking:
        a(f"**Получить ответы автора на блокирующие вопросы:** {', '.join(q.qid for q in blocking)}.")
        a("")

    # Приложения
    if r.resolutions:
        a("## Приложение А. Ответы автора и предложенные формулировки")
        a("")
        for res in r.resolutions:
            a(f"**{res.qid}** — ответ: «{res.answer}»")
            a("")
            if res.resolved_issue_ids:
                a(f"- Закрыты дефекты: {', '.join(res.resolved_issue_ids)}")
            if res.proposed_text:
                a(f"- Предлагаемая формулировка{(' для ' + res.req_id) if res.req_id else ''}: «{res.proposed_text}»")
            if res.comment:
                a(f"- Комментарий агента: {res.comment}")
            a("")
    if r.rejected:
        a("## Приложение Б. Замечания, отклонённые критиком")
        a("")
        a("Эти замечания модель-аналитик предложила, но независимый критик счёл их ложными. Приведены для прозрачности.")
        a("")
        for i, why in r.rejected:
            a(f"- {i.title} ({', '.join(i.req_ids) or 'набор'}) — причина: {why or 'не указана'}")
        a("")
    a("## Приложение В. Методика")
    a("")
    a("1. **Чтение и нарезка** выполняются кодом (без LLM): результат воспроизводим, у каждого требования есть номер строки.")
    a("2. **Правила** (код) ищут формальные дефекты: размытые слова, заглушки, пустые поля, неатомарность, HTTP-семантику, "
      "несогласованные типы и единицы, коды ответа, границы диапазонов, версию документа, согласования.")
    a("3. **LLM-аналитик** проверяет каждое требование по 9 критериям ISO 29148 п. 5.2.5 и набор целиком по п. 5.2.6 / IEEE 830 п. 4.3.")
    a("4. **Проверка цитат кодом**: замечание попадает в отчёт, только если его цитата найдена в документе.")
    a("5. **Критик** (второй проход LLM) отсеивает ложные срабатывания; дубли правил и LLM сливаются.")
    a("6. **Уточнитель** превращает дефекты в вопросы автору с вариантами ответа; в интерактивном режиме ответы применяются.")
    a("")
    a("*Отчёт сформирован агентом автоматически и вручную не редактировался.*")
    a("")
    return "\n".join(L)
