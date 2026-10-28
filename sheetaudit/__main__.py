"""Command-line entry point for workbook candidate inspection."""

import argparse
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json

from .engine import audit


def json_value(value):
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, (timedelta, Decimal)):
        return str(value)
    raise TypeError(f"Unsupported workbook value: {type(value).__name__}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Find structural spreadsheet audit candidates.")
    parser.add_argument("path", help="Path to an .xlsx workbook")
    parser.add_argument("--json", action="store_true", help="Print full candidate evidence as JSON")
    args = parser.parse_args()
    try:
        candidates = audit(args.path)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"sheetaudit: {exc}\n")
    if args.json:
        print(json.dumps([asdict(c) for c in candidates], ensure_ascii=False, indent=2, default=json_value))
    else:
        for candidate in candidates:
            print(f"{candidate.sheet}!{candidate.cell}\t{','.join(candidate.kind)}\t"
                  f"value={candidate.value!r}\timpact={candidate.impact!r}")
        print(f"{len(candidates)} candidate(s)")


if __name__ == "__main__":
    main()
