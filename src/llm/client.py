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
        self.calls = 0          # всего HTTP-запросов (включая повторы)
        self.ok_calls = 0       # успешных ответов
        self.failed_in_row = 0
        self.disabled = ""      # причина, если LLM выключена до конца прогона
        self.model = settings.model
        self._last_call = 0.0
        self._dead: set[str] = set()  # модели без квоты или снятые — не пробуем повторно

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
        """Один логический запрос: повторы, переход на запасную модель, «предохранитель»."""
        if self.disabled:
            raise LLMError(f"LLM отключена в этом прогоне: {self.disabled}")
        last_err: Exception | None = None
        models = [m for m in [self.model] + [m for m in self.s.fallback_models if m != self.model]
                  if m not in self._dead]
        for idx, model in enumerate(models):
            has_next = idx + 1 < len(models)
            for attempt in range(self.s.max_retries + 1):
                if self.calls >= self.s.max_calls:
                    raise LLMError(f"Достигнут лимит вызовов LLM за прогон ({self.s.max_calls}). Увеличьте LLM_MAX_CALLS.")
                wait = self.s.min_interval - (time.monotonic() - self._last_call)
                if wait > 0 and self._last_call:
                    time.sleep(wait)
                self._last_call = time.monotonic()
                self.calls += 1
                t0 = time.monotonic()
                try:
                    out = self._gemini(system, user, model) if self.s.provider == "gemini" else self._openai(system, user)
                    log.info("LLM %s: ok за %.1f c (%s, попытка %d)", purpose, time.monotonic() - t0, model, attempt + 1)
                    self.ok_calls += 1
                    self.failed_in_row = 0
                    if model != self.model:
                        log.warning("Перехожу на модель %s", model)
                        self.model = model
                    return out
                except _QuotaExhausted as e:
                    # дневная квота этой модели исчерпана: повторы бессмысленны, пробуем следующую модель
                    last_err = e
                    self._dead.add(model)
                    log.warning("LLM %s: у модели %s исчерпана квота", purpose, model)
                    if not has_next:
                        self.disabled = str(e)
                        raise LLMError(str(e)) from e
                    break
                except _ModelUnavailable as e:
                    last_err = e
                    self._dead.add(model)
                    log.warning("LLM %s: модель %s недоступна: %s", purpose, model, e)
                    break  # к следующей модели
                except _Retryable as e:
                    last_err = e
                    # перегрузка (503): не ждём долго, если есть другая модель — переходим к ней
                    if attempt >= self.s.max_retries or (has_next and e.overloaded and attempt >= 1):
                        break
                    delay = min(30.0, e.retry_after or 2 ** (attempt + 1) + random.uniform(0, 1))
                    log.warning("LLM %s: %s; повтор через %.0f c", purpose, e, delay)
                    time.sleep(delay)
            if self.s.provider != "gemini":
                break
        self.failed_in_row += 1
        if self.failed_in_row >= 2:
            self.disabled = f"два запроса подряд не удались ({last_err})"
        raise LLMError(f"LLM недоступна после повторов: {last_err}")

    def _gemini(self, system: str, user: str, model: str) -> str:
        url = f"{self.s.base_url}/models/{model}:generateContent"
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
        if r.status_code == 429 and re.search(r"PerDay|per day|daily|exceeded your current quota|billing", r.text or "", re.I) \
                and not re.search(r"PerMinute|per minute", r.text or "", re.I):
            raise _QuotaExhausted(f"квота ключа исчерпана (HTTP 429): {msg}. Подождите сброса квоты или используйте другой ключ.")
        if r.status_code == 404 or (r.status_code == 400 and re.search(r"model", msg, re.I) and
                                    re.search(r"not found|not supported|no longer available", msg, re.I)):
            raise _ModelUnavailable(f"HTTP {r.status_code}: {msg}")
        if r.status_code == 429 or r.status_code >= 500:
            raise _Retryable(f"HTTP {r.status_code}: {msg}", retry_after=_retry_after(r),
                             overloaded=r.status_code == 503)
        if r.status_code in (401, 403):
            raise LLMError(f"Ключ API отклонён (HTTP {r.status_code}): {msg}")
        raise LLMError(f"Ошибка LLM API (HTTP {r.status_code}): {msg}")


class _QuotaExhausted(Exception):
    pass


class _ModelUnavailable(Exception):
    pass


class _Retryable(Exception):
    def __init__(self, msg: str, retry_after: float | None = None, overloaded: bool = False):
        super().__init__(msg)
        self.retry_after = retry_after
        self.overloaded = overloaded


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
    salvaged = _salvage(t)
    if salvaged is not None:
        return salvaged
    raise ValueError("Ответ модели не является JSON")


def _salvage(t: str) -> Any:
    """Обрезанный JSON (модель оборвала ответ): отрезаем хвост до последнего целого объекта и закрываем скобки."""
    a = t.find("{")
    if a < 0:
        return None
    body = t[a:]
    ends = [i for i, ch in enumerate(body) if ch == "}"][::-1][:60]
    for i in ends:
        cand = body[:i + 1]
        stack = []
        in_str = esc = False
        for ch in cand:
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]" and stack:
                stack.pop()
        if in_str:
            continue
        try:
            return json.loads(cand + "".join(reversed(stack)))
        except json.JSONDecodeError:
            continue
    return None
