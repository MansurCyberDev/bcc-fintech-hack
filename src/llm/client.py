"""Клиент LLM: Gemini (REST API Google AI Studio) или любой OpenAI-совместимый сервер.

- ответы только в JSON, с проверкой и одной попыткой «починить» невалидный JSON;
- повторы при 429/5xx/сетевых ошибках с экспоненциальной задержкой;
- лимит числа вызовов за прогон (контроль квоты бесплатного тарифа);
- ключ никогда не пишется в лог.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Any

import requests

from ..config import Settings

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.s = settings
        self.http = session or requests.Session()
        self.calls = 0
        self._last_call = 0.0

    # ------------------------------------------------------------------ public
    def generate_json(self, system: str, user: str, purpose: str = "") -> Any:
        """Отправляет запрос и возвращает разобранный JSON (dict/list)."""
        text = self._generate(system, user, purpose)
        try:
            return parse_json(text)
        except ValueError:
            log.warning("LLM вернула невалидный JSON (%s), прошу исправить", purpose)
            fix = self._generate(
                system,
                user + "\n\nТвой предыдущий ответ не был валидным JSON:\n" + text[:4000]
                + "\n\nВерни ТОЛЬКО валидный JSON по той же схеме, без пояснений.",
                purpose + " (repair)",
            )
            return parse_json(fix)

    def list_models(self) -> list[str]:
        if self.s.provider == "gemini":
            r = self.http.get(f"{self.s.base_url}/models", headers={"x-goog-api-key": self.s.api_key},
                              timeout=self.s.timeout)
            self._raise_for(r)
            return [m["name"].removeprefix("models/") for m in r.json().get("models", [])
                    if "generateContent" in m.get("supportedGenerationMethods", [])]
        r = self.http.get(f"{self.s.base_url}/models", headers={"Authorization": f"Bearer {self.s.api_key}"},
                          timeout=self.s.timeout)
        self._raise_for(r)
        return [m.get("id", "") for m in r.json().get("data", [])]

    # ----------------------------------------------------------------- private
    def _generate(self, system: str, user: str, purpose: str) -> str:
        if self.calls >= self.s.max_calls:
            raise LLMError(f"Достигнут лимит вызовов LLM за прогон ({self.s.max_calls}). Увеличьте LLM_MAX_CALLS.")
        last_err: Exception | None = None
        for attempt in range(self.s.max_retries + 1):
            wait = self.s.min_interval - (time.monotonic() - self._last_call)
            if wait > 0 and self._last_call:
                time.sleep(wait)
            self._last_call = time.monotonic()
            self.calls += 1
            t0 = time.monotonic()
            try:
                if self.s.provider == "gemini":
                    out = self._gemini(system, user)
                else:
                    out = self._openai(system, user)
                log.info("LLM %s: ok за %.1f c (попытка %d)", purpose, time.monotonic() - t0, attempt + 1)
                return out
            except _Retryable as e:
                last_err = e
                delay = e.retry_after or min(60.0, 2 ** (attempt + 1)) + random.uniform(0, 1)
                log.warning("LLM %s: %s; повтор через %.0f c", purpose, e, delay)
                if attempt < self.s.max_retries:
                    time.sleep(delay)
                if self.calls >= self.s.max_calls:
                    break
        raise LLMError(f"LLM недоступна после повторов: {last_err}")

    def _gemini(self, system: str, user: str) -> str:
        url = f"{self.s.base_url}/models/{self.s.model}:generateContent"
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": self.s.temperature,
                "responseMimeType": "application/json",
            },
        }
        try:
            r = self.http.post(url, json=body, headers={"x-goog-api-key": self.s.api_key}, timeout=self.s.timeout)
        except requests.RequestException as e:
            raise _Retryable(f"сетевая ошибка: {type(e).__name__}") from e
        self._raise_for(r)
        data = r.json()
        cands = data.get("candidates") or []
        if not cands:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "пустой ответ")
            raise LLMError(f"Gemini не вернула ответ: {reason}")
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text.strip():
            raise _Retryable(f"пустой текст ответа (finishReason={cands[0].get('finishReason')})")
        return text

    def _openai(self, system: str, user: str) -> str:
        url = f"{self.s.base_url}/chat/completions"
        body = {
            "model": self.s.model,
            "temperature": self.s.temperature,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        try:
            r = self.http.post(url, json=body, headers={"Authorization": f"Bearer {self.s.api_key}"},
                               timeout=self.s.timeout)
        except requests.RequestException as e:
            raise _Retryable(f"сетевая ошибка: {type(e).__name__}") from e
        self._raise_for(r)
        try:
            return r.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError("Неожиданный формат ответа OpenAI-совместимого API") from e

    @staticmethod
    def _raise_for(r: requests.Response) -> None:
        if r.status_code < 300:
            return
        msg = _error_message(r)
        if r.status_code == 429 or r.status_code >= 500:
            raise _Retryable(f"HTTP {r.status_code}: {msg}", retry_after=_retry_after(r))
        if r.status_code in (401, 403):
            raise LLMError(f"Ключ API отклонён (HTTP {r.status_code}): {msg}")
        if r.status_code == 404:
            raise LLMError(f"Модель или адрес не найдены (HTTP 404): {msg}. Проверьте GEMINI_MODEL/OPENAI_MODEL.")
        raise LLMError(f"Ошибка LLM API (HTTP {r.status_code}): {msg}")


class _Retryable(Exception):
    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


def _error_message(r: requests.Response) -> str:
    try:
        j = r.json()
        if isinstance(j, list) and j:
            j = j[0]
        return str((j.get("error") or {}).get("message", ""))[:300] or r.text[:300]
    except ValueError:
        return r.text[:300]


def _retry_after(r: requests.Response) -> float | None:
    h = r.headers.get("Retry-After")
    if h and h.isdigit():
        return min(float(h), 90.0)
    # Gemini кладёт retryDelay в details: "retryDelay": "31s"
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', r.text or "")
    if m:
        return min(float(m.group(1)) + 1, 90.0)
    return None


def parse_json(text: str) -> Any:
    """Достаёт JSON из ответа модели (снимает ```json ... ``` и мусор вокруг)."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    # первый { ... последний }
    for open_, close in (("{", "}"), ("[", "]")):
        a, b = t.find(open_), t.rfind(close)
        if a >= 0 and b > a:
            try:
                return json.loads(t[a:b + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("Ответ модели не является JSON")
