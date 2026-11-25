"""HTML-версия отчёта: тот же Markdown, отрисованный в самодостаточную страницу."""
from __future__ import annotations

import html as _html

import markdown

from ..models import Report

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1b1f24;--muted:#5b6470;--line:#e3e6ea;--accent:#0b6e4f;
--crit:#c62828;--major:#e67700;--minor:#b59400;--quote:#f1f5f3}
@media (prefers-color-scheme:dark){:root{--bg:#111417;--card:#191d21;--text:#e7eaee;--muted:#9aa4ae;
--line:#2a3036;--accent:#3ccf96;--quote:#1e2522}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 -apple-system,"Segoe UI",Roboto,Arial,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:26px;line-height:1.25;margin:8px 0 16px}
h2{font-size:20px;margin:40px 0 12px;padding-top:12px;border-top:1px solid var(--line)}
h3{font-size:16.5px;margin:28px 0 8px}
h4{font-size:15px;margin:18px 0 6px}
blockquote{margin:8px 0;padding:8px 14px;background:var(--quote);border-left:3px solid var(--accent);border-radius:4px}
blockquote p{margin:2px 0}
table{border-collapse:collapse;width:100%;margin:10px 0;font-size:13.5px;display:block;overflow-x:auto}
th,td{border:1px solid var(--line);padding:6px 8px;vertical-align:top;text-align:left}
th{background:var(--card);position:sticky;top:0}
tr:nth-child(even) td{background:color-mix(in srgb,var(--card) 60%,transparent)}
code{background:var(--quote);padding:1px 4px;border-radius:3px;font-size:13px}
details{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px 12px;margin:10px 0}
summary{cursor:pointer;color:var(--muted)}
em{color:var(--muted)}
nav{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line);
padding:8px 16px;display:flex;gap:14px;flex-wrap:wrap;font-size:13.5px}
nav a{color:var(--accent);text-decoration:none}
@media print{nav{display:none}body{background:#fff}}
"""

NAV = [
    ("Паспорт", "1"), ("Нарезка", "2"), ("Требования", "3"), ("Набор", "4"),
    ("Вопросы", "5"), ("Сводка", "6"), ("Итог", "7"),
]


def render(report: Report, md_text: str) -> str:
    # Текст документа попадает в отчёт: экранируем HTML, чтобы документ не мог внедрить скрипт.
    safe = md_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for tag in ("details", "summary"):
        safe = safe.replace(f"&lt;{tag}&gt;", f"<{tag}>").replace(f"&lt;/{tag}&gt;", f"</{tag}>")
    safe = safe.replace("&lt;details&gt;&lt;summary&gt;", "<details><summary>")
    # цитаты markdown начинаются с «>», возвращаем их в начале строк
    safe = "\n".join(("> " + l[5:]) if l.startswith("&gt; ") else (">" if l == "&gt;" else l)
                     for l in safe.split("\n"))
    body = markdown.markdown(safe, extensions=["tables", "md_in_html", "toc", "sane_lists"],
                             output_format="html5")
    title = _html.escape(f"Отчёт: {report.document.title or report.document.name}")
    nav = "".join(f'<a href="#{_anchor(n)}">{label}</a>' for label, n in NAV)
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{CSS}</style></head>
<body><nav>{nav}</nav><main>{body}</main>
<script>
// якоря для навигации: id у заголовков h2 по номеру раздела
document.querySelectorAll('h2').forEach(h=>{{const m=h.textContent.match(/^(\\d+)\\./);if(m)h.id='s'+m[1];}});
</script></body></html>"""


def _anchor(n: str) -> str:
    return "s" + n
