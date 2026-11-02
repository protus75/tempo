import json
import os
import stat
from dataclasses import asdict
from datetime import UTC, datetime, timedelta, timezone

import pytest

from tempo import journal as module
from tempo.journal import Event, Journal, JournalCorruption
from tests.test_journal import FakeClock, T0, line, seed


def paths(journal):
    return sorted(journal.dir.glob("*.jsonl"))


def snapshot(journal):
    return {path.name: path.read_bytes() for path in paths(journal)}


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_roll_constants_and_size_boundary(tmp_path, monkeypatch, offset):
    assert module.ROLL_BYTES == 64 * 1024 * 1024
    assert module.ROLL_AGE == timedelta(hours=24)
    j = Journal(tmp_path, FakeClock(T0))
    first = j.append("signal", {"signal": "drain_handoff", "n": 1})
    original = paths(j)[0]
    length = original.stat().st_size
    monkeypatch.setattr(module, "ROLL_BYTES", length - offset)
    second = j.append("signal", {"signal": "drain_handoff", "crossing": "whole" * 50})
    assert len(paths(j)) == (1 if offset < 0 else 2)
    if offset < 0:
        assert list(j.read_segments()) == [(first, second)]
        before = original.read_bytes()
        third = j.append("signal", {"signal": "drain_handoff", "n": 3})
        assert original.read_bytes() == before
        assert list(j.read_segments()) == [(first, second), (third,)]
    else:
        assert list(j.read_segments()) == [(first,), (second,)]


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_roll_age_boundary_uses_injected_clock(tmp_path, offset):
    clock = FakeClock(T0)
    j = Journal(tmp_path, clock)
    first = j.append("signal", {"signal": "drain_handoff", "n": 1})
    clock.advance(3600)
    j.append("signal", {"signal": "drain_handoff", "n": 2})
    clock.now = T0 + module.ROLL_AGE + timedelta(microseconds=offset)
    j.append("signal", {"signal": "drain_handoff", "n": 3})
    assert len(paths(j)) == (1 if offset < 0 else 2)
    assert j.read()[0] == first

    # Neither an old filename nor mtime gives an empty segment an age.
    empty = tmp_path / "empty"
    seed(empty, "000009-20200101.jsonl")
    os.utime(empty / "journal/000009-20200101.jsonl", (0, 0))
    local = timezone(timedelta(hours=-5))
    clock.now = datetime(2026, 8, 4, 23, 30, tzinfo=local)
    j = Journal(empty, clock)
    event = j.append("signal", {"signal": "drain_handoff"})
    assert len(paths(j)) == 1
    assert event.ts == "2026-08-05T04:30:00+00:00"
    clock.advance(86400)
    j.append("signal", {"signal": "drain_handoff"})
    assert paths(j)[-1].name == "000010-20260806.jsonl"
    fresh = Journal(tmp_path / "fresh", clock)
    fresh.append("signal", {"signal": "drain_handoff"})
    assert paths(fresh)[0].name == "000001-20260806.jsonl"


@pytest.mark.parametrize("boundary", ["size", "age"])
def test_roll_sequence_and_restart(tmp_path, monkeypatch, boundary):
    clock = FakeClock(T0)
    j = Journal(tmp_path, clock)
    assert j.read() == [] and list(j.read_segments()) == []
    assert not j.dir.exists()
    seed(tmp_path, "000002-20260804.jsonl", line("signal", 1))
    seed(tmp_path, "000019-20260804.jsonl", line("signal", 2))
    if boundary == "size":
        monkeypatch.setattr(module, "ROLL_BYTES", paths(j)[-1].stat().st_size)
    else:
        clock.advance(86400)
    before = snapshot(j)
    j = Journal(tmp_path, clock)
    assert len(j.read()) == 2
    assert snapshot(j) == before
    j.append("signal", {"signal": "drain_handoff", "n": 3})
    assert paths(j)[-1].name == f"000020-{clock.now:%Y%m%d}.jsonl"
    monkeypatch.setattr(module, "ROLL_BYTES", 1)
    for sequence in (21, 22):
        j = Journal(tmp_path, clock)
        j.append("signal", {"signal": "drain_handoff", "n": sequence})
        assert paths(j)[-1].name == f"{sequence:06d}-{clock.now:%Y%m%d}.jsonl"
    assert [e.body["n"] for e in j.read()] == [1, 2, 3, 21, 22]


def test_roll_preserves_records_and_read_laws(tmp_path, monkeypatch):
    j = Journal(tmp_path, FakeClock(T0))
    bodies = [
        ("effect_intent", {"action": "notify", "args": ["hello"]}),
        ("effect_completion", {"result": {"nested": [True, None]}, "usd": 1.5}),
        ("timer_armed", {"deadline": T0.isoformat()}),
        ("timer_fired", {"timer": "one"}),
        ("cap_consumed", {"cap": "infra"}),
        ("state_transition", {"to": "merged", "commit": "abc"}),
        ("signal", {"kind": "drain_handoff", "unicode": "é"}),
    ]
    monkeypatch.setattr(module, "ROLL_BYTES", 1)
    events = []
    old = {}
    for type_, body in bodies:
        events.append(j.append(type_, body, ticket="one", key="key/one"))
        assert all(path.read_bytes() == data for path, data in old.items())
        old[paths(j)[-1]] = paths(j)[-1].read_bytes()
    assert j.read() == events
    assert list(j.read_segments()) == [(event,) for event in events]
    for path, event in zip(paths(j), events):
        assert json.loads(path.read_bytes()) == asdict(event)
        assert isinstance(event, Event)
    active = paths(j)[-1]
    active.write_bytes(active.read_bytes() + b'{"torn":')
    assert j.read() == events
    assert list(j.read_segments())[-1] == (events[-1],)
    rolled = paths(j)[0]
    rolled.write_bytes(rolled.read_bytes() + b'{"torn":')
    for read in (j.read, lambda: list(j.read_segments())):
        with pytest.raises(JournalCorruption, match="restore it from backup"):
            read()

    for tail in (b"bad\n", b"bad\n" + line("signal", 8).encode() + b"\n"):
        corrupt = tmp_path / str(len(tail))
        seed(corrupt, "000001-20260804.jsonl", line("signal", 7))
        path = corrupt / "journal/000001-20260804.jsonl"
        path.write_bytes(path.read_bytes() + tail)
        journal = Journal(corrupt, FakeClock(T0 + module.ROLL_AGE))
        before = snapshot(journal)
        for action in (journal.read, lambda: list(journal.read_segments()),
                       lambda: journal.append("signal", {"signal": "drain_handoff"})):
            with pytest.raises(JournalCorruption):
                action()
        assert snapshot(journal) == before


@pytest.mark.parametrize("boundary", ["size", "age"])
def test_startup_tail_repair_precedes_roll(tmp_path, monkeypatch, boundary):
    clock = FakeClock(T0)
    j = Journal(tmp_path, clock)
    first = j.append("signal", {"signal": "drain_handoff", "n": 1})
    path = paths(j)[0]
    complete = path.read_bytes()
    path.write_bytes(complete + b'{"torn":')
    if boundary == "size":
        monkeypatch.setattr(module, "ROLL_BYTES", len(complete))
    else:
        clock.advance(86400)
    j = Journal(tmp_path, clock)
    second = j.append("signal", {"signal": "drain_handoff", "n": 2})
    assert path.read_bytes() == complete
    assert list(j.read_segments()) == [(first,), (second,)]
    # A tail alone is truncated to an empty segment and gets its first event.
    other = tmp_path / "only-tail"
    seed(other, "000001-20200101.jsonl")
    path = other / "journal/000001-20200101.jsonl"
    path.write_bytes(b'{"torn":')
    monkeypatch.setattr(module, "ROLL_BYTES", 1024)
    j = Journal(other, clock)
    assert j.read() == []
    event = j.append("signal", {"signal": "drain_handoff"})
    assert list(j.read_segments()) == [(event,)]


@pytest.mark.parametrize("failure", [None, "create", "directory", "publication_fsync",
                                    "published_directory", "write", "event_fsync", "clock", "collision"])
def test_roll_durability_and_failures(tmp_path, monkeypatch, failure):
    clock = FakeClock(T0)
    j = Journal(tmp_path, clock)
    first = j.append("signal", {"signal": "drain_handoff", "n": 1})
    old = paths(j)[0]
    before = old.read_bytes()
    monkeypatch.setattr(module, "ROLL_BYTES", 1)
    trace = []
    real_publish, real_write, real_fsync = j._fs.publish, os.write, os.fsync

    def refuse():
        raise OSError("injected failure")

    def publish(path, data):
        trace.append("publish")
        if failure == "create":
            refuse()
        if failure == "collision":
            path.write_bytes(b"existing custody")
        real_publish(path, data)
        trace.append("published")

    def write(fd, data):
        trace.append("write")
        if failure == "write":
            refuse()
        return real_write(fd, data)

    def fsync(fd):
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        trace.append("dir_fsync" if directory else "file_fsync")
        if failure == "directory" and directory:
            refuse()
        if failure == "publication_fsync" and not directory:
            refuse()
        if failure == "published_directory" and directory and len(paths(j)) == 2:
            refuse()
        if failure == "event_fsync" and "write" in trace and not directory:
            refuse()
        real_fsync(fd)

    monkeypatch.setattr(j._fs, "publish", publish)
    monkeypatch.setattr(module.os, "write", write)
    monkeypatch.setattr(module.os, "fsync", fsync)
    if failure == "clock":
        j._clock = refuse
    if failure is None:
        second = j.append("signal", {"signal": "drain_handoff", "n": 2})
        assert list(j.read_segments()) == [(first,), (second,)]
        assert trace.index("publish") < trace.index("published") < trace.index("write")
        assert trace[trace.index("published") - 1] == "dir_fsync"
        assert trace[-1] == "file_fsync"
    else:
        with pytest.raises(OSError):
            j.append("signal", {"signal": "drain_handoff", "n": 2})
        if failure == "collision":
            assert paths(j)[-1].read_bytes() == b"existing custody"
        if failure in {"create", "directory", "publication_fsync", "clock"}:
            assert snapshot(j) == {old.name: before}
        if failure == "published_directory":
            assert "write" not in trace and paths(j)[-1].read_bytes() == b""
    assert old.read_bytes() == before

    j._clock = clock
    stable = snapshot(j)
    for args, kwargs in [(("unknown", {}), {}), (("checkpoint", {}), {}),
                         (("signal", []), {}), (("signal", {"signal": "drain_handoff"}), {"ticket": 1}),
                         (("signal", {"signal": "drain_handoff"}), {"key": 1}), (("signal", {"signal": "drain_handoff", "bad": object()}), {})]:
        with pytest.raises((ValueError, TypeError)):
            j.append(*args, **kwargs)
        assert snapshot(j) == stable
    j.close()
    with pytest.raises(RuntimeError, match="closed"):
        j.append("signal", {"signal": "drain_handoff"})
    assert snapshot(j) == stable
