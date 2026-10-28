"""First live check against Token Factory: at most 6 small calls, recorded to a JSON file.

Answers the open questions from MODEL_STAGE_NOTES.md before any real review runs:
which response_format style each model accepts, whether Super emits tool calls, and
what usage, cost and rate-limit headers look like. Spending goes through the shared
ledger and the normal caps. The key is read from NEBIUS_API_KEY only.

Usage: NEBIUS_API_KEY=... python scripts/first_live_check.py <out.json>
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sheetaudit.budget import Budget, BudgetStopped, FileLedger  # noqa: E402
from sheetaudit.llm.client import HttpChatClient, LLMError  # noqa: E402
from sheetaudit.llm.jsonout import parse, response_format  # noqa: E402
from sheetaudit.llm.models import BASE_URL, NANO, SUPER, ULTRA  # noqa: E402

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string", "enum": ["ok"]}},
          "required": ["answer"], "additionalProperties": False}
ASK = [{"role": "system", "content": "Reply with JSON only: {\"answer\": \"ok\"}."},
       {"role": "user", "content": "Return the JSON object now."}]
TOOL = [{"type": "function", "function": {
    "name": "read_range", "description": "Read cells from a sheet.",
    "parameters": {"type": "object", "properties": {"sheet": {"type": "string"}, "range": {"type": "string"}},
                   "required": ["sheet", "range"], "additionalProperties": False}}}]
TOOL_ASK = [{"role": "system", "content": "Use the read_range tool to read Members!C1:C5 before answering."},
            {"role": "user", "content": "What is in Members!C1:C5?"}]


def record(budget, client, label, model, messages, **kwargs):
    started = time.monotonic()
    row = dict(label=label, model=model, response_format=kwargs.get("response_format"))
    try:
        result = budget.complete(client, "live_check", "ek", "public", model, messages, **kwargs)
    except BudgetStopped as exc:
        row.update(outcome="budget_stopped", detail=str(exc))
        return row
    except LLMError as exc:
        row.update(outcome="http_error", status=exc.status, detail=str(exc))
        return row
    row.update(outcome="ok", seconds=round(time.monotonic() - started, 2), usage=result.usage,
               rate_headers=result.rate_headers, tool_calls=[c.get("function", {}).get("name") for c in result.tool_calls],
               content_head=(result.content or "")[:300])
    try:
        row["parsed"] = parse(result.content)
    except ValueError as exc:
        row["parsed"] = None
        row["parse_error"] = str(exc)
    return row


def main():
    out = Path(sys.argv[1])
    # EK_BASE_URL and X129_LEDGER_PATH exist only to dry-run this script against a local stub.
    client = HttpChatClient(base_url=os.environ.get("EK_BASE_URL", BASE_URL), timeout_s=60, max_retries=1)
    # A one-off check: no granted-credits cap here (production paths go through sheetaudit.api.handle).
    budget = Budget(FileLedger(os.environ.get("X129_LEDGER_PATH") or Path.home() / ".x129" / "ledger.jsonl"))
    rows = []
    for style in ("openai", "nebius_doc", "json_object"):
        rows.append(record(budget, client, f"nano_{style}", NANO, ASK,
                           response_format=response_format(SCHEMA, style), max_tokens=600))
    rows.append(record(budget, client, "super_tools", SUPER, TOOL_ASK, tools=TOOL, tool_choice="auto", max_tokens=600))
    rows.append(record(budget, client, "super_json_object", SUPER, ASK,
                       response_format=response_format(SCHEMA, "json_object"), max_tokens=600))
    rows.append(record(budget, client, "ultra_json_object", ULTRA, ASK,
                       response_format=response_format(SCHEMA, "json_object"), max_tokens=600))
    report = dict(rows=rows, cost=budget.report())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    for row in rows:
        print(row["label"], row["outcome"], row.get("status", ""), "parsed" if row.get("parsed") else "", row.get("tool_calls", ""))
    print("total_usd", report["cost"]["total_usd"])


if __name__ == "__main__":
    main()
