"""A fake-host orchestrator session on the fixture repo (AGENT_PLAN.md P4 steps b-e, P5a, P5b): begin_session,
status, start, requisition, implement, check, review, merge, each failure decision, a resumed session, the
handoff, split, a triage pass that authors a ticket, confirm, and a retro, every action fenced by the host's
hooks."""

import json
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from tempo.audit import audit_journal
from tempo.checkout import Refusal
from tempo.config import load_config
from tempo.journal import EventType, Journal
from tempo.lockfile import LOCK_STALE_S
from tempo.stages import PRIOR_ATTEMPTS
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
    assert audit_journal(Journal(load_config(None, cwd=repo).state_dir, host.clock)) == []

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


RMA_TICKET = json.dumps({"verdict": "rma", "summary": "infeasible", "findings": [
    {"code": "ticket", "message": "the ticket cannot be built as written", "paved_road": "rewrite it",
     "kind": "authoring_error"}]})


def failed_run(host: FakeHost) -> dict:
    """Start the ticket and fail its requisition review: the quickest final failure."""
    host.tool("start", {"stem": STEM})
    failed = call(host, "requisition_review", RMA_TICKET)
    while not failed["final"]:
        failed = call(host, "requisition_review", RMA_TICKET)
    assert failed["outcome"] == "gate_failed"
    return failed


def terminals(host: FakeHost) -> list[dict]:
    events = Journal(load_config(None, cwd=host.repo).state_dir, host.clock).read()
    return [e.body for e in events if e.type == EventType.STATE_TRANSITION and e.body["to"] != "running"]


def eligible(host: FakeHost) -> list[str]:
    return [t["stem"] for t in host.tool("list_eligible")["eligible"]]


def test_retry_records_the_lessons_and_the_next_start_reoffers_the_ticket(repo):
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    failed_run(host)
    decided = host.tool("retry", {"stem": STEM, "lessons": "the ticket was fine; the reviewer misread it"})
    assert decided["dispatch"] == "retry" and decided["to"] == "gate_failed" and decided["stage"] == "requisition_review"
    assert terminals(host)[-1]["reason"] == "ticket"
    assert "the reviewer misread it" in (repo / "tickets" / STEM / "decisions.md").read_text()
    assert "decisions.md" in git(repo, "show", "--stat", "HEAD")
    assert not (repo / ".tempo" / "worktrees" / STEM).exists()
    assert eligible(host) == [STEM]
    assert host.tool("start", {"stem": STEM})["attempt"] == 1



def test_a_requisition_review_failure_is_harvested_and_read_attempts_shows_its_findings(repo):
    """P7: a requisition_review failure's harvest was refused, and read_attempts before the decision showed no
    findings at all: it read only decided attempts."""
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    failed_run(host)
    pending = host.tool("read_attempts", {"stem": STEM})
    assert pending["failure"]["stage"] == "requisition_review" and pending["text"] is None
    assert [f["message"] for f in pending["failure"]["findings"]] == ["the ticket cannot be built as written"]
    host.tool("retry", {"stem": STEM, "lessons": "the reviewer misread it"})
    events = Journal(load_config(None, cwd=repo).state_dir, host.clock).read()
    assert not [e for e in events if e.type == EventType.SIGNAL and e.body.get("signal") == "harvest_failed"]
    assert (repo / "tickets" / STEM / "attempts" / "0" / "harvest.json").is_file()
    decided = host.tool("read_attempts", {"stem": STEM})
    assert decided["failure"] is None
    assert "the ticket cannot be built as written" in decided["text"] and "no findings artifact" not in decided["text"]


def test_retry_is_refused_after_identical_k_failures_for_the_same_reason(repo):
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    for _ in range(2):
        failed_run(host)
        host.tool("retry", {"stem": STEM, "lessons": "try again"})
    failed_run(host)
    with pytest.raises(Refusal, match="3 times running"):
        host.tool("retry", {"stem": STEM, "lessons": "try again"})
    assert host.tool("escalate", {"stem": STEM, "lessons": "a stronger model"})["dispatch"] == "escalate"


def test_escalate_runs_the_next_start_at_the_next_rung(repo):
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    failed_run(host)
    decided = host.tool("escalate", {"stem": STEM, "lessons": "needs more effort"})
    assert decided["rung"] == terminals(host)[-1]["rung"]
    host.tool("start", {"stem": STEM})
    events = Journal(load_config(None, cwd=repo).state_dir, host.clock).read()
    draw = [e.body for e in events if e.type == EventType.CAP_CONSUMED and e.body["cap"] == "retry"][-1]
    assert draw["rung"] == decided["rung"]


def test_park_holds_the_ticket_until_reoffer(repo):
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    failed_run(host)
    host.tool("park", {"stem": STEM, "lessons": "the criteria contradict each other"})
    assert eligible(host) == [] and host.tool("status")["parked"] == [STEM]
    with pytest.raises(Refusal, match="not running"):
        host.tool("park", {"stem": STEM, "lessons": "again"})
    host.tool("reoffer", {"stem": STEM, "lessons": "the contradiction was in the reviewer's reading"})
    assert eligible(host) == [STEM] and host.tool("status")["parked"] == []
    with pytest.raises(Refusal, match="not parked"):
        host.tool("reoffer", {"stem": STEM, "lessons": "again"})


def test_reject_retires_the_ticket(repo):
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    failed_run(host)
    assert host.tool("reject", {"stem": STEM, "lessons": "not worth doing"})["to"] == "rejected"
    assert "state: rejected" in (repo / "tickets" / STEM / "ticket.md").read_text()
    assert [t["to"] for t in terminals(host)] == ["gate_failed", "rejected"]
    assert audit_journal(Journal(load_config(None, cwd=repo).state_dir, host.clock)) == []
    assert eligible(host) == []


def test_a_decision_needs_a_failed_run_and_lessons(repo):
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    host.tool("start", {"stem": STEM})
    with pytest.raises(Refusal, match="no failed stage"):
        host.tool("retry", {"stem": STEM, "lessons": "x"})
    call(host, "requisition_review", RMA_TICKET)
    with pytest.raises(Refusal, match="lessons is blank"):
        host.tool("park", {"stem": STEM, "lessons": " "})


def test_a_session_killed_mid_ticket_resumes_from_status(repo):
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    host.tool("start", {"stem": STEM})
    call(host, "requisition_review", APPROVE_TICKET)
    prepared = host.tool("prepare", {"stem": STEM, "surface": "implement"})
    assert prepared["state"] == "pending"  # the chat dies here, with the implement call open

    host.clock.now += timedelta(seconds=LOCK_STALE_S + 1)
    host.chat, host.token = "chat-2", None
    assert host.tool("begin_session")["number"] == 2
    status = host.tool("status")
    assert [(c["call_id"], c["live_session"]) for c in status["open_calls"]] == [(prepared["call"], False)]
    implemented = call(host, "implement", IMPLEMENTED, edits={"calc.py": SUBTRACT, "test_calc.py": TEST})
    assert implemented["outcome"] == "ok", implemented
    job = host.tool("check", {"stem": STEM})
    assert host.tool("job", {"id": job["id"]})["result"]["outcome"] == "ok"
    call(host, "review", APPROVE_DIFF)
    job = host.tool("merge", {"stem": STEM})
    assert host.tool("job", {"id": job["id"]})["result"]["outcome"] == "ok"


def test_a_retry_after_a_review_snag_renders_its_findings_in_the_next_implement_prompt(repo):
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    host.tool("start", {"stem": STEM})
    call(host, "requisition_review", APPROVE_TICKET)
    call(host, "implement", IMPLEMENTED, edits={"calc.py": SUBTRACT, "test_calc.py": TEST})
    host.tool("check", {"stem": STEM})
    snag = json.dumps({"verdict": "snag", "summary": "off by one", "findings": [
        {"code": "logic", "message": "subtract swaps its operands", "paved_road": "return a - b"}]})
    reviewed = call(host, "review", snag)
    while not reviewed["final"]:
        reviewed = call(host, "review", snag)
    host.tool("retry", {"stem": STEM, "lessons": "the operands were swapped"})
    host.tool("start", {"stem": STEM})
    call(host, "requisition_review", APPROVE_TICKET)
    prepared = host.tool("prepare", {"stem": STEM, "surface": "implement"})
    parts = [line.removeprefix("part: ") for line in prepared["spawn_prompt"].splitlines() if line.startswith("part: ")]
    prompt = "".join(Path(p).read_text() for p in parts)
    assert PRIOR_ATTEMPTS in prompt and "subtract swaps its operands" in prompt
    assert "the operands were swapped" in prompt  # the retry's lessons, from decisions.md


def ticket_text(goal: str, *, depends: str = "none", state: str = "confirmed") -> str:
    text = (Path(__file__).resolve().parent.parent / "fixture" / "tickets" / STEM / "ticket.md").read_text()
    text = text.replace("`calc.py` gains `subtract(a, b)`, so the fixture has one small real change to make.", goal)
    return text.replace("## Depends on\nnone", f"## Depends on\n{depends}").replace("state: confirmed",
                                                                                      f"state: {state}")


def spawn(host: FakeHost, args: dict, reply: str) -> dict:
    prepared = host.tool("prepare", args)
    assert prepared["state"] == "pending", prepared
    host.subagent(prepared, reply)
    return host.until_done(host.tool("submit", {"call": prepared["call"]}), "submit", {"call": prepared["call"]})


def test_split_publishes_reviewed_successors_and_retires_the_original(repo):
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    failed_run(host)
    tickets = [{"stem": "subtract-core", "ticket": ticket_text("`calc.py` gains `subtract(a, b)`.")},
               {"stem": "subtract-check", "ticket": ticket_text("`test_calc.py` checks `subtract`.",
                                                                depends="- subtract-core")}]
    with pytest.raises(Refusal, match="at least two"):
        host.tool("split_propose", {"stem": STEM, "tickets": tickets[:1], "lessons": "too big"})
    proposed = host.tool("split_propose", {"stem": STEM, "tickets": tickets, "lessons": "two pieces"})
    assert proposed["dispatch"] == "split" and proposed["proposals"] == ["subtract-core", "subtract-check"]
    assert eligible(host) == [] and host.tool("status")["parked"] == [STEM]
    review = {"surface": "requisition_review", "stem": STEM}
    assert spawn(host, {**review, "proposal": "subtract-core"}, APPROVE_TICKET)["outcome"] == "ok"
    with pytest.raises(Refusal, match="proposal subtract-check has no approve verdict"):
        host.tool("split_commit", {"stem": STEM})
    assert spawn(host, {**review, "proposal": "subtract-check"}, RMA_TICKET)["outcome"] == "gate_failed"
    with pytest.raises(Refusal, match="proposal subtract-check has no approve verdict"):
        host.tool("split_commit", {"stem": STEM})

    tickets[1]["ticket"] = ticket_text("`test_calc.py` asserts `subtract(5, 3) == 2`.", depends="- subtract-core")
    assert host.tool("split_propose", {"stem": STEM, "tickets": tickets, "lessons": "clearer check"})["order"] == 2
    for stem in ("subtract-core", "subtract-check"):
        assert spawn(host, {**review, "proposal": stem}, APPROVE_TICKET)["outcome"] == "ok"
    assert host.tool("split_commit", {"stem": STEM})["successors"] == ["subtract-core", "subtract-check"]
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == [
        "tickets/subtract-check/ticket.md", "tickets/subtract-core/ticket.md"]
    assert eligible(host) == ["subtract-core"]  # the check depends on the core; the original is superseded
    assert not (repo / ".tempo" / "worktrees" / f"{STEM}.split").exists()
    with pytest.raises(Refusal, match="no pending split"):
        host.tool("split_commit", {"stem": STEM})
    assert "two pieces" in (repo / "tickets" / STEM / "decisions.md").read_text()


def test_a_triage_pass_authors_a_draft_that_confirm_releases(repo):
    from tempo.box import BOX_DIR, Box
    from tempo.seams import LocalFileSystem

    host = FakeHost("claude", repo)
    host.tool("begin_session")
    box = Box(load_config(None, cwd=repo).state_dir / BOX_DIR, LocalFileSystem())
    id, _ = box.enqueue(message_class="suggestion", origin="test", summary="calc needs multiply")
    assert [m["id"] for m in host.tool("read_box")["pending"]] == [id]
    triage = json.dumps({"verdict": "author", "link": None, "summary": "worth a ticket",
                         "rationale": "multiply is missing", "evidence": [], "reopen_after_days": None})
    triaged = spawn(host, {"surface": "triage", "message": id}, triage)
    assert triaged["outcome"] == "ok" and triaged["next"] == "author"
    candidate = ticket_text("`calc.py` gains `multiply(a, b)`.").replace("state: confirmed\nsource: human\n", "")
    authored = spawn(host, {"surface": "author", "message": id},
                     json.dumps({"stem": "add-multiply", "ticket": candidate}))
    assert authored["next"] == "requisition_review"
    snag = json.dumps({"verdict": "snag", "summary": "vague", "findings": [
        {"code": "ticket", "message": "name the test", "paved_road": "cite test_calc.py", "kind": "authoring_error"}]})
    reviewed = spawn(host, {"surface": "requisition_review", "message": id}, snag)
    assert reviewed["next"] == "author" and not (repo / "tickets" / "add-multiply").exists()
    prepared = host.tool("prepare", {"surface": "author", "message": id})
    parts = [line.removeprefix("part: ") for line in prepared["spawn_prompt"].splitlines() if line.startswith("part: ")]
    assert "cite test_calc.py" in "".join(Path(p).read_text() for p in parts)  # the snag re-prompts author
    host.subagent(prepared, json.dumps({"stem": "add-multiply", "ticket": candidate}))
    host.until_done(host.tool("submit", {"call": prepared["call"]}), "submit", {"call": prepared["call"]})
    published = spawn(host, {"surface": "requisition_review", "message": id}, APPROVE_TICKET)
    assert published["outcome"] == "ok" and published["line"] == f"{id}: ticket -> add-multiply"
    assert host.tool("read_box")["pending"] == []
    assert "state: draft" in (repo / "tickets" / "add-multiply" / "ticket.md").read_text()
    assert "add-multiply" not in eligible(host)

    with pytest.raises(Refusal, match="lessons is blank"):
        host.tool("confirm", {"stem": "add-multiply", "lessons": ""})
    assert host.tool("confirm", {"stem": "add-multiply", "lessons": "scope is one function"})["state"] == "confirmed"
    assert "state: confirmed" in git(repo, "show", "HEAD~1:tickets/add-multiply/ticket.md")
    assert "add-multiply" in eligible(host)
    with pytest.raises(Refusal, match="not draft"):
        host.tool("confirm", {"stem": "add-multiply", "lessons": "again"})


def test_retro_runs_when_status_shows_it_due(repo):
    host = FakeHost("claude", repo)
    Journal(load_config(None, cwd=repo).state_dir, host.clock).append(
        EventType.SIGNAL, {"signal": "handoff_written", "number": 0}, ticket=None)
    host.tool("begin_session")
    assert host.tool("status")["retro_due"] is False
    with pytest.raises(Refusal, match="retro is not due"):
        host.tool("prepare", {"surface": "retro"})
    host.tool("write_handoff", {"text": "nothing to do"})
    host.clock.now += timedelta(days=7)
    host.chat, host.token = "chat-2", None
    host.tool("begin_session")
    assert host.tool("status")["retro_due"] is True
    retro = spawn(host, {"surface": "retro"}, json.dumps({"proposals": []}))
    assert retro["outcome"] == "ok" and retro["final"]
    assert (repo / "tickets" / "retro" / "000001.md").read_text().startswith("# Retro receipt")
    assert host.tool("status")["retro_due"] is False
