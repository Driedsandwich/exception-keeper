"""Generate fictional fixtures. Every formula has an explicitly computed cache."""

import json
from pathlib import Path

import xlsxwriter


def _setup(book, name, headers):
    sheet = book.add_worksheet(name)
    sheet.write_row(0, 0, headers, book.add_format({"bold": True, "bg_color": "#E2EBF3"}))
    sheet.freeze_panes(1, 1)
    sheet.set_column(0, len(headers) - 1, 18)
    return sheet


def club(path: Path) -> list[dict]:
    with xlsxwriter.Workbook(path) as book:
        money = book.add_format({"num_format": "0.00"})
        pct = book.add_format({"num_format": "0%"})
        sheet = _setup(book, "Members", ["Member", "Type", "Fee", "Notes"])
        sheet.set_column(3, 3, 38)
        rules = _setup(book, "Fee Rules", ["Rule", "Value"])
        rules.write_row(1, 0, ["Standard fee", 100])
        rules.write(2, 0, "Student rate")
        rules.write_number(2, 1, 0.5, pct)
        rules.write_row(3, 0, ["Guest fee", 75])
        fees = []
        for member in range(1, 31):
            row = member + 1
            member_type = "Student" if member % 3 == 0 else "Standard"
            sheet.write_row(row - 1, 0, [f"Member {member:02d}", member_type])
            fee = 50 if member_type == "Student" else 100
            formula = (f'=IF(B{row}="Student",\'Fee Rules\'!$B$2*'
                       "'Fee Rules'!$B$3,'Fee Rules'!$B$2)")
            if row in (6, 20, 26):
                fee = 125 if row == 6 else 0
                sheet.write_number(row - 1, 2, fee, money)
                if row == 20:
                    sheet.write(row - 1, 3, "Waived — board decision")
            else:
                if row == 14:
                    formula = formula.replace("$B$2", "$B$4")
                    fee = 37.5 if member_type == "Student" else 75
                sheet.write_formula(row - 1, 2, formula, money, fee)
            fees.append(fee)
        sheet.write(31, 0, "TOTAL")
        sheet.write_formula(31, 2, "=SUM(C2:C30)", money, sum(fees[:-1]))
    return [
        {"sheet": "Members", "cell": "C6", "kind": "hardcoded_in_formula_run", "label": "error"},
        {"sheet": "Members", "cell": "C14", "kind": "pattern_break", "label": "error"},
        {"sheet": "Members", "cell": "C20", "kind": "hardcoded_in_formula_run", "label": "intended_exception"},
        {"sheet": "Members", "cell": "C26", "kind": "hardcoded_in_formula_run", "label": "ambiguous"},
        {"sheet": "Members", "cell": "C32", "kind": "aggregate_range_gap", "label": "error"},
    ]


def business(path: Path) -> list[dict]:
    with xlsxwriter.Workbook(path) as book:
        money = book.add_format({"num_format": "0.00"})
        sheet = _setup(book, "Budget", ["Expense", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Total", "Notes", "Base"])
        for row, name in enumerate(["Rent", "Supplies", "Utilities", "Services", "Transport"], 2):
            base = 100 + 25 * (row - 2)
            sheet.write(row - 1, 0, name)
            sheet.write_number(row - 1, 9, base, money)
            values = []
            for col in range(1, 7):
                formula = f"=$J{row}*(1+$B$10)"
                value = base * 1.08
                if row == 4 and col == 3:
                    # Copying B$10 one column right drifted to the empty C10.
                    formula = f"=$J{row}*(1+C$10)"
                    value = base
                if row == 5 and col == 4:
                    value = 999
                    sheet.write_number(row - 1, col, value, money)
                else:
                    sheet.write_formula(row - 1, col, formula, money, value)
                values.append(value)
            end = "F" if row == 3 else "G"
            total = sum(values[:-1]) if row == 3 else sum(values)
            sheet.write_formula(row - 1, 7, f"=SUM(B{row}:{end}{row})", money, total)
        sheet.write(8, 0, "Assumptions")
        sheet.write(9, 0, "Tax rate")
        sheet.write_number(9, 1, 0.08, book.add_format({"num_format": "0%"}))
        sheet.write_row(10, 0, ["USD/JPY", 150])
    return [
        {"sheet": "Budget", "cell": "H3", "kind": "aggregate_range_gap", "label": "error"},
        {"sheet": "Budget", "cell": "D4", "kind": "pattern_break", "label": "error"},
        {"sheet": "Budget", "cell": "E5", "kind": "hardcoded_in_formula_run", "label": "error"},
        {"sheet": "Budget", "cell": "B10", "kind": "labelled_constant", "label": "assumption"},
        {"sheet": "Budget", "cell": "B11", "kind": "labelled_constant", "label": "assumption"},
    ]


def event(path: Path) -> list[dict]:
    with xlsxwriter.Workbook(path) as book:
        money = book.add_format({"num_format": "0.00"})
        sheet = _setup(book, "Event", ["Item", "Quantity", "Unit cost", "Amount", "Notes"])
        sheet.set_column(4, 4, 48)
        rows = [("Supplies", 20, 5), ("Printing", 50, 2), ("Refreshments", 25, None),
                ("Venue", 1, 400), ("Equipment", 2, 75)]
        for row, (name, quantity, cost) in enumerate(rows, 2):
            sheet.write_row(row - 1, 0, [name, quantity])
            if cost is not None:
                sheet.write_number(row - 1, 2, cost, money)
            if row == 5:
                sheet.write_number(row - 1, 3, 375, money)
                sheet.write(row - 1, 4, "Manual override: venue quote received")
            else:
                sheet.write_formula(row - 1, 3, f"=B{row}*C{row}", money, quantity * (cost or 0))
        sheet.write_comment("D4", "Unit cost pending from Vendor A")
    return [
        {"sheet": "Event", "cell": "D4", "kind": "empty_reference", "label": "error"},
        {"sheet": "Event", "cell": "D5", "kind": "hardcoded_in_formula_run", "label": "intended_exception"},
    ]


def make_samples(directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    truth = {
        "club_membership_fees.xlsx": club(directory / "club_membership_fees.xlsx"),
        "small_business_budget.xlsx": business(directory / "small_business_budget.xlsx"),
        "event_budget.xlsx": event(directory / "event_budget.xlsx"),
    }
    (directory / "ground_truth.json").write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8")
    return truth


if __name__ == "__main__":
    for filename, items in make_samples(Path(__file__).resolve().parent).items():
        print(f"{filename}: {len(items)} planted items")
