"""Production evidence, report effects, and lock-held retro invocation."""

import asyncio
import json
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from tempo import runner
from tempo.box import Box, DecisionRecord, render_record
from tempo.effects import Effects
from tempo.journal import EventType, Journal
from tempo.llm import FakeLLM
from tempo.retro import (Retro, RetroArtifact, measurements, production_evidence, project_window,
                         render_report, select_trigger)
from tempo.specs import RENDER_BOUND_CHARS, REQ_RENDER_HEADROOM, data_open, data_close
from tests.support import Clock, make_root, pause_checkout, confirmed, commit_ticket


def rig(tmp_path, script=()):
    from tempo.__main__ import build_control
    c = pause_checkout(make_root(tmp_path))
    c = replace(c, journal=Journal(c.config.state_dir, c.clock), control=None)
    c = replace(c, control=build_control(c))
    llm = FakeLLM(list(script))
    writer = runner.bind(c, llm)
    writer.stages.driver.retry_cap = 0
    return c, writer, Retro(c, writer), llm


def completed(journal, sequence):
    return journal.append(EventType.EFFECT_COMPLETION, {"result": {"commit": "receipt"}},
                          key=f"retro/{sequence}")


def merged(journal, stem="one", commit=None):
    return journal.append(EventType.STATE_TRANSITION, {"to": "merged", "commit": commit}, ticket=stem)


def test_window_uses_completed_report_boundary(tmp_path):
    c, writer, hook, llm = rig(tmp_path)
    j = c.journal
    assert project_window([]).events == () and project_window([]).started_at is None
    assert select_trigger(project_window([]), measurements(project_window([])), c.clock(), forced=False) is None
    first = merged(j, "first", "real-merge-sha")
    j.append(EventType.EFFECT_INTENT, {}, key="retro/1")
    initial = project_window(j.read())
    assert initial.events[0] == first and initial.boundary == "initial"
    facts = measurements(initial)
    assert facts["merged_count"] == 1 and facts["merges"][0]["commit"] == "real-merge-sha"
    report = render_report(initial, facts, {"head": "actual-head", "spec_version": "1.0"}, [],
                           RetroArtifact(proposals=[]))
    assert report == render_report(initial, facts, {"head": "actual-head", "spec_version": "1.0"}, [],
                                  RetroArtifact(proposals=[]))
    assert '"merged_count": 1' in report and '"actual-head"' in report
    completed(j, 1)
    older = merged(j, "older")
    completed(j, 2)
    last = j.append(EventType.SIGNAL, {"kind": "handoff_written"}, ticket=None)
    j.append(EventType.EFFECT_INTENT, {}, key="retro/3")
    window = project_window(j.read())
    assert window.boundary == "retro/2" and window.sequence == 3
    assert window.events == (last, j.read()[-1]) and older not in window.events
    assert measurements(window)["merged_count"] == 0


@pytest.mark.parametrize("size", [100, int(RENDER_BOUND_CHARS * REQ_RENDER_HEADROOM / 2), RENDER_BOUND_CHARS * 2])
def test_window_over_headroom_is_read_on_demand(tmp_path, monkeypatch, size):
    c, writer, hook, llm = rig(tmp_path, ['{"proposals": []}'])
    merged(c.journal)
    c.journal.append(EventType.SIGNAL, {"kind": "handoff_written", "payload": "x" * size}, key="large")
    # Preserve multiplicity, null tickets, and journal order rather than sorting timestamps.
    event = c.journal.read()[-1]
    c.journal.append(event.type, event.body, ticket=event.ticket, key=event.key)
    expected = [asdict(e) for e in c.journal.read()]
    evidence = asyncio.run(production_evidence(project_window(c.journal.read()), c))
    driver = writer.stages.driver
    writes = []
    write = driver.spool._fs.write
    def captured(path, content):
        writes.append(path)
        write(path, content)
    monkeypatch.setattr(driver.spool._fs, "write", captured)
    result = asyncio.run(hook(forced=True))
    assert result == {"commit": asyncio.run(c.git.rev_parse(c.repo, "HEAD"))}
    prompt = llm.requests[0].rendered
    assert len(prompt) <= RENDER_BOUND_CHARS
    material = json.loads(prompt.split(data_open("window") + "\n", 1)[1].split("\n" + data_close("window"), 1)[0])
    assert material["measurements"] == json.loads(json.dumps(evidence))
    if size == 100:
        assert material["events"] == expected and "events_path" not in material
        assert not list(driver.spool.root.rglob("window.jsonl"))
    else:
        assert "events" not in material
        path = Path(material["events_path"])
        assert path in writes
        assert path == driver.spool.root / "retro/1/1/retro/call-01/window.jsonl"
        assert [json.loads(line) for line in path.read_text().splitlines()] == expected
    completion, = [e for e in c.journal.read() if e.key == "retro/1" and e.type == EventType.EFFECT_COMPLETION]
    assert completion.body == {"result": result}
    assert asyncio.run(c.git._run(c.repo, "show", "--name-only", "--format=", result["commit"])).splitlines() == [
        "tickets/retro/000001.md"]


@pytest.mark.parametrize("kind", ["merges", "elapsed", "override", "rework", "gate-failure", "integration-red"])
def test_retro_trigger_boundaries(tmp_path, kind):
    c, writer, hook, llm = rig(tmp_path)
    j = c.journal
    limit = 25 if kind == "merges" else 5
    def operand(n):
        stem = f"admission-{n}"
        j.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket=stem)
        if kind == "merges":
            merged(j, stem)
        elif kind == "rework":
            from tempo.rework import rework
            from tempo.tickets import validate_ticket
            ticket = validate_ticket(stem, confirmed(), c.repo)
            writer.stages.driver.llm = FakeLLM(['{"action": "escalate", "tickets": []}'])
            result = asyncio.run(rework(writer.ctx, ticket, confirmed(), [], attempt=n,
                workspace=c.repo, tier="medium", effort="low", stuck_budget=120,
                handoff=None))
            assert result.outcome == "ok"
        elif kind == "gate-failure":
            from tempo.tickets import validate_ticket
            ticket = validate_ticket(stem, confirmed(), c.repo)
            assert asyncio.run(runner.failure_terminal(writer.ctx, ticket, outcome="gate_failed", stage="check",
                               findings=[], attempt=n)) == "gate_failed"
        elif kind == "integration-red":
            writer.queue._facts(stem, set(), set(), red=["work/red.py"])
        elif kind == "override":
            from tempo.stages import Invoice
            path = f"tickets/{stem}/checks.json"
            invoice = Invoice(produced_by_spec_version=1, produced_at_sha="branch", stem=stem, passed=True,
                changed_files=[], inserted_lines=0, bypassed=["scope_fence"], reports=[])
            async def publish():
                c.fs.write(c.repo / path, invoice.model_dump_json().encode())
                await c.git.add(c.repo, [path])
                await c.git.commit(c.repo, "checks", only=[path])
                return {"commit": await c.git.rev_parse(c.repo, "HEAD")}
            asyncio.run(writer.stages.driver.effects.run(publish, key=f"ticket-plane/{stem}/0/checks", ticket=stem))
            merged(j, stem)
    def due():
        window = project_window(j.read())
        facts = asyncio.run(production_evidence(window, c))
        return select_trigger(window, facts, c.clock(), forced=False)
    if kind == "elapsed":
        merged(j)
        c.clock.now += timedelta(days=7, microseconds=-1)
        assert due() is None
        c.clock.now += timedelta(microseconds=1)
    else:
        for n in range(limit - 1):
            operand(n)
        assert due() is None
        # Duplicate records of one invocation never combine into another operand.
        if kind in {"integration-red", "rework", "gate-failure"}:
            event = j.read()[-1]
            j.append(event.type, event.body, ticket=event.ticket, key=event.key)
            assert due() is None
        operand(limit - 1)
    assert due() == kind


def test_mixed_spikes_do_not_sum_and_new_invocations_do_count(tmp_path):
    c, writer, hook, _ = rig(tmp_path)
    j = c.journal
    for n in range(4):
        j.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket="same-stem")
        writer.queue._facts("same-stem", set(), set(), red=["red.py"])
        j.append(EventType.SIGNAL, {"signal": "rework_order", "attempt": n}, ticket="same-stem")
        j.append(EventType.STATE_TRANSITION, {"to": "gate_failed"}, ticket="same-stem")
    window = project_window(j.read())
    facts = measurements(window)
    assert facts["spikes"] == {"override": 0, "rework": 4, "gate-failure": 4, "integration-red": 4}
    assert select_trigger(window, facts, c.clock(), forced=False) is None


def test_report_commit_replays_without_advancing_unfinished_window(tmp_path, monkeypatch):
    c, writer, hook, llm = rig(tmp_path, ['{"proposals": []}'])
    merged(c.journal)
    window = project_window(c.journal.read())
    # Even a committed file does not close the unfinished effect's journal window.
    class Crash(BaseException):
        pass
    append = c.journal.append
    def crash(type_, body, **kwargs):
        if type_ == EventType.EFFECT_COMPLETION and kwargs.get("key") == "retro/1":
            raise Crash()
        return append(type_, body, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(c.journal, "append", crash)
        with pytest.raises(Crash):
            asyncio.run(hook(forced=True))
    receipt = c.repo / "tickets/retro/000001.md"
    assert receipt.is_file() and project_window(c.journal.read()).boundary == "initial"
    head = asyncio.run(c.git.rev_parse(c.repo, "HEAD"))
    body = receipt.read_bytes()
    driver = writer.stages.driver
    driver.effects = Effects(c.journal)
    assert asyncio.run(hook(forced=True)) == {"commit": head}
    assert len(llm.requests) == 1 and receipt.read_bytes() == body
    assert asyncio.run(c.git.rev_parse(c.repo, "HEAD")) == head
    effects = [e for e in c.journal.read() if e.key == "retro/1"]
    assert [e.type for e in effects] == [EventType.EFFECT_INTENT, EventType.EFFECT_INTENT, EventType.EFFECT_COMPLETION]
    assert effects[-1].ticket is None and effects[-1].body == {"result": {"commit": head}}
    assert asyncio.run(hook.commit_report(driver, window, "must never be written")) == {"commit": head}
    assert receipt.read_bytes() == body and project_window(c.journal.read()).boundary == "retro/1"
    assert asyncio.run(c.git._run(c.repo, "show", "--name-only", "--format=", head)).splitlines() == [
        "tickets/retro/000001.md"]


def test_report_file_without_intent_cannot_replace_the_production_receipt(tmp_path):
    c, writer, hook, llm = rig(tmp_path, ['{"proposals": []}'])
    path = "tickets/retro/000001.md"
    async def inherited():
        c.fs.write(c.repo / path, b"an inherited file is not completed evidence\n")
        await c.git.add(c.repo, [path])
        await c.git.commit(c.repo, "inherited file", only=[path])
    asyncio.run(inherited())
    assert project_window(c.journal.read()).boundary == "initial"
    merged(c.journal)
    result = asyncio.run(hook(forced=True))
    assert result["commit"] == asyncio.run(c.git.rev_parse(c.repo, "HEAD"))
    assert len(llm.requests) == 1 and (c.repo / path).read_text().startswith("# Retro receipt")


def test_failed_retro_suppresses_window_until_completion(tmp_path):
    c, writer, hook, llm = rig(tmp_path, [RuntimeError("secret diagnostic"), RuntimeError("later outage"), '{"proposals": []}'])
    assert asyncio.run(hook()) is None and c.journal.read() == []
    assert Box(c.config.state_dir / "box", c.fs).messages() == []
    merged(c.journal)
    assert asyncio.run(hook(forced=True)) is None
    [failed] = [e for e in c.journal.read() if e.body.get("kind") == "retro_failed"]
    assert asdict(failed) == {"v": 1, "type": "signal", "ts": failed.ts, "ticket": None,
        "key": "retro-failed/initial/forced", "body": {"kind": "retro_failed", "window_boundary": "initial",
        "trigger": "forced", "error_code": "ValueError"}}
    [message] = Box(c.config.state_dir / "box", c.fs).messages()
    assert message.origin == failed.key and message.message_class == "failure_report"
    assert "secret diagnostic" not in json.dumps(failed.body) + message.summary
    before = c.journal.read()
    restarted = replace(c, journal=Journal(c.config.state_dir, c.clock))
    writer.stages.driver.journal = restarted.journal
    writer.stages.driver.effects = Effects(restarted.journal)
    resumed = Retro(restarted, writer)
    for forced in (False, True, True):
        assert asyncio.run(resumed(forced=forced)) is None
    assert restarted.journal.read() == before and len(llm.requests) == 1
    assert len(Box(c.config.state_dir / "box", c.fs).messages()) == 1
    # Journal order, even with equal timestamps, releases a failure on a later merge.
    merged(restarted.journal, "later")
    assert asyncio.run(resumed(forced=True)) is None
    failures = [e for e in restarted.journal.read() if e.body.get("kind") == "retro_failed"]
    assert len(failures) == 2 and failures[-1].key == failed.key
    assert failures[-1].body == failed.body
    after_release = restarted.journal.read()
    assert asyncio.run(resumed(forced=True)) is None
    assert restarted.journal.read() == after_release and len(llm.requests) == 2
    assert len(Box(c.config.state_dir / "box", c.fs).messages()) == 1
    # A lock-held completed report releases the renewed durable suppression.
    asyncio.run(hook.commit_report(writer.stages.driver, project_window(after_release), "recovered report\n"))
    merged(restarted.journal, "next")
    assert asyncio.run(resumed(forced=True))["commit"]
    assert len(llm.requests) == 3 and project_window(restarted.journal.read()).boundary == "retro/2"


def test_artifact_is_closed_and_proposals_pair_the_risks():
    for raw in ({"proposals": [], "merged_count": 999}, {"proposals": [{"fixed_failure": "failure"}]},
                {"proposals": [{"fixed_failure": "failure", "overcorrection_risk": "risk", "proposed_spec_paths": ["../x.md"]}]}):
        with pytest.raises(ValidationError):
            RetroArtifact.model_validate(raw)


def test_receipt_uses_engine_provenance_and_tombstones(tmp_path):
    proposal = {"fixed_failure": "A witnessed failure", "overcorrection_risk": "Rejecting useful inputs",
                "proposed_spec_paths": ["specs/review.md"]}
    c, writer, hook, llm = rig(tmp_path, [json.dumps({"proposals": [proposal]})])
    commit_ticket(c.repo, "one", confirmed())
    record = DecisionRecord(id="retired", kind="tombstone", link="one", reopen_after_days=7)
    c.fs.write(c.repo / "tickets/decisions/retired.md", render_record(record, "Evidence digest\n").encode())
    c.journal.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket="one")
    c.clock.now += timedelta(seconds=120)
    head = asyncio.run(c.git.rev_parse(c.repo, "HEAD"))
    merged(c.journal, "one", head)
    assert asyncio.run(hook(forced=True))["commit"] != head
    report = (c.repo / "tickets/retro/000001.md").read_text()
    data = json.loads(report.split("```json\n")[1].split("\n```")[0])
    assert data["provenance"] == {"head": head, "spec_version": "1.2", "at": c.clock().isoformat()}
    assert data["measurements"]["merged_count"] == 1
    assert data["measurements"]["elapsed_seconds"] == [{"ticket": "one", "commit": head, "actual_seconds": 120.0}]
    assert data["tombstone_digest"] == [{**record.model_dump(), "body": "Evidence digest\n"}]
    assert data["proposals"] == [proposal]
    assert all(request.worktree is None for request in llm.requests)
    message, = Box(c.config.state_dir / "box", c.fs).messages()
    assert message.message_class == "retro_finding" and message.retro_report_key == "retro/1"
    assert {key: getattr(message, key) for key in proposal} == proposal
    completion, = [e for e in c.journal.read() if e.type == EventType.EFFECT_COMPLETION and e.key == "retro/1"]
    occurrence, = [e for e in c.journal.read() if e.body.get("kind") == "storm_occurrence"]
    assert c.journal.read().index(completion) < c.journal.read().index(occurrence)


def test_retro_itemizes_confirmed_class_tombstones(tmp_path, monkeypatch):
    from tempo.box import Resolution, record_path
    from tempo.storm import storm_producer
    c, writer, hook, llm = rig(tmp_path, ['{"proposals": []}'])
    box = storm_producer(root=c.config.state_dir / "box", fs=c.fs, journal=c.journal, clock=c.clock)
    id, _ = box.enqueue(message_class="failure_report", origin="test", summary="confirmed-class failure",
                        occurrence_id="original")
    record = DecisionRecord(id=f"tombstone-{id}", kind="tombstone", link="known", reopen_after_days=7)
    c.fs.write(c.repo / record_path(record.id), render_record(record, "Witnessed failure\n").encode())
    box.resolve(id, Resolution(kind="tombstone", link=record.id))
    expected = {**record.model_dump(), "body": "Witnessed failure\n", "box_id": id,
                "message_class": "failure_report", "reports": 0, "reopen": False}
    before = {p: p.read_bytes() for p in box.root.glob("*.json")}
    with monkeypatch.context() as patch:
        patch.setattr(c.fs, "write", lambda *_: pytest.fail("tombstone projection wrote"))
        patch.setattr(c.journal, "append", lambda *_: pytest.fail("tombstone projection journaled"))
        assert hook.tombstones() == [expected]
    assert {p: p.read_bytes() for p in box.root.glob("*.json")} == before
    for n in range(3):
        box.record_rereport(id, occurrence_id=f"report/{n}")
    expected.update(reports=3, reopen=True)
    assert hook.tombstones() == [expected]
    merged(c.journal)
    assert asyncio.run(hook(forced=True))["commit"]
    data = json.loads((c.repo / "tickets/retro/000001.md").read_text().split("```json\n")[1].split("\n```")[0])
    assert data["tombstone_digest"] == [expected]
    assert data["tombstone_reopens"] == [{"kind": "tombstone_auto_reopened", "box_id": id,
                                         "signature": box.get(id).signature, "reports": 3}]


def test_escape_report_projection(tmp_path, monkeypatch):
    from tests.test_triage import escape_rig, escape_report
    from tempo.scorecard import project_scorecard
    from tempo.status import project, render as render_status
    async def scenario():
        c, writer, box, checked, admitted = await escape_rig(tmp_path)
        c, message, _ = await escape_report(c, commit=admitted.commit, paths=["tempo/thing.py"])
        window = project_window(c.journal.read())
        evidence = await production_evidence(window, c)
        before = c.journal.read()
        def forbidden(*args, **kwargs):
            pytest.fail("pure escape projection performed an effect or read")
        with monkeypatch.context() as patch:
            for target, method in ((c.fs, "write"), (c.git, "_run"), (c.journal, "append"),
                                   (c.journal, "read"), (writer.stages.driver.llm, "call"),
                                   (Path, "read_text"), (Path, "read_bytes"), (Box, "resolve")):
                patch.setattr(target, method, forbidden)
            card = project_scorecard(window)
            receipt = render_report(window, evidence, {}, [], RetroArtifact(proposals=[]))
            status = render_status(project(before, window=window))
            assert card == project_scorecard(window)
            assert receipt == render_report(window, evidence, {}, [], RetroArtifact(proposals=[]))
            assert receipt.split("| Surface |")[1] == status.split("| Surface |")[1]
            assert "| review | 1 | 0 | 1 | 0 | 0.0000 | 1.0000 | false |" in receipt
        assert c.journal.read() == before
        writer.stages.driver.llm = FakeLLM(['{"proposals": []}'])
        result = await Retro(c, writer)(forced=True)
        receipt = await c.git._run(c.repo, "show", f"{result['commit']}:tickets/retro/000001.md")
        assert "| review | 1 | 0 | 1 |" in receipt
    asyncio.run(scenario())
