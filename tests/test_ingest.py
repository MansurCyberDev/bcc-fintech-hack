import pytest

from src.ingest import IngestError, load_document
from conftest import SAMPLE_MD


def test_markdown(tmp_path):
    p = tmp_path / "spec.md"
    p.write_text(SAMPLE_MD, encoding="utf-8")
    d = load_document(p)
    assert d.fmt == "markdown"
    assert d.title == "Тестовая спецификация"
    assert d.lines[0].startswith("# ")


def test_cp1251_text(tmp_path):
    p = tmp_path / "spec.txt"
    p.write_bytes("Требование: система должна работать.".encode("cp1251"))
    d = load_document(p)
    assert "Требование" in d.text


def test_docx(tmp_path):
    docx = pytest.importorskip("docx")
    doc = docx.Document()
    doc.add_heading("Спецификация", level=1)
    doc.add_heading("Функциональные требования", level=2)
    doc.add_paragraph("FR-1. Система должна хранить лимит.")
    t = doc.add_table(rows=2, cols=3)
    t.rows[0].cells[0].text, t.rows[0].cells[1].text, t.rows[0].cells[2].text = "A", "B", "C"
    t.rows[1].cells[0].text, t.rows[1].cells[1].text, t.rows[1].cells[2].text = "1", "2", "3"
    p = tmp_path / "spec.docx"
    doc.save(p)
    d = load_document(p)
    assert "# Спецификация" in d.text
    assert "## Функциональные требования" in d.text
    assert "| 1 | 2 | 3 |" in d.text


def test_pdf(tmp_path):
    pytest.importorskip("reportlab")
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    import os
    if not os.path.exists(font):
        pytest.skip("нет шрифта с кириллицей")
    pdfmetrics.registerFont(TTFont("DejaVu", font))
    p = tmp_path / "spec.pdf"
    c = canvas.Canvas(str(p))
    c.setFont("DejaVu", 12)
    c.drawString(50, 800, "Ф-1. Система должна хранить лимит.")
    c.save()
    d = load_document(p)
    assert d.fmt == "pdf"
    assert "Система должна хранить лимит" in d.text


@pytest.mark.parametrize("name,content,msg", [
    ("missing.md", None, "не найден"),
    ("spec.xlsx", b"x", "не поддерживается"),
    ("empty.md", b"   \n\n", "не найден текст"),
])
def test_errors(tmp_path, name, content, msg):
    p = tmp_path / name
    if content is not None:
        p.write_bytes(content)
    with pytest.raises(IngestError, match=msg):
        load_document(p)


def test_pdf_structure_is_restored(tmp_path):
    """Заголовки и таблицы PDF восстанавливаются: нарезка PDF совпадает с исходным Markdown."""
    from pathlib import Path
    from src.split import split_requirements
    root = Path(__file__).resolve().parent.parent
    pdf = root / "examples" / "requirements.pdf"
    if not pdf.exists():
        pytest.skip("нет examples/requirements.pdf")
    d_pdf = load_document(pdf)
    d_md = load_document(root / "docs" / "requirements.md")
    assert "#### Функциональные требования" in d_pdf.text
    assert "| REST type | GET |" in d_pdf.text
    ids_pdf = [r.doc_id for r in split_requirements(d_pdf) if r.doc_id.startswith("Ф-")]
    ids_md = [r.doc_id for r in split_requirements(d_md) if r.doc_id.startswith("Ф-")]
    assert ids_pdf == ids_md == ["Ф-1", "Ф-2", "Ф-3", "Ф-4"]
