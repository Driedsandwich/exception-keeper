"""Conservative structural heuristics. Candidates are not error decisions."""

from collections import Counter
import re

from .candidates import make_candidate
from .context import column_header, row_label
from .loader import Cell, Sheet, Workbook, is_number
from .r1c1 import normalize, parse_reference, references, tokens


AGGREGATES = {"SUM", "AVERAGE", "COUNT", "MIN", "MAX"}
ASSUMPTION = re.compile(
    r"\b(rate|tax|fx|exchange|inflation|discount)\b|%|税率|為替|\b[A-Z]{3}\s*/\s*[A-Z]{3}\b",
    re.IGNORECASE,
)


def _aggregate_refs(formula: str):
    """Yield direct range operands of aggregate functions, including nested calls."""
    stack = []
    for token in tokens(formula):
        if token.subtype == "OPEN":
            stack.append(token.value[:-1].upper() if token.type == "FUNC" else None)
        elif token.subtype == "CLOSE":
            if stack:
                stack.pop()
        elif token.subtype == "RANGE" and stack and stack[-1] in AGGREGATES:
            ref = parse_reference(token.value)
            if ref and ref.end:
                yield ref


def _summary(cell: Cell, axis: str) -> bool:
    for ref in _aggregate_refs(cell.formula or ""):
        if ref.sheet and ref.sheet.casefold() != cell.sheet.casefold():
            continue
        r1, c1, r2, c2 = ref.bounds
        if axis == "column" and c1 == c2 == cell.column and not r1 <= cell.row <= r2:
            return True
        if axis == "row" and r1 == r2 == cell.row and not c1 <= cell.column <= c2:
            return True
    return False


def _lines(sheet: Sheet):
    for col in range(1, sheet.max_column + 1):
        yield "column", [sheet.at(row, col) for row in range(1, sheet.max_row + 1)]
    for row in range(1, sheet.max_row + 1):
        yield "row", [sheet.at(row, col) for col in range(1, sheet.max_column + 1)]


def pattern_break(book: Workbook):
    candidates = []
    for sheet in book.sheets.values():
        for axis, line in _lines(sheet):
            run = []

            def inspect_run():
                if len(run) < 3:
                    return
                patterns = [normalize(c.formula, c.coordinate) for c in run]
                majority, count = Counter(patterns).most_common(1)[0]
                if count < 2 or count <= len(run) / 2:
                    return
                for cell, pattern in zip(run, patterns):
                    if pattern != majority:
                        candidate = make_candidate(book, cell, "pattern_break", majority)
                        candidate.context["pattern_axis"] = axis
                        candidates.append(candidate)

            for cell in line:
                if cell.formula and not _summary(cell, axis):
                    run.append(cell)
                else:
                    inspect_run()
                    run = []
            inspect_run()
    return candidates


def hardcoded_in_formula_run(book: Workbook):
    candidates = []
    for sheet in book.sheets.values():
        for axis, line in _lines(sheet):
            for before, cell, after in zip(line, line[1:], line[2:]):
                if cell.formula or not is_number(cell.cached_value):
                    continue
                if not before.formula or not after.formula or _summary(before, axis) or _summary(after, axis):
                    continue
                majority = normalize(before.formula, before.coordinate)
                if majority == normalize(after.formula, after.coordinate):
                    candidate = make_candidate(book, cell, "hardcoded_in_formula_run", majority)
                    candidate.context["pattern_axis"] = axis
                    candidates.append(candidate)
    return candidates


def aggregate_range_gap(book: Workbook):
    candidates = []
    for sheet in book.sheets.values():
        for cell in sheet.cells.values():
            if not cell.formula:
                continue
            all_refs = references(cell.formula)
            omitted = {}
            for ref in _aggregate_refs(cell.formula):
                target = book.sheet(ref.sheet or cell.sheet)
                if target is None:
                    continue
                r1, c1, r2, c2 = ref.bounds
                single = r1 == r2 and c1 == c2
                same_sheet = target.name == cell.sheet
                if c1 == c2 and (r1 < r2 or (single and same_sheet and cell.column == c2 and cell.row > r2)):
                    row, col, dr, dc = r2 + 1, c2, 1, 0
                elif r1 == r2 and (c1 < c2 or (single and same_sheet and cell.row == r2 and cell.column > c2)):
                    row, col, dr, dc = r2, c2 + 1, 0, 1
                else:
                    continue
                # Follow the numeric tail of the block, never cross the total itself.
                while row <= target.max_row and col <= target.max_column:
                    if target.name == cell.sheet and (row, col) == (cell.row, cell.column):
                        break
                    extra = target.at(row, col)
                    if not (is_number(extra.cached_value) or extra.formula):
                        break
                    if _summary(extra, "column" if dr else "row"):
                        break
                    covered = any(
                        (r.sheet or cell.sheet).casefold() == target.name.casefold()
                        and r.bounds[0] <= row <= r.bounds[2]
                        and r.bounds[1] <= col <= r.bounds[3]
                        for r in all_refs
                    )
                    if not covered:
                        omitted[(target.name, extra.coordinate)] = extra
                    row, col = row + dr, col + dc
            if omitted:
                cells = list(omitted.values())
                impact = (sum(c.cached_value for c in cells)
                          if all(is_number(c.cached_value) for c in cells) else None)
                candidate = make_candidate(book, cell, "aggregate_range_gap", impact=impact)
                candidate.context["omitted_cells"] = [
                    {"sheet": c.sheet, "cell": c.coordinate, "value": c.cached_value} for c in cells
                ]
                candidates.append(candidate)
    return candidates


def _in_data_block(sheet: Sheet, row: int, col: int) -> bool:
    """A labelled row/column with occupied cells on both sides of the hole."""
    target = sheet.at(row, col)
    vertical = (1 < row < sheet.max_row and sheet.at(row - 1, col).occupied
                and sheet.at(row + 1, col).occupied)
    horizontal = (1 < col < sheet.max_column and sheet.at(row, col - 1).occupied
                  and sheet.at(row, col + 1).occupied)
    return bool(row_label(sheet, target) and column_header(sheet, target) and (vertical or horizontal))


def empty_reference(book: Workbook):
    candidates = []
    for sheet in book.sheets.values():
        for cell in sheet.cells.values():
            if not cell.formula:
                continue
            empty = set()
            for ref in references(cell.formula):
                target = book.sheet(ref.sheet or cell.sheet)
                if target is None:
                    continue
                r1, c1, r2, c2 = ref.bounds
                # Avoid expanding whole-column-sized ranges or out-of-sheet blanks.
                if (r2 - r1 + 1) * (c2 - c1 + 1) > 10_000:
                    continue
                for row in range(r1, min(r2, target.max_row) + 1):
                    for col in range(c1, min(c2, target.max_column) + 1):
                        blank = target.at(row, col)
                        if not blank.occupied and _in_data_block(target, row, col):
                            empty.add((target.name, blank.coordinate))
            if empty:
                candidate = make_candidate(book, cell, "empty_reference")
                candidate.context["empty_references"] = [
                    {"sheet": name, "cell": address} for name, address in sorted(empty)
                ]
                candidates.append(candidate)
    return candidates


def labelled_constant(book: Workbook):
    candidates = []
    for sheet in book.sheets.values():
        for cell in sheet.cells.values():
            if cell.formula or not is_number(cell.cached_value):
                continue
            labels = [row_label(sheet, cell), column_header(sheet, cell)]
            if any(ASSUMPTION.search(label) for label in labels if label):
                candidates.append(make_candidate(book, cell, "labelled_constant"))
    return candidates
