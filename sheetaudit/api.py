"""Transport-independent POST /api/ek. Storage, clock and chat are injected.

Only hashes and accounting markers persist; workbook bytes remain in memory.
"""
import base64
import binascii
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import io
import json
from typing import Mapping
from uuid import UUID, uuid4
import zipfile

from .budget import Budget, BudgetStopped, JST
from .engine import audit_book
from .llm.client import MockChatClient, redact
from .llm.jsonout import STYLES, validate
from .llm.models import NANO, SUPER, ULTRA
from .loader import load
from .review.judge import fallback, judge
from .review.schemas import OUTLINE
from .review.structure import structure, validate_outline
from .review.summary import summarize

MAX_UPLOAD = 1_000_000
MAX_REQUEST = 1_600_000
MAX_CANDIDATES = 12
DISABLED = "Live calls are disabled. Use the sample results (cost $0)."
# Live 2026-10-24: strict json_schema answers cut quotes at the first double quote (=IF(B14= for
# =IF(B14="Student",...)), which still grounds as a substring; json_object answers broke the shape
# or ran out of tokens more often (7/12 against 9/12 twice), so the strict schema stays the default.
DEFAULT_SCHEMA_STYLE = "openai"


@dataclass
class Dependencies:
    ledger: object
    client: object
    now: object = lambda: datetime.now(JST)


class InputError(Exception):
    def __init__(self, code="bad_request"):
        self.code = code


def fingerprint(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def choose_lane(passphrase, env):
    expected = env.get("EK_JUDGE_PASSPHRASE", "")
    supplied = passphrase if isinstance(passphrase, str) else ""
    digest = lambda text: hashlib.sha256(text.encode()).digest()
    equal = hmac.compare_digest(digest(supplied), digest(expected))
    return "judge" if expected and equal else "public"


def upload(encoded):
    if not isinstance(encoded, str):
        raise InputError()
    if len(encoded) > 4 * ((MAX_UPLOAD + 2) // 3):
        raise InputError("too_large")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise InputError() from None
    if len(raw) > MAX_UPLOAD:
        raise InputError("too_large")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > 200 or sum(m.file_size for m in members) > 20_000_000:
                raise InputError("too_large")
            names = set(archive.namelist())
            if not {"[Content_Types].xml", "xl/workbook.xml"} <= names:
                raise InputError("unsupported_file")
            # Reject macro-enabled packages even if renamed to .xlsx.
            types = archive.read("[Content_Types].xml")
            if (b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml" not in types
                    or any("vbaproject" in n.lower() for n in names)):
                raise InputError("unsupported_file")
        book = load(io.BytesIO(raw))
    except InputError:
        raise
    except ValueError as exc:
        code = "too_large" if "200,000-cell scan limit" in str(exc) else "unsupported_file"
        raise InputError(code) from None
    except Exception:
        raise InputError("unsupported_file") from None
    return book, hashlib.sha256(raw).hexdigest()


def handle(request: dict, env: Mapping, deps: Dependencies):
    """Return (HTTP status, JSON body); never expose exception text or credentials."""
    session, budget, started = None, None, False
    stage, lane = None, None
    secrets = [env.get("NEBIUS_API_KEY"), env.get("EK_JUDGE_PASSPHRASE")]
    if isinstance(request, dict):
        secrets.append(request.get("passphrase"))

    def safe(body):
        # Saved dates use the same JSON representation across transports and hashes.
        body = json.loads(json.dumps(body, ensure_ascii=False, default=str, allow_nan=False))
        for secret in secrets:
            if isinstance(secret, str) and secret:
                body = redact(body, secret)
        return body

    def reply(status, body):
        body = safe(body)
        return status, body

    def day():
        instant = deps.now()
        if instant.tzinfo is None:
            raise BudgetStopped("Clock must be timezone aware")
        return instant.astimezone(JST).date().isoformat()

    def finish(success, **details):
        nonlocal started
        deps.ledger.add("ek", lane, day(), 0, session=session, stage=stage,
                        event="finished", success=success, **details)
        started = False

    def costs():
        spent = float(deps.ledger.spent_session("ek", session)) if session else 0
        return dict(session_cost_usd=spent, remaining_session_usd=max(0, .15 - spent),
                    cost=budget.report() if budget else {"total_usd": 0})

    try:
        if (not isinstance(request, dict) or set(request) -
                {"stage", "session", "workbook_b64", "payload", "passphrase"}):
            raise InputError()
        try:
            size = len(json.dumps(request, allow_nan=False))
        except (TypeError, ValueError):
            raise InputError() from None
        if size > MAX_REQUEST:
            raise InputError("too_large")
        stage = request.get("stage")
        payload = request.get("payload", {})
        if (not isinstance(stage, str) or stage not in {"start", "judge", "summary"} or not isinstance(payload, dict)
                or ("passphrase" in request and
                    (not isinstance(request["passphrase"], str) or len(request["passphrase"]) > 1000))):
            raise InputError()
        allowed = {"start": set(), "judge": {"candidate_id", "outline"},
                   "summary": {"decisions", "outline"}}[stage]
        if set(payload) - allowed:
            raise InputError()
        schema_style = env.get("EK_SCHEMA_STYLE", DEFAULT_SCHEMA_STYLE)
        judge_mode = env.get("EK_JUDGE_MODE", "tools")
        if schema_style not in STYLES or judge_mode not in {"tools", "json_only"}:
            raise InputError()
        lane = choose_lane(request.get("passphrase"), env)
        if stage == "start":
            if request.get("session") is not None:
                raise InputError()
            session = str(uuid4())
        else:
            value = request.get("session")
            try:
                valid_session = isinstance(value, str) and str(UUID(value)) == value
            except ValueError:
                valid_session = False
            if not valid_session:
                raise InputError()
            session = value
        book, workbook_hash = upload(request.get("workbook_b64"))
        candidates = audit_book(book)
        by_id = {c.id: c for c in candidates}
        candidate_id = payload.get("candidate_id")
        if stage == "judge" and (not isinstance(candidate_id, str) or candidate_id not in by_id):
            raise InputError()
        outline = payload.get("outline", {"sheets": []})
        if validate(outline, OUTLINE):
            raise InputError()
        warnings = []
        outline = validate_outline(outline, book, warnings)

        # Serialize validation and admission with the same ledger lock as spending.
        with deps.ledger.locked():
            mine = [r for r in deps.ledger.records() if r["app"] == "ek" and r.get("session") == session]
            completed = [r for r in mine if r.get("event") == "finished" and r.get("success")]
            if stage != "start":
                begins = [r for r in mine if r.get("event") == "started"]
                ends = [r for r in mine if r.get("event") == "finished"]
                origin = next((r for r in completed if r["stage"] == "start"), None)
                if (not origin or origin["lane"] != lane or len(begins) != len(ends)
                        or origin.get("workbook_hash") != workbook_hash
                        or any(r["stage"] == "summary" for r in begins)):
                    raise InputError()
                if "outline" in payload and fingerprint(outline) != origin["outline_hash"]:
                    raise InputError()
                judges = [r for r in begins if r["stage"] == "judge"]
                if stage == "judge" and (len(judges) >= MAX_CANDIDATES or
                        any(r.get("candidate_id") == candidate_id for r in judges)):
                    raise InputError()
                if stage == "summary":
                    decisions = payload.get("decisions")
                    judged = {r["candidate_id"]: r["decision_hash"] for r in completed if r["stage"] == "judge"}
                    if (not isinstance(decisions, list) or len(decisions) > MAX_CANDIDATES
                            or (candidates and not judged) or len(decisions) != len(judged)):
                        raise InputError()
                    seen = set()
                    for decision in decisions:
                        if not isinstance(decision, dict):
                            raise InputError()
                        cid = decision.get("candidate_id")
                        if (not isinstance(cid, str) or cid not in by_id or cid in seen
                                or fingerprint(decision) != judged.get(cid)):
                            raise InputError()
                        seen.add(cid)
            try:
                credits = Decimal(env.get("EK_GRANTED_CREDITS_USD", "0"))
            except (InvalidOperation, TypeError, ValueError):
                credits = Decimal(0)
            if env.get("EK_LIVE") != "1" or not credits.is_finite() or credits <= 0:
                return reply(429, dict(error="budget", message=DISABLED))
            budget = Budget(deps.ledger, now=deps.now, session=session, credits_usd=credits)
            deps.ledger.add("ek", lane, day(), 0, session=session, stage=stage,
                            event="started", **({"candidate_id": candidate_id} if stage == "judge" else {}))
            started = True

        model_stage = "structure" if stage == "start" else stage
        client = deps.client.for_stage(model_stage, candidate_id) if isinstance(deps.client, MockChatClient) else deps.client

        def call(model, messages, **kwargs):
            return budget.complete(client, model_stage, "ek", lane, model, messages, **kwargs)

        if stage == "start":
            outline = structure(book, call, warnings, schema_style)
            data = dict(outline=outline, candidates=[asdict(c) for c in candidates],
                        max_candidates=MAX_CANDIDATES,
                        sheets=[dict(name=s.name, max_row=s.max_row, max_column=s.max_column)
                                for s in book.sheets.values()])
            details = dict(workbook_hash=workbook_hash, outline_hash=fingerprint(outline))
        elif stage == "judge":
            candidate = by_id[candidate_id]
            decision = dict(asdict(candidate), **judge(candidate, book, outline, call,
                            schema_style=schema_style, judge_mode=judge_mode))
            decision = safe(decision)
            data = dict(decision=decision)
            details = dict(candidate_id=candidate_id, decision_hash=fingerprint(decision))
        else:
            # Rebuild all engine fields. Never use a client's or a model's impact.
            selected = {d["candidate_id"]: d for d in decisions}
            findings = []
            for candidate in candidates:
                reason = "not reviewed (limit)" if len(selected) >= MAX_CANDIDATES else "not reviewed (session ended)"
                decision = selected.get(candidate.id, fallback(candidate, reason))
                if candidate.id not in selected:
                    decision["verdict"] = "not reviewed (limit)" if len(selected) >= MAX_CANDIDATES else "not reviewed"
                findings.append(dict(decision, **asdict(candidate)))
            data = dict(summary=summarize(findings, outline, call, warnings, schema_style), findings=findings)
            details = {}
        data = safe(data)
        if stage == "start":
            details["outline_hash"] = fingerprint(data["outline"])
        with deps.ledger.locked():
            finish(True, **details)
            cost = costs()
        return reply(200, dict(data, session=session, lane=lane, warnings=warnings,
                              model={"start": NANO, "judge": SUPER, "summary": ULTRA}[stage],
                              source="mock" if isinstance(deps.client, MockChatClient) else "live", **cost))
    except Exception as exc:
        if started:
            try:
                with deps.ledger.locked():
                    finish(False)
            except Exception:
                pass  # An unfinished marker fails closed on subsequent requests.
        code = exc.code if isinstance(exc, InputError) else "budget" if isinstance(exc, BudgetStopped) else "upstream_error"
        try:
            cost = costs()
        except Exception:
            cost = {}
        messages = {"budget": "Budget refused. Use the sample results (cost $0).",
                    "bad_request": "Invalid input or stage order.",
                    "too_large": "Workbook or request exceeds the size limit.",
                    "unsupported_file": "Upload an ordinary .xlsx workbook.",
                    "upstream_error": "Model service or ledger unavailable. Use the sample results."}
        return reply({"budget": 429, "bad_request": 400, "too_large": 413,
                      "unsupported_file": 415}.get(code, 502),
                     dict(error=code, message=messages[code], **cost))
