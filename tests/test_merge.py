"""Merge admission against a temp repo (TEMPO_PLAN.md sections 7, 9, 10; 19.P1)."""

from tests.test_stages import adopt_conduct

import asyncio
import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tempo.config import load_config
from tempo.box import BOX_DIR, Box
from tempo.driver import Driver
from tempo.git import Git
from tempo.journal import EventType
from tempo.llm import FakeLLM
from tempo.merge import Admission, AdmissionBoundary, Candidate, PostRebaseGate, merge
from tempo.mergequeue import CONFLICT_FACTS, MergeQueue
from tempo import runner
from tempo.seams import LocalFileSystem, SubprocessExec
from tempo.stages import StageContext, run_stages
from tempo.tickets import validate_ticket
from tests.test_seed_path import (context as seed_context, repo as seed_repo, requisition, seed_text,
                                  ticket as seeding_ticket, write_seeds)
from tests.test_stages import ENV, SNAG, SPECS, STEM, agent, author, git, verdict
from tests.test_cli import root
from tests.test_stages import report_context, _report_bytes

GOAL = "`thing.py` holds the word ok."


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tempo").mkdir(parents=True)
    (root / "tempo" / "thing.py").write_text("")
    (root / "tempo" / "other.py").write_text("")
    (root / "TEMPO_PLAN.md").write_text("# Plan\n")
    (root / "config.yaml").write_text((Path(__file__).parent / "test_stages.py").read_text()
                                      .split('CONFIG = """')[1].split('"""')[0])
    (root / ".gitignore").write_text(".tempo/\n")
    adopt_conduct(root)
    git(root, "init", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    return root


def context(repo: Path, script: list) -> StageContext:
    config = load_config(None, cwd=repo)
    clock = lambda: datetime(2026, 10, 5, tzinfo=UTC)  # noqa: E731

    async def never(seconds: float) -> None:
        await asyncio.Event().wait()

    exec_ = SubprocessExec()
    driver = Driver.from_config(config, llm=FakeLLM(script), env=ENV, clock=clock, sleep=never)
    return StageContext(repo=repo, config=config, env=ENV, exec_=exec_, git=Git(exec_, env=ENV, timeout=30.0),
                        fs=LocalFileSystem(), driver=driver, specs_dir=SPECS)


def reviewed(repo: Path, script: list, *, approve: bool = True, authored: bool = False):
    """Author the ticket and run Implement -> Check -> Review; return what merge needs."""
    if not authored:
        author(repo)
    ctx = context(repo, script)
    ticket = validate_ticket(STEM, (repo / "tickets" / STEM / "ticket.md").read_text(), repo)
    run = asyncio.run(run_stages(ctx, ticket))
    assert run.last == ("review", run.results["review"])
    assert (run.results["review"].outcome == "ok") is approve
    return ctx, ticket, run.attempt


def admit(ctx, ticket, attempt):
    return asyncio.run(merge(ctx, ticket, attempt=attempt))


def merged_events(ctx):
    return [e for e in ctx.driver.journal.read()
            if e.type == EventType.STATE_TRANSITION and e.body.get("to") == "merged"]


def refused_untouched(ctx, result, main_before: str, code: str) -> None:
    assert result.outcome == "gate_failed" and result.artifact is None
    assert code in [f.code for f in result.findings]
    assert git(ctx.repo, "rev-parse", "main").strip() == main_before  # main never moved
    assert git(ctx.repo, "rev-parse", "--verify", STEM)  # branch left in place
    assert ctx.worktree(STEM).is_dir()
    assert not merged_events(ctx)


def test_a_passing_ticket_squash_merges_with_trailers_and_journals_merged(repo):
    ctx, ticket, attempt = reviewed(repo, [agent({"tempo/thing.py": "ok\n"}), verdict()])
    head = git(repo, "rev-parse", STEM).strip()

    result = admit(ctx, ticket, attempt)

    assert result.outcome == "ok", result.findings
    admission = result.artifact
    assert isinstance(admission, Admission) and admission.reviewed_sha == head
    assert git(repo, "rev-parse", "main").strip() == admission.commit
    message = git(repo, "log", "-1", "--format=%B", "main")
    assert message.splitlines()[0] == f"tempo({STEM}): {GOAL}"
    trailers = git(repo, "log", "-1", "--format=%(trailers:only,unfold)", "main").split("\n")
    assert f"tempo-ticket: {STEM}" in trailers and f"tempo-reviewed-sha: {head}" in trailers
    # One commit per ticket, code only: the ticket plane rode its own lane.
    assert git(repo, "show", "--name-only", "--format=", "main").split() == ["tempo/thing.py"]
    assert (repo / "tempo" / "thing.py").read_text() == "ok\n"

    [event] = merged_events(ctx)
    assert event.ticket == STEM and event.body == {"to": "merged", "commit": admission.commit, "reviewed_sha": head}
    assert not ctx.worktree(STEM).exists()
    assert git(repo, "branch", "--list", STEM) == ""
    assert git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("verb", ["bind", "run", "drain"])
def test_bootstrap_pipeline_keeps_inline_admission(repo, monkeypatch, verb):
    author(repo)
    source = context(repo, [])
    checkout = runner.Checkout(repo, source.config, source.env, source.exec_, source.git,
                               source.driver.journal, source.fs, source.driver.clock, source.driver.sleep)
    from tempo.__main__ import build_control
    checkout = dataclasses.replace(checkout, control=build_control(checkout))
    llm = FakeLLM([agent({"tempo/thing.py": "ok\n"}), verdict()])

    def forbidden(*args, **kwargs):
        pytest.fail("bootstrap dispatch used the composed queue")

    monkeypatch.setattr(MergeQueue, "offer", forbidden)
    monkeypatch.setattr(MergeQueue, "process", forbidden)
    calls = []
    original = runner.merge

    async def inline(ctx, ticket, *, attempt):
        calls.append((ctx, ticket, attempt))
        return await original(ctx, ticket, attempt=attempt)

    monkeypatch.setattr(runner, "merge", inline)
    def pipeline(local):
        dispatch = runner.bind(local, llm)
        dispatch.queue.paused, dispatch.queue.hold_id = True, "daemon-only-hold"
        local.control.hold(dispatch.queue.hold_id)
        return dispatch
    ticket = validate_ticket(STEM, (repo / "tickets" / STEM / "ticket.md").read_text(), repo)
    if verb == "bind":
        assert asyncio.run(pipeline(checkout)(ticket)) == "merged"
    else:
        from tempo.__main__ import main
        from tests.test_drain_reentry import NoChild
        assert main(["run", STEM] if verb == "run" else ["drain"], cwd=repo, env=ENV,
                    clock=checkout.clock, pipeline=pipeline, reexec=NoChild()) == 0
    [(ctx, original_ticket, attempt)] = calls
    assert original_ticket.stem == ticket.stem and attempt == 0
    assert len(merged_events(ctx)) == 1
    assert not any(e.body.get("kind") == CONFLICT_FACTS for e in ctx.driver.journal.read())
    assert (repo / "tempo/thing.py").read_text() == "ok\n" and not ctx.worktree(STEM).exists()


def test_foreground_admission_never_supervised_holds(repo, monkeypatch):
    test_bootstrap_pipeline_keeps_inline_admission(repo, monkeypatch, "run")


@pytest.mark.parametrize("failure", ["ticket-plane", "missing-checks", "malformed-checks", "missing-approval", "wrong-sha",
                                    "missing-blob", "invalid-blob", "dirty-main"])
def test_daemon_prechecks_precede_offer(seed_repo, monkeypatch, failure):
    ctx, ticket, attempt = seed_reviewed(seed_repo)
    async def scenario():
        checks = "tickets/add-thing/checks.json"
        seed = "tickets/alpha-seed/ticket.md"
        code = "requisition_review"
        if failure == "ticket-plane":
            code = "post_rebase_regate"
            path = ctx.worktree(ticket.stem) / "tickets/committed.md"
            ctx.fs.write(path, b"forbidden code-lane ticket\n")
            await ctx.git.add(ctx.worktree(ticket.stem), ["tickets/committed.md"])
            await ctx.git.commit(ctx.worktree(ticket.stem), "code lane violation")
        elif failure == "missing-checks":
            (ctx.repo / checks).unlink()
        elif failure == "malformed-checks":
            ctx.fs.write(ctx.repo / checks, b"invalid")
        elif failure in {"missing-approval", "wrong-sha"}:
            data = json.loads((ctx.repo / checks).read_text())
            if failure == "missing-approval":
                data["seeds"] = []
            else:
                data["seeds"][0]["ticket_sha"] = "wrong"
            ctx.fs.write(ctx.repo / checks, json.dumps(data).encode())
        elif failure == "missing-blob":
            (ctx.repo / seed).unlink()
        elif failure == "invalid-blob":
            ctx.fs.write(ctx.repo / seed, b"invalid")
            data = json.loads((ctx.repo / checks).read_text())
            data["seeds"][0]["ticket_sha"] = (await ctx.git._run(ctx.repo, "hash-object", seed)).strip()
            ctx.fs.write(ctx.repo / checks, json.dumps(data).encode())
        else:
            # A dirty, valid replacement cannot cure committed wrong-SHA custody.
            data = json.loads((ctx.repo / checks).read_text())
            data["seeds"][0]["ticket_sha"] = "wrong"
            valid = (ctx.repo / checks).read_bytes()
            ctx.fs.write(ctx.repo / checks, json.dumps(data).encode())
        if failure != "ticket-plane":
            await ctx.git.add(ctx.repo, [seed, checks])
            await ctx.git.commit(ctx.repo, "precheck refusal fixture")
        if failure == "dirty-main":
            ctx.fs.write(ctx.repo / checks, valid)
        ctx.config.review.gate_severity = {code: "soft"}
        before = await ctx.git.rev_parse(ctx.repo, "main")
        head = await ctx.git.rev_parse(ctx.repo, ticket.stem)
        journal = ctx.driver.journal.read()
        queue = MergeQueue(ctx, escalate=lambda _: None)
        def forbidden(*args, **kwargs):
            pytest.fail("precheck refusal offered, rebased or ran Verification")
        monkeypatch.setattr(queue, "offer", forbidden)
        monkeypatch.setattr(ctx.git, "rebase_stop_at_conflict", forbidden)
        monkeypatch.setattr(ctx.git, "rebase", forbidden)
        monkeypatch.setattr(stages, "gather_evidence", forbidden)
        boundary = AdmissionBoundary(ctx, mode="daemon", queue=queue)
        result = await boundary(ticket, attempt=attempt)
        assert result.outcome == "gate_failed" and result.artifact is None
        assert code in {f.code for f in result.findings}
        assert all(f.paved_road for f in result.findings)
        assert await ctx.git.rev_parse(ctx.repo, "main") == before
        assert await ctx.git.rev_parse(ctx.repo, ticket.stem) == head
        assert ctx.worktree(ticket.stem).exists() and not queue.pending
        assert ctx.driver.journal.read() == journal and not boundary.results
    from tempo import stages
    asyncio.run(scenario())


def test_admission_mode_is_explicit(repo, monkeypatch):
    ctx, ticket, attempt = reviewed(repo, [agent({"tempo/thing.py": "ok\n"}), verdict()])
    queue = MergeQueue(ctx, escalate=lambda _: None)
    def forbidden(*args, **kwargs):
        pytest.fail("construction or inline admission entered daemon queue")
    monkeypatch.setattr(queue, "offer", forbidden)
    monkeypatch.setattr(queue, "process", forbidden)
    before = ctx.driver.journal.read()
    with monkeypatch.context() as idle:
        idle.setattr(ctx.exec_, "run", forbidden)
        idle.setattr(asyncio, "create_task", forbidden)
        boundary = AdmissionBoundary(ctx, queue=queue)
        daemon = AdmissionBoundary(ctx, mode="daemon", queue=queue)
    assert boundary.mode == "inline" and daemon.mode == "daemon"
    assert not boundary.results and not daemon.results and not queue.pending
    assert ctx.driver.journal.read() == before
    for kwargs in ({"mode": "unknown", "queue": queue}, {"mode": "daemon"}):
        with pytest.raises(runner.Refusal, match="supply.*MergeQueue.*serve composition"):
            AdmissionBoundary(ctx, **kwargs)
    queue.paused, queue.hold_id = True, "daemon-hold"
    from tempo.lockfile import Lockfile
    lock = Lockfile(ctx.config.state_dir, instance_id="inline-mode", clock=ctx.driver.clock)
    lock.acquire()
    try:
        assert asyncio.run(boundary(ticket, attempt=attempt)).outcome == "ok"
    finally:
        lock.release()


def test_approval_carries_across_a_clean_rebase_onto_a_moved_main(repo):
    ctx, ticket, attempt = reviewed(repo, [agent({"tempo/thing.py": "ok\n"}), verdict()])
    head = git(repo, "rev-parse", STEM).strip()
    (repo / "tempo" / "other.py").write_text("moved\n")
    git(repo, "commit", "-am", "main moves")

    result = admit(ctx, ticket, attempt)

    assert result.outcome == "ok", result.findings
    assert f"tempo-reviewed-sha: {head}" in git(repo, "log", "-1", "--format=%B", "main")
    assert (repo / "tempo" / "other.py").read_text() == "moved\n"
    assert git(repo, "show", "--name-only", "--format=", "main").split() == ["tempo/thing.py"]


def test_a_rerun_worktree_with_lifted_tracked_copies_is_restored_and_merges(repo):
    # A prior run already lifted run.md / checks.json / review.md to main, so this run's worktree is born
    # tracking them; the new lift unlinks them there, and an un-restored tree would refuse the rebase.
    author(repo)
    for name in ("run.md", "checks.json", "review.md"):
        (repo / "tickets" / STEM / name).write_text("prior run\n")
    git(repo, "add", "tickets")
    git(repo, "commit", "-m", f"tempo({STEM}): prior run")
    ctx, ticket, attempt = reviewed(repo, [agent({"tempo/thing.py": "ok\n"}), verdict()], authored=True)
    assert "D tickets/" in git(ctx.worktree(STEM), "status", "--porcelain")

    result = admit(ctx, ticket, attempt)

    assert result.outcome == "ok", result.findings
    assert git(repo, "show", "--name-only", "--format=", "main").split() == ["tempo/thing.py"]
    assert "prior run" not in (repo / "tickets" / STEM / "review.md").read_text()


def test_a_failing_hard_gate_blocks_admission(repo):
    # Verification goes red on the rebased candidate but stays green on the merge base.
    ctx, ticket, attempt = reviewed(repo, [agent({"tempo/thing.py": "ok\n"}), verdict()])
    ticket = dataclasses.replace(ticket, verification=(("test", "!", "-s", "tempo/thing.py"),))
    (repo / "tempo" / "other.py").write_text("x\n")
    git(repo, "commit", "-am", "main moves")
    before = git(repo, "rev-parse", "main").strip()

    result = admit(ctx, ticket, attempt)

    refused_untouched(ctx, result, before, "verification")


def test_base_red_verification_does_not_refuse_merge_regating(repo):
    ctx, ticket, attempt = reviewed(repo, [agent({"tempo/thing.py": "ok\n"}), verdict()])
    ticket = dataclasses.replace(ticket, verification=(("grep", "-q", "absent", "tempo/thing.py"),))

    result = admit(ctx, ticket, attempt)

    assert result.outcome == "ok", result.findings
    messages = Box(ctx.config.state_dir / BOX_DIR, ctx.fs).messages()
    assert len(messages) == 1 and messages[0].outcome == "base_red"
    assert messages[0].stage == "merge"
    assert not (ctx.config.worktree_root / ".base" / STEM).exists()


def test_an_approval_pinned_to_an_older_head_blocks_admission(repo):
    ctx, ticket, attempt = reviewed(repo, [agent({"tempo/thing.py": "ok\n"}), verdict()])
    (ctx.worktree(STEM) / "tempo" / "thing.py").write_text("ok but changed after review\n")
    git(ctx.worktree(STEM), "commit", "-am", "post-review edit")
    before = git(repo, "rev-parse", "main").strip()

    result = admit(ctx, ticket, attempt)

    refused_untouched(ctx, result, before, "correctness_review")
    assert "the approval pins" in result.findings[0].message


def test_a_snag_verdict_is_no_approval(repo):
    ctx, ticket, attempt = reviewed(repo, [agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG])],
                                    approve=False)
    before = git(repo, "rev-parse", "main").strip()

    result = admit(ctx, ticket, attempt)

    refused_untouched(ctx, result, before, "correctness_review")
    assert "not approve" in result.findings[0].message


def test_a_branch_committing_ticket_plane_files_is_refused_with_the_outbox_road(tmp_path):
    candidate = Candidate(stem=STEM, reviewed_sha="abc", ticket_text="", review_text=None,
                          changed_files=["tempo/thing.py", f"tickets/{STEM}/notes.md"])

    report = PostRebaseGate().check(candidate, tmp_path)

    [finding] = report.findings
    assert report.verdict == "fail" and finding.path == f"tickets/{STEM}/notes.md"
    assert "driver lifts them" in finding.paved_road


def test_a_main_ticket_edit_failing_the_schema_blocks_admission(repo):
    ctx, ticket, attempt = reviewed(repo, [agent({"tempo/thing.py": "ok\n"}), verdict()])
    path = repo / "tickets" / STEM / "ticket.md"
    path.write_text(path.read_text().replace("## Definition of rejected\nNeeds a new dependency.\n\n", ""))
    git(repo, "commit", "-am", "hand edit")
    before = git(repo, "rev-parse", "main").strip()

    result = admit(ctx, ticket, attempt)

    refused_untouched(ctx, result, before, "ticket_schema")


def test_a_conflicted_rebase_is_aborted_and_refused(repo):
    ctx, ticket, attempt = reviewed(repo, [agent({"tempo/thing.py": "ok\n"}), verdict()])
    head = git(repo, "rev-parse", STEM).strip()
    (repo / "tempo" / "thing.py").write_text("conflict\n")
    git(repo, "commit", "-am", "main conflicts")
    before = git(repo, "rev-parse", "main").strip()

    result = admit(ctx, ticket, attempt)

    refused_untouched(ctx, result, before, "post_rebase_regate")
    assert "re-branches from current main" in result.findings[0].paved_road
    assert git(repo, "rev-parse", STEM).strip() == head  # never left mid-rebase
    assert not (repo / ".git" / "worktrees" / STEM / "rebase-merge").exists()


def seed_reviewed(repo):
    seed = seed_text("alpha-seed")
    ctx, llm = seed_context(repo, [write_seeds({"alpha-seed": seed}), requisition("approve"), verdict()])
    ticket = seeding_ticket(repo)
    run = asyncio.run(run_stages(ctx, ticket))
    assert run.results["review"].outcome == "ok"
    assert [req.surface for req in llm.requests] == ["implement", "requisition_review", "review"]
    return ctx, ticket, run.attempt


def test_merge_safety_refuses_a_seed_whose_committed_blob_changed(seed_repo):
    ctx, ticket, attempt = seed_reviewed(seed_repo)
    path = seed_repo / "tickets/alpha-seed/ticket.md"
    path.write_text(path.read_text() + "\n")
    git(seed_repo, "commit", "-am", "seed text changes after Check")
    before = git(seed_repo, "rev-parse", "main").strip()

    result = admit(ctx, ticket, attempt)

    refused_untouched(ctx, result, before, "requisition_review")
    assert any("no approve verdict pins" in f.message for f in result.findings)


def test_merge_safety_rechecks_seed_lint_even_with_a_matching_approval(seed_repo):
    ctx, ticket, attempt = seed_reviewed(seed_repo)
    path = seed_repo / "tickets/alpha-seed/ticket.md"
    path.write_text(path.read_text().replace("## Definition of rejected\nNeeds a new dependency.\n\n", ""))
    checks_path = seed_repo / "tickets" / STEM / "checks.json"
    checks = json.loads(checks_path.read_text())
    checks["seeds"][0]["ticket_sha"] = git(seed_repo, "hash-object", str(path)).strip()
    checks_path.write_text(json.dumps(checks))
    git(seed_repo, "commit", "-am", "invalid seed and matching approval")
    before = git(seed_repo, "rev-parse", "main").strip()

    result = admit(ctx, ticket, attempt)

    refused_untouched(ctx, result, before, "requisition_review")
    assert any("Definition of rejected" in f.message for f in result.findings)


def test_merge_safety_admits_a_seed_with_matching_recorded_approval(seed_repo):
    ctx, ticket, attempt = seed_reviewed(seed_repo)

    result = admit(ctx, ticket, attempt)

    assert result.outcome == "ok", result.findings
    assert (seed_repo / "tickets/alpha-seed/ticket.md").is_file()
    assert git(seed_repo, "show", "--name-only", "--format=", "main").split() == ["tempo/thing.py"]


def test_merge_safety_reads_committed_seed_and_checks_despite_dirty_main_checkout(seed_repo):
    ctx, ticket, attempt = seed_reviewed(seed_repo)
    (seed_repo / "tickets/alpha-seed/ticket.md").write_text("uncommitted invalid ticket\n")
    (seed_repo / "tickets" / STEM / "checks.json").write_text("uncommitted invalid checks\n")

    result = admit(ctx, ticket, attempt)

    assert result.outcome == "ok", result.findings
    assert git(seed_repo, "show", "main:tickets/alpha-seed/ticket.md") == seed_text("alpha-seed")


def test_pipeline_requires_supplied_control(repo):
    from tempo import merge as owner
    from tempo.__main__ import build_control
    ctx = context(repo, [])
    checkout = runner.Checkout(repo, ctx.config, ctx.env, ctx.exec_, ctx.git,
                               ctx.driver.journal, ctx.fs, ctx.driver.clock, ctx.driver.sleep)
    consumer = build_control(checkout)
    with pytest.raises(TypeError):
        owner.compose_pipeline(ctx, escalate=lambda _: None)
    with pytest.raises(runner.Refusal, match="build_control.*Checkout.control"):
        owner.compose_pipeline(ctx, escalate=lambda _: None, control=None)
    queue = owner.compose_pipeline(ctx, escalate=lambda _: None, control=consumer)
    assert queue.control is consumer and queue.hold_id is None
    assert MergeQueue(ctx, escalate=lambda _: None).control is None
    assert ctx.driver.journal.read() == []


def test_outbox_only_admission_records_null_commit_and_retires(root, monkeypatch):
    """Inline CLI and real serve composition share the writer, lift and retirement contract."""
    from tempo import merge as owner
    from tempo.artifacts import SHAKEOUT_REPORT, ShakeoutReport
    from tempo.audit import audit_journal
    from tempo.checkpoint import _merged
    from tempo.effects import Effects
    from tempo.rework import settled_dependencies
    from tempo.stages import lift_outbox
    from tests.test_serve import admission_graph, admission_ticket, start_admission, until, finish, request
    from tempo.__main__ import main
    import shlex

    original_writer = owner.write_squash
    admissions, trace = [], []
    remove, delete = Git.worktree_remove, Git.branch_delete
    async def removed(wrapper, repo, path):
        trace.append((path.name, "remove"))
        await remove(wrapper, repo, path)
    async def deleted(wrapper, repo, branch):
        trace.append((branch, "delete"))
        await delete(wrapper, repo, branch)
    monkeypatch.setattr(Git, "worktree_remove", removed)
    monkeypatch.setattr(Git, "branch_delete", deleted)
    async def writer(ctx, ticket, reviewed, *, attempt):
        before = await ctx.git.rev_parse(root, "main")
        tree = await ctx.git.rev_parse(root, "main^{tree}")
        checks_key = f"ticket-plane/{ticket.stem}/{attempt}/checks"
        [lift] = [e for e in ctx.driver.journal.read()
                  if e.type == EventType.EFFECT_COMPLETION and e.key == checks_key]
        ctx.fs.write(ctx.worktree(ticket.stem) / f"tickets/{ticket.stem}/{SHAKEOUT_REPORT}",
                     _report_bytes().replace('"abc"', '"replayed"').encode())
        history = ctx.driver.journal.read()
        ctx.driver.effects = Effects(ctx.driver.journal)
        assert await lift_outbox(ctx, ticket.stem, "checks", attempt=attempt) == lift.body["result"]["commit"]
        assert ctx.driver.journal.read() == history
        result = await original_writer(ctx, ticket, reviewed, attempt=attempt)
        assert await ctx.git.rev_parse(root, "main") == before
        assert await ctx.git.rev_parse(root, "main^{tree}") == tree
        admissions.append((ctx, ticket, attempt, result, tree))
        return result
    monkeypatch.setattr(owner, "write_squash", writer)

    async def evidence(ctx, ticket, attempt, admission, tree):
        assert admission.commit is None and admission.produced_by_spec_version == 1
        assert admission.produced_at_sha == await ctx.git.rev_parse(root, "main")
        [terminal] = merged_events(ctx)
        assert terminal.ticket == ticket.stem and terminal.key is None
        assert terminal.body == {"to": "merged", "commit": None, "reviewed_sha": admission.reviewed_sha}
        events = ctx.driver.journal.read()
        [checks] = [e for e in events if e.type == EventType.EFFECT_COMPLETION
                    and e.key == f"ticket-plane/{ticket.stem}/{attempt}/checks"]
        rel = f"tickets/{ticket.stem}/{SHAKEOUT_REPORT}"
        assert rel in checks.body["result"]["paths"]
        blob = await ctx.git._run(root, "show", f"{checks.body['result']['commit']}:{rel}")
        ShakeoutReport.model_validate_json(blob)
        [completion] = [e for e in events if e.type == EventType.EFFECT_COMPLETION
                        and e.key == f"merge/{ticket.stem}/{attempt}"]
        assert completion.body == {"result": {"commit": None}}
        assert audit_journal(ctx.driver.journal) == []
        assert ticket.stem in settled_dependencies(events) and _merged(events) == 0
        assert not ctx.worktree(ticket.stem).exists()
        assert trace.count((ticket.stem, "remove")) == trace.count((ticket.stem, "delete")) == 1
        assert await ctx.git._run(root, "branch", "--list", ticket.stem) == ""
        assert await ctx.git.rev_parse(root, "main^{tree}") == tree
        assert "tempo-ticket:" not in await ctx.git._run(root, "log", "--format=%B", "main")
        ctx.driver.effects = Effects(ctx.driver.journal)
        before = await ctx.git.rev_parse(root, "main")
        assert await lift_outbox(ctx, ticket.stem, "checks", attempt=attempt) is None
        replay = await original_writer(ctx, ticket, admission.reviewed_sha, attempt=attempt)
        assert replay == admission and await ctx.git.rev_parse(root, "main") == before
        assert ctx.driver.journal.read() == events
        # Exercise the actual intake/dependency reader against this producing journal.
        from tests.test_stages import TICKET
        dependent = f"after-{ticket.stem}"
        raw = TICKET.format(bypass="").replace("## Depends on\nnone", f"## Depends on\n- {ticket.stem}")
        ctx.fs.write(root / f"tickets/{dependent}/ticket.md", raw.encode())
        checkout = runner.Checkout(root, ctx.config, ctx.env, ctx.exec_, ctx.git,
                                  ctx.driver.journal, ctx.fs, ctx.driver.clock, ctx.driver.sleep)
        admitted = await runner._admit(dependent, checkout)
        assert admitted.depends == (ticket.stem,)
        assert _merged(ctx.driver.journal.read()) == 0 and audit_journal(ctx.driver.journal) == []

    # The synchronous CLI owns asyncio.run, just as it does in production.
    ctx, ticket = asyncio.run(report_context(root))
    def pipeline(checkout):
        dispatch = runner.bind(checkout, ctx.driver.llm)
        return dispatch
    assert main(["run", STEM], cwd=root, env=ENV, clock=ctx.driver.clock, pipeline=pipeline) == 0
    asyncio.run(evidence(*admissions.pop()))

    async def daemon():
        async def moved(rig, local, ticket, attempt):
            rig.pinned = await local.git.rev_parse(root, ticket.stem)
            rig.fs.write(root / "tempo/other.py", b"moved main\n")
            await local.git.add(root, ["tempo/other.py"])
            await local.git.commit(root, "main advances after report approval")
        rig = await admission_graph(root, monkeypatch, {}, settled=moved)
        stem = "serve-producer"
        script = f"from pathlib import Path; Path('tickets/{stem}/{SHAKEOUT_REPORT}').write_text({_report_bytes()!r})"
        admission_ticket(root, stem, verification=f"python3 -c {shlex.quote(script)}")
        run = await start_admission(rig, monkeypatch)
        try:
            await until(rig, lambda: (stem, "merged") in rig.returns)
            request(rig, "pause", "proof-pause")
            await until(rig, lambda: rig.owner.control.projection.pause_id == "proof-pause"
                        and rig.owner.core.admission.active is None)
            local, ticket, attempt, admission, tree = admissions.pop()
            assert admission.reviewed_sha == rig.pinned
            assert trace.count((stem, "remove")) == trace.count((stem, "delete")) == 1
            assert rig.owner.queue.ctx is local
            assert not rig.owner.queue.paused
            assert "merge-integration/verify-01.txt" in [
                str(p.relative_to(local.config.state_dir / f"spools/{stem}/{attempt}"))
                for p in (local.config.state_dir / f"spools/{stem}/{attempt}").rglob("*.txt")]
            await evidence(local, ticket, attempt, admission, tree)
        finally:
            await finish(rig, run)
    asyncio.run(daemon())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["inline", "queue"])
@pytest.mark.parametrize("failure", ["absent", "wrong-run", "wrong-stem", "foreign-path", "unknown",
                                     "records-only", "missing-committed", "invalid-committed", "invalid-schema",
                                     "red", "invalid-regate", "missing-regate"])
async def test_outbox_only_merge_regate_requires_lift_custody(repo, monkeypatch, mode, failure):
    from tempo.artifacts import SHAKEOUT_REPORT
    ctx, ticket = await report_context(repo)
    run = await run_stages(ctx, ticket)
    assert run.results["review"].outcome == "ok"
    attempt = run.attempt
    key = f"ticket-plane/{STEM}/{attempt}/checks"
    read = ctx.driver.journal.read
    if failure in {"absent", "wrong-stem", "foreign-path", "unknown", "records-only"}:
        def evidence_read():
            events = []
            for event in read():
                if event.type == EventType.EFFECT_COMPLETION and event.key == key:
                    if failure == "absent":
                        continue
                    if failure == "wrong-stem":
                        event = dataclasses.replace(event, ticket="foreign")
                    else:
                        paths = ([f"tickets/foreign/{SHAKEOUT_REPORT}"] if failure == "foreign-path" else
                                 [f"tickets/{STEM}/{p}" for p in ("run.md", "checks.json", "review.md")]
                                 if failure == "records-only" else [f"tickets/{STEM}/unknown.json"])
                        result = {**event.body["result"], "paths": paths}
                        event = dataclasses.replace(event, body={"result": result})
                events.append(event)
            return events
        monkeypatch.setattr(ctx.driver.journal, "read", evidence_read)
    elif failure == "wrong-run":
        attempt += 1
    elif failure in {"missing-committed", "invalid-committed"}:
        rel = f"tickets/{STEM}/{SHAKEOUT_REPORT}"
        if failure == "missing-committed":
            (repo / rel).unlink()
        else:
            ctx.fs.write(repo / rel, b'{"invalid":true}')
        await ctx.git.add(repo, [rel])
        await ctx.git.commit(repo, "committed custody refusal fixture")
        # A valid dirty checkout copy cannot substitute for the committed blob.
        ctx.fs.write(repo / rel, _report_bytes().encode())
    elif failure == "red":
        ticket = dataclasses.replace(ticket, verification=(*ticket.verification, ("false",)))
    elif failure == "invalid-regate":
        ctx.fs.write(repo / "source.json", b'{"invalid":true}')
        await ctx.git.add(repo, ["source.json"])
        await ctx.git.commit(repo, "invalid Verification report fixture")
    elif failure == "invalid-schema":
        from tempo.stages import KNOWN_ARTIFACTS
        from tempo.artifacts import ShakeoutEntry
        monkeypatch.setitem(KNOWN_ARTIFACTS, SHAKEOUT_REPORT, ShakeoutEntry)
        ticket = dataclasses.replace(ticket, verification=(("true",),))
    else:
        ticket = dataclasses.replace(ticket, verification=(("true", f"--out=tickets/{STEM}/{SHAKEOUT_REPORT}"),))
    before = await ctx.git.rev_parse(repo, "main")
    if mode == "inline":
        result = await merge(ctx, ticket, attempt=attempt)
    else:
        queue = MergeQueue(ctx, escalate=lambda _: None)
        queue.offer(ticket, attempt=attempt)
        [result] = await queue.process()
    refused_untouched(ctx, result, before, "verification")
    assert all(f.paved_road for f in result.findings)
    assert not any((e.key or "").startswith("merge/") for e in ctx.driver.journal.read())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["inline", "queue"])
@pytest.mark.parametrize("case", ["committed-tickets", "dirty-tracked", "dirty-untracked", "code-and-report",
                                  "stale-approval", "missing-approval"])
async def test_outbox_only_exception_preserves_code_lane_safety(repo, mode, case):
    from tempo.stages import read_review, render_review
    script = [agent({"tempo/thing.py": "ok code\n"}), verdict()] if case == "code-and-report" else None
    ctx, ticket = await report_context(repo, script=script)
    run = await run_stages(ctx, ticket)
    assert run.results["review"].outcome == "ok"
    code = "verification"
    if case == "committed-tickets":
        rel = f"tickets/{STEM}/notes.md"
        ctx.fs.write(ctx.worktree(STEM) / rel, b"committed outbox violation")
        await ctx.git.add(ctx.worktree(STEM), [rel])
        await ctx.git.commit(ctx.worktree(STEM), "ticket-plane violation")
        code = "post_rebase_regate"
    elif case.startswith("dirty"):
        rel = "tempo/thing.py" if case == "dirty-tracked" else "tempo/new file.py"
        ctx.fs.write(ctx.worktree(STEM) / rel, b"uncommitted code")
        if case == "dirty-tracked":
            code = "post_rebase_regate"
    elif case.endswith("approval"):
        path = repo / f"tickets/{STEM}/review.md"
        if case == "missing-approval":
            path.unlink()
        else:
            review = read_review(path.read_text()).model_copy(update={"reviewed_sha": "stale"})
            ctx.fs.write(path, render_review(review).encode())
        code = "correctness_review"
    before = await ctx.git.rev_parse(repo, "main")
    if mode == "inline":
        result = await merge(ctx, ticket, attempt=run.attempt)
    else:
        queue = MergeQueue(ctx, escalate=lambda _: None)
        queue.offer(ticket, attempt=run.attempt)
        [result] = await queue.process()
    if case == "code-and-report":
        assert result.outcome == "ok" and result.artifact.commit is not None
        assert result.artifact.commit == await ctx.git.rev_parse(repo, "main")
        assert (await ctx.git._run(repo, "show", "--name-only", "--format=", "main")).split() == ["tempo/thing.py"]
        assert f"tempo-ticket: {STEM}" in await ctx.git._run(repo, "log", "-1", "--format=%B")
    else:
        refused_untouched(ctx, result, before, code)
        assert all(f.paved_road for f in result.findings)


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "case"), [
    (mode, case) for mode in ("inline", "queue") for case in
    ("missing", "conflicting", "malformed", "valid", "no-spec", "unrelated", "failed", "tree-mismatch")
] + [("boundary", case) for case in ("missing", "conflicting", "malformed")])
async def test_retro_prompt_spec_merge_requires_unique_bridge(repo, monkeypatch, mode, case):
    from tempo.effects import Effects
    from tempo.merge import write_squash
    from tempo.mergequeue import TreeMismatch
    from tests.test_stages import implement_reply
    author(repo)
    ctx = context(repo, [implement_reply(), verdict()])
    path = repo / f"tickets/{STEM}/ticket.md"
    source = "human" if case == "unrelated" else "box:retro_finding"
    ctx.fs.write(path, path.read_text().replace("source: human", f"source: {source}").replace(
        "## Scope fence\n- tempo/thing.py", "## Scope fence\n- tempo/thing.py\n- specs/").encode())
    for rel in ("specs/z.md", "specs/a.md", "specs/readme.txt"):
        ctx.fs.write(repo / rel, b"before\n")
    await ctx.git.add(repo, [str(path.relative_to(repo)), "specs/z.md", "specs/a.md", "specs/readme.txt"])
    await ctx.git.commit(repo, "retro admission fixture")
    ticket = validate_ticket(STEM, path.read_text(), repo)
    call = ctx.driver.llm.call
    async def implement(request):
        if request.surface == "implement":
            changes = {"tempo/thing.py": "ok\n"}
            if case != "no-spec":
                changes.update({"specs/z.md": "fixed\n", "specs/a.md": "fixed\n", "specs/readme.txt": "fixed\n"})
            for rel, text in changes.items():
                ctx.fs.write(request.worktree / rel, text.encode())
            await ctx.git.add(request.worktree, list(changes))
            await ctx.git.commit(request.worktree, "repair")
        return await call(request)
    monkeypatch.setattr(ctx.driver.llm, "call", implement)
    run = await run_stages(ctx, ticket)
    assert run.results["review"].outcome == "ok"
    body = {"kind": "retro_ticket_authored", "box_id": "box-000001-1234abcd",
            "retro_report_key": "retro/1", "ticket_stem": STEM}
    journal = ctx.driver.journal
    if case not in {"missing", "unrelated"}:
        journal.append(EventType.SIGNAL, {**body, **({"retro_report_key": "invented"} if case == "malformed" else {})},
                       ticket=STEM, key=f"retro-ticket/{STEM}")
    if case == "conflicting":
        journal.append(EventType.SIGNAL, {**body, "box_id": "box-000002-abcdef12"},
                       ticket=STEM, key=f"retro-ticket/{STEM}")
    if case == "failed":
        (repo / f"tickets/{STEM}/review.md").unlink()
    if case in {"missing", "conflicting", "malformed"}:
        ctx.config.review.gate_severity = {"ticket_schema": "soft"}
    if case == "tree-mismatch":
        parse = ctx.git.rev_parse
        async def mismatch(root, ref):
            return "different" if ref == "main^{tree}" else await parse(root, ref)
        monkeypatch.setattr(ctx.git, "rev_parse", mismatch)
    before = await ctx.git.rev_parse(repo, "main")
    reviewed_sha = await ctx.git.rev_parse(repo, STEM)
    queue = MergeQueue(ctx, escalate=lambda _: None)
    if mode == "inline":
        result = await merge(ctx, ticket, attempt=run.attempt)
    elif mode == "queue":
        queue.offer(ticket, attempt=run.attempt)
        if case == "tree-mismatch":
            with pytest.raises(TreeMismatch):
                await queue.process()
            result = None
        else:
            result, = await queue.process()
    else:
        boundary = AdmissionBoundary(ctx, mode="daemon", queue=queue)
        result = await boundary(ticket, attempt=run.attempt)
        assert not queue.pending
    signals = [e for e in journal.read() if e.body.get("kind") == "retro_prompt_spec_change_merged"]
    if case in {"missing", "conflicting", "malformed", "failed"}:
        assert result.outcome == "gate_failed" and not signals
        assert await ctx.git.rev_parse(repo, "main") == before
        assert ctx.worktree(STEM).exists() and await ctx.git.rev_parse(repo, STEM)
        assert all(f.paved_road for f in result.findings)
        if case != "failed":
            assert any("restore" in f.paved_road and "Author" in f.paved_road for f in result.findings)
    elif case in {"no-spec", "unrelated", "tree-mismatch"}:
        assert not signals
    else:
        assert result.outcome == "ok"
        signal, = signals
        sha = result.artifact.commit
        assert signal.type == EventType.SIGNAL and signal.ticket == STEM
        assert signal.key == f"retro-prompt-spec-change/{STEM}/{sha}"
        assert signal.body == {"kind": "retro_prompt_spec_change_merged", "box_id": body["box_id"],
            "retro_report_key": "retro/1", "ticket_stem": STEM, "squash_sha": sha,
            "changed_spec_paths": ["specs/a.md", "specs/z.md"]}
        assert sha == await ctx.git.rev_parse(repo, "main")
        assert (await ctx.git._run(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha)).splitlines() == [
            "specs/a.md", "specs/readme.txt", "specs/z.md", "tempo/thing.py"]
        ctx.driver.effects = Effects(journal)
        before = journal.read()
        assert (await write_squash(ctx, ticket, reviewed_sha, attempt=run.attempt)).commit == sha
        assert journal.read() == before


@pytest.mark.parametrize('mode', ['inline', 'queue'])
@pytest.mark.parametrize('change', ['current', 'stale', 'after-rebase'])
def test_core_drift_merge_rerun(repo, mode, change):
    from tempo import hostfiles
    from tests.test_mergequeue import approve
    ctx, ticket, attempt = reviewed(repo, [agent({'tempo/thing.py': 'ok\n'}), verdict()])
    async def scenario():
        if change != 'current':
            target = repo if change == 'after-rebase' else ctx.worktree(STEM)
            path = target / 'AGENTS.md'
            path.write_bytes(hostfiles.render(b'project', template=b'stale engine\n'))
            await ctx.git.add(target, ['AGENTS.md'])
            await ctx.git.commit(target, 'committed stale core')
            if change == 'stale':
                ticket_with_fence = dataclasses.replace(ticket, scope_fence=(*ticket.scope_fence, 'AGENTS.md'))
                await approve(ctx, ticket_with_fence)
            else:
                ticket_with_fence = ticket
        else:
            ticket_with_fence = ticket
        before = await ctx.git.rev_parse(repo, 'main')
        if mode == 'inline':
            result = await merge(ctx, ticket_with_fence, attempt=attempt)
        else:
            queue = MergeQueue(ctx, escalate=lambda _: None)
            queue.offer(ticket_with_fence, attempt=attempt)
            [result] = await queue.process()
        if change == 'current':
            assert result.outcome == 'ok', result.findings
        else:
            refused_untouched(ctx, result, before, 'core_drift')
            finding = next(f for f in result.findings if f.code == 'core_drift')
            assert finding.path == 'AGENTS.md' and 'tempo core' in finding.paved_road
            if change == 'after-rebase':
                assert (ctx.worktree(STEM) / 'AGENTS.md').read_bytes() == (repo / 'AGENTS.md').read_bytes()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['inline', 'queue'])
def test_core_drift_candidate_template(repo, mode):
    from tempo import hostfiles
    from tests.test_mergequeue import approve
    ctx, ticket, attempt = reviewed(repo, [agent({'tempo/thing.py': 'ok\n'}), verdict()])
    serving = hostfiles.CORE_TEMPLATE
    candidate = b'## Candidate engine conduct\n\nNew branch rules.\n'
    async def scenario():
        root = ctx.worktree(STEM)
        ctx.fs.write(root / 'tempo/hostfiles.py', b'CORE_TEMPLATE = ' + repr(candidate).encode() + b'\n')
        ctx.fs.write(root / 'AGENTS.md', hostfiles.render(b'project\xff', template=candidate))
        await ctx.git.add(root, ['tempo/hostfiles.py', 'AGENTS.md'])
        await ctx.git.commit(root, 'congruent candidate template and block')
        local = dataclasses.replace(ticket, scope_fence=(*ticket.scope_fence, 'tempo/hostfiles.py', 'AGENTS.md'))
        await approve(ctx, local)
        if mode == 'inline':
            result = await merge(ctx, local, attempt=attempt)
        else:
            queue = MergeQueue(ctx, escalate=lambda _: None)
            queue.offer(local, attempt=attempt)
            [result] = await queue.process()
        assert result.outcome == 'ok', result.findings
        assert hostfiles.CORE_TEMPLATE == serving
        assert hostfiles.classify_core((repo / 'AGENTS.md').read_bytes()).state == 'drifted'
        assert hostfiles.classify_core((repo / 'AGENTS.md').read_bytes(), template=candidate).state == 'current'
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['inline', 'queue'])
@pytest.mark.parametrize('omitted', [None, 'CLAUDE.md', 'AGENTS.md'])
def test_core_activation_first_adoption_merge(repo, mode, omitted):
    from tempo import hostfiles, stages
    from tests.test_cli import core_config
    from tests.test_mergequeue import approve
    core_config(repo, [('medium', 'implement', ['claude', 'codex']), ('medium', 'review', ['claude', 'codex'])])
    original = {'CLAUDE.md': b'# Claude project\r\nno final newline', 'AGENTS.md': b'# Codex project\n'}
    for path, data in original.items():
        (repo / path).write_bytes(data)
    git(repo, 'add', 'config.yaml', 'CLAUDE.md', 'AGENTS.md')
    git(repo, 'commit', '-m', 'marker-free first adoption base')
    author(repo)
    path = repo / f'tickets/{STEM}/ticket.md'
    path.write_text(path.read_text().replace('## Scope fence\n- tempo/thing.py',
        '## Scope fence\n- tempo/thing.py\n- CLAUDE.md\n- AGENTS.md'))
    git(repo, 'add', str(path.relative_to(repo)))
    git(repo, 'commit', '-m', 'activation fixture fence')
    changes = {name: hostfiles.render(data).decode() for name, data in original.items() if name != omitted}
    changes['tempo/thing.py'] = 'ok adopted\n'
    ctx = context(repo, [agent(changes), verdict()])
    ticket = validate_ticket(STEM, path.read_text(), repo)
    async def scenario():
        run = await run_stages(ctx, ticket)
        assert run.results['check'].outcome == ('ok' if omitted is None else 'gate_failed')
        if omitted is not None:
            assert any(f.path == omitted and f.code == 'core_drift' for f in run.results['check'].findings)
            await approve(ctx, ticket)
        before = await ctx.git.rev_parse(repo, 'main')
        async def admission(local, attempt):
            if mode == 'inline':
                return await merge(local, ticket, attempt=attempt)
            queue = MergeQueue(local, escalate=lambda _: None)
            queue.offer(ticket, attempt=attempt)
            return (await queue.process())[0]
        result = await admission(ctx, run.attempt)
        if omitted is not None:
            refused_untouched(ctx, result, before, 'core_drift')
            return
        assert result.outcome == 'ok', result.findings
        for name, data in original.items():
            assert (repo / name).read_bytes() == hostfiles.render(data)
        # Next ordinary candidate inherits both files and needs no operator repair.
        local = context(repo, [agent({'tempo/thing.py': 'ok ordinary\n'}), verdict()])
        next_run = await run_stages(local, ticket)
        assert next_run.results['check'].outcome == 'ok'
        result = await admission(local, next_run.attempt)
        assert result.outcome == 'ok', result.findings
        assert (await local.git._run(repo, 'show', '--format=', '--name-only', 'main')).split() == ['tempo/thing.py']
    asyncio.run(scenario())
