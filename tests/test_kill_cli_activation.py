"""Identity-bound kill through the bootstrap CLI's existing writer and shared inbox."""

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from tempo import __main__ as entry, control, daemon, drain, runner, stages
from tempo.audit import audit_journal
from tempo.control import ControlProjection, ControlRequest, publish_request
from tempo.journal import EventType, Journal
from tempo.llm import FakeLLM
from tempo.lockfile import Lockfile
from tempo.reconcile import orphans
from tempo.seams import LocalFileSystem
from tempo.status import last_states
from tests.test_cli import ENV, Clock, Stages, root, write
from tests.test_control_cli import acquire, discovery, invoke, requests
from tests.test_daemon_composition import CoreRig, LiveDrain, assert_core_wiring
from tests.test_drain import confirmed, offer_events
from tests.test_kill_executor_abort import Timer, Writer, turn
from tests.test_kill_signal_journal import decision
from tests.test_stages import agent, implement_reply, verdict


def applications(journal):
    events = [e for e in journal.read() if e.body.get("kind") == daemon.KILL_APPLIED]
    assert all(e.type == EventType.SIGNAL and e.ticket is None and e.key is None for e in events)
    assert all(set(e.body) == {"kind", "request_id", "lifecycle_id"} for e in events)
    return [e.body for e in events]


def applied(id, life):
    return {"kind": daemon.KILL_APPLIED, "request_id": id, "lifecycle_id": life}


def test_live_kill_publishes_only_bound_request(root, monkeypatch, capsys):
    lock = acquire(root)
    discovery(root, "exact-life", "paused")
    monkeypatch.setattr(entry, "uuid4", lambda: SimpleNamespace(hex="kill_1-A"))
    def forbidden(*args, **kwargs):
        pytest.fail("second CLI acquired the writer role")
    try:
        with monkeypatch.context() as patch:
            for obj, name in ((Journal, "append"), (entry, "build_control"),
                              (Lockfile, "release"), (daemon, "control_inbox")):
                patch.setattr(obj, name, forbidden)
            assert invoke(root, "kill") == 0
        assert capsys.readouterr().out == "kill submitted: kill_1-A\n"
        assert requests(root) == [asdict(ControlRequest("kill_1-A", "exact-life", "kill", None))]
        assert not (lock.state_dir / "journal").exists()
        original = (lock.state_dir / "control/inbox/kill_1-A.json").read_bytes()
        with pytest.raises(FileExistsError, match="submit a new request id"):
            invoke(root, "kill")
        assert (lock.state_dir / "control/inbox/kill_1-A.json").read_bytes() == original
    finally:
        lock.release()


@pytest.mark.parametrize("stale", [False, True])
def test_idle_kill_refuses_without_state(root, monkeypatch, capsys, stale):
    state = root / ".tempo/state"
    if stale:
        discovery(root, "old-life", "old-hold")
    def contents():
        return {p.relative_to(state): p.read_bytes() for p in state.rglob("*")
                if p.is_file() and p.name != "tempo.lock"}
    before, trace = contents(), []
    acquire_, release = Lockfile.acquire, Lockfile.release
    def acquired(lock):
        acquire_(lock)
        trace.append("lock")
    def released(lock):
        assert lock.held
        trace.append("unlock")
        release(lock)
    def forbidden(*args, **kwargs):
        pytest.fail("idle kill allocated control state")
    with monkeypatch.context() as patch:
        patch.setattr(Lockfile, "acquire", acquired)
        patch.setattr(Lockfile, "release", released)
        for obj, name in ((entry, "uuid4"), (entry, "build_control"), (Journal, "append"),
                          (control, "read_active"), (control, "publish_request"),
                          (control, "write_active")):
            patch.setattr(obj, name, forbidden)
        assert invoke(root, "kill") == runner.EXIT_REFUSED
    assert capsys.readouterr().err == (
        "tempo kill: nothing running to kill -- start a drain before submitting kill\n")
    assert contents() == before and trace == ["lock", "unlock"]
    lock = acquire(root)
    lock.release()


@pytest.mark.parametrize("raw", [None, b"null\n", b"{", b"{}", b"[]",
    b'{"lifecycle_id":"","hold_id":null}',
    b'{"lifecycle_id":"life","hold_id":null,"extra":1}',
    b'{"lifecycle_id":"life","lifecycle_id":"other","hold_id":null}'])
def test_kill_discovery_and_lifecycle_races_fail_closed(root, monkeypatch, capsys, raw):
    original = Lockfile.acquire
    owner = Lockfile(root / ".tempo/state", instance_id="racing-owner", clock=Clock())
    def race(lock):
        if not owner.held:
            original(owner)
        original(lock)
    monkeypatch.setattr(Lockfile, "acquire", race)
    try:
        if raw is not None:
            LocalFileSystem().write(owner.state_dir / "control/active.json", raw)
        assert invoke(root, "kill") == 2
        assert "current control identity" in capsys.readouterr().err
        assert requests(root) == []
        discovery(root, "previous", "hold")
        publish = control.publish_request
        def replaced(state, request, fs):
            discovery(root, "replacement")
            publish(state, request, fs)
        monkeypatch.setattr(control, "publish_request", replaced)
        assert invoke(root, "kill") == 0
        [request] = requests(root)
        assert request["lifecycle_id"] == "previous" and request["hold_id"] is None
        journal = Journal(owner.state_dir, Clock())
        consumer = daemon.PauseConsumer(journal=journal, lifecycle_id="replacement",
            state_dir=owner.state_dir, fs=LocalFileSystem(), sleep=lambda _: turn(),
            files=lambda: (owner.state_dir / "control/inbox").glob("*"), read=Path.read_bytes)
        consumer.inbox.consume()
        aborts = []
        async def abort():
            aborts.append(True)
        asyncio.run(consumer.apply_kill(abort))
        assert aborts == [] and consumer.projection == ControlProjection("replacement")
        assert journal.read()[-1].body == decision(request["request_id"], life="previous",
            result="stale", reason="read the current lifecycle and submit a new request")
        assert applications(journal) == [] and requests(root) == [request]
        monkeypatch.setattr(control, "publish_request", publish)
        assert invoke(root, "pause") == 0
        consumer.inbox.consume()
        assert consumer.projection.pause_id
        consumer.publish()
        assert invoke(root, "resume") == 0
        consumer.inbox.consume()
        assert consumer.projection.pause_id is None and not consumer.projection.kill_requested
    finally:
        owner.release()


def exercise_live_cli_kill(root, monkeypatch):
    """Real main -> pipeline/bind -> TicketWriter -> StageContext -> Driver."""
    write(root, "work", confirmed())
    trace, composed, controls = [], [], []
    factory = entry.build_control
    def build(checkout):
        consumer = factory(checkout)
        controls.append(consumer)
        return consumer
    monkeypatch.setattr(entry, "build_control", build)
    original_drive = runner.drive
    async def observed(ctx, work):
        dispatch_tasks.append(asyncio.current_task())
        try:
            return await original_drive(ctx, work)
        finally:
            trace.append("dispatch-clean")
    monkeypatch.setattr(runner, "drive", observed)
    original_abort = stages.StageContext.abort_current
    async def forward(ctx):
        trace.append("stage-abort")
        await original_abort(ctx)
    monkeypatch.setattr(stages.StageContext, "abort_current", forward)

    class LLM(Writer):
        preflight_notices = ()

        async def preflight(self):
            return []

        async def call(self, req):
            async def kill():
                await self.started.wait()
                assert await asyncio.to_thread(invoke, root, "kill") == 0
                wake.put_nowait(None)
                await self.unwinding.wait()
                unwind_valid.append(self.driver._active is not None
                    and applications(checkout.journal) == [] and "dispatch-clean" not in trace
                    and (checkout.config.state_dir / "control/active.json").read_bytes() != b"null\n")
                self.release.set()
            manager = asyncio.create_task(kill())
            try:
                return await super().call(req)
            finally:
                await manager
                trace.append("executor-clean")

        def abort_current(self):
            assert checkout.control.projection.kill_requested
            [request] = requests(root)
            assert any(e.body == decision(request["request_id"], life=request["lifecycle_id"])
                       for e in checkout.journal.read())
            trace.append("executor-stop")
            dispatch_cancelled.append(bool(dispatch_tasks[0].cancelling()))
            super().abort_current()

    llm = LLM(held=True)
    dispatch_tasks, dispatch_cancelled = [], []
    unwind_valid = []
    wake = asyncio.Queue()
    checkout = None
    bind = runner.bind
    def composed_bind(c, model):
        nonlocal checkout
        async def sleep(_):
            await wake.get()
        checkout = c
        # The same consumer and executor cross the production callable boundary.
        checkout.control.sleep = sleep
        before = checkout.journal.read()
        assert model is llm
        writer = bind(checkout, model)
        assert isinstance(writer, daemon.TicketWriter) and writer.ctx.driver.journal is checkout.journal
        assert writer.queue.control is controls[0] is checkout.control
        assert checkout.journal.read() == before
        llm.driver = writer.ctx.driver
        llm.driver.sleep = Timer()
        append = checkout.journal.append
        def recorded(type, body, **kwargs):
            if body.get("kind") == daemon.KILL_APPLIED:
                trace.append("applied")
            return append(type, body, **kwargs)
        monkeypatch.setattr(checkout.journal, "append", recorded)
        composed.append(writer)
        return writer
    monkeypatch.setattr(runner, "ProviderLLM", lambda *args, **kwargs: llm)
    monkeypatch.setattr(runner, "bind", composed_bind)
    assert entry.main(["drain"], cwd=root, env=ENV, clock=Clock(), pipeline=runner.pipeline) == runner.EXIT_TICKET
    assert len(controls) == len(composed) == 1
    assert llm.calls == llm.aborts == llm.cancels == 1
    assert dispatch_cancelled == [False], "dispatch cancelled before composed executor abort"
    assert unwind_valid == [True]
    assert llm.mutations == ["write"]
    assert trace == ["stage-abort", "executor-stop", "executor-clean", "dispatch-clean", "applied"]
    [request] = requests(root)
    assert applications(checkout.journal) == [applied(request["request_id"], request["lifecycle_id"])]
    assert (checkout.config.state_dir / "control/active.json").read_bytes() == b"null\n"
    assert not any(e.body.get("signal") == drain.HALT_SIGNAL for e in checkout.journal.read())
    assert orphans(checkout.journal.read()) == ["work"]
    assert composed[0].ctx.worktree("work").exists()
    assert audit_journal(checkout.journal) == []
    return checkout, composed[0]


@pytest.mark.parametrize("binding", ["intact", "forwarding", "application"])
def test_live_drain_kill_reaches_production_driver(root, monkeypatch, capsys, binding):
    if binding != "intact":
        abort = stages.StageContext.abort_current
        async def broken(ctx):
            if binding == "forwarding":
                return
            [request] = requests(root)
            ctx.driver.journal.append(EventType.SIGNAL,
                applied(request["request_id"], request["lifecycle_id"]))
            await abort(ctx)
        monkeypatch.setattr(stages.StageContext, "abort_current", broken)
        with pytest.raises(AssertionError):
            exercise_live_cli_kill(root, monkeypatch)
        return
    exercise_live_cli_kill(root, monkeypatch)
    output = capsys.readouterr().out
    assert "kill submitted: " in output and "drain stopped by kill" in output
    assert "uv run python -m tempo drain" in output


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["fresh", "retry", "machine-keep", "premise", "preparation",
                                 "stage-preparation", "verification", "between-calls"])
async def test_kill_stops_before_all_offer_accounting(root, monkeypatch, kind):
    if kind in {"stage-preparation", "verification", "between-calls"}:
        await kill_between_calls(root, monkeypatch, kind)
        return
    baseline = asyncio.all_tasks()
    live = LiveDrain(root, monkeypatch)
    c = live.checkout
    write(root, "work", confirmed())
    await c.git.add(root, ["tickets/work/ticket.md"])
    await c.git.commit(root, "ticket")
    if kind in {"retry", "machine-keep", "premise"}:
        c.journal.append(EventType.STATE_TRANSITION, {"to": "running", "ticket_sha": "older"}, ticket="work")
        body = {"to": "premise_failed" if kind == "premise" else "gate_failed"}
        c.journal.append(EventType.STATE_TRANSITION, body, ticket="work")
    if kind == "machine-keep":
        c.journal.append(EventType.SIGNAL, {"signal": "reject_arrival"}, ticket="work")
    before = offer_events(root)
    writer = runner.bind(c, FakeLLM([]))
    prepared, release = asyncio.Event(), asyncio.Event()
    async def prepare():
        prepared.set()
        await release.wait()
    task = asyncio.create_task(live.run(writer, before_dispatch=prepare if kind == "preparation" else None))
    await live.held.get()
    if kind == "preparation":
        await live.resume()
        await prepared.wait()
    live.request("kill", "kill")
    release.set()
    report = await task
    assert report.killed and report.exit_code == 1 and report.halted is None
    assert offer_events(root) == before and writer.ctx.driver.llm.requests == []
    assert applications(c.journal) == [applied("kill", live.consumer.inbox.lifecycle_id)]
    assert live.trace[-2:] == [("discovery", None), "unlock"]
    assert asyncio.all_tasks() == baseline


async def kill_between_calls(root, monkeypatch, kind):
    baseline = asyncio.all_tasks()
    live = LiveDrain(root, monkeypatch, pause=False)
    c = live.checkout
    write(root, "work", confirmed().replace("uv run pytest", "true"))
    await c.git.add(root, ["tickets/work/ticket.md"])
    await c.git.commit(root, "ticket")
    class Editing(FakeLLM):
        async def call(self, req):
            if req.surface == "implement":
                c.fs.write(req.worktree / "tempo/thing.py", b"ok\n")
                await c.git.add(req.worktree, ["tempo/thing.py"])
                await c.git.commit(req.worktree, "work")
            return await super().call(req)
    llm = Editing([implement_reply(), verdict()])
    writer = runner.bind(c, llm)
    writer.ctx.driver.sleep = Timer()
    entered, never = asyncio.Event(), asyncio.Event()
    cleaned = []
    async def barrier():
        entered.set()
        try:
            await never.wait()
        finally:
            cleaned.append(True)
    if kind == "stage-preparation":
        original = c.git.worktree_add
        async def held(*args, **kwargs):
            await original(*args, **kwargs)
            await barrier()
        monkeypatch.setattr(c.git, "worktree_add", held)
    elif kind == "verification":
        original = c.exec_.run
        async def held(argv, **kwargs):
            if list(argv) == ["true"]:
                await barrier()
            return await original(argv, **kwargs)
        monkeypatch.setattr(c.exec_, "run", held)
    else:
        original = stages.review
        async def held(*args, **kwargs):
            await barrier()
            return await original(*args, **kwargs)
        monkeypatch.setattr(stages, "review", held)
    task = asyncio.create_task(live.run(writer))
    try:
        await entered.wait()
        assert writer.ctx.driver._active is None
        live.request("kill", "kill")
        report = await task
        assert report.killed and report.exit_code == 1 and report.halted is None
        assert llm.aborted == 0
        assert len(llm.requests) == (0 if kind == "stage-preparation" else 1)
        assert applications(c.journal) == [applied("kill", live.consumer.inbox.lifecycle_id)]
        events = c.journal.read()
        accepted = next(i for i, e in enumerate(events) if e.body.get("kind") == control.CONTROL_DECISION)
        assert all(e.body.get("kind") == daemon.KILL_APPLIED for e in events[accepted + 1:])
        assert audit_journal(c.journal) == []
        assert cleaned == [True] and orphans(events) == ["work"]
        assert writer.ctx.worktree("work").exists()
        assert live.trace[-2:] == [("discovery", None), "unlock"]
    finally:
        never.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert asyncio.all_tasks() == baseline


class ActiveKill:
    """Production writer in the merged harness, with held executor and dispatch cleanup."""
    def __init__(self, root, monkeypatch):
        self.live = LiveDrain(root, monkeypatch, pause=False)
        self.c = self.live.checkout
        self.llm = Writer(held=True)
        self.writer = runner.bind(self.c, self.llm)
        self.llm.driver = self.writer.ctx.driver
        self.timer = Timer()
        self.writer.ctx.driver.sleep = self.timer
        self.cleaning, self.release = asyncio.Event(), asyncio.Event()
        self.cleaned = False
        self.dispatch_task = None
        self.failure = None
        drive = runner.drive
        async def observed(ctx, work):
            self.dispatch_task = asyncio.current_task()
            try:
                return await drive(ctx, work)
            finally:
                async def cleanup():
                    self.cleaning.set()
                    await self.release.wait()
                    self.cleaned = True
                    if self.failure is not None:
                        raise self.failure
                await daemon._protected_cleanup(asyncio.create_task(cleanup()))
        monkeypatch.setattr(runner, "drive", observed)

    async def start(self):
        write(self.c.repo, "work", confirmed())
        await self.c.git.add(self.c.repo, ["tickets/work/ticket.md"])
        await self.c.git.commit(self.c.repo, "ticket")
        self.task = asyncio.create_task(self.live.run(self.writer))
        await self.llm.started.wait()
        await self.timer.started.wait()

    async def stop(self):
        self.live.request("kill", "kill")
        await self.llm.unwinding.wait()

    async def finish(self):
        self.llm.release.set()
        self.release.set()
        await asyncio.gather(self.task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["accepted", "applied", "before-append", "after-append",
                                  "undecided", "stale", "rejected", "foreign-projection"])
async def test_kill_application_is_once_and_identity_bound(root, monkeypatch, point):
    baseline = asyncio.all_tasks()
    if point in {"undecided", "stale", "rejected", "foreign-projection"}:
        live = LiveDrain(root, monkeypatch, pause=False)
        c, consumer = live.checkout, live.consumer
        write(root, "work", confirmed().replace("uv run pytest", "true"))
        await c.git.add(root, ["tickets/work/ticket.md"])
        await c.git.commit(root, "ticket")
        if point == "foreign-projection":
            consumer.projection = ControlProjection("other-life", kill_requested=True)
        else:
            publish_request(c.config.state_dir, ControlRequest("kill",
                "prior-life" if point == "stale" else consumer.inbox.lifecycle_id,
                "kill", None), c.fs)
            if point == "undecided":
                consumer.inbox.files = lambda: ()
            if point == "rejected":
                (c.config.state_dir / "control/inbox/kill.json").write_bytes(b"{}")
        llm = FakeLLM([implement_reply("already_satisfied"),
                      json.dumps({"verdict": "abandon-human", "lessons": ["Preserve the terminal."]})])
        writer = runner.bind(c, llm)
        writer.ctx.driver.sleep = Timer()
        report = await live.run(writer)
        assert not report.killed and report.exit_code == 0
        assert llm.aborted == 0 and applications(c.journal) == []
        decisions = [e.body for e in c.journal.read() if e.body.get("kind") == control.CONTROL_DECISION]
        assert [b["decision"] for b in decisions] == ([point] if point in {"stale", "rejected"} else [])
        assert last_states(c.journal.read())["work"] == "already_satisfied"
        assert asyncio.all_tasks() == baseline
        return
    rig = ActiveKill(root, monkeypatch)
    await rig.start()
    c, consumer = rig.c, rig.live.consumer
    try:
        await rig.stop()
        rig.live.request("kill-two", "kill")
        assert applications(c.journal) == [] and not rig.task.done()
        rig.llm.release.set()
        await rig.cleaning.wait()
        assert applications(c.journal) == []
        rig.release.set()
        assert (await rig.task).killed
        life = consumer.inbox.lifecycle_id
        expected = [applied("kill", life), applied("kill-two", life)]
        assert applications(c.journal) == expected and rig.llm.aborts == 1
        assert all(p.exists() for p in (c.config.state_dir / "control/inbox").glob("*.json"))

        # Reconstruct from real acceptance/application crash points in a disposable state.
        state = c.repo / ".tempo/recovery"
        journal = Journal(state, c.clock)
        for event in c.journal.read():
            if event.body.get("kind") == control.CONTROL_DECISION:
                journal.append(event.type, event.body, ticket=None, key=None)
        if point == "applied":
            for body in expected:
                journal.append(EventType.SIGNAL, body)
        def reconstruct(lifecycle=life):
            return daemon.PauseConsumer(journal=journal, lifecycle_id=lifecycle, state_dir=state,
                fs=c.fs, sleep=c.sleep, files=lambda: (), read=Path.read_bytes)
        recovered = reconstruct()
        recovered.inbox.recover()
        aborts = []
        async def abort():
            aborts.append(True)
        append = journal.append
        if point in {"before-append", "after-append"}:
            def failed(type, body, **kwargs):
                if body.get("kind") == daemon.KILL_APPLIED:
                    if point == "after-append":
                        append(type, body, **kwargs)
                    raise OSError("application append failed")
                return append(type, body, **kwargs)
            monkeypatch.setattr(journal, "append", failed)
            with pytest.raises(OSError, match="application append failed"):
                await recovered.apply_kill(abort)
            assert applications(journal) == ([] if point == "before-append" else expected[:1])
            monkeypatch.setattr(journal, "append", append)
        await recovered.apply_kill(abort)
        await recovered.apply_kill(abort)
        again = reconstruct()
        again.inbox.recover()
        await again.apply_kill(abort)
        assert applications(journal) == expected
        fresh = reconstruct("next-life")
        fresh.inbox.recover()
        before = list(aborts)
        await fresh.apply_kill(abort)
        assert not fresh.projection.kill_requested and aborts == before
        assert applications(journal) == expected
    finally:
        await rig.finish()
    assert asyncio.all_tasks() == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "executor", "executor-refused", "dispatch", "decision", "application"])
async def test_kill_cleanup_is_atomic_and_failures_propagate(root, monkeypatch, failure):
    baseline = asyncio.all_tasks()
    if failure == "executor-refused":
        live = LiveDrain(root, monkeypatch, pause=False)
        c = live.checkout
        write(root, "work", confirmed())
        await c.git.add(root, ["tickets/work/ticket.md"])
        await c.git.commit(root, "ticket")
        started, finish, refused = (asyncio.Event() for _ in range(3))
        calls, dispatches = [], []
        class Ending(FakeLLM):
            async def call(self, req):
                calls.append(req)
                started.set()
                await finish.wait()
                raise asyncio.CancelledError("provider ended independently")
            def abort_current(self):
                pytest.fail("failed abort cancelled the active executor")
        writer = runner.bind(c, Ending([]))
        writer.ctx.driver.sleep = Timer()
        async def abort(_):
            refused.set()
            raise RuntimeError("executor refused")
        monkeypatch.setattr(stages.StageContext, "abort_current", abort)
        drive = runner.drive
        async def observed(ctx, work):
            dispatches.append(asyncio.current_task())
            return await drive(ctx, work)
        monkeypatch.setattr(runner, "drive", observed)
        task = asyncio.create_task(live.run(writer))
        try:
            await started.wait()
            live.request("kill", "kill")
            await refused.wait()
            await turn()
            assert live.consumer.projection.kill_requested and not task.done()
            assert dispatches[0].cancelling() == 0 and not dispatches[0].done()
            assert applications(c.journal) == [] and "unlock" not in live.trace
            finish.set()
            with pytest.raises(RuntimeError, match="executor refused"):
                await task
            assert len(calls) == 1 and writer.ctx.driver._active is None
            assert applications(c.journal) == [] and orphans(c.journal.read()) == ["work"]
            assert writer.ctx.worktree("work").exists() and audit_journal(c.journal) == []
            assert live.trace[-2:] == [("discovery", None), "unlock"]
        finally:
            finish.set()
            await asyncio.gather(task, return_exceptions=True)
        assert asyncio.all_tasks() == baseline
        return
    if failure == "decision":
        live = LiveDrain(root, monkeypatch, pause=False)
        c = live.checkout
        write(root, "work", confirmed().replace("uv run pytest", "true"))
        await c.git.add(root, ["tickets/work/ticket.md"])
        await c.git.commit(root, "ticket")
        started, finish = asyncio.Event(), asyncio.Event()
        class Completing(FakeLLM):
            async def call(self, req):
                started.set()
                await finish.wait()
                return await super().call(req)
            def abort_current(self):
                pytest.fail("failed decision authorized executor mutation")
        llm = Completing([implement_reply("already_satisfied"),
                          json.dumps({"verdict": "abandon-human", "lessons": ["Keep the settled terminal."]})])
        writer = runner.bind(c, llm)
        writer.ctx.driver.sleep = Timer()
        append = c.journal.append
        def failed(type, body, **kwargs):
            if body.get("kind") == control.CONTROL_DECISION:
                raise RuntimeError("decision append failed")
            return append(type, body, **kwargs)
        monkeypatch.setattr(c.journal, "append", failed)
        task = asyncio.create_task(live.run(writer))
        try:
            await started.wait()
            live.request("kill", "kill")
            await turn()
            await turn()
            assert not live.consumer.projection.kill_requested and not task.done()
            assert applications(c.journal) == [] and writer.ctx.driver._active is not None
            finish.set()
            with pytest.raises(RuntimeError, match="decision append failed"):
                await task
            assert llm.aborted == 0 and [r.surface for r in llm.requests] == ["implement", "diagnose"]
            assert last_states(c.journal.read())["work"] == "already_satisfied"
            assert applications(c.journal) == [] and audit_journal(c.journal) == []
            assert live.trace[-2:] == [("discovery", None), "unlock"]
        finally:
            finish.set()
            await asyncio.gather(task, return_exceptions=True)
        assert asyncio.all_tasks() == baseline
        return
    rig = ActiveKill(root, monkeypatch)
    await rig.start()
    consumer, c = rig.live.consumer, rig.c
    append = c.journal.append
    error = RuntimeError("cleanup refused")
    if failure == "application":
        def fail(type, body, **kwargs):
            if body.get("kind") == daemon.KILL_APPLIED:
                raise error
            return append(type, body, **kwargs)
        monkeypatch.setattr(c.journal, "append", fail)
    if failure == "executor":
        original = rig.writer.ctx.abort_current
        async def fail():
            await original()
            raise error
        monkeypatch.setattr(stages.StageContext, "abort_current", lambda _: fail())
    if failure == "dispatch":
        rig.failure = error
    try:
        rig.live.request("kill", "kill")
        await rig.llm.unwinding.wait()
        assert consumer.projection.kill_requested
        assert applications(c.journal) == []
        assert "unlock" not in rig.live.trace
        if failure is None:
            for _ in range(3):
                rig.task.cancel()
                await turn()
                assert not rig.task.done() and applications(c.journal) == []
                assert rig.dispatch_task.cancelling() == 0
                assert rig.llm.cancels == 1 and "unlock" not in rig.live.trace
        rig.llm.release.set()
        await rig.cleaning.wait()
        assert applications(c.journal) == [] and "unlock" not in rig.live.trace
        rig.release.set()
        if failure is None:
            with pytest.raises(asyncio.CancelledError):
                await rig.task
            assert applications(c.journal) == [applied("kill", consumer.inbox.lifecycle_id)]
        else:
            with pytest.raises(RuntimeError, match="cleanup refused"):
                await rig.task
            assert applications(c.journal) == []
        assert rig.cleaned and rig.writer.ctx.driver._active is None
        assert not rig.task._log_traceback
        assert rig.live.trace[-2:] == [("discovery", None), "unlock"]
    finally:
        await rig.finish()
    assert asyncio.all_tasks() == baseline


@pytest.mark.parametrize("terminal", [False, True])
def test_killed_run_is_terminal_or_restart_reconcilable(root, monkeypatch, terminal):
    if not terminal:
        with monkeypatch.context() as patch:
            c, writer = exercise_live_cli_kill(root, patch)
        producing = c.journal.read()
        intents = {e.key for e in producing if e.type == EventType.EFFECT_INTENT and e.key.startswith("llm/")}
        assert intents and not any(e.type == EventType.EFFECT_COMPLETION and e.key in intents for e in producing)
        assert last_states(producing)["work"] == "running"
        write(root, "next", confirmed())
        callback = Stages()
        assert entry.main(["run", "next"], cwd=root, env=ENV, clock=c.clock, pipeline=callback) == 0
        events = c.journal.read()
        abandoned = next(e for e in events if e.ticket == "work" and e.body.get("to") == "abandoned")
        next_run = next(e for e in events if e.ticket == "next" and e.body.get("to") == "running")
        assert events.index(abandoned) < events.index(next_run)
        harvest = root / "tickets/work/attempts/0/harvest.json"
        assert json.loads(harvest.read_bytes())["terminal"] == "abandoned"
        lift = next(e for e in events if e.type == EventType.EFFECT_COMPLETION and
                    e.body.get("result", {}).get("kind") == "harvest")
        assert events.index(lift) < events.index(abandoned)
        assert not writer.ctx.worktree("work").exists()
        assert all(e in events for e in producing)
        assert not orphans(events)
    else:
        async def completed():
            live = LiveDrain(root, monkeypatch, pause=False)
            c = live.checkout
            write(root, "work", confirmed().replace("uv run pytest", "true"))
            await c.git.add(root, ["tickets/work/ticket.md"])
            await c.git.commit(root, "ticket")
            writer = runner.bind(c, FakeLLM([agent({}, outcome="already_satisfied")]))
            append = c.journal.append
            def terminal_then_kill(type, body, **kwargs):
                event = append(type, body, **kwargs)
                if body.get("to") == "already_satisfied":
                    live.request("kill", "kill")
                return event
            monkeypatch.setattr(c.journal, "append", terminal_then_kill)
            report = await live.run(writer)
            assert report.killed and report.exit_code == 1
            assert last_states(c.journal.read())["work"] == "already_satisfied"
            assert not orphans(c.journal.read()) and audit_journal(c.journal) == []
            assert not writer.ctx.worktree("work").exists()
        asyncio.run(completed())


@pytest.mark.parametrize("boundary", ["stop", "observe"])
def test_kill_activation_keeps_worker_boundaries_dormant(root, tmp_path, monkeypatch, boundary):
    cls = daemon.WorkerStop if boundary == "stop" else daemon.WorkerFailureObserver
    async def probe(*args, **kwargs):
        raise AssertionError("worker boundary activated")
    monkeypatch.setattr(cls, boundary, probe)
    for verb in ("run", "drain"):
        stem = "work-" + verb
        write(root, stem, confirmed().replace("uv run pytest", "true"))
        def wired(checkout):
            writer = runner.bind(checkout, FakeLLM([]))
            async def dispatch(work):
                await getattr(cls, boundary)(None)
            return dispatch
        args = [verb, stem] if verb == "run" else [verb]
        with pytest.raises(AssertionError, match="worker boundary activated"):
            entry.main(args, cwd=root, env=ENV, clock=Clock(), pipeline=wired)
        def callback(checkout):
            return runner.bind(checkout, FakeLLM([agent({}, outcome="already_satisfied")]))
        assert entry.main(args, cwd=root, env=ENV, clock=Clock(), pipeline=callback) == (1 if verb == "run" else 0)
    async def composed():
        baseline = asyncio.all_tasks()
        directory = tmp_path / "composition"
        directory.mkdir()
        async def prepare(local):
            writer = runner.bind(local, FakeLLM([]))
            async def dispatch(work):
                return "merged"
            return dispatch
        rig = CoreRig(directory, prepare=prepare)
        assert_core_wiring(rig)
        assert rig.fs.files == {} and rig.exec.calls == [] and rig.journal.read() == []
        assert asyncio.all_tasks() == baseline
        work = await rig.add("composed")
        original = rig.core.admission._dispatch
        async def deliberate(work):
            await getattr(cls, boundary)(None)
        rig.core.admission._dispatch = deliberate
        with pytest.raises(AssertionError, match="worker boundary activated"):
            await rig.core.admission.dispatch(work)
        rig.core.admission._dispatch = original
        await rig.drain()
        assert asyncio.all_tasks() == baseline
    asyncio.run(composed())
    # An accepted kill still reaches only the executor boundary, never worker stop.
    with monkeypatch.context() as patch:
        exercise_live_cli_kill(root, patch)
