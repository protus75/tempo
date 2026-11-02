"""Component behavior and production CLI reachability evidence."""

import ast
import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tempo.journal import EventType, Journal
from tempo.scheduler import Scheduler
from tempo.tickets import INTAKE_SIGNAL, parse_ticket
from tempo.watcher import WATCHER_PARSE_FAILURE, Watcher
from tests.test_cli import TICKET


class Time:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=UTC)
        self.waits = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        self.waits.append((self.now + timedelta(seconds=seconds), future))
        await future

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        for deadline, future in self.waits:
            if deadline <= self.now and not future.done():
                future.set_result(None)


async def turn():
    # Yield through callbacks without a wall-clock sleep or a timer.
    future = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(future.set_result, None)
    await future


def text(priority="P2", state="confirmed", depends="none"):
    return TICKET.format(depends=depends).replace(
        "priority: P2", f"state: {state}\nsource: human\npriority: {priority}", 1)


class Rig:
    def __init__(self, root):
        self.root = root
        (root / "tempo").mkdir()
        (root / "tempo/thing.py").write_text("")
        self.time = Time()
        self.journal = Journal(root / "state", self.time)
        self.quarantine = set()
        self.drought = set()
        self.unmerged = 0
        self.calls = []
        self.scheduler = Scheduler(
            self.journal, self.dispatch, quarantined=lambda: self.quarantine,
            drought_parked=lambda: self.drought, completed_unmerged=lambda: self.unmerged,
            max_unmerged=2,
        )
        self.files = {}
        self.watcher = Watcher(
            root, read=self.files.get, journal=self.journal,
            publish=self.scheduler.update, remove=self.scheduler.remove,
            clock=self.time, sleep=self.time.sleep, debounce=5,
        )

    async def dispatch(self, ticket):
        self.calls.append(ticket.stem)

    def add(self, stem, priority="P2", state="confirmed", depends="none"):
        raw = text(priority, state, depends)
        path = self.root / "tickets" / stem / "ticket.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw)
        self.files[stem] = raw
        ticket = parse_ticket(stem, raw, self.root)
        self.scheduler.update(ticket)
        return ticket

    def transition(self, stem, to, **body):
        self.journal.append(EventType.STATE_TRANSITION, {"to": to, **body}, ticket=stem)

    def intake(self, stem):
        self.journal.append(EventType.SIGNAL,
                            {"signal": INTAKE_SIGNAL, "source": "human", "state": "confirmed",
                             "new": True, "commit": "abc"}, ticket=stem)

    async def publish(self, *stems):
        for stem in stems:
            self.watcher.change(stem)
        await turn()
        self.time.advance(5)
        await turn()

    async def drain(self):
        while await self.scheduler.dispatch_next() is not None:
            pass


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


@pytest.mark.asyncio
async def test_single_flight_dispatch(rig):
    rig.add("aa")
    rig.add("bb")
    started = asyncio.Event()
    release = asyncio.Event()
    active = 0

    async def dispatch(ticket):
        nonlocal active
        active += 1
        assert active == 1
        rig.calls.append(ticket.stem)
        started.set()
        await release.wait()
        active -= 1

    rig.scheduler.dispatch = dispatch
    first = asyncio.create_task(rig.scheduler.dispatch_next())
    await started.wait()
    second = asyncio.create_task(rig.scheduler.dispatch_next())
    await turn()
    assert rig.calls == ["aa"] and not first.done() and not second.done()
    assert rig.scheduler.active.stem == "aa"
    release.set()
    assert [t.stem for t in await asyncio.gather(first, second)] == ["aa", "bb"]
    assert active == 0 and rig.scheduler.active is None


@pytest.mark.asyncio
async def test_dispatch_failure_and_cancellation_release_slot(rig):
    rig.add("aa")
    rig.add("bb")
    rig.add("cc")

    async def fail(ticket):
        raise ValueError("dispatch failed")

    rig.scheduler.dispatch = fail
    with pytest.raises(ValueError, match="dispatch failed"):
        await rig.scheduler.dispatch_next()
    assert rig.scheduler.active is None
    started = asyncio.Event()

    async def block(ticket):
        started.set()
        await asyncio.Event().wait()

    rig.scheduler.dispatch = block
    running = asyncio.create_task(rig.scheduler.dispatch_next())
    await started.wait()
    waiter = asyncio.create_task(rig.scheduler.dispatch_next())
    await turn()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert rig.scheduler.active.stem == "bb"
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert rig.scheduler.active is None
    rig.scheduler.dispatch = rig.dispatch
    assert (await rig.scheduler.dispatch_next()).stem == "cc"


@pytest.mark.asyncio
async def test_eligibility_and_hold_release(rig):
    for state in ("draft", "rejected", "merged"):
        rig.add(state, state=state)
    rig.add("parent", state="merged")
    rig.add("noop", state="merged")
    rig.add("child", depends="- parent\n- noop")
    rig.add("reject-held")
    rig.add("quarantined")
    rig.add("drought-held")
    rig.add("settled")
    rig.add("retired")
    rig.transition("settled", "already_satisfied")
    rig.transition("retired", "rejected")
    rig.transition("reject-held", "gate_failed", routed="reject_queue")
    rig.quarantine.add("quarantined")
    rig.drought.add("drought-held")
    assert await rig.scheduler.dispatch_next() is None
    rig.transition("parent", "merged")
    assert await rig.scheduler.dispatch_next() is None
    rig.transition("noop", "already_satisfied")
    assert (await rig.scheduler.dispatch_next()).stem == "child"
    assert await rig.scheduler.dispatch_next() is None
    for stem, actor in (("reject-held", "operator"), ("machine-held", "machine")):
        if stem == "machine-held":
            rig.add(stem)
            rig.transition(stem, "gate_failed", routed="reject_queue")
            assert await rig.scheduler.dispatch_next() is None
        rig.journal.append(EventType.SIGNAL,
                           {"signal": "reject_verdict", "verdict": "keep", "actor": actor}, ticket=stem)
        assert (await rig.scheduler.dispatch_next()).stem == stem
    rig.quarantine.clear()
    assert (await rig.scheduler.dispatch_next()).stem == "quarantined"
    rig.drought.clear()
    assert (await rig.scheduler.dispatch_next()).stem == "drought-held"
    assert await rig.scheduler.dispatch_next() is None
    assert all(e.type != EventType.CAP_CONSUMED for e in rig.journal.read())


@pytest.mark.asyncio
async def test_priority_age_and_stem_order(rig):
    for stem, priority in (("zzz-old", "P2"), ("aaa-new", "P2"), ("aaa-missing", "P2"),
                           ("bbb-missing", "P2"), ("urgent", "P0"), ("low", "P3"),
                           ("mid", "P1"), ("tie-aa", "P2"), ("tie-bb", "P2")):
        rig.add(stem, priority)
    rig.intake("low")
    rig.intake("zzz-old")
    rig.time.advance(10)
    rig.intake("aaa-new")
    rig.time.advance(10)
    rig.intake("zzz-old")  # Must not move its first-event anchor.
    rig.intake("tie-bb")
    rig.intake("tie-aa")
    for n, stem in enumerate(reversed(list(rig.scheduler.pending))):
        os.utime(rig.root / "tickets" / stem / "ticket.md", (n + 1, n + 1))
    await rig.drain()
    assert rig.calls == ["urgent", "mid", "zzz-old", "aaa-new", "tie-aa", "tie-bb",
                         "aaa-missing", "bbb-missing", "low"]


@pytest.mark.asyncio
async def test_max_unmerged_backpressure(rig):
    rig.add("aa")
    rig.add("bb")
    rig.add("cc")
    for count in (2, 3):
        rig.unmerged = count
        assert await rig.scheduler.dispatch_next() is None
    rig.unmerged = 1
    started, release = asyncio.Event(), asyncio.Event()

    async def dispatch(ticket):
        started.set()
        await release.wait()
        rig.unmerged = 2

    rig.scheduler.dispatch = dispatch
    first = asyncio.create_task(rig.scheduler.dispatch_next())
    await started.wait()
    second = asyncio.create_task(rig.scheduler.dispatch_next())
    release.set()
    assert (await first).stem == "aa"
    assert await second is None  # Re-read after the previous dispatch finishes.
    rig.unmerged = 0
    rig.scheduler.dispatch = rig.dispatch
    assert (await rig.scheduler.dispatch_next()).stem == "bb"


@pytest.mark.asyncio
async def test_watcher_debounce(rig):
    rig.files["partial"] = "---\npriority:"
    rig.watcher.change("partial")
    await turn()
    rig.time.advance(4)
    await turn()
    assert not rig.watcher.cache and not rig.journal.read()
    rig.files["partial"] = text()
    rig.watcher.change("partial")
    await turn()
    rig.time.advance(1)  # The original deadline cannot publish the updated record.
    await turn()
    assert not rig.scheduler.pending and not rig.journal.read()
    rig.time.advance(3)
    await turn()
    assert not rig.watcher.cache
    rig.time.advance(1)
    await turn()
    assert rig.watcher.cache["partial"] == rig.scheduler.pending["partial"]
    assert not rig.journal.read()
    # Also consume injected change delivery through the component's async entry.
    async def changes():
        rig.files["second"] = text("P0")
        yield "second"

    consumer = asyncio.create_task(rig.watcher.run(changes()))
    await turn()
    await turn()
    rig.time.advance(5)
    await consumer
    assert rig.scheduler.pending["second"].frontmatter.priority == "P0"


@pytest.mark.asyncio
@pytest.mark.parametrize("new_ticket", [False, True])
async def test_watcher_reprioritizes_without_preemption(rig, new_ticket):
    rig.files.update({"active": text("P0"), "normal": text("P1"), "urgent": text("P3")})
    await rig.publish("active", "normal", "urgent")
    started, release = asyncio.Event(), asyncio.Event()

    async def dispatch(ticket):
        rig.calls.append(ticket.stem)
        if ticket.stem == "active":
            started.set()
            await release.wait()

    rig.scheduler.dispatch = dispatch
    running = asyncio.create_task(rig.scheduler.dispatch_next())
    await started.wait()
    waiting = asyncio.create_task(rig.scheduler.dispatch_next())
    stem = "new-urgent" if new_ticket else "urgent"
    rig.files[stem] = text("P0")
    await rig.publish(stem)
    assert rig.calls == ["active"] and not running.done() and not waiting.done()
    assert rig.scheduler.active.stem == "active"
    release.set()
    assert (await running).stem == "active"
    assert (await waiting).stem == stem
    assert rig.calls == ["active", stem]
    await rig.watcher.close()


@pytest.mark.asyncio
async def test_watcher_parse_failure_preserves_last_good(rig):
    rig.files.update({"old": text("P2"), "other": text("P1")})
    await rig.publish("old", "other")
    old = rig.watcher.cache["old"]
    rig.files["old"] = text("P0").replace("- stuck: 20m", "- stuck: broken")
    rig.files["new-invalid"] = "partial"
    await rig.publish("old", "new-invalid")
    assert rig.watcher.cache["old"] is old and rig.scheduler.pending["old"] is old
    assert "new-invalid" not in rig.watcher.cache and "new-invalid" not in rig.scheduler.pending
    signals = rig.journal.read()
    assert len(signals) == 2
    for event, stem in zip(signals, ("old", "new-invalid"), strict=True):
        assert event.type == EventType.SIGNAL and event.ticket == stem and event.key is None
        assert event.body == {"signal": WATCHER_PARSE_FAILURE,
                              "path": f"tickets/{stem}/ticket.md", "reason": event.body["reason"]}
        assert isinstance(event.body["reason"], str) and event.body["reason"].strip()
    assert (await rig.scheduler.dispatch_next()).stem == "other"
    assert (await rig.scheduler.dispatch_next()).stem == "old"  # Old P2 position survived.
    rig.files["old"] = text("P0")
    rig.files["new-invalid"] = text("P1")
    await rig.publish("old", "new-invalid")
    assert rig.watcher.cache["old"] is not old
    assert rig.watcher.cache["old"].frontmatter.priority == "P0"
    await rig.drain()
    assert rig.calls == ["other", "old", "old", "new-invalid"]
    assert rig.journal.read() == signals
    await rig.watcher.close()


@pytest.mark.asyncio
async def test_watcher_removal_drops_pending(rig):
    rig.files["removed"] = text("P0")
    await rig.publish("removed")
    assert "removed" in rig.scheduler.pending
    del rig.files["removed"]
    await rig.publish("removed")
    assert "removed" not in rig.watcher.cache and "removed" not in rig.scheduler.pending
    assert await rig.scheduler.dispatch_next() is None and not rig.journal.read()
    await rig.watcher.close()


def import_closure(sources):
    pending, reached = ["tempo.__main__"], set()
    while pending:
        module = pending.pop()
        if module in reached or module not in sources:
            continue
        reached.add(module)
        if module != "tempo":
            pending.append("tempo")  # Package initialization is also reachable.
        for node in ast.walk(ast.parse(sources[module])):
            if isinstance(node, ast.Import):
                pending.extend(a.name for a in node.names if a.name.split(".")[0] == "tempo")
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    package = module.split(".")[:-node.level]
                    base = ".".join([*package, *([base] if base else [])])
                if base == "tempo" or base.startswith("tempo."):
                    pending.append(base)
                    pending.extend(f"{base}.{a.name}" for a in node.names)
    return reached


def assert_reachable(sources):
    assert {"tempo.scheduler", "tempo.watcher"} <= import_closure(sources)


def test_scheduler_and_watcher_are_dormant(tmp_path):
    from tests.test_daemon_composition import CoreRig, assert_core_wiring, without_core_import

    root = Path(__file__).resolve().parents[1]
    sources = {".".join(p.relative_to(root).with_suffix("").parts): p.read_text()
               for p in (root / "tempo").rglob("*.py")}
    sources["tempo"] = sources.pop("tempo.__init__")
    assert_reachable(sources)
    removed = without_core_import(sources)
    with pytest.raises(AssertionError):
        assert_reachable(removed)
    for statement in ("import tempo.daemon", "from tempo import daemon"):
        changed = dict(removed)
        changed["tempo.status"] += f"\n{statement}\n"
        assert_reachable(changed)
    rig = CoreRig(tmp_path)
    assert_core_wiring(rig)
    assert rig.exec.calls == [] and rig.core.admission.task is None


@pytest.mark.asyncio
async def test_superseded_dependencies_require_all_successor_leaves(tmp_path):
    from tests.test_daemon_composition import CoreRig
    calls = []
    async def prepare(local):
        async def dispatch(ticket):
            calls.append(ticket.stem)
            return "merged"
        return dispatch
    rig = CoreRig(tmp_path, prepare=prepare)
    await rig.add("original")
    child = await rig.add("dependent", depends="- original")
    for stem, successors in [("original", ["one", "two"]), ("one", ["leaf-a", "leaf-b"])]:
        rig.journal.append(EventType.SIGNAL, {"signal": "supersedes", "successors": successors}, ticket=stem)
        rig.transition(stem, "rejected")
    rig.transition("two", "merged")
    rig.transition("leaf-a", "already_satisfied")
    for state in ("running", "gate_failed", "timeout", "rejected", "abandoned"):
        rig.transition("leaf-b", state)
        assert await rig.core.scheduler.dispatch_next() is None
    rig.transition("leaf-b", "already_satisfied")
    assert await rig.core.scheduler.dispatch_next() is child
    assert calls == ["dependent"] and "original" in rig.core.scheduler.pending
