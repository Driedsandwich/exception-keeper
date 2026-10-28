"""Small stdlib JSON transport shared by the preview and deployment stub."""
import json
import os

from .api import MAX_REQUEST, handle


def post(handler, deps, env=None):
    if handler.path != "/api/ek":
        return send_json(handler, 404, {"error": "not_found"})
    try:
        length = int(handler.headers.get("Content-Length", "-1"))
        if length > MAX_REQUEST:
            return send_json(handler, 413, {"error": "too_large"})
        if length < 0 or handler.headers.get("Transfer-Encoding"):
            raise ValueError()
        raw = handler.rfile.read(length)
        if len(raw) != length:
            raise ValueError()
        value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError, OSError):
        return send_json(handler, 400, {"error": "bad_request"})
    status, body = handle(value, os.environ if env is None else env, deps)
    send_json(handler, status, body)


def send_json(handler, status, body):
    raw = json.dumps(body, ensure_ascii=False, allow_nan=False, default=str).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(raw)
