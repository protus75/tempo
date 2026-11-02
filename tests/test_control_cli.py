"""Real CLI lock routing and identity-bound durable requests."""

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from tempo import __main__ as entry, control
from tempo.journal import Journal
from tempo.lockfile import LockHeld, Lockfile
from tempo.seams import LocalFileSystem
from tests.test_cli import Clock, ENV, Stages, root, ticket, write


@pytest.mark.parametrize("case", ["draft", "Reject"])
def test_confirm_without_writer_lock(tmp_path, case):
    import asyncio
    from tests.test_mergequeue import ctx as context_fixture, ready, commit, admission_control
    from tempo import runner
    from tempo.journal import EventType
    from tempo.tickets import stamp
    ctx = context_fixture.__wrapped__(tmp_path)
    async def exercise():
        ticket = await ready(ctx)
        path = ctx.repo / f"tickets/{ticket.stem}/ticket.md"
        if case == "Reject":
            ctx.driver.journal.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket=ticket.stem)
            ctx.driver.journal.append(EventType.STATE_TRANSITION, {"to": "gate_failed", "routed": "reject_queue"}, ticket=ticket.stem)
            assert await asyncio.to_thread(entry.main, ["confirm", ticket.stem], cwd=ctx.repo, env=ctx.env, clock=ctx.driver.clock) == 0
            assert await asyncio.to_thread(entry.main, ["confirm", ticket.stem], cwd=ctx.repo, env=ctx.env, clock=ctx.driver.clock) == 2
            assert [e.body["verdict"] for e in ctx.driver.journal.read() if e.body.get("signal") == "reject_verdict"] == ["keep"]
            return
        ctx.fs.write(path, stamp(path.read_text(), "state", "draft").encode())
        await commit(ctx, ctx.repo, [f"tickets/{ticket.stem}/ticket.md"])
        ctx.driver.journal.append(EventType.SIGNAL, {"signal": "ticket_intake", "state": "draft"}, ticket=ticket.stem)
        consumer = admission_control(ctx)
        checkout = runner.Checkout(ctx.repo, ctx.config, ctx.env, ctx.exec_, ctx.git, ctx.driver.journal, ctx.fs, ctx.driver.clock, ctx.driver.sleep)
        consumer.inbox.draft = lambda stem: runner.committed_draft(checkout, stem)
        lock = Lockfile(ctx.config.state_dir, instance_id="live", clock=ctx.driver.clock)
        lock.acquire()
        control.write_active(ctx.config.state_dir, consumer.projection, ctx.fs, hold_id=None)
        try:
            assert await asyncio.to_thread(entry.main, ["confirm", ticket.stem], cwd=ctx.repo, env=ctx.env, clock=ctx.driver.clock) == 0
            consumer.inbox.consume()
            async def confirmed(stem, request_id): await runner.confirm_draft(stem, request_id, ctx)
            await consumer.inbox.settle_admissions(confirmed)
            events = ctx.driver.journal.read()
            await consumer.inbox.settle_admissions(confirmed)
            assert ctx.driver.journal.read() == events
            assert "state: confirmed" in path.read_text()
            assert sum(e.body.get("signal") == "draft_confirmed" for e in events) == 1
        finally:
            lock.release()
    asyncio.run(exercise())


def invoke(root, verb):
    def forbidden(_):
        pytest.fail("control constructed a pipeline")
    return entry.main([verb], cwd=root, env=ENV, clock=Clock(), pipeline=forbidden)


def acquire(root):
    lock = Lockfile(root / ".tempo/state", instance_id="diagnostic-not-lifecycle", clock=Clock())
    lock.acquire()
    return lock


def discovery(root, life="restart-unique", hold=None):
    control.write_active(root / ".tempo/state", control.ControlProjection(life, hold), LocalFileSystem(), hold_id=hold)


def requests(root):
    return [json.loads(p.read_bytes()) for p in sorted((root / ".tempo/state/control/inbox").glob("*.json"))]


def test_live_pause_resume_publish_without_journal_write(root, monkeypatch, capsys):
    lock = acquire(root)
    ids = iter(["pause-exact", "resume-exact"])
    monkeypatch.setattr(entry, "uuid4", lambda: SimpleNamespace(hex=next(ids)))
    def forbidden(*args, **kwargs):
        pytest.fail("publisher wrote the journal or took the writer role")
    monkeypatch.setattr(Journal, "append", forbidden)
    monkeypatch.setattr(entry, "build_control", forbidden)
    monkeypatch.setattr(Lockfile, "release", forbidden)
    try:
        discovery(root, "life-exact")
        assert invoke(root, "pause") == 0
        assert capsys.readouterr().out == "pause submitted: pause-exact\n"
        discovery(root, "life-exact", "pause-exact")
        assert invoke(root, "resume") == 0
        assert capsys.readouterr().out == "resume submitted: resume-exact\n"
        assert requests(root) == [asdict(control.ControlRequest("pause-exact", "life-exact", "pause", None)),
                                  asdict(control.ControlRequest("resume-exact", "life-exact", "resume", "pause-exact"))]
        assert not (root / ".tempo/state/journal").exists()
        contender = Lockfile(lock.state_dir, instance_id="contender", clock=Clock())
        with pytest.raises(LockHeld):
            contender.acquire()
    finally:
        monkeypatch.undo()
        lock.release()


@pytest.mark.parametrize("raw", [None, b"null\n", b"{", b"{}", b'[]',
    b'{"lifecycle_id":"","hold_id":"hold"}', b'{"lifecycle_id":1,"hold_id":null}',
    b'{"lifecycle_id":"life","hold_id":""}', b'{"lifecycle_id":"life","hold_id":true}',
    b'{"lifecycle_id":"life","hold_id":null,"extra":1}',
    b'{"lifecycle_id":"life","lifecycle_id":"other","hold_id":null}',
    b'{"lifecycle_id":"life","hold_id":null}'])
def test_resume_requires_current_pause_identity(root, raw, capsys):
    lock = acquire(root)
    try:
        if raw is not None:
            LocalFileSystem().write(lock.state_dir / "control/active.json", raw)
        assert invoke(root, "resume") == 2
        err = capsys.readouterr().err
        assert "current control identity" in err and "submit a new request" in err
        assert requests(root) == []
        if raw != b'{"lifecycle_id":"life","hold_id":null}':
            assert "retry after the running engine publishes" in err
            assert invoke(root, "pause") == 2
            assert requests(root) == []
    finally:
        lock.release()


@pytest.mark.parametrize("verb", ["pause", "resume"])
@pytest.mark.parametrize("stale", [False, True])
def test_pause_resume_apply_directly_under_lock(root, monkeypatch, capsys, verb, stale):
    state = root / ".tempo/state"
    if stale:
        discovery(root, "old-life", "old-hold")
        control.publish_request(state, control.ControlRequest("old-pause", "old-life", "pause", None), LocalFileSystem())
    def contents():
        return {p.relative_to(state): p.read_bytes() for p in state.rglob("*") if p.is_file() and p.name != "tempo.lock"}
    before = contents()
    trace = []
    original_acquire, original_release = Lockfile.acquire, Lockfile.release
    def acquired(self):
        original_acquire(self)
        trace.append("acquire")
    def released(self):
        assert self.held
        trace.append("release")
        original_release(self)
    def forbidden(*args, **kwargs):
        pytest.fail("idle control allocated identity, constructed consumer or changed a record")
    with monkeypatch.context() as patch:
        patch.setattr(Lockfile, "acquire", acquired)
        patch.setattr(Lockfile, "release", released)
        patch.setattr(entry, "uuid4", forbidden)
        patch.setattr(entry, "build_control", forbidden)
        patch.setattr(control, "read_active", forbidden)
        patch.setattr(control, "publish_request", forbidden)
        patch.setattr(control, "write_active", forbidden)
        patch.setattr(Journal, "append", forbidden)
        assert invoke(root, verb) == 0
    assert capsys.readouterr().out == f"nothing running to {verb}\n"
    assert trace == ["acquire", "release"] and contents() == before
    lock = acquire(root)
    lock.release()
    write(root, "work", ticket())
    stages = Stages()
    lifecycles = []
    original = entry.build_control
    def build(checkout):
        consumer = original(checkout)
        lifecycles.append(consumer.projection.lifecycle_id)
        return consumer
    monkeypatch.setattr(entry, "build_control", build)
    assert entry.main(["drain"], cwd=root, env=ENV, clock=Clock(), pipeline=stages) == 0
    assert stages.calls == ["work"] and stages.lock_held == [True]
    assert len(lifecycles) == 1 and lifecycles[0] != "old-life"
    assert (state / "control/active.json").read_bytes() == b"null\n"
    if stale:
        [decision] = [e.body for e in stages.checkout.journal.read() if e.body.get("kind") == control.CONTROL_DECISION]
        assert decision["decision"] == "stale" and decision["lifecycle_id"] == "old-life"


def test_control_routing_lock_and_restart_races(root, monkeypatch, capsys):
    original = Lockfile.acquire
    next_owner = Lockfile(root / ".tempo/state", instance_id="next-owner", clock=Clock())
    replacement = Lockfile(root / ".tempo/state", instance_id="replacement", clock=Clock())
    # No preflight discovery check: an engine wins the CLI's actual lock acquisition.
    def race(self):
        if self is not replacement and not replacement.held:
            original(replacement)
        return original(self)
    monkeypatch.setattr(Lockfile, "acquire", race)
    try:
        assert invoke(root, "pause") == 2
        assert "retry after" in capsys.readouterr().err and requests(root) == []
        discovery(root, "old-life", "old-hold")
        publish = control.publish_request
        def replace_before_publication(state, request, fs):
            replacement.release()
            original(next_owner)
            discovery(root, "new-life")
            publish(state, request, fs)
        monkeypatch.setattr(control, "publish_request", replace_before_publication)
        assert invoke(root, "resume") == 0
        [request] = requests(root)
        assert request["lifecycle_id"] == "old-life" and request["hold_id"] == "old-hold"
        from tests.test_control import Rig
        new = Rig(replacement.state_dir, lifecycle="new-life")
        new.inbox.consume()
        assert new.decisions()[0]["decision"] == "stale" and new.projection.pause_id is None
        assert requests(root) == [request]
        control.write_active(replacement.state_dir, None, LocalFileSystem(), hold_id=None)
        assert invoke(root, "pause") == 2
        assert len(requests(root)) == 1
    finally:
        if replacement.held:
            replacement.release()
        if next_owner.held:
            next_owner.release()


@pytest.mark.parametrize("binding", ["identity", "durability", "idle-lock"])
def test_cli_observables_detect_removed_bindings(root, monkeypatch, binding):
    if binding == "idle-lock":
        trace = []
        original = Lockfile.acquire
        def traced_acquire(lock):
            trace.append("lock")
            original(lock)
        monkeypatch.setattr(Lockfile, "acquire", traced_acquire)
        async def bypass(checkout, verb):
            return 0
        monkeypatch.setattr(entry, "_control", bypass)
        assert invoke(root, "pause") == 0
        with pytest.raises(AssertionError):
            assert trace == ["lock"]
        return
    lock = acquire(root)
    try:
        discovery(root, "target-life", "exact-hold")
        original = control.publish_request
        def broken(state, request, fs):
            if binding == "identity":
                original(state, control.ControlRequest(request.request_id, "wrong-life", "resume", "wrong-hold"), fs)
        monkeypatch.setattr(control, "publish_request", broken)
        assert invoke(root, "resume") == 0
        with pytest.raises(AssertionError):
            values = requests(root)
            assert len(values) == 1
            assert values[0]["lifecycle_id"] == "target-life" and values[0]["hold_id"] == "exact-hold"
    finally:
        lock.release()


def test_resume_publishes_selected_hold_identity(root, monkeypatch, capsys):
    lock = acquire(root)
    ids = iter(["first-resume", "second-resume"])
    monkeypatch.setattr(entry, "uuid4", lambda: SimpleNamespace(hex=next(ids)))
    try:
        # Selection is explicit: a dispatch pause projection must not replace the supplied admission id.
        control.write_active(lock.state_dir, control.ControlProjection("life", "pause"),
                             LocalFileSystem(), hold_id="admission-exact")
        assert invoke(root, "resume") == 0
        assert requests(root) == [asdict(control.ControlRequest(
            "first-resume", "life", "resume", "admission-exact"))]
        assert invoke(root, "resume") == 0
        assert len(requests(root)) == 2
        assert all(r["hold_id"] == "admission-exact" for r in requests(root))
        discovery(root, "life")
        assert invoke(root, "resume") == 2
        assert "no current hold identity" in capsys.readouterr().err
        assert len(requests(root)) == 2
    finally:
        lock.release()
