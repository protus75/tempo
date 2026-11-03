"""Queue admission with injected seams and disposable real repositories."""

import ast
import asyncio
import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from tempo import control, merge, stages
from tempo.control import PauseConsumer
from tempo.artifacts import StageResult
from tempo.box import BOX_DIR, Box
from tempo.config import MechanicalCheck, RegenerateStrategy, UnionStrategy, load_config, snapshot_config
from tempo.driver import Driver
from tempo.git import Git
from tempo.journal import EventType
from tempo.mergequeue import CONFLICT_FACTS, RED_STREAK, TREE_MISMATCH, ConflictHandoff, MergeQueue, TreeMismatch
from tempo.seams import ExecutableNotFound, LocalFileSystem, SubprocessExec
from tempo.stages import ApprovedInvoice, CommandResult, StageContext, render_review
from tempo.tickets import validate_ticket
from tests.test_stages import adopt_conduct, CONFIG, ENV, SECRET, SPECS, STEM, TICKET

run = asyncio.run


@pytest.fixture
def ctx(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tempo").mkdir(parents=True)
    (repo / "tempo/thing.py").write_text("ok base\n")
    (repo / "tempo/other.py").write_text("")
    (repo / ".gitignore").write_text(".tempo/\n")
    (repo / "config.yaml").write_text(CONFIG)
    conduct = adopt_conduct(repo)
    config = load_config(None, cwd=repo)
    exec_ = SubprocessExec()
    driver = Driver.from_config(config, clock=lambda: datetime(2026, 10, 6, tzinfo=UTC))
    context = StageContext(repo, config, ENV, exec_, Git(exec_, env=dict(ENV), timeout=30),
                           LocalFileSystem(), driver, SPECS)
    async def init():
        await context.git.init(repo, branch="main")
        await context.git.add(repo, [".gitignore", "config.yaml", "tempo", *conduct])
        await context.git.commit(repo, "base")
    run(init())
    return context


async def commit(ctx, root, paths, message="work"):
    await ctx.git.add(root, paths)
    await ctx.git.commit(root, message)


async def approve(ctx, ticket, sha=None):
    sha = sha or await ctx.git.rev_parse(ctx.repo, ticket.stem)
    review = ApprovedInvoice(produced_by_spec_version=1, produced_at_sha=sha, stem=ticket.stem,
                             reviewed_sha=sha, verdict="approve", summary="approved", findings=[],
                             spec_version="1.0", provider="fake", model="fake")
    path = f"tickets/{ticket.stem}/review.md"
    ctx.fs.write(ctx.repo / path, render_review(review).encode())
    await commit(ctx, ctx.repo, [path], "review")


async def ready(ctx, stem=STEM, *, changes=None, fence=("tempo/thing.py",), verification=(("true",),),
                priority="P2", lifted=False, commits=None):
    text = TICKET.format(bypass="").replace("priority: P2", f"priority: {priority}")
    text = text.replace("## Scope fence\n- tempo/thing.py", "## Scope fence\n" + "\n".join("- " + f for f in fence))
    root = ctx.repo / "tickets" / stem
    ctx.fs.write(root / "ticket.md", text.encode())
    record = "".join(f"## {name}\n\n{'ok' if name == 'Outcome' else 'none'}\n\n"
                     for name in stages.RUN_RECORD_SECTIONS)
    ctx.fs.write(root / "run.md", record.encode())
    await commit(ctx, ctx.repo, [f"tickets/{stem}"], "ticket and run record")
    ticket = dataclasses.replace(validate_ticket(stem, text, ctx.repo), verification=verification)
    await ctx.git.worktree_add(ctx.repo, ctx.worktree(stem), stem, "main")
    for batch in commits or [changes or {"tempo/thing.py": f"ok {stem}\n"}]:
        for path, data in batch.items():
            ctx.fs.write(ctx.worktree(stem) / path, data.encode())
        await commit(ctx, ctx.worktree(stem), list(batch))
    await approve(ctx, ticket)
    if lifted:
        (ctx.worktree(stem) / "tickets" / stem / "run.md").unlink()
    return ticket


async def admission(ctx, ticket, *, queue=None, attempt=0):
    queue = queue or MergeQueue(ctx, escalate=lambda _: None)
    queue.offer(ticket, attempt=attempt)
    [result] = await queue.process()
    return result


def facts(ctx):
    return [e for e in ctx.driver.journal.read() if e.body.get("kind") == CONFLICT_FACTS]


async def unchanged(ctx, ticket, before, result, code):
    assert isinstance(result, StageResult) and result.outcome == "gate_failed" and result.artifact is None
    assert code in {f.code for f in result.findings}
    assert await ctx.git.rev_parse(ctx.repo, "main") == before
    assert await ctx.git.rev_parse(ctx.repo, ticket.stem)
    assert ctx.worktree(ticket.stem).exists()
    assert not any(e.type == EventType.STATE_TRANSITION for e in ctx.driver.journal.read())


def host(code, *, trigger="always", severity="hard", argv=None):
    return MechanicalCheck(code=code, argv=argv or ["host", code], trigger=trigger, severity=severity)


def scripted(ctx, monkeypatch, results):
    original = ctx.exec_.run
    calls = []
    async def execute(argv, **kw):
        if argv[0] == "host":
            calls.append((list(argv), kw))
            value = results.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value
        return await original(argv, **kw)
    monkeypatch.setattr(ctx.exec_, "run", execute)
    return calls


def admission_control(ctx, lifecycle="admission-life"):
    return PauseConsumer(journal=ctx.driver.journal, lifecycle_id=lifecycle,
                         state_dir=ctx.config.state_dir, fs=ctx.fs, sleep=lambda _: asyncio.Event().wait(),
                         files=lambda: (ctx.config.state_dir / "control/inbox").glob("*"),
                         read=Path.read_bytes)


def request(consumer, request_id, *, hold=None, lifecycle=None, verb="resume"):
    control.publish_request(consumer.state_dir, control.ControlRequest(
        request_id, lifecycle or consumer.projection.lifecycle_id, verb, hold), consumer.fs)
    consumer.inbox.consume()


def accept_resume(queue, request_id="resume"):
    request(queue.control, request_id, hold=queue.hold_id)
    assert queue.hold_id in queue.control.projection.released_hold_ids
    assert queue.paused


def test_serial_priority_age_order(ctx, monkeypatch):
    async def scenario():
        offers = [(stem, await ready(ctx, stem, priority=priority,
                   changes={f"tempo/{stem}.py": "ok"}, fence=(f"tempo/{stem}.py",)))
                  for stem, priority in [("first", "P2"), ("a-new", "P2"), ("z-old", "P2"),
                                         ("missing", "P2"), ("tie-b", "P2"), ("tie-a", "P2"), ("urgent", "P0")]]
        for stem, day in [("a-new", 3), ("z-old", 1), ("z-old", 9), ("tie-b", 2), ("tie-a", 2)]:
            monkeypatch.setattr(ctx.driver.journal, "_clock", lambda day=day: datetime(2026, 10, day, tzinfo=UTC))
            ctx.driver.journal.append(EventType.SIGNAL, {"signal": "ticket_intake"}, ticket=stem)
        entered, release = asyncio.Event(), asyncio.Event()
        original = stages.gather_safety_evidence
        order = []
        async def hold(context, ticket, claimed):
            # Full evidence also calls this seam; only count the safety entry per stem.
            if ticket.stem not in order:
                order.append(ticket.stem)
                if ticket.stem == "first":
                    entered.set()
                    await release.wait()
            return await original(context, ticket, claimed)
        monkeypatch.setattr(stages, "gather_safety_evidence", hold)
        queue = MergeQueue(ctx, escalate=lambda _: None)
        queue.offer(offers[0][1], attempt=0)
        first = asyncio.create_task(queue.process())
        await entered.wait()
        for _, ticket in offers[1:]:
            queue.offer(ticket, attempt=0)
        second = asyncio.create_task(queue.process())
        assert order == ["first"] and queue.active == "first"
        release.set()
        results = [r for batch in await asyncio.gather(first, second) for r in batch]
        expected = ["first", "urgent", "z-old", "tie-a", "tie-b", "a-new", "missing"]
        assert order == expected and [r.artifact.stem for r in results] == expected
        assert all(r.outcome == "ok" for r in results) and queue.active is None and not queue.pending
    run(scenario())


def test_restore_rebase_safety_integration_order(ctx, monkeypatch):
    async def scenario():
        ticket = await ready(ctx, lifted=True)
        ctx.fs.write(ctx.repo / "tempo/other.py", b"moved\n")
        await commit(ctx, ctx.repo, ["tempo/other.py"], "main moves")
        events = []
        for name in ("restore", "rebase_stop_at_conflict", "merge_squash"):
            original = getattr(ctx.git, name)
            async def record(*args, _name=name, _original=original, **kw):
                events.append(_name)
                if _name == "restore":
                    assert kw["source"] == ticket.stem
                if _name == "rebase_stop_at_conflict":
                    assert await ctx.git.status_porcelain(ctx.worktree(ticket.stem)) == ""
                return await _original(*args, **kw)
            monkeypatch.setattr(ctx.git, name, record)
        for name in ("gather_safety_evidence", "gather_evidence"):
            original = getattr(stages, name)
            async def record(*args, _name=name, _original=original, **kw):
                events.append(_name)
                assert (ctx.worktree(ticket.stem) / "tempo/other.py").read_text() == "moved\n"
                return await _original(*args, **kw)
            monkeypatch.setattr(stages, name, record)
        result = await admission(ctx, ticket)
        assert result.outcome == "ok", result.findings
        assert events == ["restore", "rebase_stop_at_conflict", "gather_safety_evidence",
                          "gather_evidence", "gather_safety_evidence", "merge_squash"]
    run(scenario())


@pytest.mark.parametrize("failure", ["scope_fence", "run_record", "post_rebase_regate", "correctness_review",
                                    "ticket_schema", "fast", "rebase"])
def test_safety_failure_skips_integration(ctx, monkeypatch, failure):
    async def scenario():
        ticket = await ready(ctx, verification=(("no-verification-allowed",),))
        if failure == "rebase":
            ctx.fs.write(ctx.worktree(ticket.stem) / "tempo/thing.py", b"dirty")
        elif failure in {"scope_fence", "post_rebase_regate"}:
            path = "tempo/other.py" if failure == "scope_fence" else f"tickets/{ticket.stem}/note.md"
            ctx.fs.write(ctx.worktree(ticket.stem) / path, b"change")
            await commit(ctx, ctx.worktree(ticket.stem), [path])
            await approve(ctx, ticket)
        elif failure == "run_record":
            (ctx.repo / "tickets" / ticket.stem / "run.md").unlink()
            await commit(ctx, ctx.repo, [f"tickets/{ticket.stem}/run.md"])
        elif failure in {"correctness_review", "ticket_schema"}:
            name = "review.md" if failure == "correctness_review" else "ticket.md"
            ctx.fs.write(ctx.repo / "tickets" / ticket.stem / name, b"invalid")
            await commit(ctx, ctx.repo, [f"tickets/{ticket.stem}/{name}"])
        else:
            ctx.config.review.mechanical = [host("fast")]
            ctx.config.merge.safety_checks = ["fast"]
            scripted(ctx, monkeypatch, [(1, "", "red")])
        original = stages.gather_safety_evidence
        calls = []
        async def safety(*args):
            calls.append(args)
            return await original(*args)
        async def forbidden(*args, **kw):
            pytest.fail("safety refusal ran Verification")
        monkeypatch.setattr(stages, "gather_safety_evidence", safety)
        monkeypatch.setattr(stages, "gather_evidence", forbidden)
        before = await ctx.git.rev_parse(ctx.repo, "main")
        result = await admission(ctx, ticket)
        await unchanged(ctx, ticket, before, result, "post_rebase_regate" if failure == "rebase" else failure)
        assert len(calls) == (0 if failure == "rebase" else 1)
        assert not (ctx.config.worktree_root / ".base").exists()
        assert facts(ctx)[0].body["integration_red_paths"] == []
    run(scenario())


@pytest.mark.parametrize("base_red", [False, True])
def test_verification_uses_rebased_worktree(ctx, base_red):
    async def scenario():
        argv = ("grep", "-q", "missing", "tempo/thing.py") if base_red else ("grep", "-q", "moved", "tempo/other.py")
        ticket = await ready(ctx, verification=(argv,))
        ctx.fs.write(ctx.repo / "tempo/other.py", b"moved\n")
        await commit(ctx, ctx.repo, ["tempo/other.py"])
        result = await admission(ctx, ticket)
        assert result.outcome == "ok", result.findings
        spool = ctx.config.state_dir / "spools" / ticket.stem / "0/merge-integration"
        assert (spool / "verify-01.txt").is_file()
        assert (spool / "verify-01-base.txt").exists() == base_red
        messages = Box(ctx.config.state_dir / BOX_DIR, ctx.fs).messages()
        assert bool(messages) == base_red
        assert not (ctx.config.worktree_root / ".base" / ticket.stem).exists()
    run(scenario())


def test_always_hard_host_checks_run_before_squash(ctx, monkeypatch):
    async def scenario():
        ticket = await ready(ctx)
        ctx.config.review.mechanical = [host("first"), host("skip", severity="soft"), host("last")]
        ctx.config.merge.safety_checks = ["last", "first"]
        calls = scripted(ctx, monkeypatch, [(0, "", "")] * 4)
        original = ctx.git.merge_squash
        async def squash(*args):
            assert [a[1] for a, _ in calls] == ["first", "last", "first", "last"]
            return await original(*args)
        monkeypatch.setattr(ctx.git, "merge_squash", squash)
        result = await admission(ctx, ticket)
        assert result.outcome == "ok"
        spools = ctx.config.state_dir / "spools" / ticket.stem / "0"
        assert sorted(p.relative_to(spools).as_posix() for p in spools.rglob("host-*")) == [
            "merge-integration/host-01.txt", "merge-integration/host-03.txt",
            "merge-safety/host-01.txt", "merge-safety/host-03.txt"]
    run(scenario())


@pytest.mark.parametrize("safety", [True, False])
@pytest.mark.parametrize("argv,rc", [(["true"], 0), (["false"], 1)])
def test_snapshot_configured_command_records_list_argv(ctx, safety, argv, rc):
    async def scenario():
        ticket = await ready(ctx)
        ctx.config.review.mechanical = [host("configured", argv=argv)]
        ctx.config.merge.safety_checks = ["configured"]
        snapshot = snapshot_config(ctx.config)
        assert isinstance(snapshot.review.mechanical[0].argv, tuple)
        queue = MergeQueue(dataclasses.replace(ctx, config=snapshot), escalate=lambda _: None)
        [result], [report] = await queue.host_checks(ticket, attempt=0, safety=safety)
        assert isinstance(result, CommandResult)
        assert isinstance(result.argv, list) and result.argv == argv
        assert result.rc == rc
        assert report.verdict == ("pass" if rc == 0 else "fail")
    run(scenario())


@pytest.mark.parametrize("safety", [True, False])
def test_host_check_environment_timeout_and_redaction(ctx, monkeypatch, safety):
    from tempo.redact import Redactor
    ctx.driver.redactor = ctx.driver.spool._redactor = Redactor({"FAKE_KEY": SECRET})

    async def scenario():
        ticket = await ready(ctx)
        ctx.config.review.mechanical = [host("../unsafe-code", argv=["host", SECRET])]
        ctx.config.merge.safety_checks = ["../unsafe-code"]
        calls = scripted(ctx, monkeypatch, [(7, "x" * 2100 + SECRET, SECRET)])
        queue = MergeQueue(ctx, escalate=lambda _: None)
        results, reports = await queue.host_checks(ticket, attempt=4, safety=safety)
        argv, kw = calls[0]
        assert kw["cwd"] == ctx.worktree(ticket.stem) and kw["timeout"] == ticket.stuck_minutes * 60.0
        assert kw["env"]["PATH"] == ENV["PATH"]
        assert not results[0].base_red and SECRET not in results[0].tail
        assert len(results[0].tail) <= stages.OUTPUT_TAIL_CHARS
        assert SECRET not in reports[0].findings[0].message and reports[0].code == "../unsafe-code"
        name = "safety" if safety else "integration"
        spool = ctx.config.state_dir / "spools" / ticket.stem / f"4/merge-{name}/host-01.txt"
        text = spool.read_text()
        assert SECRET not in text and "[exit 7]" in text and "--- stdout" in text and "--- stderr" in text
        assert not facts(ctx)
    run(scenario())


@pytest.mark.parametrize("failure,rc,road", [
    ((9, "bad output", "diagnostic"), 9, "make the configured command exit 0"),
    (TimeoutError(), None, "make the command finish within the ticket's stuck budget"),
    (ExecutableNotFound("host"), None, "install the executable or correct argv/PATH")])
@pytest.mark.parametrize("safety", [True, False])
def test_host_check_failure_reports(ctx, monkeypatch, failure, rc, road, safety):
    async def scenario():
        ticket = await ready(ctx)
        ctx.config.review.mechanical = [host("configured-code")]
        ctx.config.merge.safety_checks = ["configured-code"] if safety else []
        scripted(ctx, monkeypatch, [failure])
        queue = MergeQueue(ctx, escalate=lambda _: None)
        captured = []
        original = queue._command
        async def command(*args):
            result, report = await original(*args)
            captured.append((result, report))
            return result, report
        monkeypatch.setattr(queue, "_command", command)
        before = await ctx.git.rev_parse(ctx.repo, "main")
        result = await admission(ctx, ticket, queue=queue)
        await unchanged(ctx, ticket, before, result, "configured-code")
        command_result, report = captured[0]
        assert command_result.rc == rc and not command_result.base_red
        assert report.verdict == "fail" and not report.autofix_applied
        [finding] = report.findings
        assert finding.code == report.code == "configured-code"
        assert finding.path is finding.line is finding.kind is None
        assert finding.paved_road.startswith(road) and "host" in finding.message
        if rc is None:
            assert "timed out" in finding.message or "executable not found" in finding.message
        else:
            assert "bad output" in finding.message and "diagnostic" in finding.message
    run(scenario())


@pytest.mark.parametrize("designation", ["missing", "ambiguous", "soft", "path"])
def test_host_check_selection_fails_closed(ctx, monkeypatch, designation):
    async def scenario():
        ticket = await ready(ctx)
        entries = {"missing": [], "ambiguous": [host("fast"), host("fast")],
                   "soft": [host("fast", severity="soft")], "path": [host("fast", trigger=["unrelated/"])]}
        ctx.config.review.mechanical = entries[designation] + [host("soft", severity="soft"),
                                                              host("path", trigger=["tempo/"])]
        ctx.config.merge.safety_checks = ["fast"]
        calls = scripted(ctx, monkeypatch, [(0, "", "")])
        queue = MergeQueue(ctx, escalate=lambda _: None)
        before = await ctx.git.rev_parse(ctx.repo, "main")
        result = await admission(ctx, ticket, queue=queue)
        if designation == "path":
            assert result.outcome == "ok"
            assert [a[1] for a, _ in calls] == ["fast"]
        else:
            await unchanged(ctx, ticket, before, result, "post_rebase_regate")
            assert calls == []
            assert "fast" in result.findings[0].message
            assert result.findings[0].paved_road == (
                "declare exactly one hard review.mechanical entry for this code, or remove it from merge.safety_checks")
    run(scenario())


def test_host_checks_have_no_base_red_exemption(ctx, monkeypatch):
    async def scenario():
        ticket = await ready(ctx, verification=(("grep", "-q", "absent", "tempo/thing.py"),))
        ctx.config.review.mechanical = [host("host-red")]
        calls = scripted(ctx, monkeypatch, [(1, "", "red")])
        before = await ctx.git.rev_parse(ctx.repo, "main")
        result = await admission(ctx, ticket)
        await unchanged(ctx, ticket, before, result, "host-red")
        assert len(calls) == 1 and calls[0][1]["cwd"] == ctx.worktree(ticket.stem)
        assert "verification" not in {f.code for f in result.findings}
        assert Box(ctx.config.state_dir / BOX_DIR, ctx.fs).messages()[0].outcome == "base_red"
    run(scenario())


def test_semantic_conflict_never_moves_main(ctx):
    async def scenario():
        command = ["python3", "-c",
            "from pathlib import Path; import sys; sys.exit(int('add-thing' in Path('tempo/thing.py').read_text() and bool(Path('tempo/other.py').read_text())))"]
        ctx.config.review.mechanical = [host("combined", argv=command)]
        ticket = await ready(ctx)
        other = await ready(ctx, "other", priority="P1", changes={"tempo/other.py": "moved\n"},
                            fence=("tempo/other.py",))
        for candidate in (ticket, other):
            assert (await ctx.exec_.run(command, cwd=ctx.worktree(candidate.stem),
                    env=dict(ctx.env), timeout=30))[0] == 0
        next_ticket = await ready(ctx, "next", changes={"tempo/new.py": "green\n"}, fence=("tempo/new.py",))
        queue = MergeQueue(ctx, escalate=lambda _: None)
        for candidate in (ticket, other, next_ticket):
            queue.offer(candidate, attempt=0)
        results = await queue.process()
        assert [r.outcome for r in results] == ["ok", "gate_failed", "ok"]
        assert results[0].artifact.stem == "other" and results[2].artifact.stem == "next"
        assert ctx.worktree(ticket.stem).exists()
        assert (ctx.repo / "tempo/thing.py").read_text() == "ok base\n"
        assert (ctx.repo / "tempo/other.py").read_text() == "moved\n"
        assert (ctx.repo / "tempo/new.py").is_file()
        assert facts(ctx)[1].body["integration_red_paths"] == ["tempo/thing.py"]
    run(scenario())


@pytest.mark.parametrize("approval", ["valid", "wrong", "missing", "malformed", "snag"])
def test_clean_rebase_carries_pinned_approval(ctx, approval):
    async def scenario():
        ticket = await ready(ctx)
        original = await ctx.git.rev_parse(ctx.repo, ticket.stem)
        path = ctx.repo / "tickets" / ticket.stem / "review.md"
        if approval == "wrong":
            await approve(ctx, ticket, sha="wrong")
        elif approval == "missing":
            path.unlink()
            await commit(ctx, ctx.repo, [str(path.relative_to(ctx.repo))])
        elif approval != "valid":
            text = path.read_text().replace('"verdict": "approve"', '"verdict": "snag"') if approval == "snag" else "bad"
            ctx.fs.write(path, text.encode())
            await commit(ctx, ctx.repo, [str(path.relative_to(ctx.repo))])
        ctx.fs.write(ctx.repo / "tempo/other.py", b"moved\n")
        await commit(ctx, ctx.repo, ["tempo/other.py"])
        before = await ctx.git.rev_parse(ctx.repo, "main")
        result = await admission(ctx, ticket)
        if approval == "valid":
            assert result.outcome == "ok" and result.artifact.reviewed_sha == original
        else:
            await unchanged(ctx, ticket, before, result, "correctness_review")
    run(scenario())


def test_squash_tree_trailers_and_retirement(ctx, monkeypatch):
    async def scenario():
        ticket = await ready(ctx, lifted=True)
        reviewed = await ctx.git.rev_parse(ctx.repo, ticket.stem)
        trees = []
        original = ctx.git.rev_parse
        async def recording(root, ref):
            sha = await original(root, ref)
            if ref.endswith("^{tree}"):
                trees.append((root, ref, sha))
            return sha
        monkeypatch.setattr(ctx.git, "rev_parse", recording)
        result = await admission(ctx, ticket, attempt=7)
        assert result.outcome == "ok", result.findings
        assert trees[0][1] == "HEAD^{tree}" and trees[1][1] == "main^{tree}" and trees[0][2] == trees[1][2]
        assert result.artifact.commit == await original(ctx.repo, "main")
        assert result.artifact.reviewed_sha == reviewed and result.artifact.produced_at_sha == result.artifact.commit
        message = await ctx.git._run(ctx.repo, "log", "-1", "--format=%B", "main")
        assert message.startswith(f"tempo({ticket.stem}): `thing.py` holds the word ok.")
        assert f"tempo-ticket: {ticket.stem}" in message and f"tempo-reviewed-sha: {reviewed}" in message
        paths = await ctx.git._run(ctx.repo, "show", "--format=", "--name-only", "main")
        assert paths.splitlines() == ["tempo/thing.py"]
        events = ctx.driver.journal.read()
        merged = [e for e in events if e.type == EventType.STATE_TRANSITION]
        assert len(merged) == 1 and merged[0].body == {
            "to": "merged", "commit": result.artifact.commit, "reviewed_sha": reviewed}
        assert len([e for e in events if e.type == EventType.EFFECT_COMPLETION and e.key == f"merge/{ticket.stem}/7"]) == 1
        assert not ctx.worktree(ticket.stem).exists()
        assert not (await ctx.git._run(ctx.repo, "branch", "--list", ticket.stem)).strip()
    run(scenario())


def test_escape_squash_trailer_identity(ctx):
    async def scenario():
        ticket = await ready(ctx)
        reviewed = await ctx.git.rev_parse(ctx.repo, ticket.stem)
        ctx.fs.write(ctx.repo / "tempo/other.py", b"main moved\n")
        await commit(ctx, ctx.repo, ["tempo/other.py"])
        result = await admission(ctx, ticket)
        assert result.outcome == "ok", result.findings
        assert result.artifact.reviewed_sha == reviewed
        [candidate] = await ctx.git.first_parent_squashes(ctx.repo, result.artifact.commit)
        assert candidate == {"ticket": ticket.stem, "commit": result.artifact.commit,
                             "reviewed_sha": reviewed, "paths": ["tempo/thing.py"]}
        message = await ctx.git._run(ctx.repo, "show", "-s", "--format=%B", result.artifact.commit)
        assert message.count("tempo-ticket:") == message.count("tempo-reviewed-sha:") == 1
        terminal, = [e for e in ctx.driver.journal.read() if e.type == EventType.STATE_TRANSITION]
        assert terminal.body == {"to": "merged", "commit": candidate["commit"], "reviewed_sha": reviewed}
    run(scenario())


def test_tree_mismatch_escalates_and_pauses(ctx, monkeypatch):
    async def scenario():
        ticket = await ready(ctx)
        before = await ctx.git.rev_parse(ctx.repo, "main")
        original = ctx.git.rev_parse
        async def mismatch(root, ref):
            return "mismatch" if ref == "main^{tree}" else await original(root, ref)
        monkeypatch.setattr(ctx.git, "rev_parse", mismatch)
        escalations = []
        queue = MergeQueue(ctx, escalate=escalations.append, control=admission_control(ctx))
        queue.offer(ticket, attempt=0)
        with pytest.raises(TreeMismatch):
            await queue.process()
        assert queue.paused and queue.active is None and ctx.worktree(ticket.stem).exists()
        assert await original(ctx.repo, "main") != before
        assert len(escalations) == 1 and escalations[0].key is None and escalations[0].ticket == ticket.stem
        assert escalations[0].body == {"kind": TREE_MISMATCH,
            "checked_tree": await original(ctx.worktree(ticket.stem), "HEAD^{tree}"), "main_tree": "mismatch", "hold_id": queue.hold_id}
        assert await queue.process() == []
        accept_resume(queue)
        await queue.process()
        assert not queue.paused
    run(scenario())


def test_distinct_red_streak_pause_and_resume(ctx, monkeypatch):
    async def scenario():
        tickets = [await ready(ctx, stem, changes={f"tempo/{stem}.py": "ok"},
                               fence=(f"tempo/{stem}.py",)) for stem in ["one", "two", "three", "green"]]
        ctx.config.review.mechanical = [host("integration")]
        calls = scripted(ctx, monkeypatch, [(1, "", "red")] * 4 + [(0, "", "")] * 2)
        escalations = []
        queue = MergeQueue(ctx, escalate=escalations.append, control=admission_control(ctx))
        await admission(ctx, tickets[0], queue=queue)
        await approve(ctx, tickets[0])
        await admission(ctx, tickets[0], queue=queue, attempt=1)
        assert queue.red_stems == ["one"]
        # Safety failures neither reset nor increment the integration streak.
        bad = dataclasses.replace(tickets[1], scope_fence=("elsewhere",))
        await admission(ctx, bad, queue=queue)
        assert queue.red_stems == ["one"] and len(calls) == 2
        await approve(ctx, tickets[1])
        await admission(ctx, tickets[1], queue=queue)
        queue.offer(tickets[2], attempt=0)
        queue.offer(tickets[3], attempt=0)
        # Force deterministic offer age so three precedes green.
        ctx.driver.journal.append(EventType.SIGNAL, {"signal": "ticket_intake"}, ticket="three")
        [red] = await queue.process()
        assert red.outcome == "gate_failed" and queue.paused and "green" in queue.pending
        assert len(escalations) == 1 and escalations[0].body == {
            "kind": RED_STREAK, "stems": ["one", "two", "three"], "limit": 3, "hold_id": queue.hold_id}
        assert escalations[0].ticket == "three" and escalations[0].key is None
        assert await queue.process() == []
        accept_resume(queue)
        [green] = await queue.process()
        assert not queue.red_stems and not queue.paused
        assert green.outcome == "ok" and not queue.red_stems
        # Green also resets a sub-limit streak without requiring resume.
        queue.red_stems.append("earlier")
        again = await ready(ctx, "again", changes={"tempo/again.py": "ok"}, fence=("tempo/again.py",))
        assert (await admission(ctx, again, queue=queue)).outcome == "ok" and not queue.red_stems
    run(scenario())


async def conflict(ctx, *, strategy="union", generator=None, fence=("tempo/thing.py",), repeat=False):
    generated = "tempo/generated.txt"
    paths = [generated, "tempo/generated-two.txt"] if repeat else [generated]
    for path in paths:
        ctx.fs.write(ctx.repo / path, b"base\n")
    await commit(ctx, ctx.repo, paths)
    ticket = await ready(ctx, fence=fence, commits=[
        {"tempo/thing.py": "ok work\n", generated: "base\nbranch-one\n"},
        *([{paths[1]: "base\nbranch-two\n"}] if repeat else [])])
    for path in paths:
        ctx.fs.write(ctx.repo / path, b"base\nmain\n")
    await commit(ctx, ctx.repo, paths, "main conflicts")
    if strategy == "union":
        ctx.config.merge.strategies = [UnionStrategy(paths=paths, strategy="union")]
    elif strategy == "regenerate":
        ctx.config.merge.strategies = [RegenerateStrategy(paths=paths, strategy="regenerate",
            argv=generator or ["python3", "-c", "from pathlib import Path; Path('tempo/generated.txt').write_text('regenerated\\n')"])]
    return ticket, generated


@pytest.mark.parametrize("strategy,repeat", [("union", False), ("union", True), ("regenerate", False)])
def test_declared_union_and_regenerate_resolve(ctx, monkeypatch, strategy, repeat):
    async def scenario():
        ticket, path = await conflict(ctx, strategy=strategy, repeat=repeat)
        continued = []
        original = ctx.git.rebase_continue
        async def continue_rebase(root):
            continued.append(root)
            return await original(root)
        monkeypatch.setattr(ctx.git, "rebase_continue", continue_rebase)
        result = await admission(ctx, ticket)
        assert result.outcome == "ok", result.findings
        text = (ctx.repo / path).read_text()
        assert text == ("regenerated\n" if strategy == "regenerate" else "base\nmain\nbranch-one\n")
        paths = [path]
        if repeat:
            paths.append("tempo/generated-two.txt")
            assert (ctx.repo / paths[1]).read_text() == "base\nmain\nbranch-two\n"
        assert len(continued) == (2 if repeat else 1)
        [event] = facts(ctx)
        assert event.body == {"kind": CONFLICT_FACTS, "conflicted_paths": sorted(paths),
                              "resolving_rung": "mechanical", "strategy_paths": sorted(paths), "integration_red_paths": []}
    run(scenario())


@pytest.mark.parametrize("failure", ["undeclared", "ambiguous", "owned"])
def test_strategy_ownership_and_matching_fail_closed(ctx, failure):
    async def scenario():
        fence = ("tempo",) if failure == "owned" else ("tempo/thing.py",)
        ticket, path = await conflict(ctx, strategy="regenerate" if failure == "owned" else "union", fence=fence)
        if failure == "undeclared":
            ctx.config.merge.strategies = []
        elif failure == "ambiguous":
            ctx.config.merge.strategies.append(UnionStrategy(paths=["tempo"], strategy="union"))
        before = await ctx.git.rev_parse(ctx.repo, "main")
        reviewed = await ctx.git.rev_parse(ctx.repo, ticket.stem)
        result = await admission(ctx, ticket)
        assert isinstance(result, ConflictHandoff) and result.approval_invalidated
        assert result.conflicted_paths == [path] and result.reviewed_sha == reviewed
        assert result.findings[0].path == path and result.findings[0].paved_road
        assert await ctx.git.rev_parse(ctx.repo, "main") == before
        assert await ctx.git.rev_parse(ctx.repo, ticket.stem) == reviewed
        assert await ctx.git.conflicted_paths(ctx.worktree(ticket.stem)) == []
        assert await ctx.git.status_porcelain(ctx.worktree(ticket.stem)) == ""
    run(scenario())


def test_strategy_resolution_still_regates(ctx, monkeypatch):
    async def scenario():
        ticket, path = await conflict(ctx)
        gates = []
        for gate in stages.CHECK_GATES + merge.MERGE_GATES:
            original = gate.check
            def record(*args, _original=original, _code=gate.code):
                gates.append(_code)
                return _original(*args)
            monkeypatch.setattr(gate, "check", record)
        ticket = dataclasses.replace(ticket, verification=(("grep", "-q", "ok", "tempo/thing.py"),))
        ctx.config.review.mechanical = [host("combined", argv=["grep", "-q", "forbidden", path])]
        before = await ctx.git.rev_parse(ctx.repo, "main")
        result = await admission(ctx, ticket)
        await unchanged(ctx, ticket, before, result, "combined")
        assert gates == ["scope_fence", "run_record", "ticket_schema", "post_rebase_regate",
                         "correctness_review", "scope_fence", "verification",
                         "run_record", "diff_budget", "core_drift", "bug_evidence", "ticket_schema", "post_rebase_regate",
                         "correctness_review"]
        spool = ctx.config.state_dir / "spools" / ticket.stem / "0/merge-integration/verify-01.txt"
        assert spool.is_file() and "exit 0" in spool.read_text()
        assert facts(ctx)[0].body == {"kind": CONFLICT_FACTS, "conflicted_paths": [path],
            "resolving_rung": "mechanical", "strategy_paths": [path],
            "integration_red_paths": ["tempo/generated.txt", "tempo/thing.py"]}
    run(scenario())


@pytest.mark.parametrize("failure", ["generator", "undeclared", "nonappend"])
def test_unresolved_conflict_returns_typed_handoff_after_abort(ctx, failure):
    async def scenario():
        ticket, path = await conflict(ctx, strategy="regenerate" if failure == "generator" else "union",
                                      generator=["false"])
        if failure == "undeclared":
            ctx.config.merge.strategies = []
        elif failure == "nonappend":
            ctx.fs.write(ctx.repo / path, b"rewritten\n")
            await commit(ctx, ctx.repo, [path])
        reviewed, before = await ctx.git.rev_parse(ctx.repo, ticket.stem), await ctx.git.rev_parse(ctx.repo, "main")
        result = await admission(ctx, ticket)
        assert isinstance(result, ConflictHandoff)
        assert result.model_dump().keys() == {"stem", "reviewed_sha", "conflicted_paths", "findings", "approval_invalidated"}
        assert result.stem == ticket.stem and result.reviewed_sha == reviewed
        assert result.conflicted_paths == [path] and result.approval_invalidated is True
        with pytest.raises(ValidationError):
            result.approval_invalidated = False
        assert await ctx.git.rev_parse(ctx.repo, "main") == before
        assert await ctx.git.rev_parse(ctx.repo, ticket.stem) == reviewed
        assert await ctx.git.status_porcelain(ctx.worktree(ticket.stem)) == ""
        assert facts(ctx)[0].body["resolving_rung"] == "rework"
        assert not any(e.type == EventType.STATE_TRANSITION for e in ctx.driver.journal.read())
    run(scenario())


@pytest.mark.parametrize("mode", ["clean", "mechanical", "handoff", "red"])
def test_conflict_facts_shapes(ctx, monkeypatch, mode):
    async def scenario():
        if mode in {"mechanical", "handoff"}:
            ticket, path = await conflict(ctx)
            if mode == "handoff":
                ctx.config.merge.strategies = []
        else:
            ticket, path = await ready(ctx), None
        if mode == "red":
            ctx.config.review.mechanical = [host("red")]
            scripted(ctx, monkeypatch, [(1, "", "output must not be journaled")])
        await admission(ctx, ticket)
        [event] = facts(ctx)
        assert event.type == EventType.SIGNAL and event.ticket == ticket.stem and event.key is None
        assert event.body == {"kind": CONFLICT_FACTS,
            "conflicted_paths": [path] if path else [],
            "resolving_rung": {"clean": "none", "mechanical": "mechanical", "handoff": "rework", "red": "none"}[mode],
            "strategy_paths": [path] if mode == "mechanical" else [],
            "integration_red_paths": ["tempo/thing.py"] if mode == "red" else []}
    run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_admission_exception_and_cancellation_abort_and_release(ctx, monkeypatch, cancel):
    async def scenario():
        ticket, _ = await conflict(ctx, strategy="regenerate", generator=["host", "generator"])
        reviewed = await ctx.git.rev_parse(ctx.repo, ticket.stem)
        before = await ctx.git.rev_parse(ctx.repo, "main")
        original = ctx.exec_.run
        entered = asyncio.Event()
        async def execute(argv, **kw):
            if argv[0] == "host":
                entered.set()
                if cancel:
                    await asyncio.Event().wait()
                raise RuntimeError("injected generator failure")
            return await original(argv, **kw)
        monkeypatch.setattr(ctx.exec_, "run", execute)
        queue = MergeQueue(ctx, escalate=lambda _: None)
        queue.offer(ticket, attempt=0)
        task = asyncio.create_task(queue.process())
        await entered.wait()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await task
        assert queue.active is None and not queue._slot.locked()
        assert await ctx.git.rev_parse(ctx.repo, ticket.stem) == reviewed
        assert await ctx.git.rev_parse(ctx.repo, "main") == before
        assert await ctx.git.status_porcelain(ctx.worktree(ticket.stem)) == ""
        # Both failure paths release the writer for the next real candidate.
        monkeypatch.setattr(ctx.exec_, "run", original)
        next_ticket = await ready(ctx, "next", changes={"tempo/new.py": "ok"}, fence=("tempo/new.py",))
        assert (await admission(ctx, next_ticket, queue=queue)).outcome == "ok"
    run(scenario())


def import_closure(root, sources):
    pending, reached = [root], set()
    while pending:
        module = pending.pop()
        if module in reached:
            continue
        reached.add(module)
        tree = ast.parse(sources.get(module, ""))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                pending.extend(a.name for a in node.names if a.name.startswith("tempo."))
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("tempo."):
                    pending.append(node.module)
                elif node.module == "tempo":
                    pending.extend("tempo." + a.name for a in node.names if "tempo." + a.name in sources)
    return reached


def assert_reachable(sources):
    assert "tempo.mergequeue" in import_closure("tempo.__main__", sources)


def test_merge_queue_is_reachable_from_production():
    root = Path(__file__).resolve().parents[1]
    sources = {"tempo." + p.stem: p.read_text() for p in (root / "tempo").glob("*.py")}
    assert_reachable(sources)
    closure = import_closure("tempo.__main__", sources)
    assert "tempo.merge" in closure
    edge = "from tempo.mergequeue import MergeQueue"
    assert edge in sources["tempo.merge"]
    removed = {name: "\n".join(line[:len(line) - len(line.lstrip())] + "pass"
               if line.lstrip().startswith("from tempo.mergequeue import") else line
               for line in source.splitlines()) for name, source in sources.items()}
    with pytest.raises(AssertionError):
        assert_reachable(removed)
    for spelling in ["import tempo.mergequeue as queue", "from tempo import mergequeue as queue"]:
        assert_reachable({**removed, "tempo.merge": removed["tempo.merge"] + "\n" + spelling})


def test_inline_admission_does_not_use_merge_queue(ctx, monkeypatch):
    async def scenario():
        ticket = await ready(ctx)
        queue = MergeQueue(ctx, escalate=lambda _: None, control=admission_control(ctx))
        queue._hold(ticket.stem, {"kind": TREE_MISMATCH, "checked_tree": "a", "main_tree": "b"})
        def forbidden(*args, **kwargs):
            pytest.fail("inline admission entered the held queue")
        monkeypatch.setattr(MergeQueue, "offer", forbidden)
        monkeypatch.setattr(MergeQueue, "process", forbidden)
        result = await merge.merge(ctx, ticket, attempt=0)
        assert result.outcome == "ok" and facts(ctx) == [] and queue.paused
    run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_resume_waits_for_active_admission(ctx, monkeypatch, cancel):
    async def scenario():
        tickets = [await ready(ctx, stem, changes={f"tempo/{stem}.py": "ok"},
                               fence=(f"tempo/{stem}.py",)) for stem in ("one", "two", "three")]
        ctx.config.review.mechanical = [host("integration")]
        scripted(ctx, monkeypatch, [(1, "", "red")] * 3)
        queue = MergeQueue(ctx, escalate=lambda _: None, control=admission_control(ctx))
        for ticket in tickets[:2]:
            await admission(ctx, ticket, queue=queue)
        entered, release = asyncio.Event(), asyncio.Event()
        original = queue._admit
        async def hold(*args):
            result = await original(*args)
            assert queue.paused and queue.active == "three"
            entered.set()
            await release.wait()
            return result
        monkeypatch.setattr(queue, "_admit", hold)
        queue.offer(tickets[2], attempt=0)
        task = asyncio.create_task(queue.process())
        await entered.wait()
        accept_resume(queue)
        boundary = asyncio.create_task(queue.process())
        assert queue.paused and queue.red_stems == ["one", "two", "three"] and queue.active == "three"
        assert not boundary.done() and queue._slot.locked()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            await task
        await boundary
        assert queue.active is None and queue.red_stems == [] and not queue.paused and queue.hold_id is None
        assert not queue._slot.locked()
    run(scenario())


@pytest.mark.parametrize("kind", [RED_STREAK, TREE_MISMATCH])
def test_admission_hold_signal_precedes_projection(ctx, monkeypatch, kind):
    consumer = admission_control(ctx)
    chronology = []
    queue = MergeQueue(ctx, control=consumer, escalate=lambda event: chronology.append("escalate"))
    body = ({"kind": RED_STREAK, "stems": ["one", "two", "three"], "limit": 3}
            if kind == RED_STREAK else {"kind": TREE_MISMATCH, "checked_tree": "checked", "main_tree": "main"})
    append, hold = ctx.driver.journal.append, consumer.hold
    def appended(type, value, **kwargs):
        assert not queue.paused and queue.hold_id is None and consumer.admission is None
        assert set(value) == set(body) | {"hold_id"} and value == {**body, "hold_id": value["hold_id"]}
        assert value["hold_id"] and len(value["hold_id"]) == 32
        assert type == EventType.SIGNAL and kwargs == {"ticket": "three", "key": None}
        chronology.append("append")
        return append(type, value, **kwargs)
    def held(identity):
        assert queue.paused and queue.hold_id == identity
        assert ctx.driver.journal.read()[-1].body == {**body, "hold_id": identity}
        chronology.append("hold")
        hold(identity)
    monkeypatch.setattr(ctx.driver.journal, "append", appended)
    monkeypatch.setattr(consumer, "hold", held)
    identities = []
    for _ in range(2):
        queue._hold("three", body)
        identities.append(queue.hold_id)
        assert chronology[-3:] == ["append", "hold", "escalate"]
        queue.paused, queue.hold_id, consumer.admission = False, None, None
    assert identities[0] != identities[1]
    def failed(*args, **kwargs):
        raise OSError("append failed")
    monkeypatch.setattr(ctx.driver.journal, "append", failed)
    with pytest.raises(OSError, match="append failed"):
        queue._hold("three", body)
    assert not queue.paused and queue.hold_id is None and consumer.admission is None
    assert chronology == ["append", "hold", "escalate"] * 2


def test_admission_resume_matches_lifecycle_and_hold(ctx, monkeypatch):
    from tempo import mergequeue
    from types import SimpleNamespace
    future = "f" * 32
    identities = iter([future, *(mergequeue.uuid4().hex for _ in range(3))])
    monkeypatch.setattr(mergequeue, "uuid4", lambda: SimpleNamespace(hex=next(identities)))
    async def scenario():
        consumer = admission_control(ctx)
        queue = MergeQueue(ctx, control=consumer, escalate=lambda _: None)
        request(consumer, "01-premature", hold=future)
        request(consumer, "02-absent", hold="absent")
        queue._hold("three", {"kind": RED_STREAK, "stems": ["one", "two", "three"], "limit": 3})
        first = queue.hold_id
        assert first == future
        consumer.inbox.consume()
        assert future not in consumer.projection.released_hold_ids
        fresh = MergeQueue(ctx, control=consumer, escalate=lambda _: None)
        assert fresh.paused and fresh.hold_id == first
        request(consumer, "03-wrong", hold="wrong")
        request(consumer, "04-old", hold=first, lifecycle="old-life")
        assert await queue.process() == [] and queue.paused
        request(consumer, "05-pause", verb="pause")
        assert consumer.projection.pause_id == "05-pause"
        accept_resume(queue, "06-matching")
        await queue.process()
        assert not queue.paused and queue.hold_id is None and queue.red_stems == []
        assert consumer.projection.pause_id == "05-pause"
        before = ctx.driver.journal.read()
        consumer.inbox.consume()
        assert ctx.driver.journal.read() == before
        queue._hold("four", {"kind": TREE_MISMATCH, "checked_tree": "a", "main_tree": "b"})
        second = queue.hold_id
        assert first != second
        request(consumer, "07-replaced", hold=first)
        await queue.process()
        assert queue.paused and queue.hold_id == second
        request(consumer, "08-pause-release", hold="05-pause")
        await queue.process()
        assert consumer.projection.pause_id is None and queue.paused
        accept_resume(queue, "09-second")
        await queue.process()
        assert not queue.paused
        decisions = [e.body["decision"] for e in ctx.driver.journal.read()
                     if e.body.get("kind") == control.CONTROL_DECISION]
        assert decisions == ["stale"] * 4 + ["accepted", "accepted", "stale", "accepted", "accepted"]
        unbound = MergeQueue(ctx, escalate=lambda _: None)
        unbound._hold("unbound", {"kind": TREE_MISMATCH, "checked_tree": "a", "main_tree": "b"})
        assert await unbound.process() == [] and unbound.paused
        # A release for the replaced slot cannot name a later fresh hold.
        queue._hold("five", {"kind": TREE_MISMATCH, "checked_tree": "a", "main_tree": "b"})
        consumer.inbox.consume()
        await queue.process()
        assert queue.paused
    run(scenario())


def test_admission_resume_decision_precedes_release(ctx, monkeypatch):
    async def scenario():
        consumer = admission_control(ctx)
        queue = MergeQueue(ctx, control=consumer, escalate=lambda _: None)
        queue.red_stems[:] = ["one", "two", "three"]
        queue._hold("three", {"kind": RED_STREAK, "stems": list(queue.red_stems), "limit": 3})
        identity = queue.hold_id
        append = ctx.driver.journal.append
        def failed(type, body, **kwargs):
            assert queue.paused and queue.hold_id == identity and queue.red_stems == ["one", "two", "three"]
            assert identity not in consumer.projection.released_hold_ids
            raise OSError("decision append failed")
        monkeypatch.setattr(ctx.driver.journal, "append", failed)
        with pytest.raises(OSError, match="decision append failed"):
            request(consumer, "release", hold=identity)
        await queue.process()
        assert queue.paused
        chronology = []
        def durable(type, body, **kwargs):
            assert queue.paused and identity not in consumer.projection.released_hold_ids
            event = append(type, body, **kwargs)
            chronology.append("decision")
            return event
        monkeypatch.setattr(ctx.driver.journal, "append", durable)
        def crash(projection):
            assert chronology == ["decision"] and queue.paused
            assert ctx.driver.journal.read()[-1].body["decision"] == "accepted"
            raise RuntimeError("crash before application")
        monkeypatch.setattr(consumer.inbox, "apply", crash)
        with pytest.raises(RuntimeError, match="crash before application"):
            consumer.inbox.consume()
        await queue.process()
        assert queue.paused
        recovered = admission_control(ctx)
        recovered.hold(identity)
        before = ctx.driver.journal.read()
        recovered.inbox.consume()
        assert identity in recovered.projection.released_hold_ids and ctx.driver.journal.read() == before
        # Binding does not apply recovery; the lock owner recovered before serial processing.
        queue.control = recovered
        assert queue.paused and queue.red_stems == ["one", "two", "three"]
        await queue.process()
        assert not queue.paused and queue.hold_id is None and queue.red_stems == []
    run(scenario())


@pytest.mark.parametrize("ordering", ["hold", "decision"])
def test_admission_durability_probes(ctx, monkeypatch, ordering):
    if ordering == "hold":
        original = MergeQueue._hold
        def early(queue, stem, body):
            queue.paused = True
            original(queue, stem, body)
        monkeypatch.setattr(MergeQueue, "_hold", early)
        with pytest.raises(AssertionError):
            test_admission_hold_signal_precedes_projection(ctx, monkeypatch, RED_STREAK)
    else:
        consume = control.ControlInbox.consume
        def early(inbox):
            append = inbox.journal.append
            def reordered(type, body, **kwargs):
                if body.get("kind") == control.CONTROL_DECISION and body.get("decision") == "accepted":
                    inbox.apply(control.ControlProjection(inbox.lifecycle_id,
                        released_hold_ids=frozenset({body["hold_id"]})))
                return append(type, body, **kwargs)
            with monkeypatch.context() as patch:
                patch.setattr(inbox.journal, "append", reordered)
                consume(inbox)
        monkeypatch.setattr(control.ControlInbox, "consume", early)
        with pytest.raises(AssertionError):
            test_admission_resume_decision_precedes_release(ctx, monkeypatch)
