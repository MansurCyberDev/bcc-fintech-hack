from src.checks.rules import check_requirement, check_set
from src.ingest import load_document
from src.split import split_requirements
from src.verify import verify_issues
from conftest import SAMPLE_MD


def _run(tmp_path, text=SAMPLE_MD):
    p = tmp_path / "spec.md"
    p.write_text(text, encoding="utf-8")
    d = load_document(p)
    reqs = split_requirements(d)
    issues = []
    for r in reqs:
        if r.kind != "context":
            issues += check_requirement(d, r)
    issues += check_set(d, reqs)
    return d, reqs, issues


def test_rules_find_generic_defects(tmp_path):
    _, _, issues = _run(tmp_path)
    ids = {i.rule_id for i in issues}
    assert "R-VAGUE" in ids              # «быстро», «и т.д.»
    assert "R-HTTP-GET-MUTATION" in ids  # GET для «Создание»
    assert "R-SET-UNITS" in ids          # копейки и рубли
    assert "R-SET-VERSION" in ids        # нет версии
    assert "R-SET-API-TYPES" in ids      # limit integer vs string
    assert "R-EMPTY-FIELD" in ids        # Swagger: —
    assert "R-ATOMIC" in ids             # FR-2: 4 утверждения
    assert "R-BOUNDARY" in ids           # от 100 до 5 000 без граничных тестов


def test_all_rule_quotes_are_real(tmp_path):
    d, _, issues = _run(tmp_path)
    kept, dropped = verify_issues(issues, d)
    assert dropped == 0
    assert all(q.line for i in kept for q in i.quotes)


def test_clean_document_has_few_findings(tmp_path):
    text = ("# Спецификация\n\nВерсия 1.2 от 01.11.2026\n\n## Функциональные требования\n\n"
            "* **FR-1.** Приоритет: высокий. Система должна отправлять SMS не позднее 30 секунд после покупки.\n\n"
            "## Нефункциональные требования\n\n* **NFR-1.** Доступность 99,9 % в месяц.\n\n"
            "## Критерии приемки\n\n| Условие | Действие | Результат |\n| :--- | :--- | :--- |\n"
            "| Покупка | Провести | SMS за 30 с |\n")
    _, _, issues = _run(tmp_path, text)
    assert not [i for i in issues if i.severity == "critical"]
    assert "R-SET-VERSION" not in {i.rule_id for i in issues}


API_ENABLE_MD = """# Подписка

## Часть B. REST API

### Метод подключения подписки

| Поле | Значение |
| :--- | :--- |
| Description | Подключение подписки |
| REST type | GET |
| Request URL | /sub/enable |

#### 200. Successful
Подписка подключена.

#### 500. Error
Внутренняя ошибка.
"""


def test_get_for_enable_and_missing_4xx(tmp_path):
    _, _, issues = _run(tmp_path, API_ENABLE_MD)
    ids = {i.rule_id for i in issues}
    assert "R-HTTP-GET-MUTATION" in ids   # «Подключение» меняет состояние
    assert "R-SET-API-NO-4XX" in ids      # есть 200 и 500, но ни одного 4xx
