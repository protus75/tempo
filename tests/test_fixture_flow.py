"""A fake-host orchestrator session on the fixture repo (AGENT_PLAN.md P4 steps b-e): begin_session,
status, start, requisition, implement, check, review, merge, and the handoff, every action fenced by the
host's hooks."""

import json
import subprocess
from pathlib import Path

import pytest

from tempo.journal import EventType, Journal
from tempo.config import load_config
from tests.fixturerepo import STEM, materialize
from tests.hostfakes import FakeHost

SUBTRACT = "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n"
TEST = "from calc import add, subtract\n\nassert add(2, 3) == 5\nassert subtract(5, 3) == 2\n"
APPROVE_TICKET = json.dumps({"verdict": "approve", "summary": "feasible", "findings": []})
IMPLEMENTED = json.dumps({"outcome": "ok", "summary": "added subtract", "surprises": "none", "dead_ends": "none",
                          "findings": []})
APPROVE_DIFF = json.dumps({"verdict": "approve", "summary": "subtract is right", "findings": []})


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    return materialize(tmp_path / "fixture")


def call(host: FakeHost, surface: str, reply: str, **subagent) -> dict:
    prepared = host.tool("prepare", {"stem": STEM, "surface": surface})
    assert prepared["state"] == "pending", prepared
    host.subagent(prepared, reply, **subagent)
    return host.until_done(host.tool("submit", {"call": prepared["call"]}), "submit", {"call": prepared["call"]})


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_a_ticket_runs_from_intake_to_merge(repo, name):
    host = FakeHost(name, repo)
    session = host.tool("begin_session")
    assert session["host"] == name and session["number"] == 1
    status = host.tool("status")
    assert status["open_calls"] == [] and status["session"]["chat"] == "chat-1"
    assert [t["stem"] for t in host.tool("list_eligible")["eligible"]] == [STEM]
    started = host.tool("start", {"stem": STEM})
    worktree = Path(started["worktree"])

    assert call(host, "requisition_review", APPROVE_TICKET)["outcome"] == "ok"

    implemented = call(host, "implement", IMPLEMENTED, edits={"calc.py": SUBTRACT, "test_calc.py": TEST},
                       shell=[f"cd {worktree} && python3 test_calc.py"])
    assert implemented["outcome"] == "ok" and implemented["final"]
    assert implemented["artifact"]["branch"] == STEM
    assert "subtract" in git(repo, "show", f"{STEM}:calc.py")  # submit committed the subagent's work
    assert (repo / "tickets" / STEM / "run.md").is_file()  # the run record was lifted to main

    job = host.tool("check", {"stem": STEM})
    checked = host.tool("job", {"id": job["id"]})
    assert checked["state"] == "done" and checked["result"]["outcome"] == "ok", checked

    reviewed = call(host, "review", APPROVE_DIFF)
    assert reviewed["outcome"] == "ok" and reviewed["artifact"]["verdict"] == "approve"

    job = host.tool("merge", {"stem": STEM})
    merged = host.tool("job", {"id": job["id"]})
    assert merged["result"]["outcome"] == "ok", merged
    assert "subtract" in (repo / "calc.py").read_text()
    events = Journal(load_config(None, cwd=repo).state_dir, host.clock).read()
    assert [e.body["to"] for e in events if e.type == EventType.STATE_TRANSITION] == ["running", "merged"]

    assert host.tool("list_eligible")["eligible"] == []
    host.tool("write_handoff", {"text": "merged add-subtract; queue empty"})
    assert "queue empty" in (repo / "tickets/orchestrator/handoff.md").read_text()
    assert host.tool("status")["session"] is None


def test_a_failed_review_reports_the_findings_and_the_session_hands_off(repo):
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    host.tool("start", {"stem": STEM})
    call(host, "requisition_review", APPROVE_TICKET)
    call(host, "implement", IMPLEMENTED, edits={"calc.py": SUBTRACT, "test_calc.py": TEST})
    host.tool("check", {"stem": STEM})
    snag = json.dumps({"verdict": "rma", "summary": "wrong ticket", "findings": [
        {"code": "ticket", "message": "the ticket asks for the wrong thing", "paved_road": "rewrite it"}]})
    reviewed = call(host, "review", snag)
    while not reviewed["final"]:
        reviewed = call(host, "review", snag)
    assert reviewed["outcome"] == "gate_failed"
    assert [f["code"] for f in reviewed["findings"]] == ["ticket"]
    assert (repo / "tickets" / STEM / "review.md").is_file()
    host.tool("write_handoff", {"text": "review returned rma"})
