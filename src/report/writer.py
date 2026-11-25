"""Запись отчёта на диск: .md (основной), .html и .json (машиночитаемый)."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from ..models import Report
from . import html as html_report
from . import markdown as md_report


def to_dict(report: Report) -> dict:
    d = dataclasses.asdict(report)
    d["document"] = {k: v for k, v in d["document"].items() if k != "text"}
    d["rejected"] = [{"issue": dataclasses.asdict(i), "reason": why} for i, why in report.rejected]
    return d


def write_report(report: Report, out_path: str | Path, html: bool = True, json_out: bool = True) -> list[Path]:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    md_text = md_report.render(report)
    written = []
    suffix = out.suffix.lower()
    if suffix in (".html", ".htm"):
        out.write_text(html_report.render(report, md_text), encoding="utf-8")
        written.append(out)
        md_path = out.with_suffix(".md")
        md_path.write_text(md_text, encoding="utf-8")
        written.append(md_path)
    elif suffix == ".json":
        out.write_text(json.dumps(to_dict(report), ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(out)
    else:
        out.write_text(md_text, encoding="utf-8")
        written.append(out)
        if html:
            p = out.with_suffix(".html")
            p.write_text(html_report.render(report, md_text), encoding="utf-8")
            written.append(p)
    if json_out and suffix != ".json":
        p = out.with_suffix(".json")
        p.write_text(json.dumps(to_dict(report), ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(p)
    return written
