"""Connect deterministic candidates, model stages, grounding and spending limits."""
from dataclasses import asdict
from pathlib import Path

from ..budget import BudgetStopped
from ..engine import audit
from ..llm.client import LLMError, MockChatClient, redact
from ..llm.jsonout import response_format
from ..llm.models import NANO, SUPER, ULTRA
from ..loader import load
from .judge import fallback, judge
from .structure import structure
from .summary import summarize, summary_fallback

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def review(path, client, budget, *, app="ek", lane="public", schema_style=None,
           judge_mode="tools", max_candidates=12):
    if lane not in {"public", "judge"} or app != "ek":
        raise ValueError("Expected app=ek and lane=public or judge")
    if type(max_candidates) is not int or max_candidates < 0:
        raise ValueError("max_candidates must be nonnegative")
    if judge_mode not in {"tools", "json_only"}:
        raise ValueError("Unknown judge_mode")
    response_format({}, schema_style)  # Validate the switch before any calls.
    path = Path(path)
    book, candidates = load(path), audit(path)
    warnings, findings = [], []
    outline = {"sheets": []}
    stopped = budget.stopped_reason
    def caller(stage, candidate_id=None):
        stage_client = client.for_stage(stage, candidate_id) if isinstance(client, MockChatClient) else client
        def complete(model, messages, **kwargs):
            return budget.complete(stage_client, stage, app, lane, model, messages, **kwargs)
        return complete
    if candidates and max_candidates:
        try:
            outline = structure(book, caller("structure"), warnings, schema_style)
        except BudgetStopped as exc:
            stopped = str(exc)
        except LLMError as exc:
            warnings.append("Structure unavailable: " + str(exc))
    for index, candidate in enumerate(candidates):
        if index >= max_candidates:
            decision = fallback(candidate, "not reviewed (limit)")
            decision["verdict"] = "not reviewed (limit)"
        elif stopped:
            decision = fallback(candidate, "not reviewed (budget)")
            decision["verdict"] = "not reviewed (budget)"
        else:
            try:
                decision = judge(candidate, book, outline, caller("judge", candidate.id),
                                 schema_style=schema_style, judge_mode=judge_mode)
            except BudgetStopped as exc:
                stopped = str(exc)
                decision = fallback(candidate, "not reviewed (budget)")
                decision["verdict"] = "not reviewed (budget)"
            except LLMError as exc:
                decision = fallback(candidate, str(exc))
                warnings.append(f"Judge {candidate.sheet}!{candidate.cell}: {exc}")
        findings.append(dict(asdict(candidate), **decision))
    summary = summary_fallback(findings)
    if candidates and max_candidates and not stopped:
        try:
            summary = summarize(findings, outline, caller("summary"), warnings, schema_style)
        except BudgetStopped as exc:
            stopped = str(exc)
        except LLMError as exc:
            warnings.append("Summary unavailable: " + str(exc))
    stopped = stopped or budget.stopped_reason
    # Resolve against the project root so the answer does not depend on the working directory.
    sample = Path("samples/precomputed") / (path.stem + ".json")
    if not (PROJECT_ROOT / sample).exists():
        sample = sample.parent
    return redact(dict(workbook=path.name, generated_at=budget.now().isoformat(),
                       source="mock" if isinstance(client, MockChatClient) else "http",
                       models=dict(structure=NANO, judge=SUPER, summary=ULTRA), outline=outline,
                       findings=findings, summary=summary, cost=budget.report(),
                       stopped_reason=stopped, warnings=warnings,
                       precomputed_sample=str(sample) if stopped else None))
