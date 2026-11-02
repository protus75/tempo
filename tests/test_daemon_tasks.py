"""Consumer ownership, serve activation, and preserved bootstrap dormancy."""

import asyncio
import gc

import pytest

from tempo import __main__ as cli, triage
from tempo.artifacts import StageResult
from tempo.daemon import DaemonTasks
from tempo.journal import EventType
from tempo.mergequeue import ConflictHandoff, MergeQueue
from tempo.runner import Checkout
from tests.test_cli import ENV, Clock, Stages, root, ticket, write
from tests.test_daemon_composition import CoreRig, assert_core_wiring
from tests.test_mergequeue import ctx as admission_context, ready
from tests.test_scheduler import turn


class Consumers:
    def __init__(self):
        self.entered = [asyncio.Event() for _ in range(3)]
        self.release = [asyncio.Event() for _ in range(3)]
        self.cleaning = [asyncio.Event() for _ in range(3)]
        self.cleaned = []
        self.cleanup_release = asyncio.Event()
        self.calls = [0, 0, 0]
        self.cleanup_errors = {}

    def callback(self, index):
        async def consume():
            self.calls[index] += 1
            assert asyncio.current_task() is self.owner.tasks[index]
            self.entered[index].set()
            try:
                await self.release[index].wait()
                return object()
            finally:
                self.cleaning[index].set()
                await self.cleanup_release.wait()
                self.cleaned.append(index)
                if index in self.cleanup_errors:
                    raise self.cleanup_errors[index]
        return consume

    def owner_for(self, first=None):
        self.owner = DaemonTasks(watcher=first or self.callback(0),
                                 merge_queue=self.callback(1), box_consumer=self.callback(2))
        return self.owner

    async def all_entered(self):
        await asyncio.gather(*(event.wait() for event in self.entered))


def assert_finished(owner, tasks):
    assert owner.tasks == ()
    assert all(task.done() and task not in asyncio.all_tasks() for task in tasks)


@pytest.mark.asyncio
async def test_construction_is_idle():
    def forbidden():
        raise AssertionError("callback or awaitable created during construction")

    before = asyncio.all_tasks()
    owner = DaemonTasks(watcher=forbidden, merge_queue=forbidden, box_consumer=forbidden)
    assert owner.tasks == () and asyncio.all_tasks() == before


@pytest.mark.asyncio
async def test_run_owns_three_concurrent_consumers():
    consumers = Consumers()
    owner = consumers.owner_for()
    run = asyncio.create_task(owner.run())
    await consumers.all_entered()
    tasks = owner.tasks
    assert len(tasks) == len(set(tasks)) == 3 and run not in tasks
    assert consumers.calls == [1, 1, 1]
    consumers.cleanup_release.set()
    for index in range(3):
        consumers.release[index].set()
        await consumers.cleaning[index].wait()
        await turn()
        if index < 2:
            assert not run.done() and owner.tasks == tasks
            assert not any(task.cancelling() for task in tasks)
    assert await run is None
    assert consumers.cleaned == [0, 1, 2]
    assert_finished(owner, tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["sync", "async", "cancel"])
async def test_consumer_failure_cancels_and_awaits_siblings(failure):
    consumers = Consumers()
    error = asyncio.CancelledError("consumer cancelled") if failure == "cancel" else ValueError("consumer failed")
    fail = asyncio.Event()

    def synchronous():
        consumers.calls[0] += 1
        consumers.entered[0].set()
        raise error

    async def asynchronous():
        consumers.calls[0] += 1
        consumers.entered[0].set()
        await fail.wait()
        raise error

    owner = consumers.owner_for(synchronous if failure == "sync" else asynchronous)
    consumers.cleanup_errors[2] = RuntimeError("sibling cleanup failed")
    loop = asyncio.get_running_loop()
    unobserved = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _, context: unobserved.append(context))
    try:
        run = asyncio.create_task(owner.run())
        await consumers.all_entered()
        tasks = owner.tasks
        fail.set()
        await asyncio.gather(*(event.wait() for event in consumers.cleaning[1:]))
        assert not run.done() and owner.tasks == tasks and consumers.cleaned == []
        assert consumers.calls == [1, 1, 1]
        # A cleanup exception must also be observed, without replacing the original failure.
        consumers.cleanup_release.set()
        with pytest.raises(type(error)) as caught:
            await run
        if failure != "cancel":
            assert caught.value is error
        assert_finished(owner, tasks)
        error.__traceback__ = None
        del caught
        del tasks, run
        gc.collect()
        await turn()
        assert unobserved == []
    finally:
        loop.set_exception_handler(previous)


@pytest.mark.asyncio
async def test_run_cancellation_awaits_all_cleanup():
    consumers = Consumers()
    owner = consumers.owner_for()
    run = asyncio.create_task(owner.run())
    await consumers.all_entered()
    tasks = owner.tasks
    run.cancel()
    await asyncio.gather(*(event.wait() for event in consumers.cleaning))
    for _ in range(3):
        run.cancel()
        await turn()
        assert not run.done() and owner.tasks == tasks
        assert not any(task.done() for task in tasks) and consumers.cleaned == []
    consumers.cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert sorted(consumers.cleaned) == [0, 1, 2]
    assert all(task.cancelled() for task in tasks)
    assert_finished(owner, tasks)


@pytest.mark.asyncio
async def test_watcher_debounce_tasks_do_not_outlive_owner(tmp_path):
    rig = CoreRig(tmp_path)
    sleeping, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def sleep(_):
        sleeping.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    rig.core.watcher.sleep = sleep

    async def changes():
        yield "work"
        await asyncio.Event().wait()

    async def wait():
        await asyncio.Event().wait()

    owner = DaemonTasks(watcher=lambda: rig.core.watcher.run(changes()),
                        merge_queue=wait, box_consumer=wait)
    run = asyncio.create_task(owner.run())
    await sleeping.wait()
    tasks = owner.tasks
    debounce = tuple(rig.core.watcher._waits.values())
    assert len(debounce) == 1
    run.cancel()
    await cleaning.wait()
    run.cancel()
    await turn()
    assert not run.done() and not debounce[0].done() and owner.tasks == tasks
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert debounce[0].cancelled() and debounce[0] not in asyncio.all_tasks()
    assert rig.core.watcher._waits == {} and rig.journal.read() == []
    assert_finished(owner, tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["success", "failure", "cancellation"])
async def test_overlapping_run_is_refused_until_cleanup_finishes(ending):
    consumers = Consumers()
    fail, started, cleaning = asyncio.Event(), asyncio.Event(), asyncio.Event()
    first_run = True

    async def first():
        consumers.calls[0] += 1
        started.set()
        if first_run:
            try:
                await fail.wait()
                if ending == "failure":
                    raise ValueError("failed first run")
            finally:
                cleaning.set()
                if ending != "failure":
                    await consumers.cleanup_release.wait()

    owner = consumers.owner_for(first)
    run = asyncio.create_task(owner.run())
    await started.wait()
    await asyncio.gather(*(event.wait() for event in consumers.entered[1:]))
    tasks = owner.tasks
    with pytest.raises(ValueError, match="finish or cancel and await the existing"):
        await owner.run()
    if ending == "cancellation":
        run.cancel()
    else:
        fail.set()
        if ending == "success":
            for event in consumers.release:
                event.set()
    await cleaning.wait()
    with pytest.raises(ValueError, match="finish or cancel and await the existing"):
        await owner.run()
    assert consumers.calls == [1, 1, 1] and owner.tasks == tasks
    if ending != "success":
        await asyncio.gather(*(event.wait() for event in consumers.cleaning[1:]))
    consumers.cleanup_release.set()
    if ending == "success":
        await run
    else:
        with pytest.raises(asyncio.CancelledError if ending == "cancellation" else ValueError):
            await run
    assert_finished(owner, tasks)
    first_run = False
    for event in consumers.release:
        event.set()
    assert await owner.run() is None
    assert consumers.calls == [2, 2, 2] and owner.tasks == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["ok", "gate_failed", "handoff"])
async def test_merge_results_remain_consumer_owned(admission_context, monkeypatch, outcome):
    ctx = admission_context
    work = await ready(ctx)
    queue = MergeQueue(ctx, escalate=lambda _: pytest.fail("unexpected escalation"))
    queue.offer(work, attempt=0)
    expected = None
    if outcome != "ok":
        expected = (ConflictHandoff(stem=work.stem, reviewed_sha="reviewed", conflicted_paths=[], findings=[])
                    if outcome == "handoff" else
                    StageResult(outcome="gate_failed", artifact=None, findings=[]))

        async def admit(original, attempt):
            assert original is work and attempt == 0 and queue.active == work.stem
            return expected

        monkeypatch.setattr(queue, "_admit", admit)
    box_done = asyncio.Event()
    received, handled, box_results, history = [], [], [], []

    async def merge_consumer():
        results = await queue.process()
        received.append(results)
        handled.extend(results)
        await box_done.wait()
        history.extend(ctx.driver.journal.read())
        return results

    async def box_consumer():
        checkout = Checkout(ctx.repo, ctx.config, ctx.env, ctx.exec_, ctx.git,
                            ctx.driver.journal, ctx.fs, ctx.driver.clock, ctx.driver.sleep)
        box_results.append(await triage.triage_pass(checkout, ctx.driver.llm))
        box_done.set()
        return box_results[0]

    async def watcher():
        return "consumer-owned value"

    owner = DaemonTasks(watcher=watcher, merge_queue=merge_consumer, box_consumer=box_consumer)
    assert await owner.run() is None
    assert len(received) == len(handled) == 1 and handled[0] is received[0][0]
    if outcome == "ok":
        assert handled[0].outcome == "ok" and handled[0].artifact is not None
    else:
        assert handled[0] is expected
    assert box_results == [[]] and ctx.driver.llm.requests == []
    assert queue.active is None and not queue._slot.locked() and not queue.pending
    assert ctx.driver.journal.read() == history
    assert sum(e.body.get("signal") == "triage_pass" for e in history) == 1
    assert owner.tasks == ()


@pytest.mark.parametrize("verb", ["run", "drain"])
def test_background_consumers_are_dormant(root, monkeypatch, verb):
    async def noop():
        pass

    dormant = DaemonTasks(watcher=noop, merge_queue=noop, box_consumer=noop)

    async def run_probe(self):
        raise AssertionError("background run wired")

    async def triage_probe(*args, **kwargs):
        raise AssertionError("box consumption wired")

    monkeypatch.setattr(DaemonTasks, "run", run_probe)
    monkeypatch.setattr(triage, "triage_pass", triage_probe)
    write(root, "work", ticket())
    argv = ["run", "work"] if verb == "run" else ["drain"]

    def deliberately_run(checkout):
        async def dispatch(_):
            await dormant.run()
        return dispatch

    def deliberately_triage(checkout):
        async def dispatch(_):
            await triage.triage_pass(checkout, None)
        return dispatch

    for pipeline, message in [(deliberately_run, "background run wired"),
                              (deliberately_triage, "box consumption wired")]:
        with pytest.raises(AssertionError, match=message):
            cli.main(argv, cwd=root, env=ENV, clock=Clock(), pipeline=pipeline)

    def construction_probe(self, **kwargs):
        raise AssertionError("background construction wired")

    monkeypatch.setattr(DaemonTasks, "__init__", construction_probe)

    def deliberately_construct(checkout):
        DaemonTasks(watcher=noop, merge_queue=noop, box_consumer=noop)

    with pytest.raises(AssertionError, match="background construction wired"):
        cli.main(argv, cwd=root, env=ENV, clock=Clock(), pipeline=deliberately_construct)

    async def graph():
        before = asyncio.all_tasks()
        rig = CoreRig(root)
        assert_core_wiring(rig)
        assert asyncio.all_tasks() == before and rig.journal.read() == []
        assert dormant.tasks == () and rig.exec.calls == []

    asyncio.run(graph())
    stages = Stages()
    assert cli.main(argv, cwd=root, env=ENV, clock=Clock(), pipeline=stages) == 0
    assert stages.calls == ["work"] and stages.lock_held == [True]
    assert not any(e.body.get("signal") == "triage_pass" for e in stages.checkout.journal.read())
    assert any(e.type == EventType.STATE_TRANSITION and e.body.get("to") == "merged"
               for e in stages.checkout.journal.read())


@pytest.mark.asyncio
async def test_background_consumers_are_active_in_serve(root, monkeypatch):
    from tests.test_serve import activated_graph
    rig = await activated_graph(root, monkeypatch)
    assert len(rig.owner.workers) == 4 and rig.owner.tasks.tasks == ()
