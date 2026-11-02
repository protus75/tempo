"""Host reports use the production Box, Author, and regression Check seams."""

import asyncio
import json
import os
import re
import sys
from pathlib import Path

import pytest

from tempo.box import Box, Resolution
from tempo.daemon import storm_producer
from tempo.inbox import CAPS, ReportError, bounded, ingest, validate_report
from tempo.journal import Journal
from tempo.seams import LocalFileSystem
from tempo.specs import ENTRY_PARTS, PlanContractError, entry_unit_gap, required_units, resolve_plan_contract
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


def test_report_inbox_entry_units():
    plan = (ROOT / "TEMPO_PLAN.md").read_text()
    own, bug = "19.P6.report-inbox-triage", "19.P6.bug-gate-grammar"
    assert required_units(plan, "report-inbox-triage", ("19.I", own, bug, "12")) == (own, bug)
    assert required_units(plan, "later-consumer", (own, bug)) == (own, bug)
    assert ENTRY_PARTS == ("Owner", "Records", "Observable", "Tests")
    resolved = resolve_plan_contract(plan, (own, bug))
    for uid in (own, bug):
        assert resolved.count(f"### {uid} ") == 1
        assert entry_unit_gap(plan, uid) is None
        unit = resolve_plan_contract(plan, (uid,))
        for part in ENTRY_PARTS:
            for replacement in ("", f"- **{part}:**"):
                broken = re.sub(rf"^- \*\*{part}:\*\*.*$", replacement, unit, flags=re.M)
                assert part in entry_unit_gap(broken, uid)
        for broken in ("", unit + unit):
            assert entry_unit_gap(broken, uid)
            with pytest.raises(PlanContractError):
                resolve_plan_contract(broken, (uid,))


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


def test_report_replay_to_regression(tmp_path, monkeypatch):
    from dataclasses import replace
    from hosts.fixture import scaffold
    from tempo.__main__ import main
    from tempo.git import Git
    from tempo.llm import FakeLLM
    from tempo.seams import SubprocessExec
    from tempo.stages import Invoice, check, implement
    from tempo.tickets import validate_ticket
    from tests.test_cli import ENV
    from tests.test_merge import context
    from tests.test_stages import implement_reply
    from tests.test_triage import reply, requisition_reply

    host = scaffold.copy_host(tmp_path / "host")
    env = {**ENV, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
    config = host / "config.yaml"
    config.write_text(config.read_text().replace("report_inbox: reports", "report_inbox: inbox"))
    (host / "TEMPO_PLAN.md").write_text("# Disposable host plan\n")
    (host / ".gitignore").write_text(".tempo/\n.fixture-runtime/\ninbox/\n__pycache__/\n")
    source = host / "inbox/regression.json"
    source.parent.mkdir()
    source.write_bytes((host / "reports/regression.json").read_bytes())
    git = Git(SubprocessExec(), env=env, timeout=30)
    async def initialize():
        await git.init(host, branch="main")
        await git.add(host, [p.relative_to(host).as_posix() for p in host.rglob("*")
                            if p.is_file() and not {"inbox", ".git", ".fixture-runtime"}.intersection(p.parts)])
        await git.commit(host, "fixture defective base")
    asyncio.run(initialize())
    proposal = dict(scaffold.scenario(host, "regression")["responses"]["author"][0])
    stem = proposal["stem"]
    llm = FakeLLM([reply("author"), json.dumps(proposal), requisition_reply("approve")])
    monkeypatch.setattr("tempo.__main__.ProviderLLM", lambda *args, **kwargs: llm)
    assert main(["triage"], cwd=host, env=env, clock=Clock()) == 0
    box = Box(host / ".tempo/state/box", LocalFileSystem())
    message, = box.messages()
    assert message.resolution == Resolution(kind="ticket", link=stem)
    evidence = (host / f"tickets/{stem}/evidence/replay.json").read_bytes()
    (host / "evidence/regression.json").unlink()
    source.unlink()
    # Replay is interpreted solely by the host harness, with a fixed argv.
    rc, out, _ = asyncio.run(SubprocessExec().run(
        [sys.executable, "replay.py", f"tickets/{stem}/evidence/replay.json"], cwd=host, env=env, timeout=30))
    assert rc == 1 and json.loads(out)["defect"] is True

    wrapper = scaffold.scenario(host, "regression")["edits"]["fixtures/report_regression.py"]
    ctx = replace(context(host, []), env=env, git=git)
    class Implementer(FakeLLM):
        async def call(self, request):
            assert request.surface == "implement"
            work = request.worktree
            settings = json.loads((work / "settings.json").read_text())
            ctx.fs.write(work / "settings.json", json.dumps({**settings, "add_scale": 1}).encode())
            ctx.fs.write(work / "fixtures/replay.json", evidence)
            ctx.fs.write(work / "fixtures/report_regression.py", wrapper.encode())
            await ctx.git.add(work, ["settings.json", "fixtures/replay.json", "fixtures/report_regression.py"])
            await ctx.git.commit(work, "repair score and carry permanent regression")
            return await super().call(request)
    ctx.driver.llm = Implementer([implement_reply()])
    ticket = validate_ticket(stem, (host / f"tickets/{stem}/ticket.md").read_text(), host)
    async def production_check():
        implemented = await implement(ctx, ticket, attempt=0)
        assert implemented.outcome == "ok", implemented.findings
        checked = await check(ctx, ticket, implemented.artifact, attempt=0)
        assert checked.outcome == "ok", checked.findings
        invoice = Invoice.model_validate_json((host / f"tickets/{stem}/checks.json").read_text())
        report, = [r for r in invoice.reports if r.code == "bug_evidence"]
        assert report.verdict == "pass"
        bug = report.bug_evidence
        assert bug.head.rc == 0 and bug.head.regression_result == "pass"
        assert bug.base.rc == 1 and bug.base.regression_result == "defect"
        assert bug.overlay_paths == ["fixtures/replay.json", "fixtures/report_regression.py"]
        assert "settings.json" not in bug.overlay_paths
        assert (ctx.worktree(stem) / "fixtures/replay.json").read_bytes() == evidence
        assert not (ctx.config.worktree_root / ".regression-base" / stem).exists()
    asyncio.run(production_check())
