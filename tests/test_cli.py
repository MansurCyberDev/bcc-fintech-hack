import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def cli(*args, cwd=ROOT):
    return subprocess.run([sys.executable, str(ROOT / "main.py"), *args], capture_output=True, text=True, cwd=cwd,
                          env={"PATH": "/usr/bin:/bin", "LLM_PROVIDER": "none", "PYTHONIOENCODING": "utf-8"})


def test_offline_run_on_organizers_doc(tmp_path):
    out = tmp_path / "r" / "report.md"
    res = cli(str(ROOT / "docs" / "requirements.md"), str(out), "--offline")
    assert res.returncode == 0, res.stderr
    assert out.exists() and out.with_suffix(".html").exists() and out.with_suffix(".json").exists()
    text = out.read_text(encoding="utf-8")
    assert "Паспорт прогона" in text and "Итог" in text


def test_missing_file(tmp_path):
    res = cli(str(tmp_path / "nope.md"), str(tmp_path / "r.md"))
    assert res.returncode == 1
    assert "не найден" in res.stderr


def test_usage():
    res = cli()
    assert res.returncode == 2
