import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tempo.effects import Effects
from tempo.journal import EventType, Journal
from tempo.seams import FileSystem, ProcessExec

T0 = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class FakeExec:
    """Scripted process-exec seam: records every argv it is asked to run."""

    def __init__(self, rc: int = 0, out: str = "ok\n", err: str = "") -> None:
        self.calls: list[list[str]] = []
        self.result = (rc, out, err)

    async def run(self, argv, *, cwd, env, timeout, stdin_path=None):
        self.calls.append(list(argv))
        return self.result


class FakeFS:
    def __init__(self) -> None:
        self.writes: list[tuple[Path, bytes]] = []

    def write(self, path: Path, data: bytes) -> None:
        self.writes.append((path, data))

    def replace(self, src: Path, dst: Path) -> None:
        raise AssertionError("not used")

    def publish(self, path: Path, data: bytes) -> None:
        raise AssertionError("not used")


class Crash(BaseException):
    """Stands in for the process dying at a chosen point."""


class CrashAfterCompletion(Journal):
    def append(self, type, body, **kw):
        event = super().append(type, body, **kw)
        if type == EventType.EFFECT_COMPLETION:
            raise Crash
        return event


def test_fakes_satisfy_seam_protocols():
    exec_: ProcessExec = FakeExec()
    fs: FileSystem = FakeFS()
    assert isinstance(exec_, ProcessExec)
    assert isinstance(fs, FileSystem)


def git_push(exec_: FakeExec):
    async def action():
        rc, out, err = await exec_.run(["git", "push"], cwd=Path("."), env={}, timeout=30)
        return {"rc": rc, "out": out}

    return action


def test_first_run_journals_intent_then_completion_and_returns_result(tmp_path):
    j = Journal(tmp_path, FakeClock())
    exec_ = FakeExec()
    result = asyncio.run(Effects(j).run(git_push(exec_), key="push/t-1/merged", ticket="t-1"))

    assert result == {"rc": 0, "out": "ok\n"}
    assert exec_.calls == [["git", "push"]]
    events = j.read()
    assert [e.type for e in events] == ["effect_intent", "effect_completion"]
    assert all(e.key == "push/t-1/merged" and e.ticket == "t-1" for e in events)
    assert events[1].body["result"] == {"rc": 0, "out": "ok\n"}


def test_same_key_in_process_replays_without_reexecuting(tmp_path):
    j = Journal(tmp_path, FakeClock())
    exec_ = FakeExec()
    effects = Effects(j)
    first = asyncio.run(effects.run(git_push(exec_), key="k", ticket=None))
    exec_.result = (1, "different", "")
    second = asyncio.run(effects.run(git_push(exec_), key="k", ticket=None))

    assert second == first
    assert len(exec_.calls) == 1
    assert len(j.read()) == 2


def test_distinct_keys_each_execute(tmp_path):
    j = Journal(tmp_path, FakeClock())
    exec_ = FakeExec()
    effects = Effects(j)
    asyncio.run(effects.run(git_push(exec_), key="k/1", ticket=None))
    asyncio.run(effects.run(git_push(exec_), key="k/2", ticket=None))
    assert len(exec_.calls) == 2


def test_completed_key_is_not_reexecuted_after_restart(tmp_path):
    fs = FakeFS()

    async def write_file():
        fs.write(Path("a.txt"), b"x")
        return "written"

    asyncio.run(Effects(Journal(tmp_path, FakeClock())).run(write_file, key="fs/a", ticket="t"))
    restarted = Effects(Journal(tmp_path, FakeClock()))
    assert asyncio.run(restarted.run(write_file, key="fs/a", ticket="t")) == "written"
    assert len(fs.writes) == 1


def test_crash_after_completion_journaled_does_not_double_execute(tmp_path):
    exec_ = FakeExec()
    with pytest.raises(Crash):
        asyncio.run(
            Effects(CrashAfterCompletion(tmp_path, FakeClock())).run(git_push(exec_), key="k", ticket="t")
        )
    assert len(exec_.calls) == 1

    exec_.result = (9, "would differ", "")
    replayed = asyncio.run(Effects(Journal(tmp_path, FakeClock())).run(git_push(exec_), key="k", ticket="t"))
    assert replayed == {"rc": 0, "out": "ok\n"}
    assert len(exec_.calls) == 1


def test_intent_only_crash_reexecutes(tmp_path):
    exec_ = FakeExec()

    async def dies_mid_effect():
        await exec_.run(["git", "push"], cwd=Path("."), env={}, timeout=30)
        raise Crash

    with pytest.raises(Crash):
        asyncio.run(Effects(Journal(tmp_path, FakeClock())).run(dies_mid_effect, key="k", ticket="t"))

    j = Journal(tmp_path, FakeClock())
    assert asyncio.run(Effects(j).run(git_push(exec_), key="k", ticket="t")) == {"rc": 0, "out": "ok\n"}
    assert len(exec_.calls) == 2
    assert [e.type for e in j.read()] == ["effect_intent", "effect_intent", "effect_completion"]


def test_failed_action_journals_no_completion_and_reraises(tmp_path):
    j = Journal(tmp_path, FakeClock())

    async def boom():
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        asyncio.run(Effects(j).run(boom, key="k", ticket=None))
    assert [e.type for e in j.read()] == ["effect_intent"]


def test_executing_and_replayed_results_are_identical(tmp_path):
    async def returns_tuple():
        return {"pair": (1, 2)}

    first = asyncio.run(Effects(Journal(tmp_path, FakeClock())).run(returns_tuple, key="k", ticket=None))
    replayed = asyncio.run(Effects(Journal(tmp_path, FakeClock())).run(returns_tuple, key="k", ticket=None))
    assert first == replayed == {"pair": [1, 2]}


def test_unserializable_result_is_refused_without_a_completion(tmp_path):
    j = Journal(tmp_path, FakeClock())

    async def returns_object():
        return object()

    with pytest.raises(TypeError, match="JSON"):
        asyncio.run(Effects(j).run(returns_object, key="k", ticket=None))
    assert [e.type for e in j.read()] == ["effect_intent"]


@pytest.mark.parametrize("key", ["", None, 3])
def test_key_must_be_a_nonempty_string(tmp_path, key):
    j = Journal(tmp_path, FakeClock())
    called = []

    async def action():
        called.append(1)

    with pytest.raises(ValueError, match="key"):
        asyncio.run(Effects(j).run(action, key=key, ticket=None))
    assert called == []
    assert j.read() == []
