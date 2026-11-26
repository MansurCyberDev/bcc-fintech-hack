"""Полный прогон пайплайна с поддельной LLM (без сети). Проверяет, что:
- выдуманная цитата LLM отбрасывается кодом;
- критик отклоняет замечание;
- дубли правила и LLM сливаются;
- вопросы и ответы автора попадают в отчёт;
- отчёт содержит все обязательные разделы ТЗ.
"""
import json

from src.config import Settings
from src.llm.client import LLMClient
from src.pipeline import run
from src.report.writer import write_report
from conftest import SAMPLE_MD


class FakeLLM(LLMClient):
    def __init__(self, s):
        super().__init__(s)
        self.purposes = []

    def _generate(self, system, user, purpose):
        self.calls += 1
        self.purposes.append(purpose)
        assert "<document>" in user or "критик" not in purpose
        if purpose.startswith("проверка требований"):
            return json.dumps({"requirements": [
                {"rid": "R-01", "issues": [
                    {"criterion": "verifiable", "severity": "critical", "title": "Нет метрики скорости",
                     "explanation": "Слово быстро нельзя проверить", "quote": "Система должна быстро показывать баланс",
                     "question": "За сколько секунд?"},
                    {"criterion": "complete", "severity": "major", "title": "Выдуманное",
                     "explanation": "x", "quote": "Этого текста нет в документе вообще никогда"},
                    {"criterion": "necessary", "severity": "minor", "title": "Придирка",
                     "explanation": "y", "quote": "показывать баланс"},
                ]},
                {"rid": "R-99", "issues": [{"title": "нет такого", "quote": "баланс"}]},
            ]})
        if purpose == "проверка набора":
            return json.dumps({"issues": [
                {"criterion": "consistent", "severity": "critical", "title": "Лимит в копейках и рублях",
                 "explanation": "Разные единицы", "quotes": ["Лимит хранится в копейках", "от 100 до 5 000 рублей"],
                 "req_ids": ["R-02"], "question": "В чём хранить?"}],
                "overall": "Документ сырой."})
        if purpose == "поиск противоречий":
            return json.dumps({"issues": [
                {"criterion": "consistent", "severity": "major", "title": "Сброс лимита в полночь и критерий приёмки",
                 "explanation": "Покупка около полуночи: правило сброса и критерий дают разный результат",
                 "quotes": ["Лимит сбрасывается в полночь", "Покупка на 600 рублей"], "req_ids": ["R-02"]}]})
        if purpose == "критик":
            ids = [l.split()[1] for l in user.splitlines() if l.startswith("- L-")]
            verdicts = []
            for k in ids:
                line = next(l for l in user.splitlines() if l.startswith(f"- {k} "))
                verdicts.append({"id": k, "keep": "Придирка" not in line, "reason": "вкусовщина"})
            return json.dumps({"verdicts": verdicts})
        if purpose == "уточняющие вопросы":
            return json.dumps({"questions": [
                {"text": "За сколько секунд показывать баланс?", "why": "Нельзя написать тест",
                 "options": ["1 с", "3 с"], "addressee": "Product owner", "blocking": True, "issue_ids": ["D-01"]}]})
        if purpose == "применение ответов":
            return json.dumps({"resolutions": [{"qid": "Q-01", "resolved_issue_ids": ["D-01"], "req_id": "R-01",
                                                "proposed_text": "Баланс показывается за 1 с.", "comment": ""}]})
        raise AssertionError(purpose)


def make_settings():
    return Settings(provider="gemini", api_key="test", model="fake", base_url="x", timeout=1, max_retries=0,
                    max_calls=20, batch_size=50, temperature=0, max_doc_chars=50000, min_interval=0)


def test_full_pipeline_with_fake_llm(tmp_path):
    p = tmp_path / "spec.md"
    p.write_text(SAMPLE_MD, encoding="utf-8")
    ans = tmp_path / "answers.json"
    ans.write_text(json.dumps({"Q-01": "1 с"}), encoding="utf-8")
    s = make_settings()
    llm = FakeLLM(s)
    rep = run(str(p), s, client=llm, answers_path=str(ans))

    titles = [i.title for i in rep.issues]
    assert "Выдуманное" not in titles                  # цитата не найдена -> отброшено
    assert rep.run.dropped_unverified == 1
    assert "Придирка" not in titles                    # отклонено критиком
    assert rep.run.rejected_by_critic == 1
    assert all(q.verified and q.line for i in rep.issues for q in i.quotes)
    assert not any("R-99" in i.req_ids for i in rep.issues)
    # дубль: правило R-SET-UNITS и LLM про копейки/рубли -> одно замечание
    units = [i for i in rep.issues if i.criterion == "consistent" and "копейк" in " ".join(q.text for q in i.quotes).lower()]
    assert len(units) == 1 and units[0].source == "rule+llm"
    assert rep.questions[0].qid == "Q-01" and rep.questions[0].answer == "1 с"
    assert rep.resolutions and rep.resolutions[0].resolved_issue_ids == ["D-01"]
    assert rep.run.llm_calls == len(llm.purposes) == 6
    assert "поиск противоречий" in llm.purposes
    conflict = [i for i in rep.issues if i.title.startswith("Сброс лимита")]
    assert len(conflict) == 1 and conflict[0].scope == "set" and len(conflict[0].quotes) == 2
    assert rep.run.overall == "Документ сырой."

    paths = write_report(rep, tmp_path / "out" / "report.md")
    md = paths[0].read_text(encoding="utf-8")
    for section in ["## 1. Паспорт прогона", "## 2. Как агент нарезал требования", "## 3. Проверка каждого требования",
                    "## 4. Проверка всего набора требований", "## 5. Уточняющие вопросы", "## 6. Сводка", "## 7. Итог",
                    "Приложение А", "Приложение Б"]:
        assert section in md
    assert "fake" in md and "Версия агента" in md
    assert (tmp_path / "out" / "report.html").exists()
    data = json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))
    assert data["run"]["llm_calls"] == 6


def test_llm_failure_degrades_gracefully(tmp_path):
    class Broken(LLMClient):
        def _generate(self, *a, **k):
            from src.llm.client import LLMError
            self.calls += 1
            raise LLMError("сеть недоступна")
    p = tmp_path / "spec.md"
    p.write_text(SAMPLE_MD, encoding="utf-8")
    s = make_settings()
    rep = run(str(p), s, client=Broken(s))
    assert rep.issues                       # правила всё равно сработали
    assert rep.questions                    # вопросы — запасным алгоритмом
    assert rep.run.llm_errors
    assert not rep.verdict.ready_for_dev


def test_mode_is_honest_when_llm_never_answers(tmp_path):
    class Dead(LLMClient):
        def _gemini(self, *a, **k):
            from src.llm.client import _Retryable
            raise _Retryable("HTTP 503: overloaded")
    import time as _t
    p = tmp_path / "spec.md"
    p.write_text(SAMPLE_MD, encoding="utf-8")
    s = make_settings()
    orig = _t.sleep
    _t.sleep = lambda x: None
    try:
        rep = run(str(p), s, client=Dead(s))
    finally:
        _t.sleep = orig
    assert "только правила" in rep.run.mode
    assert "не ответила" in rep.run.model
    assert rep.run.llm_calls <= 4          # «предохранитель» не даёт долбить API
