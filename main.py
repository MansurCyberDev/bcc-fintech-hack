#!/usr/bin/env python3
"""Агент проверки требований. Запуск одной командой:

    python main.py docs/requirements.md reports/report.md

Первый аргумент — документ (.md, .pdf, .docx, .txt), второй — куда записать отчёт.
Рядом с отчётом .md появятся .html и .json.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from src.config import load_settings
from src.ingest import IngestError
from src.llm.client import LLMClient, LLMError
from src.pipeline import run
from src.report.writer import write_report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="ИИ-агент статической проверки требований (ISO/IEC/IEEE 29148, IEEE 830). Отчёт на русском.",
    )
    p.add_argument("input", nargs="?", help="путь к документу требований (.md, .pdf, .docx, .txt)")
    p.add_argument("output", nargs="?", help="путь к файлу отчёта (.md, .html или .json)")
    p.add_argument("--offline", action="store_true", help="не обращаться к LLM: только правила (для проверки без ключа)")
    p.add_argument("--no-critic", action="store_true", help="не запускать критика (экономит 1 запрос к LLM)")
    p.add_argument("-i", "--interactive", action="store_true",
                   help="задать уточняющие вопросы в терминале и учесть ответы в отчёте")
    p.add_argument("--answers", metavar="FILE", help='JSON с ответами автора: {"Q-01": "ответ", ...}')
    p.add_argument("--no-html", action="store_true", help="не создавать HTML-версию отчёта")
    p.add_argument("--no-json", action="store_true", help="не создавать JSON-версию отчёта")
    p.add_argument("--list-models", action="store_true", help="показать модели, доступные по вашему ключу")
    p.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S",
    )
    settings = load_settings()

    if args.list_models:
        if not settings.llm_enabled:
            print("Ключ LLM не найден. Скопируйте .env.example в .env и укажите GEMINI_API_KEY.", file=sys.stderr)
            return 2
        try:
            for m in LLMClient(settings).list_models():
                print(m)
        except LLMError as e:
            print(f"Ошибка: {e}", file=sys.stderr)
            return 1
        return 0

    if not args.input or not args.output:
        print("Использование: python main.py <документ> <отчёт.md>   (подробнее: python main.py -h)", file=sys.stderr)
        return 2

    use_llm = not args.offline
    if use_llm and not settings.llm_enabled:
        print("⚠ Ключ LLM не найден (GEMINI_API_KEY в .env). Работаю в режиме «только правила».\n"
              "  Смысловые проверки (противоречия, полнота) будут неполными. См. README → «Ключ LLM».",
              file=sys.stderr)

    started = time.monotonic()
    try:
        report = run(
            args.input, settings, use_llm=use_llm, use_critic=not args.no_critic,
            interactive=args.interactive, answers_path=args.answers,
            progress=lambda s: print(f"[{time.monotonic() - started:5.1f}s] {s}", file=sys.stderr),
        )
    except IngestError as e:
        print(f"Ошибка чтения документа: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nПрервано пользователем.", file=sys.stderr)
        return 130

    paths = write_report(report, args.output, html=not args.no_html, json_out=not args.no_json)
    real = [r for r in report.requirements if r.kind != "context"]
    crit = sum(1 for i in report.issues if i.severity == "critical")
    print(f"\nГотово за {report.run.duration_sec} с. Требований: {len(real)}, дефектов: {len(report.issues)} "
          f"(критичных: {crit}), вопросов автору: {len(report.questions)}.")
    print(f"Вердикт: {'можно начинать разработку' if report.verdict.ready_for_dev else 'разработку начинать НЕЛЬЗЯ'}.")
    if report.run.llm_errors:
        print(f"⚠ Ошибок LLM: {len(report.run.llm_errors)} (см. паспорт отчёта).")
    print("Отчёт:")
    for p in paths:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
