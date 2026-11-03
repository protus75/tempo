"""Scorecard entry obligations over real invoices and the production retro composition."""

import asyncio
import json
from dataclasses import FrozenInstanceError

import pytest

from tempo.artifacts import ReviewVerdict
from tempo.driver import LlmStage
from tempo.journal import EventType
from tempo.llm import FakeLLM
from tempo.lockfile import Lockfile
from tempo.mergequeue import MergeQueue
from tempo.retro import RetroArtifact, measurements, production_evidence, project_window, render_report
from tempo.scorecard import project_scorecard
from tempo.stages import PackingSlip, check, implement
from tempo.tickets import validate_ticket
from tests.support import confirmed
from tests.test_retro import completed, merged, rig
from tests.test_stages import implement_reply


async def invoice(c, writer, stem, *, bypass=False, attempt=0):
    text = confirmed().replace("uv run pytest", "true")
    if bypass:
        text = text.replace("kind: feature", "kind: feature\ngate_bypass:\n"
                            "  - {code: scope_fence, reason: generated sibling}")
    path = f"tickets/{stem}/ticket.md"
    c.fs.write(c.repo / path, text.encode())
    await c.git.add(c.repo, [path])
    await c.git.commit(c.repo, "ticket", only=[path])
    ticket = validate_ticket(stem, text, c.repo)
    writer.stages.driver.llm = FakeLLM([implement_reply()])
    slip = (await implement(writer.ctx, ticket, attempt=attempt)).artifact
    worktree = writer.ctx.worktree(stem)
    changed = f"outside-{stem}.py"
    c.fs.write(worktree / changed, b"outside = True\n")
    await c.git.add(worktree, [changed])
    await c.git.commit(worktree, "outside scope", only=[changed])
    return (await check(writer.ctx, ticket, slip, attempt=attempt)).artifact


async def review(c, writer, stem, verdict, *, attempt=0):
    code = "ticket" if verdict == "rma" else "logic"
    findings = [] if verdict == "approve" else [{"code": code, "message": "finding",
                                                "paved_road": "repair or retire"}]
    text = json.dumps({"verdict": verdict, "summary": "reviewed", "findings": findings})
    writer.stages.driver.llm = FakeLLM([text])
    stage = LlmStage(surface="review", emits=ReviewVerdict, gates=[], render=lambda *_: "review")
    return await writer.stages.driver.run(stage, None, ticket=stem, attempt=attempt,
        workspace=c.repo, tier="medium", effort="low", stuck_budget=600)


def rows(card):
    return {row.surface: row for row in card.surfaces}


def escape(journal, box_id, matches):
    return journal.append(EventType.EFFECT_COMPLETION, {"result": {
        "box_id": box_id, "bug_ticket": "repair", "app_commit": "a" * 40,
        "matches": matches, "confidence_note": "Bounded candidates; introducing commit unproven."}},
        ticket="repair", key=f"escape-attribution/{box_id}")


def test_escape_counts_once_per_report_ticket_surface(tmp_path):
    c, _, _, _ = rig(tmp_path)
    match = {"ticket": "historical", "commit": "a" * 40, "reviewed_sha": "b" * 40,
             "surfaces": ["review", "verification", "review"]}
    merged(c.journal, "historical", match["commit"])
    escape(c.journal, "old-window", [match])
    completed(c.journal, 1)
    duplicate = escape(c.journal, "one", [match, {**match, "commit": "c" * 40}, match])
    c.journal.append(duplicate.type, duplicate.body, ticket=duplicate.ticket, key=duplicate.key)
    escape(c.journal, "two", [match, {**match, "ticket": "another"}])
    escape(c.journal, "unattributed", [])
    c.journal.append(EventType.EFFECT_INTENT, duplicate.body, ticket="repair", key="escape-attribution/unfinished")
    c.journal.append(EventType.EFFECT_COMPLETION, {"result": {}}, ticket="current",
                     key="ticket-plane/current/0/checks")
    window = project_window(c.journal.read())
    window.completed_checks["ticket-plane/current/0/checks"] = (
        ("current", "failed", "fail", False), ("current", "bypassed", "fail", True),
        ("current", "unknown", "pass", False))
    card = project_scorecard(window)
    result = rows(card)
    assert set(result) == {"review", "verification", "failed", "bypassed", "unknown"}
    assert all(result[s].escapes == 3 and result[s].evaluated_tickets == result[s].escape_rate == 0
               for s in ("review", "verification"))
    assert all(result[s].escapes == 0 for s in ("failed", "bypassed", "unknown"))
    assert result["bypassed"].bypass_count == 1
    assert card.boundary == "retro/1" and card.merged_ticket_count == 0
    completed(c.journal, 2)
    assert project_scorecard(project_window(c.journal.read())).surfaces == ()


def test_escape_rates_and_pruning(tmp_path):
    c, _, _, _ = rig(tmp_path)
    for n in range(25):
        c.journal.append(EventType.EFFECT_COMPLETION, {"result": {}}, ticket=f"ticket-{n}",
                         key=f"ticket-plane/ticket-{n}/0/checks")
    escape(c.journal, "one", [{"ticket": "ticket-0", "commit": "a" * 40,
        "reviewed_sha": "b" * 40, "surfaces": ["z", "historical"]}])
    window = project_window(c.journal.read())
    for event in window.events[:-1]:
        window.completed_checks[event.key] = ((event.ticket, "z", "pass", False),
                                              (event.ticket, "clean", "pass", False))
    result = rows(project_scorecard(window))
    assert result["z"].escapes == 1 and result["z"].escape_rate == 1 / 25
    assert not result["z"].prune_candidate and result["clean"].prune_candidate
    assert result["historical"].escapes == 1 and result["historical"].evaluated_tickets == 0
    assert result["historical"].escape_rate == 0 and not result["historical"].prune_candidate


def test_scorecard_uses_completed_check_evidence(tmp_path):
    c, writer, _, _ = rig(tmp_path)
    old = asyncio.run(invoice(c, writer, "old"))
    completed(c.journal, 1)
    first = asyncio.run(invoice(c, writer, "first", bypass=True))
    second = asyncio.run(invoice(c, writer, "second"))
    assert old.reports and first.passed and not second.passed
    completions = [e for e in c.journal.read() if (e.key or "").endswith("/checks")
                   and e.type == EventType.EFFECT_COMPLETION]
    duplicate = completions[-1]
    c.journal.append(duplicate.type, duplicate.body, ticket=duplicate.ticket, key=duplicate.key)
    c.journal.append(EventType.EFFECT_INTENT, {}, ticket="unfinished",
                     key="ticket-plane/unfinished/0/checks")
    # A mutable invoice is never substituted for the completed lift's committed bytes.
    c.fs.write(c.repo / "tickets/first/checks.json", b"not an invoice")
    window = project_window(c.journal.read())
    evidence = asyncio.run(production_evidence(window, c))
    expected = [(inv.stem, r.code, r.verdict, r.code in inv.bypassed)
                for inv in (first, second) for r in inv.reports]
    assert evidence["check_observations"] == expected
    assert window.boundary == "retro/1"
    assert ("first", "scope_fence", "fail", True) in expected
    assert ("second", "scope_fence", "fail", False) in expected
    assert rows(project_scorecard(window))["scope_fence"].evaluated_tickets == 2
    assert asyncio.run(production_evidence(window, c)) == evidence


def test_scorecard_catches_resolved_and_upheld_findings(tmp_path):
    c, writer, _, _ = rig(tmp_path)
    asyncio.run(invoice(c, writer, "resolved"))
    async def resolve_gate():
        worktree = writer.ctx.worktree("resolved")
        (worktree / "outside-resolved.py").unlink()
        c.fs.write(worktree / "tempo/thing.py", b"ok\n")
        paths = ["outside-resolved.py", "tempo/thing.py"]
        await c.git.add(worktree, paths)
        await c.git.commit(worktree, "resolve scope finding", only=paths)
        ticket = validate_ticket("resolved", (c.repo / "tickets/resolved/ticket.md").read_text(), c.repo)
        slip = PackingSlip(produced_by_spec_version=1,
            produced_at_sha=await c.git.rev_parse(worktree, "HEAD"), stem="resolved", branch="resolved",
            outcome="ok", summary="resolved", run_record="tickets/resolved/run.md")
        return await check(writer.ctx, ticket, slip, attempt=1)
    assert asyncio.run(resolve_gate()).artifact.passed
    asyncio.run(review(c, writer, "resolved", "snag"))
    asyncio.run(review(c, writer, "resolved", "approve", attempt=1))
    merged(c.journal, "resolved")
    merged(c.journal, "resolved")
    asyncio.run(invoice(c, writer, "upheld"))
    c.journal.append(EventType.STATE_TRANSITION,
                     {"to": "gate_failed", "reason": "scope_fence", "stage": "check"}, ticket="upheld")
    asyncio.run(review(c, writer, "retired", "rma"))
    c.journal.append(EventType.STATE_TRANSITION,
                     {"to": "gate_failed", "stage": "review", "reason": "ticket"}, ticket="retired")
    c.journal.append(EventType.STATE_TRANSITION, {"to": "abandoned"}, ticket="retired")
    asyncio.run(invoice(c, writer, "bypass", bypass=True))
    c.journal.append(EventType.STATE_TRANSITION,
                     {"to": "gate_failed", "stage": "check", "reason": "scope_fence"}, ticket="bypass")
    merged(c.journal, "bypass")
    asyncio.run(review(c, writer, "unfinished", "snag"))
    c.journal.append(EventType.STATE_TRANSITION,
                     {"to": "gate_failed", "stage": "check", "reason": "diff_budget"}, ticket="gate-only")
    c.journal.append(EventType.STATE_TRANSITION,
                     {"to": "gate_failed", "stage": "check", "reason": "hardening cap spent"}, ticket="capped")
    window = project_window(c.journal.read())
    asyncio.run(production_evidence(window, c))
    result = rows(project_scorecard(window))
    assert result["scope_fence"].catches == 2 and result["scope_fence"].bypass_count == 1
    assert result["review"].catches == 2 and result["review"].evaluated_tickets == 3
    assert result["review"].catch_rate == 2 / 3
    assert result["diff_budget"].catches == 1
    assert "ticket" not in result and "logic" not in result and "hardening cap spent" not in result
    assert all(row.escapes == row.escape_rate == 0 for row in result.values())


def test_scorecard_rates_and_prune_threshold(tmp_path):
    c, _, _, _ = rig(tmp_path)
    assert project_scorecard(project_window([])).surfaces == ()
    # Collected observations alone are the pure projector's input, not mutable files.
    for n in range(25):
        c.journal.append(EventType.EFFECT_COMPLETION, {"result": {}}, ticket=f"ticket-{n}",
                         key=f"ticket-plane/ticket-{n}/0/checks")
    window = project_window(c.journal.read())
    for n, event in enumerate(window.events):
        window.completed_checks[event.key] = ((event.ticket, "z", "pass", False),
                                              (event.ticket, "a", "fail", True),
                                              (event.ticket, "a", "fail", True))
    first = project_window(window.events[:24])
    first.completed_checks.update(window.completed_checks)
    assert not any(row.prune_candidate for row in project_scorecard(first).surfaces)
    result = project_scorecard(window)
    assert [row.surface for row in result.surfaces] == ["a", "z"]
    assert all(row.evaluated_tickets == 25 and row.prune_candidate for row in result.surfaces)
    assert rows(result)["a"].bypass_count == 25
    assert all(row.catch_rate == row.escape_rate == row.escapes == 0 for row in result.surfaces)
    with pytest.raises(FrozenInstanceError):
        result.surfaces[0].catches = 1
    with pytest.raises(FrozenInstanceError):
        result.boundary = "changed"
    with pytest.raises(TypeError):
        result.signal_counts["rework"] = 99
    event = window.events[-1]
    window.completed_checks[event.key] += ((event.ticket, "z", "fail", False),)
    merged(c.journal, event.ticket)
    caught = project_window(c.journal.read())
    caught.completed_checks.update(window.completed_checks)
    assert rows(project_scorecard(caught))["z"].catches == 1
    assert rows(project_scorecard(caught))["z"].catch_rate == 1 / 25
    assert not rows(project_scorecard(caught))["z"].prune_candidate


def test_scorecard_totals_and_rendering(tmp_path, monkeypatch):
    c, writer, hook, _ = rig(tmp_path)
    asyncio.run(invoice(c, writer, "one", bypass=True))
    head = asyncio.run(c.git.rev_parse(c.repo, "HEAD"))
    asyncio.run(review(c, writer, "one", "approve"))
    call = c.journal.read()[-1]
    assert call.type == EventType.EFFECT_COMPLETION
    c.journal.append(call.type, call.body, ticket=call.ticket, key=call.key)
    merged(c.journal, "one", head)
    merged(c.journal, "one", head)
    c.journal.append(EventType.SIGNAL, {"signal": "rework_order", "attempt": 1}, ticket="one")
    MergeQueue(writer.ctx, escalate=lambda event: None, control=None)._facts("one", set(), set(), red=["red.py"])
    c.journal.append(EventType.STATE_TRANSITION, {"to": "gate_failed"}, ticket="other")
    window = project_window(c.journal.read())
    evidence = asyncio.run(production_evidence(window, c))
    before = c.journal.read()
    with monkeypatch.context() as patch:
        patch.setattr(c.fs, "write", lambda *_: pytest.fail("projection wrote"))
        patch.setattr(c.journal, "append", lambda *_a, **_k: pytest.fail("projection journaled"))
        patch.setattr(c.git, "_run", lambda *_: pytest.fail("projection read Git"))
        patch.setattr(writer.stages.driver.llm, "call", lambda *_: pytest.fail("projection called model"))
        card = project_scorecard(window)
        text = render_report(window, evidence, {"head": head}, [], RetroArtifact(proposals=[]))
        assert text == render_report(window, evidence, {"head": head}, [], RetroArtifact(proposals=[]))
    assert c.journal.read() == before
    assert card.merged_ticket_count == 1
    assert card.signal_counts == {"override": 1, "rework": 1, "gate-failure": 1, "integration-red": 1}
    assert card.gate_failure_count == 1
    assert "| scope_fence | 1 | 0 | 0 | 1 | 0.0000 | 0.0000 | false |" in text
    empty = project_window([])
    empty_text = render_report(empty, measurements(empty), {}, [], RetroArtifact(proposals=[]))
    assert empty_text.endswith("| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |\n")
    assert project_scorecard(empty).merged_ticket_count == 0
    assert all(count == 0 for count in project_scorecard(empty).signal_counts.values())
    writer.stages.driver.llm = FakeLLM(['{"proposals": []}'])
    lock = Lockfile(c.config.state_dir, instance_id="scorecard-test", clock=c.clock)
    lock.acquire()
    try:
        result = asyncio.run(hook(forced=True))
    finally:
        lock.release()
    receipt = asyncio.run(c.git._run(c.repo, "show", f"{result['commit']}:tickets/retro/000001.md"))
    assert receipt.split("## Surface scorecard")[1] == text.split("## Surface scorecard")[1]
    data = json.loads(receipt.split("```json\n")[1].split("\n```")[0])
    assert data["measurements"] == json.loads(json.dumps(evidence))
    assert project_window(c.journal.read()).boundary == "retro/1"
