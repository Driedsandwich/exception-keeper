"""Per-candidate judgment, bounded tools, and deterministic grounding checks."""
from dataclasses import asdict
import json
import re

from ..llm.jsonout import decode, repair_message, request_json, response_format
from ..llm.models import SUPER
from ..r1c1 import parse_reference, tokens
from .schemas import JUDGMENT, SYSTEM
from .structure import range_bounds
from .tools import TOOLS, WorkbookTools


def fallback(candidate, reason):
    return dict(candidate_id=candidate.id, verdict="ask", confidence="low", reason=reason[:400],
                evidence=[], fix=None, question="Please review the candidate and its workbook context.",
                original_verdict=None, decided_by="fallback", grounding="failed", grounding_reasons=[reason])


def _normalized(text):
    return "".join(str(text).split()).casefold() if text is not None else ""


def _target(book, address):
    if not isinstance(address, str) or "!" not in address:
        raise ValueError("Expected Sheet!A1")
    name, local = address.rsplit("!", 1)
    if not book.sheet(name) and name.startswith("'") and name.endswith("'"):
        name = name[1:-1].replace("''", "'")
    ref = parse_reference(local)
    if not ref or ref.sheet or ref.end:
        raise ValueError("Expected Sheet!A1")
    sheet, (row, col, _, _) = range_bounds(book, name, ref.start, 1)
    return sheet.at(row, col)


def _qualified(address, sheet):
    """A bare A1 address refers to the candidate's own sheet, as it would inside a formula."""
    if not isinstance(address, str) or "!" in address:
        return address
    return (sheet if re.fullmatch(r"\w+", sheet) else "'" + sheet.replace("'", "''") + "'") + "!" + address


def ground(value, candidate, book, outline):
    evidence = [dict(item, cell=_qualified(item["cell"], candidate.sheet)) for item in value["evidence"]]
    result = dict(value, evidence=evidence, original_verdict=value["verdict"], decided_by=SUPER,
                  grounding="passed", grounding_reasons=[])
    failures, documented = [], False
    rules = {s["name"] for s in outline["sheets"] if s["role"] == "rules"}
    for item in evidence:
        quote = _normalized(item["quote"])
        try:
            cell = _target(book, item["cell"])
        except ValueError:
            failures.append("Evidence cell does not exist: " + item["cell"])
            continue
        actual = [cell.formula, cell.cached_value, cell.comment]
        if not quote:
            # An empty quote can only show that the cited cell is empty; it never documents anything.
            if any(_normalized(v) for v in actual):
                failures.append("Ungrounded quote at " + item["cell"])
            continue
        # Label/header/note text counts only when the model cites the candidate cell itself;
        # otherwise the quote must be in the cited cell, so evidence addresses stay truthful.
        own = cell.sheet == candidate.sheet and cell.coordinate == candidate.cell
        context = [candidate.context.get(k) for k in ("row_label", "column_header", "notes")] if own else []
        if not quote or not any(quote in _normalized(v) for v in actual + context):
            failures.append("Ungrounded quote at " + item["cell"])
            continue
        note_address = candidate.context.get("notes_cell")
        is_note = (cell.sheet == candidate.sheet and cell.coordinate == note_address
                   and quote in _normalized(cell.cached_value))
        is_comment = (cell.sheet == candidate.sheet and cell.coordinate == candidate.cell
                      and quote in _normalized(cell.comment))
        is_rule = cell.sheet in rules and any(quote in _normalized(v) for v in actual)
        documented |= is_note or is_comment or is_rule
    if value["candidate_id"] != candidate.id:
        failures.append("Candidate ID mismatch")
    if value["verdict"] == "intended_exception" and not documented:
        failures.append("Intended exception requires a grounded note or rule quote")
    if failures:
        result.update(verdict="ask", grounding="failed", grounding_reasons=failures,
                      fix=None,
                      question="Please confirm the candidate; model evidence could not be grounded.")
    result["candidate_id"] = candidate.id
    fix = value["fix"] and dict(value["fix"], cell=_qualified(value["fix"]["cell"], candidate.sheet))
    result["fix"] = fix if not failures else None
    if fix and not failures:
        valid = isinstance(fix["new_formula"], str) and fix["new_formula"].startswith("=")
        try:
            _target(book, fix["cell"])
            formula_tokens = tokens(fix["new_formula"])
            if not formula_tokens or re.search(r"\b(?:INDIRECT|OFFSET)\s*\(", fix["new_formula"], re.I):
                valid = False
            for token in formula_tokens:
                if token.type == "OPERAND" and token.subtype == "RANGE":
                    ref = parse_reference(token.value)
                    if not ref or (ref.sheet and not book.sheet(ref.sheet)):
                        valid = False
                if token.subtype == "ERROR":
                    valid = False
        except (ValueError, TypeError):
            valid = False
        if not valid:
            result["fix"] = None
            result["grounding_reasons"].append("Invalid or unverifiable fix dropped")
    return result


def judge(candidate, book, outline, call, *, schema_style=None, judge_mode="tools"):
    if judge_mode not in {"tools", "json_only"}:
        raise ValueError("Unknown judge_mode")
    tools = WorkbookTools(book, outline, candidate)
    payload = dict(candidate=asdict(candidate), outline=outline)
    if judge_mode == "json_only":
        payload["lookups"] = tools.upfront()
    messages = [{"role": "system", "content": SYSTEM +
                 "Classify error, intended_exception, ask, or assumption_to_verify. "
                 "An intended exception needs a quoted note or rule. Evidence addresses must be Sheet!A1. "
                 "The candidate context carries the row note (quote it at notes_cell) and the cell comment "
                 "(quote it at the candidate cell). When a note, comment or rule says the deviation is deliberate, "
                 "classify intended_exception. A note that an input is missing or pending is not a reason; "
                 "classify ask or error. To show that a cell is empty, cite it with an empty quote. "
                 "Suggest a fix only when supported. Final JSON schema: " + json.dumps(JUDGMENT)},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}]
    if judge_mode == "json_only":
        value, errors = request_json(call, SUPER, messages, JUDGMENT,
                                    schema_style=schema_style, max_tokens=800)
        return ground(value, candidate, book, outline) if value else fallback(candidate, "; ".join(errors))
    for round_number in range(4):
        response = call(SUPER, messages, tools=TOOLS, tool_choice="auto",
                        response_format=response_format(JUDGMENT, schema_style), max_tokens=800, temperature=0)
        if response.tool_calls:
            # Bound fanout as well as the number of model turns.
            if len(response.tool_calls) > 8:
                return fallback(candidate, "Too many tool calls in one round")
            valid_calls = all(isinstance(t, dict) and isinstance(t.get("id"), str)
                              and isinstance(t.get("function"), dict) for t in response.tool_calls)
            if not valid_calls:
                return fallback(candidate, "Malformed tool calls")
            messages.append(dict(role="assistant", content=response.content, tool_calls=response.tool_calls))
            for item in response.tool_calls:
                function = item["function"]
                output = tools.execute(function.get("name"), function.get("arguments"))
                messages.append(dict(role="tool", tool_call_id=item["id"],
                                     content=json.dumps(output, ensure_ascii=False, default=str)))
            continue
        value, errors = decode(response.content, JUDGMENT)
        if value is None:
            messages.extend([dict(role="assistant", content=response.content or ""), repair_message(errors)])
            # One repair only, without tools. A second invalid result is a failure.
            response = call(SUPER, messages, response_format=response_format(JUDGMENT, schema_style),
                            max_tokens=800, temperature=0)
            value, errors = decode(response.content, JUDGMENT)
        return ground(value, candidate, book, outline) if value else fallback(candidate, "; ".join(errors))
    return fallback(candidate, "Tool round limit reached (4); no final judgment")
