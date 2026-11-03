import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest

from tempo.box import (
    Box,
    BoxError,
    DecisionRecord,
    Message,
    Resolution,
    Verdict,
    normalize,
    parse_record,
    read_registry,
    record_path,
    render_record,
    signature,
)
from tempo.git import Git
from tempo.seams import LocalFileSystem, SubprocessExec


def test_signature_recipe():
    assert normalize(" a 42 b ") == normalize("a b") == "a b"
    assert normalize(" 42 /tmp/42 a 37 b ") == "a b"
    assert signature("failure_report", "stem", "check", "red", reason="line 42 /tmp/a failed") == signature(
        "failure_report", "stem", "check", "red", reason="line 51 /other/b failed"
    )
    assert signature("x", reason="line 42 failed") == signature("x", reason="line failed")
    base = signature("failure_report", "stem", "check", "red", reason="failed")
    for fields in [
        ("suggestion", "stem", "check", "red"), ("failure_report", "other", "check", "red"),
        ("failure_report", "stem", "review", "red"), ("failure_report", "stem", "check", "ok"),
    ]:
        assert signature(*fields, reason="failed") != base
    line = "line 42 /tmp/a failed"
    compact = json.dumps(["suggestion", "origin", normalize(line)], separators=(",", ":"))
    assert signature("suggestion", "origin", reason=line) == hashlib.sha256(compact.encode()).hexdigest()


def test_host_report_signature(tmp_path):
    from tempo.inbox import validate_report
    from tests.test_inbox import raw_report

    box = Box(tmp_path / "box", LocalFileSystem())
    raw = raw_report("self_diagnosed")
    def enqueue(raw, occurrence):
        report = validate_report(json.dumps(raw).encode())
        descriptor = box.custody_report(occurrence, report, {})
        return box.enqueue_report(descriptor, occurrence_id=occurrence)
    id, created = enqueue(raw, "original")
    expected = hashlib.sha256(json.dumps([raw[k] for k in
        ("game_slug", "anomaly_code", "location", "app_version")], separators=(",", ":")).encode()).hexdigest()
    assert created and box.get(id).signature == expected
    assert enqueue({**raw, "summary": "Different wording", "app_commit": "new evidence"}, "rereport") == (id, False)
    assert enqueue(raw, "rereport") == (id, False)
    for field, value in (("location", "app/view.py:124"), ("location", "other/view.py:123"),
                         ("app_version", "1.2.4"), ("anomaly_code", "illegal43"), ("game_slug", "score3")):
        other, created = enqueue({**raw, field: value}, field + value.replace("/", "-"))
        assert created and other != id
    durable = Box(box.root, LocalFileSystem()).get(id)
    assert durable.report.report.model_dump() == validate_report(json.dumps(raw).encode()).model_dump()
    assert durable.report.custody.replay is None and durable.has_repro is False
    engine, _ = box.enqueue(message_class="failure_report", origin="engine", stage="check",
        outcome="gate_failed", summary="failed at 42 /tmp/file")
    assert box.get(engine).signature == signature("failure_report", "engine", "check", "gate_failed",
                                                 reason="failed at 51 /other/file")
    path = box._path(box.get(engine))
    legacy = json.loads(path.read_text())
    legacy.pop("report")
    legacy.pop("report_arrivals")
    path.write_text(json.dumps(legacy))
    assert box.get(engine).report is None
    player = raw_report()
    first, _ = enqueue(player, "player1")
    second, _ = enqueue(player, "player2")
    assert first != second  # Semantic dedup belongs to the sequential triage consumer.


def test_enqueue_dedup_and_validation(tmp_path):
    box = Box(tmp_path / "box", LocalFileSystem())
    first, created = box.enqueue(message_class="suggestion", origin="one", summary="hello")
    assert created
    message = box.get(first)
    assert first == f"box-000001-{message.signature[:8]}"
    assert [p.name for p in box.root.glob("*.json")] == [f"000001-{message.signature[:8]}.json"]
    assert Message.model_validate_json(next(box.root.glob("*.json")).read_text()) == message
    assert box.enqueue(message_class="suggestion", origin="one", summary="hello") == (first, False)
    assert len(list(box.root.glob("*.json"))) == 1
    second, created = box.enqueue(message_class="suggestion", origin="two", summary="hello")
    assert created and box.get(second).seq == 2
    box.resolve(first, Resolution(kind="ticket", link="ticket-a"))
    assert box.enqueue(message_class="suggestion", origin="one", summary="hello") == (first, False)
    assert len(list(box.root.glob("*.json"))) == 2
    for kwargs in [
        {"message_class": "bug_report", "origin": "x", "summary": "bug"},
        {"message_class": "suggestion", "origin": "x", "summary": "text", "bug_origin": "player", "has_repro": True},
        {"message_class": "suggestion", "origin": "x", "summary": "text", "stage": "check"},
    ]:
        with pytest.raises(BoxError):
            box.enqueue(**kwargs)
    assert len(list(box.root.glob("*.json"))) == 2


def test_durability_and_corruption(tmp_path):
    box = Box(tmp_path / "box", LocalFileSystem())
    id, _ = box.enqueue(message_class="bug_report", origin="host", summary="crash", bug_origin="player", has_repro=True)
    verdict = Verdict(verdict="author", produced_by_spec_version="1", rationale="repro exists")
    box.record_verdict(id, verdict)
    assert box.pending()[0].verdict == verdict
    resolution = Resolution(kind="ticket", link="crash-fix")
    box.resolve(id, resolution)
    assert Box(box.root, LocalFileSystem()).get(id).resolution == resolution
    assert box.pending() == []
    with pytest.raises(BoxError, match=id):
        box.resolve(id, resolution)
    (box.root / "bad.json").write_text("{}")
    with pytest.raises(BoxError, match="bad.json"):
        box.messages()


def test_registry_format(tmp_path):
    record = DecisionRecord(id="b", kind="decision", link="ticket-b", reopen_after_days=30)
    assert record_path("b") == Path("tickets/decisions/b.md")
    assert parse_record(render_record(record, "Reason\n")) == (record, "Reason\n")
    for text in [
        "body only", "---\nid: b\nkind: decision\nlink: ticket-b\n---\nbody",
        "---\nid: b\nkind: decision\nlink: ticket-b\nreopen_after_days: 30\nextra: x\n---\nbody",
    ]:
        with pytest.raises(BoxError):
            parse_record(text)
    folder = tmp_path / "tickets" / "decisions"
    folder.mkdir(parents=True)
    (folder / "b.md").write_text(render_record(record, "B"))
    (folder / "a.md").write_text(render_record(DecisionRecord(id="a", kind="tombstone", link="b", reopen_after_days=7), "A"))
    assert [item[0].id for item in read_registry(tmp_path)] == ["a", "b"]


def test_tombstone_rereports_reopen_at_threshold(tmp_path, monkeypatch):
    from tempo.storm import storm_producer
    from tempo.journal import EventType, Journal
    from tests.support import Clock, commit_ticket, confirmed
    from tests.test_triage import root_with_box, reply, run
    from tempo.llm import FakeLLM

    root, _ = root_with_box(tmp_path)
    clock = Clock()
    journal = Journal(root / ".tempo/state", clock)
    box = storm_producer(root=root / ".tempo/state/box", fs=LocalFileSystem(), journal=journal, clock=clock)
    commit_ticket(root, "known", confirmed())
    fields = dict(message_class="failure_report", origin="test", summary="observed failure")
    id, _ = box.enqueue(**fields, occurrence_id="original")
    assert box.get(id).reports == 0 and box.get(id).reopen is False
    assert run(root, monkeypatch, FakeLLM([reply("tombstone", link="known")])) == 0
    box.record_verdict(id, Verdict(verdict="tombstone", produced_by_spec_version="1.0", rationale="prior match"))
    path = root / record_path(f"tombstone-{id}")
    committed_bytes = path.read_bytes()
    box.enqueue(**fields, occurrence_id="original")
    assert box.get(id).reports == 0
    box.enqueue(**fields, occurrence_id="signature-1")
    box.enqueue(**fields, occurrence_id="signature-1")
    assert box.get(id).reports == 1 and box.get(id).resolution.kind == "tombstone"
    incoming, _ = box.enqueue(**{**fields, "summary": "semantically the same new evidence"}, occurrence_id="semantic-source")
    assert run(root, monkeypatch, FakeLLM([reply("tombstone", link=f"tombstone-{id}")])) == 0
    assert box.get(incoming).status == "resolved"
    assert box.get(id).reports == 2 and box.get(id).verdict.verdict == "tombstone"
    observed = []
    append = journal.append
    def before_clear(type_, body, **kwargs):
        if body.get("kind") == "tombstone_auto_reopened":
            target = box.get(id)
            assert target.reports == 3 and target.resolution.kind == "tombstone"
            assert target.status == "resolved" and target.verdict is not None
            observed.append(body)
        return append(type_, body, **kwargs)
    monkeypatch.setattr(journal, "append", before_clear)
    box.enqueue(**fields, occurrence_id="signature-crossing")
    message = box.get(id)
    assert message.reports == 3 and message.reopen and message.status == "pending"
    assert message.resolution is message.verdict is None
    signal, = [e for e in journal.read() if e.body.get("kind") == "tombstone_auto_reopened"]
    assert signal.key == f"tombstone-reopen/{id}/3" and signal.type == EventType.SIGNAL and signal.ticket is None
    assert observed == [signal.body] == [{"kind": "tombstone_auto_reopened", "box_id": id,
        "signature": message.signature, "reports": 3}]
    assert path.read_bytes() == committed_bytes
    assert asyncio.run(Git(SubprocessExec(), env=os.environ, timeout=30)._run(
        root, "show", f"HEAD:{record_path(f'tombstone-{id}')}")).encode() == committed_bytes
    restarted = storm_producer(root=box.root, fs=box.fs, journal=Journal(root / ".tempo/state", clock), clock=clock)
    before = restarted._rereport.journal.read()
    for _ in range(3):
        restarted.enqueue(**fields, occurrence_id="signature-crossing")
        restarted.record_rereport(id, occurrence_id=f"semantic/{incoming}")
        restarted.pending()
        restarted.recover()
    assert restarted.get(id) == message and restarted._rereport.journal.read() == before
    assert path.read_bytes() == committed_bytes


def test_tombstone_reopen_requires_journal_callback(tmp_path, monkeypatch):
    from tempo.box import RereportCallbackRequired, RereportJournal
    from tempo.journal import Journal
    from tests.support import Clock

    box = Box(tmp_path / "box", LocalFileSystem())
    id, _ = box.enqueue(message_class="suggestion", origin="test", summary="same")
    box.record_verdict(id, Verdict(verdict="tombstone", produced_by_spec_version="1", rationale="duplicate"))
    resolution = Resolution(kind="tombstone", link="old")
    box.resolve(id, resolution)
    for _ in range(2):
        box.record_rereport(id)
    with pytest.raises(RereportCallbackRequired, match=r"supply rereport=RereportJournal\(journal\)"):
        box.record_rereport(id)
    assert box.get(id).reports == 3 and box.get(id).resolution == resolution
    assert box.get(id).status == "resolved" and not box.get(id).reopen
    journal = Journal(tmp_path, Clock())
    bound = Box(box.root, box.fs, rereport=RereportJournal(journal))
    append = journal.append
    def refused(type_, body, **kwargs):
        if body.get("kind") == "tombstone_auto_reopened":
            raise OSError("append failed")
        return append(type_, body, **kwargs)
    monkeypatch.setattr(journal, "append", refused)
    with pytest.raises(OSError, match="append failed"):
        bound.record_rereport(id, occurrence_id="retryable")
    assert bound.get(id).reports == 4 and bound.get(id).resolution == resolution
    assert bound.get(id).verdict is not None and not bound.get(id).reopen
    monkeypatch.setattr(journal, "append", append)
    bound.record_rereport(id, occurrence_id="retryable")
    assert bound.get(id).reports == 4 and bound.get(id).reopen
    assert len([e for e in journal.read() if e.body.get("kind") == "tombstone_auto_reopened"]) == 1


@pytest.mark.parametrize("point", ["intent", "count", "signal", "reopen", "completion"])
@pytest.mark.parametrize("after", [False, True])
def test_rereport_crash_replay_counts_and_reopens_once(tmp_path, monkeypatch, point, after):
    from tempo.storm import storm_producer
    from tempo.journal import EventType, Journal
    from tests.support import Clock

    class Crash(BaseException):
        pass
    clock = Clock()
    journal = Journal(tmp_path, clock)
    box = storm_producer(root=tmp_path / "box", fs=LocalFileSystem(), journal=journal, clock=clock)
    fields = dict(message_class="suggestion", origin="test", summary="same")
    id, _ = box.enqueue(**fields, occurrence_id="original")
    box.resolve(id, Resolution(kind="tombstone", link="registry"))
    for n in range(2):
        box.enqueue(**fields, occurrence_id=f"report/{n}")
    append, write = journal.append, box.fs.write
    def crashed_append(type_, body, **kwargs):
        target = ((point == "intent" and type_ == EventType.EFFECT_INTENT)
                  or (point == "completion" and type_ == EventType.EFFECT_COMPLETION)
                  or (point == "signal" and body.get("kind") == "tombstone_auto_reopened"))
        if target:
            if after:
                append(type_, body, **kwargs)
            raise Crash()
        return append(type_, body, **kwargs)
    def crashed_write(path, data):
        record = json.loads(data)
        if record["reports"] == 3 and ((point == "count" and not record["reopen"]) or
                                      (point == "reopen" and record["reopen"])):
            if after:
                write(path, data)
            raise Crash()
        write(path, data)
    with monkeypatch.context() as patch:
        patch.setattr(journal, "append", crashed_append)
        patch.setattr(box.fs, "write", crashed_write)
        with pytest.raises(Crash):
            box.enqueue(**fields, occurrence_id="crossing")
    restarted = storm_producer(root=box.root, fs=box.fs, journal=Journal(tmp_path, clock), clock=clock)
    for _ in range(2):
        restarted.enqueue(**fields, occurrence_id="crossing")
    assert restarted.get(id).reports == 3 and restarted.get(id).reopen
    signals = [e for e in restarted._rereport.journal.read() if e.body.get("kind") == "tombstone_auto_reopened"]
    assert len(signals) == 1 and signals[0].key == f"tombstone-reopen/{id}/3"


@pytest.mark.parametrize("after", [False, True])
def test_late_rereport_replay_preserves_later_arrival_count(tmp_path, monkeypatch, after):
    from tempo.storm import storm_producer
    from tempo.journal import Journal
    from tests.support import Clock

    clock = Clock()
    box = storm_producer(root=tmp_path / "box", fs=LocalFileSystem(),
                         journal=Journal(tmp_path, clock), clock=clock)
    fields = dict(message_class="suggestion", origin="test", summary="same")
    id, _ = box.enqueue(**fields, occurrence_id="original")
    box.resolve(id, Resolution(kind="tombstone", link="registry"))
    write = box.fs.write
    def interrupted(path, data):
        if after:
            write(path, data)
        raise OSError("count interrupted")
    with monkeypatch.context() as patch:
        patch.setattr(box.fs, "write", interrupted)
        with pytest.raises(OSError, match="count interrupted"):
            box.enqueue(**fields, occurrence_id="first-report")
    restarted = storm_producer(root=box.root, fs=box.fs, journal=Journal(tmp_path, clock), clock=clock)
    restarted.enqueue(**fields, occurrence_id="second-report")
    assert restarted.get(id).reports == 2 and not restarted.get(id).reopen
    restarted.enqueue(**fields, occurrence_id="first-report")
    assert restarted.get(id).reports == 2 and not restarted.get(id).reopen
    restarted.enqueue(**fields, occurrence_id="third-report")
    assert restarted.get(id).reports == 3 and restarted.get(id).reopen
    signals = [e for e in restarted._rereport.journal.read()
               if e.body.get("kind") == "tombstone_auto_reopened"]
    assert len(signals) == 1 and signals[0].key == f"tombstone-reopen/{id}/3"


def test_tombstone_visibility_uses_message_class(tmp_path, monkeypatch):
    from tests.test_retro_box import PROPOSAL
    box = Box(tmp_path / "box", LocalFileSystem())
    expected = []
    for cls, bug, repro, visible in [("suggestion", None, None, False), ("override_report", None, None, False),
        ("failure_report", None, None, True), ("retro_finding", None, None, True),
        ("bug_report", "self_diagnosed", False, True), ("bug_report", "player", True, True),
        ("bug_report", "player", False, False)]:
        evidence = {**PROPOSAL, "retro_report_key": "retro/1"} if cls == "retro_finding" else {}
        id, _ = box.enqueue(message_class=cls, origin=f"{cls}/{bug}/{repro}", summary="concern",
                            bug_origin=bug, has_repro=repro, **evidence)
        box.resolve(id, Resolution(kind="tombstone", link=f"tombstone-{id}"))
        if visible:
            expected.append(id)
    before = {p: p.read_bytes() for p in box.root.glob("*.json")}
    def forbidden(*args, **kwargs):
        pytest.fail("visibility wrote to the queue")
    monkeypatch.setattr(box.fs, "write", forbidden)
    assert [m.id for m in box.confirmed_tombstones()] == expected
    assert all("priority" not in m.model_dump() for m in box.messages())
    assert {p: p.read_bytes() for p in box.root.glob("*.json")} == before
