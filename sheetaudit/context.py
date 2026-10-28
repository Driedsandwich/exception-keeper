"""Small, deterministic context windows for later human or LLM assessment."""

from .loader import Cell, Sheet, Workbook
from .r1c1 import references


NOTES_HEADERS = {"notes", "note", "comment", "remarks", "memo", "備考"}


def _text(cell: Cell) -> bool:
    return cell.formula is None and isinstance(cell.cached_value, str) and bool(cell.cached_value.strip())


def row_label(sheet: Sheet, cell: Cell) -> str | None:
    return next((sheet.at(cell.row, col).cached_value
                 for col in range(cell.column - 1, 0, -1)
                 if _text(sheet.at(cell.row, col))), None)


def column_header(sheet: Sheet, cell: Cell) -> str | None:
    # The initial text rows before the first numeric/formula row form the header area.
    first_data = min((c.row for c in sheet.cells.values()
                      if c.formula or isinstance(c.cached_value, (int, float))),
                     default=sheet.max_row + 1)
    return next((sheet.at(row, cell.column).cached_value
                 for row in range(min(cell.row - 1, first_data - 1), 0, -1)
                 if _text(sheet.at(row, cell.column))), None)


def cell_context(book: Workbook, cell: Cell) -> dict:
    sheet = book.sheets[cell.sheet]
    note_headers = [c for c in sheet.cells.values()
                    if c.row < cell.row and _text(c)
                    and c.cached_value.strip().casefold() in NOTES_HEADERS]
    note_header = max(note_headers, key=lambda c: (c.row, -c.column), default=None)
    context = {
        "row_label": row_label(sheet, cell),
        "column_header": column_header(sheet, cell),
        "notes": sheet.at(cell.row, note_header.column).cached_value if note_header else None,
        "notes_cell": sheet.at(cell.row, note_header.column).coordinate if note_header else None,
        "comment": cell.comment,
        "number_format": cell.number_format,
    }
    # Include rule values and their labels; bound context size even for large ranges.
    targets = []
    seen = set()
    for ref in references(cell.formula or ""):
        target_sheet = book.sheet(ref.sheet or cell.sheet)
        if target_sheet is None:
            continue
        for address in (ref.start, ref.end):
            if not address:
                continue
            target = target_sheet.cells.get(address.replace("$", "").upper())
            key = (target_sheet.name, address.replace("$", "").upper())
            if key in seen or len(targets) >= 24:
                continue
            seen.add(key)
            targets.append({"sheet": key[0], "cell": key[1],
                            "formula": target.formula if target else None,
                            "value": target.cached_value if target else None,
                            "row_label": row_label(target_sheet, target) if target else None,
                            "comment": target.comment if target else None})
    context["references"] = targets
    return context
