"""Heartbeat construction, custody, freshness, and calibrated behavioral dormancy."""

import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest

from tempo import __main__ as cli
from tempo.config import load_config
from tempo.daemon import HeartbeatCycle
from tempo.git import Git
from tempo.heartbeat import Heartbeat, is_fresh
from tempo.journal import Journal
from tempo.seams import LocalFileSystem, SubprocessExec
from tests.test_cli import ENV, Clock, Stages, root, ticket, write
from tests.test_daemon_composition import CoreRig, assert_core_wiring

NAMES = ("workers", "merge_queue", "box_consumer", "watcher")
AGE = timedelta(seconds=10)


class Evidence:
    def __init__(self, trace=None, local=None):
        self.now = datetime(2026, 1, 1, tzinfo=UTC)
        self.files = {}
        self.trace = trace if trace is not None else []
        self.local = local

    def clock(self):
        return self.now

    def write(self, path, data):
        self.trace.append((path, data))
        if self.local is not None:
            self.local.write(path, data)
        self.files[path] = (data, self.now)

    def metadata(self, path):
        return self.files[path][1] if path in self.files else None


def boundary(heartbeat, probe=lambda: True):
    return HeartbeatCycle(heartbeat=heartbeat, **dict.fromkeys(NAMES, probe))


@pytest.mark.asyncio
async def test_construction_is_idle(tmp_path):
    def forbidden(*args):
        pytest.fail("construction performed work")

    fs = Evidence()
    fs.write = forbidden
    before = asyncio.all_tasks()
    heartbeat = Heartbeat(state_dir=tmp_path, fs=fs)
    boundary(heartbeat, forbidden)
    assert asyncio.all_tasks() == before
    assert fs.files == {} and fs.trace == []
    assert not heartbeat.path.exists()


def test_healthy_cycle_refreshes_heartbeat(root):
    config = load_config(None, cwd=root)
    journal = Journal(config.state_dir, Clock())
    before = journal.read()
    trace = []
    fs = Evidence(trace, LocalFileSystem())
    heartbeat = Heartbeat(state_dir=config.state_dir, fs=fs)

    def probe(name):
        def healthy():
            trace.append(name)
            return True  # Running workers and idle consumers can both be responsive.
        return healthy

    owner = HeartbeatCycle(heartbeat=heartbeat, **{name: probe(name) for name in NAMES})
    assert owner.cycle() is True
    assert trace == [*NAMES, (config.state_dir / "heartbeat", b"")]
    first = fs.metadata(heartbeat.path)
    fs.now += AGE
    trace.clear()
    assert owner.cycle() is True
    assert trace == [*NAMES, (heartbeat.path, b"")]
    assert fs.metadata(heartbeat.path) == first + AGE
    assert heartbeat.path.read_bytes() == b""
    assert list(config.state_dir.iterdir()) == [heartbeat.path]
    assert journal.read() == before

    async def custody():
        git = Git(SubprocessExec(), env=ENV, timeout=30)
        assert not any(path.startswith(".tempo/") for path in await git.ls_files(root))
        assert await git.status_porcelain(root) == ""
    asyncio.run(custody())


@pytest.mark.asyncio
@pytest.mark.parametrize("component", NAMES)
@pytest.mark.parametrize("state", ["dead", "cancelled", "completed", "wedged"])
async def test_unhealthy_core_stops_refresh(tmp_path, component, state):
    fs = Evidence()
    heartbeat = Heartbeat(state_dir=tmp_path, fs=fs)
    entered, release = asyncio.Event(), asyncio.Event()

    async def core():
        entered.set()
        await release.wait()
        if state == "dead":
            raise RuntimeError("core died")

    task = asyncio.create_task(core())
    await entered.wait()
    responsive = True
    calls = []

    def probe(name):
        def health():
            calls.append(name)
            return name != component or (responsive and not task.done())
        return health

    owner = HeartbeatCycle(heartbeat=heartbeat, **{name: probe(name) for name in NAMES})
    assert owner.cycle() is True
    saved = dict(fs.files)
    fs.trace.clear()
    if state == "cancelled":
        task.cancel()
    elif state != "wedged":
        release.set()
    if state != "wedged":
        await asyncio.gather(task, return_exceptions=True)
    responsive = False
    calls.clear()
    assert owner.cycle() is False
    assert calls == list(NAMES) and fs.files == saved and fs.trace == []
    assert task.done() is (state != "wedged")
    assert task.cancelling() == (1 if state == "cancelled" else 0)
    fs.now += AGE + timedelta(microseconds=1)
    assert not is_fresh(heartbeat.path, metadata=fs.metadata, clock=fs.clock, max_age=AGE)

    # A blocked main-loop continuation cannot independently refresh evidence.
    blocked, resume = asyncio.Event(), asyncio.Event()
    healthy = boundary(heartbeat)
    async def main_loop():
        blocked.set()
        await resume.wait()
        return healthy.cycle()
    loop = asyncio.create_task(main_loop())
    await blocked.wait()
    fs.now += AGE
    assert fs.files == saved and fs.trace == [] and not loop.done()
    resume.set()
    assert await loop is True
    assert fs.trace == [(heartbeat.path, b"")]
    assert is_fresh(heartbeat.path, metadata=fs.metadata, clock=fs.clock, max_age=AGE)
    release.set()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("failure", [*NAMES, "write"])
def test_heartbeat_failures_propagate(tmp_path, failure):
    fs = Evidence()
    heartbeat = Heartbeat(state_dir=tmp_path, fs=fs)
    error = OSError("heartbeat failure")
    def fail(*args):
        raise error
    probes = dict.fromkeys(NAMES, lambda: True)
    if failure == "write":
        fs.write = fail
    else:
        probes[failure] = fail
    owner = HeartbeatCycle(heartbeat=heartbeat, **probes)
    with pytest.raises(OSError) as caught:
        owner.cycle()
    assert caught.value is error and fs.files == {} and fs.trace == []


@pytest.mark.parametrize("age, expected", [(timedelta(0), True), (AGE, True),
    (AGE + timedelta(microseconds=1), False), (timedelta(microseconds=-1), False), (None, False)])
def test_external_heartbeat_freshness(tmp_path, age, expected):
    fs = Evidence()
    path = tmp_path / "explicit-heartbeat"
    reads = []
    mtime = None if age is None else (fs.now - age).astimezone(timezone(timedelta(hours=3)))
    def metadata(requested):
        reads.append(requested)
        return mtime
    assert is_fresh(path, metadata=metadata, clock=fs.clock, max_age=AGE) is expected
    assert reads == [path] and fs.files == {} and fs.trace == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("invalid", ["zero", "negative", "naive-mtime", "naive-clock", "metadata"])
def test_external_heartbeat_refuses_invalid_evidence(tmp_path, invalid):
    fs = Evidence()
    path = tmp_path / "heartbeat"
    reads = []
    error = OSError("metadata failed")
    def metadata(requested):
        reads.append(requested)
        if invalid == "metadata":
            raise error
        return fs.now.replace(tzinfo=None) if invalid == "naive-mtime" else fs.now
    age = timedelta(0) if invalid == "zero" else -AGE if invalid == "negative" else AGE
    clock = (lambda: fs.now.replace(tzinfo=None)) if invalid == "naive-clock" else fs.clock
    if invalid == "metadata":
        with pytest.raises(OSError) as caught:
            is_fresh(path, metadata=metadata, clock=clock, max_age=age)
        assert caught.value is error
    else:
        road = "supply a strictly positive" if invalid in {"zero", "negative"} else "supply timezone-aware"
        with pytest.raises(ValueError, match=road):
            is_fresh(path, metadata=metadata, clock=clock, max_age=age)
    assert reads == ([] if invalid in {"zero", "negative"} else [path])
    assert fs.files == {} and fs.trace == [] and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("verb", ["run", "drain"])
def test_heartbeat_is_dormant(root, tmp_path, monkeypatch, verb):
    heartbeat = Heartbeat(state_dir=root / ".tempo/state", fs=Evidence())
    dormant = boundary(heartbeat)
    def cycle_probe(self):
        raise AssertionError("heartbeat cycle wired")
    def construction_probe(self, **kwargs):
        raise AssertionError("heartbeat construction wired")
    monkeypatch.setattr(HeartbeatCycle, "cycle", cycle_probe)
    write(root, "work", ticket())
    argv = ["run", "work"] if verb == "run" else ["drain"]

    def wired_cycle(checkout):
        async def dispatch(_):
            dormant.cycle()
        return dispatch
    with pytest.raises(AssertionError, match="heartbeat cycle wired"):
        cli.main(argv, cwd=root, env=ENV, clock=Clock(), pipeline=wired_cycle)
    monkeypatch.setattr(HeartbeatCycle, "__init__", construction_probe)
    def wired_construction(checkout):
        boundary(heartbeat)
    with pytest.raises(AssertionError, match="heartbeat construction wired"):
        cli.main(argv, cwd=root, env=ENV, clock=Clock(), pipeline=wired_construction)

    original = cli.build_daemon_core
    for name, action in [("construction", lambda: boundary(heartbeat)),
                         ("cycle", dormant.cycle)]:
        def wired_graph(*args, _action=action, **kwargs):
            core = original(*args, **kwargs)
            _action()
            return core
        monkeypatch.setattr(cli, "build_daemon_core", wired_graph)
        graph_root = tmp_path / name
        graph_root.mkdir()
        with pytest.raises(AssertionError, match=f"heartbeat {name} wired"):
            CoreRig(graph_root)
    monkeypatch.setattr(cli, "build_daemon_core", original)

    async def graph():
        graph_root = tmp_path / "ordinary"
        graph_root.mkdir()
        before = asyncio.all_tasks()
        rig = CoreRig(graph_root)
        assert_core_wiring(rig)
        assert asyncio.all_tasks() == before and rig.journal.read() == []
        assert rig.exec.calls == [] and rig.fs.files == {}
        assert not (rig.checkout.config.state_dir / "heartbeat").exists()
    asyncio.run(graph())
    stages = Stages()
    assert cli.main(argv, cwd=root, env=ENV, clock=Clock(), pipeline=stages) == 0
    assert stages.calls == ["work"] and stages.lock_held == [True]
    assert not (stages.checkout.config.state_dir / "heartbeat").exists()
    assert not any("heartbeat" in str(event.body) for event in stages.checkout.journal.read())


@pytest.mark.asyncio
async def test_heartbeat_is_active_in_serve(root, monkeypatch):
    from tests.test_serve import activated_graph
    rig = await activated_graph(root, monkeypatch)
    assert rig.owner.heartbeat.heartbeat.path.read_bytes() == b""
