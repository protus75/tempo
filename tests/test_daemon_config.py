"""Per-admission capture, production core reachability, and bootstrap preservation."""

import asyncio
from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import BaseModel

from tempo import config as config_module, daemon
from tempo.__main__ import main
from tempo.config import ConfigError, ConfigSnapshot, load_config, snapshot_config
from tempo.daemon import DaemonAdmission, snapshot_dispatch
from tempo.journal import EventType
from tempo.seams import LocalFileSystem
from tests.test_cli import ENV, Clock, root, ticket, write, with_retro_driver
from tests.test_config import VALID
from tests.test_daemon_admission import assert_reachable, assert_idle, tickets, turn


def put_config(directory, text=VALID):
    LocalFileSystem().write(directory / "config.yaml", text.encode())


def python_values(value):
    if isinstance(value, ConfigSnapshot):
        return {key: python_values(getattr(value, key)) for key in value._fields}
    if isinstance(value, Mapping):
        return {key: python_values(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [python_values(item) for item in value]
    return value


def assert_detached(source, snapshot):
    if isinstance(source, BaseModel):
        assert isinstance(snapshot, ConfigSnapshot) and source is not snapshot
        assert set(snapshot._fields) == set(type(source).model_fields)
        for key in type(source).model_fields:
            assert_detached(getattr(source, key), getattr(snapshot, key))
    elif isinstance(source, dict):
        assert isinstance(snapshot, Mapping) and source is not snapshot
        for key, item in source.items():
            assert_detached(item, snapshot[key])
    elif isinstance(source, list):
        assert isinstance(snapshot, tuple) and source is not snapshot
        for item, captured in zip(source, snapshot, strict=True):
            assert_detached(item, captured)
    else:
        assert snapshot == source


@pytest.mark.parametrize("extras", ["", """worktree_root: elsewhere
report_inbox: inbox
notify: [notify-send, tempo]
context_files: [docs/first.md, docs/second.md]
scheduler: {max_unmerged: 5}
caps: {retry: 4}
box_policy: {failure_report: draft}
"""])
def test_snapshot_preserves_all_validated_config_values(tmp_path, extras):
    put_config(tmp_path, VALID + extras)
    source = load_config(None, cwd=tmp_path)
    snapshot = snapshot_config(source)
    assert python_values(snapshot) == source.model_dump(mode="python")
    assert_detached(source, snapshot)
    assert snapshot.state_dir == tmp_path / ".tempo"
    assert snapshot.worktree_root == tmp_path / ("elsewhere" if extras else ".tempo/worktrees")
    assert snapshot.hosts["claude"].quota_window_minutes == 60
    assert snapshot.hosts["claude"].pins["review"] == "opus"
    assert snapshot.report_inbox == (tmp_path / "inbox" if extras else None)
    assert snapshot.notify == (("notify-send", "tempo") if extras else None)
    assert snapshot.context_files == ((Path("docs/first.md"), Path("docs/second.md")) if extras else ())
    assert snapshot.merge.strategies[0].argv == ("uv", "lock")
    with pytest.raises(AttributeError):
        getattr(snapshot, "unknown_field")


def test_snapshot_is_recursively_immutable_and_detached(tmp_path):
    put_config(tmp_path)
    source = load_config(None, cwd=tmp_path)
    expected = source.model_dump(mode="python")
    snapshot = snapshot_config(source)
    assert_detached(source, snapshot)

    def refuse_mutation(value):
        if isinstance(value, ConfigSnapshot):
            for key in value._fields:
                with pytest.raises(AttributeError):
                    setattr(value, key, None)
                with pytest.raises(AttributeError):
                    delattr(value, key)
                refuse_mutation(getattr(value, key))
            with pytest.raises(TypeError):
                value._fields["new"] = "value"
        elif isinstance(value, Mapping):
            with pytest.raises(TypeError):
                value["new"] = "value"
            if value:
                with pytest.raises(TypeError):
                    del value[next(iter(value))]
            for item in value.values():
                refuse_mutation(item)
        elif isinstance(value, tuple):
            with pytest.raises(AttributeError):
                value.append("value")
            if value:
                with pytest.raises(TypeError):
                    value[0] = "value"
            for item in value:
                refuse_mutation(item)

    refuse_mutation(snapshot)
    source.state_dir = Path("changed")
    source.hosts["claude"].models_by_tier.medium = "changed"
    source.hosts["claude"].pins["review"] = "changed"
    source.scheduler.max_unmerged = 8
    source.review.mechanical[0].argv.append("changed")
    source.review.mechanical[1].trigger.append("changed/")
    source.review.trigger_map["tempo/"].append("changed")
    del source.review.gate_severity["scope_fence"]
    source.box_policy["suggestion"] = "confirmed"
    source.merge.strategies.clear()
    source.hosts.clear()
    source.context_files.append(Path("changed"))
    assert python_values(snapshot) == expected


@pytest.mark.asyncio
async def test_dispatch_captures_once_and_keeps_snapshot_until_completion(tmp_path, tickets, monkeypatch):
    put_config(tmp_path)
    source = load_config(None, cwd=tmp_path)
    expected = source.model_dump(mode="python")
    calls, bound = [], []
    started, release = asyncio.Event(), asyncio.Event()
    terminal = "callback-owned-terminal"

    def load():
        calls.append("load")
        return source

    def capture(value):
        assert value is source
        calls.append("capture")
        return snapshot_config(value)

    def bind(snapshot):
        calls.append("bind")
        bound.append(snapshot)

        async def callback(ticket):
            calls.append("callback")
            assert ticket is tickets[0]
            assert python_values(snapshot) == expected
            started.set()
            await release.wait()
            assert snapshot is bound[0]
            assert python_values(snapshot) == expected
            return terminal
        return callback

    monkeypatch.setattr(daemon, "snapshot_config", capture)
    adapted = snapshot_dispatch(load, bind)
    assert calls == []
    caller = asyncio.create_task(adapted(tickets[0]))
    await started.wait()
    source.scheduler.max_unmerged = 99
    source.review.trigger_map["tempo/"].clear()
    source.hosts["claude"].pins.clear()
    put_config(tmp_path, VALID + "scheduler: {max_unmerged: 7}\n")
    release.set()
    assert await caller is terminal
    assert calls == ["load", "capture", "bind", "callback"]


@pytest.mark.asyncio
async def test_next_dispatch_observes_valid_config_edits(tmp_path, tickets):
    put_config(tmp_path)
    snapshots = []

    def bind(snapshot):
        snapshots.append(snapshot)

        async def callback(ticket):
            return "merged"
        return callback

    admission = DaemonAdmission(snapshot_dispatch(lambda: load_config(None, cwd=tmp_path), bind))
    assert await admission.dispatch(tickets[0]) == "merged"
    changed = VALID.replace("pins: {review: opus}", "pins: {review: haiku-max}").replace(
        "gate_severity: {scope_fence: hard}", "gate_severity: {scope_fence: soft}")
    put_config(tmp_path, changed + "tickets: {max_minutes: 120}\nscheduler: {max_unmerged: 4}\n")
    assert await admission.dispatch(tickets[1]) == "merged"
    before, after = snapshots
    assert before is not after
    assert (before.tickets.max_minutes, after.tickets.max_minutes) == (90, 120)
    assert (before.scheduler.max_unmerged, after.scheduler.max_unmerged) == (2, 4)
    assert (before.hosts["claude"].pins["review"], after.hosts["claude"].pins["review"]) == ("opus", "haiku-max")
    assert (before.review.gate_severity["scope_fence"], after.review.gate_severity["scope_fence"]) == ("hard", "soft")
    assert_idle(admission)


@pytest.mark.asyncio
async def test_waiting_dispatch_captures_after_admission(tmp_path, tickets):
    put_config(tmp_path)
    started, release, cleaning, finish = (asyncio.Event() for _ in range(4))
    loads, snapshots, finished = [], [], []

    def load():
        if loads:
            assert finished == [tickets[0]]
        loads.append(load_config(None, cwd=tmp_path))
        return loads[-1]

    def bind(snapshot):
        snapshots.append(snapshot)

        async def callback(ticket):
            if ticket is tickets[0]:
                try:
                    started.set()
                    await release.wait()
                finally:
                    cleaning.set()
                    await finish.wait()
                    finished.append(ticket)
            return "merged"
        return callback

    admission = DaemonAdmission(snapshot_dispatch(load, bind))
    first = asyncio.create_task(admission.dispatch(tickets[0]))
    await started.wait()
    second = asyncio.create_task(admission.dispatch(tickets[1]))
    await turn()
    assert len(loads) == len(snapshots) == 1
    release.set()
    await cleaning.wait()
    put_config(tmp_path, VALID + "scheduler: {max_unmerged: 5}\n")
    await turn()
    assert len(loads) == 1 and not second.done()
    finish.set()
    assert await asyncio.gather(first, second) == ["merged", "merged"]
    assert [snapshot.scheduler.max_unmerged for snapshot in snapshots] == [2, 5]
    assert_idle(admission)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, "bad: [", VALID + "notify: null\n",
    VALID.replace("schema_version: 1", "schema_version: 2"), VALID + "unknown: value\n",
    VALID.replace("pins: {review: opus}", "pins: {review: sonnet}"),
    VALID.replace("models: {claude: opus}", "models: {codex: opus}")])
async def test_invalid_reload_never_binds_or_uses_stale_config(tmp_path, tickets, invalid):
    put_config(tmp_path)
    bound, called = [], []

    def bind(snapshot):
        bound.append(snapshot)

        async def callback(ticket):
            called.append(ticket)
            return "merged"
        return callback

    admission = DaemonAdmission(snapshot_dispatch(lambda: load_config(None, cwd=tmp_path), bind))
    assert await admission.dispatch(tickets[0]) == "merged"
    if invalid is None:
        (tmp_path / "config.yaml").unlink()
    else:
        put_config(tmp_path, invalid)
    with pytest.raises(ConfigError):
        await admission.dispatch(tickets[1])
    assert len(bound) == len(called) == 1
    assert_idle(admission)
    put_config(tmp_path, VALID + "scheduler: {max_unmerged: 6}\n")
    assert await admission.dispatch(tickets[1]) == "merged"
    assert len(bound) == len(called) == 2 and bound[-1].scheduler.max_unmerged == 6
    assert_idle(admission)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["load", "capture", "bind", "callback", "cancel"])
async def test_snapshot_dispatch_unwinds_failures_and_cancellation(tmp_path, tickets, monkeypatch, failure):
    put_config(tmp_path)
    source = load_config(None, cwd=tmp_path)
    error = ValueError("original failure")
    calls = []
    failing = True
    started, cleaning, release = (asyncio.Event() for _ in range(3))

    def step(name):
        calls.append(name)
        if failing and failure == name:
            raise error

    def load():
        step("load")
        return source

    def capture(value):
        step("capture")
        return snapshot_config(value)

    def bind(snapshot):
        step("bind")

        async def callback(ticket):
            step("callback")
            if failing and failure == "cancel":
                try:
                    started.set()
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release.wait()
            return "merged"
        return callback

    monkeypatch.setattr(daemon, "snapshot_config", capture)
    admission = DaemonAdmission(snapshot_dispatch(load, bind))
    if failure == "cancel":
        first = asyncio.create_task(admission.dispatch(tickets[0]))
        await started.wait()
        owned = admission.task
        waiter = asyncio.create_task(admission.dispatch(tickets[1]))
        await turn()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert calls == ["load", "capture", "bind", "callback"]
        assert admission.task is owned and not owned.cancelling()
        first.cancel()
        await cleaning.wait()
        second = asyncio.create_task(admission.dispatch(tickets[1]))
        await turn()
        assert calls == ["load", "capture", "bind", "callback"] and not second.done()
        failing = False
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert owned.done() and owned.cancelled()
        assert await second == "merged"
    else:
        with pytest.raises(ValueError) as caught:
            await admission.dispatch(tickets[0])
        assert caught.value is error
        assert calls == ["load", "capture", "bind", "callback"][:["load", "capture", "bind", "callback"].index(failure) + 1]
        assert_idle(admission)
        failing = False
        assert await admission.dispatch(tickets[1]) == "merged"
    assert_idle(admission)
    assert await admission.dispatch(tickets[0]) == "merged"


@pytest.mark.parametrize("verb", ["run", "drain"])
@pytest.mark.parametrize("operation", ["snapshot_config", "snapshot_dispatch"])
def test_config_snapshot_is_dormant(root, monkeypatch, verb, operation):
    calls = []

    def pipeline(checkout):
        async def callback(ticket):
            calls.append(ticket.stem)
            checkout.journal.append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket=ticket.stem)
            return "merged"
        return with_retro_driver(checkout, callback)

    def probe(*args):
        raise AssertionError(f"{operation} wired into CLI")

    monkeypatch.setattr(daemon, operation, probe)
    if operation == "snapshot_config":
        monkeypatch.setattr(config_module, operation, probe)

    def wired(checkout):
        return DaemonAdmission(daemon.snapshot_dispatch(
            lambda: checkout.config, lambda snapshot: pipeline(checkout))).dispatch

    write(root, "work", ticket())
    argv = ["run", "work"] if verb == "run" else ["drain"]
    with pytest.raises(AssertionError, match=f"{operation} wired"):
        main(argv, cwd=root, env=ENV, clock=Clock(), pipeline=wired)
    assert calls == []
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
