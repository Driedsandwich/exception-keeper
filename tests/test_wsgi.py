"""Call the deployment WSGI callable directly, without a deployment or browser."""
import io
import json
from pathlib import Path

import pytest

import app as deployment
from sheetaudit.api import Dependencies, DISABLED, MAX_REQUEST
from sheetaudit.llm.client import MockChatClient
from test_api import request, run_flow, script
from test_redis_ledger import NOW, TOKEN, redis, upstash_server

ROOT = Path(__file__).resolve().parents[1]


def call(path="/api/ek", method="POST", value=None, *, raw=None, **extra):
    raw = json.dumps(value).encode() if raw is None else raw
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "",
               "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw), **extra}
    received = {}

    def start_response(status, headers):
        received.update(status=int(status.split()[0]), headers=dict(headers))

    result = deployment.app(environ, start_response)
    try:
        body = b"".join(result)
    finally:
        if hasattr(result, "close"):
            result.close()
    return received["status"], body, received["headers"]


@pytest.fixture
def no_live(monkeypatch):
    for name in ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN", "KV_REST_API_URL",
                 "KV_REST_API_TOKEN", "EK_LIVE", "EK_GRANTED_CREDITS_USD", "X129_LEDGER_PATH"):
        monkeypatch.delenv(name, raising=False)


def test_full_sample_flow_through_wsgi_with_shared_redis(redis, monkeypatch, capsys, caplog):
    monkeypatch.setenv("EK_LIVE", "1")
    monkeypatch.setenv("EK_GRANTED_CREDITS_USD", "10")
    monkeypatch.setenv("NEBIUS_API_KEY", "fake-api-secret")
    monkeypatch.setenv("EK_JUDGE_PASSPHRASE", "fake-judge-secret")
    monkeypatch.setattr(deployment, "Dependencies", lambda ledger, client: Dependencies(ledger, client, NOW))
    client = MockChatClient(script())

    def make_client(**kwargs):
        assert kwargs == {"timeout_s": 55, "max_retries": 0}
        return client

    monkeypatch.setattr(deployment, "HttpChatClient", make_client)

    def send(req):
        status, raw, headers = call(value=req)
        assert headers["Cache-Control"] == "no-store"
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        assert headers["Content-Length"] == str(len(raw))
        for secret in (TOKEN, "fake-api-secret", "fake-judge-secret"):
            assert secret.encode() not in raw
        return status, json.loads(raw)

    final = run_flow(send, "event_budget")
    assert len(final["findings"]) == 2 and client.requests
    rows = [json.loads(raw) for raw in redis.lists["x129:ledger:records"]]
    assert {row["stage"] for row in rows} == {"start", "structure", "judge", "summary"}
    assert {row["event"] for row in rows} == {"started", "finished", "usage"}
    captured = capsys.readouterr()
    assert all(secret not in captured.out + captured.err + caplog.text
               for secret in (TOKEN, "fake-api-secret", "fake-judge-secret"))


@pytest.mark.parametrize("path,file", [("/", "web/index.html"), ("/app.mjs", "web/app.mjs"),
    ("/styles.css", "web/styles.css"), ("/dev-smoke.html", "web/dev-smoke.html"),
    ("/samples/event_budget.xlsx", "samples/event_budget.xlsx"),
    ("/samples/precomputed/event_budget.json", "samples/precomputed/event_budget.json")])
def test_static_assets(path, file):
    status, raw, headers = call(path, "GET")
    assert status == 200 and raw == (ROOT / file).read_bytes()
    assert headers["Content-Length"] == str(len(raw))
    if path.endswith(".mjs"):
        assert headers["Content-Type"] == "text/javascript"
    head_status, head_body, head_headers = call(path, "HEAD")
    assert head_status == 200 and head_body == b"" and head_headers == headers


@pytest.mark.parametrize("path", ["/unknown", "/README.md", "/samples/ground_truth.json", "/samples/other.xlsx",
    "/samples/precomputed/other.json", "/samples/", "/../README.md", "/%2e%2e/README.md",
    "/samples/../event_budget.xlsx", "/samples/precomputed/../../ground_truth.json",
    "/..%2fREADME.md", "/..%5cREADME.md", "/%00", "/web/../../README.md"])
def test_unknown_paths_and_traversal_are_refused(path):
    assert call(path, "GET")[0] == 404


def test_symlink_outside_static_root_is_refused(tmp_path, monkeypatch):
    (tmp_path / "web").mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("not public")
    (tmp_path / "web/link.txt").symlink_to(outside)
    monkeypatch.setattr(deployment, "ROOT", tmp_path)
    assert call("/link.txt", "GET")[0] == 404


@pytest.mark.parametrize("partial", [None, "url", "token", "file"])
def test_live_refuses_missing_shared_redis(no_live, monkeypatch, partial):
    monkeypatch.setenv("EK_LIVE", "1")
    monkeypatch.setenv("EK_GRANTED_CREDITS_USD", "10")
    if partial == "url":
        monkeypatch.setenv("KV_REST_API_URL", "http://127.0.0.1:1")
    if partial == "token":
        monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", TOKEN)
    if partial == "file":
        monkeypatch.setenv("X129_LEDGER_PATH", "/must-not-use-files")
    status, raw, _ = call(value=request())
    assert status == 503 and json.loads(raw) == {
        "error": "budget", "message": "Shared durable ledger is not configured."}
    assert TOKEN.encode() not in raw


def test_default_live_disabled_without_files(no_live, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Live-disabled preview must not write a ledger")
    monkeypatch.setattr(Path, "open", forbidden)
    # ENCODED_SAMPLE was read before file opens were forbidden.
    status, raw, _ = call(value={"stage": "start", "workbook_b64": ENCODED_SAMPLE})
    assert status == 429 and json.loads(raw)["message"] == DISABLED


# A small existing sample; no uploads are saved by the transport.
ENCODED_SAMPLE = request()["workbook_b64"]


@pytest.mark.parametrize("raw,length,transfer,status,error", [
    (b"{}", "2", "", 400, "bad_request"),
    (b"not JSON", "8", "", 400, "bad_request"),
    (b"{", "2", "", 400, "bad_request"),
    (b"NaN", "3", "", 400, "bad_request"),
    (b"{}", "-1", "", 400, "bad_request"),
    (b"{}", "bad", "", 400, "bad_request"),
    (b"{}", "2", "chunked", 400, "bad_request"),
    (b"", str(MAX_REQUEST + 1), "", 413, "too_large")])
def test_json_request_limits_match_preview(no_live, raw, length, transfer, status, error):
    actual, body, headers = call(raw=raw, CONTENT_LENGTH=length, HTTP_TRANSFER_ENCODING=transfer)
    assert actual == status and json.loads(body)["error"] == error
    assert headers["Cache-Control"] == "no-store"


def test_unknown_post_and_method(no_live):
    assert call("/other", value={})[0] == 404
    assert call("/api/ek", "GET")[0] == 404
    assert call("/", "PUT")[0] == 404
    assert call(value={}, QUERY_STRING="x=1")[0] == 404


def test_redis_failure_returns_budget_without_secrets(redis, monkeypatch, capsys, caplog):
    monkeypatch.setenv("EK_LIVE", "1")
    monkeypatch.setenv("EK_GRANTED_CREDITS_USD", "10")
    redis.fault = ("LRANGE", json.dumps({"error": TOKEN}).encode())
    status, raw, _ = call(value=request())
    assert status == 429 and json.loads(raw)["error"] == "budget"
    captured = capsys.readouterr()
    assert TOKEN not in raw.decode() + captured.out + captured.err + caplog.text


def test_response_redacts_all_environment_secrets(no_live, monkeypatch):
    secrets = ["fake-nebius", "fake-passphrase", TOKEN, "fake-kv-token"]
    for name, secret in zip(("NEBIUS_API_KEY", "EK_JUDGE_PASSPHRASE", "UPSTASH_REDIS_REST_TOKEN", "KV_REST_API_TOKEN"), secrets):
        monkeypatch.setenv(name, secret)

    def echo(transport, deps):
        from sheetaudit.http import send_json
        send_json(transport, 502, {"error": " ".join(secrets)})

    monkeypatch.setattr(deployment, "post", echo)
    status, raw, _ = call(value={})
    assert status == 502 and all(secret.encode() not in raw for secret in secrets)
