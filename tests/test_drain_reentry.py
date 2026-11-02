"""Findings-fed re-entry behind the `drain` verb (TEMPO_PLAN.md sections 11.2, 19.P1) against a temp checkout.

A stem re-offered after a non-ok terminal renders the prior terminal's durable findings artifacts (review.md
reject findings, a failing checks.json) as a prior-attempts block in criteria-position; ticket.md is never
written. The production stage pipeline runs behind a scripted FakeLLM.
"""

from pathlib import Path

from tempo import runner
from tempo.__main__ import main
from tempo.box import BOX_DIR, Box
from tempo.config import load_config
from tempo.journal import EventType, Journal
from tempo.llm import FakeLLM
from tempo.seams import LocalFileSystem
from tempo.specs import data_close, data_open
from tempo.stages import PRIOR_ATTEMPTS
from tests.test_stages import ENV, SNAG, STEM, agent, git, verdict
from tests.test_terminal import author, clock, diagnosis_reply, repo, transitions  # noqa: F401 -- `repo` is the fixture


def drain(repo: Path, script: list, *, green_base: bool = True) -> tuple[int, FakeLLM]:
    if green_base:
        (repo / "tempo" / "thing.py").write_text("ok base\n")
        git(repo, "commit", "-am", "green verification base")
    llm = FakeLLM(script)
    code = main(["drain"], cwd=repo, env=ENV, clock=clock, pipeline=lambda c: runner.bind(c, llm))
    return code, llm


def implements(llm: FakeLLM) -> list[str]:
    return [r.rendered for r in llm.requests if r.surface == "implement"]


def ticket_block(rendered: str) -> str:
    """The rendered ticket data block: the prompt section the acceptance criteria live in."""
    return rendered.split(data_open("ticket"), 1)[1].split(data_close("ticket"), 1)[0]


def between_criteria_and_next_section(block: str) -> str:
    return block.split("## Acceptance criteria", 1)[1].split("## Verification", 1)[0]


def test_a_reoffer_after_a_review_reject_renders_its_findings_in_criteria_position(repo):
    author(repo)
    ticket_md = None

    def capture_ticket(req):
        nonlocal ticket_md
        ticket_md = (repo / "tickets" / STEM / "ticket.md").read_text()
        return agent({"tempo/thing.py": "ok\n"})(req)

    code, llm = drain(repo, [agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG]), diagnosis_reply(),
                             capture_ticket, verdict()])

    assert code == 0
    assert [t["to"] for t in transitions(repo)] == ["running", "gate_failed", "running", "merged"]
    first, second = implements(llm)
    assert PRIOR_ATTEMPTS not in first
    block = ticket_block(second)
    criteria = between_criteria_and_next_section(block)
    assert PRIOR_ATTEMPTS in criteria
    assert SNAG["message"] in criteria and SNAG["paved_road"] in criteria
    # Rendered fresh, never written into the ticket file.
    assert PRIOR_ATTEMPTS not in ticket_md
    assert PRIOR_ATTEMPTS not in git(repo, "show", f"main:tickets/{STEM}/ticket.md")


def test_a_reoffer_after_a_red_check_renders_the_failing_checks(repo):
    author(repo)
    code, llm = drain(repo, [agent({"tempo/thing.py": "nope\n"}), diagnosis_reply(),
                             agent({"tempo/thing.py": "ok\n"}), verdict()])

    assert code == 0
    first, second = implements(llm)
    assert PRIOR_ATTEMPTS not in first
    criteria = between_criteria_and_next_section(ticket_block(second))
    assert PRIOR_ATTEMPTS in criteria
    assert "`gate_failed` at check" in criteria
    assert "`grep -q ok tempo/thing.py` exited 1" in criteria  # the red verification command's finding


def test_a_base_red_check_merges_without_retry_and_files_one_report(repo):
    author(repo)
    # The quiescence retro follows the merge.
    code, llm = drain(repo, [agent({"tempo/thing.py": "changed\n"}), verdict(), '{"proposals": []}'], green_base=False)

    assert code == 0
    assert len(implements(llm)) == 1
    assert [t["to"] for t in transitions(repo)] == ["running", "merged"]
    assert not any(e.type == EventType.CAP_CONSUMED and e.body.get("cap") == "retry"
                   for e in Journal(repo / ".tempo" / "state", clock).read())
    config = load_config(None, cwd=repo)
    messages = Box(config.state_dir / BOX_DIR, LocalFileSystem()).messages()
    assert len(messages) == 1
    assert messages[0].message_class == "failure_report" and messages[0].outcome == "base_red"
    assert "grep -q ok tempo/thing.py" in messages[0].summary


def test_a_stale_review_md_never_feeds_a_later_check_failure(repo):
    # Attempt 1 rejected at review; attempt 2 went red at check: attempt 3 sees the check, not the stale reject.
    author(repo)
    code, llm = drain(repo, [agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG]), diagnosis_reply(),
                             agent({"tempo/thing.py": "nope\n"}), diagnosis_reply(),
                             agent({"tempo/thing.py": "ok\n"}), verdict()])

    assert code == 0
    third = between_criteria_and_next_section(ticket_block(implements(llm)[2]))
    assert "`gate_failed` at check" in third
    assert SNAG["message"] not in third
