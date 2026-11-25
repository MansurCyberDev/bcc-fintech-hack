#!/usr/bin/env python3
"""Веб-платформа агента проверки требований.

    python web.py            # http://127.0.0.1:8000

Сервер на стандартной библиотеке Python (без Flask). Слушает только localhost.
Анализ выполняет то же ядро, что и CLI (src/pipeline.run), веб только показывает результат.
Интерфейс — одна страница в папке static/ (HTML + CSS + JS без сборки).

API (JSON):
    GET  /api/config                   режим LLM, форматы, лимиты
    POST /api/analyze                  multipart: file, offline=1, no_critic=1  -> {"id": ...}
    POST /api/sample/<name>            запуск на примере из репозитория        -> {"id": ...}
    GET  /api/jobs/<id>                статус, этапы, журнал; после завершения — полный отчёт
    POST /api/jobs/<id>/answers        {"answers": {"Q-01": "..."}}: учесть ответы автора
    GET  /api/jobs/<id>/export.<md|html|json>   экспорт отчёта
"""
from __future__ import annotations

import argparse
import json
import logging
import re
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

from src import __version__
from src.config import ROOT, load_settings
from src.ingest import SUPPORTED, IngestError
from src.llm.client import LLMClient
from src.pipeline import apply_report_answers, run
from src.report import html as html_report
from src.report import markdown as md_report
from src.report.writer import to_dict

MAX_UPLOAD = 10 * 1024 * 1024
MAX_JOBS = 50
STATIC = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml"}
SAMPLES = {
    "requirements": ROOT / "docs" / "requirements.md",
    "cashback": ROOT / "examples" / "eval" / "cashback.md",
}
STAGES = ["Чтение документа", "Нарезка на требования", "Проверка правилами", "Анализ LLM",
          "Проверка цитат и критик", "Уточняющие вопросы", "Вердикт и отчёт"]

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
WORKDIR = Path(tempfile.mkdtemp(prefix="arv-web-"))
SETTINGS = load_settings()


# ------------------------------------------------------------------ задачи

def new_job(name: str) -> str:
    jid = secrets.token_urlsafe(9)
    with LOCK:
        if len(JOBS) >= MAX_JOBS:  # память ограничена: удаляем самые старые завершённые
            for old in sorted(JOBS, key=lambda k: JOBS[k]["started"])[: len(JOBS) - MAX_JOBS + 1]:
                JOBS.pop(old, None)
        JOBS[jid] = {"status": "running", "stage": 0, "log": [], "started": time.time(), "finished": None,
                     "name": name, "report": None, "error": ""}
    return jid


def job_progress(jid: str, msg: str) -> None:
    job = JOBS[jid]
    m = re.match(r"(\d)/7\s+(.*)", msg)
    if m:
        job["stage"] = int(m.group(1))
        msg = m.group(2)
    job["log"].append({"t": round(time.time() - job["started"], 1), "msg": msg})


def run_job(jid: str, path: Path, offline: bool, critic: bool, cleanup: bool) -> None:
    try:
        rep = run(str(path), SETTINGS, use_llm=not offline, use_critic=critic,
                  progress=lambda s: job_progress(jid, s))
        JOBS[jid].update(status="done", report=rep, stage=len(STAGES) + 1)
        job_progress(jid, "Готово")
    except IngestError as e:
        JOBS[jid].update(status="error", error=f"Не удалось прочитать документ: {e}")
    except Exception as e:  # показываем пользователю, детали — в лог сервера
        logging.exception("Ошибка проверки")
        JOBS[jid].update(status="error", error=f"Внутренняя ошибка: {type(e).__name__}: {e}")
    finally:
        JOBS[jid]["finished"] = time.time()
        if cleanup:
            path.unlink(missing_ok=True)


def apply_job_answers(jid: str, answers: dict) -> None:
    job = JOBS[jid]
    rep = job["report"]
    try:
        for q in rep.questions:
            q.answer = str(answers.get(q.qid, "") or "").strip()[:2000]
        client = LLMClient(SETTINGS) if SETTINGS.llm_enabled and rep.run.mode.startswith("LLM") else None
        apply_report_answers(rep, client)
        job_progress(jid, "Ответы автора учтены")
    except Exception as e:
        logging.exception("Ошибка применения ответов")
        job_progress(jid, f"Не удалось учесть ответы: {type(e).__name__}")
    finally:
        job["status"] = "done"


def job_payload(jid: str) -> dict | None:
    job = JOBS.get(jid)
    if not job:
        return None
    end = job["finished"] or time.time()
    out = {
        "id": jid, "status": job["status"], "name": job["name"], "error": job["error"],
        "stage": job["stage"], "stages": STAGES, "log": job["log"][-60:],
        "elapsed": round(end - job["started"], 1),
    }
    rep = job["report"]
    if rep is not None and job["status"] in ("done", "applying"):
        out["report"] = to_dict(rep)
        out["lines"] = rep.document.text.split("\n")
    return out


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "ReqValidator/" + __version__

    def log_message(self, fmt, *args):
        logging.info("%s %s", self.address_string(), fmt % args)

    def _send(self, body: bytes, ctype: str, status=HTTPStatus.OK, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        if ctype.startswith("text/html") and not (extra or {}).get("Content-Disposition"):
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status=HTTPStatus.OK):
        self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def _error(self, msg: str, status=HTTPStatus.BAD_REQUEST):
        self._json({"error": msg}, status)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            return self._static("index.html")
        if p.startswith("/static/"):
            return self._static(p[len("/static/"):])
        if p == "/api/config":
            return self._json({
                "llm_enabled": SETTINGS.llm_enabled, "model": SETTINGS.model_label, "version": __version__,
                "formats": sorted(SUPPORTED), "max_mb": MAX_UPLOAD // (1024 * 1024),
                "samples": [{"id": k, "name": v.name} for k, v in SAMPLES.items() if v.exists()],
            })
        m = re.fullmatch(r"/api/jobs/([\w-]+)", p)
        if m:
            data = job_payload(m.group(1))
            return self._json(data) if data else self._error("Проверка не найдена", HTTPStatus.NOT_FOUND)
        m = re.fullmatch(r"/api/jobs/([\w-]+)/export\.(md|html|json)", p)
        if m:
            return self._export(m.group(1), m.group(2))
        return self._error("Не найдено", HTTPStatus.NOT_FOUND)

    def _static(self, name: str):
        f = (STATIC / name).resolve()
        if STATIC not in f.parents or not f.is_file() or f.suffix not in STATIC_TYPES:
            return self._error("Не найдено", HTTPStatus.NOT_FOUND)
        self._send(f.read_bytes(), STATIC_TYPES[f.suffix])

    def _export(self, jid: str, ext: str):
        job = JOBS.get(jid)
        if not job or job["report"] is None:
            return self._error("Отчёт не найден", HTTPStatus.NOT_FOUND)
        rep = job["report"]
        stem = re.sub(r"[^\w.-]+", "_", Path(rep.document.name).stem)[:60] or "report"
        md = md_report.render(rep)
        if ext == "md":
            body, ctype = md.encode("utf-8"), "text/markdown; charset=utf-8"
        elif ext == "html":
            body, ctype = html_report.render(rep, md).encode("utf-8"), "text/html; charset=utf-8"
        else:
            body = json.dumps(to_dict(rep), ensure_ascii=False, indent=2).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        disp = "inline" if ext == "html" and "download" not in self.path else "attachment"
        self._send(body, ctype, extra={"Content-Disposition": f'{disp}; filename="report_{stem}.{ext}"'})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            return self._error(f"Файл больше {MAX_UPLOAD // (1024 * 1024)} МБ.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        data = self.rfile.read(length)
        p = self.path.split("?")[0]
        if p == "/api/analyze":
            return self._analyze(data)
        m = re.fullmatch(r"/api/sample/(\w+)", p)
        if m:
            return self._sample(m.group(1), data)
        m = re.fullmatch(r"/api/jobs/([\w-]+)/answers", p)
        if m:
            return self._answers(m.group(1), data)
        return self._error("Не найдено", HTTPStatus.NOT_FOUND)

    @staticmethod
    def _flags(fields: dict) -> tuple[bool, bool]:
        offline = str(fields.get("offline", "")) in ("1", "true", "on") or not SETTINGS.llm_enabled
        critic = str(fields.get("no_critic", "")) not in ("1", "true", "on")
        return offline, critic

    def _analyze(self, data: bytes):
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            return self._error("Ожидается загрузка файла (multipart/form-data).")
        msg = BytesParser(policy=email_policy).parsebytes(
            f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode() + data)
        file_part, fields = None, {}
        for part in msg.iter_parts() if msg.is_multipart() else []:
            name = part.get_param("name", header="content-disposition")
            if name == "file":
                file_part = part
            elif name:
                fields[name] = (part.get_content() or "").strip()
        if file_part is None or not file_part.get_filename():
            return self._error("Выберите файл.")
        ext = Path(file_part.get_filename()).suffix.lower()
        if ext not in SUPPORTED:
            return self._error(f"Формат {ext or '(без расширения)'} не поддерживается. "
                               f"Поддерживаются: {', '.join(sorted(SUPPORTED))}.")
        payload = file_part.get_payload(decode=True) or b""
        if not payload.strip():
            return self._error("Файл пустой.")
        safe_name = "".join(c for c in Path(file_part.get_filename()).name if c.isalnum() or c in "._- ")[:80]
        jid = new_job(safe_name or f"document{ext}")
        d = WORKDIR / jid
        d.mkdir()
        path = d / (safe_name if safe_name.lower().endswith(ext) else f"document{ext}")
        path.write_bytes(payload)
        offline, critic = self._flags(fields)
        threading.Thread(target=run_job, args=(jid, path, offline, critic, True), daemon=True).start()
        self._json({"id": jid}, HTTPStatus.ACCEPTED)

    def _sample(self, name: str, data: bytes):
        path = SAMPLES.get(name)
        if not path or not path.exists():
            return self._error("Пример не найден", HTTPStatus.NOT_FOUND)
        try:
            fields = json.loads(data or b"{}")
        except ValueError:
            fields = {}
        offline, critic = self._flags(fields if isinstance(fields, dict) else {})
        jid = new_job(path.name)
        threading.Thread(target=run_job, args=(jid, path, offline, critic, False), daemon=True).start()
        self._json({"id": jid}, HTTPStatus.ACCEPTED)

    def _answers(self, jid: str, data: bytes):
        job = JOBS.get(jid)
        if not job or job["status"] != "done" or job["report"] is None:
            return self._error("Отчёт ещё не готов или не найден.", HTTPStatus.CONFLICT)
        try:
            body = json.loads(data or b"{}")
            answers = body.get("answers", {})
            assert isinstance(answers, dict)
        except (ValueError, AssertionError, AttributeError):
            return self._error("Ожидается JSON вида {\"answers\": {\"Q-01\": \"...\"}}.")
        job["status"] = "applying"
        job_progress(jid, "Учитываю ответы автора")
        threading.Thread(target=apply_job_answers, args=(jid, answers), daemon=True).start()
        self._json({"id": jid}, HTTPStatus.ACCEPTED)


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def main() -> int:
    ap = argparse.ArgumentParser(description="Веб-платформа агента проверки требований")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1", help="адрес (по умолчанию только этот компьютер)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    srv = make_server(a.host, a.port)
    mode = SETTINGS.model_label if SETTINGS.llm_enabled else "только правила (ключ LLM не найден)"
    print(f"Откройте http://{a.host}:{a.port}   режим: {mode}   (Ctrl+C — остановить)", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
