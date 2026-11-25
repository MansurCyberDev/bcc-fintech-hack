import json

import pytest

from src.config import Settings
from src.llm.client import LLMClient, LLMError, parse_json


def settings(**kw):
    base = dict(provider="gemini", api_key="k", model="m", base_url="https://x", timeout=5, max_retries=2,
                max_calls=10, batch_size=10, temperature=0, max_doc_chars=10000, min_interval=0)
    base.update(kw)
    return Settings(**base)


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = json.dumps(body) if not isinstance(body, str) else body

    def json(self):
        if isinstance(self._body, str):
            raise ValueError
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        return self.responses.pop(0)


def gemini_ok(text):
    return Resp(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})


def test_parse_json_variants():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Вот ответ: {"a": [1, 2]} конец') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        parse_json("не json")


def test_retry_on_429_then_ok(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    sess = FakeSession([Resp(429, {"error": {"message": "quota"}}, {"Retry-After": "1"}), gemini_ok('{"ok": true}')])
    c = LLMClient(settings(), session=sess)
    assert c.generate_json("sys", "user") == {"ok": True}
    assert c.calls == 2
    url, body, headers = sess.calls[0]
    assert headers["x-goog-api-key"] == "k"          # ключ в заголовке, а не в URL
    assert "k" not in url
    assert body["generationConfig"]["responseMimeType"] == "application/json"


def test_invalid_json_is_repaired():
    sess = FakeSession([gemini_ok("это не json"), gemini_ok('{"fixed": 1}')])
    assert LLMClient(settings(), session=sess).generate_json("s", "u") == {"fixed": 1}


def test_bad_key_is_not_retried():
    sess = FakeSession([Resp(403, {"error": {"message": "API key not valid"}})])
    with pytest.raises(LLMError, match="Ключ API"):
        LLMClient(settings(), session=sess).generate_json("s", "u")
    assert len(sess.calls) == 1


def test_call_limit(monkeypatch):
    c = LLMClient(settings(max_calls=1), session=FakeSession([gemini_ok("{}"), gemini_ok("{}")]))
    c.generate_json("s", "u")
    with pytest.raises(LLMError, match="лимит"):
        c.generate_json("s", "u")


def test_openai_compatible():
    sess = FakeSession([Resp(200, {"choices": [{"message": {"content": '{"x": 2}'}}]})])
    c = LLMClient(settings(provider="openai"), session=sess)
    assert c.generate_json("s", "u") == {"x": 2}
    assert sess.calls[0][0].endswith("/chat/completions")
    assert sess.calls[0][2]["Authorization"] == "Bearer k"


def test_daily_quota_fails_fast_and_disables(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: (_ for _ in ()).throw(AssertionError("не должно ждать")))
    body = {"error": {"message": "You exceeded your current quota", "details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}}
    sess = FakeSession([Resp(429, body)])
    c = LLMClient(settings(), session=sess)
    with pytest.raises(LLMError, match="квота"):
        c.generate_json("s", "u")
    assert c.disabled and len(sess.calls) == 1
    with pytest.raises(LLMError, match="отключена"):      # следующие шаги не ходят в сеть
        c.generate_json("s", "u")
    assert len(sess.calls) == 1


def test_unavailable_model_falls_back(monkeypatch):
    sess = FakeSession([Resp(404, {"error": {"message": "model is no longer available to new users"}}),
                        gemini_ok('{"ok": 1}')])
    c = LLMClient(settings(fallback_models=("lite",)), session=sess)
    assert c.generate_json("s", "u") == {"ok": 1}
    assert c.model == "lite"
    assert "/models/lite:" in sess.calls[1][0]


def test_overloaded_model_falls_back(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    sess = FakeSession([Resp(503, {"error": {"message": "overloaded"}})] * 3 + [gemini_ok('{"ok": 2}')])
    c = LLMClient(settings(fallback_models=("lite",)), session=sess)
    assert c.generate_json("s", "u") == {"ok": 2}
    assert c.model == "lite"


def test_truncated_json_is_salvaged():
    # модель оборвала ответ посреди второго замечания: первое должно сохраниться
    cut = '{"issues": [{"title": "A", "quote": "x"}, {"title": "B", "quo'
    data = parse_json(cut)
    assert data["issues"] == [{"title": "A", "quote": "x"}]


def test_quota_of_one_model_moves_to_next(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    body = {"error": {"message": "You exceeded your current quota", "details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}}
    sess = FakeSession([Resp(429, body), gemini_ok('{"ok": 3}'), gemini_ok('{"ok": 4}')])
    c = LLMClient(settings(fallback_models=("lite",)), session=sess)
    assert c.generate_json("s", "u") == {"ok": 3}
    assert not c.disabled and c.model == "lite"
    assert c.generate_json("s", "u") == {"ok": 4}     # модель без квоты больше не пробуем
    assert "/models/lite:" in sess.calls[2][0]


def test_overload_switches_model_quickly(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    sess = FakeSession([Resp(503, {"error": {"message": "high demand"}})] * 2 + [gemini_ok('{"ok": 5}')])
    c = LLMClient(settings(max_retries=4, fallback_models=("lite",)), session=sess)
    assert c.generate_json("s", "u") == {"ok": 5}
    assert len(sess.calls) == 3 and c.model == "lite"


def test_fallback_chain_uses_models_available_to_key(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)

    class Sess(FakeSession):
        def get(self, url, headers=None, timeout=None):
            names = ["gemini-2.0-flash", "gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.5-flash-image", "text-embedding-004"]
            return Resp(200, {"models": [{"name": "models/" + n, "supportedGenerationMethods": ["generateContent"]}
                                         for n in names]})
    sess = Sess([Resp(503, {"error": {"message": "high demand"}})] * 2 + [gemini_ok('{"ok": 6}')])
    c = LLMClient(settings(fallback_models=("gemini-2.5-flash-lite",)), session=sess)
    assert c.generate_json("s", "u") == {"ok": 6}
    assert c.model == "gemini-3.6-flash"   # снятая 2.5 убрана; первой — новейшая полноценная Flash


def test_second_key_used_when_quota_exhausted(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    body = {"error": {"message": "You exceeded your current quota", "details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}}
    sess = FakeSession([Resp(429, body), gemini_ok('{"ok": 7}')])
    c = LLMClient(settings(api_key="k1", extra_keys=("k2",)), session=sess)
    assert c.generate_json("s", "u") == {"ok": 7}
    assert sess.calls[0][2]["x-goog-api-key"] == "k1" and sess.calls[1][2]["x-goog-api-key"] == "k2"
