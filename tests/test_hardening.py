"""Hardening round closure and its lineage cap (TEMPO_PLAN.md sections 11.1, 11.4)."""

from datetime import UTC, datetime

import pytest

from tempo.audit import audit, audit_journal
from tempo.caps import CAPS, DECLARED_CAPS, consume, draws, remaining, spent
from tempo.config import Caps
from tempo.hardening import ROUND_SIGNAL, ROUND_STATES, hardener_round, open_round, rounds, round_state
from tempo.journal import TERMINAL_STATES, EventType, Journal
from tests.test_seed_path import repo, add_registry_row, context, ticket, terminal_body


def journal(tmp_path):
    return Journal(tmp_path, lambda: datetime(2026, 10, 8, tzinfo=UTC))


def test_round_state_maps_every_run_state():
    assert set(ROUND_STATES) == TERMINAL_STATES | {"running"}
    assert set(ROUND_STATES.values()) <= {"open", "closed"}


@pytest.mark.parametrize("terminal,expected", [
    ("merged", "closed"), ("already_satisfied", "closed"),
    ("rejected", "closed"), ("premise_failed", "closed"),
    ("gate_failed", "open"), ("invalid_artifact", "open"),
    ("timeout", "open"), ("infra_error", "open"),
    ("abandoned", "open"), ("budget_exceeded", "open"),
])
def test_round_state_closes_per_section_11_4(tmp_path, terminal, expected):
    j = journal(tmp_path)
    record(j)
    j.append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket="other")
    j.append(EventType.SIGNAL, {"signal": "drain_handoff", "to": "merged", "routed": "reject_queue"}, ticket="hardener")
    j.append(EventType.STATE_TRANSITION, {"to": "running", "routed": "reject_queue"}, ticket="hardener")
    assert round_state(j.read(), 1) == "open"
    j.append(EventType.STATE_TRANSITION, {"to": terminal}, ticket="hardener")
    assert round_state(iter(j.read()), 1) == expected
    j.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket="hardener")
    j.append(EventType.STATE_TRANSITION, {"to": "gate_failed"}, ticket="hardener")
    assert round_state(j.read(), 1) == expected
    j.append(EventType.STATE_TRANSITION, {"to": "gate_failed", "routed": "reject_queue"}, ticket="hardener")
    assert round_state(j.read(), 1) == "closed"
    j.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket="hardener")
    assert round_state(j.read(), 1) == "closed"


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES))
def test_every_reject_routed_terminal_closes(tmp_path, terminal):
    j = journal(tmp_path)
    record(j)
    j.append(EventType.STATE_TRANSITION, {"to": terminal, "routed": "reject_queue"}, ticket="hardener")
    assert round_state(j.read(), 1) == "closed"


def test_hardening_draws_pass_the_declared_cap_audit(tmp_path):
    j = journal(tmp_path)
    consume(j, "held", "hardening", "ticket-blob")
    assert DECLARED_CAPS == CAPS + ("hardening",)
    assert audit(j.read_segments()) == []
    assert audit_journal(j) == []
    assert [v.invariant for v in audit(j.read_segments(), caps=CAPS)] == ["declared_cap"]


def test_hardening_uses_the_lineage_fold_without_blocking_dispatch(tmp_path):
    j = journal(tmp_path)
    config = Caps(hardening=2)
    consume(j, "held", "hardening", "old-blob")
    consume(j, "held", "hardening", "new-blob")
    consume(j, "other", "hardening", "other-blob")
    assert draws(j.read(), "held", "hardening") == 2
    assert remaining(config, j.read(), "held", "hardening") == 0
    assert spent(config, iter(j.read()), "held") is None
    j.append(EventType.SIGNAL, {"signal": "reject_verdict", "verdict": "keep", "actor": "machine"}, ticket="held")
    assert remaining(config, j.read(), "held", "hardening") == 0
    j.append(EventType.SIGNAL, {"signal": "reject_verdict", "verdict": "keep", "actor": "operator"}, ticket="held")
    assert remaining(config, j.read(), "held", "hardening") == 2


def record(j, number=1, hardener="hardener"):
    return j.append(EventType.SIGNAL, {"signal": ROUND_SIGNAL, "round": number,
                    "units": {"19.P3.row": "absent"}, "filed_by": "filer",
                    "gaps": [{"unit": "19.P3.row", "message": "fact"}]}, ticket=hardener)


def test_round_record_anchors_identity_coverage_and_closure(tmp_path):
    j = journal(tmp_path)
    assert rounds(j.read()) == () and open_round(j.read()) is None
    assert hardener_round(j.read(), "plan-gap-1") is None
    j.append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket="arbitrary-name")
    record(j, hardener="arbitrary-name")
    [r] = rounds(iter(j.read()))
    assert (r.number, r.hardener, r.units, r.filed_by, r.gaps, r.position) == (
        1, "arbitrary-name", {"19.P3.row": "absent"}, "filer",
        [{"unit": "19.P3.row", "message": "fact"}], 1)
    assert round_state(j.read(), 1) == "open"
    assert open_round(j.read()) == hardener_round(j.read(), "arbitrary-name") == r
    j.append(EventType.STATE_TRANSITION, {"to": "premise_failed"}, ticket="arbitrary-name")
    assert round_state(j.read(), 1) == "closed" and open_round(j.read()) is None
    record(j, number=2, hardener="another-name")
    assert open_round(j.read()).number == 2
    assert round_state(j.read(), 1) == "closed"


def test_one_open_round_engine_wide(repo):
    import asyncio
    from dataclasses import replace
    from tempo.runner import hold_on_hardening
    from tests.test_stages import STEM, git
    add_registry_row(repo)
    ctx, _ = context(repo, [])
    plan = repo / "TEMPO_PLAN.md"
    plan.write_text(plan.read_text().replace("[gamma-seed]", "[gamma-seed, delta-seed]").replace(
        "  gamma-seed:", "  delta-seed: {fence: [tempo/thing.py]}\n  gamma-seed:"))
    git(repo, "add", "TEMPO_PLAN.md")
    git(repo, "commit", "-m", "second registry row")
    first = ticket(repo)
    second = replace(first, stem="existing-a")
    gaps = {"19.P3.gamma-seed": ["missing fact"]}
    asyncio.run(hold_on_hardening(ctx, first, gaps, attempt=0))
    # Deleting the hardener's directory cannot change the journaled round identity.
    path = repo / "tickets/plan-gap-1/ticket.md"
    path.unlink()
    path.parent.rmdir()
    asyncio.run(hold_on_hardening(ctx, second, {"19.P3.delta-seed": ["different fact"]}, attempt=7))
    assert len(rounds(ctx.driver.journal.read())) == 1
    assert terminal_body(ctx)["round"] == 1
    other = [e.body for e in ctx.driver.journal.read() if e.ticket == second.stem
             and e.type == EventType.STATE_TRANSITION][-1]
    assert other["round"] == 1
    assert other["plan_units"] == {"19.P3.delta-seed": "absent"}
    assert rounds(ctx.driver.journal.read())[0].units == {"19.P3.gamma-seed": "absent"}
    assert not (repo / "tickets/plan-gap-2/ticket.md").exists()
    ctx.driver.journal.append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket="plan-gap-1")
    asyncio.run(hold_on_hardening(ctx, second, {"19.P3.delta-seed": ["different fact"]}, attempt=8))
    assert [r.number for r in rounds(ctx.driver.journal.read())] == [1, 2]
    assert open_round(ctx.driver.journal.read()).number == 2
    assert open_round(ctx.driver.journal.read()).units == {"19.P3.delta-seed": "absent"}
    assert (repo / "tickets/plan-gap-2/ticket.md").is_file()
    assert git(repo, "log", "--format=%s").count("hardening round") == 2


@pytest.mark.parametrize("outcome", ["premise_failed", "gate_failed"])
@pytest.mark.parametrize("hardener", [False, True])
def test_a_hardener_is_never_hardened(repo, outcome, hardener, monkeypatch):
    import asyncio
    from tempo import runner
    from tempo.artifacts import Cost, Finding, StageResult
    from tempo.stages import StagesRun
    from tests.test_stages import STEM
    add_registry_row(repo)
    ctx, _ = context(repo, [])
    if hardener:
        record(ctx.driver.journal, hardener=STEM)
    async def stage(ctx, ticket):
        return StagesRun(attempt=1, results={"implement" if outcome == "premise_failed" else "check":
            StageResult(outcome=outcome, artifact=None, cost=Cost(), findings=[Finding(
                code="premise" if outcome == "premise_failed" else "requisition_review",
                message="missing fact", paved_road="state the fact",
                kind="spec_gap", unit="19.P3.gamma-seed")])})
    monkeypatch.setattr(runner, "run_stages", stage)
    assert asyncio.run(runner.drive(ctx, ticket(repo))) == outcome
    assert (terminal_body(ctx).get("dispatch") == "spec_gap_hold") == (not hardener)
    assert len(rounds(ctx.driver.journal.read())) == 1
    assert any(e.body.get("cap") == "hardening" for e in ctx.driver.journal.read()) == (not hardener)
