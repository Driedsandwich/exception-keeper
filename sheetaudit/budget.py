"""Conservative, shared spending ledger. Dates are always Asia/Tokyo.

HTTP attempts with unknown usage are charged at their reservation, including failed
calls. File locks cover admission, the call and accounting, across instances.
"""
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
import fcntl
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from .llm.client import redact
from .llm.models import PRICES

JST = ZoneInfo("Asia/Tokyo")
D = lambda value: Decimal(str(value))


class BudgetStopped(Exception):
    pass


class Ledger(Protocol):
    """Shared accounting interface; locked spans admission through recording."""

    def add(self, app, lane, day, amount, **details): ...
    def spent(self, app, lane, day): ...
    def spent_period(self, app, start, end): ...
    def total(self): ...
    def records(self): ...
    def spent_session(self, app, session): ...
    def locked(self): ...


class MemoryLedger:
    def __init__(self, entries=()):
        self.entries = list(entries)
        self._lock = threading.RLock()

    @contextmanager
    def locked(self):
        with self._lock:
            yield

    def add(self, app, lane, day, amount, **details):
        if not D(amount).is_finite() or D(amount) < 0:
            raise ValueError("Ledger amount must be finite and nonnegative")
        with self.locked():
            self.entries.append(dict(app=app, lane=lane, day=str(day), amount=float(amount), **details))

    def spent(self, app, lane, day):
        return sum((D(e["amount"]) for e in self.entries
                    if e["app"] == app and e["lane"] == lane and e["day"] == str(day)), D(0))

    def spent_period(self, app, start, end):
        return sum((D(e["amount"]) for e in self.entries
                    if e["app"] == app and str(start) <= e["day"] <= str(end)), D(0))

    def total(self):
        return sum((D(e["amount"]) for e in self.entries), D(0))

    def records(self):
        with self.locked():
            return [dict(e) for e in self.entries]

    def spent_session(self, app, session):
        return sum((D(e["amount"]) for e in self.records()
                    if e["app"] == app and e.get("session") == session), D(0))


_FILE_LOCKS = {}
_FILE_LOCKS_GUARD = threading.Lock()


class FileLedger(MemoryLedger):
    def __init__(self, path):
        super().__init__()
        self.path = Path(path).resolve()
        with _FILE_LOCKS_GUARD:
            self._lock = _FILE_LOCKS.setdefault(str(self.path), threading.RLock())
        self._depth = 0
        self._file = None

    @contextmanager
    def _shared_lock(self):
        """Use PatchWitness's .lock protocol as well as Python's file lock.

        Token ownership prevents releasing a lock that another owner replaced.
        Never reclaim a stale lock here; ambiguous interrupted work fails closed.
        """
        path = Path(str(self.path) + ".lock")
        token = str(uuid4())
        deadline = time.monotonic() + 1
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w") as stream:
                    stream.write(token)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise ValueError("Ledger busy; refusing calls") from None
                time.sleep(.01)
        try:
            yield
        finally:
            if path.read_text() == token:
                path.unlink()

    @contextmanager
    def locked(self):
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._shared_lock(), self.path.open("a+", encoding="utf-8") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                self._file = stream
                self._depth = 1
                try:
                    stream.seek(0)
                    self.entries = [json.loads(line) for line in stream if line.strip()]
                    if any(not isinstance(e, dict) or not {"app", "lane", "day", "amount"} <= e.keys()
                           or not math.isfinite(float(e["amount"])) or D(e["amount"]) < 0
                           for e in self.entries):
                        raise ValueError("Invalid ledger entry")
                    yield
                finally:
                    self._depth = 0
                    self._file = None
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def add(self, app, lane, day, amount, **details):
        if not D(amount).is_finite() or D(amount) < 0:
            raise ValueError("Ledger amount must be finite and nonnegative")
        with self.locked():
            entry = dict(app=app, lane=lane, day=str(day), amount=float(amount), **details)
            self._file.seek(0, 2)
            self._file.write(json.dumps(redact(entry), ensure_ascii=False) + "\n")
            self._file.flush()
            # flush + fsync before releasing the shared lock.
            os.fsync(self._file.fileno())
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


def estimate_tokens(value):
    return math.ceil(len(json.dumps(value, ensure_ascii=False, default=str)) / 3)


def price(model, prompt, completion, cached=0):
    rates = PRICES[model]
    cached = min(prompt, max(0, cached)) if "cached_input" in rates else 0
    return ((D(prompt - cached) * D(rates["input"]) +
             D(cached) * D(rates.get("cached_input", rates["input"])) +
             D(completion) * D(rates["output"])) / D(1_000_000))


class Budget:
    def __init__(self, ledger: Ledger | None = None, *, now=None, run_cap=0.15,
                 session=None, credits_usd=None):
        self.ledger = ledger if ledger is not None else MemoryLedger()
        self.now = now or (lambda: datetime.now(JST))
        if not D(run_cap).is_finite() or D(run_cap) < 0:
            raise ValueError("Invalid run cap")
        self.run_cap = min(D(run_cap), D("0.15"))
        self.run_spent = D(0)
        self._explicit_session = session is not None
        self.session = session if session is not None else str(uuid4())
        self.credits_usd = D(credits_usd) if credits_usd is not None else None
        if self.credits_usd is not None and (not self.credits_usd.is_finite() or self.credits_usd <= 0):
            raise ValueError("Granted credits must be positive and finite")
        self.costs = {}
        self.rate_headers = []
        self.stopped_reason = None

    def _check(self, reserve, app, lane, day):
        if app != "ek" or lane not in {"public", "judge"}:
            raise BudgetStopped("Unsupported app or lane")
        spent = self.ledger.spent_session(app, self.session)
        if spent + reserve > self.run_cap:
            label = "Per-session" if self._explicit_session else "Per-run"
            raise BudgetStopped(label + " $0.15 cap (or lower configured cap)")
        if self.ledger.total() + reserve > D(45):
            raise BudgetStopped("Global $45 cap across ek and pw")
        if self.credits_usd is not None and self.ledger.total() + reserve > self.credits_usd:
            raise BudgetStopped("Granted credits cap across ek and pw")
        if "2026-10-23" <= day <= "2026-10-30":
            if self.ledger.spent_period("ek", "2026-10-23", "2026-10-30") + reserve > D(7):
                raise BudgetStopped("Build-period ek $7 cap")
        elif "2026-10-31" <= day <= "2026-11-30":
            if self.ledger.spent_period("ek", day, day) + reserve > D("0.15"):
                raise BudgetStopped("Daily ek $0.15 cap")
        elif "2026-12-01" <= day <= "2026-12-16":
            cap = D("0.60" if lane == "judge" else "0.20")
            if self.ledger.spent("ek", lane, day) + reserve > cap:
                raise BudgetStopped(f"Daily {lane} ${cap} cap")
        else:
            raise BudgetStopped("Outside permitted dates")

    def complete(self, client, stage, app, lane, model, messages, **kwargs):
        if self.stopped_reason:
            raise BudgetStopped(self.stopped_reason)
        prompt_est = estimate_tokens(dict(messages=messages, **kwargs)) + 64
        completion_max = kwargs.get("max_tokens", 1024)
        attempt_max = getattr(client, "max_retries", 0) + 1
        reserve_one = price(model, prompt_est, completion_max)
        reserve = reserve_one * attempt_max
        # Hold the shared ledger lock until the call has been accounted for.
        try:
            with self.ledger.locked():
                instant = self.now()
                if instant.tzinfo is None:
                    raise BudgetStopped("Budget clock must be timezone aware")
                day = instant.astimezone(JST).date().isoformat()
                self._check(reserve, app, lane, day)
                result = None
                try:
                    result = client.complete(model, messages, **kwargs)
                finally:
                    usage = result.usage if result else None
                    reported = (isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0
                                for k in ("prompt_tokens", "completion_tokens")))
                    prompt = usage["prompt_tokens"] if reported else prompt_est
                    completion = (usage["completion_tokens"] if reported else
                                  estimate_tokens(dict(content=result.content, tool_calls=result.tool_calls))
                                  if result else completion_max)
                    cached = 0
                    if reported:
                        details = usage.get("prompt_tokens_details")
                        if isinstance(details, dict) and type(details.get("cached_tokens")) is int:
                            cached = details["cached_tokens"]
                    attempts = result.attempts if result else attempt_max
                    amount = price(model, prompt, completion, cached) + reserve_one * (attempts - 1)
                    estimated = not reported or attempts > 1
                    # Failed attempts use the ceiling; successful missing usage uses characters.
                    self.ledger.add(app, lane, day, amount, session=self.session,
                                    stage=stage, event="usage", estimated=estimated)
                    self.run_spent += amount
                    stats = self.costs.setdefault(stage, dict(estimated_usd=0.0, prompt_tokens=0,
                                             completion_tokens=0, calls=0, estimated=False))
                    stats["estimated_usd"] += float(amount)
                    stats["prompt_tokens"] += prompt + prompt_est * (attempts - 1)
                    stats["completion_tokens"] += completion + completion_max * (attempts - 1)
                    stats["calls"] += attempts
                    stats["estimated"] |= estimated
                    if result:
                        self.rate_headers.append(dict(stage=stage, headers=result.rate_headers))
                    try:
                        self._check(D(0), app, lane, day)
                    except BudgetStopped as exc:
                        self.stopped_reason = str(exc)
                return result
        except BudgetStopped as exc:
            self.stopped_reason = str(exc)
            raise
        except (OSError, ValueError, KeyError, TypeError):
            self.stopped_reason = "Ledger unavailable or invalid; refusing further calls"
            raise BudgetStopped(self.stopped_reason) from None

    def report(self):
        return dict(stages=self.costs, total_usd=float(self.run_spent),
                    usage=("none" if not self.costs else "estimated" if any(v["estimated"]
                           for v in self.costs.values()) else "reported"),
                    rate_headers=self.rate_headers)
