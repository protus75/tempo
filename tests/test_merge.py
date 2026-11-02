"""Merge admission against a temp repo (TEMPO_PLAN.md sections 7, 9, 10; 19.P1)."""

from tests.test_stages import adopt_conduct

import asyncio
import dataclasses
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
from tests.test_stages import ENV, SNAG, SPECS, STEM, agent, author, git, verdict
from tempo.checkout import Checkout, Refusal

GOAL = "`thing.py` holds the word ok."


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tempo").mkdir(parents=True)
    (root / "tempo" / "thing.py").write_text("")
    (root / "tempo" / "other.py").write_text("")
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
    checkout = Checkout(repo, source.config, source.env, source.exec_, source.git,
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
        assert main(["run", STEM] if verb == "run" else ["drain"], cwd=repo, env=ENV,
                    clock=checkout.clock, pipeline=pipeline) == 0
    [(ctx, original_ticket, attempt)] = calls
    assert original_ticket.stem == ticket.stem and attempt == 0
    assert len(merged_events(ctx)) == 1
    assert not any(e.body.get("kind") == CONFLICT_FACTS for e in ctx.driver.journal.read())
    assert (repo / "tempo/thing.py").read_text() == "ok\n" and not ctx.worktree(STEM).exists()


def test_run_keeps_inline_admission(repo, monkeypatch):
    test_bootstrap_pipeline_keeps_inline_admission(repo, monkeypatch, "run")


def test_daemon_prechecks_precede_offer(repo, monkeypatch):
    ctx, ticket, attempt = reviewed(repo, [agent({"tempo/thing.py": "ok\n"}), verdict()])
    async def scenario():
        code = "post_rebase_regate"
        path = ctx.worktree(ticket.stem) / "tickets/committed.md"
        ctx.fs.write(path, b"forbidden code-lane ticket\n")
        await ctx.git.add(ctx.worktree(ticket.stem), ["tickets/committed.md"])
        await ctx.git.commit(ctx.worktree(ticket.stem), "code lane violation")
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
        with pytest.raises(Refusal, match="supply.*MergeQueue.*serve composition"):
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


def test_pipeline_requires_supplied_control(repo):
    from tempo import merge as owner
    from tempo.__main__ import build_control
    ctx = context(repo, [])
    checkout = Checkout(repo, ctx.config, ctx.env, ctx.exec_, ctx.git,
                               ctx.driver.journal, ctx.fs, ctx.driver.clock, ctx.driver.sleep)
    consumer = build_control(checkout)
    with pytest.raises(TypeError):
        owner.compose_pipeline(ctx, escalate=lambda _: None)
    with pytest.raises(Refusal, match="build_control.*Checkout.control"):
        owner.compose_pipeline(ctx, escalate=lambda _: None, control=None)
    queue = owner.compose_pipeline(ctx, escalate=lambda _: None, control=consumer)
    assert queue.control is consumer and queue.hold_id is None
    assert MergeQueue(ctx, escalate=lambda _: None).control is None
    assert ctx.driver.journal.read() == []


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
