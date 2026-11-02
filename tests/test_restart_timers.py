"""Lock-held production restart, recovery custody and durable deadline evidence."""

import ast
import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tempo import __main__ as cli, daemon, reconcile, runner, serve
from tempo.audit import audit_journal
from tempo.journal import EventType, Journal, JournalCorruption, render_ts, run_seq
from tempo.lockfile import Lockfile, LockHeld
from tempo.restart import Restart
from tempo.seams import LocalFileSystem, SubprocessExec
from tempo.timers import Timers
from tests.test_cli import ENV, root
from tests.test_daemon_composition import CoreRig, assert_startup_pause_wiring
from tests.test_daemon_composition import CONFIG
from tests.test_scheduler import Time, turn
from tempo import stages


def timers(state, time):
    return Timers(journal=Journal(state, time), clock=time, sleep=time.sleep)


def green(journal):
    assert audit_journal(journal) == []


async def repository(root, monkeypatch, *, prepare=None):
    """Reuse CoreRig and its real build root, with Git/FS seams backed by a disposable repo."""
    rig = CoreRig(root, prepare=prepare, config=CONFIG + "worktree_root: recovery-workspaces\n")
    process, fs = SubprocessExec(), LocalFileSystem()
    scripted = rig.exec.run

    async def run(argv, **kwargs):
        if argv[0] in {"git", "true"}:
            return await process.run(argv, **kwargs)
        return await scripted(argv, **kwargs)

    monkeypatch.setattr(rig.exec, "run", run)
    rig.checkout.git._env = ENV
    write = rig.fs.write

    def written(path, data):
        write(path, data)
        fs.write(path, data)

    monkeypatch.setattr(rig.fs, "write", written)
    (root / ".gitignore").write_text(".tempo/\nstate/\nrecovery-workspaces/\n")
    from tests.test_stages import adopt_conduct
    conduct = adopt_conduct(root)
    await rig.checkout.git.add(root, ["config.yaml", ".gitignore", "tempo/thing.py", *conduct])
    await rig.checkout.git.commit(root, "restart fixture config")
    return rig


async def orphan(rig, stem, *, intent=False, worktree=True):
    journal = rig.journal
    if intent:
        journal.append(EventType.EFFECT_INTENT, {}, ticket=stem, key=f"test/{stem}/0")
    else:
        rig.transition(stem, "running")
    path = rig.checkout.config.worktree_root / stem
    if worktree:
        path.parent.mkdir(parents=True, exist_ok=True)
        await rig.checkout.git.worktree_add(rig.root, path, stem, "main")
        (path / "interrupted.py").write_text("unfinished")
    return path


def held(rig):
    lock = Lockfile(rig.checkout.config.state_dir, instance_id="restart-test", clock=rig.time)
    lock.acquire()
    return lock


@pytest.mark.asyncio
async def test_restart_construction_is_idle(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("construction read, wrote, created a task or called recovery")

    for owner, names in ((Journal, ("read", "append")), (asyncio, ("create_task",)),
                         (reconcile, ("reconcile",)), (runner, ("harvest_orphan",))):
        for name in names:
            monkeypatch.setattr(owner, name, forbidden)
    before = asyncio.all_tasks()
    rig = CoreRig(tmp_path, prepare=forbidden)
    restart = rig.core.restart
    assert isinstance(restart, Restart) and not restart.ready
    assert restart.checkout.journal is restart.timers.journal is rig.journal
    assert restart.checkout.git is rig.checkout.git
    assert restart.checkout.fs is rig.checkout.fs
    assert restart.timers.clock is rig.time and restart.timers.sleep == rig.time.sleep
    assert not restart.timers.pending and not restart.timers.fired
    assert_startup_pause_wiring(rig.core)
    assert asyncio.all_tasks() == before
    assert rig.exec.calls == [] and rig.fs.files == {}
    assert not (tmp_path / "state").exists()
    assert "serve" in cli._parser()._subparsers._group_actions[0].choices


@pytest.mark.asyncio
async def test_restart_reuses_orphan_reconciliation(root, monkeypatch):
    rig = await repository(root, monkeypatch)
    lock = held(rig)
    trace, calls = [], []
    try:
        # Terminal history with unfinished effects stays closed, even with a leftover workspace.
        historical = await orphan(rig, "history")
        rig.journal.append(EventType.EFFECT_INTENT, {}, ticket="history", key="old/incomplete")
        rig.transition("history", "infra_error")
        running = await orphan(rig, "running")
        intent = await orphan(rig, "intent", intent=True)
        await orphan(rig, "missing", worktree=False)
        bad = await orphan(rig, "bad-harvest")
        prior = rig.journal.read()
        original_reconcile, original_harvest = reconcile.reconcile, stages.harvest_orphan
        remove, prune, append = rig.checkout.git.worktree_remove, rig.checkout.git.worktree_prune, rig.journal.append

        async def recovery(journal, git, repo, worktree_root, harvest):
            assert journal is rig.journal and git is rig.checkout.git and repo == root
            assert worktree_root == root / "recovery-workspaces"
            calls.append("reconcile")
            return await original_reconcile(journal, git, repo, worktree_root, harvest)

        async def harvest(checkout, stem, attempt):
            assert checkout is rig.core.restart.checkout
            assert checkout.journal is rig.journal and checkout.fs is rig.fs
            assert attempt == run_seq(rig.journal.read(), stem) == 0
            trace.append((stem, "harvest"))
            if stem == "bad-harvest":
                raise ValueError("scripted harvest failure")
            await original_harvest(checkout, stem, attempt)
            artifact = root / f"tickets/{stem}/attempts/{attempt}/harvest.json"
            assert json.loads(artifact.read_text())["terminal"] == "abandoned"
            assert artifact.read_text() == await checkout.git._run(root, "show", f"HEAD:{artifact.relative_to(root)}")

        def appended(type, body, **kwargs):
            event = append(type, body, **kwargs)
            if type == EventType.STATE_TRANSITION:
                trace.append((kwargs["ticket"], body["to"]))
            elif body.get("kind") == reconcile.RECOVERY_ALERT:
                assert rig.journal.read()[-1] == event
                trace.append((kwargs["ticket"], "recovery_alert"))
            return event

        async def removed(repo, path):
            trace.append((path.name, "remove"))
            terminal, alert = rig.journal.read()[-2:]
            assert terminal.type == EventType.STATE_TRANSITION and terminal.ticket == path.name
            assert terminal.body == {"to": "abandoned"} and terminal.key is None
            assert alert.type == EventType.SIGNAL and alert.ticket == path.name and alert.key is None
            assert alert.body == {"kind": "recovery_alert", "disposition": "alert", "outcome": "abandoned",
                                  "reason": "orphaned run", "run_seq": run_seq(rig.journal.read(), path.name) - 1}
            await remove(repo, path)

        async def pruned(repo):
            trace.append((None, "prune"))
            await prune(repo)

        monkeypatch.setattr(reconcile, "reconcile", recovery)
        monkeypatch.setattr(stages, "harvest_orphan", harvest)
        monkeypatch.setattr(rig.journal, "append", appended)
        monkeypatch.setattr(rig.checkout.git, "worktree_remove", removed)
        monkeypatch.setattr(rig.checkout.git, "worktree_prune", pruned)
        await rig.core.startup()
        assert rig.core.restart.ready and calls == ["reconcile"]
        assert historical.exists() and not any(p.exists() for p in (running, intent, bad))
        events = rig.journal.read()
        assert events[:len(prior)] == prior
        for stem in ("running", "intent", "bad-harvest"):
            assert (trace.index((stem, "harvest")) < trace.index((stem, "abandoned"))
                    < trace.index((stem, "recovery_alert")) < trace.index((stem, "remove")))
            assert trace[trace.index((stem, "remove")) + 1] == (None, "prune")
        assert ("missing", "harvest") not in trace and ("missing", "remove") not in trace
        assert trace[-1] == (None, "prune")
        [error] = [e for e in events if e.body.get("signal") == "harvest_failed"]
        assert error.ticket == "bad-harvest" and error.key is None
        assert error.body == {"signal": "harvest_failed", "error": "ValueError: scripted harvest failure"}
        for stem in ("running", "intent", "missing", "bad-harvest"):
            [terminal] = [e for e in events if e.ticket == stem and e.type == EventType.STATE_TRANSITION
                          and e.body["to"] == "abandoned"]
            assert terminal.key is None and terminal.body == {"to": "abandoned"}
            [alert] = [e for e in events if e.ticket == stem and e.body.get("kind") == reconcile.RECOVERY_ALERT]
            assert alert.type == EventType.SIGNAL and alert.key is None
            assert alert.body == {"kind": "recovery_alert", "disposition": "alert", "outcome": "abandoned",
                                  "reason": "orphaned run", "run_seq": 0}
            assert events.index(terminal) + 1 == events.index(alert)
            assert run_seq(events, stem) == 1
        # The preserved intent-only recovery has no running transition; the existing
        # auditor reports that absent witness. Audit the actual evidence without repair.
        violations = audit_journal(rig.journal)
        assert [(v.invariant, v.ticket, v.detail) for v in violations] == [
            ("one_terminal_per_run", "intent", "terminal 'abandoned' has no open run")]
        assert await rig.core.sweep_orphans() == []
        await rig.core.startup()
        assert rig.journal.read() == events and calls == ["reconcile", "reconcile"]
        # A fresh open run uses the incremented sequence in the SAME harvester.
        monkeypatch.setattr(stages, "harvest_orphan", original_harvest)
        await rig.checkout.git.branch_delete(root, "running")
        await orphan(rig, "running")
        assert await rig.core.sweep_orphans() == ["running"]
        assert (root / "tickets/running/attempts/1/harvest.json").is_file()
        assert run_seq(rig.journal.read(), "running") == 2
        assert [e.body["run_seq"] for e in rig.journal.read()
                if e.ticket == "running" and e.body.get("kind") == reconcile.RECOVERY_ALERT] == [0, 1]
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_recovery_alert_through_serve_entrypoint(root, monkeypatch):
    rig = await repository(root, monkeypatch)
    dead = await orphan(rig, "dead")
    entered, release = asyncio.Event(), asyncio.Event()
    original = stages.harvest_orphan
    calls = []

    async def harvest(checkout, stem, attempt):
        assert checkout.journal is rig.journal and attempt == 0
        entered.set()
        await release.wait()
        await original(checkout, stem, attempt)

    def signals(stop):
        terminal, alert = rig.journal.read()[-2:]
        assert terminal.body == {"to": "abandoned"} and terminal.ticket == "dead"
        assert alert.ticket == "dead" and alert.key is None and alert.type == EventType.SIGNAL
        assert alert.body == {"kind": "recovery_alert", "disposition": "alert", "outcome": "abandoned",
                              "reason": "orphaned run", "run_seq": 0}
        assert not dead.exists()
        calls.append("ready")
        stop()
        return lambda: calls.append("retired")

    monkeypatch.setattr(stages, "harvest_orphan", harvest)
    baseline = asyncio.all_tasks()
    task = asyncio.create_task(serve.serve(rig.checkout, signals=signals, prepare=rig.prepare, llm=rig.llm))
    try:
        await entered.wait()
        assert calls == [] and dead.exists()
        assert not any(e.body.get("kind") == reconcile.RECOVERY_ALERT for e in rig.journal.read())
        release.set()
        assert await task == 0 and calls == ["ready", "retired"]
        assert rig.exec.calls == [] and asyncio.all_tasks() == baseline
        green(rig.journal)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["active-run", "cleanup", "admission"])
async def test_orphan_sweep_preserves_live_ownership(root, monkeypatch, owner):
    entered, release, cleaning, finish = (asyncio.Event() for _ in range(4))

    async def prepare(local):
        async def dispatch(ticket):
            local.journal.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket=ticket.stem)
            try:
                entered.set()
                await release.wait()
            finally:
                local.journal.append(EventType.STATE_TRANSITION, {"to": "infra_error"}, ticket=ticket.stem)
                cleaning.set()
                await finish.wait()
            return "infra_error"
        return dispatch

    rig = await repository(root, monkeypatch, prepare=prepare)
    lock = held(rig)
    task = sweep = None
    try:
        await rig.core.startup()
        dead = await orphan(rig, "dead")
        ticket = await rig.add("live")
        if owner == "admission":
            # Exercise the real inline admission with its own barrier-held Git child.
            from tests.test_daemon_composition import capture_pipeline_queue
            from tempo.llm import FakeLLM
            from tests.test_mergequeue import ready
            captured = capture_pipeline_queue(monkeypatch)
            runner.bind(rig.core.restart.checkout, FakeLLM([]))
            ctx, _, queue = captured[0]
            candidate = await ready(ctx)
            rig.transition(candidate.stem, "running")
            git_call = rig.checkout.git._call
            async def block(repo, *args, **kwargs):
                if args[:1] == ("rebase",):
                    entered.set()
                    await release.wait()
                return await git_call(repo, *args, **kwargs)
            monkeypatch.setattr(rig.checkout.git, "_call", block)
            queue.offer(candidate, attempt=0)
            async def admission(_):
                await queue.process()
                return "merged"
            rig.core.admission._dispatch = admission
            task = asyncio.create_task(rig.core.admission.dispatch(ticket))
            await entered.wait()
            assert queue.active is not None and queue._slot.locked()
        else:
            task = asyncio.create_task(rig.core.admission.dispatch(ticket))
            await entered.wait()
            if owner == "cleanup":
                task.cancel()
                await cleaning.wait()
        before = rig.journal.read()
        def forbidden(*args, **kwargs):
            pytest.fail("live ownership authorized a sweep mutation")
        with monkeypatch.context() as patch:
            patch.setattr(reconcile, "reconcile", forbidden)
            patch.setattr(stages, "harvest_orphan", forbidden)
            patch.setattr(rig.journal, "append", forbidden)
            patch.setattr(rig.checkout.git, "_call", forbidden)
            assert await rig.core.sweep_orphans() == []
        assert rig.journal.read() == before and dead.exists()
        release.set()
        finish.set()
        if owner == "cleanup":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        assert await rig.core.sweep_orphans() == ["dead"]
        assert not dead.exists()
        events = rig.journal.read()
        assert await rig.core.sweep_orphans() == [] and rig.journal.read() == events
        # The idle maintenance lock also excludes an offer arriving during harvest.
        await orphan(rig, "next-dead")
        sweeping, continue_sweep = asyncio.Event(), asyncio.Event()
        original_harvest = stages.harvest_orphan
        async def harvest(checkout, stem, attempt):
            sweeping.set()
            await continue_sweep.wait()
            await original_harvest(checkout, stem, attempt)
        monkeypatch.setattr(stages, "harvest_orphan", harvest)
        offered = []
        async def dispatch(_):
            offered.append("dispatch")
            return "already_satisfied"
        rig.core.admission._dispatch = dispatch
        sweep = asyncio.create_task(rig.core.sweep_orphans())
        await sweeping.wait()
        task = asyncio.create_task(rig.core.admission.dispatch(ticket))
        await turn()
        assert not task.done() and offered == []
        continue_sweep.set()
        assert await sweep == ["next-dead"]
        assert await task == "already_satisfied" and offered == ["dispatch"]
        green(rig.journal)
    finally:
        release.set()
        finish.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if sweep is not None and not sweep.done():
            sweep.cancel()
            await asyncio.gather(sweep, return_exceptions=True)
        lock.release()


def test_timer_records_and_identity(tmp_path):
    time = Time()
    owner = timers(tmp_path, time)
    local = time().astimezone(timezone(timedelta(hours=-4)))
    value = owner.arm("one-deadline", local, ticket="work")
    [armed] = owner.journal.read()
    assert asdict(armed) == {"v": 1, "type": EventType.TIMER_ARMED, "ts": render_ts(time()),
                             "ticket": "work", "key": None,
                             "body": {"timer_id": "one-deadline", "deadline": render_ts(time())}}
    assert owner.arm("one-deadline", time(), ticket="work") is value
    assert owner.journal.read() == [armed]
    assert owner.fire_due() == (value,)
    fired = owner.journal.read()[-1]
    assert asdict(fired) == {**asdict(armed), "type": EventType.TIMER_FIRED}
    before = owner.journal.read()
    assert owner.arm("one-deadline", local, ticket="work") is value
    assert owner.fire_due() == () and owner.journal.read() == before
    for deadline, ticket in ((time() + timedelta(seconds=1), "work"), (time(), None), (time(), "another")):
        with pytest.raises(ValueError, match="fresh id"):
            owner.arm("one-deadline", deadline, ticket=ticket)
    for identity, ticket in (("", None), (1, None), (True, None), ("valid", 1)):
        with pytest.raises(ValueError, match="(?s)invalid timer record.*restore valid journal evidence"):
            owner.arm(identity, time(), ticket=ticket)
    with pytest.raises(ValueError, match="aware"):
        owner.arm("naive", datetime(2026, 1, 1), ticket=None)
    assert owner.journal.read() == before
    owner.arm("fresh", time(), ticket=None)
    assert owner.fire_due()[0].ticket is None
    green(owner.journal)
    # Timer writes have exactly one production owner, including aliases and enum spellings.
    material = Path(__file__).resolve().parents[1] / "tempo"
    writers = set()
    for path in material.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "append":
                if node.args and ((isinstance(node.args[0], ast.Attribute) and
                    node.args[0].attr in {"TIMER_ARMED", "TIMER_FIRED"}) or
                    isinstance(node.args[0], ast.Constant) and node.args[0].value in {"timer_armed", "timer_fired"}):
                    writers.add(path.name)
    assert writers == {"timers.py"}


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["arm", "fire", "after-arm", "after-fire"])
async def test_timer_append_is_write_ahead(tmp_path, monkeypatch, point):
    time = Time()
    owner = timers(tmp_path, time)
    original = owner.journal.append
    actions = []
    if point in {"fire", "after-fire"}:
        owner.arm("deadline", time(), ticket=None)
    before = owner.journal.read()

    def append(type, body, **kwargs):
        assert "deadline" not in owner.fired
        assert ("deadline" in owner.pending) == (point in {"fire", "after-fire"})
        if point.startswith("after-"):
            original(type, body, **kwargs)
        raise OSError("scripted append failure")

    monkeypatch.setattr(owner.journal, "append", append)
    with pytest.raises(OSError, match="append failure"):
        if point in {"arm", "after-arm"}:
            owner.arm("deadline", time(), ticket=None)
            actions.append("arm-success")
        else:
            actions.extend(await owner.wait_next())
    assert actions == [] and not owner.fired
    assert bool(owner.pending) == (point in {"fire", "after-fire"})
    if point.startswith("after-"):
        assert len(owner.journal.read()) == len(before) + 1
    else:
        assert owner.journal.read() == before
    recovered = timers(tmp_path, time)
    recovered.reconstruct()
    assert bool(recovered.pending) == (point in {"fire", "after-arm"})
    assert bool(recovered.fired) == (point == "after-fire")
    green(recovered.journal)


def invalid_history(now):
    body = {"timer_id": "deadline", "deadline": render_ts(now)}
    bad = [{}, {**body, "timer_id": ""}, {**body, "timer_id": False},
           {**body, "deadline": now.replace(tzinfo=None).isoformat()},
           {**body, "deadline": "2026-01-01T00:00:00Z"},
           {**body, "deadline": "2026-01-01T01:00:00+01:00"},
           {**body, "deadline": 7}, {**body, "extra": "no"}]
    cases = [[(EventType.TIMER_ARMED, value, None, None)] for value in bad]
    cases += [[(EventType.TIMER_FIRED, body, None, None)],
              [(EventType.TIMER_ARMED, body, None, "key")],
              [(EventType.TIMER_ARMED, body, None, None),
               (EventType.TIMER_ARMED, {**body, "deadline": render_ts(now + timedelta(seconds=1))}, None, None)],
              [(EventType.TIMER_ARMED, body, "work", None), (EventType.TIMER_FIRED, body, None, None)],
              [(EventType.TIMER_ARMED, body, None, None),
               (EventType.TIMER_FIRED, {**body, "deadline": render_ts(now + timedelta(seconds=1))}, None, None)],
              [(EventType.TIMER_ARMED, body, None, None), (EventType.TIMER_FIRED, body, None, None),
               (EventType.TIMER_ARMED, body, "other", None)]]
    return cases


def test_timers_rearm_from_journal(tmp_path):
    time = Time()
    owner = timers(tmp_path / "valid", time)
    assert owner.fire_due() == () and owner.journal.read() == []
    absolute = time() + timedelta(seconds=100)
    owner.arm("future", absolute, ticket="future-owner")
    owner.arm("equal", time(), ticket=None)
    owner.arm("overdue", time() - timedelta(seconds=1), ticket="past-owner")
    # A separate segment pins arm/fire matching across boundaries, without adding a roll owner.
    owner.journal.dir.joinpath("000002-20260101.jsonl").touch()
    assert [t.body.timer_id for t in owner.fire_due()] == ["overdue", "equal"]
    time.advance(50)
    recovered = timers(tmp_path / "valid", time)
    recovered.reconstruct()
    assert recovered.pending["future"].deadline == absolute
    assert set(recovered.fired) == {"equal", "overdue"}
    assert recovered.fire_due() == ()
    before = recovered.journal.read()
    recovered.arm("equal", time() - timedelta(seconds=50), ticket=None)
    recovered.arm("future", absolute, ticket="future-owner")
    assert recovered.journal.read() == before
    time.advance(50)
    assert [t.body.timer_id for t in recovered.fire_due()] == ["future"]
    again = timers(tmp_path / "valid", time)
    again.reconstruct()
    assert not again.pending and set(again.fired) == {"future", "overdue", "equal"}
    assert again.fire_due() == () and len(list(again.journal.read_segments())) == 2
    assert all(set(t.body.model_dump()) == {"timer_id", "deadline"} for t in again.fired.values())
    green(again.journal)
    for n, history in enumerate(invalid_history(time())):
        member = timers(tmp_path / f"invalid-{n}", time)
        for type, body, ticket, key in history:
            member.journal.append(type, body, ticket=ticket, key=key)
        evidence = member.journal.read()
        with pytest.raises(ValueError, match="(?s)invalid timer record.*restore valid journal evidence"):
            member.reconstruct()
        assert not member.pending and not member.fired and member.journal.read() == evidence
        green(member.journal)


@pytest.mark.asyncio
async def test_timer_deadline_wait_uses_injected_sleep(tmp_path):
    time = Time()
    wake = asyncio.Queue()
    waits, cleanup = [], []

    async def sleep(seconds):
        waits.append(seconds)
        try:
            await wake.get()
        finally:
            cleanup.append(seconds)

    owner = Timers(journal=Journal(tmp_path, time), clock=time, sleep=sleep)
    for identity, seconds in (("later", 30), ("bb", 10), ("aa", 10)):
        owner.arm(identity, time() + timedelta(seconds=seconds), ticket=None)
    actions = []
    async def consume():
        result = await owner.wait_next()
        assert all(t.body.timer_id in owner.fired for t in result)
        actions.extend(t.body.timer_id for t in result)
        return result
    task = asyncio.create_task(consume())
    await turn()
    assert waits == [10] and actions == []
    time.advance(3)
    wake.put_nowait(None)
    await turn()
    assert waits == [10, 7] and actions == [] and not task.done()
    time.advance(7)
    wake.put_nowait(None)
    assert [t.body.timer_id for t in await task] == ["aa", "bb"]
    assert actions == ["aa", "bb"]
    task = asyncio.create_task(consume())
    await turn()
    assert waits[-1] == 20
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup == waits and task not in asyncio.all_tasks()
    assert set(owner.pending) == {"later"} and actions == ["aa", "bb"]
    time.advance(20)
    assert [t.body.timer_id for t in await consume()] == ["later"]
    assert await owner.wait_next() == ()
    green(owner.journal)


@pytest.mark.asyncio
async def test_production_restart_precedes_dispatch(root, monkeypatch):
    trace, locals = [], []
    entered, release = asyncio.Event(), asyncio.Event()

    async def prepare(local):
        assert rig.core.restart.ready
        assert local.journal is rig.journal is rig.core.restart.timers.journal
        assert "overdue" in rig.core.restart.timers.fired
        locals.append(local)
        trace.append("prepare")
        async def dispatch(ticket):
            trace.append("dispatch")
            local.journal.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket=ticket.stem)
            local.journal.append(EventType.STATE_TRANSITION, {"to": "already_satisfied"}, ticket=ticket.stem)
            return "already_satisfied"
        return dispatch

    rig = await repository(root, monkeypatch, prepare=prepare)
    lock = held(rig)
    offered = []
    try:
        dead = await orphan(rig, "dead")
        persisted = timers(rig.checkout.config.state_dir, rig.time)
        persisted.arm("completed", rig.time(), ticket=None)
        persisted.fire_due()
        persisted.arm("overdue", rig.time() - timedelta(seconds=1), ticket="timer-owner")
        future = rig.time() + timedelta(seconds=500)
        persisted.arm("future", future, ticket=None)
        lifetime = rig.core.restart.timers
        first, second = await rig.add("first"), await rig.add("second")
        original = stages.harvest_orphan
        async def harvest(checkout, stem, attempt):
            assert rig.core.admission.active is None and rig.core.admission.task is None
            contender = Lockfile(checkout.config.state_dir, instance_id="contender", clock=rig.time)
            with pytest.raises(LockHeld):
                contender.acquire()
            trace.append("reconcile")
            entered.set()
            await release.wait()
            await original(checkout, stem, attempt)
        monkeypatch.setattr(stages, "harvest_orphan", harvest)
        reconstruct, fire, snapshot = lifetime.reconstruct, lifetime.fire_due, daemon.snapshot_config
        def fold():
            assert not dead.exists()
            trace.append("rearm")
            reconstruct()
        def firing():
            trace.append("fire")
            return fire()
        def capture(config):
            trace.append("snapshot")
            assert rig.core.restart.ready
            return snapshot(config)
        monkeypatch.setattr(lifetime, "reconstruct", fold)
        monkeypatch.setattr(lifetime, "fire_due", firing)
        monkeypatch.setattr(daemon, "snapshot_config", capture)
        original_checkpoint = rig.core.control.checkpoint
        async def checkpoint():
            assert rig.core.restart.ready
            trace.append("pause")
            await original_checkpoint()
        monkeypatch.setattr(rig.core.control, "checkpoint", checkpoint)
        offered.append(asyncio.create_task(rig.core.admission.dispatch(first)))
        await entered.wait()
        offered.append(asyncio.create_task(rig.core.admission.dispatch(second)))
        await turn()
        assert trace == ["reconcile"] and not locals and not rig.core.restart.ready
        assert all(not task.done() for task in offered)
        assert not lifetime.pending and not lifetime.fired
        release.set()
        assert await asyncio.gather(*offered) == ["already_satisfied"] * 2
        assert trace == ["reconcile", "rearm", "fire", "pause", "snapshot", "prepare", "dispatch",
                         "pause", "snapshot", "prepare", "dispatch"]
        assert rig.core.restart.timers is lifetime
        assert lifetime.pending["future"].deadline == future
        assert set(lifetime.fired) == {"completed", "overdue"}
        events = rig.journal.read()
        assert len([e for e in events if e.type == EventType.TIMER_ARMED]) == 3
        assert len([e for e in events if e.type == EventType.TIMER_FIRED]) == 2
        abandoned = next(i for i, e in enumerate(events) if e.body.get("to") == "abandoned")
        fired = next(i for i, e in enumerate(events) if e.type == EventType.TIMER_FIRED and e.body["timer_id"] == "overdue")
        dispatched = next(i for i, e in enumerate(events) if e.ticket == "first")
        assert abandoned < fired < dispatched
        await rig.core.startup()
        assert rig.journal.read() == events and trace.count("reconcile") == trace.count("fire") == 1
        green(rig.journal)
    finally:
        release.set()
        await asyncio.gather(*offered, return_exceptions=True)
        lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["corruption", "git", "cleanup", "prune", "fold", "fire", "reconcile-append", "cancel"])
async def test_restart_failures_do_not_report_ready(root, monkeypatch, failure):
    prepared = []
    async def prepare(local):
        prepared.append(local)
        pytest.fail("dispatch crossed failed startup")
    rig = await repository(root, monkeypatch, prepare=prepare)
    lock = held(rig)
    tasks = []
    try:
        ticket = await rig.add("work")
        path = await orphan(rig, "dead")
        pending = timers(rig.checkout.config.state_dir, rig.time)
        pending.arm("deadline", rig.time(), ticket=None)
        entered, release = asyncio.Event(), asyncio.Event()
        original_reconcile = reconcile.reconcile
        async def recovery(*args):
            entered.set()
            await release.wait()
            return await original_reconcile(*args)
        monkeypatch.setattr(reconcile, "reconcile", recovery)
        error = OSError("scripted recovery failure")
        if failure == "corruption":
            rig.journal.dir.joinpath("000001-20260101.jsonl").open("ab").write(b"broken\n")
        elif failure == "git":
            (path / ".git").write_text("gitdir: /missing-metadata\n")
        elif failure in {"cleanup", "prune"}:
            async def fail(*args):
                raise error
            monkeypatch.setattr(rig.checkout.git, "worktree_remove" if failure == "cleanup" else "worktree_prune", fail)
        elif failure == "fold":
            rig.journal.append(EventType.TIMER_FIRED, {"timer_id": "unpaired", "deadline": render_ts(rig.time())})
        elif failure != "cancel":
            append = rig.journal.append
            def failed(type, body, **kwargs):
                if type == (EventType.TIMER_FIRED if failure == "fire" else EventType.STATE_TRANSITION):
                    raise error
                return append(type, body, **kwargs)
            monkeypatch.setattr(rig.journal, "append", failed)
        for _ in range(2):
            tasks.append(asyncio.create_task(rig.core.admission.dispatch(ticket)))
        await entered.wait()
        await turn()
        assert prepared == [] and not rig.core.restart.ready and all(not task.done() for task in tasks)
        if failure == "cancel":
            tasks[0].cancel()
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # Harvest errors are intentionally signalled by reconcile; corrupt Git during
        # removal still fails the startup, never a manufactured recovery success.
        assert all(isinstance(result, BaseException) for result in results)
        if failure == "cancel":
            assert all(isinstance(result, asyncio.CancelledError) for result in results)
        else:
            assert results[0] is results[1]
        assert not rig.core.restart.ready and prepared == []
        assert rig.core.admission.active is None and rig.core.admission.task is None
        with pytest.raises(type(results[0])):
            await rig.core.startup()
        with pytest.raises(type(results[0])):
            await rig.core.sweep_orphans()
        if failure == "corruption":
            with pytest.raises(JournalCorruption):
                audit_journal(rig.journal)
        else:
            green(rig.journal)
    finally:
        await asyncio.gather(*tasks, return_exceptions=True)
        lock.release()
