"""Explicit pause checkpoints and their active production composition."""

import asyncio

import pytest

from tempo import control, daemon
from tempo.__main__ import main
from tempo.config import load_config
from tempo.daemon import DaemonAdmission, snapshot_dispatch
from tests.test_cli import CONFIG, ENV, Clock, root, ticket, write
from tests.test_control import Rig
from tests.test_daemon_admission import assert_idle, tickets, turn
from tests.test_daemon_composition import CoreRig, assert_core_wiring, assert_startup_pause_wiring
from tests.support import journal


@pytest.mark.asyncio
async def test_pause_checkpoint_precedes_dispatch_and_snapshot(tickets, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    checkpoints, loads, binds, calls = [], [], [], []
    (tmp_path / "config.yaml").write_text(CONFIG)
    config = load_config(None, cwd=tmp_path)

    async def checkpoint():
        checkpoints.append(asyncio.current_task())
        entered.set()
        await release.wait()

    def load():
        loads.append(config)
        return config

    def bind(snapshot):
        binds.append(snapshot)
        async def dispatch(ticket):
            calls.append(ticket)
            return "original-terminal"
        return dispatch

    admission = DaemonAdmission(snapshot_dispatch(load, bind), before_dispatch=checkpoint)
    first = asyncio.create_task(admission.dispatch(tickets[0]))
    await entered.wait()
    second = asyncio.create_task(admission.dispatch(tickets[1]))
    await turn()
    assert checkpoints == [first] and loads == binds == calls == []
    assert_idle(admission)
    release.set()
    assert await asyncio.gather(first, second) == ["original-terminal"] * 2
    assert calls == list(tickets) and calls[0] is tickets[0]
    assert len(loads) == len(binds) == 2
    assert_idle(admission)


@pytest.mark.asyncio
async def test_pause_does_not_preempt_active_dispatch(tickets):
    active, finish, held, resume = (asyncio.Event() for _ in range(4))
    paused = False
    calls = []

    async def checkpoint():
        if paused:
            held.set()
            await resume.wait()

    async def dispatch(ticket):
        calls.append(ticket)
        if ticket is tickets[0]:
            active.set()
            await finish.wait()
        return "merged"

    admission = DaemonAdmission(dispatch, before_dispatch=checkpoint)
    first = asyncio.create_task(admission.dispatch(tickets[0]))
    await active.wait()
    owned = admission.task
    paused = True
    second = asyncio.create_task(admission.dispatch(tickets[1]))
    finish.set()
    assert await first == "merged" and not owned.cancelled()
    await held.wait()
    assert calls == [tickets[0]] and not second.done()
    assert_idle(admission)
    resume.set()
    assert await second == "merged" and calls == list(tickets)


@pytest.mark.asyncio
async def test_only_matching_resume_releases_pause(tickets, tmp_path):
    rig = Rig(tmp_path)
    wake, consumed = asyncio.Event(), asyncio.Queue()
    calls = []

    async def checkpoint():
        while True:
            wake.clear()
            rig.inbox.consume()
            await consumed.put(rig.inbox.recover())
            if rig.projection.pause_id is None:
                return
            await wake.wait()

    async def dispatch(ticket):
        assert rig.decisions()[-1]["decision"] == "accepted"
        assert rig.decisions()[-1]["verb"] == "resume"
        calls.append(ticket)
        return "merged"

    rig.publish("00-premature", "resume", "10-pause")
    rig.publish("10-pause")
    admission = DaemonAdmission(dispatch, before_dispatch=checkpoint)
    task = asyncio.create_task(admission.dispatch(tickets[0]))
    assert (await consumed.get()).pause_id == "10-pause"
    for id, hold, lifecycle in (("20-wrong", "wrong", None),
                                ("30-old-life", "10-pause", "previous")):
        rig.publish(id, "resume", hold, lifecycle)
        wake.set()
        assert (await consumed.get()).pause_id == "10-pause"
        assert calls == [] and not task.done()
    rig.publish("40-resume", "resume", "10-pause")
    rig.publish("50-pause")
    wake.set()
    assert (await consumed.get()).pause_id == "50-pause"
    rig.publish("60-old-hold", "resume", "10-pause")
    wake.set()
    assert (await consumed.get()).pause_id == "50-pause"
    assert calls == []
    rig.publish("70-matching", "resume", "50-pause")
    wake.set()
    assert (await consumed.get()).pause_id is None
    assert await task == "merged" and calls == [tickets[0]]
    assert [d["decision"] for d in rig.decisions()] == [
        "stale", "accepted", "stale", "stale", "accepted", "accepted", "stale", "accepted"]
    assert rig.reconstruct().recover().pause_id is None


@pytest.mark.asyncio
async def test_checkpoint_failure_and_cancellation_leave_no_dispatch(tickets):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    failure = None

    async def checkpoint():
        entered.set()
        await release.wait()
        if failure is not None:
            raise failure

    async def dispatch(ticket):
        calls.append(ticket)
        return "merged"

    admission = DaemonAdmission(dispatch, before_dispatch=checkpoint)
    blocked = asyncio.create_task(admission.dispatch(tickets[0]))
    await entered.wait()
    queued = asyncio.create_task(admission.dispatch(tickets[1]))
    await turn()
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    blocked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked
    assert calls == [] and not admission._slot.locked()
    assert_idle(admission)
    failure = ValueError("checkpoint failed")
    release.set()
    with pytest.raises(ValueError, match="checkpoint failed"):
        await admission.dispatch(tickets[0])
    assert_idle(admission)
    assert calls == [] and not admission._slot.locked()
    failure = None
    assert await admission.dispatch(tickets[1]) == "merged"
    assert calls == [tickets[1]]


