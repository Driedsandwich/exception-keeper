"""Shared REST ledger; the wire contract is unverified until deploy (see notes)."""
from contextlib import contextmanager
from decimal import InvalidOperation
from http.client import HTTPException
import json
import math
import os
import time
from urllib import error, request
from urllib.parse import urlsplit
from uuid import uuid4

from .budget import BudgetStopped, D, MemoryLedger
from .llm.client import redact


class RedisLedgerError(BudgetStopped, ValueError):
    """A safe budget refusal with no server response or configuration text."""


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RedisLedger(MemoryLedger):
    def __init__(self, *, lock_wait_s=100, poll_s=.5):
        super().__init__()
        self._url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL", "")
        try:
            parts = urlsplit(self._url)
            if (parts.scheme not in {"http", "https"} or not parts.hostname
                    or parts.username or parts.password or parts.query or parts.fragment):
                raise ValueError()
            if not self._token() or not math.isfinite(lock_wait_s) or lock_wait_s < 0 or not 0 < poll_s <= .5:
                raise ValueError()
        except (ValueError, TypeError):
            raise RedisLedgerError("Ledger configuration invalid; refusing calls") from None
        prefix = os.environ.get("X129_LEDGER_PREFIX", "x129:ledger")
        self._records_key, self._lock_key = prefix + ":records", prefix + ":lock"
        self._wait, self._poll = lock_wait_s, poll_s
        self._depth = 0
        self._expires = 0

    @staticmethod
    def _token():
        # Resolve the secret for each HTTP call; never retain it on the ledger.
        return os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN", "")

    def _command(self, *command):
        """Keep the assumed Upstash POST/JSON contract in one small function."""
        try:
            token = self._token()
            if not token:
                raise ValueError()
            req = request.Request(self._url, data=json.dumps(command, allow_nan=False).encode(),
                                  headers={"Authorization": "Bearer " + token,
                                           "Content-Type": "application/json"}, method="POST")
            # No redirects, retries, response logging or provider error text.
            with request.build_opener(_NoRedirect()).open(req, timeout=5) as response:
                raw = response.read(10_000_001)
            if len(raw) > 10_000_000:
                raise ValueError()
            body = json.loads(raw)
            if not isinstance(body, dict) or "error" in body or "result" not in body:
                raise ValueError()
            return body["result"]
        except error.HTTPError as exc:
            exc.close()
            raise RedisLedgerError("Ledger unavailable or invalid; refusing calls") from None
        except (OSError, HTTPException, ValueError, TypeError, RecursionError):
            raise RedisLedgerError("Ledger unavailable or invalid; refusing calls") from None

    @contextmanager
    def locked(self):
        with self._lock:
            if self._depth:
                self._check_lease()
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            owner = str(uuid4())
            deadline = time.monotonic() + self._wait
            while True:
                began = time.monotonic()
                result = self._command("SET", self._lock_key, owner, "NX", "PX", 120000)
                if result == "OK":
                    self._expires = began + 120
                    break
                if result is not None:
                    raise RedisLedgerError("Invalid ledger lock reply; refusing calls")
                if time.monotonic() >= deadline:
                    raise RedisLedgerError("Ledger busy; refusing calls")
                time.sleep(min(self._poll, max(0, deadline - time.monotonic())))
            try:
                self.entries = []
                rows = self._command("LRANGE", self._records_key, 0, -1)
                try:
                    if not isinstance(rows, list) or any(not isinstance(row, str) for row in rows):
                        raise ValueError()
                    entries = [json.loads(row) for row in rows]
                    for entry in entries:
                        self._validate(entry)
                except (ValueError, TypeError, InvalidOperation, OverflowError, RecursionError):
                    raise RedisLedgerError("Invalid ledger entry; refusing calls") from None
                self.entries = entries
                self._check_lease()
                self._depth = 1
                yield
                self._check_lease()
            finally:
                self._depth = 0
                self._expires = 0
                result = self._command("EVAL", "if redis.call('GET', KEYS[1]) == ARGV[1] then "
                                       "return redis.call('DEL', KEYS[1]) else return 0 end",
                                       1, self._lock_key, owner)
                if type(result) is not int or result != 1:
                    raise RedisLedgerError("Ledger lock ownership lost; refusing calls")

    def _check_lease(self):
        if time.monotonic() >= self._expires:
            raise RedisLedgerError("Ledger lock expired; refusing calls")

    @staticmethod
    def _validate(entry):
        if (not isinstance(entry, dict) or not {"app", "lane", "day", "amount"} <= entry.keys()
                or not math.isfinite(float(entry["amount"])) or D(entry["amount"]) < 0):
            raise ValueError("Invalid ledger entry")

    def add(self, app, lane, day, amount, **details):
        try:
            if not D(amount).is_finite() or D(amount) < 0:
                raise ValueError()
            entry = dict(app=app, lane=lane, day=str(day), amount=float(amount), **details)
            self._validate(entry)
            entry = redact(redact(entry), self._token())
            raw = json.dumps(entry, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, InvalidOperation, OverflowError, RecursionError):
            raise RedisLedgerError("Invalid ledger entry; refusing calls") from None
        with self.locked():
            result = self._command("RPUSH", self._records_key, raw)
            if type(result) is not int or result != len(self.entries) + 1:
                raise RedisLedgerError("Invalid ledger append reply; refusing calls")
            self.entries.append(entry)

    def spent(self, app, lane, day):
        with self.locked():
            return super().spent(app, lane, day)

    def spent_period(self, app, start, end):
        with self.locked():
            return super().spent_period(app, start, end)

    def total(self):
        with self.locked():
            return super().total()
