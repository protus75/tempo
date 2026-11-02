"""Host reports use the production Box, Author, and regression Check seams."""

import json
from pathlib import Path

import pytest

from tempo.box import Box, Resolution
from tempo.storm import storm_producer
from tempo.inbox import CAPS, ReportError, bounded, ingest, validate_report
from tempo.journal import Journal
from tempo.seams import LocalFileSystem
from tests.test_drain import Clock

ROOT = Path(__file__).resolve().parents[1]


def raw_report(origin="player", **updates):
    data = dict(schema_version=1, origin=origin, summary="Observed scoring defect",
                app_commit="host-supplied-evidence", app_version="1.2.3")
    if origin == "self_diagnosed":
        data.update(game_slug="score2", anomaly_code="illegal42", location="app/view.py:123")
    return {**data, **updates}


def arrival(root, **updates):
    root.mkdir(parents=True, exist_ok=True)
    (root / "inbox").mkdir(exist_ok=True)
    (root / "replay.json").write_text(json.dumps({"initial_state": {"score": 0}, "actions": []}))
    (root / "log.txt").write_text("structured log\n")
    path = root / "inbox/a.json"
    path.write_text(json.dumps(raw_report(evidence={"replay": "replay.json", "log": "log.txt"}, **updates)))
    return path


def test_report_schema_is_closed():
    def parse(data):
        return validate_report(json.dumps(data).encode())
    for origin in ("player", "self_diagnosed"):
        raw = raw_report(origin)
        for version in (0, 1):
            report = parse({**raw, "schema_version": version})
            assert report.schema_version == version and report.origin == origin
            assert report.implicated_paths == [] and report.evidence.model_dump() == {"replay": None, "log": None}
        assert parse({**raw, "evidence": {}}).evidence == report.evidence
        for field in raw:
            for value in (None, [], " ", 3) if field != "schema_version" else (-1, 2, True, "1", 1.0):
                with pytest.raises(ReportError, match="closed version-1"):
                    parse({**raw, field: value})
            with pytest.raises(ReportError):
                parse({k: v for k, v in raw.items() if k != field})
        for extra in ({"extra": "x"}, {"argv": ["touch", "owned"]}, {"signature": "invented"},
                      {"evidence": {"extra": "x"}}, {"evidence": None}, {"implicated_paths": None}):
            with pytest.raises(ReportError):
                parse({**raw, **extra})
    for field in ("game_slug", "anomaly_code", "location"):
        with pytest.raises(ReportError):
            parse(raw_report(**{field: "not allowed for a player"}))
    for value in ("", "../escape", "/absolute", "a/../../escape", "a\\b"):
        for extra in ({"implicated_paths": [value]}, {"evidence": {"log": value}}):
            with pytest.raises(ReportError):
                parse(raw_report(**extra))


def test_report_evidence_bounds(tmp_path):
    seen = []
    def metadata(path):
        seen.append("stat")
        return path.stat()
    def read(path):
        assert seen[-1] == "stat"
        seen.append("read")
        return path.read_bytes()
    for kind, cap in CAPS.items():
        path = tmp_path / kind
        data = b'{"initial_state":{},"actions":[]}' if kind == "replay" else b"log"
        path.write_bytes(data + b" " * (cap - len(data)))
        assert len(bounded(tmp_path, kind, kind, metadata=metadata, read=read)) == cap
        path.write_bytes(path.read_bytes() + b" ")
        before = len(seen)
        with pytest.raises(ReportError, match="over-limit"):
            bounded(tmp_path, kind, kind, metadata=metadata, read=read)
        assert seen[before:] == ["stat"]
        path.write_bytes(data)
        with pytest.raises(ReportError, match="grew"):
            bounded(tmp_path, kind, kind, read=lambda p: data + b" " * cap)
    (tmp_path / "directory").mkdir()
    outside = tmp_path.parent / "outside"
    outside.write_text("evidence")
    (tmp_path / "link").symlink_to(outside)
    for value in ("missing", "directory", "../outside", "link"):
        with pytest.raises(ReportError, match="host-contained regular"):
            bounded(tmp_path, value, "log", read=lambda p: pytest.fail("read refused evidence"))
    for record in ({}, {"actions": []}, {"initial_state": {}},
                   {"initial_state": {}, "actions": {}}):
        (tmp_path / "replay").write_text(json.dumps(record))
        with pytest.raises(ReportError, match="ordered actions"):
            bounded(tmp_path, "replay", "replay")
    # The host owns the state and action vocabulary; intake owns only the envelope.
    (tmp_path / "replay").write_text(json.dumps({"initial_state": [0], "actions": ["tick", 42]}))
    assert bounded(tmp_path, "replay", "replay")


def test_report_custody_precedes_enqueue(tmp_path, monkeypatch):
    source = arrival(tmp_path)
    box = Box(tmp_path / "state/box", LocalFileSystem())
    write = box.fs.write
    original = {kind: (tmp_path / name).read_bytes() for kind, name in
                (("replay", "replay.json"), ("log", "log.txt"))}
    publication = []
    def observed(path, data):
        if path.parent == box.root:
            message = json.loads(data)
            descriptor = message["report"]
            assert box.messages() == []
            for kind, value in descriptor["custody"].items():
                assert (box.root / value).read_bytes() == original[kind]
            (tmp_path / "replay.json").unlink()
            (tmp_path / "log.txt").write_text("changed")
            publication.append(message["id"])
        write(path, data)
    monkeypatch.setattr(box.fs, "write", observed)
    assert ingest(box, root=tmp_path, inbox=source.parent, diagnostic=pytest.fail) == publication
    message, = Box(box.root, LocalFileSystem()).messages()
    assert message.has_repro and message.bug_origin == "player"
    for kind, value in message.report.custody.model_dump().items():
        assert bounded(box.root, value, kind) == original[kind]


@pytest.mark.parametrize("point", ["custody", "enqueue", "consume"])
@pytest.mark.parametrize("after", [False, True])
def test_report_retry_ingests_once(tmp_path, monkeypatch, point, after):
    class Crash(BaseException):
        pass
    source = arrival(tmp_path, origin="self_diagnosed")
    clock = Clock()
    journal = Journal(tmp_path / "state", clock)
    box = storm_producer(root=tmp_path / "state/box", fs=LocalFileSystem(), journal=journal, clock=clock)
    write = box.fs.write
    def interrupted(path, data):
        target = ((point == "custody" and path.name == "report.json")
                  or (point == "enqueue" and path.parent == box.root)
                  or (point == "consume" and path.name == "consumed"))
        if target:
            if after:
                write(path, data)
            raise Crash()
        write(path, data)
    with monkeypatch.context() as patch:
        patch.setattr(box.fs, "write", interrupted)
        with pytest.raises(Crash):
            ingest(box, root=tmp_path, inbox=source.parent, diagnostic=pytest.fail)
    restarted = storm_producer(root=box.root, fs=box.fs, journal=journal, clock=clock)
    for _ in range(3):
        ingest(restarted, root=tmp_path, inbox=source.parent, diagnostic=pytest.fail)
    message, = restarted.messages()
    assert message.reports == 0 and len(message.report_arrivals) == 1
    occurrences = [e for e in journal.read() if e.body.get("kind") == "storm_occurrence"]
    assert len(occurrences) == 1 and occurrences[0].body["emitting_stage"] is None
    # The same arrival stays consumed even if its source bytes are later modified.
    restarted.resolve(message.id, Resolution(kind="tombstone", link="known"))
    source.write_text("broken")
    assert ingest(restarted, root=tmp_path, inbox=source.parent, diagnostic=pytest.fail) == []
    assert restarted.get(message.id).reports == 0
    bad = source.with_name("bad.json")
    bad.write_text("{}")
    diagnostics = []
    ingest(restarted, root=tmp_path, inbox=source.parent, diagnostic=diagnostics.append)
    assert len(diagnostics) == 1 and "bad.json" in diagnostics[0] and "repair" in diagnostics[0]
    assert bad.read_text() == "{}" and len(restarted.messages()) == 1


