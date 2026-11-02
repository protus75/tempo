"""Identity-bound holds through the production bootstrap CLI and drain composition."""

import asyncio
import json
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock

import pytest
import pytest_asyncio

from tempo import __main__ as cli, caps, daemon, journal as journal_module
from tempo.control import CONTROL_DECISION, ControlRequest, publish_request, read_active
from tempo.drain import HALT_SIGNAL, drain
from tempo.journal import EventType, Journal, JournalCorruption
from tempo.lockfile import Lockfile, LockHeld
from tempo.seams import LocalFileSystem
from tempo.storm import StormLedger
from tempo.tickets import INTAKE_SIGNAL, ticket_path
from tests.test_cli import ENV
from tests.test_daemon_composition import CoreRig
from tests.test_drain import Script, commit_ticket, confirmed, make_root, pause_checkout
from tests.test_drain_reentry import NoChild
from tests.test_journal import FakeClock, T0
from tests.test_storm import SIG, snapshot
from tests.test_storm_notification_activation import digest, occurrences, six, trips
from tests.test_storm_producer import ARRIVAL


class LiveStorm:
    """The existing production checkout harness with barrier-driven control delivery."""

    def __init__(self, root):
        self.root = root
        self.clock = FakeClock(T0)
        self.waiting, self.wake = asyncio.Queue(), asyncio.Queue()
        self.calls, self.tasks = [], []
        self.aborted = 0
        self.hook = None
        self.cleaned = asyncio.Event()
        self.rebuild()

    def rebuild(self):
        checkout = pause_checkout(self.root)
        self.c = replace(checkout, clock=self.clock, journal=Journal(checkout.config.state_dir, self.clock),
                         sleep=self.sleep, control=None)
        self.c = replace(self.c, control=cli.build_control(self.c))
        self.consumer = self.c.control
        self.box = self.consumer._recover.__self__
        assert self.box._arrival.__self__.journal is self.c.journal
        assert self.consumer.inbox.journal is self.c.journal

    @property
    def holds(self):
        return self.consumer.storm_holds()

    @property
    def decisions(self):
        return [e for e in self.c.journal.read() if e.body.get("kind") == CONTROL_DECISION]

    @property
    def offers(self):
        return [e for e in self.c.journal.read()
                if e.type in {EventType.CAP_CONSUMED, EventType.STATE_TRANSITION}
                or e.body.get("signal") == "reject_verdict"]

    def trip(self, *, origin="one", stage="implement", reason=None, prefix="arrival"):
        incoming = dict(ARRIVAL, origin=origin, stage=stage,
                        outcome=None if stage is None else "gate_failed")
        if reason is not None:
            incoming["reason"] = reason
        for n in range(6):
            self.box.enqueue(**incoming, occurrence_id=f"{prefix}/{n}")
        return trips(self.c.journal)[-1].body["trip_id"]

    def request(self, identity, verb="resume", hold=None, life=None, *, wake=True):
        publish_request(self.c.config.state_dir,
                        ControlRequest(identity, life or self.consumer.inbox.lifecycle_id, verb, hold), self.c.fs)
        if wake:
            self.wake.put_nowait(None)

    async def sleep(self, delay):
        assert delay == .1
        contender = Lockfile(self.c.config.state_dir, instance_id="contender", clock=self.clock)
        with pytest.raises(LockHeld):
            contender.acquire()
        self.waiting.put_nowait(read_active(self.c.config.state_dir, Path.read_bytes))
        await self.wake.get()

    async def dispatch(self, ticket):
        self.calls.append(ticket.stem)
        try:
            if self.hook is not None:
                await self.hook(ticket)
            self.c.journal.append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket=ticket.stem)
            return "merged"
        finally:
            self.cleaned.set()

    async def abort(self):
        self.aborted += 1

    def start(self, *, before_dispatch=None):
        # Match the production dispatch object's abort surface without replacing its owner.
        rig = self
        class Dispatch:
            async def __call__(self, ticket):
                return await rig.dispatch(ticket)

            async def abort_current(self):
                await rig.abort()
        task = asyncio.create_task(drain(self.c, Dispatch(), reexec=NoChild(),
                                         before_dispatch=before_dispatch))
        self.tasks.append(task)
        return task

    async def close(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


@pytest_asyncio.fixture
async def live(tmp_path):
    rig = LiveStorm(make_root(tmp_path))
    try:
        yield rig
    finally:
        await rig.close()


@pytest.mark.parametrize("stage", ["implement", "check", "review"])
def test_live_drain_holds_only_emitting_stem(tmp_path, monkeypatch, stage):
    root = make_root(tmp_path)
    for stem, priority, depends in (("one", "P0", "none"), ("urgent", "P0", "none"),
                                   ("aaa-new", "P1", "none"), ("zzz-old", "P1", "none"),
                                   ("child", "P0", "- one")):
        commit_ticket(root, stem, confirmed(priority=priority, depends=depends))
    clock = FakeClock(T0)
    journal = Journal(root / ".tempo/state", clock)
    journal.append(EventType.SIGNAL, {"signal": INTAKE_SIGNAL}, ticket="zzz-old")
    clock.advance(1)
    journal.append(EventType.SIGNAL, {"signal": INTAKE_SIGNAL}, ticket="aaa-new")
    box = daemon.storm_producer(root=root / ".tempo/state/box", fs=LocalFileSystem(),
                                journal=journal, clock=clock)
    six(box, stage=stage)
    trip, = trips(journal)
    assert len(occurrences(journal, trip.body["signature"])) == 6
    assert len(box.messages()) == 2
    script, consumers = Script(), []
    original = cli.build_control

    def build(checkout):
        consumer = original(checkout)
        consumers.append(consumer)
        async def sleep(_):
            assert script.calls == ["urgent", "zzz-old", "aaa-new"]
            assert read_active(checkout.config.state_dir, Path.read_bytes) == (
                consumer.inbox.lifecycle_id, trip.body["trip_id"])
            assert not any(e.ticket == "one" and e.type in
                           {EventType.CAP_CONSUMED, EventType.STATE_TRANSITION} for e in journal.read())
            await cli._control(checkout, "resume")
        consumer.sleep = sleep
        return consumer
    monkeypatch.setattr(cli, "build_control", build)
    monkeypatch.setattr("tempo.triage.triage_pass", Mock(side_effect=AssertionError("consumed Box")))
    assert cli.main(["drain"], cwd=root, env=ENV, clock=clock, pipeline=script, reexec=NoChild()) == 0
    assert script.calls == ["urgent", "zzz-old", "aaa-new", "one", "child"]
    assert script.lock_held == [True] * 5
    assert consumers[0].storm_holds() == {} and len(trips(journal)) == 1
    assert all(m.status == "pending" and m.verdict is m.resolution is None for m in box.messages())


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["fresh", "retry", "reject"])
async def test_storm_hold_precedes_all_dispatch_accounting(live, kind):
    commit_ticket(live.root, "one", confirmed(priority="P0"))
    commit_ticket(live.root, "unrelated", confirmed())
    sha = await live.c.git.rev_parse(live.root, "HEAD:" + ticket_path("one"))
    if kind != "fresh":
        for _ in range(2):
            caps.consume(live.c.journal, "one", "retry", sha)
        live.c.journal.append(EventType.STATE_TRANSITION, {"to": "gate_failed"}, ticket="one")
    if kind == "reject":
        live.c.journal.append(EventType.SIGNAL, {"signal": "reject_arrival"}, ticket="one")
    identity = live.trip()
    before = [e for e in live.offers if e.ticket == "one"]
    ticket_bytes = (live.root / ticket_path("one")).read_bytes()
    box_bytes = snapshot(live.box.root)
    task = live.start()
    life, hold = await live.waiting.get()
    assert hold == identity and life == live.consumer.inbox.lifecycle_id and not task.done()
    assert live.calls == ["unrelated"]
    assert [e for e in live.offers if e.ticket == "one"] == before
    live.clock.advance(1)
    live.request("release", hold=identity)
    assert (await task).merged == ["unrelated", "one"]
    after = [e for e in live.offers if e.ticket == "one"][len(before):]
    if kind == "reject":
        keep = after.pop(0)
        assert keep.body == {"signal": "reject_verdict", "verdict": "keep", "actor": "machine"}
    if kind != "fresh":
        assert after.pop(0).type == EventType.CAP_CONSUMED
    assert [e.body["to"] for e in after] == ["running", "merged"]
    events = live.c.journal.read()
    assert all(events.index(live.decisions[-1]) < events.index(e)
               for e in live.offers if e.ticket == "one" and e not in before)
    assert caps.draws(events, "one", "retry") == (0 if kind == "fresh" else 3)
    assert (live.root / ticket_path("one")).read_bytes() == ticket_bytes
    assert snapshot(live.box.root) == box_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,origin", [(None, "one"), ("unknown", "one"),
    ("depends", "one"), ("", "one"), ("implement", None),
    ("check", "tests/test_host.py::test_example"), ("review", "decisions"),
    ("review", "nonexistent")])
async def test_non_pipeline_and_non_ticket_trips_do_not_hold(live, stage, origin):
    commit_ticket(live.root, "one", confirmed())
    ledger = StormLedger(journal=live.c.journal, clock=live.clock)
    for n in range(6):
        ledger.record(signature=SIG, occurrence_id=str(n), emitting_stage=stage, emitting_origin=origin)
    live.box.recover()
    trip, = trips(live.c.journal)
    assert trip.body["emitting_stage"] == stage and trip.body["emitting_origin"] == origin
    before = snapshot(live.box.root)
    report = await live.start()
    assert report.merged == ["one"] and report.exit_code == 0
    assert live.waiting.empty() and live.decisions == []
    assert len(trips(live.c.journal)) == 1 and len(live.box.messages()) == 1
    assert snapshot(live.box.root) == before


@pytest.mark.asyncio
async def test_live_resume_releases_in_same_drain(live):
    commit_ticket(live.root, "one", confirmed())
    sha = await live.c.git.rev_parse(live.root, "HEAD:" + ticket_path("one"))
    for _ in range(2):
        caps.consume(live.c.journal, "one", "retry", sha)
    live.c.journal.append(EventType.STATE_TRANSITION, {"to": "gate_failed"}, ticket="one")
    identity = live.trip()
    task = live.start()
    lifecycle, hold = await live.waiting.get()
    assert hold == identity and not task.done() and live.calls == []
    before = live.offers
    live.clock.advance(1)
    assert await asyncio.to_thread(cli.main, ["resume"], cwd=live.root, env=ENV, clock=live.clock) == 0
    request, = list((live.c.config.state_dir / "control/inbox").glob("*.json"))
    value = json.loads(request.read_bytes())
    assert value == asdict(ControlRequest(request.stem, lifecycle, "resume", identity))
    assert live.offers == before and live.decisions == []
    live.wake.put_nowait(None)
    report = await task
    assert report.merged == ["one"] and live.calls == ["one"]
    decision, = live.decisions
    assert decision.ticket is decision.key is None and decision.type == EventType.SIGNAL
    assert decision.body == {"kind": CONTROL_DECISION, **value,
                             "decision": "accepted", "reason": "matching lifecycle and hold identities"}
    events = live.c.journal.read()
    draw = next(e for e in live.offers[len(before):] if e.type == EventType.CAP_CONSUMED)
    assert events.index(decision) < events.index(draw)
    assert caps.draws(events, "one", "retry") == 3
    assert live.holds == {} and live.consumer.projection.pause_id is None
    assert (live.c.config.state_dir / "control/active.json").read_bytes() == b"null\n"


@pytest.mark.asyncio
async def test_storm_resume_is_identity_bound_and_once(live):
    # A pre-trip request cannot acquire authority when its predicted identity arrives later.
    identity = digest([SIG, "arrival/0", "arrival/5"])
    live.request("00-premature", hold=identity, wake=False)
    await live.consumer.checkpoint()
    assert live.decisions[-1].body["decision"] == "stale"
    assert live.trip() == identity
    commit_ticket(live.root, "one", confirmed())
    task = live.start()
    await live.waiting.get()
    for name, hold, lifecycle in (("10-wrong", "unknown", None),
                                  ("20-old-life", identity, "retired")):
        live.request(name, hold=hold, life=lifecycle)
        assert (await live.waiting.get())[1] == identity
        assert live.decisions[-1].body["decision"] == "stale"
        assert not task.done() and live.offers == []
    live.c.fs.publish(live.c.config.state_dir / "control/inbox/25-malformed.json", b"{}")
    live.wake.put_nowait(None)
    await live.waiting.get()
    assert live.decisions[-1].body["decision"] == "rejected" and live.holds == {identity: "one"}
    live.request("30-release", hold=identity)
    assert (await task).merged == ["one"]
    events = live.c.journal.read()
    live.consumer.inbox.consume()
    live.consumer.inbox.consume()
    assert live.c.journal.read() == events
    with pytest.raises(FileExistsError, match="new request id"):
        live.request("30-release", hold=identity, wake=False)
    live.request("40-repeated", hold=identity, wake=False)
    live.consumer.inbox.consume()
    assert live.decisions[-1].body["decision"] == "stale"
    later = live.trip(prefix="new")
    assert later != identity and live.holds == {later: "one"}
    live.request("50-old-trip", hold=identity, wake=False)
    live.consumer.inbox.consume()
    assert live.decisions[-1].body["decision"] == "stale" and live.holds == {later: "one"}
    assert live.consumer.projection.released_hold_ids == {identity}


@pytest.mark.asyncio
async def test_multiple_storm_holds_release_independently(live):
    for stem in ("one", "two", "unrelated"):
        commit_ticket(live.root, stem, confirmed())
    first = live.trip()
    second = live.trip(origin="two", prefix="two")
    third = live.trip(reason="another signature", prefix="another")
    assert list(live.holds.items()) == [(first, "one"), (second, "two"), (third, "one")]
    task = live.start()
    assert (await live.waiting.get())[1] == first
    assert live.calls == ["unrelated"]
    # Discovery prioritizes dispatch and admission, while validation sees every storm identity.
    live.consumer.hold("admission")
    assert read_active(live.c.config.state_dir, Path.read_bytes)[1] == "admission"
    live.request("10-pause", "pause")
    assert (await live.waiting.get())[1] == "10-pause"
    live.request("20-third", hold=third)
    assert (await live.waiting.get())[1] == "10-pause"
    assert live.consumer.projection.pause_id == "10-pause" and live.consumer.admission == "admission"
    assert live.holds == {first: "one", second: "two"}
    live.request("30-dispatch", hold="10-pause")
    assert (await live.waiting.get())[1] == "admission"
    live.request("40-admission", hold="admission")
    assert (await live.waiting.get())[1] == first
    # The remaining hold advertises next and continues to suppress only its own stem.
    live.request("50-first", hold=first)
    assert (await live.waiting.get())[1] == second
    assert live.calls == ["unrelated", "one"]
    live.request("60-second", hold=second)
    assert (await task).merged == ["unrelated", "one", "two"]
    assert live.holds == {} and len(trips(live.c.journal)) == 3
    assert len([m for m in live.box.messages() if m.origin.startswith("storm-breaker/")]) == 3


@pytest.mark.asyncio
async def test_storm_hold_survives_restart_roll_and_expiry(live, monkeypatch):
    monkeypatch.setattr(journal_module, "ROLL_BYTES", 1)
    commit_ticket(live.root, "one", confirmed())
    identity = live.trip()
    old_lifecycle = live.consumer.inbox.lifecycle_id
    live.c.journal.close()
    live.clock.advance(7200)
    live.rebuild()
    assert live.consumer.inbox.lifecycle_id != old_lifecycle
    assert live.holds == {identity: "one"} and len(list(live.c.journal.dir.glob("*.jsonl"))) > 1
    live.request("old-pending", hold=identity, life=old_lifecycle, wake=False)
    task = live.start()
    assert (await live.waiting.get())[1] == identity
    assert live.decisions[-1].body["decision"] == "stale" and live.calls == []
    live.request("new-release", hold=identity)
    assert (await task).merged == ["one"]
    before = live.c.journal.read()
    live.c.journal.close()
    live.clock.advance(7200)
    live.rebuild()
    assert live.holds == {}
    live.box.recover()
    live.consumer.inbox.consume()
    assert live.c.journal.read() == before
    later = live.trip(prefix="later")
    assert later != identity and live.holds == {later: "one"}
    live.consumer.inbox.consume()
    assert live.holds == {later: "one"}
    assert len([e for e in live.decisions if e.body["decision"] == "accepted"]) == 1


@pytest.mark.asyncio
async def test_storm_release_rearms_on_new_arrivals_only(live, monkeypatch):
    first = live.trip()
    for n in range(6, 12):
        live.box.enqueue(**ARRIVAL, occurrence_id=f"arrival/{n}")
    assert len(trips(live.c.journal)) == 1
    live.request("release", hold=first, wake=False)
    await live.consumer.checkpoint()
    assert live.holds == {}
    monkeypatch.setattr(journal_module, "ROLL_BYTES", 1)
    live.c.journal.close()
    live.rebuild()
    for n in range(12):
        live.box.enqueue(**ARRIVAL, occurrence_id=f"arrival/{n}")
    live.box.recover()
    assert live.holds == {} and len(trips(live.c.journal)) == 1
    for n in range(5):
        live.box.enqueue(**ARRIVAL, occurrence_id=f"fresh/{n}")
    assert live.holds == {} and len(trips(live.c.journal)) == 1
    live.box.enqueue(**ARRIVAL, occurrence_id="fresh/5")
    second = digest([SIG, "fresh/0", "fresh/5"])
    assert [e.body["trip_id"] for e in trips(live.c.journal)] == [first, second]
    assert live.holds == {second: "one"}
    before = snapshot(live.c.config.state_dir)
    for n in range(6):
        live.box.enqueue(**ARRIVAL, occurrence_id=f"fresh/{n}")
    assert snapshot(live.c.config.state_dir) == before
    reports = [m for m in live.box.messages() if m.origin.startswith("storm-breaker/")]
    assert [m.origin for m in reports] == ["storm-breaker/" + first, "storm-breaker/" + second]
    events = live.c.journal.read()
    release, = [e for e in live.decisions if e.body["decision"] == "accepted"]
    assert events.index(release) < events.index(occurrences(live.c.journal, SIG)[12])
    assert len(occurrences(live.c.journal, SIG)) == 18


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["trip", "decision", "discovery"])
@pytest.mark.parametrize("after", [False, True])
async def test_storm_hold_crash_and_corrupt_evidence(live, monkeypatch, point, after):
    class Crash(BaseException):
        pass
    append, write = live.c.journal.append, live.c.fs.write
    if point == "trip":
        for n in range(5):
            live.box.enqueue(**ARRIVAL, occurrence_id=f"arrival/{n}")
        identity = digest([SIG, "arrival/0", "arrival/5"])
    else:
        identity = live.trip()
        live.consumer.publish()
        live.request("release", hold=identity, wake=False)
    before_projection = live.consumer.projection

    def crash_append(type, body, **kwargs):
        target = body.get("kind") == ("storm_breaker_trip" if point == "trip" else CONTROL_DECISION)
        if target:
            assert live.consumer.projection == before_projection
            if after:
                append(type, body, **kwargs)
            raise Crash()
        return append(type, body, **kwargs)

    def crash_write(path, data):
        if path.name == "active.json":
            assert live.decisions[-1].body["decision"] == "accepted"
            assert live.consumer.projection == before_projection
            if after:
                write(path, data)
            raise Crash()
        return write(path, data)

    with monkeypatch.context() as patch:
        if point == "discovery":
            patch.setattr(live.c.fs, "write", crash_write)
        else:
            patch.setattr(live.c.journal, "append", crash_append)
        with pytest.raises(Crash):
            if point == "trip":
                live.box.enqueue(**ARRIVAL, occurrence_id="arrival/5")
            else:
                live.consumer.inbox.consume()
    assert live.consumer.projection == before_projection
    # A durable decision is authority even if its publication did not finish.
    released = point == "discovery" or (point == "decision" and after)
    assert live.holds == ({} if released else ({identity: "one"} if point != "trip" or after else {}))
    if released:
        decision_count = len(live.decisions)
        await live.consumer.checkpoint()
        assert identity in live.consumer.projection.released_hold_ids
        assert read_active(live.c.config.state_dir, Path.read_bytes)[1] is None
        assert len(live.decisions) == decision_count
    same_lifecycle = live.consumer.inbox.lifecycle_id
    live.c.journal.close()
    live.clock.advance(7200)
    live.rebuild()
    if point == "trip":
        live.box.recover()
        assert live.holds == {identity: "one"}
        assert len(trips(live.c.journal)) == 1 and len(live.box.messages()) == 2
    elif released:
        live.consumer.publish()
        assert live.holds == {} and read_active(live.c.config.state_dir, Path.read_bytes)[1] is None
        live.consumer.inbox.consume()
        assert len(live.decisions) == 1
    else:
        assert live.holds == {identity: "one"}
        live.consumer.inbox.consume()
        assert live.decisions[-1].body["decision"] == "stale"
        assert live.decisions[-1].body["lifecycle_id"] == same_lifecycle
    # Validate a trip before exposing any target; the source evidence cannot be repaired by this reader.
    trip = trips(live.c.journal)[0]
    live.c.journal.append(EventType.SIGNAL, {**trip.body, "held": None}, key=trip.key)
    before = snapshot(live.c.config.state_dir)
    with pytest.raises(ValueError, match="repair the producing evidence.*never overwrite"):
        live.consumer.publish()
    assert snapshot(live.c.config.state_dir) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("after", [False, True])
async def test_storm_discovery_failure_recovers_without_another_trip(live, monkeypatch, after):
    identity = live.trip()
    write = live.c.fs.write
    def failed(path, data):
        if path.name == "active.json":
            assert trips(live.c.journal)[0].body["trip_id"] == identity
            if after:
                write(path, data)
            raise OSError("publication failed")
        return write(path, data)
    events = live.c.journal.read()
    with monkeypatch.context() as patch:
        patch.setattr(live.c.fs, "write", failed)
        with pytest.raises(OSError, match="publication failed"):
            live.consumer.publish()
    assert not live.consumer._published
    assert live.holds == {identity: "one"} and live.c.journal.read() == events
    live.rebuild()
    live.consumer.publish()
    assert read_active(live.c.config.state_dir, Path.read_bytes)[1] == identity
    assert live.c.journal.read() == events and len(live.box.messages()) == 2
    live.consumer.retire()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["kill", "budget", "resume"])
async def test_storm_wait_preserves_kill_budget_and_quiescence(live, stop):
    for stem, depends in (("active", "none"), ("one", "none"),
                           ("unrelated", "none"), ("child", "- one")):
        commit_ticket(live.root, stem, confirmed(depends=depends, priority="P0" if stem == "active" else "P2"))
    active, finish = asyncio.Event(), asyncio.Event()
    async def hook(ticket):
        if ticket.stem == "active":
            active.set()
            await finish.wait()
    live.hook = hook
    task = live.start()
    await active.wait()
    await live.waiting.get()  # The existing in-flight control monitor, through the injected seam.
    identity = live.trip()
    assert live.calls == ["active"] and not live.cleaned.is_set()
    finish.set()
    assert (await live.waiting.get())[1] == identity
    assert live.cleaned.is_set() and live.calls == ["active", "unrelated"]
    assert not task.done() and live.aborted == 0
    before = snapshot(live.box.root)
    if stop == "kill":
        live.request("kill", "kill")
    elif stop == "budget":
        live.clock.advance(live.c.config.drain.max_runtime_hours * 3600)
        live.wake.put_nowait(None)
    else:
        live.request("release", hold=identity)
    report = await task
    assert snapshot(live.box.root) == before
    if stop == "kill":
        assert report.killed and report.exit_code == 1 and live.aborted == 1
        assert live.calls == ["active", "unrelated"]
        assert [e.body["kind"] for e in live.c.journal.read()
                if e.body.get("kind") in {CONTROL_DECISION, daemon.KILL_APPLIED}] == [CONTROL_DECISION, daemon.KILL_APPLIED]
    elif stop == "budget":
        assert report.halted and report.exit_code == 1 and live.aborted == 0
        assert live.calls == ["active", "unrelated"]
        assert any(e.body.get("signal") == HALT_SIGNAL for e in live.c.journal.read())
        assert report.blocked == [("child", ("one",))] and report.unadmitted == ["one"]
    else:
        assert report.exit_code == 0 and live.calls == ["active", "unrelated", "one", "child"]
    assert (live.c.config.state_dir / "control/active.json").read_bytes() == b"null\n"
    # Ordinary no-hold completion remains quiescent; a held producer without eligible work never waits.
    if stop != "resume":
        live.c.journal.append(EventType.STATE_TRANSITION, {"to": "rejected"}, ticket="one")
        live.c.journal.append(EventType.STATE_TRANSITION, {"to": "rejected"}, ticket="child")
    live.rebuild()
    assert (await live.start()).exit_code == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["retire", "caps", "depends", "edit"])
async def test_storm_wait_reprepares_committed_material(live, change):
    commit_ticket(live.root, "one", confirmed())
    sha = await live.c.git.rev_parse(live.root, "HEAD:" + ticket_path("one"))
    if change == "caps":
        live.c.journal.append(EventType.STATE_TRANSITION, {"to": "gate_failed"}, ticket="one")
    identity = live.trip()
    task = live.start()
    await live.waiting.get()
    if change == "retire":
        live.c.journal.append(EventType.STATE_TRANSITION, {"to": "rejected"}, ticket="one")
    elif change == "caps":
        for _ in range(live.c.config.caps.retry):
            caps.consume(live.c.journal, "one", "retry", sha)
    elif change == "depends":
        commit_ticket(live.root, "missing-parent", confirmed().replace("state: confirmed", "state: draft"))
        commit_ticket(live.root, "one", confirmed(depends="- missing-parent"))
    else:
        commit_ticket(live.root, "one", confirmed(priority="P0"))
    live.request("release", hold=identity)
    report = await task
    assert live.calls == (["one"] if change == "edit" else [])
    if change == "depends":
        assert report.blocked == [("one", ("missing-parent",))]
    if change == "edit":
        running = next(e for e in live.c.journal.read() if e.body.get("to") == "running")
        assert running.body["ticket_sha"] == await live.c.git.rev_parse(live.root, "HEAD:" + ticket_path("one"))
        assert running.body["ticket_sha"] != sha
    assert report.exit_code == 0 and live.holds == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("ineligible", ["draft", "blocked", "spent", "premise", "settled", "oversized"])
async def test_storm_hold_without_dispatchable_work_is_quiescent(live, ineligible):
    text = confirmed()
    if ineligible == "draft":
        text = text.replace("state: confirmed", "state: draft")
    elif ineligible == "blocked":
        commit_ticket(live.root, "missing-parent", confirmed().replace("state: confirmed", "state: draft"))
        text = confirmed(depends="- missing-parent")
    elif ineligible == "oversized":
        text = text.replace("- stuck: 20m", "- stuck: 100m")
    commit_ticket(live.root, "one", text)
    sha = await live.c.git.rev_parse(live.root, "HEAD:" + ticket_path("one"))
    if ineligible in {"spent", "premise", "settled"}:
        live.c.journal.append(EventType.STATE_TRANSITION, {"to": "running", "ticket_sha": sha}, ticket="one")
        live.c.journal.append(EventType.STATE_TRANSITION,
            {"to": {"spent": "gate_failed", "premise": "premise_failed", "settled": "merged"}[ineligible]},
            ticket="one")
    if ineligible == "spent":
        for _ in range(live.c.config.caps.retry):
            caps.consume(live.c.journal, "one", "retry", sha)
    identity = live.trip()
    offers = live.offers
    assert (await live.start()).exit_code == 0
    assert live.waiting.empty() and live.calls == [] and live.offers == offers
    assert live.holds == {identity: "one"}
    if ineligible == "spent":
        assert any(e.ticket == "one" and e.body.get("signal") == "reject_arrival"
                   for e in live.c.journal.read())


def test_storm_projection_construction_is_idle(tmp_path, monkeypatch):
    rig = CoreRig(tmp_path)
    rig.fs.write = LocalFileSystem().write
    read, append = Mock(wraps=rig.journal.read), Mock(wraps=rig.journal.append)
    monkeypatch.setattr(rig.journal, "read", read)
    monkeypatch.setattr(rig.journal, "append", append)
    consumer = cli.build_control(rig.checkout)
    assert consumer.storm_holds() == {}
    append.assert_not_called()
    # A fresh root constructs the real core without reading or publishing anything.
    read.reset_mock()
    cli.build_control(rig.checkout)
    read.assert_not_called()
    assert rig.fs.files == {} and rig.exec.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["shape", "key", "type", "conflict", "decision", "rolled"])
async def test_corrupt_release_evidence_refuses_without_dispatch(live, damage, monkeypatch):
    identity = live.trip()
    live.request("release", hold=identity, wake=False)
    live.consumer.inbox.consume()
    decision, = live.decisions
    body, type, key = dict(decision.body), EventType.SIGNAL, None
    if damage == "shape":
        body.pop("reason")
    elif damage == "key":
        key = "unexpected"
    elif damage == "type":
        type = EventType.EFFECT_INTENT
    elif damage == "conflict":
        body["hold_id"] = "different"
    elif damage == "decision":
        body["decision"] = []
    else:
        monkeypatch.setattr(journal_module, "ROLL_BYTES", 1)
        live.c.journal.append(EventType.SIGNAL, {"kind": "drain_handoff"})
        path = sorted(live.c.journal.dir.glob("*.jsonl"))[0]
        path.write_bytes(path.read_bytes() + b"{torn")
    if damage != "rolled":
        live.c.journal.append(type, body, key=key)
    before = snapshot(live.c.config.state_dir)
    with pytest.raises(JournalCorruption if damage == "rolled" else ValueError,
                       match="restore.*backup" if damage == "rolled" else "repair.*evidence.*never overwrite"):
        live.consumer.publish()
    assert snapshot(live.c.config.state_dir) == before and live.calls == []
