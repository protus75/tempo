"""Implement replies file second problems through the production run command."""

import json
from pathlib import Path

from tempo import runner
from tempo.__main__ import main
from tempo.box import BOX_DIR, Box
from tempo.config import load_config
from tempo.llm import FakeLLM
from tempo.seams import LocalFileSystem
from tempo.stages import ImplementReply
from tests.test_stages import ENV, STEM, agent, verdict
from tests.test_terminal import author, clock, diagnosis_reply, repo  # noqa: F401 -- fixture


FINDING = {"code": "premise", "message": "the premise is false", "paved_road": "correct the ticket"}


def reply(outcome: str, problems: list[str] | None) -> str:
    data = {
        "outcome": outcome, "summary": "handled the ticket", "surprises": "none",
        "dead_ends": "none", "predicted_vs_actual": "none",
        "findings": [FINDING] if outcome == "premise_failed" else [],
    }
    if problems is not None:
        data["second_problems"] = [{"summary": summary} for summary in problems]
    return json.dumps(data)


def run(repo: Path, script: list) -> int:
    llm = FakeLLM(script)
    return main(["run", STEM], cwd=repo, env=ENV, clock=clock,
                pipeline=lambda checkout: runner.bind(checkout, llm))


def box(repo: Path) -> Box:
    config = load_config(None, cwd=repo)
    return Box(config.state_dir / BOX_DIR, LocalFileSystem())


def filed_lines(repo: Path) -> list[str]:
    text = (repo / "tickets" / STEM / "run.md").read_text()
    return text.split("## Second problems filed\n\n", 1)[1].split("\n\n## ", 1)[0].splitlines()


def test_ok_reply_files_two_problems_and_lifts_ids_in_order(repo):
    author(repo)
    summaries = ["First issue at line 42", "Second issue has\na newline"]

    def implement(req):
        agent({"tempo/thing.py": "ok\n"})(req)
        return reply("ok", summaries)

    assert run(repo, [implement, verdict()]) == 0
    messages = box(repo).messages()
    assert len(messages) == 2
    assert [(m.message_class, m.origin, m.stage, m.outcome) for m in messages] == [
        ("suggestion", STEM, "implement", "ok"),
        ("suggestion", STEM, "implement", "ok"),
    ]
    assert filed_lines(repo) == [f"- {messages[0].id}: {summaries[0]}",
                                 f"- {messages[1].id}: Second issue has a newline"]


def test_repeated_line_number_dedups_and_absent_key_means_none(repo):
    author(repo)
    assert run(repo, [reply("premise_failed", ["Wrong line 42"]), diagnosis_reply()]) == 1
    first = box(repo).messages()
    assert len(first) == 1
    assert run(repo, [reply("premise_failed", ["Wrong line 51"]), diagnosis_reply()]) == 1
    assert [m.id for m in box(repo).messages()] == [first[0].id]
    assert filed_lines(repo) == [f"- {first[0].id}: Wrong line 51"]
    assert run(repo, [reply("premise_failed", None), diagnosis_reply()]) == 1
    assert filed_lines(repo) == ["none"]
    assert [m.id for m in box(repo).messages()] == [first[0].id]


def test_absent_key_creates_no_box_entry_and_premise_failure_files(repo):
    author(repo)
    assert ImplementReply.model_validate(json.loads(reply("ok", None))).second_problems == []
    assert run(repo, [reply("premise_failed", None), diagnosis_reply()]) == 1
    assert filed_lines(repo) == ["none"]
    assert box(repo).messages() == []
    assert not box(repo).root.exists()
    assert run(repo, [reply("premise_failed", ["Plan line is wrong"]), diagnosis_reply()]) == 1
    (message,) = box(repo).messages()
    assert (message.origin, message.stage, message.outcome) == (STEM, "implement", "premise_failed")
    assert filed_lines(repo) == [f"- {message.id}: Plan line is wrong"]
