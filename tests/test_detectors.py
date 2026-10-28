"""Small synthetic regressions independent of the three sample layouts."""

import pytest
import xlsxwriter

from sheetaudit import audit
from sheetaudit.context import cell_context
from sheetaudit.loader import load


@pytest.mark.parametrize("function", ["SUM", "AVERAGE", "COUNT", "MIN", "MAX"])
@pytest.mark.parametrize("axis", ["row", "column"])
def test_multiple_omissions_all_aggregate_functions(tmp_path, function, axis):
    path = tmp_path / "aggregate.xlsx"
    with xlsxwriter.Workbook(path) as book:
        sheet = book.add_worksheet("Data")
        if axis == "column":
            for row, value in enumerate([10, 20, 40, 50], 1):
                sheet.write_number(row, 1, value)
            sheet.write_formula("B6", f"={function}(B2:B3)", None, 0)
            address, missing = "B6", ["B4", "B5"]
        else:
            for col, value in enumerate([10, 20, 40, 50], 1):
                sheet.write_number(1, col, value)
            sheet.write_formula("F2", f"={function}(B2:C2)", None, 0)
            address, missing = "F2", ["D2", "E2"]
    candidate = next(c for c in audit(path) if "aggregate_range_gap" in c.kind)
    assert candidate.cell == address
    assert candidate.impact == 90
    assert [c["cell"] for c in candidate.context["omitted_cells"]] == missing


def test_explicit_extra_arguments_and_separate_blocks_are_not_gaps(tmp_path):
    path = tmp_path / "covered.xlsx"
    with xlsxwriter.Workbook(path) as book:
        sheet = book.add_worksheet("Data")
        for address, value in {"B2": 10, "B3": 20, "B4": 30, "B5": 40,
                               "D2": 10, "D3": 20, "D5": 40}.items():
            sheet.write_number(address, value)
        sheet.write_formula("B6", "=SUM(B2:B3,B4,B5)", None, 100)
        sheet.write_formula("D6", "=SUM(D2:D3)", None, 30)
    assert not [c for c in audit(path) if "aggregate_range_gap" in c.kind]


def test_unknown_cache_is_not_reported_as_zero_impact(tmp_path):
    path = tmp_path / "uncached.xlsx"
    with xlsxwriter.Workbook(path) as book:
        sheet = book.add_worksheet("Data")
        sheet.write_number("B2", 10)
        sheet.write_number("B3", 20)
        sheet.write_formula("B4", "=10*5", None, "")
        sheet.write_formula("B5", "=SUM(B2:B3)", None, 30)
    candidate = next(c for c in audit(path) if c.cell == "B5")
    assert "aggregate_range_gap" in candidate.kind
    assert candidate.impact is None
    assert candidate.context["omitted_cells"][0]["value"] is None


@pytest.mark.parametrize("header", ["Notes", "NOTE", "comment", "Remarks", "memo", "備考"])
def test_note_headers_and_nearest_row_label(tmp_path, header):
    path = tmp_path / "notes.xlsx"
    with xlsxwriter.Workbook(path) as book:
        sheet = book.add_worksheet("Data")
        sheet.write_row("A1", ["Item", "Amount", header])
        sheet.write_row("A2", ["Item 01", 12, "Approved exception"])
        sheet.write_comment("B2", "Cell-level explanation")
    book = load(path)
    context = cell_context(book, book.sheets["Data"].cells["B2"])
    assert context["row_label"] == "Item 01"
    assert context["column_header"] == "Amount"
    assert context["notes"] == "Approved exception"
    assert context["comment"] == "Cell-level explanation"


def test_single_cell_range_can_have_a_tail_gap(tmp_path):
    path = tmp_path / "single.xlsx"
    with xlsxwriter.Workbook(path) as book:
        sheet = book.add_worksheet("Data")
        sheet.write_number("B2", 10)
        sheet.write_number("B3", 20)
        sheet.write_formula("B4", "=SUM(B2:B2)", None, 10)
    assert next(c for c in audit(path) if c.cell == "B4").impact == 20


def test_pattern_and_hardcode_horizontal_without_sample_positions(tmp_path):
    path = tmp_path / "horizontal.xlsx"
    with xlsxwriter.Workbook(path) as book:
        sheet = book.add_worksheet("Data")
        for col in range(1, 7):
            sheet.write_number(0, col, 10)
            letter = chr(ord("A") + col)
            formula = f"={letter}1*2"
            sheet.write_formula(1, col, formula if col != 3 else f"={letter}1*3", None,
                                20 if col != 3 else 30)
            if col == 4:
                sheet.write_number(2, col, 25)
            else:
                sheet.write_formula(2, col, f"={letter}1*2", None, 20)
    candidates = {c.cell: c for c in audit(path)}
    assert "pattern_break" in candidates["D2"].kind
    assert "hardcoded_in_formula_run" in candidates["E3"].kind


def test_cross_sheet_empty_reference_and_text_literal(tmp_path):
    path = tmp_path / "empty.xlsx"
    with xlsxwriter.Workbook(path) as book:
        inputs = book.add_worksheet("Input Data")
        inputs.write_row("A1", ["Item", "Cost"])
        inputs.write_row("A2", ["Item 01", 10])
        inputs.write("A3", "Item 02")
        inputs.write_row("A4", ["Item 03", 20])
        output = book.add_worksheet("Output")
        output.write_formula("A1", "='Input Data'!B3*2", None, 0)
        output.write_formula("A2", '="B3"', None, "B3")
    candidates = audit(path)
    assert [(c.sheet, c.cell, c.kind) for c in candidates] == [("Output", "A1", ["empty_reference"])]
