"""Explicit mock/live CLI. No network access without --live."""
import argparse
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path

from ..budget import Budget, FileLedger, MemoryLedger
from ..llm.client import HttpChatClient, MockChatClient
from ..llm.jsonout import STYLES
from .pipeline import review


def main():
    parser = argparse.ArgumentParser(description="Review spreadsheet candidates with grounded model evidence")
    parser.add_argument("path", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mock", type=Path)
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--lane", choices=("public", "judge"), default="public")
    parser.add_argument("--schema-style", choices=STYLES, default=None)
    parser.add_argument("--judge-mode", choices=("tools", "json_only"), default="tools")
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.live and not os.environ.get("NEBIUS_API_KEY"):
        parser.error("--live requires NEBIUS_API_KEY in the environment")
    credits = None
    if args.live:
        try:
            credits = Decimal(os.environ.get("EK_GRANTED_CREDITS_USD", "0"))
        except InvalidOperation:
            credits = Decimal(0)
        if os.environ.get("EK_LIVE") != "1" or not credits.is_finite() or credits <= 0:
            parser.error("Live calls are disabled. Use the sample results (cost $0).")
    if args.mock:
        client = MockChatClient(json.loads(args.mock.read_text(encoding="utf-8")))
        ledger = FileLedger(args.ledger) if args.ledger else MemoryLedger()
    else:
        client = HttpChatClient()
        # A shared durable ledger is mandatory in live mode; never default to memory.
        # One file for both apps (ek and pw), independent of the working directory.
        default = os.environ.get("X129_LEDGER_PATH") or Path.home() / ".x129" / "ledger.jsonl"
        ledger = FileLedger(args.ledger or Path(default))
    report = review(args.path, client, Budget(ledger, credits_usd=credits), lane=args.lane,
                    schema_style=args.schema_style, judge_mode=args.judge_mode)
    output = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.out:
        args.out.write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
