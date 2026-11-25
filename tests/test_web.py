"""Веб-платформа: API поверх того же ядра, что и CLI (без LLM, чтобы тест был быстрым и офлайн)."""
import json
import threading
import time
import urllib.error
import urllib.request
import uuid

import pytest

import web
from conftest import SAMPLE_MD


@pytest.fixture(scope="module")
def base():
    srv = web.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def call(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def upload(base, name, content: bytes, offline=True):
    b = uuid.uuid4().hex
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
             f'Content-Type: application/octet-stream\r\n\r\n'.encode() + content + b"\r\n"]
    if offline:
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="offline"\r\n\r\n1\r\n'.encode())
    body = b"".join(parts) + f"--{b}--\r\n".encode()
    return call(f"{base}/api/analyze", body, {"Content-Type": f"multipart/form-data; boundary={b}"}, "POST")


def wait_done(base, jid, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        st, _, body = call(f"{base}/api/jobs/{jid}")
        data = json.loads(body)
        if data["status"] not in ("running", "applying"):
            return data
        time.sleep(0.2)
    raise AssertionError("анализ не завершился")


def test_index_and_config(base):
    st, hdr, body = call(base + "/")
    assert st == 200 and b"Requirements Validator" in body
    assert "default-src 'self'" in hdr["Content-Security-Policy"]
    st, _, body = call(base + "/api/config")
    cfg = json.loads(body)
    assert ".pdf" in cfg["formats"] and cfg["max_mb"] == 10


def test_full_flow_upload_report_answers_export(base):
    st, _, body = upload(base, "spec.md", SAMPLE_MD.encode())
    assert st == 202
    jid = json.loads(body)["id"]
    data = wait_done(base, jid)
    assert data["status"] == "done"
    assert data["stage"] > len(data["stages"])            # все этапы пройдены
    assert any("правил" in l["msg"] for l in data["log"])  # журнал этапов в реальном времени
    rep = data["report"]
    stats = rep["stats"]
    assert stats["requirements_total"] > 0 and stats["issues_total"] == len(rep["issues"])
    assert 0 <= stats["quality_index"] <= 100
    assert all("contradiction" in i for i in rep["issues"])
    assert data["lines"] and all(q["line"] for i in rep["issues"] for q in i["quotes"])
    assert rep["verdict"]["summary"]

    # ответы автора
    q = rep["questions"][0]["qid"]
    st, _, _ = call(f"{base}/api/jobs/{jid}/answers", json.dumps({"answers": {q: "Уточним до релиза"}}).encode(),
                    {"Content-Type": "application/json"}, "POST")
    assert st == 202
    data = wait_done(base, jid)
    assert data["report"]["questions"][0]["answer"] == "Уточним до релиза"
    assert data["report"]["stats"]["questions_answered"] == 1

    # экспорт
    for ext, marker in (("md", b"# "), ("json", b'"verdict"'), ("html", b"<!doctype html>")):
        st, hdr, body = call(f"{base}/api/jobs/{jid}/export.{ext}")
        assert st == 200 and marker in body
        assert "attachment" in hdr["Content-Disposition"] or ext == "html"


def test_sample_runs_same_core(base):
    st, _, body = call(f"{base}/api/sample/requirements", b'{"offline": "1"}', {"Content-Type": "application/json"}, "POST")
    assert st == 202
    data = wait_done(base, json.loads(body)["id"])
    assert data["report"]["stats"]["requirements_total"] == 48     # как в CLI


def test_bad_inputs(base):
    st, _, body = upload(base, "virus.exe", b"MZ")
    assert st == 400 and "не поддерживается" in json.loads(body)["error"]
    st, _, body = upload(base, "empty.md", b"   ")
    assert st == 400
    assert call(f"{base}/api/jobs/nope")[0] == 404
    assert call(f"{base}/static/../web.py")[0] == 404
    assert call(f"{base}/static/%2e%2e/web.py")[0] == 404
    assert call(f"{base}/api/jobs/nope/answers", b"{}", {"Content-Type": "application/json"}, "POST")[0] == 409


def test_document_text_is_not_executed_in_export(base):
    evil = SAMPLE_MD + "\n\n<script>alert(1)</script> <img src=x onerror=alert(2)>\n"
    st, _, body = upload(base, "evil.md", evil.encode())
    jid = json.loads(body)["id"]
    wait_done(base, jid)
    _, _, html = call(f"{base}/api/jobs/{jid}/export.html")
    assert b"<script>alert(1)" not in html and b"<img src=x" not in html
