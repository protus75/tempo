import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tempo.artifacts import Finding
from tempo.config import load_config
from tempo.driver import Driver
from tempo.gates import lint_gate
from tempo.llm import FakeLLM
from tempo.specs import REQ_RENDER_HEADROOM, RENDER_BOUND_CHARS
from tempo.requisition import (RequisitionGate, RequisitionReply, RequisitionVerdict,
                               base_render_chars, review_target, review_ticket)
from tempo.specs import lint_spec, load_spec
from tempo.stages import StageContext, implement_inputs, implement_stage
from tempo.tickets import TicketInvalid
from tempo.specs import render

SPECS = Path(__file__).resolve().parent.parent / "specs"
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
- expected: 10m
- stuck: 20m
"""


def setup(tmp_path: Path, responses: list) -> tuple[Path, Driver, FakeLLM]:
    (tmp_path / "context.txt").write_text("context")
    (tmp_path / "config.yaml").write_text("""schema_version: 1
state_dir: .state
providers:
  - name: fake
    kind: cli
    package: test-cli
    auth: FAKE_KEY
    models_by_tier: {low: m, medium: m, high: m, max: m}
    limits: {concurrency: 1, est_cost_per_call_usd: 0.5}
routing:
  - {tier: high, surface: review, candidates: [{provider: fake}]}
review: {}
merge: {}
engine_plane_safety_inventory: [context.txt]
""")
    llm = FakeLLM(responses)

    async def never(seconds: float) -> None:
        await asyncio.Event().wait()

    driver = Driver.from_config(load_config(None, cwd=tmp_path), llm=llm, env={"FAKE_KEY": "secret"},
                                clock=lambda: datetime(2026, 10, 6, tzinfo=UTC), sleep=never)
    return tmp_path, driver, llm


def run(driver: Driver, repo: Path, text: str = TICKET, stem: str = "one-thing") -> RequisitionVerdict:
    return asyncio.run(review_ticket(driver, repo=repo, plan="", stem=stem, text=text,
                                     specs_dir=SPECS, tier="high", stem_slot="s", run_seq=0,
                                     attempt=1, call_seq=1, expected_budget='surface'))


def reply(verdict: str, findings: list | None = None) -> str:
    return json.dumps({"verdict": verdict, "summary": "reviewed", "findings": findings or []})


FINDING = {"code": "scope", "message": "missing file", "paved_road": "add it to the fence", "kind": "authoring_error"}


def test_spec_loads_and_names_finding_keys():
    source = (SPECS / "requisition_review.md").read_text()
    assert lint_spec(source) == []
    spec = load_spec(source)
    assert spec.meta.llm_surface == "requisition_review"
    assert spec.inputs == ("ticket", "plan_contract", "context", "render", "prior_review", "retry_findings")
    assert all(key in source for key in ("`code`", "`message`", "`paved_road`", "`kind`", "`unit`"))


@pytest.mark.parametrize("unit,valid", [(None, False), ("19.P3.other", False), ("19.P3.governing", True)])
def test_spec_gap_finding_unit_is_validated(tmp_path, unit, valid):
    finding = {**FINDING, "kind": "spec_gap"}
    if unit is not None:
        finding["unit"] = unit
    repo, driver, _ = setup(tmp_path, [reply("snag", [finding])])
    plan = """### 19.P3 Phase 3
```yaml
# BEGIN_REGISTRY_P3
seeds:
  governing: {}
  other: {}
# END_REGISTRY_P3
```
### 19.P3.governing Governing
- **Owner:** o
- **Records:** r
- **Observable:** b
- **Tests:** t
"""
    (repo / "TEMPO_PLAN.md").write_text(plan)
    text = TICKET.replace("## Goal / Why", "## Plan contract\n- 19.P3.governing\n\n## Goal / Why")
    verdict = asyncio.run(review_ticket(driver, repo=repo, plan=plan, stem="one-thing", text=text,
                                       specs_dir=SPECS, tier="high", stem_slot="s", run_seq=0,
                                       attempt=1, call_seq=1, expected_budget='surface'))
    assert verdict.mechanical == (None if valid else "invalid reply")
    if valid:
        assert verdict.findings == [Finding(**finding)]


@pytest.mark.parametrize("unit,valid", [(None, True), ("19.P3.governing", False)])
def test_spec_gap_without_hardenable_units_requires_null(tmp_path, unit, valid):
    repo, driver, _ = setup(tmp_path, [reply("snag", [{**FINDING, "kind": "spec_gap", "unit": unit}])])
    assert run(driver, repo).mechanical == (None if valid else "invalid reply")


@pytest.mark.parametrize("kind", [None, "authoring_error"])
def test_a_unit_requires_spec_gap_kind(kind):
    with pytest.raises(ValueError, match="unit is non-null only for kind spec_gap"):
        Finding(**{**FINDING, "kind": kind, "unit": "19.P3.governing"})


@pytest.mark.parametrize("unit", [None, "19.P3.other"])
def test_invalid_implement_spec_gap_unit_is_reprompted(tmp_path, unit):
    from tests.test_stages import implement_reply

    valid = {"code": "premise", "message": "missing governing fact", "paved_road": "state it",
             "kind": "spec_gap", "unit": "19.P3.governing"}
    repo, driver, llm = setup(tmp_path, [implement_reply("premise_failed", [{**valid, "unit": unit}]),
                                       implement_reply("premise_failed", [valid])])
    plan = """### 19.P3 Phase 3
```yaml
# BEGIN_REGISTRY_P3
seeds:
  governing: {}
  other: {}
# END_REGISTRY_P3
```
### 19.P3.governing Governing
- **Owner:** o
- **Records:** r
- **Observable:** b
- **Tests:** t
"""
    (repo / "TEMPO_PLAN.md").write_text(plan)
    text = TICKET.replace("## Goal / Why", "## Plan contract\n- 19.P3.governing\n\n## Goal / Why")
    path = repo / "tickets/one-thing/ticket.md"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    ticket = review_target(repo, "one-thing", text)
    ctx = StageContext(repo=repo, config=load_config(None, cwd=repo), env={}, exec_=None,
                       git=None, fs=None, driver=driver, specs_dir=SPECS)
    stage, _ = implement_stage(ctx, ticket, repo)
    result = asyncio.run(driver.run(stage, ticket, ticket=ticket.stem, attempt=0, workspace=repo,
                                   tier="high", effort="high", stuck_budget=60, expected_budget=300.0, scope_fence=()))
    assert result.outcome == "ok"
    assert result.artifact.findings == [Finding(**valid)]
    assert len(llm.requests) == 2
    assert "spec_gap unit must be one of" in llm.requests[1].rendered


def test_approve_and_effect_key(tmp_path):
    repo, driver, llm = setup(tmp_path, [reply("approve")])
    verdict = run(driver, repo)
    assert verdict.verdict == "approve" and verdict.findings == []
    assert verdict.mechanical is None
    assert len(llm.requests) == 1
    assert llm.requests[0].surface == "requisition_review"
    assert any(e.key == "llm/s/0/requisition_review/1/1" for e in driver.journal.read())


def test_snag_and_invalid_reply(tmp_path):
    repo, driver, llm = setup(tmp_path, [reply("snag", [FINDING]), reply("wrong")])
    assert run(driver, repo).findings[0].paved_road == "add it to the fence"
    # A new call identity avoids replaying the first completion.
    invalid = asyncio.run(review_ticket(driver, repo=repo, plan="", stem="one-thing", text=TICKET,
                                        specs_dir=SPECS, tier="high", stem_slot="s", run_seq=0,
                                        attempt=1, call_seq=2, expected_budget='surface'))
    assert invalid.verdict == "snag" and invalid.mechanical == "invalid reply"
    assert len(llm.requests) == 2
    with pytest.raises(Exception):
        RequisitionReply.model_validate_json(reply("approve", [FINDING]))


def test_invalid_ticket_and_reserved_stem_spend_no_call(tmp_path):
    repo, driver, llm = setup(tmp_path, [])
    with pytest.raises(TicketInvalid):
        run(driver, repo, TICKET.replace("priority: P2", "priority: PX"))
    with pytest.raises(TicketInvalid):
        run(driver, repo, stem="retro")
    assert llm.requests == []


def test_over_headroom_spends_no_call(tmp_path):
    repo, driver, llm = setup(tmp_path, [])
    (repo / "context.txt").write_text("x" * (int(REQ_RENDER_HEADROOM * RENDER_BOUND_CHARS) + 1_000))
    verdict = run(driver, repo)
    assert verdict.verdict == "snag"
    assert verdict.mechanical == "render over headroom"
    assert verdict.findings[0].code == "render_feasibility"
    assert llm.requests == []


def test_render_over_max_bound_is_still_headroom_snag(tmp_path):
    repo, driver, llm = setup(tmp_path, [])
    (repo / "context.txt").write_text("x" * (RENDER_BOUND_CHARS + 1_000))
    verdict = run(driver, repo)
    assert verdict.verdict == "snag"
    assert verdict.findings[0].code == "render_feasibility"
    assert verdict.render_chars > RENDER_BOUND_CHARS
    assert llm.requests == []


def test_provider_error_is_mechanical_snag(tmp_path):
    repo, driver, llm = setup(tmp_path, [RuntimeError("provider unavailable")])
    verdict = run(driver, repo)
    assert verdict.verdict == "snag" and verdict.mechanical == "provider error"
    assert "provider unavailable" in verdict.findings[0].message
    assert len(llm.requests) == 1


def test_implement_reads_context_from_its_worktree(tmp_path):
    repo, driver, _ = setup(tmp_path, [])
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


def test_review_render_over_bound_is_mechanical_snag(tmp_path, monkeypatch):
    repo, driver, llm = setup(tmp_path, [])
    import tempo.requisition as requisition
    import tempo.specs as specs
    # The base Implement render fits the bound; the review render, which embeds it, does not.
    base = base_render_chars(repo, "", review_target(repo, "one-thing", TICKET), TICKET, SPECS)
    monkeypatch.setattr(specs, "RENDER_BOUND_CHARS", base + 1)
    monkeypatch.setattr(requisition, "RENDER_BOUND_CHARS", 2 * base)
    verdict = run(driver, repo)
    assert verdict.verdict == "snag" and verdict.mechanical == "review render over bound"
    assert verdict.findings[0].code == "requisition_review"
    assert llm.requests == []


def test_base_render_matches_implement_and_gate(tmp_path):
    repo, driver, _ = setup(tmp_path, [reply("approve")])
    ticket = review_target(repo, "one-thing", TICKET)
    spec = load_spec((SPECS / "implement.md").read_text())
    production = render(spec, {**implement_inputs(repo, "", TICKET, ticket, ()),
                               "retry_findings": "none"})
    assert base_render_chars(repo, "", ticket, TICKET, SPECS) == len(production)
    approved = run(driver, repo)
    snag = approved.model_copy(update={"verdict": "snag", "findings": [Finding(**FINDING)]})
    gate = RequisitionGate()
    assert lint_gate(gate, [approved, snag], repo) == []
    assert gate.check(snag, repo).findings == snag.findings
