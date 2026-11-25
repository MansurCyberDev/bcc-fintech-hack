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
