"""Слияние дублей: правило и LLM про одно место и одну тему — один дефект."""
from src.models import Issue, Quote
from src.pipeline import merge_duplicates


def issue(src, crit, title, line, req="R-01"):
    return Issue(criterion=crit, severity="major", title=title, explanation="пояснение",
                 quotes=[Quote(text="GET /x", line=line)], req_ids=[req], source=src)


def test_rule_and_llm_same_topic_are_merged():
    a = issue("rule", "correct", "GET используется для изменения данных", 10)
    b = issue("llm", "conforming", "Метод GET меняет состояние", 10)
    out, merged = merge_duplicates([a, b])
    assert merged == 1 and len(out) == 1 and out[0].source == "rule+llm"


def test_different_lines_are_not_merged():
    a = issue("rule", "correct", "GET используется для изменения данных", 10)
    b = issue("llm", "correct", "GET используется для изменения данных", 20)
    out, merged = merge_duplicates([a, b])
    assert merged == 0 and len(out) == 2


def test_llm_splits_of_one_problem_are_merged():
    a = issue("llm", "consistent", "Ошибка валидации суммы возвращается с кодом 200", 190)
    b = issue("llm", "correct", "Код 200 при ошибке валидации суммы", 190)
    b.scope = "set"
    out, merged = merge_duplicates([a, b])
    assert merged == 1 and len(out) == 1


def test_llm_different_topics_on_same_line_stay_apart():
    a = issue("llm", "unambiguous", "Размытая формулировка «и далее»", 33)
    b = issue("llm", "complete", "Не указан формат даты", 33)
    out, merged = merge_duplicates([a, b])
    assert merged == 0 and len(out) == 2
