"""Non-ok terminal handling behind `run <stem>` (TEMPO_PLAN.md sections 11.2, 18, 19.P1).

A non-ok terminal journals the run's single terminal transition and exits 1, leaving ticket and branch in
place; an engine-plane refusal exits 2 with nothing dispatched. Retry, diagnosis, and harvest are Phase 2.
"""

from tests.test_stages import adopt_conduct

import json

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tempo import runner
from tempo.__main__ import main
from tempo.config import load_config
from tempo.journal import EventType, Journal
from tempo.llm import FakeLLM
from tests.test_stages import CONFIG, ENV, SNAG, STEM, TICKET, agent, git, implement_reply, verdict


def clock() -> datetime:
    return datetime(2026, 10, 5, tzinfo=UTC)


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


def author(repo: Path, stem: str = STEM, depends: str = "none") -> None:
    """Hand-author the ticket file only: `run` intake commits it."""
    path = repo / "tickets" / stem / "ticket.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TICKET.format(bypass="").replace("## Depends on\nnone", f"## Depends on\n{depends}"))


def run(repo: Path, script: list) -> tuple[int, FakeLLM]:
    llm = FakeLLM(script)
    code = main(["run", STEM], cwd=repo, env=ENV, clock=clock, pipeline=lambda c: runner.bind(c, llm))
    return code, llm


def diagnosis_reply(*lessons: str) -> str:
    return json.dumps({"verdict": "retry", "lessons": list(lessons or ("Apply the terminal findings.",))})


def transitions(repo: Path) -> list[dict]:
    return [e.body for e in Journal(repo / ".tempo" / "state", clock).read()
            if e.type == EventType.STATE_TRANSITION and e.ticket == STEM]


def left_in_place(repo: Path, expected: str = "") -> None:
    """Ticket committed on main, branch still there, main's code untouched."""
    assert git(repo, "ls-files", f"tickets/{STEM}/ticket.md").strip() == f"tickets/{STEM}/ticket.md"
    assert git(repo, "rev-parse", "--verify", STEM).strip()
    assert git(repo, "show", "main:tempo/thing.py") == expected


def test_a_review_snag_journals_its_terminal_and_exits_1(repo):
    author(repo)
    code, llm = run(repo, [agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG]), diagnosis_reply()])

    assert code == runner.EXIT_TICKET
    assert transitions(repo) == [{"to": "running"}, {"to": "gate_failed", "stage": "review",
                                                       "reason": "logic", "dispatch": "retry"}]
    left_in_place(repo)
    assert git(repo, "show", f"{STEM}:tempo/thing.py") == "ok\n"  # the branch keeps the work


def test_a_red_check_journals_its_terminal_and_never_reaches_review(repo):
    (repo / "tempo" / "thing.py").write_text("ok base\n")
    git(repo, "commit", "-am", "green verification base")
    author(repo)
    code, llm = run(repo, [agent({"tempo/thing.py": "nope\n"}), diagnosis_reply()])

    assert code == runner.EXIT_TICKET
    assert transitions(repo)[-1] == {"to": "gate_failed", "stage": "check", "reason": "verification",
                                     "dispatch": "retry"}
    assert [r.surface for r in llm.requests if r.surface != "diagnose"] == ["implement"]
    left_in_place(repo, "ok base\n")


def test_a_spent_retry_cap_is_a_non_ok_terminal(repo):
    author(repo)
    cap = load_config(None, cwd=repo).caps.retry
    code, llm = run(repo, ["not json"] * (cap + 1) + [diagnosis_reply()])

    assert code == runner.EXIT_TICKET
    assert len([r for r in llm.requests if r.surface != "diagnose"]) == cap + 1
    assert transitions(repo)[-1] == {"to": "invalid_artifact", "stage": "implement",
                                     "reason": "invalid_artifact", "dispatch": "retry"}
    left_in_place(repo)


def test_a_premise_failure_journals_its_terminal(repo):
    author(repo)
    finding = {"code": "premise", "message": "thing.py is generated", "paved_road": "edit the generator"}
    code, _ = run(repo, [implement_reply("premise_failed", [finding]), diagnosis_reply()])

    assert code == runner.EXIT_TICKET
    assert transitions(repo)[-1] == {"to": "premise_failed", "stage": "implement",
                                     "reason": "premise", "dispatch": "retry"}
    assert (repo / "tickets" / STEM / "ticket.md").is_file()


def test_a_refused_merge_journals_its_terminal_and_leaves_main_where_it_was(repo):
    author(repo)

    def implement_while_main_moves(req):
        (repo / "tempo" / "thing.py").write_text("ok but conflicting\n")
        git(repo, "commit", "-am", "main moves under the run")
        return agent({"tempo/thing.py": "ok\n"})(req)

    code, _ = run(repo, [implement_while_main_moves, verdict(), diagnosis_reply()])

    assert code == runner.EXIT_TICKET
    assert transitions(repo)[-1] == {"to": "gate_failed", "stage": "merge",
                                     "reason": "post_rebase_regate", "dispatch": "retry"}
    assert git(repo, "show", "main:tempo/thing.py") == "ok but conflicting\n"  # no squash landed
    assert git(repo, "show", f"{STEM}:tempo/thing.py") == "ok\n"  # the branch keeps the work


def test_an_engine_plane_refusal_exits_2_and_dispatches_nothing(repo, capsys):
    author(repo, stem="first")
    author(repo, depends="- first")
    code, llm = run(repo, [])

    assert code == runner.EXIT_REFUSED
    assert "depends on unmerged first" in capsys.readouterr().err
    assert llm.requests == [] and transitions(repo) == []


def test_an_unbuildable_provider_layer_refuses_with_exit_2(repo, capsys):
    author(repo)  # the test config's `fake` provider has no shipped cli adapter
    code = main(["run", STEM], cwd=repo, env=ENV, clock=clock)

    assert code == runner.EXIT_REFUSED
    assert "no shipped cli adapter" in capsys.readouterr().err
    assert transitions(repo) == []
