"""Drive the production CLI core through injected delivery, process, filesystem and time seams."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import BaseModel

from tempo import __main__ as cli, caps, daemon, driver, merge, providers, runner, stages
from tempo.config import ConfigError, ConfigSnapshot, load_config
from tempo.daemon import DaemonAdmission
from tempo.driver import Driver, LlmStage
from tempo.git import Git
from tempo.journal import EventType, Journal
from tempo.llm import FakeLLM
from tempo.mergequeue import CONFLICT_FACTS, MergeQueue
from tempo.providers import ProviderLLM, child_env
from tempo.redact import Redactor
from tempo.scheduler import Scheduler
from tempo.stages import StageContext
from tempo.tickets import INTAKE_SIGNAL, validate_ticket
from tempo.watcher import WATCHER_PARSE_FAILURE, Watcher
from tests.test_cli import root, write
from tests.test_daemon_admission import assert_idle
from tests.test_daemon_config import assert_detached, python_values
from tests.test_providers import CONFIG, ENV, claude_ok, codex_ok, jsonl
from tests.test_scheduler import Time, import_closure, text, turn
from tests.test_mergequeue import ctx as admission_context, ready
from tempo import control
from tempo.checkout import Checkout, Refusal


class ScriptFS:
    def __init__(self):
        self.files = {}

    def write(self, path, data):
        self.files[path] = data

    def replace(self, src, dst):
        self.files[dst] = self.files.pop(src)


class ScriptExec:
    def __init__(self, fs):
        self.fs = fs
        self.calls = []
        self.hook = None
        self.latest = "1.2.3"
        self.reply = "ok"
        self.refuse_probe = False

    async def run(self, argv, *, cwd, env, timeout, stdin_path=None, on_spawn=None, on_stdout_line=None):
        prompt = self.fs.files[stdin_path].decode() if stdin_path else None
        self.calls.append((list(argv), cwd, dict(env), prompt, timeout))
        if self.hook is not None:
            await self.hook(argv)
        if argv[1:] == ["--version"]:
            return 0, "1.2.3", ""
        if argv[0] == "pnpm":
            return 0, self.latest, ""
        assert argv[0] in {"claude", "codex"} and prompt is not None
        if on_spawn is not None:
            on_spawn(123)
        if self.refuse_probe and prompt == providers.PROBE_PROMPT:
            return 1, jsonl({"type": "turn.failed", "error": {"message": "model not supported"}}), ""
        reply = "ok" if prompt == providers.PROBE_PROMPT else self.reply
        out = claude_ok(reply) if argv[0] == "claude" else codex_ok(reply)
        if on_stdout_line is not None:
            for line in out.splitlines(keepends=True):
                on_stdout_line(line)
        return 0, out, ""

    def kill_group(self, pgid):
        raise AssertionError("no scripted child survives its invocation")


@pytest.mark.asyncio
async def test_production_watchdog_shares_active_executor(root, monkeypatch):
    from tempo.drain import drain
    from tests.test_restart_timers import repository
    from tests.test_serve import until, finish, terminals

    contexts = []
    async def drive(ctx, ticket, **kwargs):
        assert ctx.driver.llm._exec is ctx.exec_
        ctx.exec_.reply = '{"text":"done"}'
        for surface, identity in (("implement", ticket.stem), ("triage", None)):
            result = await ctx.driver.run(
                LlmStage(surface=surface, emits=Reply, gates=[], render=lambda *_: "probe"), None,
                ticket=identity, attempt=0, workspace=ctx.repo, tier="medium", effort="low",
                stuck_budget=1200,
                run_seq=5 + len(contexts) if identity is None else None)
            assert result.outcome == 'ok'
            d = ctx.driver.detector
            assert d.call.detector is d
            assert d.notify.notifications.exec_ is not ctx.exec_
            assert ctx.driver.llm.abort_current.__self__._exec is ctx.exec_
            assert ctx.driver.llm._adapters[d.call.identity[0]]._exec is ctx.exec_
        contexts.append(ctx)
        ctx.driver.journal.append(EventType.STATE_TRANSITION, {'to': 'already_satisfied'}, ticket=ticket.stem)
        return 'already_satisfied'

    class Reply(BaseModel):
        text: str

    monkeypatch.setattr(runner, 'drive', drive)
    rig = await repository(root, monkeypatch)
    await rig.add('bootstrap')
    checkout = replace(rig.checkout, control=cli.build_control(rig.checkout))
    dispatch = await runner.prepare_pipeline(checkout)
    report = await drain(checkout, dispatch)
    assert report.exit_code == 0 and contexts[0].exec_ is checkout.exec_
    assert contexts[0].driver.detector.notify.notifications.exec_ is not contexts[0].exec_
    from tempo.seams import LocalFileSystem
    serving = rig
    rig.fs.publish = LocalFileSystem().publish
    rig.owner = cli.build_serve(rig.checkout)
    write(root, 'continuous', text())
    run = asyncio.create_task(serving.owner.run())
    try:
        await until(serving, lambda: bool(terminals(serving, 'continuous')))
        assert contexts[-1].exec_ is serving.checkout.exec_
        assert len(serving.owner.workers) == 4 and len(serving.owner.tasks.tasks) == 3
        assert serving.owner.notifications.compose(serving.checkout.config).exec_ is not contexts[-1].exec_
    finally:
        await finish(serving, run)


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
            Git(self.exec, env=child_env(ENV, initial), timeout=30),
            self.journal, self.fs, self.time, self.time.sleep,
        )
        self.files = {}
        self.quarantine, self.drought = set(), set()
        self.unmerged = 0
        self.calls, self.locals = [], []
        kwargs = {} if prepare is None else {"prepare": prepare}
        self.core = cli.build_daemon_core(
            self.checkout, config_path=config_path, read=self.files.get, debounce=5,
            quarantined=lambda: self.quarantine, drought_parked=lambda: self.drought,
            completed_unmerged=lambda: self.unmerged, **kwargs,
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
    rig.put_config(CONFIG.replace("x-med", "x-edited") + "scheduler: {max_unmerged: 5}\n")
    release.set()
    await cleaning.wait()
    assert len(locals) == 1 and not waiting.done()
    finish.set()
    assert await asyncio.gather(first, waiting) == ["merged", "merged"]
    before, after = [local.config for local in locals]
    assert before is not after
    assert (before.scheduler.max_unmerged, after.scheduler.max_unmerged) == (2, 5)
    assert (before.providers[1].models_by_tier.medium, after.providers[1].models_by_tier.medium) == ("x-med", "x-edited")
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


def observe_preparation(monkeypatch):
    trace, snapshots, contexts = [], [], []
    capture = daemon.snapshot_config
    construct = ProviderLLM.__init__
    preflight = ProviderLLM.preflight
    bind = runner.bind
    from_config = Driver.from_config
    redact = Redactor.from_config
    severity = driver.merge_severity

    def snapshot(config):
        value = capture(config)
        snapshots.append(value)
        trace.append("capture")
        return value

    def provider(self, config, **kwargs):
        assert config is snapshots[-1]
        trace.append("provider")
        construct(self, config, **kwargs)

    async def probe(self):
        assert self._config is snapshots[-1]
        trace.append("preflight-start")
        result = await preflight(self)
        trace.append("preflight-end")
        return result

    def binding(checkout, llm):
        assert trace[-1] == "preflight-end"
        assert checkout.config is llm._config is snapshots[-1]
        trace.append("bind")
        return bind(checkout, llm)

    def driving(cls, config, **kwargs):
        assert config is snapshots[-1]
        trace.append("driver")
        return from_config(config, **kwargs)

    def redacting(cls, config, env):
        assert config is snapshots[-1]
        trace.append("redactor")
        return redact(config, env)

    def merging(config):
        assert config is snapshots[-1]
        trace.append("severity")
        return severity(config)

    original_context = runner.StageContext

    def context(**kwargs):
        assert kwargs["config"] is snapshots[-1]
        ctx = original_context(**kwargs)
        contexts.append(ctx)
        trace.append("context")
        return ctx

    compose = runner.compose_pipeline

    def queue(ctx, *, escalate, control):
        assert trace[-1] == "context" and ctx is contexts[-1]
        value = compose(ctx, escalate=escalate, control=control)
        assert value.ctx is ctx and value.escalate is escalate
        trace.append("queue")
        return value

    monkeypatch.setattr(daemon, "snapshot_config", snapshot)
    monkeypatch.setattr(ProviderLLM, "__init__", provider)
    monkeypatch.setattr(ProviderLLM, "preflight", probe)
    monkeypatch.setattr(runner, "bind", binding)
    monkeypatch.setattr(Driver, "from_config", classmethod(driving))
    monkeypatch.setattr(Redactor, "from_config", classmethod(redacting))
    monkeypatch.setattr(driver, "merge_severity", merging)
    monkeypatch.setattr(runner, "StageContext", context)
    monkeypatch.setattr(runner, "compose_pipeline", queue)
    return trace, snapshots, contexts


@pytest.mark.asyncio
async def test_production_core_uses_real_snapshot_pipeline(tmp_path, monkeypatch):
    trace, snapshots, contexts = observe_preparation(monkeypatch)
    rig = CoreRig(tmp_path, config=CONFIG.replace("review: {}", "review: {gate_severity: {scope_fence: soft}}")
                  + "caps: {retry: 2}\n")
    ticket = await rig.add("work")
    assert trace == []
    reads = []
    original_env, original_resolve = providers.child_env, providers.resolve

    def filtered(env, config, serving=None):
        assert config is snapshots[-1]
        if serving is not None:
            assert serving is next(p for p in config.providers if p.name == serving.name)
        reads.append("env")
        return original_env(env, config, serving)

    def routed(config, tier, surface):
        assert config is snapshots[-1]
        reads.append("route")
        return original_resolve(config, tier, surface)

    monkeypatch.setattr(providers, "child_env", filtered)
    monkeypatch.setattr(providers, "resolve", routed)

    class Reply(BaseModel):
        text: str

    called = []

    async def before_host_work(ctx, original):
        called.append(original)
        assert original is ticket and ctx is contexts[0] and isinstance(ctx, StageContext)
        assert rig.core.admission.active is original and rig.core.admission.task is asyncio.current_task()
        snapshot = snapshots[0]
        assert ctx.config is snapshot and isinstance(ctx.driver, Driver)
        llm = ctx.driver.llm
        assert isinstance(llm, ProviderLLM) and llm._config is snapshot
        for p in snapshot.providers:
            adapter = llm._adapters[p.name]
            assert type(adapter) is providers.ADAPTERS[p.name]
            assert adapter.provider is p and adapter._config is snapshot
            assert adapter._exec is ctx.exec_ is rig.exec and adapter._fs is ctx.fs is rig.fs
        assert providers.resolve(snapshot, "medium", "implement").model == "x-med"
        served = providers.resolve(snapshot, "medium", "review")
        assert served.provider is snapshot.providers[0] and served.model == "c-max"
        assert providers.child_env(ENV, snapshot) == {k: v for k, v in ENV.items() if not k.endswith("_KEY")}
        assert ctx.driver.severity["scope_fence"] == "soft" and ctx.driver.retry_cap == 2
        assert caps.remaining(snapshot.caps, (), original.stem, "retry") == 2
        assert caps.spent(snapshot.caps, (), original.stem) is None
        assert caps.next_rung(snapshot, "medium", "low") == {"tier": "high", "effort": "low"}
        rig.exec.reply = json.dumps({"text": ENV["CLAUDE_KEY"] + " " + ENV["CODEX_KEY"]})
        result = await ctx.driver.run(
            LlmStage(surface="review", emits=Reply, gates=[], render=lambda *_: rig.exec.reply),
            None, ticket=original.stem, attempt=0, workspace=tmp_path,
            tier="medium", effort="low", stuck_budget=60)
        assert result.outcome == "ok" and result.provider == "claude" and result.model == "c-max"
        assert result.artifact.text == "[REDACTED:CLAUDE_KEY] [REDACTED:CODEX_KEY]"
        return "original-terminal"

    monkeypatch.setattr(runner, "drive", before_host_work)
    assert await rig.core.scheduler.dispatch(ticket) == "original-terminal"
    assert called == [ticket] and len(snapshots) == len(contexts) == 1
    assert trace[:4] == ["capture", "provider", "redactor", "preflight-start"]
    assert trace[4:] == ["preflight-end", "bind", "driver", "redactor", "severity", "context", "queue"]
    assert set(reads) == {"env", "route"}
    model_calls = [call for call in rig.exec.calls if call[3] is not None]
    assert len(model_calls) == 6  # Five distinct probes, then one read-only driver call.
    for argv, cwd, env, prompt, _ in model_calls:
        serving, other = ("CLAUDE_KEY", "CODEX_KEY") if argv[0] == "claude" else ("CODEX_KEY", "CLAUDE_KEY")
        assert env[serving] == ENV[serving] and other not in env and cwd == tmp_path
        assert "bypassPermissions" not in argv and "--dangerously-bypass-approvals-and-sandbox" not in argv
        assert all(ENV[name] not in prompt for name in (serving, other))
    for data in rig.fs.files.values():
        assert all(ENV[name].encode() not in data for name in ("CLAUDE_KEY", "CODEX_KEY"))
    assert all(ENV[name] not in str(rig.journal.read()) for name in ("CLAUDE_KEY", "CODEX_KEY"))
    assert_idle(rig.core.admission)


@pytest.mark.asyncio
async def test_production_core_awaits_preflight_inside_admission(tmp_path, monkeypatch):
    trace, snapshots, contexts = observe_preparation(monkeypatch)
    rig = CoreRig(tmp_path)
    first_ticket, second_ticket = await rig.add("first"), await rig.add("second")
    started, release = asyncio.Event(), asyncio.Event()
    called = []

    async def hook(argv):
        assert asyncio.current_task() is rig.core.admission.task
        if not started.is_set():
            assert trace == ["capture", "provider", "redactor", "preflight-start"]
            started.set()
            await release.wait()

    async def stop(ctx, ticket):
        assert ctx.config is snapshots[-1] and trace[-1] == "queue"
        called.append(ticket)
        return "merged"

    rig.exec.hook = hook
    monkeypatch.setattr(runner, "drive", stop)
    assert trace == [] and rig.exec.calls == [] and not contexts
    first = asyncio.create_task(rig.core.admission.dispatch(first_ticket))
    await started.wait()
    owned = rig.core.admission.task
    waiter = asyncio.create_task(rig.core.admission.dispatch(second_ticket))
    cancelled = asyncio.create_task(rig.core.admission.dispatch(second_ticket))
    await turn()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert len(snapshots) == 1 and not contexts and not called and not waiter.done()
    assert rig.core.admission.task is owned and not owned.cancelling()
    rig.put_config(CONFIG.replace("x-med", "x-edited"))
    release.set()
    assert await asyncio.gather(first, waiter) == ["merged", "merged"]
    assert called == [first_ticket, second_ticket]
    assert len(snapshots) == len(contexts) == 2 and snapshots[0] is not snapshots[1]
    assert [s.providers[1].models_by_tier.medium for s in snapshots] == ["x-med", "x-edited"]
    assert [c.driver.llm._config for c in contexts] == snapshots
    assert contexts[0].driver.llm is not contexts[1].driver.llm
    for step in ("capture", "provider", "preflight-start", "preflight-end", "bind", "driver", "context", "queue"):
        assert trace.count(step) == 2
    probed = [argv[argv.index("-m") + 1] for argv, _, _, prompt, _ in rig.exec.calls
              if argv[0] == "codex" and prompt is not None]
    assert probed == ["x-med", "x-low", "x-edited", "x-low"]
    assert_idle(rig.core.admission)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["refusal", "error", "cancel"])
async def test_production_core_unwinds_preflight_refusal_and_cancellation(tmp_path, monkeypatch, failure):
    trace, snapshots, contexts = observe_preparation(monkeypatch)
    rig = CoreRig(tmp_path)
    first_ticket, second_ticket = await rig.add("first"), await rig.add("second")
    assert trace == []
    started, cleaning, finish = (asyncio.Event() for _ in range(3))
    error = ValueError("version probe failed")
    called = []

    async def hook(argv):
        if failure == "error":
            raise error
        if failure == "cancel":
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await finish.wait()

    async def stop(ctx, ticket):
        called.append(ticket)
        return "merged"

    rig.exec.hook = hook
    rig.exec.refuse_probe = failure == "refusal"
    monkeypatch.setattr(runner, "drive", stop)
    if failure == "cancel":
        first = asyncio.create_task(rig.core.admission.dispatch(first_ticket))
        await started.wait()
        owned = rig.core.admission.task
        first.cancel()
        await cleaning.wait()
        second = asyncio.create_task(rig.core.admission.dispatch(second_ticket))
        await turn()
        assert len(snapshots) == 1 and not contexts and not called and not second.done()
        assert rig.core.admission.task is owned and rig.core.admission.active is first_ticket
        rig.exec.hook = None
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert owned.done() and owned.cancelled() and owned not in asyncio.all_tasks()
        assert await second == "merged"
    else:
        with pytest.raises(Refusal if failure == "refusal" else ValueError) as caught:
            await rig.core.admission.dispatch(first_ticket)
        if failure == "refusal":
            message = str(caught.value)
            assert "provider preflight failed" in message and "failed its probe" in message
            assert "pnpm add -g test-cli@latest" in message
            assert "fix each named provider, then run the same command again" in message
        else:
            assert caught.value is error
        assert not contexts and not called and "bind" not in trace
        assert_idle(rig.core.admission)
        rig.exec.hook, rig.exec.refuse_probe = None, False
        assert await rig.core.admission.dispatch(second_ticket) == "merged"
    assert called == [second_ticket] and len(contexts) == 1 and len(snapshots) == 2
    assert trace.count("bind") == trace.count("context") == 1
    assert_idle(rig.core.admission)


@pytest.mark.parametrize("refuse", [False, True])
def test_bootstrap_pipeline_prepares_before_dispatch(tmp_path, monkeypatch, refuse):
    rig = CoreRig(tmp_path)
    rig.checkout = replace(rig.checkout, control=rig.core.control)
    ticket = asyncio.run(rig.add("work"))
    bound, called = [], []
    original_bind = runner.bind

    def bind(checkout, llm):
        assert checkout is not rig.checkout and llm._config is checkout.config
        assert isinstance(checkout.config, ConfigSnapshot)
        assert checkout.control.providers is rig.core.control.providers
        assert len([call for call in rig.exec.calls if call[3] is not None]) == 5
        bound.append(checkout)
        return original_bind(checkout, llm)

    async def stop(ctx, original):
        assert original is ticket and ctx.config is bound[0].config
        called.append(original)
        return "merged"

    monkeypatch.setattr(runner, "bind", bind)
    monkeypatch.setattr(runner, "drive", stop)
    rig.exec.refuse_probe = refuse
    callback = runner.pipeline(rig.checkout)
    assert bound == called == [] and rig.exec.calls == []
    if refuse:
        with pytest.raises(Refusal, match="provider preflight failed"):
            asyncio.run(callback.prepare())
        assert bound == called == []
    else:
        asyncio.run(callback.prepare())
        assert len(bound) == 1 and called == []
        assert asyncio.run(callback(ticket)) == "merged" and called == [ticket]
    assert_idle(rig.core.admission)


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
async def test_production_consumes_conflict_handoff_after_unwind(admission_context, monkeypatch):
    from tempo.mergequeue import ConflictHandoff
    from tests.test_mergequeue import conflict
    from tests.test_stages import agent, verdict
    from tests.test_rework import order, requisition
    source = admission_context
    _, writer = production_writer(source, [])
    ctx, queue = writer.ctx, writer.queue
    ticket, path = await conflict(ctx)
    ctx.config.merge.strategies = []
    text = (ctx.repo / f"tickets/{ticket.stem}/ticket.md").read_text()
    chronology = []
    call = ctx.git._call
    async def observe(root, *args, **kwargs):
        value = await call(root, *args, **kwargs)
        if args == ("rebase", "--abort"):
            chronology.append("abort")
        return value
    monkeypatch.setattr(ctx.git, "_call", observe)
    def forbid(*args, **kwargs):
        pytest.fail("handoff reused code approval or called diagnosis")
    monkeypatch.setattr(runner, "diagnose", forbid)
    monkeypatch.setattr(ctx.git, "merge_squash", forbid)
    queue.offer(ticket, attempt=0)
    [handoff] = await queue.process()
    assert isinstance(handoff, ConflictHandoff) and handoff.approval_invalidated
    assert chronology == ["abort"] and not ctx.driver.llm.requests
    assert queue.active is None and not queue._slot.locked()
    def reply(req):
        assert queue.active is None and not queue._slot.locked()
        chronology.append("rework")
        assert handoff.model_dump_json() in req.rendered
        assert path in req.rendered and all(f.message in req.rendered for f in handoff.findings)
        assert text in req.rendered
        return order("update", [(ticket.stem, text.replace("holds the word ok", "holds the word ok after fresh review"))])
    ctx.driver.llm = FakeLLM([reply, requisition()])
    from tempo.drain import _Drain
    from tempo.lockfile import Lockfile, LockHeld
    checkout = Checkout(ctx.repo, ctx.config, ctx.env, ctx.exec_, ctx.git,
                               ctx.driver.journal, ctx.fs, ctx.driver.clock, ctx.driver.sleep)
    lock = Lockfile(ctx.config.state_dir, instance_id="production", clock=ctx.driver.clock)
    lock.acquire()
    try:
        async def consume(original):
            contender = Lockfile(ctx.config.state_dir, instance_id="contender", clock=ctx.driver.clock)
            with pytest.raises(LockHeld):
                contender.acquire()
            return await writer.consume_handoff(original, handoff, attempt=0)
        await _Drain(checkout, consume)._run_one(ticket, False, "blob")
    finally:
        lock.release()
    assert chronology == ["abort", "rework"]
    events = ctx.driver.journal.read()
    [terminal] = [e.body for e in events if e.type == EventType.STATE_TRANSITION and e.body.get("to") != "running"]
    assert terminal == {"to": "gate_failed", "stage": "merge",
                        "reason": ",".join(sorted({f.code for f in handoff.findings})), "dispatch": "retry"}
    assert not any(e.type == EventType.CAP_CONSUMED for e in events)
    assert [r.surface for r in ctx.driver.llm.requests] == ["rework", "requisition_review"]
    assert not ctx.worktree(ticket.stem).exists()
    # Re-offer uses the ordinary Implement/Check/Review path, never the invalidated queue approval.
    monkeypatch.undo()
    ctx.driver.llm = FakeLLM([agent({"tempo/thing.py": "ok fresh\n"}),
                             verdict()])
    fresh = validate_ticket(ticket.stem, (ctx.repo / f"tickets/{ticket.stem}/ticket.md").read_text(), ctx.repo)
    await writer(fresh)
    assert [r.surface for r in ctx.driver.llm.requests] == ["implement", "review"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["update", "split", "escalate", "exhausted"])
async def test_production_applies_reviewed_rework_orders(admission_context, action):
    from tempo.mergequeue import ConflictHandoff
    from tempo.backlog import SUPERSEDES
    from tempo.rework import record_supersedes
    from tests.test_rework import order, requisition, successors
    checkout, writer = production_writer(admission_context, [])
    ctx = writer.ctx
    ticket = await ready(ctx)
    text = (ctx.repo / f"tickets/{ticket.stem}/ticket.md").read_text()
    if action == "exhausted":
        from tests.test_mergequeue import commit
        text = text.replace("kind: feature", "kind: feature\nagent_tier: max\nagent_effort: max")
        ctx.fs.write(ctx.repo / f"tickets/{ticket.stem}/ticket.md", text.encode())
        await commit(ctx, ctx.repo, [f"tickets/{ticket.stem}/ticket.md"])
        ticket = validate_ticket(ticket.stem, text, ctx.repo)
    proposals = successors(text.strip()) if action == "split" else [(ticket.stem, text.strip().replace("holds the word ok", "holds ok after narrowing"))] if action == "update" else []
    ctx.driver.llm = FakeLLM([order("escalate" if action == "exhausted" else action, proposals)]
                            + [requisition() for _ in proposals])
    finding = stages.Finding(code="post_rebase_regate", message="conflict", paved_road="fresh review")
    handoff = ConflictHandoff(stem=ticket.stem, reviewed_sha=await ctx.git.rev_parse(ctx.repo, ticket.stem),
                              conflicted_paths=["tempo/thing.py"], findings=[finding, finding])
    from tempo.drain import _Drain
    from tempo.lockfile import Lockfile
    returned = []
    async def consume(original):
        result = await writer.consume_handoff(original, handoff, attempt=0)
        returned.append(result)
        return result
    lock = Lockfile(ctx.config.state_dir, instance_id="production", clock=ctx.driver.clock)
    lock.acquire()
    try:
        await _Drain(checkout, consume)._run_one(
            ticket, False, await ctx.git.rev_parse(ctx.repo, f"HEAD:tickets/{ticket.stem}/ticket.md"))
    finally:
        lock.release()
    [result] = returned
    events = ctx.driver.journal.read()
    transitions = [e for e in events if e.type == EventType.STATE_TRANSITION and e.body.get("to") != "running"]
    terminal = transitions[0].body
    assert {k: terminal[k] for k in ("to", "stage", "reason")} == {
        "to": "gate_failed", "stage": "merge", "reason": "post_rebase_regate"}
    maps = [e for e in events if e.body.get("signal") == SUPERSEDES]
    if action == "split":
        assert result == "rejected" and transitions[-1].body == {"to": "rejected"}
        assert terminal == {"to": "gate_failed", "stage": "merge", "reason": "post_rebase_regate"}
        [mapping] = maps
        assert events.index(mapping) < events.index(transitions[0]) < events.index(transitions[-1])
        for stem, proposal in proposals:
            assert await ctx.git._run(ctx.repo, "show", f"HEAD:tickets/{stem}/ticket.md") == proposal
        assert "state: rejected" in (ctx.repo / f"tickets/{ticket.stem}/ticket.md").read_text()
        from tempo.rework import ReworkOrder
        replay = ReworkOrder(action="split", tickets=[{"stem": s, "ticket": t} for s, t in proposals],
                             stem=ticket.stem, attempt=0, produced_by_spec_version=1, produced_at_sha="reviewed")
        count = len(events)
        assert await record_supersedes(ctx, replay) == [] and len(ctx.driver.journal.read()) == count
    else:
        assert result == "gate_failed" and maps == [] and len(transitions) == 1
        if action == "update":
            assert terminal["dispatch"] == "retry" and "routed" not in terminal and "rung" not in terminal
            assert await ctx.git._run(ctx.repo, "show", f"HEAD:tickets/{ticket.stem}/ticket.md") == proposals[0][1]
        elif action == "escalate":
            assert terminal["dispatch"] == "escalate" and "rung" in terminal and "routed" not in terminal
        else:
            assert terminal["dispatch"] == terminal["routed"] == "reject_queue" and "rung" not in terminal
        if action != "update":
            assert (ctx.repo / f"tickets/{ticket.stem}/ticket.md").read_text() == text
    assert not any(e.type == EventType.CAP_CONSUMED for e in events)
    assert not ctx.worktree(ticket.stem).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["split", "update"])
@pytest.mark.parametrize("failure", ["validation", "review", "write", "add", "commit", "partial", "incomplete", "all-commits", "spent"])
async def test_production_refuses_failed_rework_publication(admission_context, monkeypatch, failure, action):
    from tempo.mergequeue import ConflictHandoff
    from tests.test_rework import order, requisition, successors
    checkout, writer = production_writer(admission_context, [])
    ctx = writer.ctx
    ticket = await ready(ctx)
    text = (ctx.repo / f"tickets/{ticket.stem}/ticket.md").read_text()
    proposals = (successors(text) if action == "split" else
                 [(ticket.stem, text.replace("holds the word ok", "holds ok after narrowing"))])
    ctx.driver.retry_cap = 0
    script = [order(action, proposals), *[requisition() for _ in proposals]]
    if failure == "validation":
        script = [order("update", [(ticket.stem, "invalid ticket")])]
    if failure == "review":
        script = [order(action, proposals), requisition("rma")]
    if failure == "write":
        write = ctx.fs.write
        def failed_write(path, data):
            if path == ctx.repo / f"tickets/{proposals[-1][0]}/ticket.md":
                raise RuntimeError("proposal write failed")
            write(path, data)
        monkeypatch.setattr(ctx.fs, "write", failed_write)
    if failure == "add":
        add = ctx.git.add
        failed_once = False
        async def failed_add(root, paths):
            nonlocal failed_once
            if root == ctx.repo and paths == [f"tickets/{s}/ticket.md" for s, _ in proposals] and not failed_once:
                failed_once = True
                raise RuntimeError("proposal add failed")
            await add(root, paths)
        monkeypatch.setattr(ctx.git, "add", failed_add)
    if failure in {"commit", "partial", "incomplete", "all-commits"}:
        commit = ctx.git.commit
        async def failed(root, message, **kwargs):
            if message.endswith(": rework") or failure == "all-commits":
                if failure in {"partial", "incomplete"}:
                    await commit(root, message, only=[f"tickets/{proposals[0][0]}/ticket.md"])
                    if failure == "incomplete" and action == "split":
                        return
                raise RuntimeError("publication failed")
            await commit(root, message, **kwargs)
        monkeypatch.setattr(ctx.git, "commit", failed)
    if failure == "spent":
        for _ in range(ctx.config.caps.retry):
            caps.consume(ctx.driver.journal, ticket.stem, "retry", "old")
        script = []
    ctx.driver.llm = FakeLLM(script)
    handoff = ConflictHandoff(stem=ticket.stem, reviewed_sha="reviewed", conflicted_paths=[],
                              findings=[stages.Finding(code="post_rebase_regate", message="conflict", paved_road="fresh review")])
    assert await writer.consume_handoff(ticket, handoff, attempt=0) == "gate_failed"
    events = ctx.driver.journal.read()
    [terminal] = [e.body for e in events if e.type == EventType.STATE_TRANSITION]
    assert terminal == {"to": "gate_failed", "stage": "merge", "reason": "post_rebase_regate",
                        "dispatch": "reject_queue", "routed": "reject_queue"}
    assert not any(e.body.get("signal") == "supersedes" for e in events)
    assert (ctx.repo / f"tickets/{ticket.stem}/ticket.md").read_text() == text
    assert await ctx.git._run(ctx.repo, "show", f"HEAD:tickets/{ticket.stem}/ticket.md") == text
    assert not (ctx.repo / "tickets/piece-one/ticket.md").exists()
    assert not (ctx.repo / "tickets/piece-two/ticket.md").exists()
    assert caps.draws(events, ticket.stem, "diagnosis") == 0
    assert caps.draws(events, ticket.stem, "retry") == (ctx.config.caps.retry if failure == "spent" else 0)
    if failure == "spent":
        assert ctx.driver.llm.requests == []
    else:
        artifact = json.loads((ctx.repo / f"tickets/{ticket.stem}/attempts/0/harvest.json").read_text())
        assert len(artifact["findings"]) >= 2 and all(f["paved_road"] for f in artifact["findings"])
    if failure != "all-commits":
        assert await ctx.git.status_porcelain(ctx.repo) == ""
        # A later ordinary drain must not intake a leaked machine proposal as human work.
        from tempo.drain import drain
        intake_before = [e for e in events if e.body.get("signal") == INTAKE_SIGNAL]
        for _ in range(ctx.config.caps.retry - caps.draws(events, ticket.stem, "retry")):
            caps.consume(ctx.driver.journal, ticket.stem, "retry", "old")
        async def forbidden(ticket):
            pytest.fail("following drain dispatched refused Rework output")
        await drain(checkout, forbidden)
        assert [e for e in ctx.driver.journal.read() if e.body.get("signal") == INTAKE_SIGNAL] == intake_before
        assert await ctx.git.status_porcelain(ctx.repo) == ""


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


class LiveDrain:
    """The real drain and CLI control factory, with filesystem traces and barrier wakeups."""

    def __init__(self, root, monkeypatch, *, pause=True):
        from tempo.control import ControlRequest, publish_request
        from tempo.lockfile import Lockfile, LockHeld
        from tempo.seams import LocalFileSystem
        from tests.test_drain import Clock, journal
        from tests.test_cli import ENV as checkout_env
        from tempo.seams import SubprocessExec
        process = SubprocessExec()
        self.checkout = Checkout(root, load_config(None, cwd=root), checkout_env, process,
                                        Git(process, env=checkout_env, timeout=30), journal(root),
                                        LocalFileSystem(), Clock())
        self.trace, self.consumers, self.calls = [], [], []
        self.pause = pause
        self.held = asyncio.Queue()
        self.wake = asyncio.Queue()
        original_acquire, original_release = Lockfile.acquire, Lockfile.release
        def acquire(lock):
            original_acquire(lock)
            self.trace.append("lock")
        def release(lock):
            self.trace.append("unlock")
            original_release(lock)
        monkeypatch.setattr(Lockfile, "acquire", acquire)
        monkeypatch.setattr(Lockfile, "release", release)
        original = cli.build_control
        def build(checkout):
            assert checkout is self.checkout
            consumer = original(checkout)
            self.consumers.append(consumer)
            consume = consumer.inbox.consume
            def consumed():
                self.trace.append("consume")
                consume()
            consumer.inbox.consume = consumed
            return consumer
        monkeypatch.setattr(cli, "build_control", build)
        append = self.checkout.journal.append
        def appended(*args, **kwargs):
            event = append(*args, **kwargs)
            if event.body.get("kind") == "control_decision":
                self.trace.append(("decision", event.body.copy()))
            return event
        monkeypatch.setattr(self.checkout.journal, "append", appended)
        rig = self
        class Files(LocalFileSystem):
            def write(self, path, data):
                if path == rig.checkout.config.state_dir / "control/active.json":
                    contender = Lockfile(rig.checkout.config.state_dir, instance_id="trace", clock=rig.checkout.clock)
                    with pytest.raises(LockHeld):
                        contender.acquire()
                    assert rig.trace[0] == "lock" and "unlock" not in rig.trace
                    rig.trace.append(("discovery", json.loads(data)))
                    if data != b"null\n":
                        value = json.loads(data)
                        if value["hold_id"] is not None:
                            assert any(e.body.get("request_id") == value["hold_id"] and
                                       e.body.get("decision") == "accepted" for e in rig.checkout.journal.read())
                super().write(path, data)
                if (path.name == "active.json" and data != b"null\n" and rig.pause):
                    rig.pause = False
                    publish_request(rig.checkout.config.state_dir,
                                    ControlRequest("10-pause", json.loads(data)["lifecycle_id"], "pause", None), self)
        async def sleep(delay):
            assert delay > 0
            await self.held.put(self.consumer.projection)
            await self.wake.get()
        self.checkout = replace(self.checkout, fs=Files(), sleep=sleep, control=None)
        self.checkout = replace(self.checkout, control=cli.build_control(self.checkout))

    @property
    def consumer(self):
        return self.consumers[-1]

    def request(self, id, verb, hold=None, life=None):
        from tempo.control import ControlRequest, publish_request
        publish_request(self.checkout.config.state_dir,
                        ControlRequest(id, life or self.consumer.inbox.lifecycle_id, verb, hold), self.checkout.fs)
        self.wake.put_nowait(None)

    async def resume(self):
        self.request("90-resume", "resume", self.consumer.projection.pause_id)

    async def run(self, dispatch=None, before_dispatch=None):
        from tempo.drain import drain
        async def callback(ticket):
            self.calls.append(ticket.stem)
            self.trace.append("dispatch")
            self.checkout.journal.append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket=ticket.stem)
            return "merged"
        return await drain(self.checkout, dispatch or callback, before_dispatch=before_dispatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["fresh", "retry", "machine-keep", "empty"])
async def test_live_drain_pause_blocks_all_offer_accounting(root, monkeypatch, kind):
    from tests.test_drain import confirmed, offer_events
    from tempo.control import CONTROL_DECISION
    live = LiveDrain(root, monkeypatch)
    c = live.checkout
    if kind != "empty":
        write(root, "work", confirmed())
        await c.git.add(root, ["tickets/work/ticket.md"])
        await c.git.commit(root, "ticket")
    if kind in {"retry", "machine-keep"}:
        c.journal.append(EventType.STATE_TRANSITION, {"to": "gate_failed"}, ticket="work")
    if kind == "machine-keep":
        c.journal.append(EventType.SIGNAL, {"signal": "reject_arrival"}, ticket="work")
    before = offer_events(root)
    task = asyncio.create_task(live.run())
    assert (await live.held.get()).pause_id == "10-pause"
    assert offer_events(root) == before and live.calls == [] and not task.done()
    for id, hold, life in (("20-premature", "future", None), ("30-wrong", "wrong", None),
                           ("40-old", "10-pause", "previous")):
        live.request(id, "resume", hold, life)
        assert (await live.held.get()).pause_id == "10-pause"
        assert offer_events(root) == before and live.calls == [] and not task.done()
    await live.resume()
    report = await task
    assert report.merged == ([] if kind == "empty" else ["work"])
    events = c.journal.read()
    decisions = [e for e in events if e.body.get("kind") == CONTROL_DECISION]
    assert [e.body["decision"] for e in decisions] == ["accepted", "stale", "stale", "stale", "accepted"]
    after = offer_events(root)[len(before):]
    if kind == "machine-keep":
        keep = after.pop(0)
        assert keep.body == {"signal": "reject_verdict", "actor": "machine", "verdict": "keep"}
    if kind in {"retry", "machine-keep"}:
        draw = after.pop(0)
        assert draw.type == EventType.CAP_CONSUMED and draw.body["cap"] == "retry"
        assert events.index(decisions[-1]) < events.index(draw)
    if kind != "empty":
        assert [e.body["to"] for e in after] == ["running", "merged"]
        assert events.index(decisions[-1]) < events.index(after[0])
    assert live.trace[-2:] == [("discovery", None), "unlock"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["caps", "retire", "priority", "retry", "premise"])
async def test_live_drain_resume_rechecks_selection_and_caps(root, monkeypatch, change):
    from tests.test_drain import confirmed, offer_events
    live = LiveDrain(root, monkeypatch, pause=False)
    c = live.checkout
    write(root, "work", confirmed())
    await c.git.add(root, ["tickets/work/ticket.md"])
    await c.git.commit(root, "ticket")
    blob = await c.git.rev_parse(root, "HEAD:tickets/work/ticket.md")
    if change in {"retry", "caps", "premise"}:
        c.journal.append(EventType.STATE_TRANSITION, {"to": "running", "ticket_sha": "old"}, ticket="work")
        c.journal.append(EventType.STATE_TRANSITION, {"to": "premise_failed" if change == "premise" else "gate_failed",
                                                    "rung": {"tier": "high", "effort": "max"}}, ticket="work")
    checkpoints = 0
    async def pause_after_preparation():
        nonlocal checkpoints
        from tempo.control import ControlRequest, publish_request
        checkpoints += 1
        if checkpoints == 2:
            publish_request(c.config.state_dir,
                            ControlRequest("10-pause", live.consumer.inbox.lifecycle_id, "pause", None), c.fs)
    task = asyncio.create_task(live.run(before_dispatch=pause_after_preparation))
    await live.held.get()
    if change == "caps":
        for _ in range(c.config.caps.retry):
            caps.consume(c.journal, "work", "retry", blob)
    elif change == "retire":
        c.journal.append(EventType.STATE_TRANSITION, {"to": "rejected"}, ticket="work")
    elif change == "priority":
        write(root, "urgent", confirmed(priority="P0"))
        await c.git.add(root, ["tickets/urgent/ticket.md"])
        await c.git.commit(root, "new urgent")
    before = offer_events(root)
    await live.resume()
    report = await task
    assert live.calls == ([] if change in {"caps", "retire"} else ["urgent", "work"] if change == "priority" else ["work"])
    assert report.merged == live.calls
    after = offer_events(root)[len(before):]
    draws = [e for e in after if e.type == EventType.CAP_CONSUMED]
    if change == "retry":
        assert len(draws) == 1
        assert draws[0].body == {"cap": "retry", "ticket_sha": blob, "rung": {"tier": "high", "effort": "max"}}
        assert after[0] is draws[0] and after[1].body == {"to": "running", "ticket_sha": blob}
    else:
        assert draws == []


@pytest.mark.asyncio
@pytest.mark.parametrize("exit", ["normal", "failure", "cancel", "cancel-held"])
async def test_control_lifecycle_cleanup_on_exit(root, monkeypatch, exit):
    from tempo.lockfile import Lockfile
    from tests.test_drain import confirmed
    live = LiveDrain(root, monkeypatch)
    c = live.checkout
    write(root, "work", confirmed())
    await c.git.add(root, ["tickets/work/ticket.md"])
    await c.git.commit(root, "ticket")
    entered, finish = asyncio.Event(), asyncio.Event()
    async def dispatch(ticket):
        live.calls.append(ticket.stem)
        if exit == "failure":
            raise ValueError("stage failure")
        if exit == "cancel":
            entered.set()
            await finish.wait()
        c.journal.append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket=ticket.stem)
        return "merged"
    task = asyncio.create_task(live.run(dispatch))
    await live.held.get()
    life = live.consumer.inbox.lifecycle_id
    if exit != "cancel-held":
        await live.resume()
    if exit == "cancel-held":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif exit == "failure":
        with pytest.raises(ValueError, match="stage failure"):
            await task
    elif exit == "cancel":
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    trace = live.trace
    assert trace[0] == "lock"
    discoveries = [i for i, item in enumerate(trace) if isinstance(item, tuple) and item[0] == "discovery"]
    assert trace[discoveries[0]] == ("discovery", {"lifecycle_id": life, "hold_id": None})
    refreshes = [("10-pause", "pause")] + ([] if exit == "cancel-held" else [(None, "resume")])
    for hold, verb in refreshes:
        decision = next(i for i, item in enumerate(trace) if isinstance(item, tuple) and item[0] == "decision" and item[1]["verb"] == verb)
        refresh = next(i for i in discoveries[1:] if trace[i][1] == {"lifecycle_id": life, "hold_id": hold})
        assert decision < refresh
    retirement = discoveries[-1]
    assert trace[retirement:retirement + 2] == [("discovery", None), "unlock"]
    # Retired discovery refuses a publisher that has established contention.
    from tests.test_control_cli import invoke
    lock = Lockfile(c.config.state_dir, instance_id="unpublished-replacement", clock=c.clock)
    lock.acquire()
    try:
        assert await asyncio.to_thread(invoke, root, "pause") == 2
    finally:
        lock.release()
    # The replacement uses a fresh lifecycle and overwrites retirement, never recovering the old hold.
    live.trace.clear()
    c = live.checkout = replace(c, journal=Journal(c.config.state_dir, c.clock), control=None)
    c = live.checkout = replace(c, control=cli.build_control(c))
    if exit in {"failure", "cancel", "cancel-held"}:
        c.journal.append(EventType.STATE_TRANSITION, {"to": "already_satisfied"}, ticket="work")
    await live.run()
    assert live.consumer.inbox.lifecycle_id != life and live.consumer.projection.pause_id is None
    assert live.trace[0] == "lock"
    assert live.trace[1] == ("discovery", {"lifecycle_id": live.consumer.inbox.lifecycle_id, "hold_id": None})
    assert live.trace[-2:] == [("discovery", None), "unlock"]


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
    rig.put_config(CONFIG.replace("x-med", "edited-after-release"))
    publish_request(state, ControlRequest("90-resume", life, "resume", "10-pause"), LocalFileSystem())
    wake.set()
    assert await waiting == "merged"
    assert completed == effects == ["first", "second"] and len(snapshots) == len(prepared) == 2
    assert prepared[-1].config.providers[1].models_by_tier.medium == "edited-after-release"


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["checkpoint", "publication", "refresh", "retirement", "decision-order"])
async def test_pause_activation_observables_detect_removed_bindings(root, monkeypatch, binding):
    """Calibrate positive production assertions by breaking their actual binding."""
    from tempo.control import ControlProjection
    from tests.test_drain import confirmed
    live = LiveDrain(root, monkeypatch)
    c = live.checkout
    write(root, "work", confirmed())
    await c.git.add(root, ["tickets/work/ticket.md"])
    await c.git.commit(root, "ticket")
    async def noop(_):
        pass
    if binding == "checkpoint":
        monkeypatch.setattr(control.PauseConsumer, "checkpoint", noop)
    elif binding == "publication":
        monkeypatch.setattr(control.PauseConsumer, "publish", lambda _: None)
    elif binding == "refresh":
        def apply(consumer, projection):
            consumer.projection = projection
        monkeypatch.setattr(live.consumer.inbox, "apply", lambda projection: apply(live.consumer, projection))
    elif binding == "retirement":
        monkeypatch.setattr(control.PauseConsumer, "retire", lambda _: None)
    else:
        original_append = c.journal.append
        def append(type, body, **kwargs):
            if body.get("kind") == "control_decision" and body["verb"] == "pause":
                live.consumer._apply(ControlProjection(body["lifecycle_id"], body["request_id"]))
            return original_append(type, body, **kwargs)
        monkeypatch.setattr(c.journal, "append", append)
    task = asyncio.create_task(live.run())
    waiter = asyncio.create_task(live.held.get())
    try:
        await asyncio.wait((task, waiter), return_when=asyncio.FIRST_COMPLETED)
        if binding in {"checkpoint", "publication"}:
            await task
            with pytest.raises(AssertionError):
                assert waiter.done(), "production pause checkpoint never held the offer"
        elif binding == "decision-order":
            with pytest.raises(AssertionError):
                await task
        else:
            assert await waiter
            if binding == "refresh":
                with pytest.raises(AssertionError):
                    assert json.loads((c.config.state_dir / "control/active.json").read_bytes())["hold_id"] == "10-pause"
            await live.resume()
            await task
            if binding == "retirement":
                with pytest.raises(AssertionError):
                    assert live.trace[-2:] == [("discovery", None), "unlock"]
    finally:
        for pending in (waiter, task):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(waiter, task, return_exceptions=True)


@pytest.mark.asyncio
async def test_production_shares_one_admission_control_consumer(root, tmp_path, monkeypatch):
    from tests.test_cli import Clock, ENV as checkout_env
    captured = capture_pipeline_queue(monkeypatch)
    trace, roots, consumers = [], [], []
    original = cli.build_control
    def build(checkout):
        assert checkout.control is None
        trace.append("build")
        roots.append(checkout)
        consumer = original(checkout)
        consumers.append(consumer)
        for name in ("publish", "checkpoint", "retire"):
            callback = getattr(consumer, name)
            if name == "checkpoint":
                async def checked(_callback=callback):
                    trace.append("consume")
                    await _callback()
                consumer.checkpoint = checked
            else:
                def observed(_callback=callback, _name=name):
                    trace.append(_name)
                    _callback()
                setattr(consumer, name, observed)
        return consumer
    monkeypatch.setattr(cli, "build_control", build)
    def pipeline(checkout):
        assert trace == ["build"]
        trace.append("pipeline")
        assert checkout.control is consumers[0]
        for field in ("journal", "fs", "sleep", "clock", "git", "config"):
            assert getattr(checkout, field) is getattr(roots[0], field)
        writer = runner.bind(checkout, FakeLLM([]))
        assert writer.queue is captured[-1][2]
        assert writer.queue.control is checkout.control
        assert writer.ctx.driver.journal is checkout.control.inbox.journal is checkout.journal
        assert writer.queue.pending == {} and writer.queue.hold_id is None
        assert checkout.journal.read() == []
        return writer
    assert await asyncio.to_thread(cli.main, ["drain"], cwd=root, env=checkout_env,
                                   clock=Clock(), pipeline=pipeline) == 0
    assert len(consumers) == len(roots) == len(captured) == 1
    assert trace[:3] == ["build", "pipeline", "publish"] and "consume" in trace
    assert trace[-1] == "retire"
    # The daemon root snapshots its one carrier into each fresh real pipeline.
    captured.clear()
    locals = []
    async def prepare(local):
        locals.append(local)
        return runner.bind(local, FakeLLM([]))
    async def dispatched(ctx, ticket):
        return "merged"
    monkeypatch.setattr(runner, "drive", dispatched)
    trace.clear()
    rig = CoreRig(tmp_path, prepare=prepare)
    assert len(consumers) == 2
    for stem in ("first", "second"):
        ticket = await rig.add(stem)
        assert await rig.core.admission.dispatch(ticket) == "merged"
    assert len(captured) == 2 and captured[0][2] is not captured[1][2]
    assert all(local.control is rig.core.control for local in locals)
    assert all(queue.control is rig.core.control for _, _, queue in captured)
    assert rig.core.control is consumers[-1] and rig.checkout.control is None


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


@pytest.mark.parametrize("verb", ["run", "drain"])
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
async def test_admission_control_binding_probe(root, tmp_path, monkeypatch):
    compose = runner.compose_pipeline
    def unbound(ctx, *, escalate, control):
        queue = compose(ctx, escalate=escalate, control=control)
        queue.control = None
        return queue
    monkeypatch.setattr(runner, "compose_pipeline", unbound)
    with pytest.raises(AssertionError):
        await test_production_shares_one_admission_control_consumer(root, tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_production_serve_graph_is_reachable(root, monkeypatch):
    from tempo.serve import Serve
    from tests.test_serve import graph, until, finish
    rig = await graph(root, monkeypatch)
    before = asyncio.all_tasks()
    owner = Serve(rig.checkout, read=rig.owner.read, stems=rig.owner.stems)
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
    "flake", "operator", "core", "serve", "drain", "driver", "retro"])
async def test_production_boxes_receive_rereport_callback(admission_context, tmp_path, monkeypatch, site):
    import inspect
    from tempo.box import Box, RereportJournal
    from tempo.triage import triage_pass
    from tempo.retro import Retro
    from tempo.drain import drain
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
    elif site == "drain":
        checkout = replace(checkout, control=cli.build_control(checkout))
        assert checkout.control._recover.__self__._rereport.journal is checkout.journal
        await drain(checkout, writer)
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
