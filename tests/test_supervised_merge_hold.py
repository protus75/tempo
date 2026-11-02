"""Durable supervision through the production admission and runner seams."""

import asyncio
from dataclasses import replace
import json

import pytest

from tempo import control, merge, runner, stages
from tempo.artifacts import Cost, OUTCOMES, StageResult
from tempo.audit import audit_journal
from tempo.effects import Effects
from tempo.journal import EVENT_VERSIONS, SIGNAL_NAMES, TERMINAL_STATES, EventType
from tempo.llm import FakeLLM, LLMResult
from tempo.mergequeue import MERGE_REGATE, MergeQueue, held_admissions, regate_receipts
from tempo.specs import ENTRY_PARTS, entry_unit_gap, required_units, resolve_plan_contract
from tests.test_mergequeue import ctx, ready, commit, admission_control


def supervised(ctx):
    queue = MergeQueue(ctx, escalate=lambda _: pytest.fail("supervision escalated"), control=admission_control(ctx))
    queue.supervised = True
    queue.control.inbox.admissions = lambda: {s: h for s, (h, _) in held_admissions(ctx.driver.journal.read()).items()}
    return queue


def baseline(ctx, verdict="GO", *, change=None):
    from tempo.providers import resolve
    from tempo.specs import load_spec
    tiers = {r.tier for r in ctx.config.routing if r.surface == "review"}
    identity = {surface: dict(spec_major=int(load_spec((ctx.specs_dir / f"{surface}.md").read_text()).meta.version.split(".")[0]),
        rows={tier: dict(provider=resolve(ctx.config, tier, surface).provider.name,
                        model=resolve(ctx.config, tier, surface).model) for tier in tiers}) for surface in ("review", "author")}
    if change:
        change(identity)
    ctx.driver.journal.append(EventType.SIGNAL, dict(signal="review_baseline", verdict=verdict, identity=identity))


async def served(ctx, ticket, pair=("fake", "m")):
    reply = json.dumps(dict(verdict="approve", summary="served approval", findings=[]))
    ctx.driver.llm = FakeLLM([LLMResult(reply, 1, 1, *pair, 0.0)])
    invoice = stages.Invoice(produced_by_spec_version=1, produced_at_sha=await ctx.git.rev_parse(ctx.repo, ticket.stem),
        stem=ticket.stem, passed=True, changed_files=[], inserted_lines=0, bypassed=[], reports=[])
    assert (await stages.review(ctx, ticket, invoice, attempt=0)).outcome == "ok"


async def hold(ctx, *, stem="held", evidence=True, **kwargs):
    ticket = await ready(ctx, stem, **kwargs)
    if evidence:
        await served(ctx, ticket)
    ctx.driver.journal.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket=stem)
    queue = supervised(ctx)
    queue.offer(ticket, attempt=0)
    before = await ctx.git.rev_parse(ctx.repo, "main")
    [result] = await queue.process()
    assert isinstance(result, merge.Held)
    assert await ctx.git.rev_parse(ctx.repo, "main") == before
    return ticket, queue, result


def confirm(queue, held, *, verdict="confirm", request_id="confirmation"):
    request = control.AdmissionRequest(request_id, queue.control.inbox.lifecycle_id, verdict,
                                       held.stem, held.hold_id, "operator")
    control.publish_request(queue.control.state_dir, request, queue.ctx.fs)
    queue.control.inbox.consume()


def test_supervised_hold_entry_unit():
    plan = (runner.SPECS_DIR.parent / "TEMPO_PLAN.md").read_text()
    unit = "19.P6.supervised-merge-hold"
    assert required_units(plan, "supervised-merge-hold", ()) == (unit,)
    for stem in ("supervised-merge-hold", "phase6-continue-05"):
        assert unit in required_units(plan, stem, ["19.I", unit, unit, "9", "12"])
        assert required_units(plan, stem, [unit, unit]).count(unit) == 1
        assert resolve_plan_contract(plan, [unit, unit]).count(f"### {unit} ") == 1
    assert entry_unit_gap(plan, unit) is None
    for part in ENTRY_PARTS:
        missing = plan.replace(f"- **{part}:**", f"- **Removed {part}:**")
        assert entry_unit_gap(missing, unit) == f"entry unit {unit} lacks its {part} part(s)"


@pytest.mark.parametrize("case", ["absent", "no-go", "revoked", "drift", "missing", "failover", "safety-red", "integration-red"])
def test_hold_after_green_before_main_mutation(ctx, monkeypatch, case):
    async def exercise():
        ticket = await ready(ctx)
        if case != "missing":
            await served(ctx, ticket, ("alternate", "spill") if case == "failover" else ("fake", "m"))
        if case in ("no-go", "revoked", "drift", "missing", "failover"):
            baseline(ctx, "NO_GO" if case == "no-go" else "GO",
                     change=(lambda i: i["review"].update(spec_major=999)) if case == "revoked" else None)
        if case == "drift":
            ctx.config.providers[0].models_by_tier.medium = "drifted"
        if case == "safety-red":
            (ctx.repo / f"tickets/{ticket.stem}/run.md").unlink()
            await commit(ctx, ctx.repo, [f"tickets/{ticket.stem}/run.md"])
        if case == "integration-red":
            from tempo.config import MechanicalCheck
            ctx.config.review.mechanical = [MechanicalCheck(code="combined", argv=["false"], trigger="always", severity="hard")]
        order = []
        for name in ("gather_safety_evidence", "gather_evidence"):
            original = getattr(stages, name)
            async def observed(*args, _name=name, _original=original, **kwargs):
                assert not regate_receipts(ctx.driver.journal.read())
                order.append(_name)
                return await _original(*args, **kwargs)
            monkeypatch.setattr(stages, name, observed)
        queue = supervised(ctx)
        queue.offer(ticket, attempt=0)
        before = await ctx.git.rev_parse(ctx.repo, "main")
        [result] = await queue.process()
        assert order[0] == "gather_safety_evidence"
        if case.endswith("red"):
            assert result.outcome == "gate_failed" and not regate_receipts(ctx.driver.journal.read())
        else:
            assert isinstance(result, merge.Held) and order == ["gather_safety_evidence", "gather_evidence", "gather_safety_evidence"]
            receipt = regate_receipts(ctx.driver.journal.read())[result.hold_id]
            assert receipt["admission"] == "HELD" and receipt["candidate_base_sha"] == before
            assert receipt["reason"] == ("go_revoked" if case in ("revoked", "drift") else
                "review_off_baseline" if case in ("missing", "failover") else "go_absent")
        assert await ctx.git.rev_parse(ctx.repo, "main") == before and ctx.worktree(ticket.stem).exists()
        assert not any(e.body.get("to") in TERMINAL_STATES for e in ctx.driver.journal.read())
    asyncio.run(exercise())


@pytest.mark.parametrize("case", ["covered", "later", "low", "high", "max", "contradictory", "duplicate-call", "bad-call", "bad-review", "missing-row"])
def test_go_coverage_uses_actual_review_identity(ctx, monkeypatch, case):
    async def exercise():
        ticket = await ready(ctx)
        if case in ("low", "high", "max", "missing-row"):
            tier = "high" if case == "missing-row" else case
            route = next(r for r in ctx.config.routing if r.surface == "review")
            ctx.config.routing.append(route.model_copy(update={"tier": tier}, deep=True))
            route.candidates[0].model = "other-tier-model"
            ticket = replace(ticket, frontmatter=ticket.frontmatter.model_copy(update={"agent_tier": tier}))
        await served(ctx, ticket)
        if case in ("contradictory", "duplicate-call", "bad-call"):
            events = ctx.driver.journal.read()
            altered = [replace(e, body={**e.body, "result": (None if case == "bad-call" else {**e.body["result"], "model": "foreign"})})
                if e.type == EventType.EFFECT_COMPLETION and (e.key or "").startswith("llm/") else e for e in events]
            if case == "duplicate-call":
                altered = [item for old, new in zip(events, altered, strict=True)
                           for item in ([new, old] if new != old else [old])]
            monkeypatch.setattr(ctx.driver.journal, "read", lambda: altered + original()[len(events):])
        if case == "bad-review":
            path = f"tickets/{ticket.stem}/review.md"
            raw = (ctx.repo / path).read_text().replace('"model": "m"', '"model": "foreign"')
            ctx.fs.write(ctx.repo / path, raw.encode())
            await commit(ctx, ctx.repo, [path])
        if case != "later":
            baseline(ctx, change=(lambda i: i["review"]["rows"].pop("high")) if case == "missing-row" else None)
        queue = supervised(ctx)
        queue.offer(ticket, attempt=0)
        [result] = await queue.process()
        if case in ("covered", "low", "high", "max"):
            assert result.outcome == "ok" and result.artifact.stem == ticket.stem
        else:
            assert isinstance(result, merge.Held)
            if case == "later":
                before = regate_receipts(ctx.driver.journal.read())
                baseline(ctx)
                monkeypatch.setattr(stages, "gather_evidence", lambda *_a, **_k: pytest.fail("unchanged GO release regated"))
                [result] = await queue.process()
                assert result.outcome == "ok" and regate_receipts(ctx.driver.journal.read()) == before
                assert not control.releases(ctx.driver.journal.read())
    original = ctx.driver.journal.read
    asyncio.run(exercise())


def test_held_run_keeps_worktree_and_has_no_terminal(ctx, monkeypatch):
    async def exercise():
        vocabularies = OUTCOMES, dict(EVENT_VERSIONS), set(SIGNAL_NAMES), set(TERMINAL_STATES), dict(stages.KNOWN_ARTIFACTS)
        ticket, queue, held = await hold(ctx)
        async def stage_run(*_):
            return stages.StagesRun(0, {"review": StageResult(outcome="ok", artifact=None, findings=[], cost=Cost())})
        async def admission(*_a, **_k):
            return held
        monkeypatch.setattr(runner, "run_stages", stage_run)
        for name in ("harvest_failure", "failure_terminal", "consume"):
            monkeypatch.setattr(runner, name, lambda *_a, **_k: pytest.fail("held run entered failure spine"))
        assert await runner.drive(ctx, ticket, admission=admission) == "running"
        assert held == merge.Held(ticket.stem, 0, held.hold_id) and not isinstance(held, StageResult)
        assert held_admissions(ctx.driver.journal.read())[ticket.stem][0] == held.hold_id
        assert ctx.worktree(ticket.stem).exists() and not queue.pending and queue.active is None and not queue._slot.locked()
        assert not any(e.body.get("to") in TERMINAL_STATES or e.type == EventType.CAP_CONSUMED for e in ctx.driver.journal.read())
        assert vocabularies == (OUTCOMES, dict(EVENT_VERSIONS), set(SIGNAL_NAMES), set(TERMINAL_STATES), dict(stages.KNOWN_ARTIFACTS))
        assert audit_journal(ctx.driver.journal) == []
    asyncio.run(exercise())
