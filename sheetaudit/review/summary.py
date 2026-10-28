"""Ultra orders grounded findings; identifiers are checked by code."""
import json

from ..llm.jsonout import request_json
from ..llm.models import ULTRA
from .schemas import SUMMARY, SYSTEM


def summarize(findings, outline, call, warnings, schema_style=None):
    compact = [{k: f[k] for k in ("candidate_id", "verdict", "reason", "grounding", "impact")}
               for f in findings if f["verdict"] in {"error", "intended_exception", "ask", "assumption_to_verify"}]
    messages = [dict(role="system", content=SYSTEM + "Order findings and identify conflicts. Write in English. The headline is one sentence of at most "
                  "160 characters (the schema cuts longer text mid-word). JSON schema: " + json.dumps(SUMMARY)),
                dict(role="user", content=json.dumps(dict(outline=outline, findings=compact), ensure_ascii=False))]
    value, errors = request_json(call, ULTRA, messages, SUMMARY, schema_style=schema_style, max_tokens=4000)
    if value is None:
        warnings.append("Summary failed: " + "; ".join(errors))
        return summary_fallback(findings)
    ids = {f["candidate_id"] for f in compact}
    order = list(dict.fromkeys(i for i in value["order"] if i in ids))
    if order != value["order"]:
        warnings.append("Summary: unknown or duplicate IDs dropped")
    value["order"] = order
    conflicts = []
    for conflict in value["conflicts"]:
        valid = list(dict.fromkeys(i for i in conflict["candidate_ids"] if i in ids))
        if valid != conflict["candidate_ids"]:
            warnings.append("Summary conflict: unknown or duplicate IDs dropped")
        if len(valid) >= 2:
            conflicts.append(dict(conflict, candidate_ids=valid))
    value["conflicts"] = conflicts
    return value


def summary_fallback(findings):
    return dict(headline="Model summary unavailable; inspect individual findings.",
                order=[f["candidate_id"] for f in findings], conflicts=[], question_for_user=None)
