"""Production storm escalation, producer identities, and write-ahead crash recovery."""

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest

from tempo import __main__ as cli, daemon, journal as journal_module, runner, stages
from tempo.artifacts import Finding, StageResult
from tempo.box import Box, BoxError, Message, Resolution, Verdict
from tempo.flake import Flake, RerunEvidence
from tempo.journal import EventType, Journal
from tempo.llm import FakeLLM
from tempo.tickets import validate_ticket
from tests.test_cli import ENV
from tempo.seams import LocalFileSystem
from tempo.storm import STORM_THRESHOLD, STORM_TRIP, STORM_WINDOW, StormLedger
from tests.test_daemon_composition import CoreRig
from tests.test_drain import Script, commit_ticket, confirmed, make_root
from tests.test_journal import FakeClock, T0
from tests.test_mergequeue import ctx as admission_context, ready, scripted
from tests.test_stages import STEM, agent
from tests.test_storm import SIG, snapshot
from tests.test_storm_producer import ARRIVAL, compose
from tempo import storm
from tempo import backlog
from tempo.checkout import Checkout


def digest(parts):
    return hashlib.sha256(json.dumps(parts, separators=(",", ":"), ensure_ascii=True)
                          .encode("utf-8")).hexdigest()


def identity(parts):
    return "storm-arrival/" + digest(parts)


def occurrences(journal, signature=None):
    return [e for e in journal.read() if e.body.get("kind") == "storm_occurrence"
            and (signature is None or e.body["signature"] == signature)]


def trips(journal):
    return [e for e in journal.read() if e.body.get("kind") == STORM_TRIP]


def six(box, **changes):
    for n in range(6):
        box.enqueue(**{**ARRIVAL, **changes}, occurrence_id=f"arrival/{n}")


def test_production_arrivals_and_dedup_hits_trip_once(tmp_path):
    rig = CoreRig(tmp_path)
    # Keep the harness's process seam; give the queue its real disposable filesystem.
    rig.fs.write = LocalFileSystem().write
    box = rig.core.control._recover.__self__
    assert box.fs is rig.checkout.fs
    assert box._arrival.__self__.journal is rig.checkout.journal
    assert box._arrival.__self__.clock is rig.checkout.clock
    six(box)
    sources = [m for m in box.messages() if m.origin == "one"]
    reports = [m for m in box.messages() if m.origin.startswith("storm-breaker/")]
    assert len(sources) == len(reports) == len(trips(rig.journal)) == 1
    assert len(occurrences(rig.journal, SIG)) == 6
    trip = trips(rig.journal)[0]
    tid = digest([SIG, "arrival/0", "arrival/5"])
    assert trip.key == "storm-trip/" + tid and trip.ticket is None
    assert trip.body == dict(kind=STORM_TRIP, signature=SIG, trip_id=tid,
        first_occurrence_id="arrival/0", crossing_occurrence_id="arrival/5",
        emitting_stage="implement", emitting_origin="one",
        held=dict(emitting_stage="implement", emitting_origin="one"))
    report = reports[0]
    assert Message.model_validate_json(box._path(report).read_bytes()) == report
    assert (report.message_class, report.origin, report.stage, report.outcome, report.bug_origin,
            report.has_repro, report.status, report.verdict, report.resolution) == (
        "failure_report", "storm-breaker/" + tid, None, None, None, None, "pending", None, None)
    assert all(part in report.summary for part in ("P0", SIG, tid, "6", "one-hour", "implement", "one"))
    events = rig.journal.read()
    assert events.index(occurrences(rig.journal, SIG)[-1]) < events.index(trip)
    assert events.index(trip) < events.index(occurrences(rig.journal, report.signature)[0])
    assert [m.seq for m in box.messages()] == [1, 2]
    # A source not yet stored at crossing must allocate after the nested report.
    state = tmp_path / "nested"
    box, journal, clock = compose(state)
    ledger = StormLedger(journal=journal, clock=clock)
    for n in range(5):
        ledger.record(signature=SIG, occurrence_id=str(n), emitting_stage="implement", emitting_origin="one")
    box.enqueue(**ARRIVAL, occurrence_id="cross")
    assert [m.seq for m in box.messages()] == [1, 2]
    assert box.messages()[0].origin.startswith("storm-breaker/")


def test_storm_threshold_window_and_signature_isolation(tmp_path, monkeypatch):
    assert STORM_THRESHOLD == 5 and STORM_WINDOW == timedelta(hours=1)
    box, journal, clock = compose(tmp_path)
    ledger = StormLedger(journal=journal, clock=clock)
    # Seed the predecessor ledger to test out-of-order timestamps before activation.
    for name, seconds in [("future", .000001), ("excluded", -3600), ("first", -3599.999999),
                          ("second", -300), ("third", -200), ("fourth", -100)]:
        clock.now = T0 + timedelta(seconds=seconds)
        ledger.record(signature=SIG, occurrence_id=name, emitting_stage="check", emitting_origin="one")
    monkeypatch.setattr(journal_module, "ROLL_BYTES", 1)
    clock.now = T0
    box.enqueue(**ARRIVAL, occurrence_id="fifth")
    assert trips(journal) == [] and ledger.count(SIG) == 5
    box.enqueue(**{**ARRIVAL, "origin": "other"}, occurrence_id="isolated")
    assert trips(journal) == []
    box.enqueue(**ARRIVAL, occurrence_id="crossing")
    trip, = trips(journal)
    assert trip.body["first_occurrence_id"] == "first"
    assert trip.body["crossing_occurrence_id"] == "crossing"
    assert trip.body["trip_id"] == digest([SIG, "first", "crossing"])
    clock.advance(7200)
    box, journal, _ = compose(tmp_path, clock)
    before = snapshot(tmp_path)
    box.recover()
    assert snapshot(tmp_path) == before and len(trips(journal)) == 1
    assert len(list(journal.dir.glob("*.jsonl"))) > 1


@pytest.mark.parametrize("stage,origin", [("implement", "one"), ("check", None), ("review", "one"),
                                         (None, None), (None, "suggestion-origin"),
                                         ("depends", "child"), ("unknown", "one"), ("", "")])
def test_storm_trip_targets_are_closed(tmp_path, stage, origin):
    box, journal, clock = compose(tmp_path)
    ledger = StormLedger(journal=journal, clock=clock)
    for n in range(6):
        ledger.record(signature=SIG, occurrence_id=str(n), emitting_stage=stage, emitting_origin=origin)
    box.recover()
    trip, = trips(journal)
    assert trip.body["emitting_stage"] == stage and trip.body["emitting_origin"] == origin
    assert trip.body["held"] == (dict(emitting_stage=stage, emitting_origin=origin)
                                 if stage in {"implement", "check", "review"} else None)
    assert len(box.messages()) == 1


@pytest.mark.parametrize("point", ["occurrence", "trip", "report-occurrence", "report"])
@pytest.mark.parametrize("after", [False, True])
@pytest.mark.parametrize("exception", [OSError, BaseException])
def test_storm_trip_and_report_crash_replay(tmp_path, monkeypatch, point, after, exception):
    box, journal, clock = compose(tmp_path)
    for n in range(5):
        box.enqueue(**ARRIVAL, occurrence_id=f"arrival/{n}")
    append, write = journal.append, box.fs.write

    def fail_append(type, body, **kwargs):
        is_target = ((point == "trip" and body.get("kind") == STORM_TRIP)
                     or (point == "occurrence" and body.get("occurrence_id") == "arrival/5")
                     or (point == "report-occurrence" and str(body.get("occurrence_id")).startswith("storm-report/")))
        if is_target:
            if after:
                append(type, body, **kwargs)
            raise exception("injected crash")
        return append(type, body, **kwargs)

    def fail_write(path, data):
        if point == "report" and json.loads(data)["origin"].startswith("storm-breaker/"):
            if after:
                write(path, data)
            raise exception("injected crash")
        return write(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(journal, "append", fail_append)
        patch.setattr(box.fs, "write", fail_write)
        with pytest.raises(exception, match="injected crash"):
            box.enqueue(**ARRIVAL, occurrence_id="arrival/5")
    if point == "occurrence" and not after:
        # No durable crossing exists; retry the original arrival while its window is live.
        box, journal, _ = compose(tmp_path, clock)
        box.enqueue(**ARRIVAL, occurrence_id="arrival/5")
    clock.advance(7200)
    monkeypatch.setattr(journal_module, "ROLL_BYTES", 1)
    box, journal, _ = compose(tmp_path, clock)
    # Reconstruction alone finishes the sequence, even after the original window expired.
    box.recover()
    trip, = trips(journal)
    assert trip.body["trip_id"] == digest([SIG, "arrival/0", "arrival/5"])
    report, = [m for m in box.messages() if m.origin.startswith("storm-breaker/")]
    verdict = Verdict(verdict="decision", produced_by_spec_version="1", rationale="already reviewed")
    resolution = Resolution(kind="decision", link="known")
    box.record_verdict(report.id, verdict)
    box.resolve(report.id, resolution)
    before = snapshot(tmp_path)
    box.recover()
    box.enqueue(**ARRIVAL, occurrence_id="arrival/5")
    assert snapshot(tmp_path) == before
    assert box.get(report.id).verdict == verdict and box.get(report.id).resolution == resolution
    assert len(occurrences(journal, SIG)) == 6
    assert len(occurrences(journal, report.signature)) == 1
    assert len(trips(journal)) == 1 and len(box.messages()) == 2


@pytest.mark.parametrize("damage", ["missing", "extra", "key", "ticket", "type", "id", "held",
                                   "stage", "origin", "first", "crossing", "signature", "kind"])
def test_storm_trip_evidence_fails_closed(tmp_path, damage):
    box, journal, _ = compose(tmp_path)
    six(box)
    trip, = trips(journal)
    body, key, ticket, type = dict(trip.body), trip.key, None, EventType.SIGNAL
    if damage == "missing":
        del body["held"]
    elif damage == "extra":
        body["extra"] = "unexpected"
    elif damage == "key":
        key = "storm-trip/wrong"
    elif damage == "ticket":
        ticket = "one"
    elif damage == "type":
        type = EventType.EFFECT_INTENT
    else:
        field = {"id": "trip_id", "stage": "emitting_stage", "origin": "emitting_origin",
                 "first": "first_occurrence_id", "crossing": "crossing_occurrence_id"}.get(damage, damage)
        body[field] = "drain_halted" if field == "kind" else "wrong"
    journal.append(type, body, key=key, ticket=ticket)
    before = snapshot(tmp_path)
    for operation in (box.recover, lambda: box.enqueue(**ARRIVAL, occurrence_id="new")):
        with pytest.raises(ValueError, match="repair the producing evidence.*never overwrite"):
            operation()
        assert snapshot(tmp_path) == before


def test_storm_report_cannot_recurse(tmp_path):
    box, journal, _ = compose(tmp_path)
    six(box)
    report, = [m for m in box.messages() if m.origin.startswith("storm-breaker/")]
    for n in range(6, 12):
        box.enqueue(**ARRIVAL, occurrence_id=f"arrival/{n}")
        box.recover()
    assert len(occurrences(journal, SIG)) == 12 and len(trips(journal)) == 1
    own, = occurrences(journal, report.signature)
    tid = trips(journal)[0].body["trip_id"]
    assert own.body == dict(kind="storm_occurrence", signature=report.signature,
        occurrence_id="storm-report/" + tid, emitting_stage=None, emitting_origin="storm-breaker/" + tid)
    assert box.publish_storm_report(trips(journal)[0].body, 999) == (report.id, False)
    assert box.get(report.id) == report and len(occurrences(journal, report.signature)) == 1


@pytest.mark.parametrize("verb", ["run", "drain"])
def test_storm_activation_does_not_hold_dispatch_or_notify(tmp_path, monkeypatch, verb):
    """Drain waits for the emitting ticket's resume; explicit run remains operator-invoked."""
    root = make_root(tmp_path)
    commit_ticket(root, "one", confirmed())
    commit_ticket(root, "unrelated", confirmed())
    script = Script()
    clock = FakeClock(T0)
    journal = Journal(root / ".tempo/state", clock)
    box = storm.storm_producer(root=root / ".tempo/state/box", fs=LocalFileSystem(),
                                journal=journal, clock=clock)
    six(box)
    def forbidden(*args, **kwargs):
        pytest.fail("storm invoked external notification")
    from tempo.effects import Effects
    effect = Effects.run
    async def allowed(self, action, **kwargs):
        if kwargs['key'].startswith('notify/'):
            forbidden()
        return await effect(self, action, **kwargs)
    monkeypatch.setattr("tempo.effects.Effects.run", allowed)
    monkeypatch.setattr("tempo.triage.triage_pass", forbidden)
    if verb == "drain":
        original = cli.build_control
        def build(checkout):
            consumer = original(checkout)
            async def sleep(_):
                # Only the emitting ticket remains; the lifecycle and lock are live.
                assert script.calls == ["unrelated"]
                hold, = consumer.storm_holds()
                assert json.loads((checkout.config.state_dir / "control/active.json").read_bytes())["hold_id"] == hold
                await cli._control(checkout, "resume")
            consumer.sleep = sleep
            return consumer
        monkeypatch.setattr(cli, "build_control", build)
    assert cli.main([verb, "one"] if verb == "run" else [verb], cwd=root, env=ENV,
                    clock=clock, pipeline=script) == 0
    assert script.calls == (["one"] if verb == "run" else ["unrelated", "one"])
    assert script.lock_held == [True] * len(script.calls)
    assert len(trips(journal)) == 1
    if verb == "drain":
        assert StormLedger(journal=journal, clock=clock).holds() == {}
        decisions = [e for e in journal.read() if e.body.get("kind") == "control_decision"]
        assert [e.body["decision"] for e in decisions] == ["accepted"]
    assert all(m.status == "pending" for m in box.messages())


SITES = ("implement", "fallback-raises", "fallback-absent", "dependency", "base", "flake")


async def exercise_site(ctx, monkeypatch, site, attempt=2):
    clock = ctx.driver.clock
    journal = ctx.driver.journal
    box = storm.storm_producer(root=ctx.config.state_dir / "box", fs=ctx.fs, journal=journal, clock=clock)
    async def ticket_fixture(**kwargs):
        path = ctx.repo / f"tickets/{STEM}/ticket.md"
        if not path.exists():
            return await ready(ctx, **kwargs)
        ticket = validate_ticket(STEM, path.read_text(), ctx.repo)
        return replace(ticket, verification=kwargs.get("verification", ticket.verification))

    parts = []
    if site == "implement":
        ticket = await ticket_fixture()
        def reply(request):
            raw = json.loads(agent({"tempo/thing.py": "ok\n"})(request).removeprefix("```json\n").removesuffix("\n```"))
            raw["second_problems"] = [{"summary": "same issue 1"}, {"summary": "same issue 2"}]
            return json.dumps(raw)
        ctx.driver.llm = FakeLLM([reply])
        result = await stages.implement(ctx, ticket, attempt=attempt)
        assert result.outcome == "ok"
        parts = [["second-problem", STEM, attempt, n] for n in range(2)]
        stage, origin = "implement", STEM
    elif site.startswith("fallback"):
        ticket = await ticket_fixture()
        if site == "fallback-absent":
            if ctx.worktree(STEM).exists():
                await ctx.git.worktree_remove(ctx.repo, ctx.worktree(STEM))
        else:
            async def failed(*args, **kwargs):
                raise OSError("harvest failed")
            monkeypatch.setattr(runner, "harvest", failed)
        finding = Finding(code="logic", message="Rework refused", paved_road="shrink and retry")
        result = await runner.failure_terminal(ctx, ticket, outcome="gate_failed", stage="review", findings=[],
            attempt=attempt, reworked=StageResult(outcome="gate_failed", artifact=None, findings=[finding]),
            rework_requested=True)
        assert result == "gate_failed"
        assert journal.read()[-1].body["to"] == "gate_failed"
        parts = [["rework-failure", STEM, attempt, "review", "gate_failed"]]
        stage, origin = "review", STEM
    elif site == "dependency":
        await ticket_fixture()
        path = ctx.repo / f"tickets/{STEM}/ticket.md"
        if "## Depends on\nnone" in path.read_text():
            ctx.fs.write(ctx.repo / "tickets/dead/ticket.md", path.read_bytes())
            ctx.fs.write(path, path.read_text().replace("## Depends on\nnone", "## Depends on\n- dead").encode())
            await ctx.git.add(ctx.repo, [f"tickets/{STEM}/ticket.md", "tickets/dead/ticket.md"])
            await ctx.git.commit(ctx.repo, "dependency")
            journal.append(EventType.STATE_TRANSITION, {"to": "rejected"}, ticket="dead")
        checkout = Checkout(ctx.repo, ctx.config, ctx.env, ctx.exec_, ctx.git, journal, ctx.fs, clock)
        await backlog.dead_dependents("dead", checkout)
        before = journal.read()
        await backlog.dead_dependents("dead", checkout)
        assert journal.read() == before
        parts = [["dead-dependency", STEM, "dead"]]
        stage, origin = "depends", STEM
    elif site == "base":
        ticket = await ticket_fixture(verification=(("host", "bad-a"), ("host", "bad-b"), ("host", "new-red")))
        scripted(ctx, monkeypatch, [(1, "failed", "")] * 5 + [(0, "pass", "")])
        evidence = await stages.gather_evidence(ctx, ticket, "ok", attempt=attempt, stage="review")
        assert [r.base_red for r in evidence.verification] == [True, True, False]
        assert not (ctx.config.worktree_root / ".base" / STEM).exists()
        before = journal.read()
        scripted(ctx, monkeypatch, [(1, "failed", "")] * 5 + [(0, "pass", "")])
        await stages.gather_evidence(ctx, ticket, "ok", attempt=attempt, stage="review")
        assert journal.read() == before
        parts = [["base-red", STEM, attempt, "review", n] for n in (1, 2)]
        stage, origin = "review", STEM
    else:
        flake = daemon.flake_detection(journal=journal, box=box, config=ctx.config,
                                      escalate=lambda _: pytest.fail("unexpected cap"))
        test_id = "tests/test_é.py::test_12"
        evidence = RerunEvidence(test_id, "fail", "pass", True, True, True)
        with pytest.raises(BoxError, match="occurrence_id"):
            flake.detect(test_id=test_id, evidence=evidence, summary="flaky", reason="failure")
        parts = [["flake-detection", STEM, attempt, test_id, n] for n in range(2)]
        for part in parts:
            projection = flake.detect(test_id=test_id, evidence=evidence, summary="flaky", reason="failure",
                                      occurrence_id=identity(part))
        assert projection.tests == {test_id} and len(projection.detected) == 1
        before = journal.read()
        for box_id in projection.detected:
            assert flake.release(box_id=box_id) == projection
        assert journal.read() == before
        stage, origin = "check", test_id
    return box, parts, stage, origin


@pytest.mark.parametrize("site", SITES)
def test_production_arrival_site_closure(admission_context, monkeypatch, site):
    ctx = admission_context
    hook = Mock(wraps=storm.storm_producer)
    monkeypatch.setattr(storm, "storm_producer", hook)
    box, parts, stage, origin = asyncio.run(exercise_site(ctx, monkeypatch, site))
    assert hook.call_count >= (1 if site == "flake" else 2)
    incoming = occurrences(ctx.driver.journal)
    assert len(incoming) == len(parts)
    assert all(e.body["emitting_stage"] == stage and e.body["emitting_origin"] == origin for e in incoming)
    assert all(call.kwargs["journal"] is ctx.driver.journal and call.kwargs["fs"] is ctx.fs
               and call.kwargs["clock"] is ctx.driver.clock for call in hook.call_args_list)


@pytest.mark.parametrize("site", SITES)
def test_production_occurrence_id_recipes(admission_context, monkeypatch, site):
    ctx = admission_context
    box, parts, stage, origin = asyncio.run(exercise_site(ctx, monkeypatch, site))
    incoming = occurrences(ctx.driver.journal)
    assert [e.body["occurrence_id"] for e in incoming] == [identity(part) for part in parts]
    # Replay each exact caller arrival after rotation, restart and expiry.
    monkeypatch.setattr(journal_module, "ROLL_BYTES", 1)
    fake = FakeClock(ctx.driver.clock() + timedelta(hours=2))
    restarted = Journal(ctx.config.state_dir, fake)
    replay = storm.storm_producer(root=box.root, fs=box.fs, journal=restarted, clock=fake)
    for event in incoming:
        message = next(m for m in box.messages() if m.signature == event.body["signature"])
        fields = dict(message_class=message.message_class, origin=origin, stage=stage, outcome=message.outcome,
                      summary=message.summary, reason=("failure" if site == "flake" else None))
        assert replay.enqueue(**fields, occurrence_id=event.body["occurrence_id"]) == (message.id, False)
    assert occurrences(restarted) == incoming
    # Distinct runs keep the integer attempt in the recipe, even on the same signature.
    if site in {"implement", "flake"}:
        message = box.messages()[0]
        fresh = list(parts[0])
        fresh[2] = 3
        replay.enqueue(message_class=message.message_class, origin=origin, stage=stage, outcome=message.outcome,
                       summary=message.summary, reason="failure" if site == "flake" else None,
                       occurrence_id=identity(fresh))
        assert len(occurrences(restarted)) == len(parts) + 1


@pytest.mark.parametrize("site", SITES)
def test_production_arrival_preserves_caller_behavior(admission_context, monkeypatch, site):
    ctx = admission_context
    box, parts, stage, origin = asyncio.run(exercise_site(ctx, monkeypatch, site))
    assert all(m.status == "pending" and m.verdict is m.resolution is None for m in box.messages())
    assert len(occurrences(ctx.driver.journal)) == len(parts)
    assert not trips(ctx.driver.journal)
    if site.startswith("fallback"):
        report, = box.messages()
        assert report.summary == "Rework refused; shrink and retry"
        assert (report.message_class, report.origin, report.stage, report.outcome) == (
            "failure_report", STEM, "review", "gate_failed")
    if site in {"implement", "flake"}:
        assert len(box.messages()) == 1  # distinct arrivals preserve the existing signature dedup


def test_successful_rework_harvest_is_not_an_arrival(admission_context):
    ctx = admission_context
    async def exercise():
        ticket = await ready(ctx)
        finding = Finding(code="logic", message="refused", paved_road="shrink and retry")
        result = await runner.failure_terminal(ctx, ticket, outcome="gate_failed", stage="review", findings=[],
            attempt=2, reworked=StageResult(outcome="gate_failed", artifact=None, findings=[finding]),
            rework_requested=True)
        assert result == "gate_failed"
    asyncio.run(exercise())
    assert occurrences(ctx.driver.journal) == []
    assert Box(ctx.config.state_dir / "box", ctx.fs).messages() == []
    assert (ctx.repo / f"tickets/{STEM}/attempts/2/harvest.json").is_file()


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("point", ["occurrence", "queue"])
@pytest.mark.parametrize("after", [False, True])
def test_producer_recipes_replay_interrupted_delivery(admission_context, monkeypatch, site, point, after):
    ctx = admission_context
    journal, fs = ctx.driver.journal, ctx.fs
    append, write = journal.append, fs.write
    class Crash(BaseException):
        pass
    def failed_append(type, body, **kwargs):
        if body.get("kind") == "storm_occurrence":
            if after:
                append(type, body, **kwargs)
            raise Crash()
        return append(type, body, **kwargs)
    def failed_write(path, data):
        if path.parent == ctx.config.state_dir / "box":
            if after:
                write(path, data)
            raise Crash()
        return write(path, data)
    with monkeypatch.context() as patch:
        patch.setattr(journal if point == "occurrence" else fs,
                      "append" if point == "occurrence" else "write",
                      failed_append if point == "occurrence" else failed_write)
        with pytest.raises(Crash):
            asyncio.run(exercise_site(ctx, patch, site))
    first = occurrences(journal)
    box, parts, stage, origin = asyncio.run(exercise_site(ctx, monkeypatch, site))
    incoming = occurrences(journal)
    assert [e.body["occurrence_id"] for e in incoming] == [identity(part) for part in parts]
    assert incoming[:len(first)] == first
    assert len(box.messages()) == (2 if site == "base" else 1)


@pytest.mark.parametrize("site", ["implement", "flake"])
def test_fresh_producing_runs_have_distinct_arrival_ids(admission_context, monkeypatch, site):
    ctx = admission_context
    box, first, _, _ = asyncio.run(exercise_site(ctx, monkeypatch, site, attempt=2))
    box, second, _, _ = asyncio.run(exercise_site(ctx, monkeypatch, site, attempt=3))
    assert len(box.messages()) == 1
    assert [e.body["occurrence_id"] for e in occurrences(ctx.driver.journal)] == [
        identity(part) for part in first + second]


@pytest.mark.asyncio
async def test_storm_leaves_daemon_selection_and_accounting_unchanged(admission_context, monkeypatch):
    ctx = admission_context
    one = await ready(ctx, "one")
    other = await ready(ctx, "unrelated")
    calls = []
    async def prepare(checkout):
        async def dispatch(ticket):
            calls.append(ticket.stem)
            checkout.journal.append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket=ticket.stem)
            return "merged"
        return dispatch
    checkout = Checkout(ctx.repo, ctx.config, ctx.env, ctx.exec_, ctx.git, ctx.driver.journal,
                               ctx.fs, ctx.driver.clock, ctx.driver.sleep)
    def forbidden(*args, **kwargs):
        pytest.fail("storm invoked an external notification")
    monkeypatch.setattr("tempo.effects.Effects.run", forbidden)
    core = cli.build_daemon_core(checkout, read=lambda _: None, debounce=0,
        quarantined=lambda: set(), drought_parked=lambda: set(), completed_unmerged=lambda: 0,
        prepare=prepare)
    box = core.control._recover.__self__
    six(box)
    core.scheduler.update(one)
    core.scheduler.update(other)
    assert await core.scheduler.dispatch_next() == one
    assert await core.scheduler.dispatch_next() == other
    assert await core.scheduler.dispatch_next() is None
    assert calls == ["one", "unrelated"]
    assert core.control.admission is None and core.control.projection.pause_id is None
    hold, = core.control.storm_holds()
    assert core.control.storm_holds()[hold] == "one"
    # This activation publishes identities; continuous stage selection lands with serve.
    from tempo.control import ControlRequest, publish_request
    publish_request(ctx.config.state_dir,
                    ControlRequest("daemon-release", core.control.inbox.lifecycle_id, "resume", hold), ctx.fs)
    await core.control.checkpoint()
    assert core.control.storm_holds() == {}
    assert hold in core.control.projection.released_hold_ids
    assert not any(e.type == EventType.CAP_CONSUMED for e in ctx.driver.journal.read())
    assert len(trips(ctx.driver.journal)) == 1


@pytest.mark.parametrize("startup", ["run", "drain", "daemon"])
def test_production_startup_recovers_without_new_arrival(admission_context, startup):
    ctx = admission_context
    clock = FakeClock(T0)
    journal = Journal(ctx.config.state_dir, clock)
    ledger = StormLedger(journal=journal, clock=clock)
    for n in range(6):
        ledger.record(signature=SIG, occurrence_id=f"arrival/{n}",
                      emitting_stage="implement", emitting_origin="one")
    clock.advance(7200)
    if startup == "daemon":
        journal = Journal(ctx.config.state_dir, clock)
        checkout = Checkout(ctx.repo, ctx.config, ctx.env, ctx.exec_, ctx.git, journal, ctx.fs, clock)
        core = cli.build_daemon_core(checkout, read=lambda _: None, debounce=0,
            quarantined=lambda: set(), drought_parked=lambda: set(), completed_unmerged=lambda: 0)
        before = snapshot(ctx.config.state_dir)
        assert len(trips(journal)) == 0
        assert snapshot(ctx.config.state_dir) == before
        asyncio.run(core.startup())
    else:
        if startup == "run":
            asyncio.run(ready(ctx, "one"))
        assert cli.main([startup, "one"] if startup == "run" else [startup], cwd=ctx.repo, env=ctx.env,
                        clock=clock, pipeline=Script()) == 0
        journal = Journal(ctx.config.state_dir, clock)
    assert len(trips(journal)) == 1
    reports = Box(ctx.config.state_dir / "box", ctx.fs).messages()
    assert len(reports) == 1 and reports[0].origin.startswith("storm-breaker/")
    assert len(occurrences(journal, SIG)) == 6


def test_future_occurrences_do_not_trip_before_their_time(tmp_path):
    box, journal, clock = compose(tmp_path)
    ledger = StormLedger(journal=journal, clock=clock)
    clock.advance(1)
    for n in range(6):
        ledger.record(signature=SIG, occurrence_id=str(n), emitting_stage=None, emitting_origin=None)
    clock.now = T0
    box.recover()
    assert trips(journal) == [] and box.messages() == []
    clock.advance(1)
    box.recover()
    assert len(trips(journal)) == len(box.messages()) == 1
