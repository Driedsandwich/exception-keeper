"""Run all detectors, merge by cell, and return a stable sheet/row/column order."""

from pathlib import Path

from openpyxl.utils.cell import coordinate_to_tuple

from .candidates import Candidate
from .detectors import (aggregate_range_gap, empty_reference, hardcoded_in_formula_run,
                        labelled_constant, pattern_break)
from .loader import load


def audit(path: str | Path) -> list[Candidate]:
    book = load(path)
    return audit_book(book)


def audit_book(book) -> list[Candidate]:
    """Use an already loaded workbook; the API never needs a temporary file."""
    merged = {}
    for detector in (pattern_break, hardcoded_in_formula_run, aggregate_range_gap,
                     empty_reference, labelled_constant):
        for candidate in detector(book):
            key = (candidate.sheet, candidate.cell)
            if key not in merged:
                merged[key] = candidate
                continue
            existing = merged[key]
            existing.kind = sorted(set(existing.kind + candidate.kind))
            existing.majority_r1c1 = existing.majority_r1c1 or candidate.majority_r1c1
            existing.context.update(candidate.context)
            if candidate.impact is not None:
                existing.impact = candidate.impact
    sheet_order = {name: i for i, name in enumerate(book.sheets)}
    return sorted(merged.values(), key=lambda c: (sheet_order[c.sheet], *coordinate_to_tuple(c.cell)))
