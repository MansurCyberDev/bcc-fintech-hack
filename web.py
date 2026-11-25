#!/usr/bin/env python3
"""Веб-интерфейс для демонстрации: загрузить документ -> получить отчёт -> ответить на вопросы агента.

    python web.py            # http://127.0.0.1:8000

Работает на стандартной библиотеке Python (без Flask). Слушает только localhost.
Основной способ сдачи — CLI (main.py); веб-интерфейс использует тот же пайплайн.
"""
from __future__ import annotations

import argparse
import html
import logging
import secrets
import sys
import tempfile
import threading
import time
from email.parser import BytesParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from src.config import load_settings
from src.ingest import SUPPORTED, IngestError
from src.llm.client import LLMClient
from src.pipeline import apply_report_answers, run
from src.report import html as html_report
from src.report import markdown as md_report
from src.report.writer import to_dict

MAX_UPLOAD = 10 * 1024 * 1024
JOBS: dict[str, dict] = {}       # id -> {status, report, error, progress, started}
LOCK = threading.Lock()
WORKDIR = Path(tempfile.mkdtemp(prefix="arv-web-"))
SETTINGS = load_settings()

PAGE_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1b1f24;--muted:#5b6470;--line:#e3e6ea;--accent:#0b6e4f;--accent-2:#e8f3ee;--err:#c62828}
@media (prefers-color-scheme:dark){:root{--bg:#111417;--card:#191d21;--text:#e7eaee;--muted:#9aa4ae;--line:#2a3036;--accent:#3ccf96;--accent-2:#16271f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 -apple-system,"Segoe UI",Roboto,Arial,sans-serif}
main{max-width:760px;margin:0 auto;padding:40px 16px 64px}
h1{font-size:26px;margin:0 0 6px}p.lead{color:var(--muted);margin:0 0 24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px;margin:0 0 16px}
label{display:block;font-weight:600;margin:0 0 6px}
input[type=file]{width:100%;padding:14px;border:1.5px dashed var(--line);border-radius:8px;background:var(--bg);color:var(--text)}
.row{display:flex;gap:10px;align-items:center;margin:14px 0 0;color:var(--muted);font-size:14px}
button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:11px 18px;font-size:15px;font-weight:600;cursor:pointer}
button:disabled{opacity:.6;cursor:progress}
.muted{color:var(--muted);font-size:13.5px}.err{color:var(--err)}
.pill{display:inline-block;padding:2px 8px;border-radius:99px;background:var(--accent-2);color:var(--accent);font-size:12.5px;font-weight:600}
.q{border-top:1px solid var(--line);padding:14px 0}.q:first-child{border-top:0}
.q textarea{width:100%;min-height:54px;margin-top:6px;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--text);font:inherit}
.opts button{background:var(--accent-2);color:var(--accent);padding:4px 10px;font-size:13px;margin:4px 6px 0 0}
.bar{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}.bar a{color:var(--accent)}
ol.steps{padding-left:20px;color:var(--muted)}
"""


def page(title: str, body: str, refresh: int | None = None) -> bytes:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">{meta}'
            f'<title>{html.escape(title)}</title><style>{PAGE_CSS}</style></head>'
            f'<body><main>{body}</main></body></html>').encode("utf-8")


def index_page(error: str = "") -> bytes:
    mode = (f'Модель: <span class="pill">{html.escape(SETTINGS.model_label)}</span>' if SETTINGS.llm_enabled else
            '<span class="pill">LLM не подключена</span> — будут только проверки правилами. '
            'Добавьте GEMINI_API_KEY в .env и перезапустите.')
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    exts = ", ".join(sorted(SUPPORTED))
    return page("Проверка требований", f"""
<h1>Проверка требований</h1>
<p class="lead">Загрузите документ с требованиями. Агент разобьёт его на требования, проверит каждое и весь набор
по ISO/IEC/IEEE 29148 и IEEE 830, подтвердит каждую цитату и подготовит вопросы автору.</p>
<form class="card" method="post" action="/check" enctype="multipart/form-data"
      onsubmit="this.querySelector('button').disabled=true;this.querySelector('button').textContent='Проверяю…'">
  <label for="f">Документ ({exts})</label>
  <input id="f" type="file" name="file" accept="{exts}" required>
  <div class="row"><input type="checkbox" id="off" name="offline" value="1"><label for="off" style="margin:0;font-weight:400">
  Только правила, без LLM (быстро, без ключа)</label></div>
  <div class="row"><button type="submit">Проверить</button><span>{mode}</span></div>
  {err}
</form>
<div class="card"><b>Что будет в отчёте</b><ol class="steps">
<li>Паспорт прогона и таблица нарезки на требования</li><li>Проверка каждого требования и набора целиком</li>
<li>Уточняющие вопросы автору — на них можно ответить прямо здесь</li><li>Сводка и вердикт: можно ли начинать разработку</li></ol></div>
""")


def job_page(jid: str) -> tuple[bytes, int | None]:
    job = JOBS.get(jid)
    if not job:
        return page("Не найдено", '<p>Проверка не найдена. <a href="/">Назад</a></p>'), None
    if job["status"] == "running":
        el = int(time.time() - job["started"])
        return page("Проверяю…", f"""<h1>Проверяю документ…</h1>
<div class="card"><p>{html.escape(job['progress'])}</p><p class="muted">Прошло {el} с. Страница обновится сама.
С LLM проверка занимает 1–3 минуты (ограничения бесплатного тарифа).</p></div>"""), 2
    if job["status"] == "error":
        return page("Ошибка", f'<h1>Не получилось</h1><div class="card err">{html.escape(job["error"])}</div>'
                              f'<p><a href="/">Попробовать снова</a></p>'), None
    rep = job["report"]
    real = [r for r in rep.requirements if r.kind != "context"]
    crit = sum(i.severity == "critical" for i in rep.issues)
    verdict = "✅ Можно начинать разработку" if rep.verdict.ready_for_dev else "❌ Разработку начинать нельзя"
    qs = []
    for q in rep.questions:
        opts = "".join(
            f'<button type="button" onclick="this.closest(\'.q\').querySelector(\'textarea\').value=this.textContent">'
            f'{html.escape(o)}</button>' for o in q.options)
        qs.append(f"""<div class="q"><b>{q.qid}{' 🚫' if q.blocking else ''}</b> {html.escape(q.text)}
<div class="muted">Кому: {html.escape(q.addressee)}{(' · ' + html.escape(q.why)) if q.why else ''}</div>
<div class="opts">{opts}</div><textarea name="{q.qid}" placeholder="Ответ автора (можно оставить пустым)">{html.escape(q.answer)}</textarea></div>""")
    answers = "".join(qs) or "<p>Вопросов нет.</p>"
    res = ""
    if rep.resolutions:
        closed = sum(len(r.resolved_issue_ids) for r in rep.resolutions)
        res = f'<p class="pill">Ответы учтены: закрыто дефектов — {closed}</p>'
    return page("Отчёт готов", f"""<h1>{html.escape(verdict)}</h1>
<p class="lead">{html.escape(rep.verdict.summary)}</p>
<div class="card"><b>{len(real)}</b> требований · <b>{len(rep.issues)}</b> дефектов (критичных: {crit}) ·
<b>{len(rep.questions)}</b> вопросов · {rep.run.duration_sec} с · {html.escape(rep.run.model)}
<div class="bar"><a href="/report/{jid}.html" target="_blank">Открыть полный отчёт</a>
<a href="/report/{jid}.md">Скачать .md</a><a href="/report/{jid}.json">Скачать .json</a><a href="/">Новая проверка</a></div></div>
<form class="card" method="post" action="/answer/{jid}"
      onsubmit="this.querySelector('button[type=submit]').disabled=true">
<b>Уточняющие вопросы автору</b>{res}
<p class="muted">Ответьте на вопросы — агент отметит закрытые дефекты, предложит новые формулировки и пересчитает вердикт.</p>
{answers}
{'<button type="submit">Учесть ответы</button>' if rep.questions else ''}</form>"""), None


def run_job(jid: str, path: Path, offline: bool) -> None:
    def progress(s: str) -> None:
        JOBS[jid]["progress"] = s
    try:
        rep = run(str(path), SETTINGS, use_llm=not offline, progress=progress)
        JOBS[jid].update(status="done", report=rep)
    except IngestError as e:
        JOBS[jid].update(status="error", error=f"Ошибка чтения документа: {e}")
    except Exception as e:  # показываем пользователю, детали — в лог
        logging.exception("Ошибка проверки")
        JOBS[jid].update(status="error", error=f"Внутренняя ошибка: {type(e).__name__}: {e}")
    finally:
        path.unlink(missing_ok=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "ReqValidator/1.0"

    def log_message(self, fmt, *args):
        logging.info("%s %s", self.address_string(), fmt % args)

    def _send(self, body: bytes, ctype="text/html; charset=utf-8", status=HTTPStatus.OK, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, to: str):
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/":
            return self._send(index_page())
        if p.startswith("/job/"):
            body, refresh = job_page(p[5:])
            return self._send(body, extra={"Refresh": str(refresh)} if refresh else None)
        if p.startswith("/report/"):
            name = p[8:]
            jid, _, ext = name.rpartition(".")
            job = JOBS.get(jid)
            if not job or job["status"] != "done":
                return self._send(page("Нет", "Отчёт не найден"), status=HTTPStatus.NOT_FOUND)
            rep = job["report"]
            md = md_report.render(rep)
            if ext == "html":
                return self._send(html_report.render(rep, md).encode("utf-8"))
            if ext == "md":
                return self._send(md.encode("utf-8"), "text/markdown; charset=utf-8",
                                  extra={"Content-Disposition": 'attachment; filename="report.md"'})
            if ext == "json":
                import json
                return self._send(json.dumps(to_dict(rep), ensure_ascii=False, indent=2).encode("utf-8"),
                                  "application/json; charset=utf-8",
                                  extra={"Content-Disposition": 'attachment; filename="report.json"'})
        return self._send(page("Нет", "Страница не найдена"), status=HTTPStatus.NOT_FOUND)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            return self._send(index_page("Файл больше 10 МБ."), status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        data = self.rfile.read(length)
        if self.path == "/check":
            return self._check(data)
        if self.path.startswith("/answer/"):
            return self._answer(self.path[8:], data)
        return self._send(page("Нет", "Не найдено"), status=HTTPStatus.NOT_FOUND)

    def _check(self, data: bytes):
        ctype = self.headers.get("Content-Type", "")
        msg = BytesParser(policy=email_policy).parsebytes(
            f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode() + data)
        file_part, offline = None, False
        for part in msg.iter_parts() if msg.is_multipart() else []:
            name = part.get_param("name", header="content-disposition")
            if name == "file":
                file_part = part
            elif name == "offline":
                offline = True
        if file_part is None or not file_part.get_filename():
            return self._send(index_page("Выберите файл."), status=HTTPStatus.BAD_REQUEST)
        ext = Path(file_part.get_filename()).suffix.lower()
        if ext not in SUPPORTED:
            return self._send(index_page(f"Формат {ext} не поддерживается."), status=HTTPStatus.BAD_REQUEST)
        jid = secrets.token_urlsafe(8)
        # исходное имя сохраняем (для паспорта), но в безопасной папке задачи
        safe_name = "".join(c for c in Path(file_part.get_filename()).name if c.isalnum() or c in "._- ")[:80] or f"doc{ext}"
        d = WORKDIR / jid
        d.mkdir()
        path = d / safe_name
        path.write_bytes(file_part.get_payload(decode=True) or b"")
        with LOCK:
            JOBS[jid] = {"status": "running", "progress": "Начинаю", "started": time.time()}
        threading.Thread(target=run_job, args=(jid, path, offline or not SETTINGS.llm_enabled), daemon=True).start()
        self._redirect(f"/job/{jid}")

    def _answer(self, jid: str, data: bytes):
        job = JOBS.get(jid)
        if not job or job["status"] != "done":
            return self._redirect("/")
        form = parse_qs(data.decode("utf-8", errors="replace"))
        rep = job["report"]
        for q in rep.questions:
            q.answer = (form.get(q.qid, [""])[0] or "").strip()[:2000]
        client = LLMClient(SETTINGS) if SETTINGS.llm_enabled and rep.run.mode.startswith("LLM") else None
        apply_report_answers(rep, client)
        self._redirect(f"/job/{jid}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Веб-интерфейс агента проверки требований")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1", help="адрес (по умолчанию только этот компьютер)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"Откройте http://{a.host}:{a.port}  (Ctrl+C — остановить)", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
