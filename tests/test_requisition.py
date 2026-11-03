import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from tempo.artifacts import Finding
from tempo.config import load_config
from tempo.driver import Driver, unwrap_fence
from tempo.gates import lint_gate
from tempo.specs import REQ_RENDER_HEADROOM, RENDER_BOUND_CHARS
from tempo.requisition import (RequisitionGate, RequisitionReply, RequisitionVerdict,
                               base_render_chars, requisition, review_target)
from tempo.specs import lint_spec, load_spec
from tempo.stages import StageContext, implement_inputs, implement_stage
from tempo.tickets import TicketInvalid
from tempo.specs import render

SPECS = Path(__file__).resolve().parent.parent / "tempo" / "specs"
TICKET = """---
state: confirmed
source: human
priority: P2
kind: feature
---

## Depends on
none

## Context
- context.txt

## Goal / Why
Create one thing.

## Scope in / Scope out
In: thing. Out: the rest.

## Scope fence
- thing.py

## Acceptance criteria
1. `thing.py` exists, checked by `test -f thing.py`.

## Verification
```
test -f thing.py
```

## Definition of rejected
Needs an outside file.

## Time budget
- stuck: 20m
"""


def setup(tmp_path: Path) -> Path:
    (tmp_path / "context.txt").write_text("context")
    (tmp_path / "config.yaml").write_text("""schema_version: 1
state_dir: .state
review: {}
merge: {}
hosts:
  claude:
    models_by_tier: {low: sonnet, medium: sonnet, high: sonnet, max: sonnet}
    pins: {review: opus, requisition_review: opus}
orchestrator:
  models: {claude: opus}
engine_plane_safety_inventory: [context.txt]
""")
    return tmp_path


def run(repo: Path, response: str | None = None, text: str = TICKET, stem: str = "one-thing") -> RequisitionVerdict:
    """The verdict `prepare` and `submit` reach: the mechanical precheck, else the reply's verdict."""
    req = requisition(repo, stem, text, SPECS)
    if (snag := req.precheck()) is not None:
        return snag
    assert response is not None, "a call was needed"
    try:
        reply_ = RequisitionReply.model_validate_json(unwrap_fence(response))
    except ValidationError as exc:
        return req.invalid(f"invalid requisition review reply: {exc.error_count()} schema error(s)", "fake", "fake")
    return req.verdict(reply_, "fake", "fake")


def reply(verdict: str, findings: list | None = None) -> str:
    return json.dumps({"verdict": verdict, "summary": "reviewed", "findings": findings or []})


FINDING = {"code": "scope", "message": "missing file", "paved_road": "add it to the fence", "kind": "authoring_error"}


def test_spec_loads_and_names_finding_keys():
    source = (SPECS / "requisition_review.md").read_text()
    assert lint_spec(source) == []
    spec = load_spec(source)
    assert spec.meta.llm_surface == "requisition_review"
    assert spec.inputs == ("ticket", "context", "render", "retry_findings")
    assert all(key in source for key in ("`code`", "`message`", "`paved_road`", "`kind`"))


def test_spec_gap_finding_is_accepted(tmp_path):
    finding = {**FINDING, "kind": "spec_gap"}
    repo = setup(tmp_path)
    verdict = run(repo, reply("snag", [finding]))
    assert verdict.mechanical is None and verdict.findings == [Finding(**finding)]


def test_approve(tmp_path):
    repo = setup(tmp_path)
    verdict = run(repo, reply("approve"))
    assert verdict.verdict == "approve" and verdict.findings == []
    assert verdict.mechanical is None and (verdict.provider, verdict.model) == ("fake", "fake")


def test_snag_and_invalid_reply(tmp_path):
    repo = setup(tmp_path)
    assert run(repo, reply("snag", [FINDING])).findings[0].paved_road == "add it to the fence"
    invalid = run(repo, reply("wrong"))
    assert invalid.verdict == "snag" and invalid.mechanical == "invalid reply"
    with pytest.raises(Exception):
        RequisitionReply.model_validate_json(reply("approve", [FINDING]))


def test_invalid_ticket_and_reserved_stem_spend_no_call(tmp_path):
    repo = setup(tmp_path)
    with pytest.raises(TicketInvalid):
        run(repo, text=TICKET.replace("priority: P2", "priority: PX"))
    with pytest.raises(TicketInvalid):
        run(repo, stem="retro")


def test_over_headroom_spends_no_call(tmp_path):
    repo = setup(tmp_path)
    (repo / "context.txt").write_text("x" * (int(REQ_RENDER_HEADROOM * RENDER_BOUND_CHARS) + 1_000))
    verdict = run(repo)
    assert verdict.verdict == "snag"
    assert verdict.mechanical == "render over headroom"
    assert verdict.findings[0].code == "render_feasibility"


def test_render_over_max_bound_is_still_headroom_snag(tmp_path):
    repo = setup(tmp_path)
    (repo / "context.txt").write_text("x" * (RENDER_BOUND_CHARS + 1_000))
    verdict = run(repo)
    assert verdict.verdict == "snag"
    assert verdict.findings[0].code == "render_feasibility"
    assert verdict.render_chars > RENDER_BOUND_CHARS


def test_implement_reads_context_from_its_worktree(tmp_path):
    repo = setup(tmp_path)
    driver = Driver.from_config(load_config(None, cwd=repo), clock=lambda: datetime(2026, 10, 6, tzinfo=UTC))
    path = repo / "tickets" / "one-thing" / "ticket.md"
    path.parent.mkdir(parents=True)
    path.write_text(TICKET)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "context.txt").write_text("worktree context")
    ticket = review_target(repo, "one-thing", TICKET)
    ctx = StageContext(repo=repo, config=load_config(None, cwd=repo), env={}, exec_=None,
                       git=None, fs=None, driver=driver, specs_dir=SPECS)
    stage, _ = implement_stage(ctx, ticket, worktree)
    prompt = stage.render(ticket, [])
    assert "worktree context" in prompt
    assert "### context.txt\ncontext\n" not in prompt


def test_base_render_matches_implement_and_gate(tmp_path):
    repo = setup(tmp_path)
    ticket = review_target(repo, "one-thing", TICKET)
    spec = load_spec((SPECS / "implement.md").read_text())
    production = render(spec, {**implement_inputs(repo, TICKET, ticket, ()),
                               "retry_findings": "none"})
    assert base_render_chars(repo, ticket, TICKET, SPECS) == len(production)
    approved = run(repo, reply("approve"))
    snag = approved.model_copy(update={"verdict": "snag", "findings": [Finding(**FINDING)]})
    gate = RequisitionGate()
    assert lint_gate(gate, [approved, snag], repo) == []
    assert gate.check(snag, repo).findings == snag.findings
