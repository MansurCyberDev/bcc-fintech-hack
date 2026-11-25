"""Централизованная конфигурация. Значения берутся из .env / переменных окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = ROOT / "prompts"


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(override=False)  # .env в текущей папке


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    provider: str            # gemini | openai | none
    api_key: str
    model: str
    base_url: str
    timeout: int
    max_retries: int
    max_calls: int           # ограничение числа запросов к LLM за прогон (контроль стоимости/квот)
    batch_size: int          # сколько требований проверять за один запрос
    temperature: float
    max_doc_chars: int       # сколько символов документа максимум отдаём модели
    min_interval: float      # пауза между запросами (бесплатный тариф Gemini ограничивает RPM)
    fallback_models: tuple = ()  # запасные модели, если основная недоступна/перегружена
    extra_keys: tuple = ()       # запасные ключи: переход на следующий, когда у ключа кончилась квота

    @property
    def llm_enabled(self) -> bool:
        return self.provider != "none" and bool(self.api_key)

    @property
    def model_label(self) -> str:
        if not self.llm_enabled:
            return "не использовалась (режим только правил)"
        return f"{self.provider}:{self.model}"


def load_settings() -> Settings:
    _load_dotenv()
    provider = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
    if provider == "gemini":
        keys = [k.strip() for k in (os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")).split(",")
                + os.getenv("GEMINI_API_KEYS", "").split(",") if k.strip()]
        keys = list(dict.fromkeys(keys))
        key = keys[0] if keys else ""
        # «-latest» — псевдоним Google на актуальную Flash-модель: не ломается, когда старые версии снимают
        model = os.getenv("GEMINI_MODEL", "") or "gemini-flash-latest"
        fallbacks = os.getenv("GEMINI_FALLBACK_MODELS", "gemini-flash-lite-latest,gemini-flash-latest")
        base = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")
    elif provider in ("openai", "openai-compatible"):
        provider = "openai"
        key = os.getenv("OPENAI_API_KEY", "")
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        fallbacks = os.getenv("OPENAI_FALLBACK_MODELS", "")
        base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    else:
        provider, key, model, base, fallbacks = "none", "", "", "", ""
    if provider != "gemini":
        keys = [key] if key else []
    return Settings(
        provider=provider,
        api_key=key.strip(),
        model=model.strip(),
        base_url=base.rstrip("/"),
        timeout=_int("LLM_TIMEOUT", 120),
        max_retries=_int("LLM_MAX_RETRIES", 3),
        max_calls=_int("LLM_MAX_CALLS", 60),
        batch_size=max(1, _int("LLM_BATCH_SIZE", 12)),
        temperature=_float("LLM_TEMPERATURE", 0.1),
        max_doc_chars=_int("LLM_MAX_DOC_CHARS", 150_000),
        min_interval=_float("LLM_MIN_INTERVAL", 4.0),
        fallback_models=tuple(m.strip() for m in fallbacks.split(",") if m.strip()),
        extra_keys=tuple(keys[1:]),
    )
