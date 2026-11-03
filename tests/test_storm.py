from dataclasses import asdict
from datetime import timedelta, timezone
from unittest.mock import Mock

import pytest

from tempo import journal as journal_module
from tempo.box import Box, signature
from tempo.journal import EventType, Journal, JournalCorruption
from tempo.seams import LocalFileSystem
from tempo.storm import STORM_WINDOW, StormLedger, storm_producer
from tests.test_journal import FakeClock, T0

SIG = signature("failure_report", "one", "implement", "gate_failed", reason="failed path/one 123")
OTHER = "b" * 64


def ledger(root, clock=None):
    clock = clock or FakeClock(T0)
    journal = Journal(root, clock)
    return StormLedger(journal=journal, clock=clock), journal, clock


def record(ledger, identity="arrival", **changes):
    return ledger.record(**{"signature": SIG, "occurrence_id": identity,
                            "emitting_stage": "implement", "emitting_origin": "one", **changes})


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_storm_construction_and_reads_are_idle(tmp_path, monkeypatch):
    clock = Mock(return_value=T0)
    journal = Journal(tmp_path, clock)
    read = Mock(wraps=journal.read)
    append = Mock(side_effect=AssertionError("projection wrote an event"))
    monkeypatch.setattr(journal, "read", read)
    monkeypatch.setattr(journal, "append", append)
    storm = StormLedger(journal=journal, clock=clock)
    read.assert_not_called()
    append.assert_not_called()
    clock.assert_not_called()
    assert list(tmp_path.iterdir()) == []
    assert storm.occurrences(SIG) == () and storm.count(SIG) == 0
    assert read.call_count == clock.call_count == 2
    append.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("stage,origin", [("implement", "one"), (None, None),
                                         (None, "bootstrap-ingest"), ("", "")])
def test_occurrence_signal_shape_and_identity(tmp_path, stage, origin):
    storm, journal, clock = ledger(tmp_path)
    identity = " producer/id with spaces "
    event = record(storm, identity, emitting_stage=stage, emitting_origin=origin)
    assert asdict(event) == {
        "v": 1, "type": "signal", "ts": T0.isoformat(), "ticket": None,
        "key": f"storm-occurrence/{SIG}/{identity}",
        "body": {"kind": "storm_occurrence", "signature": SIG, "occurrence_id": identity,
                 "emitting_stage": stage, "emitting_origin": origin},
    }
    assert journal.read() == [event] and storm.occurrences(SIG) == (event,)
    # These are other event kinds even when their data resembles occurrence data.
    journal.append(EventType.EFFECT_INTENT, event.body, ticket="one", key=event.key)
    journal.append(EventType.SIGNAL, {"kind": "handoff_written"})
    assert storm.count(SIG) == 1


@pytest.mark.parametrize("changes", [
    {"signature": "A" * 64}, {"signature": "a" * 63}, {"signature": "g" * 64},
    {"signature": "a" * 64 + "\n"}, {"signature": None}, {"signature": 123},
    {"occurrence_id": ""}, {"occurrence_id": " \t\n"}, {"occurrence_id": None},
    {"occurrence_id": 1}, {"emitting_stage": False}, {"emitting_origin": []},
])
def test_occurrence_signal_shape_and_identity_invalid_input(tmp_path, monkeypatch, changes):
    storm, journal, _ = ledger(tmp_path)
    monkeypatch.setattr(journal, "read", Mock(side_effect=AssertionError("read invalid input")))
    with pytest.raises(ValueError, match="fresh id.*repair the producing evidence"):
        record(storm, **changes)
    assert snapshot(tmp_path) == {}


@pytest.mark.parametrize("damage", ["extra", "missing", "signature", "id", "stage", "origin",
                                   "kind", "key", "null-key", "ticket"])
def test_occurrence_signal_shape_and_identity_invalid_evidence(tmp_path, damage):
    storm, journal, _ = ledger(tmp_path)
    valid = record(storm)
    body, key, ticket = dict(valid.body), valid.key, None
    if damage == "extra":
        body["extra"] = 1
    elif damage == "missing":
        del body["emitting_origin"]
    elif damage in {"signature", "id", "stage", "origin"}:
        field = {"id": "occurrence_id", "stage": "emitting_stage", "origin": "emitting_origin"}.get(damage, damage)
        body[field] = 7
    elif damage == "kind":
        body["kind"] = "handoff_written"
    elif damage == "key":
        key = "wrong"
    elif damage == "null-key":
        key = None
    else:
        ticket = "one"
    journal.append(EventType.SIGNAL, body, ticket=ticket, key=key)
    before = snapshot(tmp_path)
    for operation in (lambda: storm.count(OTHER), lambda: record(storm, "new")):
        with pytest.raises(ValueError, match="repair the producing evidence.*never overwrite"):
            operation()
        assert snapshot(tmp_path) == before


def test_occurrence_replay_is_once(tmp_path):
    storm, journal, clock = ledger(tmp_path / "state")
    box = Box(tmp_path / "state/box", LocalFileSystem())
    kwargs = dict(message_class="failure_report", origin="one", summary="failed path/one 123",
                  stage="implement", outcome="gate_failed")
    message_id, created = box.enqueue(**kwargs)
    assert created and box.get(message_id).signature == SIG
    first = record(storm)
    before = snapshot(tmp_path)
    clock.advance(1)
    assert record(storm) == first
    assert snapshot(tmp_path) == before
    for changes in ({"emitting_stage": None}, {"emitting_origin": "two"}):
        with pytest.raises(ValueError, match="fresh id"):
            record(storm, **changes)
        assert snapshot(tmp_path) == before
    assert box.enqueue(**kwargs) == (message_id, False)
    second = record(storm, "second-arrival")
    assert first.key != second.key and storm.occurrences(SIG) == (first, second)
    assert len(box.messages()) == 1 and len(journal.read()) == 2
    # Duplicate durable evidence folds once and retains its first timestamp.
    journal.append(EventType.SIGNAL, first.body, key=first.key)
    assert storm.occurrences(SIG) == (first, second)
    journal.append(EventType.SIGNAL, {**first.body, "emitting_origin": "two"}, key=first.key)
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match="fresh id"):
        record(storm, "third")
    assert snapshot(tmp_path) == before


def test_window_boundaries_and_signature_isolation(tmp_path):
    assert STORM_WINDOW == timedelta(hours=1)
    storm, journal, clock = ledger(tmp_path)
    events = {}
    # Deliberately write timestamps out of order to pin journal order in the projection.
    for identity, seconds in [("now", 0), ("below", -3600.000001), ("equal", -3600),
                              ("above", -3599.999999), ("future", .000001), ("middle", -100)]:
        clock.now = T0 + timedelta(seconds=seconds)
        events[identity] = record(storm, identity)
    record(storm, "other", signature=OTHER)
    clock.now = T0.astimezone(timezone(timedelta(hours=-5)))
    assert storm.occurrences(SIG) == tuple(events[k] for k in ("now", "above", "middle"))
    assert storm.count(SIG) == 3 and storm.count(OTHER) == 1
    clock.now = T0 + STORM_WINDOW
    assert storm.occurrences(SIG) == (events["future"],)
    before = snapshot(tmp_path)
    clock.now = T0.replace(tzinfo=None)
    with pytest.raises(ValueError, match="aware"):
        storm.count(SIG)
    assert snapshot(tmp_path) == before


def test_occurrences_survive_roll_and_restart(tmp_path, monkeypatch):
    storm, journal, clock = ledger(tmp_path)
    first = record(storm, "expired")
    clock.advance(3601)
    monkeypatch.setattr(journal_module, "ROLL_BYTES", 1)
    second = record(storm, "rolled-live")
    clock.advance(1)
    third = record(storm, "active-live")
    paths = sorted(journal.dir.glob("*.jsonl"))
    assert len(paths) == 3
    storm, journal, _ = ledger(tmp_path, clock)
    before = snapshot(tmp_path)
    assert record(storm, "expired") == first
    assert snapshot(tmp_path) == before
    assert storm.occurrences(SIG) == (second, third) and storm.count(SIG) == 2
    paths[-1].write_bytes(paths[-1].read_bytes() + b'{"torn":')
    before = snapshot(tmp_path)
    assert storm.count(SIG) == 2 and snapshot(tmp_path) == before
    fourth = record(storm, "repaired-tail")
    assert storm.occurrences(SIG) == (second, third, fourth)
    # A rolled torn tail is corruption, never a skipped occurrence.
    paths[0].write_bytes(paths[0].read_bytes() + b'{"torn":')
    before = snapshot(tmp_path)
    for operation in (lambda: storm.count(SIG), lambda: record(storm, "blocked")):
        with pytest.raises(JournalCorruption, match="rolled segment"):
            operation()
        assert snapshot(tmp_path) == before


@pytest.mark.parametrize("damage", [b"not JSON\n", b'{}\n'])
def test_occurrences_survive_roll_and_restart_active_corruption(tmp_path, damage):
    storm, journal, _ = ledger(tmp_path)
    record(storm)
    path = next(journal.dir.glob("*.jsonl"))
    path.write_bytes(path.read_bytes() + damage)
    before = snapshot(tmp_path)
    with pytest.raises(JournalCorruption):
        storm.count(SIG)
    with pytest.raises(JournalCorruption):
        record(storm, "blocked")
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("when", ["read", "append", "before-crash", "after-crash"])
def test_occurrence_append_failures_and_crash_replay(tmp_path, monkeypatch, when):
    class Crash(BaseException):
        pass

    storm, journal, clock = ledger(tmp_path)
    append = journal.append
    durable = []

    def fail(*args, **kwargs):
        if when == "after-crash":
            durable.append(append(*args, **kwargs))
        raise (OSError("injected failure") if when in {"read", "append"} else Crash())

    with monkeypatch.context() as patch:
        patch.setattr(journal, "read" if when == "read" else "append", fail)
        with pytest.raises(OSError if when in {"read", "append"} else Crash):
            record(storm)
        if when == "read":
            with pytest.raises(OSError):
                storm.count(SIG)
    storm, journal, _ = ledger(tmp_path, clock)
    assert storm.count(SIG) == (1 if when == "after-crash" else 0)
    if when != "after-crash":
        assert snapshot(tmp_path) == {}
    else:
        assert storm.occurrences(SIG) == tuple(durable)
    clock.advance(5)
    event = record(storm)
    if durable:
        assert event == durable[0] and event.ts == T0.isoformat()
    before = snapshot(tmp_path)
    assert record(storm) == event and len(journal.read()) == 1
    assert snapshot(tmp_path) == before


def test_ledger_trip_uses_only_journal_and_box(tmp_path, monkeypatch):
    from tempo import control, daemon
    from tempo.effects import Effects

    def forbidden(*args, **kwargs):
        pytest.fail("storm invoked an external side effect")

    monkeypatch.setattr(Effects, "run", forbidden)
    monkeypatch.setattr(control, "publish_request", forbidden)
    monkeypatch.setattr(daemon.DaemonAdmission, "dispatch", forbidden)
    storm, journal, clock = ledger(tmp_path)
    box = storm_producer(root=tmp_path / "box", fs=LocalFileSystem(),
                                journal=journal, clock=clock)
    for n in range(12):
        box.enqueue(message_class="failure_report", origin="one", summary="failed path/one 123",
                    stage="implement", outcome="gate_failed", occurrence_id=str(n))
    assert list(storm.holds().values()) == ["one"]
    assert storm.count(SIG) == 12
    assert len([e for e in journal.read() if e.body.get("kind") == "storm_breaker_trip"]) == 1
    assert len(box.messages()) == 2


def test_storm_ledger_is_reachable(tmp_path, monkeypatch):
    from tests.test_storm_producer import assert_production_reachable

    assert_production_reachable(tmp_path, monkeypatch)
