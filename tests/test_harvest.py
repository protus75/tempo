"""Failed-run harvest through the production CLI and ticket-plane lane."""

import json
from pathlib import Path

import pytest

from tempo import runner
from tempo.__main__ import main
from tempo.artifacts import Harvest, StageResult
from tempo.journal import EventType, Journal
from tempo.llm import FakeLLM
from tempo.specs import data_close, data_open
from tempo.stages import PRIOR_ATTEMPTS, StagesRun
from tests.test_stages import ENV, SNAG, STEM, agent, git, verdict
from tests.test_terminal import author, clock, diagnosis_reply, repo  # noqa: F401 -- fixture


def run(repo: Path, script: list, verb: str = "run") -> tuple[int, FakeLLM]:
    llm = FakeLLM(script)
    argv = [verb, STEM] if verb == "run" else [verb]
    code = main(argv, cwd=repo, env=ENV, clock=clock, pipeline=lambda c: runner.bind(c, llm))
    return code, llm


def events(repo: Path):
    return [e for e in Journal(repo / ".tempo" / "state", clock).read() if e.ticket == STEM]


def harvest(repo: Path, attempt: int = 0) -> Harvest:
    path = repo / "tickets" / STEM / "attempts" / str(attempt) / "harvest.json"
    return Harvest.model_validate_json(path.read_text())


def criteria(rendered: str) -> str:
    block = rendered.split(data_open("ticket"), 1)[1].split(data_close("ticket"), 1)[0]
    return block.split("## Acceptance criteria", 1)[1].split("## Verification", 1)[0]


def test_review_snag_commits_closed_harvest_without_code_content_and_wipes(repo):
    author(repo)
    marker = "CODE_EDIT_MARKER_NEVER_IN_HARVEST"
    long_summary = "x" * (runner.HARVEST_TAIL_CHARS + 100)

    def review(req):
        path = repo / ".tempo" / "state" / "spools" / "providers" / STEM / "fake-0001" / "events.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("e" * (runner.HARVEST_TAIL_CHARS + 100))
        return json.dumps({"verdict": "snag", "summary": long_summary, "findings": [SNAG]})

    code, _ = run(repo, [agent({"tempo/thing.py": f"ok # {marker}\n"}), review, diagnosis_reply()])
    assert code == 1
    h = harvest(repo)
    assert h.terminal == "gate_failed" and h.stage == "review" and h.attempt == 0
    assert [f.model_dump(exclude_none=True) for f in h.findings] == [SNAG]
    assert "tempo/thing.py" in h.diff_stat and marker not in h.diff_stat
    assert len(h.stage_log_tail) <= runner.HARVEST_TAIL_CHARS
    assert len(h.events_tail) <= runner.HARVEST_TAIL_CHARS
    assert h.run_record == f"tickets/{STEM}/run.md"
    assert set(h.model_dump()) == set(Harvest.model_fields)
    for path in (repo / "tickets" / STEM / "attempts").rglob("*"):
        if path.is_file():
            assert marker not in path.read_text()
    log = git(repo, "log", "--format=%s", "main").splitlines()
    assert log[0] == f"tempo({STEM}): diagnosis" and f"tempo({STEM}): harvest" in log
    assert not (repo / ".tempo" / "state" / "worktrees" / STEM).exists()
    assert git(repo, "rev-parse", "--verify", STEM).strip()


def test_infra_harvest_precedes_cap_and_terminal_and_carries_spooled_error(repo):
    author(repo)

    def fail_with_unreviewed_outbox(req):
        extra = req.worktree / "tickets" / STEM / "attempts" / "0" / "leak.txt"
        extra.parent.mkdir(parents=True)
        extra.write_text("unreviewed agent material")
        raise RuntimeError("provider exploded at call")

    code, _ = run(repo, [fail_with_unreviewed_outbox, diagnosis_reply()])
    assert code == 1
    h = harvest(repo)
    assert h.findings == [] and "provider exploded at call" in h.reason
    assert not (repo / "tickets" / STEM / "attempts" / "0" / "leak.txt").exists()
    history = events(repo)
    lift = next(i for i, e in enumerate(history) if e.type == EventType.EFFECT_COMPLETION
                and e.key == f"ticket-plane/{STEM}/0/harvest")
    cap = next(i for i, e in enumerate(history) if e.type == EventType.CAP_CONSUMED
               and e.body["cap"] == "infra")
    terminal = next(i for i, e in enumerate(history) if e.type == EventType.STATE_TRANSITION
                    and e.body.get("to") == "infra_error")
    assert lift < cap < terminal
    assert not (repo / ".tempo" / "state" / "worktrees" / STEM).exists()


def test_harvest_failure_is_soft(repo, monkeypatch):
    author(repo)

    async def broken(*args, **kwargs):
        raise RuntimeError("cannot harvest")

    monkeypatch.setattr(runner, "harvest", broken)
    code, _ = run(repo, [RuntimeError("provider failed"), diagnosis_reply()])
    assert code == 1
    history = events(repo)
    assert [e.body for e in history if e.type == EventType.SIGNAL
            and e.body.get("signal") == "harvest_failed"] == [
                {"signal": "harvest_failed", "error": "RuntimeError: cannot harvest"}]
    assert any(e.type == EventType.STATE_TRANSITION and e.body.get("to") == "infra_error" for e in history)
    assert not (repo / ".tempo" / "state" / "worktrees" / STEM).exists()


def test_missing_worktree_skips_harvest(repo, monkeypatch):
    author(repo)

    async def no_workspace(ctx, ticket):
        return StagesRun(attempt=0, results={"implement": StageResult(
            outcome="premise_failed", artifact=None, findings=[])})

    monkeypatch.setattr(runner, "run_stages", no_workspace)
    code, _ = run(repo, [])
    assert code == 1
    assert not (repo / "tickets" / STEM / "attempts").exists()
    assert not any(e.type == EventType.SIGNAL and e.body.get("signal") == "harvest_failed" for e in events(repo))


def test_merged_run_has_no_attempts_dir(repo):
    author(repo)
    code, _ = run(repo, [agent({"tempo/thing.py": "ok\n"}), verdict()])
    assert code == 0
    assert not (repo / "tickets" / STEM / "attempts").exists()


def test_drain_reentry_quotes_prior_harvest_and_summarizes_older(repo):
    author(repo)
    first = "first dead end"
    second = "second dead end"
    code, llm = run(repo, [RuntimeError(first), diagnosis_reply(), RuntimeError(second), diagnosis_reply(),
                           agent({"tempo/thing.py": "ok\n"}), verdict()], verb="drain")
    assert code == 0
    renders = [criteria(r.rendered) for r in llm.requests if r.surface == "implement"]
    assert len(renders) == 3
    assert PRIOR_ATTEMPTS not in renders[0]
    assert f"tickets/{STEM}/attempts/0/" in renders[1]
    assert "> Apply the terminal findings." in renders[1]
    assert "> Apply the terminal findings." in renders[2]
    assert first not in renders[2]
    assert "- older attempt 0: `infra_error` at implement:" in renders[2]
    for line in renders[2].splitlines():
        if first in line or second in line:
            assert line.startswith("> ")
