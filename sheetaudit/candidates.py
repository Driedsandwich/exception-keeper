"""Candidate payloads contain evidence, never an error verdict."""

from dataclasses import dataclass
from hashlib import sha256

from .context import cell_context
from .loader import Cell, Workbook
from .r1c1 import normalize


@dataclass
class Candidate:
    id: str
    sheet: str
    cell: str
    kind: list[str]
    formula: str | None
    r1c1: str | None
    value: object
    majority_r1c1: str | None
    neighbours: list[dict]
    context: dict
    impact: int | float | None


def snapshot(cell: Cell) -> dict:
    return {"cell": cell.coordinate, "formula": cell.formula,
            "value": cell.cached_value,
            "r1c1": normalize(cell.formula, cell.coordinate) if cell.formula else None}


def make_candidate(book: Workbook, cell: Cell, kind: str,
                   majority: str | None = None, impact=None) -> Candidate:
    sheet = book.sheets[cell.sheet]
    neighbours = [snapshot(sheet.at(row, cell.column))
                  for row in range(max(1, cell.row - 3), min(sheet.max_row, cell.row + 3) + 1)
                  if row != cell.row]
    context = cell_context(book, cell)
    context["row_neighbours"] = [snapshot(sheet.at(cell.row, col))
                                 for col in range(max(1, cell.column - 3),
                                                  min(sheet.max_column, cell.column + 3) + 1)
                                 if col != cell.column]
    identity = sha256(f"{cell.sheet}\0{cell.coordinate}".encode()).hexdigest()[:16]
    return Candidate(identity, cell.sheet, cell.coordinate, [kind], cell.formula,
                     normalize(cell.formula, cell.coordinate) if cell.formula else None,
                     cell.cached_value, majority, neighbours, context, impact)
