"""Implement -> Check -> Review stage transitions with the scripted fake (TEMPO_PLAN.md sections 4, 5, 19.P1)."""

import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from tempo.config import load_config
from tempo.box import BOX_DIR, Box
from tempo.driver import Driver
from tempo.git import Git
from tempo.llm import FakeLLM, LLMRequest
from tempo.seams import LocalFileSystem, SubprocessExec
from tempo.stages import (
    DIFF_BUDGET_FILES,
    ApprovedInvoice,
    DiffBudgetGate,
    Evidence,
    Invoice,
    PackingSlip,
    RunRecordGate,
    SnagList,
    StageContext,
    _prior_findings,
    read_review,
    run_stages,
)
from tempo.tickets import validate_ticket


def test_retro_driver_access_is_public_and_read_only(repo):
    from tempo import runner
    from tempo.retro import Retro, RetroConstructionError
    from tests.test_drain import pause_checkout
    checkout = pause_checkout(repo)
    writer = runner.bind(checkout, FakeLLM([]))
    driver = writer.stages.driver
    assert Retro(checkout, writer).driver() is driver
    with pytest.raises(AttributeError):
        writer.stages.driver = None
    with pytest.raises(AttributeError):
        del writer.stages.driver
    with pytest.raises(RetroConstructionError, match="construct the pipeline with its shared Driver.*Pipeline.stages.driver"):
        Retro(checkout, SimpleNamespace()).driver()


SPECS = Path(__file__).resolve().parent.parent / "specs"
SECRET = "sk-test-not-for-children"
ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/nonexistent",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "FAKE_KEY": SECRET,
}

CONFIG = """schema_version: 1
state_dir: .tempo/state
review: {}
merge: {}
hosts:
  codex:
    models_by_tier: {low: m, medium: m, high: m, max: m}
    pins: {review: r}
orchestrator:
  models: {codex: o}
engine_plane_safety_inventory: [config.yaml]
"""

TICKET = """---
state: confirmed
source: human
priority: P2
kind: feature
{bypass}---

## Depends on
none

## Context
- tempo/thing.py

## Goal / Why
`thing.py` holds the word ok.

## Scope in / Scope out
In: thing. Out: the rest.

## Scope fence
- tempo/thing.py

## Acceptance criteria
1. `grep -q ok tempo/thing.py` exits 0.

## Verification
```
grep -q ok tempo/thing.py
env
```

## Definition of rejected
Needs a new dependency.

## Time budget
- stuck: 20m
"""

STEM = "add-thing"


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], env=ENV, capture_output=True, text=True,
                          check=True).stdout


def adopt_conduct(root: Path) -> list[str]:
    """Render each fixture's actual routes from its own project bytes before committing its base."""
    from tempo.hostfiles import render
    from tempo.hostfiles import conduct_paths

    paths = conduct_paths(load_config(None, cwd=root), root)
    for path in paths:
        path.write_bytes(render(path.read_bytes() if path.exists() else b""))
    return [p.relative_to(root).as_posix() for p in paths]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tempo").mkdir(parents=True)
    (root / "tempo" / "thing.py").write_text("")
    (root / "config.yaml").write_text(CONFIG)
    (root / ".gitignore").write_text(".tempo/\n")
    adopt_conduct(root)
    git(root, "init", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    return root


def author(repo: Path, bypass: str = "") -> None:
    path = repo / "tickets" / STEM / "ticket.md"
    path.parent.mkdir(parents=True)
    path.write_text(TICKET.format(bypass=bypass))
    git(repo, "add", "tickets")
    git(repo, "commit", "-m", f"tempo({STEM}): ticket")


def implement_reply(outcome: str = "ok", findings: list | None = None) -> str:
    return "```json\n" + json.dumps({
        "outcome": outcome, "summary": "wrote ok", "surprises": "", "dead_ends": "none",
        "findings": findings or [],
    }) + "\n```"


def agent(files: dict[str, str], outcome: str = "ok"):
    """A scripted implementer: edits the worktree, commits on the branch, replies."""

    def act(req: LLMRequest) -> str:
        assert req.worktree is not None
        for rel, text in files.items():
            (req.worktree / rel).parent.mkdir(parents=True, exist_ok=True)
            (req.worktree / rel).write_text(text)
        if files:
            git(req.worktree, "add", *files)
            git(req.worktree, "commit", "-m", "work")
        return implement_reply(outcome)

    return act


def verdict(v: str = "approve", findings: list | None = None) -> str:
    return json.dumps({"verdict": v, "summary": "looked", "findings": findings or []})


SNAG = {"code": "logic", "path": "tempo/thing.py", "line": 1, "message": "wrong word", "paved_road": "write ok"}


def run(repo: Path, script: list, bypass: str = ""):
    author(repo, bypass)
    config = load_config(None, cwd=repo)
    llm = FakeLLM(script)
    clock = lambda: datetime(2026, 10, 5, tzinfo=UTC)  # noqa: E731

    async def never(seconds: float) -> None:
        await asyncio.Event().wait()

    exec_ = SubprocessExec()
    driver = Driver.from_config(config, llm=llm, env=ENV, clock=clock, sleep=never)
    ctx = StageContext(repo=repo, config=config, env=ENV, exec_=exec_, git=Git(exec_, env=ENV, timeout=30.0),
                       fs=LocalFileSystem(), driver=driver, specs_dir=SPECS)
    ticket = validate_ticket(STEM, (repo / "tickets" / STEM / "ticket.md").read_text(), repo)
    return asyncio.run(run_stages(ctx, ticket)), llm, config


def subjects(repo: Path) -> list[str]:
    return git(repo, "log", "--format=%s", "main").splitlines()[::-1]


def outcomes(result) -> dict[str, str]:
    return {name: r.outcome for name, r in result.results.items()}


def _report_ticket(monkeypatch, verification: str) -> None:
    monkeypatch.setattr(__import__(__name__, fromlist=["TICKET"]), "TICKET",
                        TICKET.replace("env\n", verification + "\n"))


def test_implement_check_review_all_ok_with_artifacts_and_provenance(repo):
    result, llm, config = run(repo, [agent({"tempo/thing.py": "ok\n"}), verdict()])

    assert outcomes(result) == {"implement": "ok", "check": "ok", "review": "ok"}
    head = git(repo, "rev-parse", STEM).strip()
    slip, invoice, review = (r.artifact for r in result.results.values())
    assert isinstance(slip, PackingSlip) and slip.produced_at_sha == head and slip.produced_by_spec_version == 1
    assert isinstance(invoice, Invoice) and invoice.passed and invoice.changed_files == ["tempo/thing.py"]
    assert invoice.produced_at_sha == head
    assert isinstance(review, ApprovedInvoice) and review.reviewed_sha == head and review.spec_version == "1.1"
    assert (review.provider, review.model) == ("fake", "fake")

    # One ticket-plane commit per stage terminal, on main, never on the branch.
    assert subjects(repo)[-3:] == [f"tempo({STEM}): run-record", f"tempo({STEM}): checks", f"tempo({STEM}): review"]
    canonical = repo / "tickets" / STEM
    assert read_review((canonical / "review.md").read_text()) == review
    assert Invoice.model_validate_json((canonical / "checks.json").read_text()) == invoice
    run_md = (canonical / "run.md").read_text()
    assert "## Outcome\n\nok\n" in run_md and "- provider: fake\n- model: fake\n- spec: implement 1.7" in run_md
    assert git(repo, "diff", "--name-only", f"main...{STEM}").split() == ["tempo/thing.py"]
    outbox = config.worktree_root / STEM / "tickets" / STEM
    assert sorted(p.name for p in outbox.iterdir()) == ["ticket.md"]  # lifted and unlinked

    implement_req, review_req = llm.requests
    assert implement_req.surface == "implement" and implement_req.worktree == config.worktree_root / STEM
    assert "### tempo/thing.py" in implement_req.rendered
    assert review_req.surface == "review" and review_req.worktree is None  # read-only surface
    assert "+ok" in review_req.rendered  # the reviewer sees `git diff main...<stem>`


def test_an_untracked_foreign_ticket_is_ignored(repo):
    def act(req: LLMRequest) -> str:
        assert req.worktree is not None
        (req.worktree / "tempo/thing.py").write_text("ok\n")
        git(req.worktree, "add", "tempo/thing.py")
        git(req.worktree, "commit", "-m", "work")
        foreign = req.worktree / "tickets" / "foreign" / "ticket.md"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("untracked ticket\n")
        return implement_reply()

    result, llm, _ = run(repo, [act, verdict()])

    assert outcomes(result) == {"implement": "ok", "check": "ok", "review": "ok"}
    assert all(req.surface != "requisition_review" for req in llm.requests)
    assert not (repo / "tickets" / "foreign" / "ticket.md").exists()


def test_old_checks_json_without_verification_still_feeds_prior_findings(repo):
    (repo / "tempo" / "thing.py").write_text("ok base\n")
    git(repo, "commit", "-am", "green base")
    result, _, _ = run(repo, [agent({"tempo/thing.py": "nope\n"})])
    path = repo / "tickets" / STEM / "checks.json"
    old = json.loads(path.read_text())
    del old["verification"]
    path.write_text(json.dumps(old))

    assert Invoice.model_validate_json(path.read_text()).verification == []
    assert [f.code for f in _prior_findings(SimpleNamespace(repo=repo), STEM, "check")] == ["verification"]


def test_premise_failed_stops_after_implement_with_its_run_record(repo):
    finding = {"code": "premise", "path": None, "line": None, "message": "contradicts X", "paved_road": "drop X"}
    act = lambda req: implement_reply("premise_failed", [finding])  # noqa: E731
    result, llm, _ = run(repo, [act])

    assert outcomes(result) == {"implement": "premise_failed"}
    assert [f.message for f in result.last[1].findings] == ["contradicts X"]
    assert "## Outcome\n\npremise_failed\n" in (repo / "tickets" / STEM / "run.md").read_text()
    assert len(llm.requests) == 1


def test_check_fails_closed_on_an_out_of_fence_edit_and_never_reaches_review(repo):
    result, llm, _ = run(repo, [agent({"tempo/thing.py": "ok\n", "tempo/other.py": "x\n"})])

    assert outcomes(result) == {"implement": "ok", "check": "gate_failed"}
    assert [(f.code, f.path) for f in result.last[1].findings] == [("scope_fence", "tempo/other.py")]
    assert not result.last[1].artifact.passed
    assert json.loads((repo / "tickets" / STEM / "checks.json").read_text())["passed"] is False
    assert len(llm.requests) == 1  # review never called


def test_gate_bypass_demotes_its_code_to_soft(repo):
    bypass = "gate_bypass:\n  - {code: scope_fence, reason: generated sibling}\n"
    result, _, _ = run(repo, [agent({"tempo/thing.py": "ok\n", "tempo/other.py": "x\n"}), verdict()], bypass)

    assert outcomes(result) == {"implement": "ok", "check": "ok", "review": "ok"}
    invoice = result.results["check"].artifact
    assert invoice.bypassed == ["scope_fence"]
    assert [f.code for f in result.results["check"].findings] == ["scope_fence"]  # still reported, soft


def test_red_verification_fails_check(repo):
    (repo / "tempo" / "thing.py").write_text("ok base\n")
    git(repo, "commit", "-am", "green base")
    result, _, config = run(repo, [agent({"tempo/thing.py": "nope\n"})])

    assert outcomes(result) == {"implement": "ok", "check": "gate_failed"}
    assert [f.code for f in result.last[1].findings] == ["verification"]
    assert "grep -q ok tempo/thing.py" in result.last[1].findings[0].message
    assert Box(config.state_dir / BOX_DIR, LocalFileSystem()).messages() == []
    assert not (config.worktree_root / ".base" / STEM).exists()


def test_base_red_command_is_recorded_and_filed_without_failing_check(repo, monkeypatch):
    _report_ticket(monkeypatch, "test -f tempo/thing.py")
    result, _, config = run(repo, [agent({"tempo/thing.py": "changed\n"}), verdict()])

    assert outcomes(result)["check"] == "ok"
    invoice = Invoice.model_validate_json((repo / "tickets" / STEM / "checks.json").read_text())
    assert len(invoice.verification) == 2
    assert invoice.verification[0].base_red is True and invoice.verification[0].rc == 1
    assert invoice.verification[1].base_red is False and invoice.verification[1].rc == 0
    messages = Box(config.state_dir / BOX_DIR, LocalFileSystem()).messages()
    assert len(messages) == 1
    assert (messages[0].message_class, messages[0].outcome, messages[0].origin) == (
        "failure_report", "base_red", STEM)
    assert "grep -q ok tempo/thing.py" in messages[0].summary
    assert (config.state_dir / "spools" / STEM / str(result.attempt) / "check" /
            "verify-01-base.txt").is_file()
    assert not (config.worktree_root / ".base" / STEM).exists()


def test_empty_diff_claimed_ok_fails_verification(repo):
    (repo / "tempo" / "thing.py").write_text("ok\n")
    git(repo, "commit", "-am", "green base")
    result, _, _ = run(repo, [agent({})])

    assert outcomes(result) == {"implement": "ok", "check": "gate_failed"}
    assert [f.message for f in result.last[1].findings] == ["the branch carries no committed change"]


def test_already_satisfied_is_proven_by_green_verification_and_skips_review(repo):
    (repo / "tempo" / "thing.py").write_text("ok\n")
    git(repo, "commit", "-am", "already there")
    result, llm, _ = run(repo, [agent({}, outcome="already_satisfied")])

    assert outcomes(result) == {"implement": "ok", "check": "already_satisfied"}
    assert result.results["implement"].artifact.outcome == "already_satisfied"
    assert len(llm.requests) == 1


def test_review_snag_is_gate_failed_with_a_snag_list(repo):
    result, _, _ = run(repo, [agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG])])

    assert outcomes(result) == {"implement": "ok", "check": "ok", "review": "gate_failed"}
    assert [f.message for f in result.last[1].findings] == ["wrong word"]
    pinned = read_review((repo / "tickets" / STEM / "review.md").read_text())
    assert isinstance(pinned, SnagList) and pinned.reviewed_sha == git(repo, "rev-parse", STEM).strip()


def test_invalid_review_reply_is_reprompted_with_its_validation_finding(repo):
    result, llm, _ = run(repo, [agent({"tempo/thing.py": "ok\n"}), verdict("approve", [SNAG]), verdict()])

    assert outcomes(result)["review"] == "ok"
    assert "approve carries an empty findings list" in llm.requests[2].rendered


def test_a_fresh_run_tears_down_the_prior_worktree_and_branch(repo):
    stale = load_config(None, cwd=repo).worktree_root / STEM
    git(repo, "worktree", "add", "-b", STEM, str(stale), "main")
    (stale / "tempo" / "thing.py").write_text("stale\n")
    git(stale, "commit", "-am", "stale")

    result, _, _ = run(repo, [agent({"tempo/thing.py": "ok\n"}), verdict()])

    assert outcomes(result)["review"] == "ok"
    assert "stale" not in git(repo, "log", "--format=%s", STEM)


# --- gates --------------------------------------------------------------------------------------


def evidence(**kw) -> Evidence:
    base = dict(stem=STEM, head_sha="abc", claimed="ok", scope_fence=["tempo/"], changed_files=["tempo/a.py"],
                inserted_lines=1, verification=[], run_record=None)
    return Evidence(**{**base, **kw})




def test_diff_budget_caps_files_and_inserted_lines(tmp_path):
    gate = DiffBudgetGate()
    assert gate.check(evidence(), tmp_path).verdict == "pass"
    over = evidence(changed_files=[f"tempo/{n}.py" for n in range(DIFF_BUDGET_FILES + 1)])
    assert "split the ticket" in gate.check(over, tmp_path).findings[0].paved_road
    assert gate.check(evidence(inserted_lines=1_501), tmp_path).verdict == "fail"


def test_run_record_gate_requires_every_section_and_a_closed_outcome(tmp_path):
    gate = RunRecordGate()
    assert gate.check(evidence(), tmp_path).findings[0].message == "run.md is missing"
    bad = "## Outcome\n\nshipped\n\n## Dead ends\n\nnone\n"
    messages = [f.message for f in gate.check(evidence(run_record=bad), tmp_path).findings]
    assert len(messages) == 2 and "'shipped' is not an outcome" in messages[1]


def test_gather_safety_evidence_runs_no_verification(repo, monkeypatch):
    import dataclasses
    from tempo import stages
    from tests.test_merge import context
    author(repo)
    ctx = context(repo, [])
    ticket = validate_ticket(STEM, (repo / "tickets" / STEM / "ticket.md").read_text(), repo)
    ticket = dataclasses.replace(ticket, scope_fence=("tempo/thing.py",))

    async def scenario():
        ctx.fs.write(repo / "tickets" / STEM / "run.md", b"lifted run record")
        await ctx.git.worktree_add(repo, ctx.worktree(STEM), STEM, "main")
        (ctx.worktree(STEM) / "tempo/thing.py").write_text("ok\n")
        await ctx.git.add(ctx.worktree(STEM), ["tempo/thing.py"])
        await ctx.git.commit(ctx.worktree(STEM), "work")
        original = ctx.exec_.run
        calls = []
        async def recording(argv, **kw):
            calls.append((argv, kw))
            return await original(argv, **kw)
        monkeypatch.setattr(ctx.exec_, "run", recording)
        safety = await stages.gather_safety_evidence(ctx, ticket, "ok")
        assert safety.verification == []
        assert safety.run_record == "lifted run record" and safety.inserted_lines > 0
        assert all(argv[0] == "git" and "worktree" not in argv for argv, _ in calls)
        full = await stages.gather_evidence(ctx, ticket, "ok", attempt=0)
        assert full.model_copy(update={"verification": []}) == safety
        assert len(full.verification) == len(ticket.verification)
    asyncio.run(scenario())


def test_full_gather_evidence_reuses_safety_gatherer(repo, monkeypatch):
    from tempo import stages
    from tests.test_merge import context
    author(repo)
    ctx = context(repo, [])
    ticket = validate_ticket(STEM, (repo / "tickets" / STEM / "ticket.md").read_text(), repo)
    original = stages.gather_safety_evidence
    gathered = []
    async def recording(*args):
        evidence = await original(*args)
        gathered.append(evidence)
        return evidence
    monkeypatch.setattr(stages, "gather_safety_evidence", recording)

    async def scenario():
        await ctx.git.worktree_add(repo, ctx.worktree(STEM), STEM, "main")
        (ctx.worktree(STEM) / "tempo/thing.py").write_text("changed\n")
        await ctx.git.add(ctx.worktree(STEM), ["tempo/thing.py"])
        await ctx.git.commit(ctx.worktree(STEM), "work")
        result = await stages.gather_evidence(ctx, ticket, "ok", attempt=4)
        assert len(gathered) == 1
        assert gathered[0] == result.model_copy(update={"verification": []})
        assert result.verification[0].base_red
        assert result.verification[1].rc == 0
        assert (ctx.config.state_dir / "spools" / STEM / "4/check/verify-01-base.txt").is_file()
        assert not (ctx.config.worktree_root / ".base" / STEM).exists()
    asyncio.run(scenario())


