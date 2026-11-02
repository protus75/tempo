"""Reconcile-on-entry harvest through the production run composition."""

from pathlib import Path

from tempo import runner, stages
from tempo.__main__ import main
from tempo.artifacts import Harvest
from tempo.journal import EventType, Journal
from tempo.llm import FakeLLM
from tempo.specs import data_close, data_open
from tempo.stages import PRIOR_ATTEMPTS
from tests.test_stages import ENV, STEM, agent, git, verdict
from tests.test_terminal import author, clock, repo  # noqa: F401 -- fixture


def history(repo: Path):
    return [e for e in Journal(repo / ".tempo" / "state", clock).read() if e.ticket == STEM]


def invoke(repo: Path, script: list, stem: str = STEM) -> tuple[int, FakeLLM]:
    llm = FakeLLM(script)
    code = main(["run", stem], cwd=repo, env=ENV, clock=clock, pipeline=lambda c: runner.bind(c, llm))
    return code, llm


def orphan(repo: Path, attempt: int = 0) -> Path:
    author(repo)
    git(repo, "add", f"tickets/{STEM}/ticket.md")
    git(repo, "commit", "-m", "ticket")
    j = Journal(repo / ".tempo" / "state", clock)
    if attempt:
        j.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket=STEM)
        j.append(EventType.STATE_TRANSITION, {"to": "gate_failed", "stage": "implement"}, ticket=STEM)
    j.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket=STEM)
    path = repo / ".tempo" / "state" / "worktrees" / STEM
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", STEM, str(path), "main")
    (path / "tempo" / "thing.py").write_text("orphan edit\n")
    git(path, "add", "tempo/thing.py")
    git(path, "commit", "-m", "interrupted work")
    spool = repo / ".tempo" / "state" / "spools" / STEM / str(attempt) / "implement"
    spool.mkdir(parents=True)
    (spool / "error.txt").write_text("provider died with useful detail")
    return path


def test_run_harvests_orphan_before_reap_and_rerenders_its_reason(repo):
    path = orphan(repo, attempt=1)
    code, llm = invoke(repo, [agent({"tempo/thing.py": "ok\n"}), verdict()])

    assert code == 0
    saved = repo / "tickets" / STEM / "attempts" / "1" / "harvest.json"
    harvest = Harvest.model_validate_json(saved.read_text())
    assert harvest.attempt == 1 and harvest.terminal == "abandoned" and harvest.stage is None
    assert harvest.findings == [] and harvest.wall_seconds is None
    assert "tempo/thing.py" in harvest.diff_stat
    assert "provider died with useful detail" in harvest.reason
    assert f"tempo({STEM}): harvest" in git(repo, "log", "--format=%s", "main")
    events = history(repo)
    lift = next(i for i, e in enumerate(events) if e.type == EventType.EFFECT_COMPLETION
                and e.key == f"ticket-plane/{STEM}/1/harvest")
    abandoned = next(i for i, e in enumerate(events) if e.type == EventType.STATE_TRANSITION
                     and e.body.get("to") == "abandoned")
    assert lift < abandoned
    assert not path.exists()
    implement = next(r.rendered for r in llm.requests if r.surface == "implement")
    ticket_block = implement.split(data_open("ticket"), 1)[1].split(data_close("ticket"), 1)[0]
    assert PRIOR_ATTEMPTS in ticket_block
    assert "provider died with useful detail" in ticket_block


def test_missing_worktree_reaps_without_a_harvest_commit(repo):
    Journal(repo / ".tempo" / "state", clock).append(
        EventType.STATE_TRANSITION, {"to": "running"}, ticket=STEM)
    code, _ = invoke(repo, [], stem="missing")
    assert code == 2
    assert any(e.type == EventType.STATE_TRANSITION and e.body.get("to") == "abandoned"
               for e in history(repo))
    assert f"tempo({STEM}): harvest" not in git(repo, "log", "--format=%s", "main")


def test_harvest_failure_signals_and_still_reaps(repo, monkeypatch):
    path = orphan(repo)

    async def broken(*args, **kwargs):
        raise RuntimeError("cannot harvest")

    monkeypatch.setattr(stages, "harvest", broken)
    code, _ = invoke(repo, [], stem="missing")
    assert code == 2
    events = history(repo)
    assert [e.body for e in events if e.type == EventType.SIGNAL
            and e.body.get("signal") == "harvest_failed"] == [
                {"signal": "harvest_failed", "error": "RuntimeError: cannot harvest"}]
    assert any(e.type == EventType.STATE_TRANSITION and e.body.get("to") == "abandoned" for e in events)
    assert not path.exists()
