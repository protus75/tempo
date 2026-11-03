"""Kill decisions use the lock holder's inbox and journal fold, including live drain activation."""

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from tempo import control
from tempo.control import ControlProjection, ControlRequest, publish_request
from tempo.journal import EventType, Journal
from tempo.seams import LocalFileSystem
from tests.test_cli import root
from tests.test_effects import Crash, FakeClock


ACCEPTED = "matching lifecycle and hold identities"
STALE = "read the current lifecycle and submit a new request"
SHAPE = ('submit a new request with exactly {request_id: nonempty ASCII letters/digits/_/-, '
         'lifecycle_id: nonempty string, verb: pause | resume | kill, hold_id: string | null}; '
         'match request_id to the filename; pause/kill require null hold_id; '
         'for resume read the current hold and submit its exact nonempty identity')


def decision(id, *, life="life-1", verb="kill", hold=None,
             result="accepted", reason=ACCEPTED):
    return {"kind": control.CONTROL_DECISION, "request_id": id, "lifecycle_id": life,
            "verb": verb, "hold_id": hold, "decision": result, "reason": reason}


class Rig:
    def __init__(self, state, *, life="life-1"):
        self.state, self.life = state, life
        self.clock, self.fs = FakeClock(), LocalFileSystem()
        self.journal = Journal(state, self.clock)
        self.projection = ControlProjection(life)
        self.holds, self.applications, self.changes = set(), [], []
        self.inbox = self.reconstruct()

    def apply(self, projection):
        assert any(e.body.get("decision") == "accepted" and
                   e.body.get("lifecycle_id") == self.life for e in self.journal.read())
        self.applications.append(projection)
        if projection != self.projection:
            self.changes.append(projection)
        self.projection = projection
        self.holds.difference_update(projection.released_hold_ids)

    def reconstruct(self, apply=None):
        return control.control_inbox(
            journal=self.journal, lifecycle_id=self.life, holds=lambda: self.holds,
            apply=apply or self.apply,
            files=lambda: reversed(list((self.state / "control/inbox").glob("*"))),
            read=Path.read_bytes)

    def publish(self, id, verb="kill", hold=None, life=None):
        publish_request(self.state, ControlRequest(id, life or self.life, verb, hold), self.fs)
        return self.state / "control/inbox" / (id + ".json")

    def decisions(self):
        events = self.journal.read()
        assert all(e.type == EventType.SIGNAL and e.ticket is None and e.key is None for e in events)
        assert all(set(e.body) == set(decision("id")) for e in events)
        return [e.body for e in events]


def test_kill_decision_precedes_projection(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    assert rig.inbox.journal is rig.journal and rig.journal._clock is rig.clock
    assert rig.inbox.recover() == ControlProjection(rig.life)
    assert rig.applications == [] and rig.decisions() == []
    path = rig.publish("kill_1-A")
    assert json.loads(path.read_bytes()) == asdict(ControlRequest("kill_1-A", rig.life, "kill", None))
    assert list(tmp_path.rglob("*.json")) == [path]
    assert not rig.journal.dir.exists()
    with pytest.raises(FileExistsError, match="submit a new request id"):
        rig.publish("kill_1-A", life="different")
    assert json.loads(path.read_bytes())["lifecycle_id"] == rig.life

    def apply(projection):
        # A fresh reader sees the fsynced record before any desired state changes.
        [event] = Journal(tmp_path, rig.clock).read()
        assert event.body == decision("kill_1-A")
        assert event.type == EventType.SIGNAL and event.ticket is None and event.key is None
        assert not rig.projection.kill_requested
        rig.apply(projection)

    rig.inbox.apply = apply
    append = rig.journal.append
    def failed(*args, **kwargs):
        raise OSError("append refused")
    monkeypatch.setattr(rig.journal, "append", failed)
    with pytest.raises(OSError, match="append refused"):
        rig.inbox.consume()
    assert rig.decisions() == [] and rig.applications == [] and path.exists()
    assert not rig.projection.kill_requested
    monkeypatch.setattr(rig.journal, "append", append)
    rig.inbox.consume()
    assert rig.projection == ControlProjection(rig.life, kill_requested=True)
    assert rig.decisions() == [decision("kill_1-A")]


def malformed_kills():
    good = asdict(ControlRequest("request", "life-1", "kill", None))
    values = [None, [], "private-payload", True, 1, {}, {**good, "extra": "private-payload"}]
    values += [{k: v for k, v in good.items() if k != missing} for missing in good]
    for key in good:
        values += [{**good, key: value} for value in (None, True, 1, [], {})
                   if key != "hold_id" or value is not None]
    values += [{**good, "request_id": value} for value in
               ("", "other", "../escape", "a/b", "a\\b", "é", "a.b", "a b", "a\n")]
    values += [{**good, "lifecycle_id": ""}, {**good, "hold_id": "hold"}]
    values += [{**good, "verb": value} for value in ("", "KILL", "stop")]
    return [json.dumps(value).encode() for value in values] + [
        b"{", b"\xff", b'{"request_id":"request","request_id":"request",'
        b'"lifecycle_id":"life-1","verb":"kill","hold_id":null}']


@pytest.mark.parametrize("raw", malformed_kills())
def test_kill_identity_and_shape_fail_closed(tmp_path, raw):
    rig = Rig(tmp_path)
    rig.publish("00-stale", life="old-life")
    path = tmp_path / "control/inbox/request.json"
    rig.fs.publish(path, raw)
    for name in ("é.json", ".request.json", ".control-private.tmp", "unsafe.name.json", "safe.txt"):
        rig.fs.publish(path.with_name(name), b"private-payload")
    rig.inbox.consume()
    assert rig.decisions() == [decision("00-stale", life="old-life", result="stale", reason=STALE),
                               decision("request", life=None, verb=None,
                                        result="rejected", reason=SHAPE)]
    assert rig.projection == ControlProjection(rig.life) and rig.applications == []
    rig.publish("zz-current")
    rig.inbox.consume()
    assert rig.decisions()[-1] == decision("zz-current") and rig.projection.kill_requested
    assert len(rig.changes) == 1


def test_kill_decision_is_once(tmp_path):
    rig = Rig(tmp_path)
    path = rig.publish("kill")
    original = path.read_bytes()
    rig.inbox.consume()
    rig.inbox.consume()
    assert path.read_bytes() == original and len(rig.applications) == 1
    path.write_bytes(b"changed and malformed")
    rig.inbox.consume()
    rig.reconstruct().consume()
    path.write_text(json.dumps(asdict(ControlRequest("kill", "new-life", "pause", None))))
    rig.reconstruct().consume()
    assert path.exists() and rig.decisions() == [decision("kill")]
    assert rig.projection == ControlProjection(rig.life, kill_requested=True)
    assert len(rig.changes) == 1


@pytest.mark.parametrize("point", ["before-append", "after-append", "before-apply", "after-apply"])
@pytest.mark.parametrize("failure", [Crash, asyncio.CancelledError])
def test_kill_decision_crash_recovery(tmp_path, monkeypatch, point, failure):
    rig = Rig(tmp_path)
    path = rig.publish("kill")
    append = rig.journal.append
    def interrupted(*args, **kwargs):
        if point == "before-append":
            raise failure()
        event = append(*args, **kwargs)
        if point == "after-append":
            raise failure()
        return event
    def apply(projection):
        if point == "before-apply":
            raise failure()
        rig.apply(projection)
        if point == "after-apply":
            raise failure()
    monkeypatch.setattr(rig.journal, "append", interrupted)
    rig.inbox.apply = apply
    with pytest.raises(failure):
        rig.inbox.consume()
    assert path.exists()
    assert len(rig.decisions()) == (0 if point == "before-append" else 1)
    assert rig.projection.kill_requested == (point == "after-apply")
    rig.journal = Journal(tmp_path, rig.clock)
    recovered = rig.reconstruct()
    recovered.consume()
    recovered.consume()
    rig.reconstruct().recover()
    assert rig.decisions() == [decision("kill")]
    assert rig.projection == ControlProjection(rig.life, kill_requested=True)
    assert len(rig.changes) == 1
    new = Rig(tmp_path, life="new-life")
    new.inbox.consume()
    assert new.inbox.recover() == ControlProjection("new-life")
    assert new.applications == []
    new.publish("old-kill", life=rig.life)
    new.inbox.consume()
    assert new.decisions() == [decision("kill"),
                               decision("old-kill", result="stale", reason=STALE)]
    assert new.projection == ControlProjection("new-life") and new.applications == []


def test_kill_projection_stays_latched(tmp_path):
    rig = Rig(tmp_path)
    rig.holds = {"admission", "storm"}
    for id, verb, hold in (("50-pause", "pause", None), ("40-release", "resume", "30-pause"),
                           ("30-pause", "pause", None), ("20-storm", "resume", "storm"),
                           ("10-kill", "kill", None)):
        rig.publish(id, verb, hold)
    rig.inbox.consume()
    assert [d["request_id"] for d in rig.decisions()] == [
        "10-kill", "20-storm", "30-pause", "40-release", "50-pause"]
    assert all(d["decision"] == "accepted" for d in rig.decisions())
    assert all(p.kill_requested for p in rig.applications)
    assert [p.pause_id for p in rig.applications] == [None, None, "30-pause", None, "50-pause"]
    assert rig.holds == {"admission"}
    rig.publish("60-resume", "resume", "50-pause")
    rig.inbox.consume()
    assert rig.projection == ControlProjection(rig.life, None,
                                               frozenset({"storm", "30-pause", "50-pause"}), True)
    rig.publish("70-admission", "resume", "admission")
    rig.inbox.consume()
    assert rig.holds == set() and rig.projection.kill_requested
    before = rig.projection
    rig.publish("80-old", life="old")
    rig.inbox.consume()
    rig.reconstruct().recover()
    assert rig.projection == before


