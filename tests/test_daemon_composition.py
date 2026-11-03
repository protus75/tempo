"""Drive the production CLI core through injected delivery, process, filesystem and time seams."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from tempo import __main__ as cli, caps, daemon, merge, runner, stages
from tempo.config import ConfigError, ConfigSnapshot, load_config
from tempo.daemon import DaemonAdmission
from tempo.git import Git
from tempo.journal import EventType, Journal
from tempo.llm import FakeLLM, LLMResult
from tempo.mergequeue import CONFLICT_FACTS, MergeQueue
from tempo.scheduler import Scheduler
from tempo.tickets import INTAKE_SIGNAL, validate_ticket
from tempo.watcher import WATCHER_PARSE_FAILURE, Watcher
from tests.test_cli import root, write  # noqa: F401 -- fixture
from tests.test_daemon_admission import assert_idle
from tests.test_daemon_config import assert_detached, python_values
from tests.test_scheduler import Time, import_closure, text, turn
from tests.test_mergequeue import ctx as admission_context, ready  # noqa: F401 -- fixture
from tempo import control
from tempo.checkout import Checkout


CONFIG = """\
schema_version: 1
state_dir: state
review: {}
merge: {}
hosts:
  claude:
    models_by_tier: {low: sonnet, medium: sonnet, high: sonnet, max: sonnet}
    pins: {review: opus}
orchestrator:
  models: {claude: opus}
engine_plane_safety_inventory: []
"""

ENV = {"PATH": "/usr/bin", "HOME": "/home/op"}


class ScriptFS:
    def __init__(self):
        self.files = {}

    def write(self, path, data):
        self.files[path] = data

    def replace(self, src, dst):
        self.files[dst] = self.files.pop(src)


class ScriptExec:
    """The rig's process seam: tempo runs no agent CLI, so any child it is asked for is a test failure."""

    def __init__(self, fs):
        self.fs = fs
        self.calls = []
        self.hook = None
        self.reply = "ok"

    async def run(self, argv, *, cwd, env, timeout, stdin_path=None, on_spawn=None, on_stdout_line=None):
        self.calls.append((list(argv), cwd, dict(env), None, timeout))
        if self.hook is not None:
            await self.hook(argv)
        raise AssertionError(f"unexpected child process {argv}")

    def kill_group(self, pgid):
        raise AssertionError("no scripted child survives its invocation")


class ScriptLLM:
    """The rig's model: every call answers with the exec seam's current `reply`."""

    kind = "cli"

    def __init__(self, exec_):
        self.exec = exec_
        self.requests = []

    async def call(self, req):
        self.requests.append(req)
        return LLMResult(text=self.exec.reply, provider="claude", model="sonnet")

    def abort_current(self):
        pass


class CoreRig:
    def __init__(self, root, *, prepare=None, config=CONFIG, config_path=None):
        self.root = root
        (root / "tempo").mkdir(exist_ok=True)
        (root / "tempo/thing.py").write_text("")
        self.config_path = config_path
        self.put_config(config)
        self.time = Time()
        self.fs = ScriptFS()
        self.exec = ScriptExec(self.fs)
        self.journal = Journal(root / "state", self.time)
        initial = load_config(config_path, cwd=root)
        self.checkout = Checkout(
            root, initial, ENV, self.exec,
            Git(self.exec, env=dict(ENV), timeout=30),
            self.journal, self.fs, self.time, self.time.sleep,
        )
        self.files = {}
        self.quarantine, self.drought = set(), set()
        self.unmerged = 0
        self.calls, self.locals = [], []
        self.llm = ScriptLLM(self.exec)

        async def bound(checkout):
            return runner.bind(checkout, self.llm)

        self.prepare = prepare or bound
        self.core = cli.build_daemon_core(
            self.checkout, config_path=config_path, read=self.files.get, debounce=5,
            quarantined=lambda: self.quarantine, drought_parked=lambda: self.drought,
            completed_unmerged=lambda: self.unmerged, prepare=self.prepare,
        )

    def put_config(self, config):
        (self.config_path or self.root / "config.yaml").write_text(config)

    async def publish(self, *stems):
        async def changes():
            for stem in stems:
                yield stem

        consumer = asyncio.create_task(self.core.watcher.run(changes()))
        await turn()
        await turn()
        self.time.advance(5)
        await consumer

    async def add(self, stem, priority="P2", state="confirmed", depends="none"):
        self.files[stem] = text(priority, state, depends)
        write(self.root, stem, self.files[stem])
        await self.publish(stem)
        return self.core.scheduler.pending[stem]

    def transition(self, stem, to, **body):
        self.journal.append(EventType.STATE_TRANSITION, {"to": to, **body}, ticket=stem)

    def intake(self, stem):
        self.journal.append(EventType.SIGNAL,
                            {"signal": INTAKE_SIGNAL, "source": "human", "state": "confirmed",
                             "new": True, "commit": "abc"}, ticket=stem)

    async def drain(self):
        while await self.core.scheduler.dispatch_next() is not None:
            pass


def assert_core_wiring(rig):
    core = rig.core
    assert isinstance(core.admission, DaemonAdmission)
    assert isinstance(core.scheduler, Scheduler) and isinstance(core.watcher, Watcher)
    assert core.scheduler.journal is core.watcher.journal is rig.checkout.journal
    assert core.scheduler.dispatch == core.admission.dispatch
    assert core.watcher.publish == core.scheduler.update
    assert core.watcher.remove == core.scheduler.remove
    assert core.watcher.read == rig.files.get
    assert core.watcher.clock is rig.checkout.clock and core.watcher.sleep == rig.checkout.sleep
    assert core.watcher.debounce == 5
    assert core.scheduler.max_unmerged == rig.checkout.config.scheduler.max_unmerged


def assert_startup_pause_wiring(core):
    boundary = core.admission._before_dispatch
    assert boundary is not None
    assert isinstance(boundary.__self__, daemon.StartupBoundary)
    assert boundary == boundary.__self__.checkpoint
    assert boundary.__self__.restart is core.restart
    assert boundary.__self__.pause is core.control


@pytest.fixture
def rig(tmp_path):
    async def prepare(local):
        rig.locals.append(local)

        async def dispatch(ticket):
            rig.calls.append(ticket.stem)
            return "callback-owned-terminal"

        return dispatch

    rig = CoreRig(tmp_path, prepare=prepare)
    return rig


@pytest.mark.asyncio
async def test_production_root_builds_daemon_core_without_running_work(tmp_path, monkeypatch):
    async def prepare(_):
        raise AssertionError("prepared at construction")

    def forbid(*args, **kwargs):
        raise AssertionError("captured at construction")

    # Initial mutable checkout loading is separate from the root's per-admission loader.
    original = cli.load_config
    loads = []

    def load(*args, **kwargs):
        loads.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(cli, "load_config", load)
    monkeypatch.setattr(daemon, "snapshot_config", forbid)
    tasks = asyncio.all_tasks()
    rig = CoreRig(tmp_path, prepare=prepare)
    assert_core_wiring(rig)
    assert_idle(rig.core.admission)
    assert not rig.core.scheduler.pending and rig.core.scheduler.active is None
    assert not rig.core.watcher.cache and not rig.core.watcher._waits
    assert loads == [] and rig.exec.calls == [] and rig.fs.files == {}
    assert rig.journal.read() == [] and asyncio.all_tasks() == tasks


@pytest.mark.asyncio
async def test_production_core_dispatches_through_admission_and_snapshot(tmp_path):
    called = []

    async def prepare(local):
        assert local is not rig.checkout
        assert isinstance(local.config, ConfigSnapshot)
        assert_detached(rig.checkout.config, local.config)
        for field in ("repo", "env", "exec_", "git", "journal", "fs", "clock", "sleep"):
            assert getattr(local, field) is getattr(rig.checkout, field)
        called.append(local)

        async def callback(original):
            assert original is ticket
            assert rig.core.admission.active is original
            assert rig.core.admission.task is asyncio.current_task()
            with pytest.raises(AttributeError):
                local.config.caps.retry = 99
            return "original-terminal"

        return callback

    rig = CoreRig(tmp_path, prepare=prepare)
    ticket = await rig.add("work")
    assert await rig.core.scheduler.dispatch(ticket) == "original-terminal"
    assert len(called) == 1 and rig.journal.read() == []
    assert_idle(rig.core.admission)


@pytest.mark.asyncio
@pytest.mark.parametrize("new_ticket", [False, True])
async def test_production_core_reprioritizes_without_preemption(tmp_path, new_ticket):
    started, release, cleaning, finish = (asyncio.Event() for _ in range(4))
    calls = []

    async def prepare(local):
        async def callback(ticket):
            calls.append(ticket.stem)
            if ticket.stem == "active":
                try:
                    started.set()
                    await release.wait()
                finally:
                    cleaning.set()
                    await finish.wait()
            return "merged"
        return callback

    rig = CoreRig(tmp_path, prepare=prepare)
    for stem, priority in (("active", "P0"), ("normal", "P1"), ("urgent", "P3")):
        await rig.add(stem, priority)
    first = asyncio.create_task(rig.core.scheduler.dispatch_next())
    await started.wait()
    owned = rig.core.admission.task
    waiter = asyncio.create_task(rig.core.scheduler.dispatch_next())
    stem = "new-urgent" if new_ticket else "urgent"
    rig.files[stem] = text("P0")
    rig.core.watcher.change(stem)
    await turn()
    rig.time.advance(4)
    await turn()
    assert stem not in rig.core.scheduler.pending or rig.core.scheduler.pending[stem].frontmatter.priority == "P3"
    rig.time.advance(1)
    await turn()
    assert rig.core.scheduler.pending[stem].frontmatter.priority == "P0"
    release.set()
    await cleaning.wait()
    assert calls == ["active"] and not first.done() and not waiter.done()
    assert rig.core.scheduler.active is rig.core.admission.active
    assert rig.core.admission.task is owned and not owned.done()
    finish.set()
    assert (await first).stem == "active" and (await waiter).stem == stem
    assert calls == ["active", stem] and owned.done()
    assert_idle(rig.core.admission)
    assert rig.core.scheduler.active is None
    await rig.core.watcher.close()


@pytest.mark.asyncio
async def test_production_core_preserves_last_good_and_removes_tickets(rig):
    old = await rig.add("old", "P2")
    await rig.add("other", "P1")
    rig.files["old"] = text("P0").replace("- stuck: 20m", "- stuck: broken")
    rig.files["new-invalid"] = "partial"
    await rig.publish("old", "new-invalid")
    assert rig.core.watcher.cache["old"] is rig.core.scheduler.pending["old"] is old
    assert "new-invalid" not in rig.core.watcher.cache and "new-invalid" not in rig.core.scheduler.pending
    events = rig.journal.read()
    assert len(events) == 2
    for event, stem in zip(events, ("old", "new-invalid"), strict=True):
        assert event.type == EventType.SIGNAL and event.ticket == stem and event.key is None
        assert event.body == {"signal": WATCHER_PARSE_FAILURE, "path": f"tickets/{stem}/ticket.md",
                              "reason": event.body["reason"]}
        assert isinstance(event.body["reason"], str) and event.body["reason"].strip()
    await rig.drain()
    assert rig.calls == ["other", "old"]
    rig.files.update({"old": text("P0"), "new-invalid": text("P1")})
    await rig.publish("old", "new-invalid")
    assert rig.core.watcher.cache["old"] is not old
    del rig.files["old"]
    await rig.publish("old")
    assert "old" not in rig.core.watcher.cache and "old" not in rig.core.scheduler.pending
    await rig.drain()
    assert rig.calls == ["other", "old", "new-invalid"] and rig.journal.read() == events


@pytest.mark.asyncio
async def test_production_core_preserves_eligibility_order_and_backpressure(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    async def prepare(local):
        async def callback(ticket):
            rig.calls.append(ticket.stem)
            if ticket.stem == "pressure-aa":
                started.set()
                await release.wait()
                rig.unmerged = 2
            return "merged"
        return callback

    rig = CoreRig(tmp_path, prepare=prepare)
    await rig.core.startup()
    for state in ("draft", "rejected", "merged"):
        await rig.add(state, state=state)
    await rig.add("parent", state="merged")
    await rig.add("noop", state="merged")
    child = await rig.add("child", depends="- parent\n- noop")
    assert child.depends == ("parent", "noop")
    for stem in ("reject-held", "quarantine", "drought", "settled", "retired", "running"):
        await rig.add(stem)
    rig.transition("settled", "already_satisfied")
    rig.transition("retired", "rejected")
    rig.transition("running", "running")
    rig.transition("reject-held", "gate_failed", routed="reject_queue")
    rig.quarantine.add("quarantine")
    rig.drought.add("drought")
    assert await rig.core.scheduler.dispatch_next() is None
    rig.transition("parent", "merged")
    assert await rig.core.scheduler.dispatch_next() is None
    rig.transition("noop", "already_satisfied")
    assert await rig.core.scheduler.dispatch_next() is child
    assert await rig.core.scheduler.dispatch_next() is None
    for actor in ("operator", "machine"):
        rig.journal.append(EventType.SIGNAL,
                           {"signal": "reject_verdict", "verdict": "keep", "actor": actor}, ticket="reject-held")
        assert (await rig.core.scheduler.dispatch_next()).stem == "reject-held"
        if actor == "operator":
            await rig.add("reject-held")
            rig.transition("reject-held", "gate_failed", routed="reject_queue")
            assert await rig.core.scheduler.dispatch_next() is None
    rig.quarantine.clear()
    assert (await rig.core.scheduler.dispatch_next()).stem == "quarantine"
    rig.drought.clear()
    assert (await rig.core.scheduler.dispatch_next()).stem == "drought"
    rig.calls.clear()
    for stem, priority in (("zzz-old", "P2"), ("aaa-new", "P2"), ("aaa-missing", "P2"),
                           ("bbb-missing", "P2"), ("urgent", "P0"), ("low", "P3"),
                           ("mid", "P1"), ("tie-aa", "P2"), ("tie-bb", "P2")):
        await rig.add(stem, priority)
    rig.intake("low")
    rig.intake("zzz-old")
    rig.time.advance(10)
    rig.intake("aaa-new")
    rig.time.advance(10)
    rig.intake("zzz-old")
    rig.intake("tie-bb")
    rig.intake("tie-aa")
    for count in (2, 3):
        rig.unmerged = count
        assert await rig.core.scheduler.dispatch_next() is None
    rig.unmerged = 1
    await rig.drain()
    assert rig.calls == ["urgent", "mid", "zzz-old", "aaa-new", "tie-aa", "tie-bb",
                         "aaa-missing", "bbb-missing", "low"]
    await rig.add("pressure-aa")
    await rig.add("pressure-bb")
    first = asyncio.create_task(rig.core.scheduler.dispatch_next())
    await started.wait()
    waiting = asyncio.create_task(rig.core.scheduler.dispatch_next())
    release.set()
    assert (await first).stem == "pressure-aa"
    assert await waiting is None
    rig.unmerged = 0
    assert (await rig.core.scheduler.dispatch_next()).stem == "pressure-bb"
    assert all(e.type != EventType.CAP_CONSUMED for e in rig.journal.read())


@pytest.mark.asyncio
async def test_production_core_reloads_config_only_after_admission(tmp_path):
    started, release, cleaning, finish = (asyncio.Event() for _ in range(4))
    locals, finished = [], []

    async def prepare(local):
        if locals:
            assert finished == ["first"]
        locals.append(local)

        async def callback(ticket):
            if ticket.stem == "first":
                expected = python_values(local.config)
                try:
                    started.set()
                    await release.wait()
                    assert python_values(local.config) == expected
                finally:
                    cleaning.set()
                    await finish.wait()
                    finished.append(ticket.stem)
            return "merged"
        return callback

    explicit = tmp_path / "explicit.yaml"
    rig = CoreRig(tmp_path, prepare=prepare, config_path=explicit)
    (tmp_path / "config.yaml").write_text("invalid default is never loaded")
    first_ticket, next_ticket = await rig.add("first"), await rig.add("second")
    first = asyncio.create_task(rig.core.admission.dispatch(first_ticket))
    await started.wait()
    waiting = asyncio.create_task(rig.core.admission.dispatch(next_ticket))
    await turn()
    assert len(locals) == 1
    rig.put_config(CONFIG.replace("pins: {review: opus}", "pins: {review: edited}") + "scheduler: {max_unmerged: 5}\n")
    release.set()
    await cleaning.wait()
    assert len(locals) == 1 and not waiting.done()
    finish.set()
    assert await asyncio.gather(first, waiting) == ["merged", "merged"]
    before, after = [local.config for local in locals]
    assert before is not after
    assert (before.scheduler.max_unmerged, after.scheduler.max_unmerged) == (2, 5)
    assert (before.hosts["claude"].pins["review"], after.hosts["claude"].pins["review"]) == ("opus", "edited")
    rig.put_config("bad: [")
    with pytest.raises(ConfigError):
        await rig.core.admission.dispatch(next_ticket)
    assert len(locals) == 2
    assert_idle(rig.core.admission)
    rig.put_config(CONFIG)
    assert await rig.core.admission.dispatch(next_ticket) == "merged" and len(locals) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["load", "bind", "callback", "cancel"])
async def test_production_core_unwinds_errors_and_cancellation(tmp_path, monkeypatch, failure):
    started, cleaning, release = (asyncio.Event() for _ in range(3))
    calls, prepared = [], []
    error = ValueError("original failure")
    failing = True

    async def prepare(local):
        prepared.append(local)

        async def callback(ticket):
            calls.append(ticket)
            if failing and failure == "callback":
                try:
                    raise error
                finally:
                    cleaning.set()
                    await release.wait()
            if failing and failure == "cancel":
                try:
                    started.set()
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release.wait()
            return "merged"
        return callback

    rig = CoreRig(tmp_path, prepare=prepare)
    first_ticket, second_ticket = await rig.add("first"), await rig.add("second")
    original_load, original_replace = cli.load_config, cli.replace

    def load(*args, **kwargs):
        if failing and failure == "load":
            raise error
        return original_load(*args, **kwargs)

    def bind(*args, **kwargs):
        if failing and failure == "bind":
            raise error
        return original_replace(*args, **kwargs)

    monkeypatch.setattr(cli, "load_config", load)
    monkeypatch.setattr(cli, "replace", bind)
    if failure == "cancel":
        first = asyncio.create_task(rig.core.scheduler.dispatch_next())
        await started.wait()
        owned = rig.core.admission.task
        waiter = asyncio.create_task(rig.core.scheduler.dispatch_next())
        await turn()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert calls == [first_ticket] and len(prepared) == 1
        assert not owned.cancelling()
        first.cancel()
        await cleaning.wait()
        second = asyncio.create_task(rig.core.scheduler.dispatch_next())
        await turn()
        assert rig.core.scheduler.active is rig.core.admission.active is first_ticket
        assert rig.core.admission.task is owned and not second.done()
        failing = False
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert owned.done() and owned.cancelled() and owned not in asyncio.all_tasks()
        assert await second is second_ticket
    elif failure == "callback":
        first = asyncio.create_task(rig.core.scheduler.dispatch_next())
        await cleaning.wait()
        owned = rig.core.admission.task
        second = asyncio.create_task(rig.core.scheduler.dispatch_next())
        await turn()
        assert calls == [first_ticket] and len(prepared) == 1 and not second.done()
        assert rig.core.scheduler.active is rig.core.admission.active is first_ticket
        assert rig.core.admission.task is owned and not owned.done()
        failing = False
        release.set()
        with pytest.raises(ValueError) as caught:
            await first
        assert caught.value is error and owned.done()
        assert await second is second_ticket
    else:
        with pytest.raises(ValueError) as caught:
            await rig.core.scheduler.dispatch_next()
        assert caught.value is error
        assert_idle(rig.core.admission)
        assert rig.core.scheduler.active is None
        assert prepared == []
        failing = False
        assert await rig.core.scheduler.dispatch_next() is second_ticket
    assert_idle(rig.core.admission)
    assert rig.core.scheduler.active is None and calls[-1] is second_ticket


def capture_pipeline_queue(monkeypatch):
    captured = []
    compose = runner.compose_pipeline

    def recording(ctx, *, escalate, control):
        queue = compose(ctx, escalate=escalate, control=control)
        captured.append((ctx, escalate, queue))
        return queue

    monkeypatch.setattr(runner, "compose_pipeline", recording)
    return captured


@pytest.mark.asyncio
async def test_production_pipeline_composes_merge_queue(tmp_path, monkeypatch):
    captured = capture_pipeline_queue(monkeypatch)
    rig = CoreRig(tmp_path)
    assert captured == []
    callback = runner.bind(replace(rig.checkout, control=rig.core.control), FakeLLM([]))
    [(ctx, consumer, queue)] = captured
    assert isinstance(queue, MergeQueue) and queue.ctx is ctx and queue.escalate is consumer
    for field in ("repo", "config", "env", "exec_", "git", "fs"):
        assert getattr(ctx, field) is getattr(rig.checkout, field)
    assert ctx.driver.clock is rig.checkout.clock and ctx.driver.sleep is rig.checkout.sleep
    assert ctx.driver.effects._journal is ctx.driver.journal
    assert ctx.driver.spool._fs is ctx.fs
    assert queue.pending == {} and queue.active is None and not queue.paused and queue.red_stems == []
    seen = []

    async def dispatch(context, ticket):
        assert context is ctx
        seen.append(ticket)
        return "merged"

    monkeypatch.setattr(runner, "drive", dispatch)
    ticket = await rig.add("work")
    assert await callback(ticket) == "merged" and seen == [ticket]
    event = rig.journal.append(EventType.SIGNAL, {"kind": "merge_red_streak", "stems": ["work"], "limit": 3},
                               ticket="work")
    before = ctx.driver.journal.read()
    assert consumer(event) is None
    assert ctx.driver.journal.read() == before and rig.exec.calls == [] and rig.fs.files == {}
    recorded = []
    supplied = recorded.append
    direct = merge.compose_pipeline(ctx, escalate=supplied, control=rig.core.control)
    assert direct.ctx is ctx and direct.escalate is supplied
    direct.escalate(event)
    assert recorded == [event] and ctx.driver.journal.read() == before
    with pytest.raises(TypeError):
        merge.compose_pipeline(ctx)


@pytest.mark.asyncio
async def test_merge_queue_composition_has_no_side_effects(tmp_path, monkeypatch):
    rig = CoreRig(tmp_path)
    captured = capture_pipeline_queue(monkeypatch)

    def forbidden(*args, **kwargs):
        pytest.fail("composition performed admission or an external effect")

    for owner, names in ((MergeQueue, ("offer", "process", "_signal", "_hold")),
                         (control.ControlInbox, ("consume", "recover")),
                         (rig.core.control.inbox, ("apply", "files", "read")),
                         (Git, ("_call",)), (stages, ("gather_evidence", "gather_safety_evidence")),
                         (Journal, ("append",)), (asyncio, ("create_task",)),
                         (rig.exec, ("run",)), (rig.fs, ("write", "replace"))):
        for name in names:
            monkeypatch.setattr(owner, name, forbidden)
    tasks = asyncio.all_tasks()
    callback = runner.bind(replace(rig.checkout, control=rig.core.control), FakeLLM([]))
    [(ctx, _, queue)] = captured
    assert callable(callback) and queue.ctx is ctx
    direct = merge.compose_pipeline(ctx, escalate=forbidden, control=rig.core.control)
    assert direct.ctx is ctx
    assert asyncio.all_tasks() == tasks and rig.journal.read() == []
    assert rig.exec.calls == [] and rig.fs.files == {}


@pytest.mark.asyncio
async def test_composed_merge_queue_admits_reviewed_candidate(admission_context, monkeypatch):
    source = admission_context
    checkout = Checkout(source.repo, source.config, source.env, source.exec_, source.git,
                               source.driver.journal, source.fs, source.driver.clock, source.driver.sleep)
    captured = capture_pipeline_queue(monkeypatch)
    checkout = replace(checkout, control=cli.build_control(checkout))
    callback = runner.bind(checkout, FakeLLM([]))
    [(ctx, consumer, queue)] = captured
    assert callable(callback) and queue.ctx is ctx and queue.escalate is consumer
    ticket = await ready(ctx, verification=(("grep", "-q", "ok", "tempo/thing.py"),))
    reviewed = await ctx.git.rev_parse(ctx.repo, ticket.stem)
    queue.offer(ticket, attempt=7)
    assert queue.pending == {ticket.stem: (ticket, 7)}
    [result] = await queue.process()
    assert result.outcome == "ok", result.findings
    artifact = result.artifact
    assert isinstance(artifact, merge.Admission)
    assert artifact.stem == ticket.stem and artifact.reviewed_sha == reviewed
    assert artifact.commit == await ctx.git.rev_parse(ctx.repo, "main")
    assert artifact.produced_at_sha == artifact.commit and artifact.produced_by_spec_version == merge.MERGE_SPEC_VERSION
    events = ctx.driver.journal.read()
    [merged] = [e for e in events if e.type == EventType.STATE_TRANSITION]
    assert merged.ticket == ticket.stem and merged.key is None
    assert merged.body == {"to": "merged", "commit": artifact.commit, "reviewed_sha": reviewed}
    [completion] = [e for e in events if e.type == EventType.EFFECT_COMPLETION]
    assert completion.key == f"merge/{ticket.stem}/7"
    [facts] = [e for e in events if e.type == EventType.SIGNAL]
    assert facts.ticket == ticket.stem and facts.key is None
    assert facts.body == {"kind": CONFLICT_FACTS, "conflicted_paths": [], "resolving_rung": "none",
                          "strategy_paths": [], "integration_red_paths": []}
    assert (ctx.config.state_dir / "spools" / ticket.stem / "7/merge-integration/verify-01.txt").is_file()
    assert not ctx.worktree(ticket.stem).exists()
    assert await ctx.git._run(ctx.repo, "branch", "--list", ticket.stem) == ""
    assert queue.pending == {} and queue.active is None


def sources():
    root = Path(__file__).resolve().parents[1]
    material = {".".join(p.relative_to(root).with_suffix("").parts): p.read_text()
                for p in (root / "tempo").rglob("*.py")}
    material["tempo"] = material.pop("tempo.__init__")
    return material


def without_core_import(material):
    edge = "from tempo.daemon import DaemonCore, daemon_core"
    assert edge in material["tempo.__main__"]
    return {name: "\n".join(line[:len(line) - len(line.lstrip())] + "pass"
            if line.lstrip().startswith("from tempo.daemon import") else line
            for line in source.splitlines()) for name, source in material.items()}


def assert_reachable(material):
    assert {"tempo.daemon", "tempo.scheduler", "tempo.watcher"} <= import_closure(material)


def test_daemon_core_is_reachable_from_cli():
    material = sources()
    assert_reachable(material)
    removed = without_core_import(material)
    with pytest.raises(AssertionError):
        assert_reachable(removed)
    for edge in ("import tempo.daemon", "from tempo import daemon"):
        restored = {**removed, "tempo.status": removed["tempo.status"] + "\n" + edge}
        assert_reachable(restored)


def production_writer(source, script):
    """Reuse the production bind and its queue with the disposable admission repository."""
    checkout = Checkout(source.repo, source.config, source.env, source.exec_, source.git,
                               source.driver.journal, source.fs, source.driver.clock, source.driver.sleep)
    checkout = replace(checkout, control=cli.build_control(checkout))
    writer = runner.bind(checkout, FakeLLM(script))
    return checkout, writer


@pytest.mark.asyncio
async def test_production_composes_rework_without_running_it(tmp_path, monkeypatch):
    rig = CoreRig(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("Rework composition performed work")
    for owner, names in ((daemon, ("rework", "apply_rework")), (Git, ("_call",)),
                         (Journal, ("append",)), (asyncio, ("create_task",)),
                         (rig.fs, ("write", "replace"))):
        for name in names:
            monkeypatch.setattr(owner, name, forbidden)
    before = asyncio.all_tasks()
    writer = runner.bind(replace(rig.checkout, control=rig.core.control), FakeLLM([]))
    assert isinstance(writer, daemon.TicketWriter) and writer.queue.ctx is writer.ctx
    assert writer.ctx.driver.journal is rig.checkout.journal
    assert writer.ctx.driver.effects._journal is rig.checkout.journal
    assert writer.ctx.fs is rig.checkout.fs and writer.ctx.git is rig.checkout.git
    assert writer.ctx.driver.clock is rig.checkout.clock and writer.ctx.driver.sleep is rig.checkout.sleep
    assert before == asyncio.all_tasks() and rig.journal.read() == []
    assert rig.fs.files == {} and rig.exec.calls == []


@pytest.mark.asyncio
async def test_production_composes_one_pause_consumer(tmp_path, monkeypatch):
    from tempo.control import ControlInbox
    from tempo.control import PauseConsumer
    constructed = []
    original = control.control_inbox
    def construct(**kwargs):
        inbox = original(**kwargs)
        constructed.append(inbox)
        return inbox
    def forbidden(*args, **kwargs):
        pytest.fail("control composition read or applied a request or started work")
    monkeypatch.setattr(control, "control_inbox", construct)
    for owner, names in ((ControlInbox, ("consume", "recover")), (Journal, ("append",)),
                         (asyncio, ("create_task",))):
        for name in names:
            monkeypatch.setattr(owner, name, forbidden)
    tasks = asyncio.all_tasks()
    rig = CoreRig(tmp_path)
    consumer = rig.core.control
    assert isinstance(consumer, PauseConsumer)
    assert constructed == [consumer.inbox]
    assert consumer.inbox.journal is rig.checkout.journal
    assert consumer.fs is rig.checkout.fs and consumer.sleep is rig.checkout.sleep
    assert consumer.inbox.journal._clock is rig.checkout.clock
    assert consumer.state_dir == rig.checkout.config.state_dir
    assert consumer.projection.lifecycle_id == consumer.inbox.lifecycle_id
    assert consumer.projection.pause_id is None
    assert_startup_pause_wiring(rig.core)
    assert tasks == asyncio.all_tasks() and rig.journal.read() == []
    assert rig.fs.files == {} and rig.exec.calls == []


@pytest.mark.asyncio
async def test_production_pause_defers_snapshot_and_effects_without_preempting(tmp_path, monkeypatch):
    from tempo.control import ControlRequest, publish_request
    from tempo.seams import LocalFileSystem
    started, finish, held, wake = (asyncio.Event() for _ in range(4))
    snapshots, prepared, completed, effects = [], [], [], []
    original = daemon.snapshot_config
    def capture(config):
        snapshots.append(config)
        return original(config)
    async def prepare(local):
        prepared.append(local)
        async def callback(ticket):
            if ticket.stem == "first":
                started.set()
                await finish.wait()
            effects.append(ticket.stem)
            local.journal.append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket=ticket.stem)
            completed.append(ticket.stem)
            return "merged"
        return callback
    rig = CoreRig(tmp_path, prepare=prepare)
    monkeypatch.setattr(daemon, "snapshot_config", capture)
    async def sleep(_):
        held.set()
        await wake.wait()
        wake.clear()
    rig.core.control.sleep = sleep
    first, second = await rig.add("first"), await rig.add("second")
    active = asyncio.create_task(rig.core.admission.dispatch(first))
    await started.wait()
    owned = rig.core.admission.task
    state, life = rig.core.control.state_dir, rig.core.control.inbox.lifecycle_id
    publish_request(state, ControlRequest("10-pause", life, "pause", None), LocalFileSystem())
    waiting = asyncio.create_task(rig.core.admission.dispatch(second))
    await turn()
    assert not owned.cancelling() and completed == []
    finish.set()
    assert await active == "merged" and not owned.cancelled()
    await held.wait()
    assert completed == effects == ["first"] and len(snapshots) == len(prepared) == 1
    assert_idle(rig.core.admission)
    assert not waiting.done()
    rig.put_config(CONFIG.replace("pins: {review: opus}", "pins: {review: edited-after-release}"))
    publish_request(state, ControlRequest("90-resume", life, "resume", "10-pause"), LocalFileSystem())
    wake.set()
    assert await waiting == "merged"
    assert completed == effects == ["first", "second"] and len(snapshots) == len(prepared) == 2
    assert prepared[-1].config.hosts["claude"].pins["review"] == "edited-after-release"


@pytest.mark.asyncio
@pytest.mark.parametrize("trip", ["red", "tree"])
async def test_composed_admission_holds_accept_identity_bound_resume(admission_context, monkeypatch, trip):
    from tempo.control import CONTROL_DECISION, ControlRequest, publish_request, read_active
    from tempo.lockfile import Lockfile
    from tempo.mergequeue import RED_STREAK, TREE_MISMATCH, TreeMismatch
    from tests.test_mergequeue import host, scripted
    from tests.test_control_cli import invoke
    source = admission_context
    captured = capture_pipeline_queue(monkeypatch)
    checkout, writer = production_writer(source, [])
    ctx, queue, consumer = writer.ctx, writer.queue, checkout.control
    assert queue is captured[0][2] and queue.control is consumer
    held, wake = asyncio.Queue(), asyncio.Queue()
    async def sleep(_):
        await held.put(consumer.projection)
        await wake.get()
    consumer.sleep = sleep
    lock = Lockfile(consumer.state_dir, instance_id="engine", clock=checkout.clock)
    lock.acquire()
    waiter = None
    try:
        consumer.publish()
        assert read_active(consumer.state_dir, Path.read_bytes) == (consumer.projection.lifecycle_id, None)
        if trip == "red":
            ctx.config.review.mechanical = [host("integration")]
            scripted(ctx, monkeypatch, [(1, "", "red")] * 3)
            tickets = [await ready(ctx, stem, changes={f"tempo/{stem}.py": "ok"},
                                   fence=(f"tempo/{stem}.py",)) for stem in ("one", "two", "three")]
            for ticket in tickets:
                queue.offer(ticket, attempt=0)
            assert len(await queue.process()) == 3
        else:
            ticket = await ready(ctx)
            original = ctx.git.rev_parse
            async def mismatch(root, ref):
                return "mismatch" if ref == "main^{tree}" else await original(root, ref)
            monkeypatch.setattr(ctx.git, "rev_parse", mismatch)
            queue.offer(ticket, attempt=0)
            with pytest.raises(TreeMismatch):
                await queue.process()
            monkeypatch.setattr(ctx.git, "rev_parse", original)
        identity, life = queue.hold_id, consumer.projection.lifecycle_id
        assert identity and queue.paused and consumer.admission == identity
        assert consumer.projection.pause_id is None
        assert read_active(consumer.state_dir, Path.read_bytes) == (life, identity)
        signal = ctx.driver.journal.read()[-1]
        assert signal.body["kind"] == (RED_STREAK if trip == "red" else TREE_MISMATCH)
        assert signal.body["hold_id"] == identity
        green = await ready(ctx, "green", changes={"tempo/green.py": "ok"}, fence=("tempo/green.py",))
        queue.offer(green, attempt=0)
        def forbidden(*args, **kwargs):
            pytest.fail("held processing ran admission work")
        with monkeypatch.context() as patch:
            patch.setattr(ctx.git, "rev_parse", forbidden)
            patch.setattr(stages, "gather_evidence", forbidden)
            for _ in range(2):
                assert await queue.process() == []
        publish_request(consumer.state_dir, ControlRequest("01-wrong", life, "resume", "wrong"), consumer.fs)
        await consumer.checkpoint()
        assert queue.paused and queue.pending == {"green": (green, 0)}
        # CLI discovery precedence selects dispatch pause, then exposes admission for another invocation.
        assert await asyncio.to_thread(invoke, ctx.repo, "pause") == 0
        waiter = asyncio.create_task(consumer.checkpoint())
        pause = (await held.get()).pause_id
        assert pause and read_active(consumer.state_dir, Path.read_bytes) == (life, pause)
        assert await asyncio.to_thread(invoke, ctx.repo, "resume") == 0
        wake.put_nowait(None)
        await waiter
        assert consumer.projection.pause_id is None and queue.paused
        assert read_active(consumer.state_dir, Path.read_bytes) == (life, identity)
        assert await queue.process() == []
        assert await asyncio.to_thread(invoke, ctx.repo, "resume") == 0
        await consumer.checkpoint()
        assert queue.paused and identity in consumer.projection.released_hold_ids
        assert read_active(consumer.state_dir, Path.read_bytes) == (life, None)
        decisions = [e for e in ctx.driver.journal.read() if e.body.get("kind") == CONTROL_DECISION]
        assert decisions[0].body["decision"] == "stale"
        assert decisions[-1].body["hold_id"] == identity and decisions[-1].body["decision"] == "accepted"
        ctx.config.review.mechanical = []
        [result] = await queue.process()
        assert result.outcome == "ok" and not queue.paused and queue.hold_id is None
        assert ctx.driver.journal.read().index(decisions[-1]) < next(
            i for i, e in enumerate(ctx.driver.journal.read()) if e.ticket == "green" and
            e.type == EventType.STATE_TRANSITION)
        # Direct identity release can reverse that order without clearing dispatch pause.
        queue._hold("later", {"kind": TREE_MISMATCH, "checked_tree": "checked", "main_tree": "main"})
        later = queue.hold_id
        publish_request(consumer.state_dir, ControlRequest("zz-pause", life, "pause", None), consumer.fs)
        waiter = asyncio.create_task(consumer.checkpoint())
        assert (await held.get()).pause_id == "zz-pause"
        publish_request(consumer.state_dir, ControlRequest("zz-release", life, "resume", later), consumer.fs)
        consumer.inbox.consume()
        await queue.process()
        assert not queue.paused and consumer.projection.pause_id == "zz-pause"
        assert read_active(consumer.state_dir, Path.read_bytes) == (life, "zz-pause")
        # Reconstruction applies accepted records and refreshes the selected discovery without new decisions.
        recovered = control.PauseConsumer(journal=checkout.journal, lifecycle_id=life,
            state_dir=consumer.state_dir, fs=consumer.fs, sleep=sleep,
            files=consumer.inbox.files, read=consumer.inbox.read)
        recovered.hold(later)
        recovered.publish()
        assert read_active(consumer.state_dir, Path.read_bytes) == (life, later)
        before = ctx.driver.journal.read()
        recovered.inbox.recover()
        assert ctx.driver.journal.read() == before
        assert later in recovered.projection.released_hold_ids
        assert read_active(consumer.state_dir, Path.read_bytes) == (life, "zz-pause")
        publish_request(consumer.state_dir, ControlRequest("zzz-pause-release", life, "resume", "zz-pause"), consumer.fs)
        wake.put_nowait(None)
        await waiter
        assert consumer.projection.pause_id is None
        assert read_active(consumer.state_dir, Path.read_bytes) == (life, None)
    finally:
        if waiter is not None and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        consumer.retire()
        lock.release()


@pytest.mark.parametrize("verb", ["run"])
def test_bootstrap_inline_admission_never_holds(admission_context, monkeypatch, verb):
    from tests.test_stages import STEM, TICKET, agent, verdict
    source = admission_context
    source.fs.write(source.repo / f"tickets/{STEM}/ticket.md", TICKET.format(bypass="").encode())
    llm = FakeLLM([agent({"tempo/thing.py": "ok\n"}), verdict()])
    captured = capture_pipeline_queue(monkeypatch)
    def forbidden(*args, **kwargs):
        pytest.fail("bootstrap acquired an admission hold or routed into the queue")
    for name in ("offer", "process", "_hold"):
        monkeypatch.setattr(MergeQueue, name, forbidden)
    inline = runner.merge
    calls = []
    async def merging(*args, **kwargs):
        calls.append(args[1].stem)
        return await inline(*args, **kwargs)
    monkeypatch.setattr(runner, "merge", merging)
    assert cli.main([verb, STEM] if verb == "run" else [verb], cwd=source.repo, env=source.env,
                    clock=source.driver.clock, pipeline=lambda c: runner.bind(c, llm)) == 0
    assert calls == [STEM] and len(captured) == 1
    queue = captured[0][2]
    assert queue.control is not None and queue.control.admission is None
    assert not queue.paused and not queue.pending and queue.hold_id is None


@pytest.mark.asyncio
async def test_production_serve_graph_is_reachable(root, monkeypatch):
    from tempo.serve import Serve
    from tests.test_serve import graph, until, finish
    rig = await graph(root, monkeypatch)
    before = asyncio.all_tasks()
    owner = Serve(rig.checkout, read=rig.owner.read, stems=rig.owner.stems, prepare=rig.prepare, llm=rig.llm)
    assert asyncio.all_tasks() == before and owner.tasks.tasks == ()
    assert rig.journal.read() == [] and owner.core.restart.timers.journal is rig.journal
    rig.owner = owner
    run = asyncio.create_task(owner.run())
    try:
        await until(rig, owner.ready.is_set)
        assert owner.stopper.workers == owner.observer.workers == owner.workers
        assert len(owner.tasks.tasks) == 3 and len(owner.workers) == 4
        assert owner.checkpoint.journal is owner.checkpoint.effects._journal is rig.journal
        assert owner.checkpoint.timers is owner.core.restart.timers
        assert "tempo.serve" in import_closure(sources())
    finally:
        await finish(rig, run)


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["implement", "fallback-raises", "fallback-absent", "dependency", "base",
    "flake", "operator", "core", "serve", "driver", "retro"])
async def test_production_boxes_receive_rereport_callback(admission_context, tmp_path, monkeypatch, site):
    import inspect
    from tempo.box import Box, RereportJournal
    from tempo.triage import triage_pass
    from tempo.retro import Retro
    from tempo.providers import ProviderCallError
    from tests.test_storm_notification_activation import SITES, exercise_site
    from tests.test_serve import graph
    from tests.test_retro_box import PROPOSAL

    source = admission_context
    checkout, writer = production_writer(source, [])
    captured = []
    construct = Box.__init__
    def bound(self, *args, **kwargs):
        caller = inspect.currentframe().f_back.f_globals["__name__"]
        if caller.startswith("tempo."):
            callback = kwargs.get("rereport")
            assert isinstance(callback, RereportJournal), f"{caller} omitted journal custody"
            captured.append((caller, callback.journal))
        construct(self, *args, **kwargs)
    monkeypatch.setattr(Box, "__init__", bound)
    if site in SITES:
        await exercise_site(source, monkeypatch, site)
    elif site == "operator":
        await triage_pass(checkout, FakeLLM([]))
    elif site == "core":
        (tmp_path / "core").mkdir()
        rig = CoreRig(tmp_path / "core")
        assert rig.core.control._recover.__self__._rereport.journal is rig.journal
    elif site == "serve":
        rig = await graph(checkout.repo, monkeypatch)
        assert rig.owner.box._rereport.journal is rig.journal
    elif site == "driver":
        error = ProviderCallError("fake", "quota", rc=1, stderr_tail="quota", failure_class="quota_exhausted",
                                  paved_road="wait for quota")
        await writer.stages.driver.provider_failure(error, owner="source", ticket=None, sequence=1,
                                                   surface="implement", workspace=checkout.repo)
    else:
        hook = Retro(checkout, writer)
        hook.failure_report("retro-failed/initial/forced")
        writer.stages.driver.llm = FakeLLM([json.dumps({"proposals": [PROPOSAL]})])
        checkout.journal.append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket="history")
        assert (await hook(forced=True))["commit"]
    assert captured, f"{site} did not exercise a production constructor"
    assert all(journal is not None for _, journal in captured)
