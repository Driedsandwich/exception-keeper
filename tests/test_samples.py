from dataclasses import asdict
import json
import subprocess
import sys

import pytest

from sheetaudit import audit
from sheetaudit.loader import load


@pytest.mark.parametrize("filename", ["club_membership_fees.xlsx", "small_business_budget.xlsx", "event_budget.xlsx"])
def test_planted_recall_and_candidate_counts(samples, filename, capsys):
    _, truth, results = samples
    candidates = {(c.sheet, c.cell): c for c in results[filename]}
    planted = {(item["sheet"], item["cell"]) for item in truth[filename]}
    for item in truth[filename]:
        candidate = candidates[(item["sheet"], item["cell"])]
        assert item["kind"] in candidate.kind
    extra = set(candidates) - planted
    with capsys.disabled():
        print(f"\n{filename}: candidates={len(candidates)}, planted={len(planted)}, "
              f"recalled={len(planted & set(candidates))}, unplanted={len(extra)}")
    assert len(extra) <= 3
    assert len(candidates) == len(results[filename])


@pytest.mark.parametrize("filename,sheet,cell,omitted,expected", [
    ("club_membership_fees.xlsx", "Members", "C32", "C31", 50),
    ("small_business_budget.xlsx", "Budget", "H3", "G3", 135),
])
def test_aggregate_impact_is_omitted_cache(samples, filename, sheet, cell, omitted, expected):
    directory, _, results = samples
    candidate = next(c for c in results[filename] if (c.sheet, c.cell) == (sheet, cell))
    cached = load(directory / filename).sheets[sheet].cells[omitted].cached_value
    assert cached == pytest.approx(expected)
    assert candidate.impact == cached
    assert candidate.context["omitted_cells"] == [{"sheet": sheet, "cell": omitted, "value": cached}]


def test_context_and_saved_values(samples):
    directory, _, results = samples
    club = {c.cell: c for c in results["club_membership_fees.xlsx"] if c.sheet == "Members"}
    assert club["C20"].context["notes"] == "Waived — board decision"
    assert club["C26"].context["notes"] is None
    assert club["C14"].context["column_header"] == "Fee"
    assert club["C14"].context["row_label"] == "Standard"
    assert any(ref["row_label"] == "Guest fee" and ref["value"] == 75
               for ref in club["C14"].context["references"])
    assert len(club["C14"].neighbours) == 6
    assert all(set(n) == {"cell", "formula", "value", "r1c1"} for n in club["C14"].neighbours)
    event = {c.cell: c for c in results["event_budget.xlsx"]}
    assert event["D5"].context["notes"] == "Manual override: venue quote received"
    assert event["D4"].context["comment"] == "Unit cost pending from Vendor A"
    assert event["D4"].context["empty_references"] == [{"sheet": "Event", "cell": "C4"}]
    for filename in results:
        book = load(directory / filename)
        assert all(c.cached_value is not None for s in book.sheets.values()
                   for c in s.cells.values() if c.formula)
    budget = load(directory / "small_business_budget.xlsx").sheets["Budget"]
    assert budget.cells["D4"].cached_value == 150
    assert budget.cells["B10"].number_format == "0%"


def test_deterministic_and_cli_json(samples):
    directory, _, results = samples
    path = directory / "club_membership_fees.xlsx"
    expected = [asdict(c) for c in results[path.name]]
    assert [asdict(c) for c in audit(path)] == expected
    output = subprocess.run([sys.executable, "-m", "sheetaudit", str(path), "--json"],
                            check=True, capture_output=True, text=True)
    assert json.loads(output.stdout) == expected
    assert len({c["id"] for c in expected}) == len(expected)


def test_every_sample_formula_cache_matches_its_actual_expression(samples):
    directory, _, _ = samples
    members = load(directory / "club_membership_fees.xlsx").sheets["Members"]
    for row in range(2, 32):
        cell = members.cells[f"C{row}"]
        if cell.formula:
            base = 75 if row == 14 else 100
            factor = 0.5 if members.cells[f"B{row}"].cached_value == "Student" else 1
            assert cell.cached_value == base * factor
    assert members.cells["C32"].cached_value == sum(members.cells[f"C{r}"].cached_value for r in range(2, 31))

    budget = load(directory / "small_business_budget.xlsx").sheets["Budget"]
    for row in range(2, 7):
        base = budget.cells[f"J{row}"].cached_value
        for col in "BCDEFG":
            cell = budget.cells[f"{col}{row}"]
            if cell.formula:
                tax = 0 if cell.coordinate == "D4" else budget.cells["B10"].cached_value
                assert cell.cached_value == pytest.approx(base * (1 + tax))
        included = "BCDEF" if row == 3 else "BCDEFG"
        assert budget.cells[f"H{row}"].cached_value == pytest.approx(
            sum(budget.cells[f"{col}{row}"].cached_value for col in included))

    event = load(directory / "event_budget.xlsx").sheets["Event"]
    for row in (2, 3, 4, 6):
        quantity = event.cells[f"B{row}"].cached_value
        cost = event.at(row, 3).cached_value or 0
        assert event.cells[f"D{row}"].cached_value == quantity * cost
