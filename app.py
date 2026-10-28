"""WSGI entrypoint for deployment; serve.py remains the local preview."""
from http import HTTPStatus
import io
import json
import mimetypes
import os
from pathlib import Path
from urllib.parse import unquote

from sheetaudit.api import Dependencies
from sheetaudit.budget import MemoryLedger
from sheetaudit.http import post
from sheetaudit.llm.client import HttpChatClient, redact
from sheetaudit.redis_ledger import RedisLedger, RedisLedgerError

ROOT = Path(__file__).resolve().parent
SAMPLE_NAMES = {"club_membership_fees", "small_business_budget", "event_budget"}


class _PostTransport:
    """Adapt WSGI input to the preview's existing bounded JSON transport."""
    def __init__(self, environ):
        self.path = environ.get("PATH_INFO", "")
        if environ.get("QUERY_STRING"):
            self.path += "?" + environ["QUERY_STRING"]
        self.headers = {"Content-Length": environ.get("CONTENT_LENGTH", "-1"),
                        "Transfer-Encoding": environ.get("HTTP_TRANSFER_ENCODING", "")}
        self.rfile = environ["wsgi.input"]
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        pass  # The WSGI response sets the same JSON headers after redaction.

    def end_headers(self):
        pass


def _static_path(path):
    path = unquote(path)
    parts = path.strip("/").split("/")
    if any(part in {".", ".."} for part in parts) or "\\" in path or "\0" in path:
        return None
    base = ROOT / "web"
    if (len(parts) == 2 and parts[0] == "samples" and parts[1].endswith(".xlsx")
            and parts[1][:-5] in SAMPLE_NAMES):
        base = ROOT / "samples"
        parts = parts[1:]
    elif (len(parts) == 3 and parts[:2] == ["samples", "precomputed"]
          and parts[2].endswith(".json") and parts[2][:-5] in SAMPLE_NAMES):
        base = ROOT / "samples" / "precomputed"
        parts = parts[2:]
    target = (base.joinpath(*parts) if path != "/" else base / "index.html").resolve()
    return target if target.is_relative_to(base) and target.is_file() else None


def _json_response(start_response, status, body):
    for name in ("NEBIUS_API_KEY", "EK_JUDGE_PASSPHRASE", "UPSTASH_REDIS_REST_TOKEN", "KV_REST_API_TOKEN"):
        body = redact(body, os.environ.get(name, ""))
    raw = json.dumps(body, ensure_ascii=False, allow_nan=False, default=str).encode()
    start_response(f"{status} {HTTPStatus(status).phrase}", [
        ("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(raw))),
        ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff")])
    return [raw]


def app(environ, start_response):
    path, method = environ.get("PATH_INFO", ""), environ.get("REQUEST_METHOD", "GET")
    if method == "POST" and path == "/api/ek":
        configured = ((os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL"))
                      and (os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN")))
        if os.environ.get("EK_LIVE") == "1" and not configured:
            return _json_response(start_response, 503, {
                "error": "budget", "message": "Shared durable ledger is not configured."})
        try:
            ledger = RedisLedger() if configured else MemoryLedger()
        except RedisLedgerError:
            return _json_response(start_response, 503, {
                "error": "budget", "message": "Shared durable ledger is not configured."})
        deps = Dependencies(ledger, HttpChatClient(timeout_s=55, max_retries=0))
        transport = _PostTransport(environ)
        post(transport, deps)
        return _json_response(start_response, transport.status, json.loads(transport.wfile.getvalue()))
    if method in {"GET", "HEAD"}:
        target = _static_path(path)
        if target is not None:
            try:
                raw = target.read_bytes()
            except OSError:
                return _json_response(start_response, 404, {"error": "not_found"})
            content_type = "text/javascript" if target.suffix == ".mjs" else mimetypes.guess_type(target.name)[0]
            start_response("200 OK", [("Content-Type", content_type or "application/octet-stream"),
                                      ("Content-Length", str(len(raw))), ("X-Content-Type-Options", "nosniff")])
            return [] if method == "HEAD" else [raw]
    return _json_response(start_response, 404, {"error": "not_found"})
