"""Worker notification suppression and preserved bootstrap dormancy."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from tempo import __main__ as main, daemon, runner, stages
from tempo.control import ControlProjection
from tempo.journal import EventType, run_seq
from tempo.llm import FakeLLM
from tempo.reconcile import RECOVERY_ALERT, orphans, reconcile
from tests.test_cli import ENV, Clock, Stages, root, ticket, write
from tests.test_daemon_composition import CoreRig, LiveDrain, assert_core_wiring
from tests.test_driver import ECHO, Stub, build
from tests.test_kill_executor_abort import Writer, Timer, invoke, cancelled, turn
from tests.test_kill_signal_journal import Rig, decision
from tests.test_kill_worker_stop import Worker, accepted, boundary, release_all
from tests.test_mergequeue import ctx as admission_context, ready


@asynccontextmanager
async def owned(workers):
    async def finished():
        return "consumer-owned success"
    callbacks = [worker.run for worker in workers]
    callbacks.extend([finished] * (3 - len(callbacks)))
    owner = daemon.DaemonTasks(watcher=callbacks[0], merge_queue=callbacks[1],
                               box_consumer=callbacks[2])
    run = asyncio.create_task(owner.run())
    tasks = ()
    try:
        await asyncio.gather(*(worker.started.wait() for worker in workers))
        tasks = owner.tasks
        yield owner, run, tasks
    finally:
        await release_all(tasks, workers)
        await asyncio.gather(run, return_exceptions=True)


def observer(stopper, rig, failures, *, lifecycle_id=None):
    return daemon.WorkerFailureObserver(
        lifecycle_id=lifecycle_id or stopper.lifecycle_id, workers=stopper.workers,
        projection=lambda: rig.projection, failure=failures.append)


@pytest.mark.asyncio
async def test_post_kill_worker_failures_are_suppressed(tmp_path):
    writer = Writer(held=True)
    driver, state = build(tmp_path, writer)
    writer.driver = driver
    driver.sleep = Timer()
    invocation = asyncio.create_task(invoke(driver))
    workers = [Worker(), Worker(failure=RuntimeError("unwind failed")), Worker()]
    stop = observation = None
    try:
        await writer.started.wait()
        async with owned(workers) as (owner, run, tasks):
            rig, projection = accepted(state)
            before = rig.journal.read()
            assert before[-1].body == decision("kill")
            assert before[-1].type == EventType.SIGNAL
            assert before[-1].ticket is None and before[-1].key is None
            stopper = boundary(driver, projection, tasks)
            failures = []
            watched = observer(stopper, rig, failures)
            observation = asyncio.create_task(watched.observe())
            stop = asyncio.create_task(stopper.stop(projection))
            await writer.unwinding.wait()
            await turn()
            assert failures == [] and not observation.done()
            assert all(task.cancelling() == 0 for task in tasks)
            writer.release.set()
            await asyncio.gather(*(worker.cleaning.wait() for worker in workers))
            await cancelled(invocation)
            assert driver._active is None and writer.aborts == 1
            for worker in workers:
                worker.release.set()
            await stop
            await observation
            await cancelled(run)
            await watched.observe()
            assert failures == [] and owner.tasks == ()
            assert tasks[0].cancelled() and tasks[2].cancelled()
            assert tasks[1].exception() is workers[1].failure
            assert all(task.done() and not task._log_traceback for task in tasks)
            assert rig.journal.read() == driver.journal.read() == before
            assert not (state / "spools/t-echo/1/implement/call-01/output.txt").exists()
    finally:
        writer.release.set()
        if not invocation.done():
            await driver.abort_current()
        await asyncio.gather(invocation, *[t for t in (stop, observation) if t],
                             return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["none", "pause", "resume", "stale", "rejected", "undecided", "other"])
@pytest.mark.parametrize("cancellation", [False, True])
async def test_worker_failures_without_matching_kill_are_preserved(tmp_path, case, cancellation):
    rig = Rig(tmp_path / "control")
    if case in {"pause", "resume"}:
        rig.holds.add("hold")
        rig.publish("request", case, "hold" if case == "resume" else None)
        rig.inbox.consume()
    elif case in {"stale", "rejected", "undecided", "other"}:
        path = rig.publish("request", life="previous" if case == "stale" else None)
        if case == "rejected":
            rig.fs.write(path, b"{}")
        if case != "undecided":
            rig.inbox.consume()
    projection = rig.reconstruct().recover()
    executor = tmp_path / "executor"
    executor.mkdir()
    driver, _ = build(executor, FakeLLM([]))
    error = None if cancellation else ValueError("original worker error")
    worker = Worker(failure=error)
    failures = []
    async with owned([worker]) as (owner, run, tasks):
        stopper = daemon.WorkerStop(lifecycle_id="replacement" if case == "other" else rig.life,
                                    abort=lambda: daemon.executor_abort(driver.abort_current), workers=tasks)
        watched = observer(stopper, rig, failures)
        observation = asyncio.create_task(watched.observe())
        before = rig.journal.read()
        try:
            await stopper.stop(projection)
            await turn()
            assert failures == [] and not observation.done()
            assert not tasks[0].done() and tasks[0].cancelling() == 0
            tasks[0].cancel()
            await worker.cleaning.wait()
            worker.release.set()
            await observation
            with pytest.raises(asyncio.CancelledError if cancellation else ValueError) as caught:
                await run
            assert len(failures) == 1
            if cancellation:
                assert isinstance(failures[0], asyncio.CancelledError) and tasks[0].cancelled()
            else:
                assert failures[0] is caught.value is error
            # A subsequent kill cannot withdraw a delivered failure.
            rig.publish("later-kill")
            rig.inbox.consume()
            await watched.observe()
            assert len(failures) == 1 and owner.tasks == ()
            assert rig.journal.read()[:len(before)] == before
            assert driver.journal.read() == [] and driver._active is None
        finally:
            await release_all(tasks, [worker])
            await asyncio.gather(observation, return_exceptions=True)


@pytest.mark.asyncio
async def test_kill_suppression_observes_all_worker_outcomes(tmp_path):
    driver, state = build(tmp_path, FakeLLM([]))
    rig = Rig(state)
    workers = [Worker(failure=ValueError("early failure")),
               Worker(failure=RuntimeError("shutdown failure")), Worker()]
    failures = []
    baseline = asyncio.all_tasks()
    async with owned(workers) as (owner, run, tasks):
        stopper = boundary(driver, rig.projection, tasks)
        watched = observer(stopper, rig, failures)
        observations = [asyncio.create_task(watched.observe()) for _ in range(2)]
        stop = None
        try:
            tasks[0].cancel()
            await workers[0].cleaning.wait()
            workers[0].release.set()
            await asyncio.gather(*(worker.cleaning.wait() for worker in workers[1:]))
            await turn()
            assert failures == [workers[0].failure]
            assert all(not waiter.done() for waiter in observations)
            rig.publish("kill")
            rig.inbox.consume()
            before = rig.journal.read()
            stop = asyncio.create_task(stopper.stop(rig.reconstruct().recover()))
            await turn()
            assert not stop.done() and all(not task.done() for task in tasks[1:])
            workers[1].release.set()
            await turn()
            assert all(not waiter.done() for waiter in observations)
            workers[2].release.set()
            await stop
            await asyncio.gather(*observations)
            with pytest.raises(ValueError) as caught:
                await run
            assert caught.value is workers[0].failure
            await watched.observe()
            assert failures == [workers[0].failure] and owner.tasks == ()
            assert all(task.done() and not task._log_traceback for task in tasks)
            assert tasks[1].exception() is workers[1].failure and tasks[2].cancelled()
            assert rig.journal.read() == before
        finally:
            await release_all(tasks, workers)
            await asyncio.gather(*observations, *([stop] if stop else []), return_exceptions=True)
    assert asyncio.all_tasks() == baseline


@pytest.mark.asyncio
async def test_kill_suppression_waiter_cancellation_awaits_cleanup(tmp_path):
    driver, state = build(tmp_path, FakeLLM([]))
    rig, projection = accepted(state)
    workers = [Worker(), Worker(failure=RuntimeError("cleanup")), Worker()]
    failures = []
    async with owned(workers) as (owner, run, tasks):
        stopper = boundary(driver, projection, tasks)
        watched = observer(stopper, rig, failures)
        waiters = [asyncio.create_task(watched.observe()) for _ in range(2)]
        stop = None
        try:
            await turn()
            await turn()
            waiters[0].cancel()
            await turn()
            assert all(task.cancelling() == 0 for task in tasks)
            stop = asyncio.create_task(stopper.stop(projection))
            await asyncio.gather(*(worker.cleaning.wait() for worker in workers))
            for _ in range(3):
                waiters[0].cancel()
                await turn()
                assert not waiters[0].done() and not stop.done()
                assert all(worker.cancellations == 1 for worker in workers)
                assert all(not task.done() for task in tasks)
            for worker in workers:
                worker.release.set()
            await cancelled(waiters[0])
            assert all(task.done() and not task._log_traceback for task in tasks)
            await waiters[1]
            await stop
            await cancelled(run)
            await watched.observe()
            assert failures == [] and owner.tasks == ()
        finally:
            await release_all(tasks, workers)
            await asyncio.gather(*waiters, *([stop] if stop else []), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("abort_failure", [False, True])
async def test_kill_suppression_preserves_run_and_abort_failures(admission_context, abort_failure):
    ctx = admission_context
    work = await ready(ctx)
    journal = ctx.driver.journal
    journal.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket=work.stem)
    # Existing runner custody produces the harvest before any notification suppression.
    await runner.harvest_failure(ctx, work, attempt=0, stage="implement", outcome="infra_error",
                                 findings=[])
    harvest_path = ctx.repo / f"tickets/{work.stem}/attempts/0/harvest.json"
    harvest = harvest_path.read_bytes()
    assert json.loads(harvest)["terminal"] == "infra_error"
    assert await runner.failure_terminal(ctx, work, outcome="infra_error", stage="implement",
                                         findings=[], attempt=0, verdict="retry") == "infra_error"
    prior_terminal = journal.read()[-1]
    await stages.prepare_worktree(ctx, work.stem)
    journal.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket=work.stem)
    writer = Writer()
    ctx.driver.llm = writer
    ctx.driver.sleep = Timer()
    invocation = asyncio.create_task(ctx.driver.run(
        ECHO, Stub(text="hi"), ticket=work.stem, attempt=1, workspace=ctx.worktree(work.stem),
        tier="medium", effort="low", stuck_budget=600))
    await writer.started.wait()
    rig, projection = accepted(ctx.config.state_dir)
    worker = Worker(failure=RuntimeError("worker unwind"))
    failures = []
    error = RuntimeError("executor abort refused")
    original = writer.abort_current
    if abort_failure:
        def refused():
            raise error
        writer.abort_current = refused
    async with owned([worker]) as (owner, run, tasks):
        stopper = boundary(ctx.driver, projection, tasks)
        watched = observer(stopper, rig, failures)
        observation = asyncio.create_task(watched.observe())
        before = journal.read()
        try:
            if abort_failure:
                with pytest.raises(RuntimeError) as caught:
                    await stopper.stop(projection)
                assert caught.value is error
                await turn()
                assert not tasks[0].done() and tasks[0].cancelling() == 0
                assert not invocation.done() and writer.cancels == 0
                assert not observation.done() and failures == []
            else:
                worker.release.set()
                await stopper.stop(projection)
                await observation
                with pytest.raises(RuntimeError) as caught:
                    await run
                assert caught.value is worker.failure and failures == []
                await cancelled(invocation)
            assert journal.read() == before and harvest_path.read_bytes() == harvest
            assert prior_terminal in journal.read()
            assert ctx.worktree(work.stem).exists() and work.stem in orphans(journal.read())
            intents = {e.key for e in before if e.type == EventType.EFFECT_INTENT
                       and e.key.startswith("llm/")}
            assert intents and not any(e.type == EventType.EFFECT_COMPLETION and e.key in intents
                                       for e in journal.read())
        finally:
            writer.abort_current = original
            if ctx.driver._active is not None and abort_failure:
                ctx.driver._active.abort = None
            await daemon.executor_abort(ctx.driver.abort_current)
            await release_all(tasks, [worker])
            await asyncio.gather(invocation, observation, return_exceptions=True)
    if not abort_failure:
        assert await runner.failure_terminal(ctx, work, outcome="infra_error", stage="implement",
                                             findings=[], attempt=1, verdict="retry") == "infra_error"
        terminal = journal.read()[-1]
        assert terminal.type == EventType.STATE_TRANSITION and terminal.ticket == work.stem
        assert terminal.body == {"to": "infra_error", "stage": "implement",
                                 "reason": "infra_error", "dispatch": "escalate",
                                 "rung": {"tier": "medium", "effort": "high"}}
        assert harvest_path.read_bytes() == harvest and not ctx.worktree(work.stem).exists()
        return
    checkout = runner.Checkout(ctx.repo, ctx.config, ctx.env, ctx.exec_, ctx.git,
                               journal, ctx.fs, ctx.driver.clock, ctx.driver.sleep)
    orphan_attempt = run_seq(journal.read(), work.stem)
    # Suppression leaves the interrupted run for the real restart writer to harvest and reap.
    reaped = await reconcile(journal, ctx.git, ctx.repo, ctx.config.worktree_root,
                            lambda stem, attempt: runner.harvest_orphan(checkout, stem, attempt))
    assert reaped == [work.stem] and not ctx.worktree(work.stem).exists()
    terminal, alert = journal.read()[-2:]
    assert terminal.type == EventType.STATE_TRANSITION and terminal.body == {"to": "abandoned"}
    assert terminal.ticket == alert.ticket == work.stem and terminal.key is alert.key is None
    assert alert.type == EventType.SIGNAL
    assert alert.body == {"kind": RECOVERY_ALERT, "disposition": "alert", "outcome": "abandoned",
                          "reason": "orphaned run", "run_seq": orphan_attempt}
    orphan_harvest = ctx.repo / f"tickets/{work.stem}/attempts/{orphan_attempt}/harvest.json"
    assert json.loads(orphan_harvest.read_bytes())["terminal"] == "abandoned"
    assert harvest_path.read_bytes() == harvest


@pytest.mark.parametrize("verb", ["run", "drain"])
def test_kill_failure_suppression_is_dormant(root, tmp_path, monkeypatch, verb):
    async def probe(*args, **kwargs):
        raise AssertionError("suppression wired")
    dormant = daemon.WorkerFailureObserver(lifecycle_id="life", workers=(),
                                          projection=lambda: ControlProjection("life"),
                                          failure=lambda _: None)
    monkeypatch.setattr(daemon.WorkerFailureObserver, "observe", probe)
    write(root, "work", ticket())
    argv = ["run", "work"] if verb == "run" else ["drain"]
    def deliberate(checkout):
        async def dispatch(_):
            await dormant.observe()
        return dispatch
    with pytest.raises(AssertionError, match="suppression wired"):
        main.main(argv, cwd=root, env=ENV, clock=Clock(), pipeline=deliberate)

    def construction_probe(*args, **kwargs):
        raise AssertionError("suppression bound")
    monkeypatch.setattr(daemon.WorkerFailureObserver, "__init__", construction_probe)
    def bind(checkout):
        daemon.WorkerFailureObserver(lifecycle_id="life", workers=(), projection=probe, failure=probe)
    with pytest.raises(AssertionError, match="suppression bound"):
        main.main(argv, cwd=root, env=ENV, clock=Clock(), pipeline=bind)
    stages = Stages()
    assert main.main(argv, cwd=root, env=ENV, clock=Clock(), pipeline=stages) == 0
    assert stages.calls == ["work"] and stages.lock_held == [True]

    async def production():
        baseline = asyncio.all_tasks()
        directory = tmp_path / "graph"
        directory.mkdir()
        async def prepare(local):
            async def dispatch(_):
                return "merged"
            return dispatch
        rig = CoreRig(directory, prepare=prepare)
        assert_core_wiring(rig)
        assert asyncio.all_tasks() == baseline and rig.journal.read() == []
        assert rig.exec.calls == [] and rig.fs.files == {}
        await rig.add("composed")
        original = rig.core.admission._dispatch
        for binding, message in ((deliberate, "suppression wired"), (bind, "suppression bound")):
            async def wired(work, binding=binding):
                callback = binding(rig.checkout)
                return await callback(work)
            rig.core.admission._dispatch = wired
            with pytest.raises(AssertionError, match=message):
                await rig.core.admission.dispatch(rig.core.scheduler.pending["composed"])
        rig.core.admission._dispatch = original
        await rig.drain()
        assert asyncio.all_tasks() == baseline
        with monkeypatch.context() as patch:
            live = LiveDrain(root, patch, pause=False)
            assert (await live.run()).merged == []
            assert asyncio.all_tasks() == baseline
    asyncio.run(production())


@pytest.mark.asyncio
async def test_failure_observation_is_active_in_serve(root, monkeypatch):
    from tests.test_serve import activated_graph
    rig = await activated_graph(root, monkeypatch)
    assert rig.owner.observer.workers == rig.owner.workers
    assert rig.owner.observer._observation.done()
