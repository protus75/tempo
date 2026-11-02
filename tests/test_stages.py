"""Implement -> Check -> Review stage transitions with the scripted fake (TEMPO_PLAN.md sections 4, 5, 19.P1)."""

import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from tempo.config import load_config
from tempo.artifacts import SHAKEOUT_REPORT, ShakeoutEntry, ShakeoutReport
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
    ScopeFenceGate,
    SnagList,
    StageContext,
    _prior_findings,
    read_review,
    run_stages,
)
from tempo.tickets import TicketInvalid, validate_ticket


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


def test_exit_window_is_materialized_before_the_real_implement_call(repo, monkeypatch):
    from dataclasses import asdict
    from tempo import stages
    from tempo.journal import EventType
    from tests.test_merge import context
    author(repo)
    path = repo / f"tickets/{STEM}/ticket.md"
    text = path.read_text() + "\n## Exit-read window\n-  signal:  current retro window  \n- state_transition: current retro window\n"
    path.write_text(text)
    git(repo, "add", f"tickets/{STEM}/ticket.md")
    git(repo, "commit", "-m", "exit declaration")
    seen = []
    def act(req):
        data = json.loads((req.worktree / ".tempo/exit-read-window.json").read_text())
        assert data == {"signal: current retro window": [asdict(first)], "state_transition: current retro window": []}
        assert '.tempo/exit-read-window.json' in req.rendered
        seen.append(data)
        return implement_reply(outcome="already_satisfied")
    ctx = context(repo, [act])
    first = ctx.driver.journal.append(EventType.SIGNAL, {"kind": "drain_handoff"}, ticket=None)
    prepare = stages.prepare_worktree
    async def append_during_prepare(*args):
        result = await prepare(*args)
        ctx.driver.journal.append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket="later")
        ctx.driver.journal.append(EventType.SIGNAL, {"kind": "drain_handoff"}, ticket="later")
        return result
    monkeypatch.setattr(stages, "prepare_worktree", append_during_prepare)
    ticket = validate_ticket(STEM, text, repo)
    assert asyncio.run(stages.implement(ctx, ticket, attempt=0)).outcome == "ok"
    assert len(seen) == 1

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
providers:
  - name: fake
    kind: cli
    package: test-cli
    auth: FAKE_KEY
    models_by_tier: {low: m, medium: m, high: m, max: m}
    limits: {concurrency: 1, est_cost_per_call_usd: 0.5}
routing:
  - {tier: medium, surface: implement, candidates: [{provider: fake}]}
  - {tier: medium, surface: review, candidates: [{provider: fake}]}
review: {}
merge: {}
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
- expected: 10m
- stuck: 20m
"""

STEM = "add-thing"


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], env=ENV, capture_output=True, text=True,
                          check=True).stdout


def adopt_conduct(root: Path) -> list[str]:
    """Render each fixture's actual routes from its own project bytes before committing its base."""
    from tempo.hostfiles import render
    from tempo.providers import routed_conduct_paths

    paths = routed_conduct_paths(load_config(None, cwd=root), root)
    for path in paths:
        path.write_bytes(render(path.read_bytes() if path.exists() else b""))
    return [p.relative_to(root).as_posix() for p in paths]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tempo").mkdir(parents=True)
    (root / "tempo" / "thing.py").write_text("")
    (root / "TEMPO_PLAN.md").write_text("# Plan\n")
    (root / "config.yaml").write_text(CONFIG)
    (root / ".gitignore").write_text(".tempo/\n")
    adopt_conduct(root)
    git(root, "init", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    return root


def author(repo: Path, bypass: str = "", plan_contract: str = "") -> None:
    path = repo / "tickets" / STEM / "ticket.md"
    path.parent.mkdir(parents=True)
    path.write_text(TICKET.format(bypass=bypass).replace("## Goal / Why", plan_contract + "## Goal / Why"))
    git(repo, "add", "tickets")
    git(repo, "commit", "-m", f"tempo({STEM}): ticket")


def implement_reply(outcome: str = "ok", findings: list | None = None) -> str:
    return "```json\n" + json.dumps({
        "outcome": outcome, "summary": "wrote ok", "surprises": "", "dead_ends": "none",
        "predicted_vs_actual": "10m predicted, 2m actual", "findings": findings or [],
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


def run(repo: Path, script: list, bypass: str = "", plan_contract: str = ""):
    author(repo, bypass, plan_contract)
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


def _report_bytes() -> str:
    entry = ShakeoutEntry(member="member_one", group="first", planted_fault="fault", expected="ok",
                          observed="ok", producing_run="member_one/0", auditor=[], green=True)
    return ShakeoutReport(produced_by_spec_version=1, produced_at_sha="abc", entries=[entry]).model_dump_json()


def _report_ticket(monkeypatch, verification: str) -> None:
    monkeypatch.setattr(__import__(__name__, fromlist=["TICKET"]), "TICKET",
                        TICKET.replace("env\n", verification + "\n"))


def _write_outbox_report(path: str, body: str):
    def act(req: LLMRequest) -> str:
        assert req.worktree is not None
        (req.worktree / "tempo/thing.py").write_text("ok\n")
        git(req.worktree, "add", "tempo/thing.py")
        git(req.worktree, "commit", "-m", "work")
        target = req.worktree / "tickets" / STEM / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        return implement_reply()
    return act


def test_verification_report_lifts_only_in_checks_commit(repo, monkeypatch):
    (repo / "source.json").write_text(_report_bytes())
    git(repo, "add", "source.json")
    git(repo, "commit", "-m", "source")
    _report_ticket(monkeypatch, f"cp source.json tickets/{STEM}/{SHAKEOUT_REPORT}")
    result, _, _ = run(repo, [agent({"tempo/thing.py": "ok\n"}), verdict()])
    assert outcomes(result)["check"] == "ok"
    assert (repo / "tickets" / STEM / SHAKEOUT_REPORT).is_file()
    run_commit = git(repo, "log", "--format=%H", "--grep", f"tempo({STEM}): run-record").splitlines()[0]
    assert SHAKEOUT_REPORT not in git(repo, "show", "--format=", "--name-only", run_commit)
    checks_commit = git(repo, "log", "--format=%H", "--grep", f"tempo({STEM}): checks").splitlines()[0]
    assert SHAKEOUT_REPORT in git(repo, "show", "--format=", "--name-only", checks_commit)


def test_invalid_report_fails_check_without_lifting_checks_or_report(repo, monkeypatch):
    (repo / "source.json").write_text('{"invalid":true}')
    git(repo, "add", "source.json")
    git(repo, "commit", "-m", "source")
    _report_ticket(monkeypatch, f"cp source.json tickets/{STEM}/{SHAKEOUT_REPORT}")
    result, _, _ = run(repo, [agent({"tempo/thing.py": "ok\n"})])
    assert outcomes(result)["check"] == "gate_failed"
    assert [f.code for f in result.last[1].findings] == ["verification"]
    assert not (repo / "tickets" / STEM / "checks.json").exists()
    assert not (repo / "tickets" / STEM / SHAKEOUT_REPORT).exists()
    assert f"tempo({STEM}): checks" not in subjects(repo)


@pytest.mark.parametrize("path", [SHAKEOUT_REPORT, f"x/{SHAKEOUT_REPORT}"])
@pytest.mark.parametrize("body", [_report_bytes(), '{"invalid":true}'])
def test_implement_report_is_not_lifted_and_stale_report_is_purged(repo, path, body):
    result, _, config = run(repo, [_write_outbox_report(path, body), verdict()])
    assert outcomes(result)["check"] == "ok"
    assert not list((repo / "tickets" / STEM).rglob(SHAKEOUT_REPORT))
    assert not list((config.worktree_root / STEM / "tickets" / STEM).rglob(SHAKEOUT_REPORT))
    assert SHAKEOUT_REPORT not in git(repo, "show", "--format=", "--name-only", "main~2")


@pytest.mark.parametrize("flag", ["--out", "--out="])
def test_named_missing_report_fails_even_when_other_commands_pass(repo, monkeypatch, flag):
    target = f"tickets/{STEM}/{SHAKEOUT_REPORT}"
    command = f"true {flag} {target}" if flag == "--out" else f"true {flag}{target}"
    _report_ticket(monkeypatch, command)
    result, _, _ = run(repo, [agent({"tempo/thing.py": "ok\n"})])
    assert outcomes(result)["check"] == "gate_failed"
    assert [f.code for f in result.last[1].findings] == ["verification"]


# --- transitions --------------------------------------------------------------------------------


def test_implement_renders_numeric_plan_contract_section(repo):
    (repo / "TEMPO_PLAN.md").write_text("# Plan\n\n## 11. Failure spine\n\nSpine prose.\n")
    git(repo, "commit", "-am", "plan section 11")

    result, llm, _ = run(repo, [agent({"tempo/thing.py": "ok\n"}), verdict()],
                         plan_contract="## Plan contract\n- section 11\n\n")

    assert outcomes(result) == {"implement": "ok", "check": "ok", "review": "ok"}
    assert "## 11. Failure spine\n\nSpine prose." in llm.requests[0].rendered


def test_bare_numeric_plan_contract_bullet_is_refused_at_intake(repo):
    (repo / "TEMPO_PLAN.md").write_text("# Plan\n\n## 11. Failure spine\n")
    author(repo, plan_contract="## Plan contract\n- 11\n\n")

    with pytest.raises(TicketInvalid) as e:
        validate_ticket(STEM, (repo / "tickets" / STEM / "ticket.md").read_text(), repo)
    assert any("Plan contract" in finding.message for finding in e.value.findings)


def test_implement_check_review_all_ok_with_artifacts_and_provenance(repo):
    result, llm, config = run(repo, [agent({"tempo/thing.py": "ok\n"}), verdict()])

    assert outcomes(result) == {"implement": "ok", "check": "ok", "review": "ok"}
    head = git(repo, "rev-parse", STEM).strip()
    slip, invoice, review = (r.artifact for r in result.results.values())
    assert isinstance(slip, PackingSlip) and slip.produced_at_sha == head and slip.produced_by_spec_version == 1
    assert isinstance(invoice, Invoice) and invoice.passed and invoice.changed_files == ["tempo/thing.py"]
    assert invoice.produced_at_sha == head
    assert isinstance(review, ApprovedInvoice) and review.reviewed_sha == head and review.spec_version == "1.0"
    assert (review.provider, review.model) == ("fake", "fake")

    # One ticket-plane commit per stage terminal, on main, never on the branch.
    assert subjects(repo)[-3:] == [f"tempo({STEM}): run-record", f"tempo({STEM}): checks", f"tempo({STEM}): review"]
    canonical = repo / "tickets" / STEM
    assert read_review((canonical / "review.md").read_text()) == review
    assert Invoice.model_validate_json((canonical / "checks.json").read_text()) == invoice
    run_md = (canonical / "run.md").read_text()
    assert "## Outcome\n\nok\n" in run_md and "- provider: fake\n- model: fake\n- spec: implement 1.4" in run_md
    assert git(repo, "diff", "--name-only", f"main...{STEM}").split() == ["tempo/thing.py"]
    outbox = config.worktree_root / STEM / "tickets" / STEM
    assert sorted(p.name for p in outbox.iterdir()) == ["ticket.md"]  # lifted and unlinked

    implement_req, review_req = llm.requests
    assert implement_req.surface == "implement" and implement_req.worktree == config.worktree_root / STEM
    assert "### tempo/thing.py" in implement_req.rendered
    assert review_req.surface == "review" and review_req.worktree is None  # read-only surface
    assert "+ok" in review_req.rendered  # the reviewer sees `git diff main...<stem>`


def test_non_seeding_ticket_ignores_an_untracked_foreign_ticket(repo):
    def act(req: LLMRequest) -> str:
        assert req.worktree is not None
        (req.worktree / "tempo/thing.py").write_text("ok\n")
        git(req.worktree, "add", "tempo/thing.py")
        git(req.worktree, "commit", "-m", "work")
        foreign = req.worktree / "tickets" / "foreign-seed" / "ticket.md"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("untracked seed\n")
        return implement_reply()

    result, llm, _ = run(repo, [act, verdict()])

    assert outcomes(result) == {"implement": "ok", "check": "ok", "review": "ok"}
    assert result.results["check"].artifact.seeds == []
    assert all(req.surface != "requisition_review" for req in llm.requests)
    assert not (repo / "tickets" / "foreign-seed" / "ticket.md").exists()
    assert f"tempo({STEM}): seeds" not in subjects(repo)


def test_old_checks_json_without_seeds_still_parses(repo):
    result, _, _ = run(repo, [agent({"tempo/thing.py": "ok\n"}), verdict()])
    old = json.loads((repo / "tickets" / STEM / "checks.json").read_text())
    del old["seeds"]

    parsed = Invoice.model_validate(old)

    assert parsed.seeds == [] and parsed.passed == result.results["check"].artifact.passed


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


def test_verification_children_never_inherit_a_provider_key(repo):
    result, _, config = run(repo, [agent({"tempo/thing.py": "ok\n"}), verdict()])

    spool = (config.state_dir / "spools" / STEM / str(result.attempt) / "check" / "verify-02.txt").read_text()
    assert "PATH=/usr/bin:/bin" in spool and "FAKE_KEY" not in spool and SECRET not in spool


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


UNIT_MAIN = "## 19. P\n\n### 19.P3 Phase\n\nreg\n\n### 19.P4 Next\n\nn\n"
UNIT_HEAD = UNIT_MAIN.replace("### 19.P4 Next", "### 19.P3.row Row\n\n- **Owner:** o\n\n### 19.P4 Next")


def test_scope_fence_admits_a_plan_edit_confined_to_its_anchored_unit(tmp_path):
    gate = ScopeFenceGate()
    anchored = dict(scope_fence=["TEMPO_PLAN.md#19.P3.row"], changed_files=["TEMPO_PLAN.md"])
    assert gate.check(evidence(**anchored, plan_main=UNIT_MAIN, plan_head=UNIT_HEAD), tmp_path).verdict == "pass"
    escaped = UNIT_HEAD.replace("reg\n", "reg edited\n")
    assert gate.check(evidence(**anchored, plan_main=UNIT_MAIN, plan_head=escaped), tmp_path).verdict == "fail"
    other = dict(anchored, scope_fence=["TEMPO_PLAN.md#19.P3.other"])
    assert gate.check(evidence(**other, plan_main=UNIT_MAIN, plan_head=UNIT_HEAD), tmp_path).verdict == "fail"
    assert gate.check(evidence(changed_files=["TEMPO_PLAN.md"]), tmp_path).verdict == "fail"


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
    ticket = dataclasses.replace(ticket, scope_fence=("tempo/thing.py", "TEMPO_PLAN.md#19.P3.row"))

    async def scenario():
        ctx.fs.write(repo / "TEMPO_PLAN.md", UNIT_MAIN.encode())
        await ctx.git.add(repo, ["TEMPO_PLAN.md"])
        await ctx.git.commit(repo, "plan")
        ctx.fs.write(repo / "tickets" / STEM / "run.md", b"lifted run record")
        await ctx.git.worktree_add(repo, ctx.worktree(STEM), STEM, "main")
        (ctx.worktree(STEM) / "tempo/thing.py").write_text("ok\n")
        ctx.fs.write(ctx.worktree(STEM) / "TEMPO_PLAN.md", UNIT_HEAD.encode())
        await ctx.git.add(ctx.worktree(STEM), ["tempo/thing.py", "TEMPO_PLAN.md"])
        await ctx.git.commit(ctx.worktree(STEM), "work")
        original = ctx.exec_.run
        calls = []
        async def recording(argv, **kw):
            calls.append((argv, kw))
            return await original(argv, **kw)
        monkeypatch.setattr(ctx.exec_, "run", recording)
        safety = await stages.gather_safety_evidence(ctx, ticket, "ok")
        assert safety.verification == []
        assert safety.plan_main == UNIT_MAIN and safety.plan_head == UNIT_HEAD
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


def test_scope_fence_admits_a_plan_edit_confined_to_all_anchored_units(tmp_path):
    gate = ScopeFenceGate()
    main = UNIT_HEAD.replace("### 19.P4 Next", "### 19.P3.other Other\n\nold\n\n### 19.P4 Next")
    head = main.replace("- **Owner:** o", "- **Owner:** changed").replace("old\n", "new\n")
    anchored = dict(scope_fence=["TEMPO_PLAN.md#19.P3.row", "TEMPO_PLAN.md#19.P3.other"],
                    changed_files=["TEMPO_PLAN.md"], plan_main=main)
    assert gate.check(evidence(**anchored, plan_head=head), tmp_path).verdict == "pass"
    assert gate.check(evidence(**anchored, plan_head=head.replace("reg\n", "escaped\n")), tmp_path).verdict == "fail"
    inserted = main.replace("### 19.P4 Next", "### 19.P3.missing Missing\n\nadded\n\n### 19.P4 Next")
    anchored["scope_fence"].append("TEMPO_PLAN.md#19.P3.missing")
    assert gate.check(evidence(**anchored, plan_head=inserted.replace("old\n", "new\n")), tmp_path).verdict == "pass"


async def report_context(repo, *, verification=None, source=None, script=None):
    """A disposable producer whose report is written by real Verification, never by its agent."""
    from tests.test_merge import context
    ctx = context(repo, script if script is not None else [agent({}), verdict()])
    ctx.fs.write(repo / "tempo/thing.py", b"ok\n")
    ctx.fs.write(repo / "source.json", (source if source is not None else _report_bytes()).encode())
    command = verification if verification is not None else f"cp source.json tickets/{STEM}/{SHAKEOUT_REPORT}"
    raw = TICKET.format(bypass="").replace("grep -q ok tempo/thing.py\nenv", command)
    ctx.fs.write(repo / f"tickets/{STEM}/ticket.md", raw.encode())
    await ctx.git.add(repo, ["tempo/thing.py", "source.json", f"tickets/{STEM}/ticket.md"])
    await ctx.git.commit(repo, "producer fixture")
    ticket = validate_ticket(STEM, raw, repo)
    return ctx, ticket


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_outbox_only_check_accepts_registered_report(repo, nested):
    from tempo.journal import EventType
    name = f"nested/{SHAKEOUT_REPORT}" if nested else SHAKEOUT_REPORT
    commands = (f"mkdir -p tickets/{STEM}/nested\n" if nested else "") + f"cp source.json tickets/{STEM}/{name}"
    ctx, ticket = await report_context(repo, verification=commands)
    run = await run_stages(ctx, ticket)
    assert outcomes(run) == {"implement": "ok", "check": "ok", "review": "ok"}
    assert await ctx.git.diff_names(repo, "main", STEM) == []
    assert run.results["check"].artifact.changed_files == []
    assert run.results["review"].artifact.reviewed_sha == run.results["implement"].artifact.produced_at_sha
    completions = [e for e in ctx.driver.journal.read() if e.type == EventType.EFFECT_COMPLETION]
    [checks] = [e for e in completions if e.key == f"ticket-plane/{STEM}/0/checks"]
    rel = f"tickets/{STEM}/{name}"
    assert checks.body["result"]["paths"] == [f"tickets/{STEM}/checks.json", rel]
    assert checks.body["result"]["kind"] == "checks"
    data = await ctx.git._run(repo, "show", f"{checks.body['result']['commit']}:{rel}")
    assert ShakeoutReport.model_validate_json(data).model_dump_json() == _report_bytes()
    assert all(rel not in e.body["result"].get("paths", []) for e in completions if e is not checks)
    assert not (ctx.worktree(STEM) / rel).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["none", "records", "unknown", "foreign", "malformed", "implement",
                                  "inherited", "equal-copy", "dirty-tracked", "dirty-untracked"])
async def test_outbox_only_check_requires_current_report(repo, case):
    commands = {
        "records": f"cp source.json tickets/{STEM}/review.md",
        "unknown": f"cp source.json tickets/{STEM}/unknown.json",
        "foreign": f"mkdir -p tickets/foreign\ncp source.json tickets/foreign/{SHAKEOUT_REPORT}",
        "malformed": f"cp source.json tickets/{STEM}/{SHAKEOUT_REPORT}",
        "equal-copy": f"cp source.json tickets/{STEM}/{SHAKEOUT_REPORT}",
        "dirty-tracked": f"cp source.json tickets/{STEM}/{SHAKEOUT_REPORT}",
        "dirty-untracked": f"cp source.json tickets/{STEM}/{SHAKEOUT_REPORT}",
    }
    def act(req):
        if case == "implement":
            (req.worktree / f"tickets/{STEM}/{SHAKEOUT_REPORT}").write_text(_report_bytes())
        if case.startswith("dirty"):
            path = "tempo/thing.py" if case == "dirty-tracked" else "tempo/untracked.py"
            (req.worktree / path).write_text("uncommitted\n")
        return implement_reply()
    ctx, ticket = await report_context(repo, verification=commands.get(case, "true"), script=[act],
                                       source='{"invalid":true}' if case == "malformed" else None)
    if case in {"inherited", "equal-copy"}:
        rel = f"tickets/{STEM}/{SHAKEOUT_REPORT}"
        ctx.fs.write(repo / rel, _report_bytes().encode())
        await ctx.git.add(repo, [rel])
        await ctx.git.commit(repo, "inherited report rejection fixture")
    run = await run_stages(ctx, ticket)
    assert outcomes(run) == {"implement": "ok", "check": "gate_failed"}
    assert not run.results["check"].artifact.passed
    assert run.last[1].findings and all(f.paved_road for f in run.last[1].findings)
    assert len(ctx.driver.llm.requests) == 1
    if case in {"implement", "inherited"}:
        assert not list((ctx.worktree(STEM) / f"tickets/{STEM}").rglob(SHAKEOUT_REPORT))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["red", "missing", "invalid", "lift"])
async def test_outbox_only_check_failure_never_admits(repo, monkeypatch, failure):
    from tempo.git import GitError
    from tempo.journal import EventType
    commands = f"cp source.json tickets/{STEM}/{SHAKEOUT_REPORT}"
    if failure == "red":
        commands += "\nfalse"
    elif failure == "missing":
        commands = f"true --out=tickets/{STEM}/{SHAKEOUT_REPORT}"
    ctx, ticket = await report_context(repo, verification=commands, script=[agent({})],
                                      source='{"invalid":true}' if failure == "invalid" else None)
    if failure == "lift":
        commit = ctx.git.commit
        async def failing(root, message, **kwargs):
            if message == f"tempo({STEM}): checks":
                raise GitError(["git", "commit"], 1, "", "injected checks failure")
            await commit(root, message, **kwargs)
        monkeypatch.setattr(ctx.git, "commit", failing)
    run = await run_stages(ctx, ticket)
    assert outcomes(run) == {"implement": "ok", "check": "gate_failed"}
    assert not run.last[1].artifact.passed
    assert all(f.code == "verification" and f.paved_road for f in run.last[1].findings)
    events = ctx.driver.journal.read()
    assert not any(e.type == EventType.STATE_TRANSITION and e.body.get("to") == "merged" for e in events)
    assert not any((e.key or "").startswith("merge/") for e in events)

    if failure in {"invalid", "lift"}:
        assert not any(e.type == EventType.EFFECT_COMPLETION and e.key == f"ticket-plane/{STEM}/0/checks"
                       for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["valid", "purge", "missing", "invalid", "machinery"])
async def test_review_baseline_report_registered_checks_lift(repo, tmp_path, case):
    from tempo.artifacts import REVIEW_BASELINE_REPORT, ReviewBaselineReport
    from tempo.journal import EventType
    from tempo import stages
    from eval import harness
    from tests.test_eval_harness import report_fixture
    data_dir = tmp_path / "grade"
    data_dir.mkdir()
    report, _, _ = report_fixture(data_dir)
    source = data_dir / "canonical.json"
    harness.write_report(source, report, fs=LocalFileSystem())
    assert ReviewBaselineReport.model_validate_json(source.read_bytes()) == report
    assert stages.KNOWN_ARTIFACTS[REVIEW_BASELINE_REPORT] is ReviewBaselineReport
    target = f"tickets/{STEM}/{REVIEW_BASELINE_REPORT}"
    verification = (f"cp source.json {target}" if case in {"valid", "purge", "invalid"}
                    else f"true --out={target}" if case == "missing" else "true")
    captured = []
    def act(req):
        if case in {"purge", "machinery"}:
            path = req.worktree / target
            path.write_text('{"stale":true}')
            captured.append(path)
        return implement_reply()
    ctx, ticket = await report_context(repo, verification=verification,
        source='{"invalid":true}' if case == "invalid" else source.read_text(), script=[act, verdict()])
    if case == "purge":
        execute = ctx.exec_.run
        async def observe(argv, **kwargs):
            if argv[0] == "cp":
                assert not (ctx.worktree(STEM) / target).exists()
            return await execute(argv, **kwargs)
        ctx.exec_.run = observe
    result = await run_stages(ctx, ticket)
    expected = "gate_failed" if case in {"missing", "invalid", "machinery"} else "ok"
    assert result.results["check"].outcome == expected
    events = ctx.driver.journal.read()
    lifts = [e for e in events if e.type == EventType.EFFECT_COMPLETION
             and target in e.body.get("result", {}).get("paths", [])]
    if expected == "ok":
        [lift] = lifts
        assert lift.key == f"ticket-plane/{STEM}/0/checks"
        assert lift.body["result"]["kind"] == "checks"
        data = await ctx.git._run(repo, "show", f"{lift.body['result']['commit']}:{target}")
        assert ReviewBaselineReport.model_validate_json(data) == report
        assert not (ctx.worktree(STEM) / target).exists()
    else:
        assert not lifts and not (repo / target).exists()
    if captured:
        assert not captured[0].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["host-loop-report.json", "exit-receipt.json"])
@pytest.mark.parametrize("case", ["valid", "purge", "missing", "invalid", "machinery"])
async def test_host_loop_and_receipt_registered_checks_lift(repo, tmp_path, name, case):
    from tempo.artifacts import HOST_LOOP_REPORT, EXIT_RECEIPT, HostLoopReport, ExitReceipt
    from tempo import stages
    from tempo.journal import EventType
    from eval import host_loop
    from tests.test_host_loop import loop_report
    from tests.test_eval_harness import report_fixture
    data_dir = tmp_path / "receipt-data"
    data_dir.mkdir()
    baseline, _, _ = report_fixture(data_dir)
    source = data_dir / name
    if name == HOST_LOOP_REPORT:
        host_loop.write_report(source, loop_report(), fs=LocalFileSystem())
        model = HostLoopReport
    else:
        host_loop.write_receipt(source, host_loop.canonical(loop_report()), host_loop.canonical(baseline),
                               fs=LocalFileSystem())
        model = ExitReceipt
    assert stages.KNOWN_ARTIFACTS[HOST_LOOP_REPORT] is HostLoopReport
    assert stages.KNOWN_ARTIFACTS[EXIT_RECEIPT] is ExitReceipt
    target = f"tickets/{STEM}/{name}"
    verification = (f"cp source.json {target}" if case in {"valid", "purge", "invalid"}
                    else f"true --out={target}" if case == "missing" else "true")
    captured = []
    def act(req):
        if case in {"purge", "machinery"}:
            path = req.worktree / target
            path.write_text('{"stale":true}')
            captured.append(path)
        return implement_reply()
    ctx, ticket = await report_context(repo, verification=verification,
        source='{"invalid":true}' if case == "invalid" else source.read_text(), script=[act, verdict()])
    if case == "purge":
        execute = ctx.exec_.run
        async def observe(argv, **kwargs):
            if argv[0] == "cp":
                assert not (ctx.worktree(STEM) / target).exists()
            return await execute(argv, **kwargs)
        ctx.exec_.run = observe
    result = await run_stages(ctx, ticket)
    expected = "gate_failed" if case in {"missing", "invalid", "machinery"} else "ok"
    assert result.results["check"].outcome == expected
    lifts = [e for e in ctx.driver.journal.read() if e.type == EventType.EFFECT_COMPLETION
             and target in e.body.get("result", {}).get("paths", [])]
    if expected == "ok":
        [lift] = lifts
        assert lift.key == f"ticket-plane/{STEM}/0/checks" and lift.body["result"]["kind"] == "checks"
        committed = await ctx.git._run(repo, "show", f"{lift.body['result']['commit']}:{target}")
        assert model.model_validate_json(committed) == model.model_validate_json(source.read_bytes())
    else:
        assert not lifts and not (repo / target).exists()
    assert not (ctx.worktree(STEM) / target).exists()
    assert all(not p.exists() for p in captured)
