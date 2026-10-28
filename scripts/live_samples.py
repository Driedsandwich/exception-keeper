"""Run sample workbooks live through the staged handler the web app uses.

Writes samples/precomputed/<name>.json (source "live") only when every stage succeeded,
plus a per-call log (model, max_tokens, finish_reason, usage; never message text).
The key is read from NEBIUS_API_KEY only. EK_GRANTED_CREDITS_USD is the credit actually
granted, shared by both apps; the shared ledger enforces it with the other caps.

Usage: NEBIUS_API_KEY=... EK_GRANTED_CREDITS_USD=0.90 python scripts/live_samples.py <log.json> [name ...]
"""
import base64
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sheetaudit.api import Dependencies, handle  # noqa: E402
from sheetaudit.budget import FileLedger, JST  # noqa: E402
from sheetaudit.llm.client import HttpChatClient, LLMError  # noqa: E402
from sheetaudit.llm.models import NANO, SUPER, ULTRA  # noqa: E402

NAMES = ["club_membership_fees", "small_business_budget", "event_budget"]


class LoggingClient:
    """Pass-through client that keeps per-call metadata only."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    @property
    def max_retries(self):
        return self.inner.max_retries

    def complete(self, model, messages, **kwargs):
        row = dict(model=model, max_tokens=kwargs.get("max_tokens"), tools=bool(kwargs.get("tools")),
                   response_format=(kwargs.get("response_format") or {}).get("type"))
        started = time.monotonic()
        try:
            result = self.inner.complete(model, messages, **kwargs)
        except LLMError as exc:
            self.calls.append(dict(row, error=exc.status, seconds=round(time.monotonic() - started, 2)))
            raise
        self.calls.append(dict(row, finish_reason=result.finish_reason, usage=result.usage,
                               attempts=result.attempts, tool_calls=len(result.tool_calls),
                               seconds=round(time.monotonic() - started, 2)))
        return result


def run(name, env, deps):
    workbook = base64.b64encode((ROOT / "samples" / (name + ".xlsx")).read_bytes()).decode()
    send = lambda stage, **extra: handle(dict(stage=stage, workbook_b64=workbook, **extra), env, deps)
    steps = []
    status, start = send("start")
    steps.append(dict(stage="start", status=status, cost=start.get("session_cost_usd")))
    if status != 200:
        return None, steps, start
    decisions, warnings = [], list(start.get("warnings", []))
    for candidate in start["candidates"][:start["max_candidates"]]:
        status, body = send("judge", session=start["session"],
                            payload=dict(candidate_id=candidate["id"], outline=start["outline"]))
        steps.append(dict(stage="judge", candidate=candidate["id"], status=status, cost=body.get("session_cost_usd"),
                          verdict=body.get("decision", {}).get("verdict"), grounding=body.get("decision", {}).get("grounding")))
        if status != 200:
            return None, steps, body
        decisions.append(body["decision"])
        warnings += body.get("warnings", [])
    status, final = send("summary", session=start["session"],
                         payload=dict(decisions=decisions, outline=start["outline"]))
    steps.append(dict(stage="summary", status=status, cost=final.get("session_cost_usd")))
    if status != 200:
        return None, steps, final
    report = dict(workbook=name + ".xlsx", generated_at=datetime.now(JST).isoformat(), source="live",
                  models=dict(structure=NANO, judge=SUPER, summary=ULTRA), outline=start["outline"],
                  findings=final["findings"], summary=final["summary"],
                  cost=dict(total_usd=final["session_cost_usd"], usage="ledger"),
                  stopped_reason=None, warnings=warnings + final.get("warnings", []), precomputed_sample=None)
    return report, steps, None


def main():
    log_path = Path(sys.argv[1])
    names = sys.argv[2:] or NAMES
    env = {k: os.environ[k] for k in ("NEBIUS_API_KEY", "EK_GRANTED_CREDITS_USD", "EK_JUDGE_PASSPHRASE")
           if k in os.environ}
    env["EK_LIVE"] = "1"
    client = LoggingClient(HttpChatClient(timeout_s=60, max_retries=1))
    deps = Dependencies(FileLedger(os.environ.get("X129_LEDGER_PATH") or Path.home() / ".x129" / "ledger.jsonl"), client)
    log = []
    for name in names:
        first = len(client.calls)
        report, steps, error = run(name, env, deps)
        log.append(dict(sample=name, ok=report is not None, steps=steps, error=error, calls=client.calls[first:]))
        if report is not None:
            out = ROOT / "samples" / "precomputed" / (name + ".json")
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        verdicts = [s.get("verdict") for s in steps if s["stage"] == "judge"]
        print(name, "ok" if report else "stopped", f"${steps[-1].get('cost') or 0:.6f}", verdicts,
              [c.get("finish_reason") for c in client.calls[first:]])
        if report is None:
            break
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
