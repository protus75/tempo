"""Executor unwind ordering and its activation through the production drain."""

import asyncio
from dataclasses import replace

import pytest

from tempo.llm import FakeLLM, LLMAborted
from tests.test_driver import ECHO, Stub, build, echo_json, log_events, spool
from tests.test_cli import root
from tests.test_kill_signal_journal import Rig, decision
from tempo.control import executor_abort


async def turn():
    # An event-loop barrier, with no wall-clock sleep.
    future = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(future.set_result, None)
    await future


class Writer:
    kind = "cli"

    def __init__(self, *, held=False):
        self.started = asyncio.Event()
        self.unwinding = asyncio.Event()
        self.release = asyncio.Event()
        if not held:
            self.release.set()
        self.stopped = False
        self.calls = 0
        self.aborts = 0
        self.cancels = 0
        self.mutations = []

    async def call(self, req):
        self.calls += 1
        self.mutations.append("write")
        self.started.set()
        while not self.stopped:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancels += 1
                if not self.stopped:
                    self.mutations.append("write after cancellation")
        self.unwinding.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            pytest.fail("executor cleanup was cancelled twice")
        raise LLMAborted("writer stopped")

    def abort_current(self):
        if hasattr(self, "driver"):
            owned = self.driver._active
            assert owned.task.cancelling() == 0, "stage cancelled before writer stop"
            assert owned.race.call.cancelling() == 0, "call cancelled before writer stop"
            assert owned.race.timer.cancelling() == 0, "timer cancelled before writer stop"
        self.aborts += 1
        self.stopped = True


class Timer:
    def __init__(self):
        self.started = asyncio.Event()
        self.observed = False
        self.task = None
        self.failure = None

    async def __call__(self, remaining):
        self.task = asyncio.current_task()
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.observed = True
            if self.failure is not None:
                raise self.failure


def invoke(driver, stage=ECHO, *, attempt=1):
    return driver.run(stage, Stub(text="hi"), ticket="t-echo", attempt=attempt,
                      workspace=driver.spool.root.parent, tier="medium", effort="low",
                      stuck_budget=600)


async def active(tmp_path, *, held=False):
    writer = Writer(held=held)
    driver, state = build(tmp_path, writer)
    writer.driver = driver
    timer = Timer()
    driver.sleep = timer
    task = asyncio.create_task(invoke(driver))
    await writer.started.wait()
    await timer.started.wait()
    return driver, state, writer, timer, task, driver._active.race


async def cancelled(task):
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_executor_abort_stops_writer_before_cancellation(tmp_path):
    driver, state, writer, timer, task, race = await active(tmp_path)
    control = Rig(state)
    control.publish("kill")
    control.inbox.consume()
    assert control.projection.kill_requested
    before = driver.journal.read()
    await executor_abort(driver.abort_current)
    await cancelled(task)
    assert writer.aborts == writer.cancels == 1
    assert writer.mutations == ["write"]
    snapshot = list(writer.mutations)
    await turn()
    assert writer.mutations == snapshot
    assert timer.observed and timer.task.done()
    assert race.call.done() and race.timer.done() and race.cleanup.done()
    assert not race.call._log_traceback and not race.timer._log_traceback
    assert driver._active is None and driver.journal.read() == before
    assert before[0].key == "llm/t-echo/0/implement/1/1"
    assert before[-1].body == decision("kill")
    assert not (spool(state) / "implement/call-01/output.txt").exists()


@pytest.mark.asyncio
async def test_executor_abort_waits_for_unwind(tmp_path):
    driver, _, writer, timer, task, race = await active(tmp_path, held=True)
    owned = driver._active
    abort = asyncio.create_task(executor_abort(driver.abort_current))
    await writer.unwinding.wait()
    await turn()
    assert not abort.done() and not task.done()
    assert driver._active is owned and owned.race is race
    assert not race.call.done() and race.cleanup is not None
    assert timer.observed
    writer.release.set()
    await abort
    await cancelled(task)
    assert driver._active is None


@pytest.mark.asyncio
async def test_executor_abort_is_atomic_under_repeated_cancellation(tmp_path):
    driver, _, writer, _, task, race = await active(tmp_path, held=True)
    first = asyncio.create_task(executor_abort(driver.abort_current))
    await writer.unwinding.wait()
    owned = driver._active
    second = asyncio.create_task(driver.abort_current())
    await turn()
    cleanup = owned.abort
    for _ in range(3):
        first.cancel()
        await turn()
        assert not first.done() and driver._active is owned
        assert owned.abort is cleanup and not cleanup.cancelled()
    assert writer.aborts == writer.cancels == 1 and not second.done()
    writer.release.set()
    await cancelled(first)
    await second
    await cancelled(task)
    assert race.cleanup.done() and driver._active is None
    await driver.abort_current()
    assert writer.aborts == 1
    driver.llm = FakeLLM([echo_json("later")])
    assert (await invoke(driver, attempt=2)).outcome == "ok"
    assert writer.aborts == 1


@pytest.mark.asyncio
async def test_executor_abort_idle_and_completion_race(tmp_path):
    driver, _ = build(tmp_path, FakeLLM([echo_json("first"), echo_json("second")]))
    await executor_abort(driver.abort_current)
    assert driver.llm.aborted == 0
    assert (await invoke(driver)).outcome == "ok"
    await driver.abort_current()
    assert driver.llm.aborted == 0 and driver._active is None
    # Complete the call and request abort before the stage consumes its outcome.
    ready, complete = asyncio.Event(), asyncio.Event()
    async def review(artifact, seq):
        ready.set()
        await complete.wait()
        raise LLMAborted("completed review race")
    task = asyncio.create_task(invoke(driver, replace(ECHO, review=review), attempt=2))
    await ready.wait()
    race = driver._active.race
    complete.set()
    await turn()
    assert race.call.done()
    await driver.abort_current()
    try:
        result = await task
    except asyncio.CancelledError:
        pass
    else:
        assert result.outcome == "infra_error"
    assert race.call.done() and race.timer.done()
    assert not race.call._log_traceback and not race.timer._log_traceback
    assert driver._active is None
    driver.llm = FakeLLM([echo_json("third")])
    assert (await invoke(driver, attempt=3)).outcome == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("review_wait", [False, True])
async def test_external_abort_does_not_reprompt_or_report_success(tmp_path, review_wait):
    if review_wait:
        driver, state = build(tmp_path, FakeLLM([echo_json("hi")]))
        ready = asyncio.Event()
        async def review(artifact, seq):
            ready.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(invoke(driver, replace(ECHO, review=review)))
        await ready.wait()
    else:
        driver, state, _, _, task, _ = await active(tmp_path)
    before = driver.journal.read()
    await executor_abort(driver.abort_current)
    await cancelled(task)
    assert driver.journal.read() == before
    assert [e.type for e in before] == (["effect_intent", "effect_completion"] if review_wait
                                      else ["effect_intent"])
    assert not any(e["event"] in {"stage_end", "llm_error", "review_error", "gate_failed"}
                   for e in log_events(state))
    assert not (spool(state) / "implement/call-02").exists()
    assert driver._active is None


@pytest.mark.asyncio
async def test_executor_abort_failure_propagates(tmp_path):
    driver, _, writer, _, task, _ = await active(tmp_path)
    stop = writer.abort_current
    def refused():
        raise RuntimeError("cannot stop writer")
    writer.abort_current = refused
    with pytest.raises(RuntimeError, match="cannot stop writer"):
        await executor_abort(driver.abort_current)
    assert not task.done() and writer.cancels == 0 and driver._active is not None
    # Release the failed-stop fixture without claiming that its abort succeeded.
    writer.abort_current = stop
    driver._active.abort = None
    await driver.abort_current()
    await cancelled(task)


@pytest.mark.asyncio
async def test_executor_abort_owns_stage_not_calling_worker(tmp_path):
    writer = Writer()
    driver, _ = build(tmp_path, writer)
    caught, release_worker = asyncio.Event(), asyncio.Event()
    async def worker():
        try:
            await invoke(driver)
        except asyncio.CancelledError:
            caught.set()
        await release_worker.wait()
    worker_task = asyncio.create_task(worker())
    await writer.started.wait()
    owned = driver._active
    assert owned.task is not worker_task
    await driver.abort_current()
    assert owned.task.done() and not worker_task.done()
    # A later invocation can start before the previous caller finishes observing cancellation.
    driver.llm = FakeLLM([echo_json("later")])
    later = asyncio.create_task(invoke(driver, attempt=2))
    await caught.wait()
    assert (await later).outcome == "ok"
    release_worker.set()
    await worker_task
    assert writer.aborts == 1 and driver._active is None


@pytest.mark.asyncio
async def test_executor_abort_observes_unwind_failure(tmp_path):
    driver, _, writer, timer, task, race = await active(tmp_path)
    timer.failure = RuntimeError("timer unwind failed")
    with pytest.raises(RuntimeError, match="timer unwind failed"):
        await driver.abort_current()
    await cancelled(task)
    assert race.call.done() and race.timer.done() and race.cleanup.done()
    assert not race.timer._log_traceback and not race.cleanup._log_traceback
    assert writer.aborts == 1 and driver._active is None


@pytest.mark.asyncio
async def test_stage_cancellation_keeps_external_abort_dormant(tmp_path, monkeypatch):
    driver, _, writer, timer, task, race = await active(tmp_path)
    async def probe():
        raise AssertionError("external abort invoked by ordinary cancellation")
    monkeypatch.setattr(driver, "abort_current", probe)
    task.cancel()
    await cancelled(task)
    assert writer.aborts == 1 and timer.observed
    assert race.call.done() and race.timer.done() and driver._active is None
