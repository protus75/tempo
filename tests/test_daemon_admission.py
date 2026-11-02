"""Ownership, production core reachability, and bootstrap CLI preservation evidence."""

import ast
import asyncio
from pathlib import Path

import pytest

from tempo.__main__ import main
from tempo.daemon import DaemonAdmission
from tempo.journal import EventType
from tempo.tickets import parse_ticket
from tests.test_cli import ENV, PLAN, Clock, root, ticket, write, with_retro_driver


@pytest.fixture
def tickets(tmp_path):
    (tmp_path / "tempo").mkdir()
    (tmp_path / "tempo/thing.py").write_text("")
    text = ticket().replace("priority: P2", "state: confirmed\nsource: human\npriority: P2", 1)
    return tuple(parse_ticket(stem, text, tmp_path, plan=PLAN) for stem in ("first", "second"))


async def turn():
    future = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(future.set_result, None)
    await future


def assert_idle(admission):
    assert admission.active is None
    assert admission.task is None


@pytest.mark.asyncio
async def test_admission_is_idle_until_dispatch():
    calls = []

    async def dispatch(ticket):
        calls.append(ticket)
        return "merged"

    tasks = asyncio.all_tasks()
    admission = DaemonAdmission(dispatch)
    assert_idle(admission)
    assert calls == []
    assert asyncio.all_tasks() == tasks


@pytest.mark.asyncio
async def test_dispatch_owns_task_and_returns_terminal(tickets):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    terminal = "callback-owned-terminal"

    async def dispatch(ticket):
        calls.append(ticket)
        assert admission.active is ticket
        assert admission.task is asyncio.current_task()
        started.set()
        await release.wait()
        return terminal

    admission = DaemonAdmission(dispatch)
    caller = asyncio.create_task(admission.dispatch(tickets[0]))
    await started.wait()
    owned = admission.task
    assert isinstance(owned, asyncio.Task) and owned is not caller
    assert not owned.done() and not caller.done()
    release.set()
    assert await caller is terminal
    assert calls == [tickets[0]] and calls[0] is tickets[0]
    assert owned.done()
    assert_idle(admission)


@pytest.mark.asyncio
async def test_concurrent_dispatch_is_single_flight(tickets):
    started, release = asyncio.Event(), asyncio.Event()
    calls, finished = [], []

    async def dispatch(ticket):
        assert len(calls) == len(finished)
        calls.append(ticket)
        if ticket is tickets[0]:
            started.set()
            await release.wait()
        finished.append(ticket)
        return ticket.stem

    admission = DaemonAdmission(dispatch)
    first = asyncio.create_task(admission.dispatch(tickets[0]))
    await started.wait()
    owned = admission.task
    second = asyncio.create_task(admission.dispatch(tickets[1]))
    await turn()
    assert calls == [tickets[0]] and finished == []
    assert admission.active is tickets[0] and admission.task is owned
    assert not first.done() and not second.done() and not owned.cancelling()
    release.set()
    assert await asyncio.gather(first, second) == ["first", "second"]
    assert calls == finished == list(tickets)
    assert_idle(admission)


@pytest.mark.asyncio
async def test_dispatch_exception_unwinds_and_releases_slot(tickets):
    cleaning, release = asyncio.Event(), asyncio.Event()
    error = ValueError("original failure")
    calls, finished = [], []

    async def dispatch(ticket):
        calls.append(ticket)
        if ticket is tickets[0]:
            try:
                raise error
            finally:
                cleaning.set()
                await release.wait()
                finished.append(ticket)
        assert finished == [tickets[0]]
        return "merged"

    admission = DaemonAdmission(dispatch)
    first = asyncio.create_task(admission.dispatch(tickets[0]))
    await cleaning.wait()
    owned = admission.task
    second = asyncio.create_task(admission.dispatch(tickets[1]))
    await turn()
    assert calls == [tickets[0]] and not first.done() and not second.done()
    release.set()
    with pytest.raises(ValueError) as caught:
        await first
    assert caught.value is error and owned.done()
    assert await second == "merged"
    assert calls == list(tickets)
    assert_idle(admission)


@pytest.mark.asyncio
async def test_cancelled_waiter_never_dispatches(tickets):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def dispatch(ticket):
        calls.append(ticket)
        started.set()
        await release.wait()
        return "merged"

    admission = DaemonAdmission(dispatch)
    first = asyncio.create_task(admission.dispatch(tickets[0]))
    await started.wait()
    owned = admission.task
    waiter = asyncio.create_task(admission.dispatch(tickets[1]))
    await turn()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert calls == [tickets[0]]
    assert admission.active is tickets[0] and admission.task is owned
    assert not owned.done() and not owned.cancelling() and not first.done()
    release.set()
    assert await first == "merged"
    assert_idle(admission)
    assert await admission.dispatch(tickets[1]) == "merged"
    assert calls == list(tickets)
    assert_idle(admission)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_again", [False, True])
async def test_active_cancellation_awaits_cleanup_before_release(tickets, cancel_again):
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls, finished = [], []

    async def dispatch(ticket):
        calls.append(ticket)
        if ticket is tickets[0]:
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                finished.append(ticket)
        assert finished == [tickets[0]]
        return "merged"

    admission = DaemonAdmission(dispatch)
    first = asyncio.create_task(admission.dispatch(tickets[0]))
    await started.wait()
    owned = admission.task
    first.cancel()
    await cleaning.wait()
    second = asyncio.create_task(admission.dispatch(tickets[1]))
    if cancel_again:
        first.cancel()
    await turn()
    assert calls == [tickets[0]] and finished == []
    assert not first.done() and not second.done() and not owned.done()
    assert admission.active is tickets[0] and admission.task is owned
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert owned.done() and owned.cancelled() and owned not in asyncio.all_tasks()
    assert await second == "merged"
    assert calls == list(tickets) and finished == [tickets[0]]
    assert_idle(admission)


def import_closure(sources):
    pending, reached = ["tempo.__main__", "tempo"], set()
    while pending:
        module = pending.pop()
        if module in reached or module not in sources:
            continue
        reached.add(module)
        for node in ast.walk(ast.parse(sources[module])):
            if isinstance(node, ast.Import):
                pending.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                pending.append(node.module)
                pending.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return reached


def assert_reachable(sources):
    assert "tempo.daemon" in import_closure(sources)


@pytest.mark.parametrize("verb", ["run", "drain"])
def test_daemon_admission_is_dormant(root, monkeypatch, verb):
    def pipeline(checkout):
        async def dispatch(ticket):
            calls.append(ticket.stem)
            checkout.journal.append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket=ticket.stem)
            return "merged"
        return with_retro_driver(checkout, dispatch)

    async def probe(self, ticket):
        raise AssertionError("daemon dispatch wired into CLI")

    monkeypatch.setattr(DaemonAdmission, "dispatch", probe)
    # Calibrate the probe through the production root with deliberate test-only wiring.
    def wired(checkout):
        return DaemonAdmission(pipeline(checkout)).dispatch

    calls = []
    write(root, "work", ticket())
    argv = ["run", "work"] if verb == "run" else ["drain"]
    with pytest.raises(AssertionError, match="daemon dispatch wired"):
        main(argv, cwd=root, env=ENV, clock=Clock(), pipeline=wired)
    assert calls == []

    def construction_probe(self, dispatch):
        raise AssertionError("daemon constructed by CLI")

    monkeypatch.setattr(DaemonAdmission, "__init__", construction_probe)
    assert main(argv, cwd=root, env=ENV, clock=Clock(), pipeline=pipeline) == 0
    assert calls == ["work"]

    repo = Path(__file__).resolve().parents[1]
    sources = {".".join(path.relative_to(repo).with_suffix("").parts): path.read_text()
               for path in (repo / "tempo").rglob("*.py")}
    sources["tempo"] = sources.pop("tempo.__init__")
    from tests.test_daemon_composition import CoreRig, assert_core_wiring, without_core_import

    assert_reachable(sources)
    removed = without_core_import(sources)
    with pytest.raises(AssertionError):
        assert_reachable(removed)
    for statement in ("import tempo.daemon", "from tempo import daemon"):
        changed = dict(removed)
        changed["tempo.status"] += f"\n{statement}\n"
        assert_reachable(changed)
    monkeypatch.undo()
    rig = CoreRig(root)
    assert_core_wiring(rig)
    assert_idle(rig.core.admission)
