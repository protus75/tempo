"""Production CLI composition with a scripted provider and a real ticket-plane checkout."""

import json
import asyncio
from pathlib import Path

import pytest

from tempo.__main__ import main
from tempo.box import Box, DecisionRecord, Resolution, Verdict, parse_record, record_path, render_record
from tempo.journal import EventType, Journal
from tempo.llm import FakeLLM
from tempo.lockfile import Lockfile
from tempo.seams import LocalFileSystem
from tests.test_cli import ENV, git_out
from tests.test_drain import Clock, commit_ticket, confirmed, make_root


def root_with_box(tmp_path: Path) -> tuple[Path, Box]:
    root = make_root(tmp_path)
    config = root.joinpath("config.yaml")
    config.write_text(config.read_text().replace("  - {tier: medium, surface: implement, candidates: [{provider: claude}]}", """  - {tier: medium, surface: implement, candidates: [{provider: claude}]}
  - {tier: low, surface: author, candidates: [{provider: claude}]}
  - {tier: medium, surface: author, candidates: [{provider: claude}]}
  - {tier: high, surface: author, candidates: [{provider: claude}]}
  - {tier: max, surface: author, candidates: [{provider: claude}]}
  - {tier: low, surface: review, candidates: [{provider: claude}]}
  - {tier: medium, surface: review, candidates: [{provider: claude}]}
  - {tier: high, surface: review, candidates: [{provider: claude}]}
  - {tier: max, surface: review, candidates: [{provider: claude}]}
"""))
    return root, Box(root / ".tempo" / "state" / "box", LocalFileSystem())


def enqueue(box: Box, summary: str) -> str:
    return box.enqueue(message_class="suggestion", origin="test", summary=summary)[0]


def reply(verdict: str, *, link=None, summary="Triage's own summary", evidence=None, days=30) -> str:
    return json.dumps({"verdict": verdict, "link": link, "summary": summary,
                       "rationale": "Observed and classified", "evidence": evidence or [],
                       "reopen_after_days": None if verdict == "author" else days})


def author_reply(stem: str) -> str:
    return json.dumps({"stem": stem, "ticket": """---
priority: P2
kind: feature
---

## Depends on
- none

## Context
- tempo/thing.py

## Goal / Why
`tempo/thing.py` is repaired.

## Scope in / Scope out
- In: `tempo/thing.py`.
- Out: unrelated files.

## Scope fence
- tempo/thing.py

## Acceptance criteria
- `uv run pytest` exits 0.

## Verification
```
uv run pytest
```

## Definition of rejected
- `tempo/thing.py` cannot be changed safely.

## Time budget
- expected: 10m
- stuck: 20m
"""})


def requisition_reply(verdict: str, findings: list | None = None) -> str:
    return json.dumps({"verdict": verdict, "summary": "reviewed", "findings": findings or []})


def run(root: Path, monkeypatch, llm: FakeLLM) -> int:
    monkeypatch.setattr("tempo.__main__.ProviderLLM", lambda *args, **kwargs: llm)
    return main(["triage"], cwd=root, env=ENV, clock=Clock())


def subjects(root: Path) -> list[str]:
    return git_out(root, "log", "--format=%s").splitlines()


def events(root: Path):
    return Journal(root / ".tempo" / "state", Clock()).read()


def test_three_verdicts_and_second_pass_and_stale_author(tmp_path, monkeypatch, capsys):
    root, box = root_with_box(tmp_path)
    commit_ticket(root, "existing", confirmed())
    marker = "RAW-SUMMARY-UNIQUE-987"
    ids = [enqueue(box, marker), enqueue(box, "another concern"), enqueue(box, "third concern")]
    messages = [box.get(id) for id in ids]
    llm = FakeLLM([reply("tombstone", link="existing", days=7), reply("decision", days=14),
                   reply("author"), author_reply("authored"), requisition_reply("approve")])
    before = subjects(root)
    assert run(root, monkeypatch, llm) == 0
    assert "ticket -> authored" in capsys.readouterr().out
    assert subjects(root)[:3] == ["tempo(authored): ticket", f"tempo(decisions): decision-{ids[1]}",
                                   f"tempo(decisions): tombstone-{ids[0]}"]
    assert subjects(root)[3:] == before
    for id, kind, link, days in [(ids[0], "tombstone", "existing", 7),
                                 (ids[1], "decision", ids[1], 14)]:
        path = record_path(f"{kind}-{id}")
        record, body = parse_record((root / path).read_text())
        assert (record.kind, record.link, record.reopen_after_days) == (kind, link, days)
        assert marker not in body
        assert box.get(id).status == "resolved"
        assert git_out(root, "rev-parse", f"HEAD:{path}").strip()
    assert box.get(ids[2]).resolution == Resolution(kind="ticket", link="authored")
    assert box.get(ids[2]).verdict.produced_by_spec_version == "1.1"
    assert len(llm.requests) == 5
    assert [(r.surface, r.tier, r.ticket) for r in llm.requests] == [
        ("triage", "medium", None), ("triage", "medium", None), ("triage", "medium", None),
        ("author", "medium", None), ("requisition_review", "medium", None),
    ]
    keys = {e.key for e in events(root) if e.type == EventType.EFFECT_COMPLETION and e.key.startswith("llm/")}
    assert keys == {f"llm/triage/{m.seq}/triage/0/1" for m in messages} | {
        f"llm/author/0/author/{messages[2].seq}/1",
        f"llm/author/0/requisition_review/{messages[2].seq}/1"}
    for m in messages:
        spool = root / ".tempo" / "state" / "spools" / "triage" / str(m.seq) / "0" / "triage" / "call-01"
        assert (spool / "prompt.md").exists() and (spool / "output.txt").exists()
    stale_id = enqueue(box, "stale concern")
    box.record_verdict(stale_id, Verdict(verdict="author", produced_by_spec_version="0.9", rationale="old"))
    assert run(root, monkeypatch, llm) == 0
    assert len(llm.requests) == 5
    assert box.get(stale_id).status == "resolved"
    assert subjects(root)[0] == f"tempo(decisions): decision-{stale_id}"
    stale, body = parse_record((root / record_path(f"decision-{stale_id}")).read_text())
    assert stale.kind == "decision" and "0.9" in body and "1.1" in body


def test_bad_tombstone_link_becomes_decision(tmp_path, monkeypatch):
    root, box = root_with_box(tmp_path)
    id = enqueue(box, "bad reference")
    llm = FakeLLM([reply("tombstone", link="missing")])
    assert run(root, monkeypatch, llm) == 0
    record, body = parse_record((root / record_path(f"decision-{id}")).read_text())
    assert record.kind == "decision" and record.link == id and "missing" in body
    assert box.get(id).status == "resolved"


def test_model_echo_of_raw_summary_is_removed_from_record(tmp_path, monkeypatch):
    root, box = root_with_box(tmp_path)
    marker = "RAW-SUMMARY-PRIVATE-123"
    id = enqueue(box, marker)
    llm = FakeLLM([reply("decision", summary=marker, evidence=[f"report said {marker}"])])
    assert run(root, monkeypatch, llm) == 0
    committed = git_out(root, "show", f"HEAD:{record_path(f'decision-{id}')}")
    assert marker not in committed


def test_precommitted_record_resolves_without_call(tmp_path, monkeypatch):
    root, box = root_with_box(tmp_path)
    id = enqueue(box, "already classified")
    path = record_path(f"decision-{id}")
    (root / path).parent.mkdir(parents=True)
    (root / path).write_text(render_record(DecisionRecord(id=f"decision-{id}", kind="decision",
                                                       link=id, reopen_after_days=8), "Prior classification\n"))
    git_out(root, "add", str(path))
    git_out(root, "commit", "-m", f"tempo(decisions): decision-{id}", "--only", "--", str(path))
    llm = FakeLLM([])
    assert run(root, monkeypatch, llm) == 0
    assert box.get(id).resolution == Resolution(kind="decision", link=f"decision-{id}")
    assert llm.requests == []


def test_uncommitted_record_is_committed_before_resolution(tmp_path, monkeypatch):
    root, box = root_with_box(tmp_path)
    id = enqueue(box, "crash after write")
    path = record_path(f"decision-{id}")
    (root / path).parent.mkdir(parents=True)
    (root / path).write_text(render_record(DecisionRecord(id=f"decision-{id}", kind="decision",
                                                       link=id, reopen_after_days=8), "Recover me\n"))
    llm = FakeLLM([])
    assert run(root, monkeypatch, llm) == 0
    assert llm.requests == []
    assert subjects(root)[0] == f"tempo(decisions): decision-{id}"
    assert git_out(root, "rev-parse", f"HEAD:{path}").strip()
    assert box.get(id).status == "resolved"


def test_uncommitted_registry_target_is_not_valid_tombstone_link(tmp_path, monkeypatch):
    root, box = root_with_box(tmp_path)
    id = enqueue(box, "new concern")
    target = "decision-other"
    path = record_path(target)
    (root / path).parent.mkdir(parents=True)
    (root / path).write_text(render_record(DecisionRecord(id=target, kind="decision", link="other",
                                                       reopen_after_days=8), "Uncommitted\n"))
    llm = FakeLLM([reply("tombstone", link=target)])
    assert run(root, monkeypatch, llm) == 0
    assert parse_record((root / record_path(f"decision-{id}")).read_text())[0].kind == "decision"
    assert box.get(id).status == "resolved"


def test_invalid_replies_leave_pending_without_commit(tmp_path, monkeypatch):
    root, box = root_with_box(tmp_path)
    id = enqueue(box, "invalid reply")
    before = subjects(root)
    llm = FakeLLM(["{}"] * 7)
    assert run(root, monkeypatch, llm) == 0
    assert box.get(id).status == "pending" and box.get(id).verdict is None
    assert subjects(root) == before
    assert len(llm.requests) == 7


def test_report_ingested_before_pending_scan(tmp_path, monkeypatch):
    from tests.test_inbox import arrival
    from tempo import inbox

    root, box = root_with_box(tmp_path)
    source = arrival(root)
    replay = root / "replay.json"
    data = replay.read_bytes()
    replay.write_bytes(data + b" " * (1024 * 1024 - len(data)))
    config = root / "config.yaml"
    config.write_text(config.read_text() + "report_inbox: inbox\n")
    assert box.pending() == []
    def classify(request):
        message, = box.pending()
        assert message.report is not None and message.has_repro
        assert "structured log" in request.rendered and 'initial_state' in request.rendered
        assert "excerpt truncated" in request.rendered
        assert (box.root / message.report.custody.replay).read_bytes() == replay.read_bytes()
        return reply("decision")
    llm = FakeLLM([classify])
    assert run(root, monkeypatch, llm) == 0
    assert len(llm.requests) == 1 and box.pending() == []
    assert source.exists()
    config.write_text(config.read_text().replace("report_inbox: inbox\n", ""))
    def forbidden(path):
        pytest.fail("absent report inbox must not read report sources")
    monkeypatch.setattr(inbox, "validate_report", forbidden)
    assert run(root, monkeypatch, FakeLLM([])) == 0


def test_report_author_failure_is_decision(tmp_path, monkeypatch):
    from tests.test_inbox import arrival
    root, box = root_with_box(tmp_path)
    arrival(root)
    config = root / "config.yaml"
    config.write_text(config.read_text() + "report_inbox: inbox\n")
    llm = FakeLLM([reply("author"), *[json.dumps({"stem": "broken", "ticket": "bad"})] * 7])
    assert run(root, monkeypatch, llm) == 0
    message, = box.messages()
    assert message.resolution == Resolution(kind="decision", link=f"decision-{message.id}")
    assert len([e for e in events(root) if e.body.get("signal") == "author_invoked"]) == 1
    assert len([r for r in llm.requests if r.surface == "author"]) == 7
    assert not (root / "tickets/broken").exists()
    assert "tickets/broken" not in git_out(root, "status", "--porcelain")
    assert run(root, monkeypatch, FakeLLM([])) == 0
    # A later arrival has its own allowance and can reuse the rejected stem.
    from tests.test_author import bug_reply
    source = root / "inbox/a.json"
    source.rename(source.with_name("b.json"))
    assert run(root, monkeypatch, FakeLLM([reply("author"), bug_reply("broken"),
                                          requisition_reply("approve")])) == 0
    assert (root / "tickets/broken/ticket.md").is_file()


def test_lock_contention_exits_two_without_pass(tmp_path, monkeypatch):
    root, box = root_with_box(tmp_path)
    enqueue(box, "locked")
    holder = Lockfile(root / ".tempo" / "state", instance_id="other", clock=Clock())
    holder.acquire()
    llm = FakeLLM([])
    try:
        assert run(root, monkeypatch, llm) == 2
    finally:
        holder.release()
    assert llm.requests == []
    assert not any(e.type == EventType.SIGNAL and e.body.get("signal") == "triage_pass" for e in events(root))


@pytest.mark.parametrize(("recorded_verdict", "tombstone_link"), [
    (True, None), (False, "own-record"), (False, "known"),
])
def test_reopened_message_bypasses_its_tombstone(tmp_path, monkeypatch, recorded_verdict, tombstone_link):
    from tempo import triage
    from tempo.box import RereportJournal
    root, source = root_with_box(tmp_path)
    box = Box(source.root, source.fs, rereport=RereportJournal(Journal(root / ".tempo/state", Clock())))
    commit_ticket(root, "known", confirmed())
    id = enqueue(box, "reopened concern")
    assert run(root, monkeypatch, FakeLLM([reply("tombstone", link="known")])) == 0
    path = root / record_path(f"tombstone-{id}")
    before = path.read_bytes()
    head = git_out(root, "rev-parse", "HEAD").strip()
    commits = subjects(root)
    for n in range(3):
        box.record_rereport(id, occurrence_id=f"distinct/{n}")
    assert box.get(id).reopen and box.get(id).verdict is None
    script = [author_reply("reconsidered"), requisition_reply("approve")]
    if recorded_verdict:
        box.record_verdict(id, Verdict(verdict="author", produced_by_spec_version="1.1", rationale="new evidence"))
    else:
        def classify(request):
            assert f"tombstone-{id}: tombstone" not in request.rendered
            assert '"reopen": true' in request.rendered
            return reply("tombstone", link=f"tombstone-{id}" if tombstone_link == "own-record" else tombstone_link)
        script.insert(0, classify)
    author = triage.author
    calls = []
    async def author_with_preserved_registry(*args):
        assert path.read_bytes() == before
        assert git_out(root, "rev-parse", "HEAD").strip() == head
        assert git_out(root, "show", f"HEAD:{record_path(f'tombstone-{id}')}").encode() == before
        calls.append(id)
        return await author(*args)
    monkeypatch.setattr(triage, "author", author_with_preserved_registry)
    llm = FakeLLM(script)
    assert run(root, monkeypatch, llm) == 0
    assert calls == [id]
    assert box.get(id).resolution == Resolution(kind="ticket", link="reconsidered")
    assert path.read_bytes() == before
    assert git_out(root, "show", f"HEAD:{record_path(f'tombstone-{id}')}").encode() == before
    assert subjects(root) == ["tempo(reconsidered): ticket", *commits]
    assert (root / "tickets/reconsidered/ticket.md").is_file()
    count = len(llm.requests)
    assert run(root, monkeypatch, llm) == 0 and len(llm.requests) == count


async def escape_rig(tmp_path):
    from tempo import runner, stages
    from tempo.tickets import validate_ticket
    from tests.test_drain import pause_checkout
    from tests.test_stages import implement_reply
    root, box = root_with_box(tmp_path)
    c = pause_checkout(root)
    writer = runner.bind(c, FakeLLM([implement_reply()]))
    text = confirmed().replace("uv run pytest", "true").replace("kind: feature",
        "kind: feature\ngate_bypass:\n  - {code: scope_fence, reason: witnessed fixture}")
    commit_ticket(root, "candidate", text)
    ticket = validate_ticket("candidate", text, root)
    slip = (await stages.implement(writer.ctx, ticket, attempt=0)).artifact
    wt = writer.ctx.worktree(ticket.stem)
    for path in ("tempo/thing.py", "outside.py"):
        c.fs.write(wt / path, b"changed\n")
    await c.git.add(wt, ["tempo/thing.py", "outside.py"])
    await c.git.commit(wt, "code")
    checked = (await stages.check(writer.ctx, ticket, slip, attempt=0)).artifact
    assert checked.passed and "scope_fence" in checked.bypassed
    writer.stages.driver.llm = FakeLLM([requisition_reply("approve")])
    review = (await stages.review(writer.ctx, ticket, checked, attempt=0)).artifact
    # A process commit forces the production admission's clean-rebase approval carry.
    c.fs.write(root / "tickets/process/note.md", b"process\n")
    await c.git.add(root, ["tickets/process/note.md"])
    await c.git.commit(root, "process")
    writer.queue.offer(ticket, attempt=0)
    [admitted] = await writer.queue.process()
    assert admitted.outcome == "ok", admitted.findings
    assert admitted.artifact.reviewed_sha == review.reviewed_sha == checked.produced_at_sha
    return c, writer, box, checked, admitted.artifact


async def escape_report(c, *, commit, paths=(), stem="repair"):
    from tempo import triage
    from dataclasses import replace
    from tests.test_inbox import arrival
    from tests.test_author import bug_reply
    arrival(c.repo, app_commit=commit, implicated_paths=list(paths))
    config = c.repo / "config.yaml"
    config.write_text(config.read_text() + "report_inbox: inbox\n")
    from tempo.config import load_config
    c = replace(c, config=load_config(None, cwd=c.repo))
    llm = FakeLLM([reply("author"), bug_reply(stem), requisition_reply("approve")])
    await triage.triage_pass(c, llm)
    box = Box(c.config.state_dir / "box", c.fs)
    [message] = box.messages()
    assert message.resolution == Resolution(kind="ticket", link=stem)
    assert message.report and message.report.custody.replay
    return c, message, llm


def attribution(c):
    return [e for e in c.journal.read() if e.type == EventType.EFFECT_COMPLETION
            and (e.key or "").startswith("escape-attribution/")]


def test_resolved_bug_escape_attribution(tmp_path):
    from tempo import triage
    from tempo.retro import production_evidence, project_window
    from tempo.scorecard import project_scorecard
    async def scenario():
        c, writer, box, checked, admitted = await escape_rig(tmp_path)
        # Later evidence, unfinished lifts, and mutable copies cannot replace admission custody.
        c.journal.append(EventType.EFFECT_INTENT, {"result": {"commit": "missing"}},
                         ticket="candidate", key="ticket-plane/candidate/0/checks")
        path = "tickets/candidate/checks.json"
        c.fs.write(c.repo / path, checked.model_copy(update={"produced_at_sha": "later",
            "reports": []}).model_dump_json().encode())
        await c.git.add(c.repo, [path])
        await c.git.commit(c.repo, "later attempt", only=[path])
        c.journal.append(EventType.EFFECT_COMPLETION,
            {"result": {"commit": await c.git.rev_parse(c.repo, "HEAD")}},
            ticket="candidate", key="ticket-plane/candidate/1/checks")
        c, message, llm = await escape_report(c, commit=admitted.commit, paths=["tempo/thing.py"])
        [effect] = attribution(c)
        assert triage.ESCAPE_ATTRIBUTION == "escape-attribution"
        assert effect.key == f"escape-attribution/{message.id}" and effect.ticket == "repair"
        result = effect.body["result"]
        assert set(result) == {"box_id", "bug_ticket", "app_commit", "matches", "confidence_note"}
        expected = sorted({r.code for r in checked.reports if r.verdict == "pass"
                           and r.code not in checked.bypassed} | {"review"})
        assert result["matches"] == [{"ticket": "candidate", "commit": admitted.commit,
            "reviewed_sha": admitted.reviewed_sha, "surfaces": expected}]
        assert "scope_fence" not in expected and "unknown" not in expected
        assert "Single-commit" in result["confidence_note"] and "narrowed" in result["confidence_note"]
        # Crossing a retro boundary does not require a current-window Check for an old admission.
        c.journal.append(EventType.EFFECT_COMPLETION, {"result": {}}, key="retro/1")
        other = message.model_copy(update={"id": "second-report"})
        await triage.attribute_escape(c, writer.stages.driver, other)
        window = project_window(c.journal.read())
        await production_evidence(window, c)
        assert all(r.escapes == 1 and r.evaluated_tickets == 0 for r in project_scorecard(window).surfaces)
        no_paths = message.model_copy(update={"id": "wrong-path", "report": message.report.model_copy(
            update={"report": message.report.report.model_copy(update={"implicated_paths": ["unrelated.py"]})})})
        result = await triage.attribute_escape(c, writer.stages.driver, no_paths)
        assert result["matches"] == [] and "unavailable" in result["confidence_note"]
        base = await c.git.rev_parse(c.repo, f"{admitted.commit}~1")
        end = await c.git.rev_parse(c.repo, "HEAD")
        ranged = message.model_copy(update={"id": "bounded-range", "report": message.report.model_copy(
            update={"report": message.report.report.model_copy(update={"app_commit": f"{base}..{end}",
                                                                       "implicated_paths": []})})})
        bounded = await triage.attribute_escape(c, writer.stages.driver, ranged)
        result_match = attribution(c)[0].body["result"]["matches"]
        assert bounded["matches"] == result_match
        assert "Bounded-range" in bounded["confidence_note"] and "without path narrowing" in bounded["confidence_note"]
        candidate = result_match[0]
        history = c.journal.read()
        # A trailer pair alone is not evidence, and an unfinished or later lift cannot prove a pass.
        missing = [e for e in history if e.type != EventType.EFFECT_COMPLETION or
                   not ((e.key or "").startswith("ticket-plane/candidate/0/") or
                        (e.key or "").startswith("llm/candidate/"))]
        assert await triage._passed_surfaces(c, candidate, missing) == []
        assert await triage._passed_surfaces(c, {**candidate, "reviewed_sha": "f" * 40}, history) == []
        unproven = {**candidate, "commit": await c.git.rev_parse(c.repo, "HEAD")}
        assert await triage._passed_surfaces(c, unproven, history) == []
        assert len(llm.requests) == 3
    asyncio.run(scenario())


def test_escape_attribution_retry(tmp_path, monkeypatch):
    from tempo import triage
    from tempo.effects import Effects
    from tempo.git import GitError
    async def scenario():
        c, writer, box, checked, admitted = await escape_rig(tmp_path)
        original = c.git.first_parent_squashes
        async def failure(*args):
            raise GitError(["git"], 1, "", "unreadable repository")
        with monkeypatch.context() as patch:
            patch.setattr(c.git, "first_parent_squashes", failure)
            c, message, llm = await escape_report(c, commit=admitted.commit)
        assert attribution(c) == []
        assert any(e.type == EventType.EFFECT_INTENT and e.key == f"escape-attribution/{message.id}"
                   for e in c.journal.read())
        diagnostic = (c.config.state_dir / "engine.log").read_text()
        assert "escape_attribution_incomplete" in diagnostic and "repair repository/evidence access" in diagnostic
        assert c.git.first_parent_squashes == original
        class Crash(BaseException):
            pass
        append = c.journal.append
        def crash(type_, body, **kwargs):
            if type_ == EventType.EFFECT_COMPLETION and (kwargs.get("key") or "").startswith("escape-attribution/"):
                raise Crash()
            return append(type_, body, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(c.journal, "append", crash)
            with pytest.raises(Crash):
                await triage.triage_pass(c, FakeLLM([]))
        assert attribution(c) == []
        await triage.triage_pass(c, FakeLLM([]))
        [completed] = attribution(c)
        with monkeypatch.context() as patch:
            patch.setattr(c.git, "first_parent_squashes", failure)
            await triage.triage_pass(c, FakeLLM([]))
        assert attribution(c) == [completed]
        assert len(llm.requests) == 3
        assert len([e for e in c.journal.read() if e.body.get("signal") == "author_invoked"]) == 1
        from tempo.scorecard import project_scorecard
        from tempo.retro import project_window
        assert all(r.escapes == 1 for r in project_scorecard(project_window(c.journal.read())).surfaces)
    asyncio.run(scenario())


def test_escape_unattributed_reports(tmp_path):
    from tempo import triage
    async def scenario():
        c, writer, box, checked, admitted = await escape_rig(tmp_path)
        c, message, _ = await escape_report(c, commit="f" * 40)
        assert attribution(c)[0].body["result"]["matches"] == []
        assert attribution(c)[0].body["result"]["confidence_note"]
        missing = message.model_copy(update={"id": "missing-report", "report": None})
        result = await triage.attribute_escape(c, writer.stages.driver, missing)
        assert result["matches"] == [] and "missing" in result["confidence_note"]
        # Missing admission and absent Author completion provide no surface evidence.
        for stem in ("missing", "candidate"):
            item = message.model_copy(update={"id": stem, "resolution": Resolution(kind="ticket", link=stem)})
            result = await triage.attribute_escape(c, writer.stages.driver, item)
            assert result["matches"] == [] and result["confidence_note"]
        before = len(attribution(c))
        for kind in ("decision", "tombstone"):
            id, _ = box.enqueue(message_class="bug_report", origin=kind, summary=kind,
                                bug_origin="player", has_repro=False)
            box.resolve(id, Resolution(kind=kind, link=kind))
        id, _ = box.enqueue(message_class="suggestion", origin="non-bug", summary="non bug")
        box.resolve(id, Resolution(kind="ticket", link="repair"))
        id, _ = box.enqueue(message_class="bug_report", origin="pending", summary="pending bug",
                            bug_origin="player", has_repro=False)
        await triage.triage_pass(c, FakeLLM(["{}"] * 7))
        assert box.get(id).status == "pending" and len(attribution(c)) == before
        assert "triage" not in __import__("tempo.drain", fromlist=["drain"]).drain.__code__.co_names
    asyncio.run(scenario())
