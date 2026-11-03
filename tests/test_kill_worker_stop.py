"""Executor-before-worker ordering and calibrated production dormancy."""

import asyncio
from functools import partial

import pytest

from tempo import __main__ as main, daemon
from tempo.control import ControlProjection
from tempo.llm import FakeLLM
from tests.test_cli import ENV, Clock, Stages, root, ticket, write
from tests.test_daemon_composition import CoreRig, assert_core_wiring
from tests.test_driver import build
from tests.test_kill_executor_abort import Writer, Timer, invoke, cancelled, turn
from tests.test_kill_signal_journal import Rig, decision
from tempo import control


def accepted(state):
    rig = Rig(state)
    rig.publish("kill")
    rig.inbox.consume()
    # Supply a projection reconstructed from the sole writer's durable decision.
    projection = rig.reconstruct().recover()
    assert projection.kill_requested
    return rig, projection


def boundary(driver, projection, workers):
    return daemon.WorkerStop(lifecycle_id=projection.lifecycle_id,
                            abort=partial(control.executor_abort, driver.abort_current),
                            workers=workers)


class Worker:
    def __init__(self, *, failure=None):
        self.started = asyncio.Event()
        self.cleaning = asyncio.Event()
        self.release = asyncio.Event()
        self.cancellations = 0
        self.failure = failure

    async def run(self):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancellations += 1
            self.cleaning.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancellations += 1
                raise AssertionError("worker cleanup cancelled again")
            if self.failure is not None:
                raise self.failure
            raise


async def release_all(tasks, workers):
    # Protect fixture cleanup even when a discriminating assertion fails.
    for worker in workers:
        worker.release.set()
    for task in tasks:
        if not task.done() and not task.cancelling():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_executor_unwinds_before_worker_cancel(tmp_path):
    writer = Writer(held=True)
    driver, state = build(tmp_path, writer)
    writer.driver = driver
    timer = Timer()
    driver.sleep = timer
    peers = [Worker(), Worker()]
    dispatch_entered, dispatch_cleaning, dispatch_release = (asyncio.Event() for _ in range(3))

    async def dispatch():
        dispatch_entered.set()
        try:
            await invoke(driver)
        finally:
            dispatch_cleaning.set()
            await dispatch_release.wait()

    owner = daemon.DaemonTasks(watcher=dispatch, merge_queue=peers[0].run,
                               box_consumer=peers[1].run)
    run = asyncio.create_task(owner.run())
    stop = None
    tasks = ()
    try:
        await dispatch_entered.wait()
        await writer.started.wait()
        await timer.started.wait()
        await asyncio.gather(*(worker.started.wait() for worker in peers))
        tasks = owner.tasks
        rig, projection = accepted(state)
        before = driver.journal.read()
        assert before[-1].body == decision("kill")
        assert before[-1].ticket is None and before[-1].key is None
        stopping = boundary(driver, projection, tasks)
        stop = asyncio.create_task(stopping.stop(projection))
        await writer.unwinding.wait()
        await turn()
        assert all(task.cancelling() == 0 and not task.done() for task in tasks)
        assert not dispatch_cleaning.is_set() and not stop.done()
        assert writer.aborts == writer.cancels == 1
        writer.release.set()
        await dispatch_cleaning.wait()
        await asyncio.gather(*(worker.cleaning.wait() for worker in peers))
        assert driver._active is None and not stop.done()
        assert tasks[0].cancelling() == 1
        dispatch_release.set()
        for worker in peers:
            worker.release.set()
        await stop
        await cancelled(run)
        assert owner.tasks == () and all(task.done() for task in tasks)
        assert all(not task._log_traceback for task in tasks)
        assert [worker.cancellations for worker in peers] == [1, 1]
        assert driver.journal.read() == rig.journal.read() == before
        assert not (state / "spools/t-echo/1/implement/call-01/output.txt").exists()
    finally:
        writer.release.set()
        dispatch_release.set()
        await release_all(tasks, peers)
        await asyncio.gather(*([stop] if stop else []), run, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["none", "pause", "resume", "undecided", "stale", "rejected", "other"])
async def test_worker_stop_requires_matching_kill(tmp_path, case):
    rig = Rig(tmp_path)
    if case in {"pause", "resume"}:
        rig.holds.add("hold")
        rig.publish("request", case, "hold" if case == "resume" else None)
        rig.inbox.consume()
    elif case in {"undecided", "stale", "rejected", "other"}:
        path = rig.publish("request", life="previous" if case == "stale" else None)
        if case == "rejected":
            rig.fs.write(path, b"{}")
        if case != "undecided":
            rig.inbox.consume()
    projection = rig.reconstruct().recover()
    (tmp_path / "executor").mkdir()
    driver, _ = build(tmp_path / "executor", FakeLLM([]))
    aborts = []
    original = driver.abort_current
    async def abort():
        aborts.append(True)
        await control.executor_abort(original)
    worker = Worker()
    task = asyncio.create_task(worker.run())
    await worker.started.wait()
    before = rig.journal.read()
    stopper = daemon.WorkerStop(lifecycle_id="replacement" if case == "other" else rig.life,
                                abort=abort, workers=[task])
    try:
        await stopper.stop(projection)
        assert aborts == [] and not task.done() and task.cancelling() == 0
        assert stopper._stop is None and rig.journal.read() == before
        assert driver._active is None and driver.journal.read() == []
    finally:
        await release_all([task], [worker])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["mixed", "finished", "empty"])
async def test_worker_stop_observes_all_workers(tmp_path, mode):
    driver, state = build(tmp_path, FakeLLM([]))
    rig, projection = accepted(state)
    before = rig.journal.read()
    baseline = asyncio.all_tasks()
    workers = [Worker(), Worker(failure=RuntimeError("cleanup failed"))]
    live = [asyncio.create_task(worker.run()) for worker in workers] if mode == "mixed" else []
    async def completed(failure=False):
        if failure:
            raise ValueError("already failed")
        return "already finished"
    finished = [] if mode == "empty" else [asyncio.create_task(completed()),
                                asyncio.create_task(completed(True)),
                                asyncio.create_task(asyncio.Event().wait())]
    if finished:
        finished[-1].cancel()
        await turn()
        assert all(task.done() for task in finished)
        assert finished[1]._log_traceback
    tasks = (*live, *finished)
    abort_entered, abort_release = asyncio.Event(), asyncio.Event()
    aborts = []
    async def abort():
        aborts.append(True)
        abort_entered.set()
        await abort_release.wait()
        await control.executor_abort(driver.abort_current)
    stopping = daemon.WorkerStop(lifecycle_id=projection.lifecycle_id, abort=abort, workers=tasks)
    stop = None
    try:
        if live:
            await asyncio.gather(*(worker.started.wait() for worker in workers))
        stop = asyncio.create_task(stopping.stop(projection))
        await abort_entered.wait()
        assert not stop.done() and all(task.cancelling() == 0 for task in live)
        abort_release.set()
        if live:
            await asyncio.gather(*(worker.cleaning.wait() for worker in workers))
            assert not stop.done() and all(not task.done() for task in live)
            for worker in workers:
                worker.release.set()
        await stop
        assert aborts == [True]
        assert all(task.done() and not task._log_traceback for task in tasks)
        assert driver.journal.read() == before and driver._active is None
        assert asyncio.all_tasks() == baseline
    finally:
        abort_release.set()
        await release_all(tasks, workers)
        if stop:
            await asyncio.gather(stop, return_exceptions=True)


@pytest.mark.asyncio
async def test_worker_stop_is_atomic_under_repeated_cancellation(tmp_path):
    writer = Writer(held=True)
    driver, state = build(tmp_path, writer)
    driver.sleep = Timer()
    invocation = asyncio.create_task(invoke(driver))
    await writer.started.wait()
    _, projection = accepted(state)
    worker = Worker()
    task = asyncio.create_task(worker.run())
    await worker.started.wait()
    stopping = boundary(driver, projection, [task])
    waiters = [asyncio.create_task(stopping.stop(projection)) for _ in range(2)]
    later_worker = Worker()
    later = None
    try:
        await writer.unwinding.wait()
        protected = stopping._stop
        for _ in range(3):
            waiters[0].cancel()
            await turn()
            assert not any(waiter.done() for waiter in waiters)
            assert task.cancelling() == 0 and stopping._stop is protected
        writer.release.set()
        await worker.cleaning.wait()
        await cancelled(invocation)
        for _ in range(3):
            for waiter in waiters:
                waiter.cancel()
            await turn()
            assert not any(waiter.done() for waiter in waiters)
            assert task.cancelling() == worker.cancellations == 1
            assert not protected.done()
        third = asyncio.create_task(stopping.stop(projection))
        waiters.append(third)
        worker.release.set()
        await cancelled(waiters[0])
        await cancelled(waiters[1])
        await third
        assert protected.done() and task.done() and writer.aborts == 1
        later = asyncio.create_task(later_worker.run())
        await later_worker.started.wait()
        await stopping.stop(projection)
        assert not later.done() and later.cancelling() == 0 and writer.aborts == 1
        fresh = boundary(driver, projection, [later])
        later_worker.release.set()
        await fresh.stop(projection)
        assert later.done() and later_worker.cancellations == 1
    finally:
        writer.release.set()
        await release_all([task] + ([later] if later else []), [worker, later_worker])
        await asyncio.gather(invocation, *waiters, return_exceptions=True)


@pytest.mark.asyncio
async def test_executor_abort_failure_does_not_cancel_workers(tmp_path):
    writer = Writer()
    driver, state = build(tmp_path, writer)
    driver.sleep = Timer()
    invocation = asyncio.create_task(invoke(driver))
    await writer.started.wait()
    _, projection = accepted(state)
    worker = Worker()
    task = asyncio.create_task(worker.run())
    await worker.started.wait()
    error = RuntimeError("executor abort refused")
    original = writer.abort_current
    def refused():
        raise error
    writer.abort_current = refused
    stopping = boundary(driver, projection, [task, invocation])
    before = driver.journal.read()
    marker = state / "restart-reconcilable-worktree"
    marker.write_text("preserve")
    try:
        with pytest.raises(RuntimeError) as caught:
            await stopping.stop(projection)
        assert caught.value is error
        assert all(not owned.done() and owned.cancelling() == 0 for owned in (task, invocation))
        assert writer.cancels == 0 and driver._active is not None
        assert driver.journal.read() == before and marker.read_text() == "preserve"
        with pytest.raises(RuntimeError) as repeated:
            await stopping.stop(projection)
        assert repeated.value is error and worker.cancellations == 0
    finally:
        writer.abort_current = original
        driver._active.abort = None
        await control.executor_abort(driver.abort_current)
        await release_all([task, invocation], [worker])


@pytest.mark.parametrize("verb", ["run"])
def test_kill_worker_stop_is_dormant(root, tmp_path, monkeypatch, verb):
    async def probe(*args, **kwargs):
        raise AssertionError("worker stop wired")
    monkeypatch.setattr(daemon.WorkerStop, "stop", probe)
    dormant = daemon.WorkerStop(lifecycle_id="life", abort=probe, workers=())
    projection = ControlProjection("life", kill_requested=True)
    write(root, "work", ticket())
    argv = ["run", "work"] if verb == "run" else ["drain"]
    def deliberate(checkout):
        async def dispatch(_):
            await dormant.stop(projection)
        return dispatch
    with pytest.raises(AssertionError, match="worker stop wired"):
        main.main(argv, cwd=root, env=ENV, clock=Clock(), pipeline=deliberate)

    def construction_probe(*args, **kwargs):
        raise AssertionError("worker stop bound")
    monkeypatch.setattr(daemon.WorkerStop, "__init__", construction_probe)
    def bind(checkout):
        daemon.WorkerStop(lifecycle_id="life", abort=probe, workers=())
    with pytest.raises(AssertionError, match="worker stop bound"):
        main.main(argv, cwd=root, env=ENV, clock=Clock(), pipeline=bind)
    stages = Stages()
    assert main.main(argv, cwd=root, env=ENV, clock=Clock(), pipeline=stages) == 0
    assert stages.calls == ["work"] and stages.lock_held == [True]

    async def production():
        before = asyncio.all_tasks()
        directory = tmp_path / "graph"
        directory.mkdir()
        async def prepare(local):
            async def dispatch(_):
                return "merged"
            return dispatch
        rig = CoreRig(directory, prepare=prepare)
        assert_core_wiring(rig)
        assert asyncio.all_tasks() == before and rig.journal.read() == []
        assert rig.exec.calls == [] and rig.fs.files == {}
        await rig.add("composed")
        # Calibrate both probes through the real production graph's dispatch binding.
        original = rig.core.admission._dispatch
        for binding, message in ((deliberate, "worker stop wired"), (bind, "worker stop bound")):
            async def wired(work, binding=binding):
                callback = binding(rig.checkout)
                return await callback(work)
            rig.core.admission._dispatch = wired
            with pytest.raises(AssertionError, match=message):
                await rig.core.admission.dispatch(rig.core.scheduler.pending["composed"])
        rig.core.admission._dispatch = original
        await rig.drain()
        assert asyncio.all_tasks() == before
    asyncio.run(production())


@pytest.mark.asyncio
async def test_worker_stop_is_active_in_serve(root, monkeypatch):
    from tests.test_serve import activated_graph
    rig = await activated_graph(root, monkeypatch)
    assert rig.owner.stopper.workers == rig.owner.workers
    assert rig.owner.stopper._stop.done()
