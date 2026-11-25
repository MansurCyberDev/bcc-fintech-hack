from src.ingest import load_document
from src.split import split_requirements
from conftest import SAMPLE_MD


def _reqs(tmp_path, text=SAMPLE_MD):
    p = tmp_path / "spec.md"
    p.write_text(text, encoding="utf-8")
    d = load_document(p)
    return d, split_requirements(d)


def test_ids_kept_and_lines(tmp_path):
    d, reqs = _reqs(tmp_path)
    by_doc = {r.doc_id: r for r in reqs}
    assert "FR-1" in by_doc and "FR-2" in by_doc
    fr1 = by_doc["FR-1"]
    assert fr1.category == "Функциональное"
    assert "быстро показывать баланс" in fr1.text
    assert "FR-1" in d.lines[fr1.line_start - 1]


def test_report_ids_sequential(tmp_path):
    _, reqs = _reqs(tmp_path)
    real = [r for r in reqs if r.kind != "context"]
    assert [r.rid for r in real] == [f"R-{i:02d}" for i in range(1, len(real) + 1)]


def test_tables_and_categories(tmp_path):
    _, reqs = _reqs(tmp_path)
    kinds = {r.kind for r in reqs}
    assert "acceptance" in kinds and "api" in kinds
    nfr = [r for r in reqs if r.category == "Нефункциональное"]
    assert nfr and nfr[0].doc_id == "—"
    # таблица «поле — значение» — одна единица
    kv = [r for r in reqs if "REST type" in r.text]
    assert len(kv) == 1


def test_no_ids_document(tmp_path):
    text = "# Док\n\n## Требования\n\nСистема хранит лимит.\n\nСистема отправляет SMS.\n"
    _, reqs = _reqs(tmp_path, text)
    real = [r for r in reqs if r.kind != "context"]
    assert len(real) == 2
    assert all(r.doc_id == "—" for r in real)
