"""Checkpoint custody through the existing journal, process, timer and production Box seams."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tempo import __main__ as cli, daemon
from tempo.checkpoint import CHECKPOINT_INTERVAL, CHECKPOINT_MERGES, Checkpoint
from tempo.effects import Effects
from tempo.enginelog import EngineLog
from tempo.git import Git
from tempo.journal import EventType, Journal, JournalCorruption
from tempo.redact import Redactor
from tempo.seams import ExecutableNotFound, LocalFileSystem
from tempo.storm import StormLedger, arrival_id
from tempo.timers import Timers
from tests.test_cli import Clock, ENV, Stages, root, ticket, write
from tests.test_daemon_composition import CoreRig, assert_core_wiring
from tests.test_restart_timers import repository
from tempo import storm

T0 = datetime(2026, 10, 7, tzinfo=UTC)
SECRET = "private-checkpoint-key"


class Time:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    async def sleep(self, _):
        raise AssertionError("checkpoint polling must not wait")


class Exec:
    def __init__(self):
        self.calls, self.results = [], []
        self.hook = None

    async def run(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.hook is not None:
            await self.hook()
        result = self.results.pop(0) if self.results else (0, "", "")
        if isinstance(result, BaseException):
            raise result
        return result


class Rig:
    def __init__(self, path):
        self.path, self.time, self.exec = path, Time(), Exec()
        self.fs = LocalFileSystem()
        self.git = Git(self.exec, env={"PATH": "/usr/bin"}, timeout=30)
        self.log = EngineLog(path / "engine.log", Redactor({"KEY": SECRET}), self.time)
        self.slot = asyncio.Lock()
        self.restart()

    def restart(self):
        self.journal = Journal(self.path, self.time)
        self.effects = Effects(self.journal)
        self.timers = Timers(journal=self.journal, clock=self.time, sleep=self.time.sleep)
        self.box = storm.storm_producer(root=self.path / "box", fs=self.fs,
                                         journal=self.journal, clock=self.time)
        self.boundary = daemon.checkpoint_push(self.path, journal=self.journal,
            effects=self.effects, timers=self.timers, git=self.git, box=self.box,
            clock=self.time, log=self.log)

    async def poll(self):
        async with self.slot:
            await self.boundary.poll()

    def merges(self, count):
        for n in range(count):
            self.journal.append(EventType.STATE_TRANSITION,
                                {"to": "merged", "commit": f"code-{n}"}, ticket=f"ticket-{n}")

    def events(self, type=None, kind=None):
        return [event for event in self.journal.read()
                if (type is None or event.type == type)
                and (kind is None or event.body.get("kind") == kind)]

    def failures(self):
        return self.events(kind="checkpoint_push_failed")

    def occurrences(self):
        return self.events(kind="storm_occurrence")


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["merges", "quiet-day", "both"])
async def test_checkpoint_merge_and_daily_triggers(tmp_path, trigger):
    rig = Rig(tmp_path)
    assert CHECKPOINT_MERGES == 5 and CHECKPOINT_INTERVAL == timedelta(hours=24)
    assert rig.events() == [] and not tmp_path.joinpath("journal").exists()
    await rig.poll()
    assert rig.exec.calls == []
    rig.journal.append(EventType.SIGNAL, {"signal": "ticket_intake", "commit": "ticket-plane"})
    rig.journal.append(EventType.STATE_TRANSITION, {"to": "running", "commit": "not-admitted"})
    if trigger != "quiet-day":
        rig.merges(4)
    await rig.poll()
    assert rig.exec.calls == []
    rig.time.now = T0 + CHECKPOINT_INTERVAL - timedelta(microseconds=1)
    await rig.poll()
    assert rig.exec.calls == []
    if trigger != "quiet-day":
        rig.merges(1)
    if trigger != "merges":
        rig.time.now += timedelta(microseconds=1)
    await rig.poll()
    assert len(rig.exec.calls) == 1
    argv, kwargs = rig.exec.calls[0]
    assert argv == ["git", "-C", str(tmp_path), "push", "origin", "main"]
    assert kwargs == {"cwd": tmp_path, "env": {"PATH": "/usr/bin"}, "timeout": 30}
    assert rig.events(EventType.EFFECT_COMPLETION)[0].body == {
        "result": {"merged": 0 if trigger == "quiet-day" else 5}}


@pytest.mark.asyncio
async def test_checkpoint_success_advances_once(tmp_path):
    rig = Rig(tmp_path)
    rig.merges(5)
    await rig.poll()
    intent, = rig.events(EventType.EFFECT_INTENT)
    done, = rig.events(EventType.EFFECT_COMPLETION)
    assert intent.body == {} and intent.key == done.key == "checkpoint-push/0"
    assert intent.ticket is done.ticket is None
    assert done.body == {"result": {"merged": 5}}
    assert rig.events().index(intent) < rig.events().index(done)
    rig.time.now += timedelta(hours=3)
    await rig.poll()
    assert rig.timers.pending["checkpoint-push/1"].deadline == T0 + CHECKPOINT_INTERVAL
    rig.restart()
    rig.merges(4)
    await rig.poll()
    assert len(rig.exec.calls) == 1
    rig.merges(1)
    await rig.poll()
    await rig.poll()
    rig.restart()
    await rig.poll()
    assert len(rig.exec.calls) == 2
    assert [(e.key, e.body) for e in rig.events(EventType.EFFECT_COMPLETION)] == [
        ("checkpoint-push/0", {"result": {"merged": 5}}),
        ("checkpoint-push/1", {"result": {"merged": 10}})]
    assert rig.timers.pending["checkpoint-push/2"].deadline == rig.time.now + CHECKPOINT_INTERVAL


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,code", [
    ((9, SECRET + " stdout", SECRET + " stderr"), "git_exit"),
    (TimeoutError(SECRET), "process_failure"),
    (ExecutableNotFound(SECRET), "process_failure"),
    (RuntimeError(SECRET), "process_failure")])
async def test_checkpoint_failed_push_refires(tmp_path, failure, code):
    rig = Rig(tmp_path)
    rig.merges(5)
    rig.exec.results = [failure, failure]
    for _ in range(2):
        await rig.poll()
        assert rig.events(EventType.EFFECT_COMPLETION) == []
    message, = rig.box.messages()
    assert message.message_class == "failure_report" and message.origin == "checkpoint-push"
    assert message.stage is message.outcome is None
    assert "origin/main" in message.summary and code in message.summary
    assert "repair the remote or credentials" in message.summary
    log = (tmp_path / "engine.log").read_text()
    assert SECRET not in log and "[REDACTED:KEY]" in log
    assert SECRET not in str(rig.events()) and SECRET not in message.model_dump_json()
    assert [e.body["attempt"] for e in rig.failures()] == [1, 2]
    rig.restart()
    await rig.poll()
    assert len(rig.exec.calls) == 3 and len(rig.box.messages()) == 1
    assert rig.events(EventType.EFFECT_COMPLETION)[0].key == "checkpoint-push/0"
    assert [e.key for e in rig.events(EventType.EFFECT_INTENT)] == ["checkpoint-push/0"] * 3
    assert rig.timers.pending["checkpoint-push/0"].deadline == T0 + CHECKPOINT_INTERVAL


@pytest.mark.asyncio
async def test_checkpoint_failure_occurrence_identity(tmp_path, monkeypatch):
    import tempo.journal as module

    monkeypatch.setattr(module, "ROLL_BYTES", 1)
    rig = Rig(tmp_path)
    rig.merges(5)
    rig.exec.results = [(1, "", "failure")] * 7
    for _ in range(4):
        await rig.poll()
    rig.restart()
    for _ in range(3):
        await rig.poll()
    assert [e.body["attempt"] for e in rig.failures()] == list(range(1, 8))
    assert len(list(rig.journal.read_segments())) > 1
    assert len(rig.box.messages()) == 2  # one source Message and one soft storm report
    source = next(m for m in rig.box.messages() if m.origin == "checkpoint-push")
    assert source.stage is source.outcome is None
    ledger = StormLedger(journal=rig.journal, clock=rig.time)
    assert ledger.holds() == {}
    [trip] = rig.events(kind="storm_breaker_trip")
    assert trip.body["held"] is None
    for attempt in range(1, 8):
        recipe = "storm-arrival/" + hashlib.sha256(json.dumps(
            ["checkpoint-push", 0, attempt], separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        assert recipe == arrival_id("checkpoint-push", 0, attempt)
        event = next(e for e in rig.occurrences() if e.body["occurrence_id"] == recipe)
        assert event.body == {"kind": "storm_occurrence", "signature": source.signature,
            "occurrence_id": recipe, "emitting_stage": None, "emitting_origin": "checkpoint-push"}
    occurrences = rig.occurrences()
    await rig.poll()  # replay then success
    await rig.poll()  # completed window replay only
    assert rig.occurrences() == occurrences
    rig.time.now += timedelta(hours=25)
    rig.restart()
    rig.exec.results = [(1, "", "failure")]
    await rig.poll()
    assert rig.failures()[-1].body["window"] == 1 and rig.failures()[-1].body["attempt"] == 1
    assert arrival_id("checkpoint-push", 1, 1) != arrival_id("checkpoint-push", 0, 1)
    assert len([m for m in rig.box.messages() if m.origin == "checkpoint-push"]) == 1
    assert rig.occurrences()[:len(occurrences)] == occurrences
    assert ledger.holds() == {}


class Crash(BaseException):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("point", [
    "before-failure", "after-failure", "after-callback", "after-box", "duplicate",
    *("error-" + point for point in ["intent", "completion", "failure", "callback", "box-read",
                                    "box-write", "corruption", "push-corruption"]),
    *("invalid-" + defect for defect in ["shape", "key", "ticket", "window", "attempt", "bool",
                                        "code", "type", "conflict", "no-intent", "late-intent"])])
async def test_checkpoint_failure_report_crash_replay(tmp_path, monkeypatch, point):
    if point.startswith("error-"):
        await _failure_durable_errors(tmp_path, monkeypatch, point.removeprefix("error-"))
        return
    if point.startswith("invalid-"):
        await _failure_invalid_evidence(tmp_path, point.removeprefix("invalid-"))
        return
    if point == "duplicate":
        await _failure_duplicate(tmp_path)
        return
    rig = Rig(tmp_path)
    rig.merges(5)
    rig.exec.results = [(1, SECRET, SECRET)]
    appended = rig.journal.append
    written = rig.fs.write

    def append(type, body, **kwargs):
        failure = body.get("kind") == "checkpoint_push_failed"
        if failure:
            assert type == EventType.SIGNAL
            assert kwargs == {"ticket": None, "key": "checkpoint-push-failed/0/1"}
            assert body == {"kind": "checkpoint_push_failed", "window": 0,
                            "attempt": 1, "error_code": "git_exit"}
            assert rig.events(EventType.EFFECT_COMPLETION) == []
        if failure and point == "before-failure":
            raise Crash()
        result = appended(type, body, **kwargs)
        if (failure and point == "after-failure") or (
                body.get("kind") == "storm_occurrence" and point == "after-callback"):
            raise Crash()
        return result

    def write_box(path, data):
        written(path, data)
        if path.parent == rig.box.root and point == "after-box":
            raise Crash()

    with monkeypatch.context() as patch:
        patch.setattr(rig.journal, "append", append)
        patch.setattr(rig.fs, "write", write_box)
        with pytest.raises(Crash):
            await rig.poll()
    assert rig.events(EventType.EFFECT_COMPLETION) == [] and len(rig.exec.calls) == 1
    assert len(rig.failures()) == (0 if point == "before-failure" else 1)
    rig.restart()
    await rig.poll()
    assert len(rig.exec.calls) == 2
    assert len(rig.box.messages()) == (0 if point == "before-failure" else 1)
    assert len(rig.occurrences()) == (0 if point == "before-failure" else 1)
    assert rig.events(EventType.EFFECT_COMPLETION)[0].body == {"result": {"merged": 5}}
    events = rig.events()
    await rig.poll()
    assert len(rig.exec.calls) == 2 and rig.occurrences() == [
        e for e in events if e.body.get("kind") == "storm_occurrence"]
    rig.time.now += timedelta(hours=2)  # expiry, below the next daily trigger
    rig.restart()
    await rig.poll()
    assert len(rig.exec.calls) == 2
    assert rig.failures() == [e for e in events if e.body.get("kind") == "checkpoint_push_failed"]
    assert SECRET not in str(rig.events())


async def _failure_durable_errors(tmp_path, monkeypatch, point):
    rig = Rig(tmp_path)
    rig.merges(5)
    if point not in {"intent", "completion", "corruption"}:
        rig.exec.results = [(1, "", SECRET)]
    if point == "push-corruption":
        rig.exec.results = [JournalCorruption("bad journal")]
    appended = rig.journal.append
    written = rig.fs.write

    def append(type, body, **kwargs):
        selected = {"intent": EventType.EFFECT_INTENT, "completion": EventType.EFFECT_COMPLETION}
        if (type == selected.get(point) or
                (point == "failure" and body.get("kind") == "checkpoint_push_failed")):
            raise OSError("durable write failed")
        return appended(type, body, **kwargs)

    def broken(*args, **kwargs):
        raise OSError("durable Box failed")

    def write_box(path, data):
        if path.parent == rig.box.root:
            broken()
        written(path, data)

    def corrupt():
        raise JournalCorruption("bad journal")

    with monkeypatch.context() as patch:
        patch.setattr(rig.journal, "append", append)
        if point == "callback":
            patch.setattr(rig.box, "_arrival", broken)
        if point == "box-read":
            patch.setattr(rig.box, "messages", broken)
        if point == "box-write":
            patch.setattr(rig.fs, "write", write_box)
        if point == "corruption":
            patch.setattr(rig.journal, "read", corrupt)
        with pytest.raises(JournalCorruption if "corruption" in point else OSError):
            await rig.poll()
    assert rig.events(EventType.EFFECT_COMPLETION) == []
    assert len(rig.failures()) == (1 if point in {"callback", "box-read", "box-write"} else 0)
    assert rig.box.messages() == []


async def _failure_invalid_evidence(tmp_path, defect):
    rig = Rig(tmp_path)
    body = {"kind": "checkpoint_push_failed", "window": 0, "attempt": 1, "error_code": "git_exit"}
    key, ticket_, type_ = "checkpoint-push-failed/0/1", None, EventType.SIGNAL
    if defect not in {"no-intent", "late-intent"}:
        rig.journal.append(EventType.EFFECT_INTENT, {}, key="checkpoint-push/0")
    if defect == "conflict":
        rig.journal.append(EventType.SIGNAL, body.copy(), key=key)
        body["error_code"] = "process_failure"
    if defect == "shape":
        body["detail"] = SECRET
    if defect == "key":
        key = "checkpoint-push-failed/0/2"
    if defect == "ticket":
        ticket_ = "ticket"
    if defect == "window":
        body["window"] = -1
    if defect == "attempt":
        body["attempt"] = 0
    if defect == "bool":
        body["attempt"] = True
    if defect == "code":
        body["error_code"] = "other"
    if defect == "type":
        type_ = EventType.EFFECT_INTENT
    rig.journal.append(type_, body, ticket=ticket_, key=key)
    if defect == "late-intent":
        rig.journal.append(EventType.EFFECT_INTENT, {}, key="checkpoint-push/0")
    before = rig.events()
    with pytest.raises(ValueError, match="repair the producing evidence"):
        await rig.poll()
    assert rig.events() == before and rig.exec.calls == [] and rig.box.messages() == []


async def _failure_duplicate(tmp_path):
    rig = Rig(tmp_path)
    rig.merges(5)
    rig.exec.results = [(1, "", "bad")]
    await rig.poll()
    failure, = rig.failures()
    rig.journal.append(failure.type, failure.body, ticket=None, key=failure.key)
    await rig.poll()
    assert len(rig.occurrences()) == 1 and len(rig.exec.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["before-action", "after-action", "after-completion", "cancel-action"])
async def test_checkpoint_crash_windows(tmp_path, monkeypatch, point):
    rig = Rig(tmp_path)
    rig.merges(5)
    appended = rig.journal.append

    def append(type, body, **kwargs):
        if point == "after-action" and type == EventType.EFFECT_COMPLETION:
            raise Crash()
        result = appended(type, body, **kwargs)
        if ((point == "before-action" and type == EventType.EFFECT_INTENT)
                or (point == "after-completion" and type == EventType.EFFECT_COMPLETION)):
            raise Crash()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(rig.journal, "append", append)
        if point == "cancel-action":
            entered = asyncio.Event()
            async def barrier():
                entered.set()
                await asyncio.Event().wait()
            rig.exec.hook = barrier
            task = asyncio.create_task(rig.poll())
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            rig.exec.hook = None
        else:
            with pytest.raises(Crash):
                await rig.poll()
    assert not rig.failures() and not rig.box.messages()
    assert len(rig.exec.calls) == (0 if point == "before-action" else 1)
    rig.restart()
    await rig.poll()
    await rig.poll()
    assert len(rig.exec.calls) == (2 if point in {"after-action", "cancel-action"} else 1)
    assert len(rig.events(EventType.EFFECT_COMPLETION)) == 1
    assert len(rig.events(EventType.EFFECT_INTENT)) == (1 if point == "after-completion" else 2)


@pytest.mark.asyncio
async def test_checkpoint_timer_survives_restart_and_roll(tmp_path, monkeypatch):
    import tempo.journal as module

    monkeypatch.setattr(module, "ROLL_BYTES", 1)
    rig = Rig(tmp_path)
    await rig.poll()
    armed, = rig.events(EventType.TIMER_ARMED)
    assert armed.body == {"timer_id": "checkpoint-push/0", "deadline": (T0 + CHECKPOINT_INTERVAL).isoformat()}
    assert armed.ticket is armed.key is None
    rig.time.now += timedelta(hours=20)
    rig.restart()
    await rig.poll()
    assert rig.timers.pending["checkpoint-push/0"].deadline == T0 + CHECKPOINT_INTERVAL
    assert rig.exec.calls == []
    rig.time.now += timedelta(hours=4)
    rig.exec.results = [(1, "", "failed")]
    await rig.poll()
    fired, = rig.events(EventType.TIMER_FIRED)
    assert fired.body == armed.body and fired.ticket is fired.key is None
    rig.time.now += timedelta(hours=1)
    rig.restart()
    await rig.poll()
    assert len(rig.exec.calls) == 2 and len(rig.events(EventType.TIMER_FIRED)) == 1
    await rig.poll()
    assert len(rig.exec.calls) == 2
    assert rig.timers.pending["checkpoint-push/1"].deadline == rig.time.now + CHECKPOINT_INTERVAL
    assert len(list(rig.journal.read_segments())) > 1
    # A merge-triggered window's old daily timer may still fire afterwards.
    rig.time.now += timedelta(hours=1)
    rig.merges(5)
    await rig.poll()
    rig.time.now += timedelta(hours=23)
    rig.restart()
    await rig.poll()
    assert len(rig.exec.calls) == 3
    assert "checkpoint-push/1" in rig.timers.fired


@pytest.mark.parametrize("verb", ["run", "drain"])
def test_checkpoint_boundary_is_dormant(root, monkeypatch, verb):
    async def graph():
        tasks = asyncio.all_tasks()
        graph_root = root.parent / "graph"
        graph_root.mkdir()
        rig = CoreRig(graph_root)
        assert_core_wiring(rig)
        clock = Time()
        rig.journal._clock = clock
        exec_ = Exec()
        box = storm.storm_producer(root=root / "checkpoint-box", fs=LocalFileSystem(),
                                    journal=rig.journal, clock=clock)
        effects = Effects(rig.journal)
        timers = rig.core.restart.timers
        boundary = daemon.checkpoint_push(root, journal=rig.journal, effects=effects,
            timers=timers, git=Git(exec_, env={"PATH": "/usr/bin"}, timeout=30), box=box,
            clock=clock, log=EngineLog(root / "checkpoint.log", Redactor({}), clock))
        assert boundary.journal is rig.journal and boundary.effects is effects
        assert boundary.timers is timers and boundary.box is box
        assert rig.journal.read() == [] and rig.exec.calls == [] and asyncio.all_tasks() == tasks
        for _ in range(5):
            rig.transition("prior", "merged", commit="code")
        async with rig.core.admission._slot:
            await boundary.poll()
        assert len(exec_.calls) == 1

    asyncio.run(graph())
    def forbidden(*args, **kwargs):
        raise AssertionError("checkpoint boundary activated")

    monkeypatch.setattr(daemon, "checkpoint_push", forbidden)
    monkeypatch.setattr(Checkpoint, "poll", forbidden)
    monkeypatch.setattr(Git, "push", forbidden)
    write(root, "work", ticket())
    argv = ["run", "work"] if verb == "run" else ["drain"]
    stages = Stages()
    assert cli.main(argv, cwd=root, env=ENV, clock=Clock(), pipeline=stages) == 0
    assert stages.calls == ["work"] and stages.lock_held == [True]
    assert not any((e.key or "").startswith("checkpoint-push/")
                   for e in stages.checkout.journal.read())
    def activated(checkout):
        daemon.checkpoint_push(checkout.repo)
    with pytest.raises(AssertionError, match="checkpoint boundary activated"):
        cli.main(argv, cwd=root, env=ENV, clock=Clock(), pipeline=activated)
    asyncio.run(_dormant_core(root, monkeypatch))


async def _dormant_core(root, monkeypatch):
    tasks = asyncio.all_tasks()
    calls = []
    async def prepare(local):
        async def dispatch(ticket):
            calls.append(ticket.stem)
            return "merged"
        return dispatch
    rig = await repository(root, monkeypatch, prepare=prepare)
    assert_core_wiring(rig)
    assert asyncio.all_tasks() == tasks
    for _ in range(5):
        rig.transition("prior", "merged", commit="code")
    await rig.core.startup()
    await rig.add("next")
    await rig.drain()
    assert calls == ["next"]
    assert not any((e.key or "").startswith("checkpoint-push/") for e in rig.journal.read())


@pytest.mark.asyncio
async def test_push_failure_preserves_production_admission(root, monkeypatch):
    calls = []
    async def prepare(local):
        async def dispatch(ticket):
            calls.append(ticket.stem)
            return "merged"
        return dispatch
    rig = await repository(root, monkeypatch, prepare=prepare)
    await rig.core.startup()
    original = rig.exec.run
    async def execute(argv, **kwargs):
        if argv[3:4] == ["push"]:
            assert kwargs["env"] == ENV
            return 1, SECRET, SECRET
        return await original(argv, **kwargs)
    monkeypatch.setattr(rig.exec, "run", execute)
    box = storm.storm_producer(root=rig.checkout.config.state_dir / "box", fs=rig.fs,
                               journal=rig.journal, clock=rig.time)
    boundary = daemon.checkpoint_push(root, journal=rig.journal, effects=Effects(rig.journal),
        timers=rig.core.restart.timers, git=rig.checkout.git, box=box, clock=rig.time,
        log=EngineLog(root / "checkpoint.log", Redactor({"KEY": SECRET}), rig.time))
    for _ in range(5):
        rig.transition("prior", "merged", commit="code")
    before = await rig.checkout.git.rev_parse(root, "main")
    async with rig.core.admission._slot:
        await boundary.poll()
    assert await rig.checkout.git.rev_parse(root, "main") == before
    assert StormLedger(journal=rig.journal, clock=rig.time).holds() == {}
    await rig.add("next")
    await rig.drain()
    assert calls == ["next"] and not rig.core.admission._slot.locked()
    assert rig.core.control.projection.pause_id is None
    assert not any(e.type == EventType.EFFECT_COMPLETION and e.key == "checkpoint-push/0"
                   for e in rig.journal.read())


@pytest.mark.asyncio
async def test_checkpoint_is_active_in_serve(root, monkeypatch):
    from tests.test_serve import activated_graph
    rig = await activated_graph(root, monkeypatch)
    assert rig.owner.checkpoint.timers is rig.owner.core.restart.timers
    assert "checkpoint-push/0" in rig.owner.checkpoint.timers.pending
