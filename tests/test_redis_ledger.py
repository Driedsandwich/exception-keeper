"""Redis wire-contract tests: loopback HTTP plus a socket-free fallback double."""
from datetime import datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
import threading
import time
from types import SimpleNamespace
from urllib import error

import pytest

from sheetaudit.api import Dependencies, handle
from sheetaudit.budget import FileLedger, JST
from sheetaudit.llm.client import MockChatClient
from sheetaudit.redis_ledger import RedisLedger, RedisLedgerError

TOKEN = "fake-redis-secret"
NOW = lambda: datetime(2026, 10, 28, 12, tzinfo=JST)


class FakeRedis:
    def __init__(self):
        self.reset()

    def reset(self):
        self.token = TOKEN
        self.lists, self.locks, self.commands = {}, {}, []
        self.fault = None

    def reply(self, raw, authorization):
        if authorization != "Bearer " + self.token:
            return json.dumps({"error": "unauthorized " + self.token}).encode()
        command = json.loads(raw)
        self.commands.append(command)
        name, key = command[0], command[1]
        if self.fault and self.fault[0] == name:
            return self.fault[1]
        if name == "SET":
            assert len(command) == 6 and command[3:] == ["NX", "PX", 120000]
        return self._execute(command)

    def _execute(self, command):
        name, key = command[0], command[1]
        if name == "SET":
            if key in self.locks and self.locks[key][1] <= time.monotonic():
                del self.locks[key]
            if key in self.locks:
                result = None
            else:
                self.locks[key] = (command[2], time.monotonic() + command[5] / 1000)
                result = "OK"
        elif name == "LRANGE":
            assert command[2:] == [0, -1]
            result = self.lists.get(key, [])
        elif name == "RPUSH":
            assert len(command) == 3
            self.lists.setdefault(key, []).append(command[2])
            result = len(self.lists[key])
        elif name == "EVAL":
            assert len(command) == 5 and command[2] == 1
            assert "redis.call('GET', KEYS[1]) == ARGV[1]" in command[1]
            assert "redis.call('DEL', KEYS[1])" in command[1]
            key, owner = command[3:]
            result = int(key in self.locks and self.locks[key][0] == owner
                         and self.locks[key][1] > time.monotonic())
            if result:
                del self.locks[key]
        else:
            raise AssertionError("Unexpected command")
        return json.dumps({"result": result}).encode()


@pytest.fixture(scope="module")
def upstash_server():
    state = FakeRedis()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = state.reply(raw, self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler, bind_and_activate=False)
    try:
        server.server_bind()
    except OSError as exc:
        server.server_close()
        pytest.skip(f"Loopback bind failed: {exc}")
    server.server_activate()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(state=state, url=f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        thread.join(timeout=1)
        server.server_close()


@pytest.fixture(params=["loopback", "socket-free"])
def redis(request, monkeypatch):
    if request.param == "loopback":
        fake = request.getfixturevalue("upstash_server")
        fake.state.reset()
    else:
        fake = SimpleNamespace(state=FakeRedis(), url="http://127.0.0.1:1")

        class Opener:
            def open(self, req, timeout):
                assert req.full_url == fake.url and req.get_method() == "POST"
                assert req.get_header("Content-type") == "application/json" and timeout == 5
                return io.BytesIO(fake.state.reply(req.data, req.get_header("Authorization")))

        monkeypatch.setattr("sheetaudit.redis_ledger.request.build_opener", lambda *args: Opener())
    for name in ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN", "KV_REST_API_URL",
                 "KV_REST_API_TOKEN", "X129_LEDGER_PREFIX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", fake.url)
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", TOKEN)
    return fake.state


def test_round_trip_and_reentrant_refresh(redis):
    first, other = RedisLedger(), RedisLedger()
    with first.locked():
        first.add("ek", "public", "2026-10-28", .02, session="s", stage="start", event="usage", estimated=True)
        assert first.total() == Decimal(".02")
        assert first.spent("ek", "public", "2026-10-28") == Decimal(".02")
        assert first.spent_period("ek", "2026-10-23", "2026-10-30") == Decimal(".02")
        assert first.spent_session("ek", "s") == Decimal(".02")
        assert len(first.records()) == 1
        assert [c[0] for c in redis.commands].count("LRANGE") == 1
    assert other.records() == first.records()
    assert other.total() == Decimal(".02")
    assert set(redis.lists) == {"x129:ledger:records"}
    assert not redis.locks


def test_file_record_format_in_both_directions(redis, tmp_path):
    file = FileLedger(tmp_path / "from-file.jsonl")
    file.add("ek", "judge", "2026-10-28", .03, session="s", stage="judge", event="usage", estimated=False)
    redis.lists["x129:ledger:records"] = file.path.read_text().splitlines()
    remote = RedisLedger()
    assert remote.records() == file.records()
    remote.add("pw", "public", "2026-10-28", .04, session="p", stage="extract", estimated=True)
    dump = tmp_path / "from-redis.jsonl"
    dump.write_text("\n".join(redis.lists["x129:ledger:records"]) + "\n")
    assert FileLedger(dump).records() == remote.records()
    assert FileLedger(dump).total() == Decimal(".07")


@pytest.mark.parametrize("amount,credits", [(45, "100"), (10, "10")])
def test_patchwitness_records_enforce_global_and_granted_caps(redis, amount, credits):
    from test_api import request, script
    records = [dict(app="pw", lane="public", day="2026-10-28", amount=0,
                    session="s", stage="extract", event="started"),
               dict(app="pw", lane="public", day="2026-10-28", amount=amount,
                    session="s", stage="extract", event="usage", estimated=True)]
    redis.lists["x129:ledger:records"] = [json.dumps(record) for record in records]
    ledger, client = RedisLedger(), MockChatClient(script())
    assert ledger.total() == Decimal(amount)
    status, body = handle(request(), {"EK_LIVE": "1", "EK_GRANTED_CREDITS_USD": credits},
                          Dependencies(ledger, client, NOW))
    assert status == 429 and body["error"] == "budget"
    assert not client.requests


@pytest.mark.parametrize("row", ['{}', '[]', 'broken JSON',
    '{"app":"pw","lane":"public","day":"2026-10-28","amount":-1}',
    '{"app":"pw","lane":"public","day":"2026-10-28","amount":"NaN"}',
    '{"app":"pw","lane":"public","day":"2026-10-28","amount":"Infinity"}',
    '{"app":"pw","lane":"public","day":"2026-10-28","amount":null}'])
def test_invalid_record_refuses_all_reads(redis, row):
    ledger = RedisLedger()
    ledger.add("ek", "public", "2026-10-28", .01)
    redis.lists["x129:ledger:records"].append(row)
    for read in (ledger.total, ledger.records, lambda: ledger.spent("ek", "public", "2026-10-28"),
                 lambda: ledger.spent_period("ek", "2026-10-23", "2026-10-30"),
                 lambda: ledger.spent_session("ek", "s")):
        with pytest.raises(RedisLedgerError, match="Invalid ledger entry"):
            read()
    assert not redis.locks


@pytest.mark.parametrize("amount", [-1, Decimal("-1e-9999"), "NaN", "Infinity", "-Infinity", "invalid"])
def test_bad_amount_never_appended(redis, amount):
    with pytest.raises(RedisLedgerError):
        RedisLedger().add("ek", "public", "2026-10-28", amount)
    assert not redis.lists and not redis.commands


def test_lock_contention_times_out_then_can_acquire(redis):
    holder, contender = RedisLedger(), RedisLedger(lock_wait_s=.02, poll_s=.005)
    with holder.locked():
        before = time.monotonic()
        with pytest.raises(RedisLedgerError, match="busy"):
            contender.add("ek", "public", "2026-10-28", .01)
        assert time.monotonic() - before >= .02
        assert not redis.lists
        assert len([c for c in redis.commands if c[0] == "SET"]) >= 3
    contender.add("ek", "public", "2026-10-28", .01)
    assert contender.total() == Decimal(".01")


def test_unlock_preserves_other_owners_lock(redis):
    ledger = RedisLedger()
    with pytest.raises(RedisLedgerError, match="ownership lost"):
        with ledger.locked():
            redis.locks["x129:ledger:lock"] = ("another-owner", time.monotonic() + 120)
    assert redis.locks["x129:ledger:lock"][0] == "another-owner"


def test_expired_lease_refuses_append(redis):
    ledger = RedisLedger()
    with pytest.raises(RedisLedgerError, match="expired"):
        with ledger.locked():
            ledger._expires = 0
            ledger.add("ek", "public", "2026-10-28", 1)
    assert not redis.lists


@pytest.mark.parametrize("command", ["SET", "LRANGE", "RPUSH", "EVAL"])
@pytest.mark.parametrize("reply", [b'{"error":"' + TOKEN.encode() + b'"}', b'not JSON ' + TOKEN.encode(),
                                   b'{}', b'[]', b'{"result":false}'])
def test_bad_redis_replies_are_safe_refusals(redis, command, reply, capsys, caplog):
    redis.fault = (command, reply)
    with pytest.raises(RedisLedgerError) as raised:
        RedisLedger().add("ek", "public", "2026-10-28", .01)
    captured = capsys.readouterr()
    assert TOKEN not in str(raised.value) + repr(raised.value) + captured.out + captured.err + caplog.text
    assert os.environ["UPSTASH_REDIS_REST_URL"] not in str(raised.value)


def test_environment_aliases_prefix_and_token_at_call_time(redis, monkeypatch):
    url = os.environ["UPSTASH_REDIS_REST_URL"]
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL")
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN")
    monkeypatch.setenv("KV_REST_API_URL", url)
    monkeypatch.setenv("KV_REST_API_TOKEN", TOKEN)
    monkeypatch.setenv("X129_LEDGER_PREFIX", "shared-test")
    ledger = RedisLedger()
    assert TOKEN not in repr(vars(ledger))
    redis.token = "rotated-fake-token"
    monkeypatch.setenv("KV_REST_API_TOKEN", redis.token)
    ledger.add("pw", "public", "2026-10-28", 0)
    assert ledger.total() == 0 and set(redis.lists) == {"shared-test:records"}
    monkeypatch.delenv("KV_REST_API_TOKEN")
    with pytest.raises(RedisLedgerError):
        ledger.total()


def test_primary_configuration_takes_precedence(redis, monkeypatch):
    monkeypatch.setenv("KV_REST_API_URL", "invalid URL")
    monkeypatch.setenv("KV_REST_API_TOKEN", "wrong-token")
    RedisLedger().add("ek", "public", "2026-10-28", 0)
    assert len(redis.lists["x129:ledger:records"]) == 1


def test_authentication_error_never_leaks_token(redis):
    redis.token = "wrong-token"
    with pytest.raises(RedisLedgerError) as raised:
        RedisLedger().total()
    assert "wrong-token" not in str(raised.value) and TOKEN not in str(raised.value)


@pytest.mark.parametrize("http_error", [False, True])
def test_transport_errors_hide_configuration_and_close_http_body(monkeypatch, http_error, capsys, caplog):
    url = "http://127.0.0.1:1"
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", url)
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", TOKEN)
    body = io.BytesIO(TOKEN.encode())

    class BrokenOpener:
        def open(self, req, timeout):
            if http_error:
                raise error.HTTPError(url, 500, TOKEN, {}, body)
            raise error.URLError(url + " " + TOKEN)

    monkeypatch.setattr("sheetaudit.redis_ledger.request.build_opener", lambda *args: BrokenOpener())
    with pytest.raises(RedisLedgerError) as raised:
        RedisLedger().total()
    captured = capsys.readouterr()
    output = str(raised.value) + captured.out + captured.err + caplog.text
    assert TOKEN not in output and url not in output
    if http_error:
        assert body.closed
