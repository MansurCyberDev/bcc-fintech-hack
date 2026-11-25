# Требования и матрица соответствия

Источник: **ТЗ** — README задания организаторов; **САЙТ** — критерии жюри bcchubagents.mnu.kz; **КОМ** — решения команды.
Статусы: Verified — проверено запуском/тестом; Implemented — реализовано, но не проверено вживую.

| ID | Источник | Требование | Где реализовано | Как проверено | Статус |
| :- | :- | :--- | :--- | :--- | :- |
| RQ-01 | ТЗ §3, §7 | Запуск одной командой: путь к документу и путь к отчёту | `main.py` | `tests/test_cli.py`, ручной прогон | Verified |
| RQ-02 | ТЗ §3 | Вход PDF (+ другие форматы) | `src/ingest.py` | `test_ingest.py` (md, txt/cp1251, docx, pdf, структура PDF) | Verified |
| RQ-03 | ТЗ §3 | Выделение отдельных требований | `src/split.py` | `test_split.py`; нарезка PDF = нарезке MD | Verified |
| RQ-04 | ТЗ §3, §5 | Проверка каждого требования по ISO 29148 5.2.5 / IEEE 830 4.3 | `checks/rules.py`, `checks/llm_checks.py` | `test_rules.py`, `test_pipeline.py` (поддельная LLM) | Verified (правила); LLM — см. TEST_REPORT |
| RQ-05 | ТЗ §3, §5 | Проверка набора целиком (5.2.6) | `check_set`, `check_set_llm` | те же тесты | Verified (правила); LLM — см. TEST_REPORT |
| RQ-06 | ТЗ §4 | Обращение к LLM (Gemini бесплатно или любая) | `src/llm/client.py` | `test_llm_client.py` (повторы, ошибки ключа, JSON, OpenAI-совм.) | Verified (на поддельном сервере); реальный вызов — см. TEST_REPORT |
| RQ-07 | ТЗ §6 | Отчёт на русском в файл | `src/report/` | `test_pipeline.py`, `test_cli.py` | Verified |
| RQ-08 | ТЗ §6 | Паспорт: документ, дата, модель, версия агента (коммит) | `report/markdown.py`, `pipeline.agent_version` | тест проверяет модель и версию | Verified |
| RQ-09 | ТЗ §6 | Таблица нарезки «ID отчёта — ID документа — формулировка», свои ID + цитата при отсутствии | `report/markdown.py` §2 | `test_pipeline.py` | Verified |
| RQ-10 | ТЗ §6 | Сводка: число требований, нарушений, список критичных | §6 отчёта | `test_pipeline.py` | Verified |
| RQ-11 | ТЗ §6 | Итог: можно ли начинать разработку/тестирование, что исправить первым | `pipeline.make_verdict`, §7 | `test_pipeline.py` | Verified |
| RQ-12 | ТЗ §6 | Каждое нарушение ссылается на конкретное место | `src/verify.py` | `test_verify.py`, `test_pipeline.py` (выдуманная цитата отброшена) | Verified |
| RQ-13 | ТЗ §7 | README: ключ, куда положить, команда, где отчёт | `README.md` | вычитка | Verified |
| RQ-14 | ТЗ §7 | `.env.example`, секреты не в репозитории | `.env.example`, `.gitignore` | `git ls-files` | Verified |
| RQ-15 | ТЗ §7 | Файл зависимостей | `requirements.txt` | установка в чистом окружении | Verified |
| RQ-16 | ТЗ §2 | Отчёт по выданному документу, сформированный агентом | `reports/` | прогон агента | см. TEST_REPORT |
| RQ-17 | САЙТ | Уточняющие вопросы автору спецификации | `src/clarify.py`, §5 отчёта | `test_pipeline.py`; `-i`, `--answers`, веб | Verified |
| RQ-18 | САЙТ | Архитектура агентов: явные роли | `docs/ARCHITECTURE.md` | — | Implemented |
| RQ-19 | КОМ | Работа без ключа (только правила) | `pipeline.run(use_llm=False)` | `test_cli.py` (offline) | Verified |
| RQ-20 | КОМ | Не зашивать ответы под документ организаторов | общие правила/промпты | тесты на других документах | Verified |
| RQ-21 | КОМ | Устойчивость к ошибкам LLM | `client.py`, `pipeline.py` | `test_llm_failure_degrades_gracefully` | Verified |
| RQ-22 | КОМ | Веб-интерфейс для демо | `web.py` | e2e в браузере (Playwright) | Verified |
| RQ-23 | КОМ | Защита от XSS в HTML-отчёте и prompt injection | `report/html.py`, `llm_checks.numbered_document` | тест | Verified |
