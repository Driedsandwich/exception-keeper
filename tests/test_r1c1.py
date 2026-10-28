import pytest

from sheetaudit.r1c1 import normalize, parse_reference, references


@pytest.mark.parametrize("formula,cell,expected", [
    ("=B2*C2", "D2", "=RC[-2]*RC[-1]"),
    ("=$B$2+B$2+$B2+B2", "D5", "=R2C2+R2C[-2]+R[-3]C2+R[-3]C[-2]"),
    ("=SUM(B2:B40)", "B41", "=SUM(R[-39]C:R[-1]C)"),
    ("=SUM($B2:C$40)", "D41", "=SUM(R[-39]C2:R40C[-1])"),
    ("=Rules!B3", "D5", "=Rules!R[-2]C[-2]"),
    ("='Fee Rules'!$B$3", "D5", "='Fee Rules'!R3C2"),
    ("='O''Brien Rules'!B3", "D5", "='O''Brien Rules'!R[-2]C[-2]"),
    ('=IF(A2="B2",B2,"Rules!B3")', "C2", '=IF(RC[-2]="B2",RC[-1],"Rules!B3")'),
    ('="He said ""B2"""&A1', "B1", '="He said ""B2"""&RC[-1]'),
    ("=A1+Z10", "A1", "=RC+R[9]C[25]"),
    ("=SUM(A1:A3,C1:C3)", "D4", "=SUM(R[-3]C[-3]:R[-1]C[-3],R[-3]C[-1]:R[-1]C[-1])"),
    ("=LOG10(B2)+MyName", "C2", "=LOG10(RC[-1])+MyName"),
    ("=SUM(B:B)+Table1[Fee]", "D5", "=SUM(B:B)+Table1[Fee]"),
    ("='[Other.xlsx]Rules'!B3", "D5", "='[Other.xlsx]Rules'!R[-2]C[-2]"),
])
def test_handwritten_normalization(formula, cell, expected):
    assert normalize(formula, cell) == expected


@pytest.mark.parametrize("first,origin,copy,target", [
    ("=B2*$F$1", "C2", "=B18*$F$1", "C18"),
    ("=$J2*(1+$B$10)", "B2", "=$J2*(1+$B$10)", "G2"),
    ("=SUM(B2:B4)", "B5", "=SUM(C2:C4)", "C5"),
    ("='Fee Rules'!B3", "C2", "='Fee Rules'!B4", "C3"),
])
def test_copied_formulas(first, origin, copy, target):
    assert normalize(first, origin) == normalize(copy, target)


def test_reference_parser_ignores_strings_and_names():
    refs = references('=IF(A1="B2:B40",Rules!$B$3,SomeName)')
    assert [(r.sheet, r.start, r.end) for r in refs] == [(None, "A1", None), ("Rules", "$B$3", None)]
    assert parse_reference("XFE1") is None
    assert parse_reference("A1048577") is None
