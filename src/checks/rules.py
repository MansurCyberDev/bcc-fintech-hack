"""Этап 3а. Детерминированные проверки (правила в коде).

Правила общие — они не знают про конкретный документ. Это «быстрые линтеры»
требований: размытые слова, заглушки, неатомарность, ошибки HTTP-семантики,
смешение единиц измерения, отсутствие версии и т.д. Работают без LLM,
поэтому агент даёт полезный отчёт даже без ключа API.
"""
from __future__ import annotations

import re
from collections import Counter

from ..models import Document, Issue, Quote, Requirement

# Размытые слова и выражения -> почему это плохо
VAGUE = {
    r"быстр(о|ый|ая|ое)": "«быстро» не задаёт измеримого значения",
    r"своевременн": "«своевременно» не задаёт срок",
    r"удобн": "«удобно» нельзя проверить тестом",
    r"интуитивн": "«интуитивно» нельзя проверить тестом",
    r"оптимальн": "не указан критерий оптимальности",
    r"эффективн": "не указана метрика эффективности",
    r"при необходимости": "не сказано, кто и когда решает, что «необходимо»",
    r"по возможности": "делает требование необязательным",
    r"как правило": "допускает неописанные исключения",
    r"и т\.\s?д\.?": "открытый перечень: неясно, что ещё входит",
    r"и т\.\s?п\.?": "открытый перечень: неясно, что ещё входит",
    r"и др\.?": "открытый перечень: неясно, что ещё входит",
    r"и далее": "открытый перечень: неясно, какие именно значения входят",
    r"достаточн": "не указан порог «достаточности»",
    r"соответствующ": "не указано, чему именно «соответствующий»",
    r"некотор(ые|ых|ым)": "неопределённое множество",
    r"различн(ые|ых)": "неопределённое множество",
    r"современн": "не измеримо",
    r"надёжн|надежн": "не указана метрика надёжности (доступность, MTBF)",
    r"\betc\b": "open-ended list",
    r"user[- ]friendly": "not measurable",
    r"\bas appropriate\b|\bif possible\b|\bas needed\b": "makes requirement optional/ambiguous",
}

PLACEHOLDERS = [
    r"\bTBD\b", r"\bTODO\b", r"\bTBA\b", r"\?\?\?", r"будет уточнен", r"уточнить позже",
    r"заполняются\b", r"заполняется\b", r"не заполнен", r"\bn/?a\b",
]

MODIFY_WORDS = r"сохран|созда|измен|обнов|замен|удал|запис|save|create|update|delete|set[A-Z_]|replace"


def _clean(line: str) -> str:
    t = re.sub(r"\*\*|__|`", "", line).strip()
    t = re.sub(r"^[*\-+]\s+", "", t)
    t = re.sub(r"^#{1,6}\s+", "", t)
    if t.startswith("|"):
        cells = [c.strip() for c in t.strip("|").split("|")]
        t = " | ".join(c for c in cells if c)
    return " ".join(t.split())


def _req_lines(doc: Document, r: Requirement) -> list[tuple[int, str]]:
    lines = doc.lines
    return [(i, lines[i - 1]) for i in range(r.line_start, r.line_end + 1) if 0 < i <= len(lines)]


def _sentence_with(text: str, pattern: str) -> str:
    for s in re.split(r"(?<=[.!?;])\s+", text):
        if re.search(pattern, s, re.I):
            return s.strip()
    return text.strip()


def _line_quote(doc: Document, r: Requirement, pattern: str) -> Quote | None:
    for ln, raw in _req_lines(doc, r):
        if re.search(pattern, raw, re.I):
            clean = _clean(raw)
            if not raw.strip().startswith("|") and len(clean) > 140:
                clean = _sentence_with(clean, pattern)
            return Quote(clean, ln, True)
    return None


def check_requirement(doc: Document, r: Requirement) -> list[Issue]:
    issues: list[Issue] = []
    text = r.text

    # 1. Размытые формулировки -> однозначность
    for pat, why in VAGUE.items():
        if re.search(pat, text, re.I):
            q = _line_quote(doc, r, pat)
            if q:
                word = re.search(pat, text, re.I).group(0)
                issues.append(Issue(
                    criterion="unambiguous", severity="major",
                    title=f"Размытая формулировка «{word}»",
                    explanation=f"Слово/оборот «{word}»: {why}. Разные читатели поймут требование по-разному.",
                    quotes=[q], req_ids=[r.rid], rule_id="R-VAGUE",
                    suggestion="Заменить на конкретный перечень или измеримое значение.",
                    question=f"Что именно подразумевается под «{word}» в {r.doc_id if r.doc_id != '—' else r.rid}? Перечислите значения явно.",
                ))

    # 2. Заглушки / незаполненные места -> полнота
    for pat in PLACEHOLDERS:
        if re.search(pat, text, re.I):
            q = _line_quote(doc, r, pat)
            if q:
                whole = len(text.split()) <= 4
                issues.append(Issue(
                    criterion="complete", severity="critical" if whole else "major",
                    title="Требование не заполнено (заглушка)",
                    explanation="Вместо содержания стоит заглушка. По такому месту нельзя ни разработать, ни проверить систему.",
                    quotes=[q], req_ids=[r.rid], rule_id="R-PLACEHOLDER",
                    suggestion="Заполнить раздел до передачи в разработку или явно пометить как «вне объёма».",
                    question="Кто и когда заполнит этот раздел? Без него требование нельзя принять.",
                ))
            break

    # 2б. Пустые значения в таблице «поле — значение» (например «Prod: —»)
    empty_rows = []
    for ln, raw in _req_lines(doc, r):
        if raw.strip().startswith("|"):
            cells = [c.strip() for c in raw.strip().strip("|").split("|")]
            if len(cells) == 2 and cells[0] and re.fullmatch(r"[—\-–]?", re.sub(r"[`*]", "", cells[1]).strip()):
                empty_rows.append((ln, raw))
        # «Prod: —» внутри значения
        if re.search(r"(Prod|Test|Stage)\s*:\s*[—–-]\s*\.?\s*($|\|)", raw):
            empty_rows.append((ln, raw))
    if empty_rows:
        quotes = [Quote(_clean(raw), ln, True) for ln, raw in empty_rows]
        issues.append(Issue(
            criterion="complete", severity="major",
            title="Пустые поля в спецификации",
            explanation="Часть обязательных полей не заполнена (стоит «—»). Нельзя понять фактическое значение для реализации/окружения.",
            quotes=quotes, req_ids=[r.rid], rule_id="R-EMPTY-FIELD",
            suggestion="Заполнить поля или указать «не применяется» с причиной.",
            question="Какие значения должны быть в пустых полях (например, адрес Prod, ссылка на Swagger)?",
        ))

    # 3. Атомарность: несколько самостоятельных утверждений
    if r.kind in ("requirement",) and not text.count("→"):
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if len(s.split()) >= 3]
        if len(sentences) >= 3:
            issues.append(Issue(
                criterion="singular", severity="minor",
                title=f"Неатомарное требование ({len(sentences)} утверждений)",
                explanation="В одном пункте несколько независимых правил. Их нельзя по отдельности проследить до тестов и отметить как выполненные.",
                quotes=[Quote(_clean(_req_lines(doc, r)[0][1]), r.line_start, True)],
                req_ids=[r.rid], rule_id="R-ATOMIC",
                suggestion="Разбить на отдельные пронумерованные требования (по одному правилу в каждом).",
            ))

    issues.extend(_http_rules(doc, r))
    return issues


def _http_rules(doc: Document, r: Requirement) -> list[Issue]:
    out: list[Issue] = []
    text = r.text
    raw_all = "\n".join(l for _, l in _req_lines(doc, r))

    # GET для изменяющей операции
    has_get = re.search(r"(REST type|Method|Метод|HTTP)[^\n]*\bGET\b", raw_all, re.I) or re.search(r"curl\s+-G\b", raw_all)
    if has_get and re.search(MODIFY_WORDS, raw_all, re.I):
        qs = [q for q in (
            _line_quote(doc, r, r"\bGET\b"),
            _line_quote(doc, r, r"Сохранени|save"),
            _line_quote(doc, r, r"curl\s+-G"),
        ) if q]
        out.append(Issue(
            criterion="correct", severity="critical",
            title="Метод GET для операции изменения данных",
            explanation=("Операция меняет состояние (сохранение/замена настройки), но описана как GET. "
                         "GET по RFC 9110 должен быть безопасным и идемпотентным: его кэшируют, повторяют, "
                         "логируют вместе с параметрами в URL. Это ошибка проектирования и риск ИБ."),
            quotes=qs, req_ids=[r.rid], rule_id="R-HTTP-GET-MUTATION",
            suggestion="Использовать PUT (замена настройки) или POST, параметры передавать в теле JSON.",
            question="Метод должен сохранять порог или читать его? Если сохранять — согласны ли заменить GET на PUT/POST с телом запроса?",
        ))

    # 200 + Created
    if re.search(r"\b200\b[^\n]*Created", raw_all):
        q = _line_quote(doc, r, r"200[^\n]*Created")
        out.append(Issue(
            criterion="correct", severity="major",
            title="Код 200 подписан как «Created»",
            explanation="«Created» по HTTP — это код 201. Код 200 означает «OK». Непонятно, какой код вернёт сервис при сохранении.",
            quotes=[q] if q else [], req_ids=[r.rid], rule_id="R-HTTP-201",
            suggestion="Определить один код успеха: 200 OK для замены или 201 Created для создания.",
            question="Какой код возвращается при успешном сохранении: 200 или 201?",
        ))
    # 200 и ошибка в теле
    if re.search(r"\b200\b", raw_all) and re.search(r"\"result\"\s*:\s*\"ERROR\"|ERROR", raw_all) and re.search(r"\"result\"\s*:\s*\"OK\"|\bOK\b", raw_all):
        qs = [q for q in (_line_quote(doc, r, r"\b200\b"), _line_quote(doc, r, r"ERROR")) if q]
        out.append(Issue(
            criterion="consistent", severity="critical",
            title="Ошибка возвращается с кодом успеха 200",
            explanation=("Под кодом 200 описаны два ответа: успешный (OK) и ошибочный (ERROR). При этом в документе "
                         "сказано, что 400 — это ответ на неверные параметры. Клиент не сможет однозначно определить исход по коду."),
            quotes=qs, req_ids=[r.rid], rule_id="R-HTTP-200-ERROR",
            suggestion="Ошибку валидации суммы возвращать с кодом 400 (или 422) и описать её отдельно.",
            question="Отклонённая сумма — это 200 c result=ERROR или 400? Нужен один вариант.",
        ))
    # заголовок со статусом X, а в тексте «возвращается Y»
    sec_code = re.match(r"(\d{3})\b", r.section.split(" > ")[-1]) if r.section else None
    m = re.search(r"возвращается\s+(\d{3})|returns?\s+(\d{3})", text, re.I)
    if sec_code and m:
        other = m.group(1) or m.group(2)
        if other != sec_code.group(1):
            q = _line_quote(doc, r, r"возвращается\s+\d{3}|returns?\s+\d{3}")
            out.append(Issue(
                criterion="consistent", severity="major",
                title=f"Раздел статуса {sec_code.group(1)} описывает ответ {other}",
                explanation=(f"Раздел назван «{r.section.split(' > ')[-1]}», но внутри сказано, что возвращается {other}. "
                             f"Непонятно, будет ли вообще использоваться код {sec_code.group(1)}."),
                quotes=[q] if q else [], req_ids=[r.rid], rule_id="R-HTTP-STATUS-MISMATCH",
                suggestion=f"Либо убрать статус {sec_code.group(1)} из перечня, либо описать, когда он возвращается.",
                question=f"Возвращает ли метод когда-нибудь {sec_code.group(1)}? Если нет — удалить его из списка статусов?",
            ))
    return out


# ---------------------------- проверки набора ----------------------------

def check_set(doc: Document, reqs: list[Requirement]) -> list[Issue]:
    issues: list[Issue] = []
    lines = doc.lines
    full = doc.text
    real = [r for r in reqs if r.kind != "context"]

    def find_line(pattern: str) -> Quote | None:
        for i, l in enumerate(lines, 1):
            if re.search(pattern, l, re.I):
                return Quote(_clean(l), i, True)
        return None

    # 1. Версия и дата документа
    has_version = re.search(r"(верси[яи]|version|ревизи)\s*(документа)?\s*[:№#]?\s*v?\d+(\.\d+)*", full, re.I)
    if not has_version:
        q = find_line(r"верси|version") or Quote(_clean(lines[0]), 1, True)
        issues.append(Issue(
            criterion="modifiable", severity="major", scope="set",
            title="Нет версии и даты документа",
            explanation="Документ не версионирован. Нельзя понять, какую редакцию проверяли, и отследить изменения (IEEE 830 4.3.7).",
            quotes=[q], rule_id="R-SET-VERSION",
            suggestion="Добавить номер версии, дату и историю изменений.",
            question="Какая это версия документа и от какой даты? Кто владелец изменений?",
        ))

    # 2. Смешение единиц измерения денег
    pairs = [
        (r"тиын|tiyn", r"тенге|₸|\bKZT\b", "тиынах", "тенге"),
        (r"копе", r"рубл|₽|\bRUB\b", "копейках", "рублях"),
        (r"\bcents?\b", r"dollars?|\$|\bUSD\b", "центах", "долларах"),
    ]
    for minor, major, a, b in pairs:
        if re.search(minor, full, re.I) and re.search(major, full, re.I):
            qa = find_line(minor)
            qb = None
            for i, l in enumerate(lines, 1):
                if re.search(major, l, re.I) and (qa is None or i != qa.line or True):
                    if qa and i == qa.line:
                        continue
                    qb = Quote(_clean(l), i, True)
                    break
            issues.append(Issue(
                criterion="consistent", severity="major", scope="set",
                title=f"Суммы хранятся в разных единицах ({a} и {b})",
                explanation=(f"В документе суммы указаны и в {a}, и в {b}. Не описано, где и как выполняется пересчёт "
                             "и округление. Сравнение суммы с порогом может дать ошибку в 100 раз."),
                quotes=[q for q in (qa, qb) if q], rule_id="R-SET-UNITS",
                suggestion="Хранить все суммы в одной единице (минимальной) или явно описать правило пересчёта.",
                question=f"В каких единицах сравнивать расходы с порогом: в {a} или в {b}? Где выполняется пересчёт?",
            ))

    # 3. Дубликаты идентификаторов
    ids = Counter(r.doc_id for r in real if r.doc_id and r.doc_id != "—" and not r.doc_id.startswith(("п.", "«", "строка")))
    for did, cnt in ids.items():
        if cnt > 1:
            dups = [r for r in real if r.doc_id == did]
            issues.append(Issue(
                criterion="traceable", severity="major", scope="set",
                title=f"Идентификатор {did} встречается {cnt} раза",
                explanation="Одинаковые идентификаторы ломают трассировку требований к тестам и задачам.",
                quotes=[Quote(_clean(lines[d.line_start - 1]), d.line_start, True) for d in dups],
                req_ids=[d.rid for d in dups], rule_id="R-SET-DUP-ID",
                suggestion="Сделать идентификаторы уникальными.",
            ))

    # 4. Трассируемость: доля требований без идентификатора
    no_id = [r for r in real if r.doc_id == "—"]
    if real and len(no_id) / len(real) > 0.3:
        sample = no_id[:3]
        issues.append(Issue(
            criterion="traceable", severity="minor", scope="set",
            title=f"{len(no_id)} из {len(real)} требований без идентификатора",
            explanation="Нефункциональные требования, требования к данным, интеграции и т.п. не пронумерованы. На них нельзя сослаться из тестов и задач.",
            quotes=[Quote(_clean(lines[r.line_start - 1]), r.line_start, True) for r in sample],
            req_ids=[r.rid for r in no_id], rule_id="R-SET-NO-ID",
            suggestion="Присвоить идентификаторы всем требованиям (например, НФ-1, Д-1, И-1).",
        ))

    # 5. Нет приоритетов
    if not re.search(r"приоритет|priority|\bmust\b|\bshould\b|MoSCoW|обязательн", full, re.I):
        issues.append(Issue(
            criterion="ranked", severity="minor", scope="set",
            title="Требования не ранжированы по важности",
            explanation="Не указан приоритет требований. При нехватке времени неясно, что делать в первую очередь (IEEE 830 4.3.5).",
            quotes=[Quote(_clean(lines[real[0].line_start - 1]), real[0].line_start, True)] if real else [],
            rule_id="R-SET-RANK",
            suggestion="Добавить приоритет (MoSCoW или высокий/средний/низкий) к каждому требованию.",
        ))

    # 6. Пустые таблицы (согласующие, чек-листы без отметок)
    for i, l in enumerate(lines, 1):
        s = l.strip()
        if s.startswith("|") and not re.search(r"[^|\s:\-]", s) and i > 2 and lines[i - 2].strip().startswith("|"):
            # строка таблицы из одних разделителей и пробелов, при этом не строка-разделитель заголовка
            if re.search(r"-{2,}", s):
                continue
            head = None
            for j in range(i - 1, 0, -1):
                if lines[j - 1].strip().startswith("#"):
                    head = Quote(_clean(lines[j - 1].lstrip("# ")), j, True)
                    break
            issues.append(Issue(
                criterion="validatable", severity="minor", scope="set",
                title="Пустая таблица согласования/заполнения",
                explanation="В таблице нет ни одной заполненной строки. Документ не согласован ответственными лицами.",
                quotes=[q for q in (head,) if q], rule_id="R-SET-EMPTY-TABLE",
                suggestion="Получить и зафиксировать согласования до старта разработки.",
                question="Кто должен согласовать документ (бизнес, архитектура, ИБ) и когда?",
            ))
    unfilled = [i for i, l in enumerate(lines, 1) if re.search(r"Да\s*/\s*Нет", l)]
    if len(unfilled) >= 3:
        issues.append(Issue(
            criterion="validatable", severity="minor", scope="set",
            title=f"Чек-лист ревью не заполнен ({len(unfilled)} пунктов «Да / Нет»)",
            explanation="Пункты чек-листа оставлены как в шаблоне. Документ формально не прошёл ревью.",
            quotes=[Quote(_clean(lines[unfilled[0] - 1]), unfilled[0], True)], rule_id="R-SET-CHECKLIST",
            suggestion="Пройти чек-лист и проставить отметки.",
        ))

    issues.extend(_api_set_rules(doc, real))
    issues.extend(_boundary_rule(doc, real))

    # 7. Обязательные разделы
    kinds = {r.kind for r in real}
    cats = {r.category for r in real}
    if "acceptance" not in kinds:
        issues.append(Issue(
            criterion="validatable", severity="major", scope="set",
            title="Нет критериев приёмки",
            explanation="Без критериев приёмки нельзя подтвердить, что система делает то, что нужно бизнесу.",
            quotes=[Quote(_clean(lines[0]), 1, True)], rule_id="R-SET-NO-AC",
            suggestion="Добавить критерии приёмки (Дано/Когда/Тогда) для каждого функционального требования.",
        ))
    if "Нефункциональное" not in cats:
        issues.append(Issue(
            criterion="set_complete", severity="major", scope="set",
            title="Нет нефункциональных требований",
            explanation="Не заданы производительность, доступность, безопасность, журналирование.",
            quotes=[Quote(_clean(lines[0]), 1, True)], rule_id="R-SET-NO-NFR",
            suggestion="Добавить раздел нефункциональных требований с измеримыми значениями.",
        ))
    return issues


def _api_set_rules(doc: Document, real: list[Requirement]) -> list[Issue]:
    out: list[Issue] = []
    lines = doc.lines
    api = [r for r in real if r.kind == "api"]
    if not api:
        return out

    # a) один и тот же параметр с разными типами в разных таблицах (запрос vs ответ)
    fmt_by_param: dict[str, list[tuple[str, int, str]]] = {}
    for i, l in enumerate(lines, 1):
        s = l.strip()
        if not s.startswith("|"):
            continue
        cells = [re.sub(r"[`*]", "", c).strip() for c in s.strip("|").split("|")]
        if len(cells) >= 3 and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cells[0] or "-") \
                and re.fullmatch(r"(string|integer|int|number|boolean|bool|float|double|object|array|long|date|datetime)",
                                 cells[1].lower()):
            fmt_by_param.setdefault(cells[0], []).append((cells[1].lower(), i, l))
    for name, rows in fmt_by_param.items():
        kinds = {f for f, _, _ in rows}
        if len(kinds) > 1:
            out.append(Issue(
                criterion="consistent", severity="major", scope="set",
                title=f"Параметр {name} имеет разные типы: {', '.join(sorted(kinds))}",
                explanation=(f"Поле «{name}» описано с разными форматами в разных таблицах (например, в запросе и в ответе). "
                             "Клиент и сервер по-разному сериализуют значение."),
                quotes=[Quote(_clean(l), ln, True) for _, ln, l in rows],
                req_ids=[r.rid for r in api if any(r.line_start <= ln <= r.line_end for _, ln, _ in rows)],
                rule_id="R-SET-API-TYPES",
                suggestion="Использовать один тип для поля во всех местах (например, integer для суммы, boolean для признака).",
                question=f"Какой тип у поля «{name}»: {' или '.join(sorted(kinds))}?",
            ))

    # b) коды ответа упоминаются, но не описаны в перечне статусов
    api_lines = sorted({ln for r in api for ln in range(r.line_start, r.line_end + 1)})
    described, mentioned = {}, {}
    for ln in api_lines:
        l = lines[ln - 1]
        m = re.match(r"\s*(?:#{1,6}\s*|\*\*)\s*([1-5]\d\d)\.", l)
        if m:
            described.setdefault(m.group(1), ln)
        for c in re.findall(r"(?<![\d.])([1-5]\d\d)(?!\d|[.,]\d|\s*(?:bit|бит|₸|тенге|000))", re.sub(r"`[^`]*`|\{[^}]*\}", "", l)):
            if 100 <= int(c) <= 599:
                mentioned.setdefault(c, ln)
    if described:
        missing = [c for c in mentioned if c not in described]
        if missing:
            out.append(Issue(
                criterion="complete", severity="major", scope="set",
                title=f"Коды ответа {', '.join(missing)} упоминаются, но не описаны",
                explanation=("В перечне статусов метода эти коды отсутствуют, хотя в тексте они встречаются. "
                             "Неясно, когда именно сервис их возвращает и какое у ответа тело."),
                quotes=[Quote(_clean(lines[mentioned[c] - 1]), mentioned[c], True) for c in missing],
                req_ids=[r.rid for r in api if any(r.line_start <= mentioned[c] <= r.line_end for c in missing)],
                rule_id="R-SET-API-CODES",
                suggestion="Перечислить ровно те коды, которые возвращает метод, с условием и примером тела для каждого.",
                question=f"Возвращает ли метод коды {', '.join(missing)}? Если да — в каких случаях?",
            ))

    # c) Content-Type задан, но тело не используется
    ct = next((ln for ln in api_lines if re.search(r"Content-Type", lines[ln - 1], re.I)), None)
    nb = next((ln for ln in api_lines if re.search(r"тело не используется|без тела|body is not used|no body", lines[ln - 1], re.I)), None)
    if ct and nb:
        out.append(Issue(
            criterion="consistent", severity="minor", scope="set",
            title="Указан Content-Type, но тело запроса не используется",
            explanation="Тип содержимого тела задан, хотя тела нет. Это признак того, что контракт запроса не доработан.",
            quotes=[Quote(_clean(lines[ct - 1]), ct, True), Quote(_clean(lines[nb - 1]), nb, True)],
            req_ids=[r.rid for r in api if r.line_start in (ct, nb)], rule_id="R-SET-API-BODY",
            suggestion="Либо перенести параметры в JSON-тело, либо убрать Content-Type.",
        ))
    return out


_NUM = r"\d{1,3}(?:[  ]\d{3})+|\d+"


def _to_int(s: str) -> int:
    return int(re.sub(r"\D", "", s))


def _boundary_rule(doc: Document, real: list[Requirement]) -> list[Issue]:
    """Для диапазона «от X до Y» должны быть критерии приёмки на границах."""
    out: list[Issue] = []
    ac = [r for r in real if r.kind == "acceptance"]
    if not ac:
        return out
    ac_text = " ".join(r.text for r in ac)
    ac_nums = {_to_int(x) for x in re.findall(_NUM, ac_text)}
    for r in real:
        if r.kind == "acceptance":
            continue
        for m in re.finditer(rf"от\s+({_NUM})\s+до\s+({_NUM})", r.text):
            lo, hi = _to_int(m.group(1)), _to_int(m.group(2))
            if lo >= hi:
                continue
            missing = [v for v in (lo, hi) if v not in ac_nums]
            if missing:
                q = _line_quote(doc, r, re.escape(m.group(1)).replace("\\ ", r"\s"))
                out.append(Issue(
                    criterion="verifiable", severity="major", scope="requirement",
                    title=f"Нет критериев приёмки для границ диапазона {m.group(0)}",
                    explanation=(f"Диапазон {m.group(0)} задан, но в критериях приёмки нет проверок граничных значений "
                                 f"({', '.join(f'{v:,}'.replace(',', ' ') for v in missing)}) и значений за границами. "
                                 "Ошибки «на единицу» не будут пойманы приёмкой."),
                    quotes=[q] if q else [Quote(_clean(doc.lines[r.line_start - 1]), r.line_start, True)],
                    req_ids=[r.rid], rule_id="R-BOUNDARY",
                    suggestion=f"Добавить критерии: {lo - 1}, {lo}, {hi}, {hi + 1} — с ожидаемым результатом для каждого.",
                    question=f"Что должно происходить при вводе значения вне диапазона (например, {lo - 1} или {hi + 1}): какое сообщение увидит клиент?",
                ))
    return out
