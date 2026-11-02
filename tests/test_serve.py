"""Drive the CLI's continuous graph with disposable repositories and injected time."""

import asyncio
import json
import signal
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tempo import __main__ as cli, daemon, runner, serve, stages, triage
from tempo.artifacts import StageResult
from tempo.audit import audit_journal
from tempo.control import ControlRequest, publish_request
from tempo.daemon import TicketWriter
from tempo.driver import LlmStage
from tempo.journal import EventType, run_seq
from tempo.llm import FakeLLM, HANG
from tempo.lockfile import Lockfile, LockHeld
from tempo.merge import Admission
from tempo.mergequeue import CONFLICT_FACTS, RED_STREAK, TREE_MISMATCH, MergeQueue, TreeMismatch
from tempo.seams import LocalFileSystem, SubprocessExec
from tempo.stages import Invoice, PackingSlip
from tests.test_cli import ENV, root, write
from tests.test_daemon_composition import sources
from tests.test_restart_timers import orphan, repository
from tests.test_scheduler import import_closure, text, turn
from tests.test_triage import reply
from tempo.checkout import Refusal


async def until(rig, predicate):
    for n in range(20000):
        if predicate():
            return
        if n % 20 == 0:
            rig.time.advance(serve.SERVE_POLL_S)
        await turn()
    raise AssertionError("production graph did not reach the scripted boundary")


async def graph(root, monkeypatch, **kwargs):
    rig = await repository(root, monkeypatch)
    rig.fs.publish = LocalFileSystem().publish
    rig.exec.reply = reply("decision")
    owner = serve.Serve(rig.checkout, read=lambda stem: (root / f"tickets/{stem}/ticket.md").read_text()
            if (root / f"tickets/{stem}/ticket.md").is_file() else None,
        stems=lambda: (p.parent.name for p in (root / "tickets").glob("*/ticket.md")),
        **{"prepare": rig.prepare, "llm": rig.llm, **kwargs})
    rig.owner = owner
    return rig


def script(monkeypatch, *, worktree=False, before=None, terminal="already_satisfied"):
    calls, contexts, artifacts = [], {}, {}
    async def implement(ctx, ticket, *, attempt):
        calls.append((ticket.stem, "implement", attempt))
        contexts[ticket.stem] = ctx
        if worktree:
            await stages.prepare_worktree(ctx, ticket.stem)
        if before is not None:
            await before(ctx, ticket, "implement", attempt)
        artifact = PackingSlip(produced_by_spec_version=1, produced_at_sha="fixture",
            stem=ticket.stem, branch=ticket.stem, outcome="ok", summary="scripted",
            run_record=f"tickets/{ticket.stem}/run.md")
        artifacts[ticket.stem] = artifact
        return StageResult(outcome="ok", artifact=artifact, findings=[])
    async def check(ctx, ticket, slip, *, attempt):
        assert slip is artifacts[ticket.stem] and ctx is contexts[ticket.stem]
        calls.append((ticket.stem, "check", attempt))
        if before is not None:
            await before(ctx, ticket, "check", attempt)
        invoice = Invoice(produced_by_spec_version=1, produced_at_sha="fixture", stem=ticket.stem,
            passed=True, changed_files=[], inserted_lines=0, bypassed=[], reports=[])
        artifacts[ticket.stem] = invoice
        return StageResult(outcome=terminal, artifact=invoice, findings=[])
    async def review(ctx, ticket, invoice, *, attempt):
        assert invoice is artifacts[ticket.stem]
        calls.append((ticket.stem, "review", attempt))
        if before is not None:
            await before(ctx, ticket, "review", attempt)
        return StageResult(outcome="timeout", artifact=None, findings=[])
    async def diagnosis(*args, **kwargs):
        return SimpleNamespace(verdict="abandon-human", lessons=[], mechanical=None)
    monkeypatch.setattr(stages, "implement", implement)
    monkeypatch.setattr(stages, "check", check)
    monkeypatch.setattr(stages, "review", review)
    monkeypatch.setattr(runner, "diagnose", diagnosis)
    return calls, contexts, artifacts


def terminals(rig, stem=None):
    return [e for e in rig.journal.read() if e.type == EventType.STATE_TRANSITION
            and e.body.get("to") != "running" and (stem is None or e.ticket == stem)]


def request(rig, verb, identity, hold=None, lifecycle=None):
    owner = rig.owner
    publish_request(rig.checkout.config.state_dir,
        ControlRequest(identity, lifecycle or owner.control.projection.lifecycle_id, verb, hold), rig.fs)


def trip(rig, stem="work", stage="check", signature="a"):
    for index in range(6):
        rig.owner.box.enqueue(message_class="failure_report", origin=stem, stage=stage,
            outcome="gate_failed", summary=signature, occurrence_id=f"{signature}/{index}")
    return [identity for identity, target in rig.owner.storm.held_stages().items()
            if target == {"emitting_origin": stem, "emitting_stage": stage}][-1]


async def finish(rig, run):
    rig.owner.stop()
    await until(rig, run.done)
    assert await run == 0
    assert rig.owner.tasks.tasks == () and all(task.done() for task in rig.owner.workers)
    assert json.loads((rig.checkout.config.state_dir / "control/active.json").read_text()) is None
    assert audit_journal(rig.journal) == []


async def activated_graph(root, monkeypatch):
    rig = await graph(root, monkeypatch)
    run = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, lambda: (rig.checkout.config.state_dir / "heartbeat").exists())
    finally:
        await finish(rig, run)
    return rig


@pytest.mark.asyncio
async def test_serve_waits_at_quiescence_and_discovers_work(root, monkeypatch):
    calls, _, _ = script(monkeypatch)
    rig = await graph(root, monkeypatch)
    run = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, lambda: rig.owner.ready.is_set())
        assert not run.done() and calls == []
        write(root, "initial", text())
        await until(rig, lambda: len(terminals(rig, "initial")) == 1)
        assert not run.done()
        write(root, "later", text(depends="- initial"))
        await until(rig, lambda: len(terminals(rig, "later")) == 1)
        assert [(stem, stage) for stem, stage, _ in calls] == [
            ("initial", "implement"), ("initial", "check"), ("later", "implement"), ("later", "check")]
    finally:
        await finish(rig, run)


@pytest.mark.asyncio
async def test_empty_box_report_intake(root, monkeypatch):
    from tests.test_inbox import arrival
    rig = await graph(root, monkeypatch)
    config = root / "config.yaml"
    config.write_text(config.read_text() + "report_inbox: inbox\n")
    assert rig.owner.box.messages() == []
    run = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, rig.owner.ready.is_set)
        arrival(root)
        await until(rig, lambda: rig.owner.box.messages()
                    and rig.owner.box.messages()[0].status == "resolved")
        message, = rig.owner.box.messages()
        assert message.report and message.has_repro
        assert message.resolution.kind == "decision"
        assert len([e for e in rig.journal.read() if e.body.get("signal") == "triage_pass"]) == 1
    finally:
        await finish(rig, run)


@pytest.mark.asyncio
async def test_report_consumer_is_sequential(root, monkeypatch):
    from tests.test_inbox import arrival, raw_report
    from tempo.driver import Driver
    rig = await graph(root, monkeypatch)
    config = root / "config.yaml"
    config.write_text(config.read_text() + "report_inbox: inbox\n")
    arrival(root)
    (root / "inbox/b.json").write_text(json.dumps(raw_report(summary="Another material defect")))
    entered, release = asyncio.Event(), asyncio.Event()
    active = maximum = calls = 0
    driving = Driver.run
    async def observed(self, stage, consumed, **kwargs):
        nonlocal active, maximum, calls
        if stage.surface != "triage":
            return await driving(self, stage, consumed, **kwargs)
        assert rig.owner.core.admission._slot.locked()
        active += 1
        maximum = max(maximum, active)
        calls += 1
        entered.set()
        try:
            await release.wait()
            return await driving(self, stage, consumed, **kwargs)
        finally:
            active -= 1
    monkeypatch.setattr(Driver, "run", observed)
    run = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, entered.is_set)
        for _ in range(100):
            await turn()
        assert maximum == calls == 1 and len(rig.owner.workers) == 4
        assert rig.owner.tasks._consumers and len(rig.owner.tasks._consumers) == 3
        release.set()
        await until(rig, lambda: len(rig.owner.box.messages()) == 2 and rig.owner.box.pending() == [])
        assert calls == 2 and maximum == 1
    finally:
        release.set()
        await finish(rig, run)


def test_serve_cli_uses_production_composition(root, monkeypatch):
    built = []
    original = serve.Serve.run
    async def run(self):
        assert self.workers == () and self.tasks.tasks == ()
        assert self.checkout.journal.read() == []
        assert self.core.restart.timers.journal is self.checkout.journal
        assert self.control is self.core.control is self.checkout.control
        assert self.control.inbox.journal is self.checkout.journal
        command = self.notifications.compose(self.checkout.config)
        assert command.exec_ is not self.checkout.exec_
        built.append(self)
        return await original(self)
    monkeypatch.setattr(serve.Serve, "run", run)
    monkeypatch.setattr(cli, "_serve_signals", lambda stop: (stop(), lambda: None)[1])
    assert cli.main(["serve"], cwd=root, env=ENV) == 2
    assert cli.main(["serve"], cwd=root, env=ENV, llm=FakeLLM([])) == 0
    assert len(built) == 1 and len(built[0].workers) == 4
    assert "tempo.serve" in import_closure(sources())


@pytest.mark.asyncio
async def test_serve_holds_lock_through_cleanup(root, monkeypatch):
    rig = await graph(root, monkeypatch)
    owner = rig.owner
    entering, release = asyncio.Event(), asyncio.Event()
    async def abort():
        entering.set()
        await release.wait()
    monkeypatch.setattr(owner, "abort", abort)
    contender = Lockfile(rig.checkout.config.state_dir, instance_id="contender", clock=rig.time)
    run = asyncio.create_task(owner.run())
    await until(rig, owner.ready.is_set)
    with pytest.raises(LockHeld):
        contender.acquire()
    other = serve.Serve(rig.checkout, read=owner.read, stems=owner.stems, prepare=rig.prepare, llm=rig.llm)
    try:
        with pytest.raises(LockHeld):
            await other.run()
    except BaseException:
        release.set()
        owner.stop()
        await until(rig, run.done)
        raise
    request(rig, "kill", "stop")
    await until(rig, entering.is_set)
    for _ in range(3):
        run.cancel()
        await turn()
        assert not run.done() and all(not t.done() for t in owner.workers)
        with pytest.raises(LockHeld):
            contender.acquire()
        assert json.loads((rig.checkout.config.state_dir / "control/active.json").read_text()) is not None
    release.set()
    await finish(rig, run)
    contender.acquire()
    contender.release()


@pytest.mark.asyncio
async def test_serve_startup_precedes_all_work(root, monkeypatch):
    rig = await graph(root, monkeypatch)
    dead = await orphan(rig, "dead")
    rig.owner.core.restart.timers.arm("overdue", rig.time() - timedelta(seconds=1), ticket=None)
    calls, _, _ = script(monkeypatch)
    write(root, "work", text())
    original = stages.harvest_orphan
    harvested, release = asyncio.Event(), asyncio.Event()
    async def harvest(checkout, stem, attempt):
        assert checkout.journal is rig.journal
        harvested.set()
        await release.wait()
        await original(checkout, stem, attempt)
    monkeypatch.setattr(stages, "harvest_orphan", harvest)
    run = asyncio.create_task(rig.owner.run())
    await until(rig, harvested.is_set)
    assert calls == [] and not rig.owner.ready.is_set()
    assert not (rig.checkout.config.state_dir / "control/active.json").exists()
    release.set()
    try:
        await until(rig, lambda: terminals(rig, "work"))
        assert not dead.exists()
        history = rig.journal.read()
        abandoned = next(i for i, e in enumerate(history) if e.body.get("to") == "abandoned")
        fired = next(i for i, e in enumerate(history) if e.type == EventType.TIMER_FIRED)
        intake = next(i for i, e in enumerate(history) if e.body.get("signal") == "ticket_intake")
        assert abandoned < fired < intake
        assert any(e.ticket == "dead" and e.type == EventType.EFFECT_COMPLETION for e in history)
    finally:
        await finish(rig, run)


@pytest.mark.asyncio
async def test_serve_recovery_refusal_starts_no_work(root, monkeypatch):
    rig = await graph(root, monkeypatch)
    rig.journal.append(EventType.TIMER_FIRED, {"timer_id": "unpaired", "deadline": rig.time().isoformat()})
    write(root, "work", text())
    with pytest.raises(Refusal, match="startup recovery refused"):
        await rig.owner.run()
    assert rig.owner.workers == () and not rig.owner.core.restart.ready
    assert not any(e.body.get("signal") == "ticket_intake" for e in rig.journal.read())


@pytest.mark.asyncio
async def test_serve_control_precedes_offer_accounting(root, monkeypatch):
    calls, _, _ = script(monkeypatch)
    rig = await graph(root, monkeypatch)
    request(rig, "pause", "pause")
    request(rig, "kill", "old", lifecycle="retired")
    request(rig, "resume", "wrong", hold="other")
    write(root, "work", text())
    run = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, lambda: rig.owner.control.projection.pause_id == "pause"
                    and "work" in rig.owner.core.scheduler.pending)
        assert calls == [] and not any(e.body.get("to") == "running" for e in rig.journal.read())
        assert not any(e.type == EventType.CAP_CONSUMED for e in rig.journal.read())
        request(rig, "resume", "resume", hold="pause")
        await until(rig, lambda: terminals(rig, "work"))
        request(rig, "kill", "stop")
        await until(rig, run.done)
        assert await run == 0
        decisions = {e.body["request_id"]: e.body["decision"] for e in rig.journal.read()
                     if e.body.get("kind") == "control_decision"}
        assert decisions == {"old": "stale", "pause": "accepted", "wrong": "stale",
                             "resume": "accepted", "stop": "accepted"}
    finally:
        await finish(rig, run)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["exception", "return", "cancel", "kill", "signal"])
async def test_serve_worker_failure_and_kill_suppression(root, monkeypatch, ending):
    failures = []
    rig = await graph(root, monkeypatch, failure=failures.append)
    entered, release = asyncio.Event(), asyncio.Event()
    error = ValueError("scripted consumer failure")
    cancellation = asyncio.CancelledError("scripted consumer cancellation")
    async def bad():
        entered.set()
        await release.wait()
        if ending == "exception":
            raise error
        if ending == "cancel":
            raise cancellation
    rig.owner.tasks._consumers = (bad, rig.owner.merge, rig.owner.triage)
    run = asyncio.create_task(rig.owner.run())
    await until(rig, entered.is_set)
    if ending == "kill":
        request(rig, "kill", "stop")
    elif ending == "signal":
        rig.owner.stop()
    else:
        release.set()
    await until(rig, run.done)
    assert await run == (0 if ending in {"kill", "signal"} else 1)
    if ending in {"kill", "signal"}:
        assert failures == []
    else:
        assert len(failures) == 1
        if ending == "exception":
            assert failures[0] is error
        elif ending == "cancel":
            assert failures[0] is cancellation
        else:
            assert isinstance(failures[0], RuntimeError)
            assert str(failures[0]) == "owned serve consumer returned unexpectedly"
            assert failures[0] is rig.owner.workers[1].exception()
    assert all(t.done() for t in rig.owner.workers)
    assert all(t.cancelled() or not t._log_traceback for t in rig.owner.workers)
    applied = [e for e in rig.journal.read() if e.body.get("kind") == "kill_applied"]
    assert len(applied) == (1 if ending == "kill" else 0)
    assert audit_journal(rig.journal) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["implement", "check", "review"])
async def test_serve_storm_suspends_stage_and_frees_selection(root, monkeypatch, stage):
    calls, contexts, artifacts = script(monkeypatch, terminal="ok" if stage == "review" else "already_satisfied")
    rig = await graph(root, monkeypatch)
    hold = trip(rig, stage=stage)
    write(root, "work", text(priority="P0"))
    write(root, "other", text())
    run = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, lambda: terminals(rig, "other"))
        await until(rig, lambda: rig.owner.core.admission.active is None
                    and rig.owner.core.scheduler.active is None)
        assert not terminals(rig, "work")
        assert not rig.owner.core.admission._slot.locked() or rig.owner.core.admission.active is None
        assert rig.owner.core.scheduler.active is None
        expected = [] if stage == "implement" else ["implement"] if stage == "check" else ["implement", "check"]
        assert [s for stem, s, _ in calls if stem == "work"] == expected
        prior = rig.owner.resume(SimpleNamespace(stem="work"))
        if stage == "implement":
            assert prior is None and not any(e.ticket == "work" and e.body.get("to") == "running"
                                             for e in rig.journal.read())
        else:
            assert isinstance(prior, TicketWriter)
            retained = prior.continuations["work"]
            assert retained.ctx is contexts["work"] and retained.next_stage == stage
            assert retained.run.results[expected[-1]].artifact is artifacts["work"]
        request(rig, "resume", "release", hold=hold)
        await until(rig, lambda: terminals(rig, "work"))
        assert [s for stem, s, _ in calls if stem == "work"] == expected + (
            ["implement", "check"] if stage == "implement" else [stage])
        assert sum(e.ticket == "work" and e.body.get("to") == "running" for e in rig.journal.read()) == 1
        assert len([e for e in rig.journal.read() if e.body.get("kind") == "storm_breaker_trip"]) == 1
    finally:
        await finish(rig, run)


@pytest.mark.asyncio
async def test_serve_storm_hold_and_resume(root, monkeypatch):
    await test_serve_storm_suspends_stage_and_frees_selection(root, monkeypatch, "check")


class HeldLLM(FakeLLM):
    def __init__(self):
        super().__init__([HANG])
        self.entered, self.cleaning, self.release = (asyncio.Event() for _ in range(3))
    async def call(self, request):
        self.entered.set()
        try:
            return await super().call(request)
        finally:
            self.cleaning.set()
            await self.release.wait()


async def active_graph(root, monkeypatch, *, signals=None, failure=None):
    llm = HeldLLM()
    async def prepare(local):
        return runner.bind(local, llm)
    rig = await graph(root, monkeypatch, prepare=prepare, signals=signals, failure=failure)
    async def implementing(ctx, ticket, *, attempt):
        await stages.prepare_worktree(ctx, ticket.stem)
        return await ctx.driver.run(LlmStage(surface="implement", emits=PackingSlip, gates=[],
            render=lambda *_: "scripted implement"), ticket, ticket=ticket.stem,
            attempt=attempt, workspace=ctx.worktree(ticket.stem), tier="medium", effort="medium",
            stuck_budget=1200)
    monkeypatch.setattr(stages, "implement", implementing)
    write(root, "work", text())
    run = asyncio.create_task(rig.owner.run())
    await until(rig, llm.entered.is_set)
    return rig, llm, run


@pytest.mark.asyncio
async def test_serve_kill_unwinds_executor_before_workers(root, monkeypatch):
    rig, llm, run = await active_graph(root, monkeypatch)
    owner = rig.owner
    failures, order = [], []
    owner.observer.failure = failures.append
    for task in owner.workers:
        task.add_done_callback(lambda _: order.append("worker"))
    request(rig, "pause", "pause")
    await until(rig, lambda: owner.control.projection.pause_id == "pause")
    request(rig, "kill", "stop")
    await until(rig, llm.cleaning.is_set)
    assert order == [] and llm.aborted == 1
    assert any(e.body.get("request_id") == "stop" and e.body.get("decision") == "accepted"
               for e in rig.journal.read())
    assert not any(e.body.get("kind") == "kill_applied" for e in rig.journal.read())
    for _ in range(3):
        run.cancel()
        await turn()
        assert not run.done() and all(not t.done() for t in owner.workers)
    request(rig, "kill", "stop-again")
    llm.release.set()
    await finish(rig, run)
    assert order == ["worker"] * 4 and failures == []
    assert {e.body["request_id"] for e in rig.journal.read() if e.body.get("kind") == "kill_applied"} == {
        "stop", "stop-again"}
    assert not terminals(rig, "work")
    assert rig.checkout.config.worktree_root.joinpath("work").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["SIGINT", "SIGTERM", "cancel"])
async def test_serve_signal_stop_and_restart_recovery(root, monkeypatch, mode):
    delivered, removed = {}, []
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, handler: delivered.__setitem__(sig, handler))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: removed.append(sig))
    failures = []
    rig, llm, run = await active_graph(root, monkeypatch, signals=cli._serve_signals,
                                       failure=failures.append)
    if mode == "cancel":
        run.cancel()
    else:
        delivered[getattr(signal, mode)]()
    await until(rig, llm.cleaning.is_set)
    assert not run.done() and all(not t.done() for t in rig.owner.workers)
    llm.release.set()
    await finish(rig, run)
    assert failures == []
    assert set(removed) == {signal.SIGINT, signal.SIGTERM}
    assert not any(e.body.get("kind") in {"control_decision", "kill_applied"} for e in rig.journal.read())
    calls, _, _ = script(monkeypatch)
    old = rig.owner
    rig.owner = serve.Serve(rig.checkout, read=old.read, stems=old.stems, prepare=rig.prepare, llm=rig.llm)
    restarted = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, lambda: len(terminals(rig, "work")) == 2)
        assert [e.body["to"] for e in terminals(rig, "work")] == ["abandoned", "already_satisfied"]
        assert calls == [("work", "implement", 1), ("work", "check", 1)]
    finally:
        await finish(rig, restarted)


@pytest.mark.asyncio
async def test_serve_storm_resume_retains_producing_run(root, monkeypatch):
    calls, contexts, artifacts = script(monkeypatch)
    rig = await graph(root, monkeypatch)
    first = trip(rig, signature="a")
    second = trip(rig, signature="b")
    write(root, "work", text(priority="P0"))
    run = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, lambda: "work" in rig.owner.writers
                    and "work" in rig.owner.writers["work"].continuations)
        writer = rig.owner.writers["work"]
        retained = writer.continuations["work"]
        snapshot, artifact = retained.ctx.config, artifacts["work"]
        path = root / "config.yaml"
        path.write_text(path.read_text() + "scheduler: {max_unmerged: 7}\n")
        request(rig, "resume", "first", hold=first)
        await until(rig, lambda: first in rig.owner.control.projection.released_hold_ids)
        assert writer.continuations["work"] is retained and not terminals(rig, "work")
        write(root, "other", text())
        await until(rig, lambda: terminals(rig, "other"))
        assert contexts["other"].config is not snapshot
        request(rig, "resume", "second", hold=second)
        await until(rig, lambda: terminals(rig, "work"))
        assert contexts["work"].config is snapshot and retained.run.attempt == 0
        assert retained.run.results["implement"].artifact is artifact
        assert [stage for stem, stage, _ in calls if stem == "work"] == ["implement", "check"]
        assert len(terminals(rig, "work")) == 1
    finally:
        await finish(rig, run)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["kill", "signal", "cancel"])
async def test_serve_suspended_run_cleanup_and_restart(root, monkeypatch, mode):
    calls, _, _ = script(monkeypatch, worktree=True)
    rig = await graph(root, monkeypatch)
    hold = trip(rig)
    write(root, "work", text())
    run = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, lambda: "work" in rig.owner.writers
                    and "work" in rig.owner.writers["work"].continuations)
        retained = rig.owner.writers["work"].continuations["work"]
        await until(rig, lambda: rig.owner.core.admission.active is None)
        assert await rig.owner.core.sweep_orphans() == []
        assert retained.ctx.worktree("work").exists() and not terminals(rig, "work")
        request(rig, "pause", "pause")
        await until(rig, lambda: rig.owner.control.projection.pause_id == "pause")
        if mode == "kill":
            request(rig, "kill", "stop")
        elif mode == "cancel":
            run.cancel()
        else:
            rig.owner.stop()
    finally:
        await finish(rig, run)
    assert rig.owner.writers["work"].continuations == {}
    assert retained.ctx.worktree("work").exists() and not terminals(rig, "work")
    old = rig.owner
    rig.owner = serve.Serve(rig.checkout, read=old.read, stems=old.stems, prepare=rig.prepare, llm=rig.llm)
    restarted = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, lambda: len(calls) == 2 and calls[-1][-1] == 1)
        assert [e.body["to"] for e in terminals(rig, "work")] == ["abandoned"]
        assert hold in rig.owner.storm.held_stages()
        request(rig, "resume", "release", hold=hold)
        await until(rig, lambda: len(terminals(rig, "work")) == 2)
    finally:
        await finish(rig, restarted)


@pytest.mark.asyncio
async def test_serve_recurring_maintenance_uses_injected_time(root, monkeypatch):
    rig = await graph(root, monkeypatch)
    pushes, original = [], rig.exec.run
    async def execute(argv, **kwargs):
        if argv[3:4] == ["push"]:
            pushes.append(list(argv))
            return (1, "", "scripted push failed") if len(pushes) == 1 else (0, "", "")
        return await original(argv, **kwargs)
    monkeypatch.setattr(rig.exec, "run", execute)
    run = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, lambda: "checkpoint-push/0" in rig.owner.core.restart.timers.pending)
        rig.time.advance(24 * 3600)
        await until(rig, lambda: len(pushes) == 2)
        assert any(e.body.get("kind") == "checkpoint_push_failed" for e in rig.journal.read())
        assert any(e.key == "checkpoint-push/0" and e.type == EventType.EFFECT_COMPLETION
                   and e.body == {"result": {"merged": 0}} for e in rig.journal.read())
        assert len([e for e in rig.journal.read() if e.type == EventType.TIMER_FIRED]) == 1
    finally:
        await finish(rig, run)


@pytest.mark.asyncio
@pytest.mark.parametrize("component", [0, 1, 2, 3, "main"])
async def test_serve_heartbeat_requires_responsive_components(root, monkeypatch, component):
    rig = await graph(root, monkeypatch)
    refreshed = []
    original = rig.fs.write
    def write_fs(path, data):
        if path.name == "heartbeat":
            refreshed.append(rig.time())
        original(path, data)
    monkeypatch.setattr(rig.fs, "write", write_fs)
    run = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, lambda: len(refreshed) >= 2)
        release, entered = asyncio.Event(), asyncio.Event()
        original_wait = None if component == "main" else rig.owner.responses[component].wait
        async def wedge(work=None):
            entered.set()
            try:
                await release.wait()
                if work is not None:
                    return await original_wait(work)
            finally:
                if work is not None and hasattr(work, "close"):
                    work.close()
        if component == "main":
            monkeypatch.setattr(rig.owner, "maintenance", wedge)
        else:
            monkeypatch.setattr(rig.owner.responses[component], "wait", wedge)
        await until(rig, entered.is_set)
        for _ in range(100):
            rig.time.advance(0.1)
            await turn()
        saved = list(refreshed)
        for _ in range(100):
            rig.time.advance(0.1)
            await turn()
        assert refreshed == saved
        release.set()
    finally:
        await finish(rig, run)


@pytest.mark.asyncio
async def test_serve_consumers_use_existing_writers(root, monkeypatch):
    from tempo.driver import Driver
    from tests.test_mergequeue import ready
    calls, contexts, _ = script(monkeypatch)
    rig = await graph(root, monkeypatch)
    original = stages.implement
    async def implementing(ctx, ticket, *, attempt):
        if ticket.stem == "source":
            assert ctx.driver.journal is ctx.driver.effects._journal is rig.journal
            writer = rig.owner.writers["source"]
            assert writer.queue.control is rig.owner.control
            for n in range(5):
                path = f"tempo/queued{n}.py"
                work = await ready(ctx, f"candidate-{n}", changes={path: "ok\n"}, fence=(path,))
                # The existing ready-candidate fixture represents an earlier producing run.
                rig.transition(work.stem, "running")
                writer.queue.offer(work, attempt=0)
        return await original(ctx, ticket, attempt=attempt)
    monkeypatch.setattr(stages, "implement", implementing)
    rig.owner.core.scheduler.drought_parked = lambda: {f"candidate-{n}" for n in range(5)}
    triaged, pushes = [], []
    driving = Driver.run
    async def driver_run(self, stage, consumed, **kwargs):
        if stage.surface == "triage":
            assert self.journal is self.effects._journal is rig.journal
            assert rig.owner.control.author_driver is self
            assert rig.owner.core.admission.active is None
            triaged.append(self)
        return await driving(self, stage, consumed, **kwargs)
    monkeypatch.setattr(Driver, "run", driver_run)
    execute = rig.exec.run
    async def execution(argv, **kwargs):
        if argv[3:4] == ["push"]:
            pushes.append(list(argv))
            return 0, "", ""
        return await execute(argv, **kwargs)
    monkeypatch.setattr(rig.exec, "run", execution)
    rig.owner.box.enqueue(message_class="suggestion", origin="operator", stage=None,
                          outcome=None, summary="a policy decision", occurrence_id="fixture-arrival")
    write(root, "source", text())
    run = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, lambda: len([e for e in terminals(rig) if e.body.get("to") == "merged"]) == 5
                    and pushes and triaged)
        merged = [e for e in terminals(rig) if e.body.get("to") == "merged"]
        assert all(e.body["commit"] and e.body["reviewed_sha"] for e in merged)
        assert rig.owner.box.pending() == []
        assert len(triaged) == 1
        write(root, "dependent", text(depends="- candidate-4"))
        await until(rig, lambda: terminals(rig, "dependent"))
        assert [stem for stem, stage, _ in calls if stage == "implement"] == ["source", "dependent"]
        assert any(e.type == EventType.EFFECT_COMPLETION and e.body == {"result": {"merged": 5}}
                   for e in rig.journal.read())
    finally:
        await finish(rig, run)


@pytest.mark.asyncio
async def test_serve_author_uses_shared_journal(root, monkeypatch):
    from tempo.driver import Driver
    from tests.test_triage import author_reply, requisition_reply
    llm = FakeLLM([reply("author"), author_reply("drafted"), requisition_reply("approve")])
    observed = []
    run_driver = Driver.run
    rig = await graph(root, monkeypatch)
    from tempo.config import load_config
    config = root / "config.yaml"
    config.write_text(config.read_text().replace("routing:\n", "routing:\n"
        "  - {tier: high, surface: review, candidates: [{provider: claude}]}\n"
        "  - {tier: max, surface: review, candidates: [{provider: claude}]}\n"))
    rig.checkout = replace(rig.checkout, config=load_config(None, cwd=root))
    old = rig.owner
    rig.owner = serve.Serve(rig.checkout, read=old.read, stems=old.stems, prepare=rig.prepare, llm=rig.llm)
    async def driving(self, stage, consumed, **kwargs):
        assert self.journal is self.effects._journal is rig.journal
        assert rig.owner.control.author_driver is self
        self.llm = llm
        observed.append((self, stage.surface))
        return await run_driver(self, stage, consumed, **kwargs)
    monkeypatch.setattr(Driver, "run", driving)
    rig.owner.box.enqueue(message_class="suggestion", origin="operator", stage=None,
        outcome=None, summary="an authored repair", occurrence_id="author-fixture")
    run = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, lambda: rig.owner.box.pending() == [])
        assert [surface for _, surface in observed] == ["triage", "author"]
        assert observed[0][0] is observed[1][0]
        assert [r.surface for r in llm.requests] == ["triage", "author", "requisition_review"]
        assert (root / "tickets/drafted/ticket.md").is_file()
        assert any(e.type == EventType.EFFECT_COMPLETION and e.key == "ticket-plane/drafted/author"
                   for e in rig.journal.read())
    finally:
        await finish(rig, run)


@pytest.mark.asyncio
async def test_serve_kill_unwinds_check_cleanup(root, monkeypatch):
    entered, cleaning, release = (asyncio.Event() for _ in range(3))
    async def before(ctx, ticket, stage, attempt):
        if stage == "check":
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
    script(monkeypatch, before=before)
    rig = await graph(root, monkeypatch)
    write(root, "work", text())
    run = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, entered.is_set)
        request(rig, "kill", "stop")
        await until(rig, cleaning.is_set)
        assert all(not t.cancelling() and not t.done() for t in rig.owner.workers)
        assert not any(e.body.get("kind") == "kill_applied" for e in rig.journal.read())
    finally:
        release.set()
        await finish(rig, run)


@pytest.mark.asyncio
async def test_serve_watcher_edits_deletions_and_last_good(root, monkeypatch):
    rig = await graph(root, monkeypatch)
    request(rig, "pause", "pause")
    write(root, "work", text())
    run = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, lambda: "work" in rig.owner.core.watcher.cache)
        original = rig.owner.core.watcher.cache["work"]
        write(root, "work", "---\ninvalid: [\n")
        await until(rig, lambda: any(e.body.get("signal") == "watcher_parse_failure"
                                    for e in rig.journal.read()))
        assert rig.owner.core.watcher.cache["work"] is original
        assert rig.owner.core.scheduler.pending["work"] is original
        write(root, "work", text(priority="P0"))
        await until(rig, lambda: rig.owner.core.watcher.cache["work"].frontmatter.priority == "P0")
        root.joinpath("tickets/work/ticket.md").unlink()
        await until(rig, lambda: "work" not in rig.owner.core.watcher.cache)
        assert "work" not in rig.owner.core.scheduler.pending and not terminals(rig, "work")
    finally:
        await finish(rig, run)


@pytest.mark.asyncio
async def test_serve_kill_unwinds_preparation(root, monkeypatch):
    entered, cleaning, release = (asyncio.Event() for _ in range(3))
    async def prepare(local):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
    rig = await graph(root, monkeypatch, prepare=prepare)
    write(root, "work", text())
    run = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, entered.is_set)
        request(rig, "kill", "stop")
        await until(rig, cleaning.is_set)
        assert all(not t.cancelling() and not t.done() for t in rig.owner.workers)
        assert not any(e.body.get("to") == "running" for e in rig.journal.read())
    finally:
        release.set()
        await finish(rig, run)


@pytest.mark.asyncio
async def test_serve_abort_failure_does_not_apply_kill(root, monkeypatch):
    rig = await graph(root, monkeypatch)
    async def abort():
        raise OSError("scripted abort cleanup failure")
    monkeypatch.setattr(rig.owner, "abort", abort)
    run = asyncio.create_task(rig.owner.run())
    await until(rig, rig.owner.ready.is_set)
    request(rig, "kill", "stop")
    await until(rig, run.done)
    assert await run == 1 and all(t.done() for t in rig.owner.workers)
    assert not any(e.body.get("kind") == "kill_applied" for e in rig.journal.read())
    assert audit_journal(rig.journal) == []


async def admission_graph(root, monkeypatch, changes, *, endings=None, settled=None):
    """The merged CoreRig, actual CLI serve composition and real stages with scripted model replies."""
    from tests.test_stages import implement_reply, verdict, SNAG
    endings = endings if endings is not None else {}
    models, contexts, returns = [], {}, []
    class Model(FakeLLM):
        async def call(self, req):
            stem = req.ticket
            if req.surface == "implement":
                for path, data in changes.get(stem, {}).items():
                    rig.fs.write(req.worktree / path, data.encode())
                if changes.get(stem):
                    await rig.checkout.git.add(req.worktree, list(changes[stem]))
                    await rig.checkout.git.commit(req.worktree, "scripted implementation")
                failed = endings.get(stem) == "implement"
                value = implement_reply("premise_failed" if failed else
                                        "already_satisfied" if endings.get(stem) == "noop" else "ok", [{
                    "code": "premise", "message": "scripted premise refusal",
                    "paved_road": "revise this ticket"}] if failed else [])
            elif req.surface == "review":
                value = verdict("snag", [SNAG]) if endings.get(stem) == "review" else verdict()
            elif req.surface == "diagnose":
                value = json.dumps({"verdict": "abandon-human", "lessons": ["inspect the admission findings"]})
            elif req.surface == "rework":
                value = json.dumps({"action": "escalate", "tickets": []})
            else:
                raise AssertionError(f"unexpected scripted surface {req.surface}")
            self.script.append(value)
            return await super().call(req)
    async def prepare(local):
        llm = Model([])
        models.append(llm)
        writer = runner.bind(local, llm)
        return writer
    rig = await graph(root, monkeypatch, prepare=prepare)
    rig.prepare, rig.models, rig.contexts, rig.returns = prepare, models, contexts, returns
    process, scripted = SubprocessExec(), rig.exec.run
    async def execute(argv, **kwargs):
        if argv[0] in {"git", "true", "false", "grep", "python3"}:
            return await process.run(argv, **kwargs)
        return await scripted(argv, **kwargs)
    monkeypatch.setattr(rig.exec, "run", execute)
    review = stages.review
    async def reviewed(ctx, ticket, invoice, *, attempt):
        result = await review(ctx, ticket, invoice, attempt=attempt)
        contexts[ticket.stem] = ctx
        if result.outcome == "ok" and settled is not None:
            await settled(rig, ctx, ticket, attempt)
        return result
    monkeypatch.setattr(stages, "review", reviewed)
    drive = runner.drive
    async def driving(ctx, ticket, **kwargs):
        result = await drive(ctx, ticket, **kwargs)
        returns.append((ticket.stem, result))
        return result
    monkeypatch.setattr(runner, "drive", driving)
    consume = TicketWriter.consume_result
    async def consumed(writer, ticket, result, *, attempt):
        live = (ticket.stem, attempt) in writer.admission.results
        await consume(writer, ticket, result, attempt=attempt)
        if not live:
            terminal = terminals(rig, ticket.stem)[-1].body["to"]
            returns.append((ticket.stem, terminal))
    monkeypatch.setattr(TicketWriter, "consume_result", consumed)
    return rig


def admission_ticket(root, stem, *, paths=("tempo/thing.py",), verification="true", depends="none", priority="P2"):
    from tests.test_stages import TICKET
    raw = TICKET.format(bypass="").replace("priority: P2", f"priority: {priority}")
    raw = raw.replace("## Depends on\nnone", f"## Depends on\n{depends}")
    raw = raw.replace("## Scope fence\n- tempo/thing.py", "## Scope fence\n" + "\n".join(f"- {p}" for p in paths))
    raw = raw.replace("grep -q ok tempo/thing.py\nenv", verification)
    write(root, stem, raw)


async def start_admission(rig, monkeypatch):
    build = cli.build_serve
    def built(*args, **kwargs):
        rig.owner = build(*args, **kwargs)
        return rig.owner
    monkeypatch.setattr(cli, "build_serve", built)
    run = asyncio.create_task(serve.serve(rig.checkout, prepare=rig.prepare, llm=rig.llm))
    await until(rig, lambda: rig.owner.ready.is_set())
    return run


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["ok", "noop", "implement", "check", "review"])
async def test_serve_routes_settled_run_to_composed_queue_once(root, monkeypatch, ending):
    changes = {} if ending == "noop" else {"tempo/other.py" if ending == "check" else "tempo/thing.py": "ok\n"}
    rig = await admission_graph(root, monkeypatch, {"work": changes}, endings={"work": ending})
    admission_ticket(root, "work")
    offers, results = [], []
    entered, release = asyncio.Event(), asyncio.Event()
    offer, admit = MergeQueue.offer, MergeQueue._admit
    def offered(queue, ticket, *, attempt):
        writer = rig.owner.writers[ticket.stem]
        assert queue is writer.queue is rig.owner.queue
        assert queue.ctx is writer.ctx and queue.control is rig.owner.control
        assert queue.ctx.driver.journal is queue.control.inbox.journal is rig.journal
        offers.append((ticket, attempt))
        offer(queue, ticket, attempt=attempt)
    async def admitted(queue, ticket, attempt):
        entered.set()
        await release.wait()
        result = await admit(queue, ticket, attempt)
        results.append(result)
        return result
    monkeypatch.setattr(MergeQueue, "offer", offered)
    monkeypatch.setattr(MergeQueue, "_admit", admitted)
    run = await start_admission(rig, monkeypatch)
    try:
        if ending == "ok":
            await until(rig, entered.is_set)
            assert len(offers) == 1 and offers[0][1] == 0
            assert not terminals(rig, "work") and rig.returns == []
            assert rig.owner.core.admission.active.stem == "work"
            assert not rig.owner.core.admission.task.done()
            release.set()
        await until(rig, lambda: rig.returns)
        [terminal] = terminals(rig, "work")
        if ending == "ok":
            assert rig.returns == [("work", "merged")] and isinstance(results[-1].artifact, Admission)
            assert terminal.body["commit"] == results[-1].artifact.commit
            assert await rig.checkout.git.rev_parse(root, "main") == results[-1].artifact.commit
        else:
            assert offers == [] and not any(r == "merged" for _, r in rig.returns)
            assert terminal.body["to"] == ("already_satisfied" if ending == "noop" else
                                           "premise_failed" if ending == "implement" else "gate_failed")
    finally:
        release.set()
        await finish(rig, run)


@pytest.mark.asyncio
async def test_serve_admission_rebases_regates_and_retires(root, monkeypatch):
    endings = {"work": "review"}
    async def moved(rig, ctx, ticket, attempt):
        if ticket.stem == "work":
            rig.pinned = await ctx.git.rev_parse(root, ticket.stem)
            assert "D tickets/" in await ctx.git.status_porcelain(ctx.worktree(ticket.stem))
            rig.fs.write(root / "tempo/other.py", b"moved\n")
            await ctx.git.add(root, ["tempo/other.py"])
            await ctx.git.commit(root, "main moves after approval")
    rig = await admission_graph(root, monkeypatch,
        {"work": {"tempo/thing.py": "ok\n"}, "dependent": {"tempo/dependent.py": "ok\n"}},
        endings=endings, settled=moved)
    # The tracked ticket-plane copies come from a real failed producing run, never a ready fixture.
    admission_ticket(root, "work")
    first = await start_admission(rig, monkeypatch)
    await until(rig, lambda: rig.returns == [("work", "gate_failed")])
    await finish(rig, first)
    await runner.verdict("work", rig.checkout, kill=False)
    endings.clear()
    config = root / "config.yaml"
    config.write_text(config.read_text().replace("review: {}", "review: {mechanical: [{code: host, argv: ['true'], trigger: always, severity: hard}]}")
                      .replace("merge: {}", "merge: {safety_checks: [host]}"))
    admission_ticket(root, "dependent", paths=("tempo/dependent.py",), depends="- work")
    trace, trees = [], []
    for name in ("gather_safety_evidence", "gather_evidence"):
        original = getattr(stages, name)
        async def evidence(ctx, ticket, *args, _name=name, _original=original, **kwargs):
            if ticket.stem == "work" and rig.owner.queue.active == "work":
                trace.append(_name)
                assert (ctx.worktree(ticket.stem) / "tempo/other.py").read_text() == "moved\n"
            return await _original(ctx, ticket, *args, **kwargs)
        monkeypatch.setattr(stages, name, evidence)
    original = rig.checkout.git.rev_parse
    async def rev_parse(path, ref):
        sha = await original(path, ref)
        if ref.endswith("^{tree}"):
            trees.append((ref, sha))
        return sha
    monkeypatch.setattr(rig.checkout.git, "rev_parse", rev_parse)
    run = await start_admission(rig, monkeypatch)
    try:
        await until(rig, lambda: ("dependent", "merged") in rig.returns)
        assert rig.returns == [("work", "gate_failed"), ("work", "merged"), ("dependent", "merged")]
        assert trace == ["gather_safety_evidence", "gather_evidence", "gather_safety_evidence"]
        assert trees[:2] == [("HEAD^{tree}", trees[0][1]), ("main^{tree}", trees[0][1])]
        [merged] = [e for e in terminals(rig, "work") if e.body.get("to") == "merged"]
        assert merged.body == {"to": "merged", "commit": merged.body["commit"], "reviewed_sha": rig.pinned}
        assert merged.key is None
        message = await rig.checkout.git._run(root, "log", "-1", "--format=%B", merged.body["commit"])
        assert message.startswith("tempo(work): `thing.py` holds the word ok.")
        assert "tempo-ticket: work" in message and f"tempo-reviewed-sha: {rig.pinned}" in message
        [effect] = [e for e in rig.journal.read() if e.key == "merge/work/1" and e.type == EventType.EFFECT_COMPLETION]
        assert effect.body == {"result": {"commit": merged.body["commit"]}}
        assert not rig.checkout.config.worktree_root.joinpath("work").exists()
        assert not (await rig.checkout.git._run(root, "branch", "--list", "work")).strip()
        spools = rig.checkout.config.state_dir / "spools/work/1"
        for path in ("merge-integration/verify-01.txt", "merge-safety/host-01.txt", "merge-integration/host-01.txt"):
            assert "exit 0" in (spools / path).read_text()
        assert [r.surface for llm in rig.models for r in llm.requests].count("review") == 3
    finally:
        await finish(rig, run)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["red-streak", "tree-mismatch", "exception", "cancel", "kill"])
async def test_serve_admission_holds_and_cleanup(root, monkeypatch, case):
    if case == "red-streak":
        changes = {stem: {f"tempo/{stem}.py": "red\n" if stem != "green" else "ok\n"}
                   for stem in ("one", "two", "three", "green")}
        rig = await admission_graph(root, monkeypatch, changes)
        admission_config(root, command=["python3", "-c", "from pathlib import Path; import sys; "
            "sys.exit(int(any(p.read_text().startswith('red') for p in Path('tempo').glob('*.py'))))"])
        for stem, priority in zip(changes, ("P0", "P1", "P2", "P3"), strict=True):
            admission_ticket(root, stem, paths=(f"tempo/{stem}.py",), priority=priority)
        offers, empty = [], asyncio.Event()
        offer, process = MergeQueue.offer, MergeQueue.process
        def offered(queue, ticket, **kwargs):
            offers.append((queue, ticket.stem, kwargs["attempt"]))
            offer(queue, ticket, **kwargs)
        async def processed(queue):
            if "green" in queue.pending and not empty.is_set():
                assert not any(e.body.get("to") == "merged" for e in terminals(rig, "green"))
                assert ("green", "merged") not in rig.returns
                empty.set()
                return []
            return await process(queue)
        monkeypatch.setattr(MergeQueue, "offer", offered)
        monkeypatch.setattr(MergeQueue, "process", processed)
        run = await start_admission(rig, monkeypatch)
        try:
            await until(rig, lambda: ("three", "gate_failed") in rig.returns)
            queue = rig.owner.queue
            identity = queue.hold_id
            assert queue.paused and queue.red_stems == ["one", "two", "three"]
            assert "green" in rig.owner.core.scheduler.pending and not terminals(rig, "green")
            assert [stem for _, stem, _ in offers] == ["one", "two", "three"]
            assert len({id(q) for q, _, _ in offers}) == 1
            [event] = [e for e in rig.journal.read() if e.body.get("kind") == RED_STREAK]
            assert event.ticket == "three" and event.key is None and event.type == EventType.SIGNAL
            assert event.body == {"kind": RED_STREAK, "stems": ["one", "two", "three"], "limit": 3, "hold_id": identity}
            request(rig, "resume", "wrong-hold", hold="wrong")
            request(rig, "resume", "wrong-life", hold=identity, lifecycle="retired")
            request(rig, "pause", "operator-pause")
            await until(rig, lambda: rig.owner.control.projection.pause_id == "operator-pause")
            assert queue.paused and not terminals(rig, "green")
            decisions = {e.body["request_id"]: e.body["decision"] for e in rig.journal.read()
                         if e.body.get("kind") == "control_decision"}
            assert decisions == {"wrong-hold": "stale", "wrong-life": "stale", "operator-pause": "accepted"}
            request(rig, "resume", "admission-release", hold=identity)
            await until(rig, lambda: identity in rig.owner.control.projection.released_hold_ids)
            await until(rig, lambda: not queue.paused)
            assert rig.owner.control.projection.pause_id == "operator-pause" and not terminals(rig, "green")
            request(rig, "resume", "pause-release", hold="operator-pause")
            await until(rig, empty.is_set)
            assert ("green", "merged") not in rig.returns and not terminals(rig, "green")
            await until(rig, lambda: ("green", "merged") in rig.returns)
            assert [stem for _, stem, _ in offers] == ["one", "two", "three", "green"]
            assert all(q is queue and attempt == 0 for q, _, attempt in offers)
            assert queue.red_stems == [] and not queue.pending and not queue.paused
            assert (root / "tempo/green.py").read_text() == "ok\n"
            assert all(not (root / f"tempo/{stem}.py").exists() for stem in ("one", "two", "three"))
        finally:
            await finish(rig, run)
        return

    async def settled(rig, ctx, ticket, attempt):
        rig.pinned = await ctx.git.rev_parse(root, ticket.stem)
        if case != "tree-mismatch":
            ctx.fs.write(root / "tempo/generated.txt", b"base\nmain\n")
            await ctx.git.add(root, ["tempo/generated.txt"])
            await ctx.git.commit(root, "main conflicts")
        rig.before = await ctx.git.rev_parse(root, "main")
    changes = {"tempo/thing.py": "ok\n"}
    if case != "tree-mismatch":
        changes["tempo/generated.txt"] = "base\nbranch\n"
    rig = await admission_graph(root, monkeypatch, {"work": changes}, settled=settled)
    if case != "tree-mismatch":
        rig.fs.write(root / "tempo/generated.txt", b"base\n")
        await rig.checkout.git.add(root, ["tempo/generated.txt"])
        await rig.checkout.git.commit(root, "generated base")
    admission_config(root, strategies=[{"paths": ["tempo/generated.txt"], "strategy": "union"}])
    admission_ticket(root, "work", paths=tuple(changes))
    entered, release, cleaning, abort_release = (asyncio.Event() for _ in range(4))
    failures, chronology = [], []
    error = RuntimeError("injected admission failure")
    call = rig.checkout.git._call
    async def called(path, *args, **kwargs):
        if args == ("rebase", "--abort"):
            cleaning.set()
            if case in {"cancel", "kill"}:
                await abort_release.wait()
            chronology.append("abort")
        return await call(path, *args, **kwargs)
    monkeypatch.setattr(rig.checkout.git, "_call", called)
    if case == "tree-mismatch":
        squash = rig.checkout.git.merge_squash
        async def corrupted(path, stem):
            await squash(path, stem)
            # A real staged-tree defect produces a real mismatching commit, not a fake hash or terminal.
            rig.fs.write(path / "tempo/engine-defect.py", b"unexpected staged content\n")
            await rig.checkout.git.add(path, ["tempo/engine-defect.py"])
        monkeypatch.setattr(rig.checkout.git, "merge_squash", corrupted)
        admit = MergeQueue._admit
        async def admitted(queue, ticket, attempt):
            try:
                return await admit(queue, ticket, attempt)
            except TreeMismatch:
                entered.set()
                await release.wait()
                raise
        monkeypatch.setattr(MergeQueue, "_admit", admitted)
    else:
        async def resolve(*args):
            entered.set()
            if case == "exception":
                raise error
            await release.wait()
            raise AssertionError("cancel the admission before releasing this resolution barrier")
        monkeypatch.setattr(MergeQueue, "_resolve", resolve)
    run = await start_admission(rig, monkeypatch)
    rig.owner.failure = failures.append
    try:
        await until(rig, entered.is_set)
        queue = rig.owner.queue
        if case == "tree-mismatch":
            identity = queue.hold_id
            [event] = [e for e in rig.journal.read() if e.body.get("kind") == TREE_MISMATCH]
            checked = await rig.checkout.git.rev_parse(rig.checkout.config.worktree_root / "work", "HEAD^{tree}")
            main = await rig.checkout.git.rev_parse(root, "main^{tree}")
            assert checked != main and event.ticket == "work" and event.key is None
            assert event.body == {"kind": TREE_MISMATCH, "checked_tree": checked, "main_tree": main, "hold_id": identity}
            assert queue.paused and queue.active == "work" and not rig.returns
            request(rig, "resume", "wrong", hold="wrong")
            request(rig, "resume", "release", hold=identity)
            await until(rig, lambda: identity in rig.owner.control.projection.released_hold_ids)
            assert queue.paused and queue.active == "work" and not rig.returns
            release.set()
        elif case in {"cancel", "kill"}:
            assert queue._slot.locked() and queue.active == "work" and not terminals(rig, "work")
            if case == "kill":
                request(rig, "kill", "stop")
            else:
                run.cancel()
            await until(rig, cleaning.is_set)
            for _ in range(3):
                run.cancel()
                await turn()
                assert not run.done()
            assert not any(e.body.get("kind") == "kill_applied" for e in rig.journal.read())
            abort_release.set()
        await until(rig, run.done)
        assert await run == (0 if case in {"cancel", "kill"} else 1)
        assert rig.returns == [] and queue.active is None and not queue._slot.locked()
        assert rig.owner.tasks.tasks == () and all(t.done() for t in rig.owner.workers)
        assert all(t.cancelled() or not t._log_traceback for t in rig.owner.workers)
        assert rig.owner.core.admission.task is None and rig.owner.core.admission.active is None
        assert json.loads((rig.checkout.config.state_dir / "control/active.json").read_text()) is None
        workspace = rig.checkout.config.worktree_root / "work"
        assert workspace.exists() and not await rig.checkout.git.conflicted_paths(workspace)
        assert await rig.checkout.git.status_porcelain(workspace) == ""
        assert audit_journal(rig.journal) == []
        if case == "tree-mismatch":
            assert len(terminals(rig, "work")) == 1 and terminals(rig, "work")[0].body["to"] == "merged"
            assert await rig.checkout.git.rev_parse(root, "main") != rig.before
            assert len(failures) == 1 and isinstance(failures[0], TreeMismatch)
        else:
            assert chronology == ["abort"] and not terminals(rig, "work")
            assert await rig.checkout.git.rev_parse(root, "work") == rig.pinned
            assert await rig.checkout.git.rev_parse(root, "main") == rig.before
            assert failures == ([error] if case == "exception" else [])
    finally:
        release.set()
        abort_release.set()
        if not run.done():
            rig.owner.stop()
            await until(rig, run.done)
            await run


def admission_config(root, *, command=None, safety=False, strategies=()):
    import yaml
    path = root / "config.yaml"
    config = yaml.safe_load(path.read_text())
    config["review"] = {"mechanical": [{"code": "combined", "argv": command or ["true"],
        "trigger": "always", "severity": "hard"}]}
    config["merge"] = {"safety_checks": ["combined"] if safety else [], "strategies": list(strategies)}
    path.write_text(yaml.safe_dump(config))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["precheck", "safety", "integration", "host-red"])
async def test_serve_admission_refusal_keeps_main_green(root, monkeypatch, failure):
    command = ["python3", "-c", "from pathlib import Path; import sys; "
        "sys.exit(int('red' in Path('tempo/thing.py').read_text() and Path('tempo/other.py').exists()))"]
    async def settled(rig, ctx, ticket, attempt):
        if ticket.stem == "work":
            if failure == "precheck":
                ctx.fs.write(ctx.worktree(ticket.stem) / "tickets/forbidden.md", b"forbidden")
                await ctx.git.add(ctx.worktree(ticket.stem), ["tickets/forbidden.md"])
                await ctx.git.commit(ctx.worktree(ticket.stem), "committed ticket-plane violation")
            rig.before = await ctx.git.rev_parse(root, "main")
    rig = await admission_graph(root, monkeypatch, {
        "work": {"tempo/thing.py": "ok red\n"}, "main-side": {"tempo/other.py": "moved\n"},
        "next": {"tempo/next.py": "ok\n"}}, settled=settled)
    admission_config(root, command=["false"] if failure == "host-red" else command,
                     safety=failure == "safety")
    admission_ticket(root, "work", priority="P0", verification="grep -q absent tempo/thing.py" if failure == "host-red" else "true")
    # Stage selection suspends a genuine producing run while another genuine run moves main.
    hold = trip(rig, stage="review") if failure in {"safety", "integration"} else None
    if hold:
        admission_ticket(root, "main-side", paths=("tempo/other.py",))
    preserved, offers = [], []
    harvest, offer = runner.harvest_failure, MergeQueue.offer
    admit = MergeQueue._admit
    async def admitted(queue, ticket, attempt):
        before = await queue.ctx.git.rev_parse(root, "main")
        result = await admit(queue, ticket, attempt)
        if ticket.stem == "work":
            assert result.outcome == "gate_failed"
            assert await queue.ctx.git.rev_parse(root, "main") == before
            assert queue.ctx.worktree(ticket.stem).exists()
        return result
    monkeypatch.setattr(MergeQueue, "_admit", admitted)
    async def harvested(ctx, ticket, **kwargs):
        if ticket.stem == "work" and kwargs["stage"] == "merge":
            if failure == "precheck":
                assert await ctx.git.rev_parse(root, "main") == rig.before
            assert ctx.worktree(ticket.stem).exists() and await ctx.git.rev_parse(root, ticket.stem)
            assert "red" not in (root / "tempo/thing.py").read_text()
            preserved.append(kwargs)
        await harvest(ctx, ticket, **kwargs)
    def offered(queue, ticket, **kwargs):
        offers.append(ticket.stem)
        offer(queue, ticket, **kwargs)
    monkeypatch.setattr(runner, "harvest_failure", harvested)
    monkeypatch.setattr(MergeQueue, "offer", offered)
    run = await start_admission(rig, monkeypatch)
    try:
        if hold:
            await until(rig, lambda: ("main-side", "merged") in rig.returns)
            candidate = rig.owner.writers["work"].ctx.worktree("work")
            # Both branches passed Check independently; this command also proves the old candidate alone green.
            assert (await rig.exec.run(command, cwd=candidate, env=ENV, timeout=30))[0] == 0
            assert (await rig.exec.run(command, cwd=root, env=ENV, timeout=30))[0] == 0
            request(rig, "resume", "review-release", hold=hold)
        await until(rig, lambda: ("work", "gate_failed") in rig.returns)
        [failure_args] = preserved
        code = "post_rebase_regate" if failure == "precheck" else "combined"
        assert code in {f.code for f in failure_args["findings"]}
        assert all(f.paved_road for f in failure_args["findings"])
        assert ("work" in offers) == (failure != "precheck")
        assert len(terminals(rig, "work")) == 1
        assert (root / "tickets/work/attempts/0/harvest.json").is_file()
        spools = rig.checkout.config.state_dir / "spools/work/0"
        assert (spools / "merge-integration/verify-01.txt").exists() == (failure in {"integration", "host-red"})
        if failure == "host-red":
            # Ticket base-red remains attributable, while an always-hard host failure still refuses.
            assert (spools / "merge-integration/verify-01-base.txt").is_file()
            assert not rig.checkout.config.worktree_root.joinpath(".base/work").exists()
        assert rig.owner.queue.red_stems == (["work"] if failure in {"integration", "host-red"} else [])
        # A fresh producing candidate proceeds; the old refused branch never reaches main.
        admission_config(root)
        admission_ticket(root, "next", paths=("tempo/next.py",))
        await until(rig, lambda: ("next", "merged") in rig.returns)
        assert "red" not in (root / "tempo/thing.py").read_text() and not rig.owner.queue.red_stems
        assert [req.surface for llm in rig.models for req in llm.requests if req.ticket == "work"].count("diagnose") == 1
    finally:
        await finish(rig, run)


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution", ["mechanical", "handoff"])
async def test_serve_conflict_handoff_runs_after_admission_unwinds(root, monkeypatch, resolution):
    async def settled(rig, ctx, ticket, attempt):
        rig.pinned = await ctx.git.rev_parse(root, ticket.stem)
        ctx.fs.write(root / "tempo/generated.txt", b"base\nmain\n")
        await ctx.git.add(root, ["tempo/generated.txt"])
        await ctx.git.commit(root, "main conflicts after review")
        rig.before = await ctx.git.rev_parse(root, "main")
    rig = await admission_graph(root, monkeypatch,
        {"work": {"tempo/thing.py": "ok\n", "tempo/generated.txt": "base\nbranch\n"}}, settled=settled)
    rig.fs.write(root / "tempo/generated.txt", b"base\n")
    await rig.checkout.git.add(root, ["tempo/generated.txt"])
    await rig.checkout.git.commit(root, "generated base")
    admission_config(root, safety=True, strategies=[{"paths": ["tempo/generated.txt"], "strategy": "union"}]
                     if resolution == "mechanical" else [])
    admission_ticket(root, "work", paths=("tempo/thing.py", "tempo/generated.txt"), verification="grep -q ok tempo/thing.py")
    chronology, gates = [], []
    call = rig.checkout.git._call
    async def called(path, *args, **kwargs):
        result = await call(path, *args, **kwargs)
        if args == ("rebase", "--abort"):
            chronology.append("abort")
        return result
    monkeypatch.setattr(rig.checkout.git, "_call", called)
    process = MergeQueue.process
    async def processed(queue):
        result = await process(queue)
        if result:
            assert queue.active is None and not queue._slot.locked()
            chronology.append("unwind")
        return result
    monkeypatch.setattr(MergeQueue, "process", processed)
    apply = daemon.apply_rework
    async def reworked(ctx, ticket, findings, **kwargs):
        queue = rig.owner.queue
        handoff = kwargs["handoff"]
        assert queue.active is None and not queue._slot.locked()
        assert chronology == ["abort", "unwind"]
        assert handoff.approval_invalidated and handoff.reviewed_sha == rig.pinned
        assert handoff.conflicted_paths == ["tempo/generated.txt"]
        assert (root / "tempo/generated.txt").read_text() == "base\nmain\n"
        assert await ctx.git.rev_parse(root, ticket.stem) == rig.pinned
        assert not await ctx.git.conflicted_paths(ctx.worktree(ticket.stem))
        assert await ctx.git.status_porcelain(ctx.worktree(ticket.stem)) == ""
        chronology.append("rework")
        return await apply(ctx, ticket, findings, **kwargs)
    monkeypatch.setattr(daemon, "apply_rework", reworked)
    from tempo import merge as merge_owner
    for gate in stages.CHECK_GATES + merge_owner.MERGE_GATES:
        check = gate.check
        def checked(*args, _gate=gate, _check=check):
            if rig.owner.queue is not None and rig.owner.queue.active == "work":
                gates.append(_gate.code)
            return _check(*args)
        monkeypatch.setattr(gate, "check", checked)
    run = await start_admission(rig, monkeypatch)
    try:
        await until(rig, lambda: rig.returns)
        [facts] = [e for e in rig.journal.read() if e.body.get("kind") == CONFLICT_FACTS]
        assert facts.ticket == "work" and facts.key is None and facts.type == EventType.SIGNAL
        assert facts.body == {"kind": CONFLICT_FACTS, "conflicted_paths": ["tempo/generated.txt"],
            "resolving_rung": "mechanical" if resolution == "mechanical" else "rework",
            "strategy_paths": ["tempo/generated.txt"] if resolution == "mechanical" else [], "integration_red_paths": []}
        [terminal] = terminals(rig, "work")
        if resolution == "mechanical":
            assert rig.returns == [("work", "merged")] and chronology == ["unwind"]
            assert (root / "tempo/generated.txt").read_text() == "base\nmain\nbranch\n"
            assert gates == ["scope_fence", "run_record", "ticket_schema", "post_rebase_regate", "correctness_review",
                "scope_fence", "verification", "run_record", "diff_budget", "core_drift", "bug_evidence", "ticket_schema", "post_rebase_regate", "correctness_review"]
            for tier in ("safety", "integration"):
                assert "exit 0" in (rig.checkout.config.state_dir / f"spools/work/0/merge-{tier}/host-01.txt").read_text()
            assert terminal.body["reviewed_sha"] == rig.pinned
        else:
            assert rig.returns == [("work", "gate_failed")] and chronology == ["abort", "unwind", "rework"]
            assert terminal.body["stage"] == "merge" and "commit" not in terminal.body
            [req] = [r for llm in rig.models for r in llm.requests if r.surface == "rework"]
            assert '"approval_invalidated":true' in req.rendered
            assert any(e.body.get("signal") == "rework_order" for e in rig.journal.read())
            assert not gates
    finally:
        await finish(rig, run)


def notify_command(root, *, enabled=True):
    import sys
    import yaml
    path = root / "config.yaml"
    raw = yaml.safe_load(path.read_text())
    if enabled:
        raw["notify"] = [sys.executable, "-c",
            "import sys; from pathlib import Path; "
            "p=Path('deliveries.jsonl'); "
            "p.open('a').write(sys.argv[-1]+'\\n'); "
            "sys.exit(1 if Path('notify-fails').exists() else 0)"]
    else:
        raw.pop("notify", None)
    path.write_text(yaml.safe_dump(raw))


def deliveries(root):
    path = root / "deliveries.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def notify_completions(rig):
    return [e for e in rig.journal.read() if e.type == EventType.EFFECT_COMPLETION
            and e.key.startswith("notify/")]


def escalation_batch(rig, label):
    hold = trip(rig, stem=f"{label}-storm", signature=label)
    # Reuse the real composition and canonical MergeQueue writer, without launching host work.
    queue = rig.owner.queue
    if queue is None:
        queue = runner.bind(rig.owner.checkout, FakeLLM([])).queue
        rig.owner.queue = queue
    queue._hold(f"{label}-red", {"kind": RED_STREAK, "stems": ["one", "two", "three"], "limit": 3})
    red = queue.hold_id
    queue._hold(f"{label}-tree", {"kind": TREE_MISMATCH, "checked_tree": "checked", "main_tree": "main"})
    return hold, red, queue.hold_id


@pytest.mark.asyncio
async def test_serve_reconciles_notifications_at_startup_and_poll(root, monkeypatch):
    rig = await graph(root, monkeypatch)
    notify_command(root)
    old = escalation_batch(rig, "old")
    assert not deliveries(root)
    original = rig.owner.notifications.poll
    polled = []
    def poll(**kwargs):
        if kwargs.get("startup"):
            assert rig.owner.core.restart.ready and not rig.owner.ready.is_set()
        polled.append(kwargs.get("startup", False))
        original(**kwargs)
    monkeypatch.setattr(rig.owner.notifications, "poll", poll)
    run = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, lambda: len(notify_completions(rig)) == 3)
        assert polled[0] is True and False in polled
        assert old[0] in rig.owner.storm.holds() and rig.owner.queue.paused
        assert terminals(rig) == []
        for row in deliveries(root):
            assert row["identity"] in old and row["exit"] == "resume"
            if row["kind"] == "storm_breaker_trip":
                assert row["held"] == {"emitting_origin": "old-storm", "emitting_stage": "check"}
                assert row["signature"] and row["trip_id"] == old[0]
            elif row["kind"] == RED_STREAK:
                assert row["stems"] == ["one", "two", "three"] and row["limit"] == 3
            else:
                assert row["checked_tree"] == "checked" and row["main_tree"] == "main"
        new = escalation_batch(rig, "new")
        await until(rig, lambda: len(notify_completions(rig)) == 6)
        assert len(deliveries(root)) == 6
        for identity in (*old, *new):
            request(rig, "resume", f"release-{identity}", hold=identity)
        await until(rig, lambda: not rig.owner.storm.holds() and not rig.owner.queue.paused)
        assert len(deliveries(root)) == 6 and terminals(rig) == []
    finally:
        await finish(rig, run)
    prior = rig.owner
    rig.owner = cli.build_serve(rig.checkout, read=prior.read, stems=prior.stems, prepare=rig.prepare,
                                llm=rig.llm)
    restarted = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, lambda: (rig.checkout.config.state_dir / "heartbeat").exists()
                    and rig.owner.ready.is_set())
        for _ in range(50):
            rig.time.advance(.1)
            await turn()
        assert len(deliveries(root)) == len(notify_completions(rig)) == 6
    finally:
        await finish(rig, restarted)


@pytest.mark.asyncio
async def test_serve_unset_notify_warns_once_and_preserves_pending(root, monkeypatch):
    from tempo.notify import pending_escalations
    from tempo.status import project
    rig = await graph(root, monkeypatch)
    identities = escalation_batch(rig, "pending")
    status = project(rig.journal.read())
    run = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, lambda: (rig.checkout.config.state_dir / "heartbeat").exists())
        for _ in range(50):
            rig.time.advance(.1)
            await turn()
        warnings = [json.loads(line) for line in rig.owner.log.path.read_text().splitlines()
                    if json.loads(line)["event"] == "notify_unset"]
        assert len(warnings) == 1 and "push is off" in warnings[0]["warning"]
        assert not deliveries(root) and not notify_completions(rig)
        current = project(rig.journal.read())
        for field in ("merged", "in_flight", "blocked", "intake", "reject"):
            assert getattr(current, field) == getattr(status, field)
        assert len(pending_escalations(rig.journal.read())) == 3
        assert identities[0] in rig.owner.storm.holds() and rig.owner.queue.paused
        notify_command(root)
        await until(rig, lambda: len(notify_completions(rig)) == 3)
        assert len(deliveries(root)) == 3 and not pending_escalations(rig.journal.read())
        assert identities[0] in rig.owner.storm.holds() and rig.owner.queue.paused
        assert sum(json.loads(line)["event"] == "notify_unset"
                   for line in rig.owner.log.path.read_text().splitlines()) == 1
    finally:
        await finish(rig, run)


@pytest.mark.asyncio
async def test_serve_failed_notify_backoff_and_restart(root, monkeypatch):
    from tempo.notify import NOTIFY_RETRY_S, pending_escalations
    rig = await graph(root, monkeypatch)
    notify_command(root)
    (root / "notify-fails").touch()
    trip(rig)
    run = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, lambda: rig.owner.notifications.retry_at)
        intents = lambda: [e for e in rig.journal.read() if e.type == EventType.EFFECT_INTENT
                          and e.key.startswith("notify/")]
        assert len(intents()) == 1 and not notify_completions(rig)
        # Unrelated journal arrivals do not reset this key's failed-command backoff.
        rig.journal.append(EventType.SIGNAL, {"kind": "author_invoked"})
        for _ in range(100):
            rig.time.advance(.1)
            await rig.owner.maintenance()
            await turn()
        assert len(intents()) == len(deliveries(root)) == 1
        assert len(pending_escalations(rig.journal.read())) == 1 and terminals(rig) == []
        rig.time.advance(NOTIFY_RETRY_S)
        await until(rig, lambda: len(deliveries(root)) == 2 and rig.owner.notifications.task.done())
        assert len(intents()) == 2 and not notify_completions(rig)
    finally:
        await finish(rig, run)
    (root / "notify-fails").unlink()
    prior = rig.owner
    rig.owner = cli.build_serve(rig.checkout, read=prior.read, stems=prior.stems, prepare=rig.prepare,
                                llm=rig.llm)
    restarted = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, lambda: len(notify_completions(rig)) == 1)
        assert len(deliveries(root)) == 3 and terminals(rig) == []
        assert not pending_escalations(rig.journal.read())
    finally:
        await finish(rig, restarted)


@pytest.mark.asyncio
async def test_serve_hung_notify_keeps_maintenance_and_cleanup_responsive(root, monkeypatch):
    rig = await graph(root, monkeypatch)
    notify_command(root)
    escalation_batch(rig, "hung")
    entered, cancelled = asyncio.Event(), asyncio.Event()
    compose = rig.owner.notifications.compose
    async def hang(argv, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    def composed(config):
        seam = compose(config)
        monkeypatch.setattr(seam.exec_, "run", hang)
        return seam
    monkeypatch.setattr(rig.owner.notifications, "compose", composed)
    refreshed = []
    write_fs = rig.fs.write
    def written(path, data):
        if path.name == "heartbeat":
            refreshed.append(rig.time())
        write_fs(path, data)
    monkeypatch.setattr(rig.fs, "write", written)
    run = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, lambda: entered.is_set() and len(refreshed) >= 2)
        assert rig.owner.ready.is_set() and not notify_completions(rig)
        request(rig, "pause", "pause-during-notify")
        await until(rig, lambda: rig.owner.control.projection.pause_id == "pause-during-notify")
        rig.owner.core.restart.timers.arm("during-notify", rig.time(), ticket=None)
        await until(rig, lambda: "during-notify" in rig.owner.core.restart.timers.fired)
        assert terminals(rig) == []
        assert len([e for e in rig.journal.read() if e.type == EventType.EFFECT_INTENT
                    and e.key.startswith("notify/")]) == 1
        request(rig, "kill", "kill-during-notify")
        await until(rig, run.done)
        assert await run == 0 and cancelled.is_set()
        assert rig.owner.notifications.task is None
    finally:
        await finish(rig, run)
