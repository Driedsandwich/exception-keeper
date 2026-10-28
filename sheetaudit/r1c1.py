"""Normalize supported A1 cell references, leaving literal strings untouched."""

from dataclasses import dataclass
import re

from openpyxl.formula.tokenizer import Tokenizer, TokenizerError
from openpyxl.utils.cell import column_index_from_string, coordinate_to_tuple


_CELL = r"\$?[A-Za-z]{1,3}\$?[1-9][0-9]*"
_REFERENCE = re.compile(
    rf"(?:(?P<sheet>'(?:[^']|'')+'|[^\s!'\[\]:]+)!)?"
    rf"(?P<start>{_CELL})(?::(?P<end>{_CELL}))?\Z"
)
_PARTS = re.compile(r"(\$?)([A-Za-z]+)(\$?)([0-9]+)\Z")


@dataclass(frozen=True)
class Reference:
    sheet: str | None
    prefix: str
    start: str
    end: str | None

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        r1, c1 = coordinate_to_tuple(self.start.replace("$", ""))
        r2, c2 = coordinate_to_tuple((self.end or self.start).replace("$", ""))
        return min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2)


def parse_reference(text: str) -> Reference | None:
    match = _REFERENCE.fullmatch(text)
    if not match:
        return None
    start, end = match["start"], match["end"]
    for address in (start, end):
        if address:
            row, col = coordinate_to_tuple(address.replace("$", ""))
            if row > 1_048_576 or col > 16_384:
                return None
    raw_sheet = match["sheet"]
    sheet = raw_sheet
    if sheet and sheet.startswith("'"):
        sheet = sheet[1:-1].replace("''", "'")
    return Reference(sheet, f"{raw_sheet}!" if raw_sheet else "", start, end)


def tokens(formula: str):
    try:
        return Tokenizer(formula).items
    except TokenizerError:
        return []


def references(formula: str) -> list[Reference]:
    return [ref for token in tokens(formula)
            if token.type == "OPERAND" and token.subtype == "RANGE"
            if (ref := parse_reference(token.value)) is not None]


def normalize(formula: str, coordinate: str) -> str:
    """Return R1C1 text relative to coordinate; unsupported operands stay as-is."""
    row, column = coordinate_to_tuple(coordinate)

    def convert(address: str) -> str:
        col_abs, col, row_abs, ref_row = _PARTS.fullmatch(address).groups()
        ref_col = column_index_from_string(col)
        r = str(int(ref_row)) if row_abs else (f"[{int(ref_row) - row}]" if int(ref_row) != row else "")
        c = str(ref_col) if col_abs else (f"[{ref_col - column}]" if ref_col != column else "")
        return f"R{r}C{c}"

    parsed = tokens(formula)
    if not parsed:
        return formula
    parts = []
    for token in parsed:
        ref = parse_reference(token.value) if token.subtype == "RANGE" else None
        if ref:
            parts.append(ref.prefix + convert(ref.start) + (":" + convert(ref.end) if ref.end else ""))
        else:
            parts.append(token.value)
    return "=" + "".join(parts)
