from src.models import Document, Issue, Quote
from src.verify import QuoteIndex, verify_issues

DOC = Document(path="x", name="x.md", fmt="markdown", text=(
    "# Заголовок\n\n* **Ф-1.** Держатель «активной» карты задаёт порог от 1 000 ₸.\n"
    "| `amount` | integer | Порог в тенге |\n"
))


def test_exact_and_normalized():
    idx = QuoteIndex(DOC)
    assert idx.find("Держатель «активной» карты задаёт порог")[0] == 3
    # другие кавычки, без markdown, другой регистр
    assert idx.find('держатель "активной" карты')[0] == 3
    assert idx.find("amount integer Порог в тенге")[0] == 4


def test_ellipsis_quote():
    assert QuoteIndex(DOC).find("Держатель … порог от 1 000")[0] == 3


def test_fuzzy_small_typo():
    line, text = QuoteIndex(DOC).find("Держатель активной карты задает порог от 1000 ₸")
    assert line == 3


def test_fabricated_quote_dropped():
    good = Issue("complete", "major", "t", "e", [Quote("задаёт порог от 1 000")])
    fake = Issue("complete", "major", "t", "e", [Quote("Система должна работать круглосуточно")])
    kept, dropped = verify_issues([good, fake], DOC)
    assert kept == [good] and dropped == 1
    assert good.quotes[0].line == 3 and good.quotes[0].verified
