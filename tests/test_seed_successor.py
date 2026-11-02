"""A reviewed seed becomes runnable after its parent's real admission, in one CLI drain."""

from tests.test_stages import adopt_conduct

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from tempo import drain, runner
from tempo.__main__ import main
from tempo.git import Git
from tempo.journal import EventType, render_ts
from tempo.llm import FakeLLM, LLMRequest
from tempo.seams import LocalFileSystem, SubprocessExec
from tempo.stages import Invoice, read_review
from tempo.status import last_states
from tempo.tickets import ticket_path, validate_ticket
from tests.test_stages import CONFIG, ENV, TICKET, implement_reply, verdict

PARENT = "proof-parent"
SUCCESSOR = "proof-successor"
NOW = datetime(2026, 10, 6, tzinfo=UTC)


def wait_for(coro):
    # FakeLLM callbacks are synchronous, but Git and the real scan are asynchronous.
    with ThreadPoolExecutor(max_workers=1) as worker:
        return worker.submit(asyncio.run, coro).result()


def ticket_text(stem: str) -> str:
    text = (TICKET.format(bypass="")
            .replace("tempo/thing.py", f"work/{stem}.py")
            .replace("`thing.py`", f"`{stem}.py`")
            .replace("env\n", "")
            .replace("kind: feature", "kind: chore\nagent_tier: medium\nagent_effort: medium")
            .replace("## Goal / Why", "## Plan contract\n- 19.L\n- 19.P3\n\n## Goal / Why"))
    if stem == PARENT:
        return text.replace(f"- work/{stem}.py\n\n## Acceptance criteria",
                            f"- work/{stem}.py\n- tickets\n\n## Acceptance criteria")
    return text.replace("source: human", "source: seed").replace(
        "## Depends on\nnone", f"## Depends on\n- {PARENT}")


def test_merged_parent_registers_and_runs_successor_in_same_drain(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    fs = LocalFileSystem()
    git = Git(SubprocessExec(), env={k: v for k, v in ENV.items() if k != "FAKE_KEY"}, timeout=30.0)

    def git_out(root: Path, *argv: str) -> str:
        return wait_for(git._run(root, *argv)).strip()

    seed_text = ticket_text(SUCCESSOR)
    initial = {
        ".gitignore": ".tempo/\n",
        "TEMPO_PLAN.md": "### 19.L Build laws\nSeed law.\n\n### 19.P3 Phase 3\nPhase law.\n",
        "config.yaml": CONFIG,
        f"work/{PARENT}.py": "",
        f"work/{SUCCESSOR}.py": "",
        ticket_path(PARENT): ticket_text(PARENT),
    }
    for rel, text in initial.items():
        fs.write(repo / rel, text.encode())
    conduct = adopt_conduct(repo)
    wait_for(git.init(repo, branch="main"))
    wait_for(git.add(repo, [*initial, *conduct]))
    wait_for(git.commit(repo, "base with only the parent ticket"))
    start = wait_for(git.rev_parse(repo, "main"))
    assert sorted(p.parent.name for p in (repo / "tickets").glob("*/ticket.md")) == [PARENT]
    assert ticket_path(SUCCESSOR) not in wait_for(git.ls_files(repo))

    dispatches = []
    reviewed = {}
    snapshots = []
    checkouts = []

    def envelope(type_, ticket, key, body):
        return {"v": 1, "type": type_, "ts": render_ts(NOW), "ticket": ticket, "key": key, "body": body}

    def seed_records(checkout):
        blob = wait_for(git.rev_parse(repo, f"main:{ticket_path(SUCCESSOR)}"))
        assert wait_for(git._run(repo, "show", f"main:{ticket_path(SUCCESSOR)}")) == seed_text
        invoice = Invoice.model_validate_json(git_out(repo, "show", f"main:tickets/{PARENT}/checks.json"))
        assert invoice.passed
        assert invoice.changed_files == [f"work/{PARENT}.py"]
        assert [s.model_dump() for s in invoice.seeds] == [{
            "stem": SUCCESSOR, "ticket_sha": blob, "verdict": "approve", "findings": [], "mechanical": None,
        }]
        history = checkout.journal.read()
        [approval] = [e for e in history if e.body.get("signal") == "requisition_verdict"]
        assert asdict(approval) == envelope(EventType.SIGNAL, SUCCESSOR, None, {
            "signal": "requisition_verdict", "seeding": PARENT, "verdict": "approve",
            "ticket_sha": blob, "findings": [], "text": seed_text,
        })
        [intake] = [e for e in history if e.body.get("signal") == "ticket_intake" and e.ticket == SUCCESSOR]
        commit = intake.body["commit"]
        assert asdict(intake) == envelope(EventType.SIGNAL, SUCCESSOR, None, {
            "signal": "ticket_intake", "source": "seed", "state": "confirmed", "new": True,
            "commit": commit, "seeded_by": PARENT,
        })
        assert git_out(repo, "show", "-s", "--format=%s", commit) == f"tempo({PARENT}): seeds"
        assert git_out(repo, "show", "--name-only", "--format=", commit).splitlines() == [ticket_path(SUCCESSOR)]
        assert wait_for(git.rev_parse(repo, f"{commit}:{ticket_path(SUCCESSOR)}")) == blob
        key = f"ticket-plane/{PARENT}/0/seeds"
        effects = [e for e in history if e.key == key]
        assert [asdict(e) for e in effects] == [
            envelope(EventType.EFFECT_INTENT, PARENT, key, {}),
            envelope(EventType.EFFECT_COMPLETION, PARENT, key, {"result": {"commit": commit}}),
        ]
        assert history.index(approval) < history.index(effects[0]) < history.index(effects[1]) < history.index(intake)

    def implement(stem):
        def act(req: LLMRequest) -> str:
            assert (req.surface, req.ticket) == ("implement", stem)
            assert req.worktree is not None
            assert dispatches == ([PARENT] if stem == PARENT else [PARENT, SUCCESSOR])
            if stem == PARENT:
                assert not (repo / ticket_path(SUCCESSOR)).exists()
                fs.write(req.worktree / ticket_path(SUCCESSOR), seed_text.encode())
                seed = validate_ticket(SUCCESSOR, seed_text, req.worktree)
                assert seed.depends == (PARENT,)
                assert seed.frontmatter.model_dump() == {
                    "state": "confirmed", "source": "seed", "priority": "P2", "kind": "chore",
                    "agent_tier": "medium", "agent_effort": "medium", "gate_bypass": [],
                }
            path = f"work/{stem}.py"
            fs.write(req.worktree / path, b"ok = True\n")
            wait_for(git.add(req.worktree, [path]))
            wait_for(git.commit(req.worktree, f"implement {stem}"))
            reviewed[stem] = wait_for(git.rev_parse(req.worktree, "HEAD"))
            assert git_out(req.worktree, "show", "--name-only", "--format=", "HEAD").splitlines() == [path]
            if stem == PARENT:
                assert ticket_path(SUCCESSOR) not in wait_for(git.ls_files(req.worktree))
                assert (req.worktree / ticket_path(SUCCESSOR)).read_text() == seed_text
            return implement_reply()
        return act

    def seed_review(req: LLMRequest) -> str:
        assert (req.surface, req.ticket, req.worktree) == ("requisition_review", None, None)
        assert seed_text in req.rendered
        assert dispatches == [PARENT]
        assert not (repo / ticket_path(SUCCESSOR)).exists()
        return verdict()

    def parent_review(req: LLMRequest) -> str:
        assert (req.surface, req.ticket, req.worktree) == ("review", PARENT, None)
        [checkout] = checkouts
        seed_records(checkout)
        history = checkout.journal.read()
        assert last_states(history) == {PARENT: "running"}
        assert dispatches == [PARENT]
        probe = drain._Drain(checkout, runner.bind(checkout, llm), frozenset())
        scan = wait_for(probe._scan())
        assert set(scan.tickets) == {PARENT, SUCCESSOR}
        assert not scan.invalid and not scan.drafts
        assert scan.tickets[SUCCESSOR].depends == (PARENT,)
        # The active parent is unavailable to this diagnostic pick; its seed is still blocked.
        assert probe._select(scan, history, last_states(history), {PARENT}) == (None, False)
        snapshots.append("seed visible, dependency unsettled")
        return verdict()

    def successor_review(req: LLMRequest) -> str:
        assert (req.surface, req.ticket, req.worktree) == ("review", SUCCESSOR, None)
        assert last_states(checkouts[0].journal.read()) == {PARENT: "merged", SUCCESSOR: "running"}
        return verdict()

    llm = FakeLLM([implement(PARENT), seed_review, parent_review, implement(SUCCESSOR), successor_review,
                   '{"proposals": []}'])

    async def never(seconds: float) -> None:
        await asyncio.Event().wait()

    def pipeline(checkout):
        checkout = replace(checkout, sleep=never)
        checkouts.append(checkout)
        bound = runner.bind(checkout, llm)
        probe = drain._Drain(checkout, bound, frozenset())
        scan = wait_for(probe._scan())
        assert set(scan.tickets) == {PARENT}
        assert checkout.journal.read() == []
        assert probe._select(scan, [], {}, set()) == (scan.tickets[PARENT], False)

        async def dispatch(ticket):
            if ticket.stem == SUCCESSOR:
                assert snapshots == ["seed visible, dependency unsettled"]
                history = checkout.journal.read()
                assert last_states(history)[PARENT] == "merged"
                scan = await probe._scan()
                pick, _ = probe._select(scan, history, last_states(history), set())
                assert pick is not None and pick.stem == SUCCESSOR
                seed_records(checkout)
            dispatches.append(ticket.stem)
            return await bound(ticket)
        dispatch.stages = bound.stages
        return dispatch

    assert main(["drain"], cwd=repo, env=ENV, clock=lambda: NOW, pipeline=pipeline) == 0
    assert len(checkouts) == 1
    assert dispatches == [PARENT, SUCCESSOR]
    assert [(r.surface, r.ticket) for r in llm.requests] == [
        ("implement", PARENT), ("requisition_review", None), ("review", PARENT),
        ("implement", SUCCESSOR), ("review", SUCCESSOR),
        ("retro", None),
    ]
    assert not llm.script and llm.aborted == 0
    [checkout] = checkouts
    seed_records(checkout)
    history = checkout.journal.read()
    terminals = [e for e in history if e.type == EventType.STATE_TRANSITION and e.body.get("to") == "merged"]
    assert [e.ticket for e in terminals] == [PARENT, SUCCESSOR]
    for event in terminals:
        stem = event.ticket
        commit = event.body["commit"]
        assert asdict(event) == envelope(EventType.STATE_TRANSITION, stem, None, {
            "to": "merged", "commit": commit, "reviewed_sha": reviewed[stem],
        })
        assert git_out(repo, "show", "--name-only", "--format=", commit).splitlines() == [f"work/{stem}.py"]
        message = git_out(repo, "show", "-s", "--format=%B", commit)
        assert f"tempo-ticket: {stem}" in message
        assert f"tempo-reviewed-sha: {reviewed[stem]}" in message
        approval = read_review((repo / "tickets" / stem / "review.md").read_text())
        assert approval.verdict == "approve" and approval.reviewed_sha == reviewed[stem]
        assert not (checkout.config.worktree_root / stem).exists()
        assert git_out(repo, "branch", "--list", stem) == ""
    [successor_running] = [e for e in history if e.type == EventType.STATE_TRANSITION
                           and e.ticket == SUCCESSOR and e.body.get("to") == "running"]
    assert history.index(terminals[0]) < history.index(successor_running)
    assert git_out(repo, "log", "--format=%s", f"{start}..main").splitlines().count(
        f"tempo({PARENT}): seeds") == 1
    [receipt] = [e for e in history if e.type == EventType.EFFECT_COMPLETION and e.key == "retro/1"]
    assert git_out(repo, "rev-parse", "main") == receipt.body["result"]["commit"]
    assert git_out(repo, "rev-parse", "main~1") == terminals[1].body["commit"]
    assert wait_for(git.status_porcelain(repo)) == ""
    probe = drain._Drain(checkout, runner.bind(checkout, llm), frozenset())
    scan = wait_for(probe._scan())
    assert set(scan.tickets) == {PARENT, SUCCESSOR} and not scan.invalid and not scan.drafts
    assert probe._select(scan, history, last_states(history), set()) == (None, False)
    out = capsys.readouterr()
    assert out.err == ""
    assert out.out == drain.Report(merged=[PARENT, SUCCESSOR]).render()
