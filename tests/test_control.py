import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from threading import Barrier, Event

import pytest

from tempo import control, daemon
from tempo.control import ControlProjection, ControlRequest, publish_request, validate_request
from tempo.journal import EventType, Journal
from tempo.seams import FileSystem, LocalFileSystem
from tests.test_cli import Stages, cli, journal, root, ticket, write
from tests.test_daemon_composition import CoreRig, assert_startup_pause_wiring
from tests.test_effects import Crash, FakeClock


class Rig:
    def __init__(self, state, *, lifecycle="life-1", journal_type=Journal):
        self.state = state
        self.fs = LocalFileSystem()
        self.journal = journal_type(state, FakeClock())
        self.lifecycle = lifecycle
        self.holds = set()
        self.projection = ControlProjection(lifecycle)
        self.changes = []
        self.applications = []
        self.inbox = self.reconstruct()

    def apply(self, projection):
        events = self.journal.read()
        assert any(e.body.get("decision") == "accepted" and
                   e.body.get("lifecycle_id") == self.lifecycle for e in events)
        self.applications.append(projection)
        if self.projection != projection:
            self.changes.append(projection)
        self.projection = projection
        self.holds.difference_update(projection.released_hold_ids)

    def reconstruct(self):
        return daemon.control_inbox(journal=self.journal, lifecycle_id=self.lifecycle,
                                    holds=lambda: self.holds, apply=self.apply,
                                    files=lambda: (self.state / "control/inbox").glob("*"),
                                    read=Path.read_bytes)

    def publish(self, id, verb="pause", hold=None, lifecycle=None):
        request = ControlRequest(id, lifecycle or self.lifecycle, verb, hold)
        publish_request(self.state, request, self.fs)
        return self.state / "control/inbox" / (id + ".json")

    def decisions(self):
        events = self.journal.read()
        assert all(e.type == EventType.SIGNAL and e.ticket is None and e.key is None for e in events)
        assert all(set(e.body) == {"kind", "request_id", "lifecycle_id", "verb", "hold_id",
                                   "decision", "reason"} for e in events)
        return [e.body for e in events]


def invalid_requests():
    good = asdict(ControlRequest("request", "life-1", "pause", None))
    cases = [None, [], "secret", 1, True, {}, {**good, "extra": "secret"}]
    cases += [{k: v for k, v in good.items() if k != missing} for missing in good]
    for key in good:
        for value in (None, True, 5, [], {}):
            if key != "hold_id" or value is not None:
                cases.append({**good, key: value})
    for key in ("request_id", "lifecycle_id"):
        cases.append({**good, key: ""})
    cases += [{**good, "request_id": value} for value in
              ("../escape", "a/b", "a\\b", "é", "a.b", "a b", "a\n", "other")]
    cases += [{**good, "verb": value} for value in ("", "PAUSE", "stop")]
    cases += [{**good, "hold_id": "hold"}, {**good, "verb": "kill", "hold_id": "hold"}]
    cases += [{**good, "verb": "resume", "hold_id": value} for value in (None, "", False, 4, [], {})]
    return cases


@pytest.mark.parametrize("value", invalid_requests())
def test_request_shape_and_identity_fail_closed(tmp_path, value):
    with pytest.raises(ValueError, match="read the current hold"):
        validate_request(value, "request.json")
    rig = Rig(tmp_path)
    path = tmp_path / "control/inbox/request.json"
    rig.fs.publish(path, json.dumps(value).encode())
    rig.inbox.consume()
    [decision] = rig.decisions()
    assert decision == {"kind": control.CONTROL_DECISION, "request_id": "request",
                        "lifecycle_id": None, "verb": None, "hold_id": None,
                        "decision": "rejected", "reason": decision["reason"]}
    assert "submit a new request" in decision["reason"] and "secret" not in decision["reason"]
    assert rig.applications == []


@pytest.mark.parametrize("raw", [b"{", b"\xff", b'{"request_id":"a","request_id":"b"}'])
def test_malformed_json_is_rejected_without_echoing(tmp_path, raw):
    rig = Rig(tmp_path)
    rig.fs.publish(tmp_path / "control/inbox/safe.json", raw)
    rig.inbox.consume()
    assert rig.decisions()[0]["decision"] == "rejected"
    assert rig.applications == []


def test_filename_identity_refusals(tmp_path):
    rig = Rig(tmp_path)
    value = asdict(ControlRequest("safe", "life-1", "pause", None))
    for filename in ("other.json", "safe.txt", "é.json", ".safe.json", "safe.json.tmp"):
        with pytest.raises(ValueError, match="filename"):
            validate_request(value, filename)
    for name in ("é.json", ".safe.json", ".control-private.tmp"):
        rig.fs.publish(tmp_path / "control/inbox" / name, b"partial")
    rig.inbox.consume()
    assert rig.decisions() == []


def test_publication_is_durable_and_never_overwrites(tmp_path, monkeypatch):
    fs = LocalFileSystem()
    assert isinstance(fs, FileSystem)
    path = tmp_path / "new/control/inbox/id.json"
    chronology = []
    fsync, link = os.fsync, os.link

    def sync(fd):
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        chronology.append("directory" if directory else "file")
        fsync(fd)

    def publish(src, dst):
        assert chronology[-1] == "file" and not path.exists()
        assert Path(src).parent == path.parent and Path(src).name.startswith(".")
        assert stat.S_IMODE(Path(src).stat().st_mode) == 0o600
        link(src, dst)
        assert path.read_bytes() == b'{"complete":true}'
        chronology.append("link")

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "link", publish)
    fs.publish(path, b'{"complete":true}')
    assert chronology[-3:] == ["file", "link", "directory"]
    assert chronology[:len(path.parent.parents) + 1] == ["directory"] * (len(path.parent.parents) + 1)
    monkeypatch.setattr(os, "link", link)
    with pytest.raises(FileExistsError):
        fs.publish(path, b"changed")
    assert path.read_bytes() == b'{"complete":true}'
    assert list(path.parent.glob(".*")) == []

    barrier = Barrier(2)
    temporaries = []
    concurrent = path.with_name("concurrent.json")

    def race(src, dst):
        temporaries.append(src)
        barrier.wait()
        link(src, dst)

    def writer(data):
        try:
            fs.publish(concurrent, data)
            return "published"
        except FileExistsError:
            return "exists"

    monkeypatch.setattr(os, "link", race)
    payloads = [b'{"writer":1}', b'{"writer":2}']
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(writer, payloads))
    assert sorted(results) == ["exists", "published"]
    assert len(set(temporaries)) == 2 and concurrent.read_bytes() in payloads
    assert list(path.parent.glob(".*")) == []


def test_complete_visibility_at_publication(tmp_path, monkeypatch):
    path = tmp_path / "id.json"
    ready, release = Event(), Event()
    link = os.link

    def blocked(src, dst):
        ready.set()
        release.wait()
        link(src, dst)

    monkeypatch.setattr(os, "link", blocked)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(LocalFileSystem().publish, path, b'{"complete":true}')
        ready.wait()
        assert not path.exists()
        release.set()
        future.result()
    assert json.loads(path.read_bytes()) == {"complete": True}


@pytest.mark.parametrize("point", ["file-fsync", "before-link", "after-link", "directory-fsync"])
def test_publication_crash_points(tmp_path, monkeypatch, point):
    rig = Rig(tmp_path)
    path = tmp_path / "control/inbox/id.json"
    fsync, link = os.fsync, os.link
    linked = False

    def sync(fd):
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if (point == "file-fsync" and not directory or
                point == "directory-fsync" and directory and linked):
            raise Crash
        fsync(fd)

    def publish(src, dst):
        nonlocal linked
        if point == "before-link":
            raise Crash
        link(src, dst)
        linked = True
        if point == "after-link":
            raise Crash

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", sync)
        patch.setattr(os, "link", publish)
        with pytest.raises(Crash):
            rig.publish("id")
    assert path.exists() == (point in {"after-link", "directory-fsync"})
    assert list(path.parent.glob(".*")) == []
    # Simulate an abrupt death that bypassed finally and left an incomplete private file.
    (path.parent / ".control-interrupted.tmp").write_bytes(b"{")
    rig.inbox.consume()
    if path.exists():
        assert rig.decisions()[0]["decision"] == "accepted"
    else:
        assert rig.decisions() == []
        rig.publish("id")
        rig.inbox.consume()
        assert len(rig.decisions()) == 1


def test_publisher_never_writes_journal(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("publisher wrote a journal")
    monkeypatch.setattr(Journal, "append", forbidden)
    request = ControlRequest("request", "explicit-target", "resume", "exact-hold")
    publish_request(tmp_path, request, LocalFileSystem())
    [path] = list(tmp_path.rglob("*.json"))
    assert path.relative_to(tmp_path) == Path("control/inbox/request.json")
    assert json.loads(path.read_bytes()) == asdict(request)
    assert not (tmp_path / "journal").exists()
    with pytest.raises(FileExistsError, match="new request id"):
        publish_request(tmp_path, ControlRequest("request", "other", "kill", None), LocalFileSystem())
    assert json.loads(path.read_bytes()) == asdict(request)


def test_decision_precedes_application(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    rig.publish("pause")
    original = rig.journal.append
    def refuse(*args, **kwargs):
        raise OSError("journal failed")
    monkeypatch.setattr(rig.journal, "append", refuse)
    with pytest.raises(OSError):
        rig.inbox.consume()
    assert rig.applications == [] and rig.decisions() == []
    monkeypatch.setattr(rig.journal, "append", original)
    rig.inbox.consume()
    assert rig.projection.pause_id == "pause" and len(rig.decisions()) == 1


def test_request_is_decided_once(tmp_path):
    rig = Rig(tmp_path)
    path = rig.publish("request")
    rig.inbox.consume()
    rig.inbox.consume()
    assert len(rig.applications) == 1
    rig.reconstruct().consume()
    # Reuse the already-decided identity with different, otherwise valid bytes.
    path.write_text(json.dumps(asdict(ControlRequest("request", "life-1", "kill", None))))
    rig.reconstruct().consume()
    assert path.exists() and len(rig.decisions()) == 1
    assert rig.projection.pause_id == "request" and not rig.projection.kill_requested
    assert len(rig.changes) == 1


@pytest.mark.parametrize("point", ["before-append", "after-append", "before-apply"])
def test_decision_crash_reconstructs_projection(tmp_path, point):
    class CrashJournal(Journal):
        def append(self, *args, **kwargs):
            if point == "before-append":
                raise Crash
            result = super().append(*args, **kwargs)
            if point == "after-append":
                raise Crash
            return result
    rig = Rig(tmp_path, journal_type=CrashJournal)
    path = rig.publish("pause")
    if point == "before-apply":
        rig.inbox.apply = lambda projection: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        rig.inbox.consume()
    assert rig.applications == [] and path.exists()
    assert len(rig.decisions()) == (0 if point == "before-append" else 1)
    rig.journal = Journal(tmp_path, FakeClock())
    rig.reconstruct().consume()
    rig.reconstruct().consume()
    assert len(rig.decisions()) == 1 and len(rig.changes) == 1
    assert rig.projection.pause_id == "pause"


def test_lifecycle_identity_never_retargets(tmp_path):
    old = Rig(tmp_path, lifecycle="restart-unique-old")
    old.publish("old-accepted")
    old.inbox.consume()
    new = Rig(tmp_path, lifecycle="restart-unique-new")
    new.holds.add("old-hold")
    for id, verb, hold in (("old-pause", "pause", None), ("old-resume", "resume", "old-hold"),
                           ("old-kill", "kill", None)):
        new.publish(id, verb, hold, lifecycle=old.lifecycle)
    new.inbox.consume()
    assert new.applications == [] and new.holds == {"old-hold"}
    assert [d["decision"] for d in new.decisions()] == ["accepted", "stale", "stale", "stale"]
    assert all(d["lifecycle_id"] == old.lifecycle for d in new.decisions())
    assert new.projection == ControlProjection(new.lifecycle)


@pytest.mark.parametrize("kind", ["pause", "admission", "storm"])
def test_resume_matches_only_its_hold(tmp_path, kind):
    rig = Rig(tmp_path)
    rig.publish("00-premature", "resume", "future")
    rig.inbox.consume()
    assert rig.decisions()[0]["decision"] == "stale"
    if kind == "pause":
        rig.publish("10-first")
        rig.inbox.consume()
        rig.publish("20-replacement")
        rig.inbox.consume()
        old, current = "10-first", "20-replacement"
    else:
        old, current = kind + "-old", kind + "-new"
        rig.holds.add(old)
        rig.holds = {current}
    rig.publish("30-old", "resume", old)
    rig.publish("40-matching", "resume", current)
    rig.inbox.consume()
    assert [d["decision"] for d in rig.decisions()][-2:] == ["stale", "accepted"]
    assert rig.projection.pause_id is None and current in rig.projection.released_hold_ids
    rig.holds.add("future")
    rig.reconstruct().consume()
    assert rig.holds == {"future"}
    assert rig.decisions()[0]["decision"] == "stale"
    rig.holds = {kind + "-later"}
    rig.reconstruct().recover()
    assert rig.holds == {kind + "-later"}


def test_latest_accepted_pause_resume_wins(tmp_path):
    rig = Rig(tmp_path)
    # Publication order differs from deterministic consumption order.
    rig.publish("30-later-pause")
    rig.publish("20-resume", "resume", "10-first-pause")
    rig.publish("10-first-pause")
    rig.inbox.consume()
    assert [d["request_id"] for d in rig.decisions()] == ["10-first-pause", "20-resume", "30-later-pause"]
    assert all(d["decision"] == "accepted" for d in rig.decisions())
    assert [p.pause_id for p in rig.changes] == ["10-first-pause", None, "30-later-pause"]
    rig.reconstruct().recover()
    assert rig.projection.pause_id == "30-later-pause" and len(rig.changes) == 3
    rig.publish("40-final-resume", "resume", "30-later-pause")
    rig.inbox.consume()
    rig.reconstruct().recover()
    assert rig.projection.pause_id is None and len(rig.changes) == 4


def test_current_kill_is_only_an_idempotent_projection(tmp_path):
    rig = Rig(tmp_path)
    rig.publish("kill", "kill")
    rig.inbox.consume()
    rig.reconstruct().recover()
    assert rig.projection.kill_requested and len(rig.changes) == 1
    assert rig.decisions()[0]["decision"] == "accepted"


def test_control_inbox_is_active(root, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("control boundary touched")

    directory = tmp_path / "production"
    directory.mkdir()
    production = CoreRig(directory)
    assert production.core.control.inbox.journal is production.journal
    assert_startup_pause_wiring(production.core)
    assert production.journal.read() == [] and production.fs.files == {}

    # Removing the actual production binding must defeat the positive observable.
    def assert_bound(core):
        assert_startup_pause_wiring(core)
    assert_bound(production.core)
    with monkeypatch.context() as patch:
        patch.setattr(production.core.admission, "_before_dispatch", None)
        with pytest.raises(AssertionError):
            assert_bound(production.core)

    for verb in ("run", "drain"):
        stem = "work-" + verb
        write(root, stem, ticket())
        stages = Stages()
        with monkeypatch.context() as patch:
            patch.setattr(control.ControlInbox, "consume", forbidden)
            if verb == "drain":
                with pytest.raises(AssertionError, match="control boundary"):
                    cli(root, verb, stages=stages)
                assert stages.calls == []
            else:
                assert cli(root, verb, stem, stages=stages) == 0
                assert stages.calls == [stem] and stages.lock_held == [True]
        if verb == "drain":
            assert cli(root, verb, stages=stages) == 0
            assert stages.calls == [stem] and stages.lock_held == [True]
    assert (root / ".tempo/state/control/active.json").read_bytes() == b"null\n"
    assert not any(e.body.get("kind") == control.CONTROL_DECISION for e in journal(root).read())
