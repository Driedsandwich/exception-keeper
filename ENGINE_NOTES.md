# Deterministic spreadsheet audit engine

Order 01 is implemented: five structural detectors, context extraction, a Python API,
a CLI, three fictional workbooks, ground truth, and regression tests. This stage emits
candidates, including documented exceptions; it never decides whether a candidate is
an error and never calls an LLM, network service, or formula evaluator.

## Run

From the project root, using the existing environment without installing anything:

```sh
.venv/bin/python samples/make_samples.py
.venv/bin/python -m pytest -q
.venv/bin/python -m sheetaudit samples/club_membership_fees.xlsx --json
```

The sample generator replaces its three generated workbooks and ground truth when
rerun. It writes an explicit Python-computed result for every formula, including
incorrect formulas: caches describe what the planted formula computes, not what the
formula ought to compute. The input workbooks are never saved by the auditor.

```python
from sheetaudit import audit

candidates = audit("samples/club_membership_fees.xlsx")
```

`Candidate.kind` is always a list of detector names. Candidates are merged by sheet
and cell, ordered by workbook sheet order, row, and column. IDs depend on sheet name
and coordinate, so remain stable across repeat audits of the same layout; they are
not global identifiers across different workbooks. `value` is the saved value.
`r1c1` describes references relative to the formula cell, preserving absolute row
and column anchors. For example, `=B2*$F$1` at C2 becomes `=RC[-1]*R1C6`.

## Detector rules

| Kind | Trigger | Evidence and limits |
| --- | --- | --- |
| `pattern_break` | A contiguous horizontal or vertical run of at least three formulas has a strict majority pattern and a different minority pattern. | Reports each minority cell and the majority R1C1 text. Numeric constants, text, and blanks end a run. Aligned aggregate summary formulas form boundaries, avoiding comparison of totals against detail formulas. |
| `hardcoded_in_formula_run` | A numeric constant is immediately between two formulas with matching R1C1 patterns, vertically or horizontally. | Includes zero and retains candidates with explanatory notes. End-of-run constants and consecutive constants are not detected by this rule. Booleans are not numeric constants. |
| `aggregate_range_gap` | A direct single-row or single-column range operand of SUM, AVERAGE, COUNT, MIN, or MAX has adjacent numeric/formula cells after its end. | Scans the contiguous tail until a blank, text, aligned summary, or the aggregate cell itself. Checks other explicit references in the formula before calling a cell omitted. Single-cell colon ranges such as B2:B2 are supported when aligned with the aggregate cell. |
| `empty_reference` | A supported explicit reference points to an empty cell inside a labelled data block. | Requires a row label, a header, and occupied neighbours on both sides in at least one direction. Cross-sheet references and small ranges are included. An uncached formula is occupied, not empty. Boundary blanks and unlabelled blocks can be missed. |
| `labelled_constant` | A numeric constant has an assumption-like nearest row label or column header. | Matches rate, tax, fx, exchange, inflation, discount, %, 税率, 為替, and currency pairs such as USD/JPY, without case sensitivity. This is a freshness candidate, not an error verdict. |

For range gaps, `impact` is the **signed sum of the omitted cells' cached numeric
values**, with each cell counted once. It is `None` if any omitted value is unknown
or nonnumeric. This is an exposure indicator: for AVERAGE, COUNT, MIN, and MAX it is
not the recomputed change in the aggregate result. Missing caches are never treated
as zero. Each gap includes `context.omitted_cells` with addresses and saved values.

R1C1 conversion supports mixed/absolute A1 references, ranges, sheet qualifiers,
quoted sheet names, and escaped quotes. String operands stay untouched. Function
names and other operands are preserved. Textual case and whitespace differences
are not canonicalized, so cosmetic formula variations can become candidates.

## Context contract

Each candidate includes up to three cells above and below with formula, saved value,
and R1C1 text. `context.row_neighbours` adds the corresponding horizontal window.
The row label is the nearest nonempty literal text cell to the left. The column
header is the nearest literal text above within the initial header area, defined
as the rows before the first numeric/formula row. This simple header rule assumes
one main table per sheet.

Notes use the nearest preceding matching header row, with a leftmost tie-break.
Supported headers are notes, note, comment, remarks, memo, and 備考, ignoring case
and surrounding whitespace. Blank notes remain null. Cell comments and number
formats are retained. `context.references` supplies up to 24 distinct referenced
endpoints with their saved values, formulas, row labels, and comments, exposing
referenced rules without copying entire sheets. Range interiors and recursive
dependencies are not expanded into this context field. For constants, that field
is empty; neighbouring formulas still expose the usual rule references.

## Scope and known limits

- **Caches:** No recalculation occurs. An absent or stale saved result remains
  absent or stale. A pattern candidate can still be found without a saved result,
  but numeric impact may be unknown.
- **Macros:** VBA and macro behaviour are not inspected or executed. The supported
  input contract is ordinary `.xlsx` workbooks.
- **Array formulas:** Array/data-table formula objects are rejected with an explicit
  error. Spill semantics, implicit intersection, and dynamic array evaluation are
  outside this engine's scope. Unsupported tokenization leaves normalization text
  unchanged and produces no extracted references for that formula.
- **Named ranges:** Names and structured table references are left unchanged and
  not resolved. Whole-row/whole-column and 3D references are not normalized or
  expanded. INDIRECT and OFFSET targets are not evaluated.
- **Pivot tables:** Pivot definitions, caches, totals, and refresh status receive
  no special treatment. Their displayed cells may produce ordinary candidates.
- **External links:** External workbooks are not opened or fetched. Quoted external
  A1 tokens may normalize syntactically, but their target values are unavailable;
  external references do not participate in gap/empty-reference checks.
- **Merged cells:** No propagation of labels or values across merged cells occurs;
  header and empty-cell heuristics may misinterpret such layouts.
- **Layouts:** Aggregate gaps cover trailing tails of explicit one-dimensional
  ranges, not missing leading rows, interior skipped ranges, two-dimensional
  rectangles, or semantic exclusions. Pattern rules need a local majority and do
  not infer intended business rules. Multi-table headers, sparse layouts, hidden
  rows, deliberate subtotals, and mixed formula families can cause misses or extra
  candidates. Hidden rows and sheets are included without special filtering.
- **Resource scope:** The loader rejects sheet rectangles over 200,000 cells before
  iterating their cells. Both workbook reads use normal loading to retain comments;
  this bound is not a guarantee against a large archive consuming memory while
  loading. Empty-reference range expansion is skipped above 10,000 cells. This is
  a small-workbook implementation, not a hardened service for untrusted archives.

## Local library sources

The API signatures and token behaviour used here were checked in the installed
source, without accessing the network. The workbook reader documents `data_only`
using this phrase:

```text
the value stored the last time Excel read the sheet
```
Source: [installed openpyxl reader, load_workbook docstring](.venv/lib/python3.14/site-packages/openpyxl/reader/excel.py), lines 317–330.

The formula tokenizer describes its input/output contract as follows:

```text
Converts a str string representing an Excel formula (in A1 notation)
into a sequence of `Token` objects.
```
Source: [installed openpyxl tokenizer](.venv/lib/python3.14/site-packages/openpyxl/formula/tokenizer.py), Tokenizer docstring.

The sample writer's signature includes the explicit result parameter:

```python
def write_formula(self, row, col, formula, cell_format=None, value=0):
```
Its parameter description states:

```text
value:       An optional value for the formula. Default is 0.
```
Source: [installed XlsxWriter worksheet source](.venv/lib/python3.14/site-packages/xlsxwriter/worksheet.py), lines 824–840. Signature above omits type annotations only.

The generator explicitly supplies this parameter instead of relying on its default.
Tests read the resulting workbooks through the loader and independently check every
sample formula's cached arithmetic against its actual expression.

## Measured verification

Observed with `.venv/bin/python -m pytest -q` on the generated workbooks:

```text
club_membership_fees.xlsx: candidates=6, planted=5, recalled=5, unplanted=1
small_business_budget.xlsx: candidates=5, planted=5, recalled=5, unplanted=0
event_budget.xlsx: candidates=2, planted=2, recalled=2, unplanted=0
48 passed in 0.62s
```

All 12 planted items were recalled at the required sheet/cell and with the required
kind, including intended exceptions, the ambiguous zero, and both budget assumptions.
The single unplanted candidate is `Fee Rules!B3`, a `labelled_constant` for Student
rate. Candidate counts are unique cells after merging kinds; for example, Budget!H3
has both `pattern_break` and `aggregate_range_gap`.

The sample gap impacts are Members!C32 = 50 from omitted C31 and Budget!H3 = 135 from
omitted G3. Regression tests also cover all five aggregate functions in both axes,
multiple omissions, missing saved values, explicit extra arguments, separate blocks,
notes headers, cross-sheet empty references, string literals, deterministic output,
and CLI JSON equivalence. The explicit requested CLI command completed successfully
and printed six candidate records. Recall and unplanted counts describe these
constructed fixtures only; they do not establish accuracy on unseen workbooks.
