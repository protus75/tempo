"""Tool refusals and the session token (AGENT_PLAN.md sections 4.1, 4.6)."""

import json
import subprocess
from datetime import timedelta

import pytest

from tempo import caps
from tempo.__main__ import _tool
from tempo.box import BOX_DIR
from tempo.checkout import Refusal
from tempo.config import load_config
from tempo.journal import EventType, Journal
from tempo.lockfile import LOCK_STALE_S
from tempo.seams import LocalFileSystem
from tempo.storm import STORM_THRESHOLD, storm_producer
from tempo.tickets import INTAKE_SIGNAL
from tests.fixturerepo import FIXTURE, STEM, materialize
from tests.hostfakes import FakeHost


@pytest.fixture
def repo(tmp_path):
    return materialize(tmp_path / "fixture")


def test_every_tool_but_begin_session_and_status_needs_the_token(repo):
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    assert host.tool("status", {"session": "wrong"})["session"]["host"] == "claude"
    for name, args in (("list_eligible", {}), ("start", {"stem": STEM}), ("job", {"id": "x"}),
                       ("write_handoff", {"text": "x"})):
        with pytest.raises(Refusal, match="token does not hold"):
            host.tool(name, {**args, "session": "wrong"})


def test_a_second_chat_is_refused_until_the_first_goes_stale(repo):
    first = FakeHost("claude", repo, chat="one")
    first.tool("begin_session")
    second = FakeHost("codex", repo, chat="two", clock=first.clock)
    with pytest.raises(Refusal, match="held by the claude chat one"):
        second.tool("begin_session")
    first.clock.now += timedelta(seconds=LOCK_STALE_S + 1)
    assert second.tool("begin_session")["number"] == 2
    with pytest.raises(Refusal, match="token does not hold"):
        first.tool("list_eligible")


def test_one_thing_at_a_time_while_a_call_is_open(repo):
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    host.tool("start", {"stem": STEM})
    prepared = host.tool("prepare", {"stem": STEM, "surface": "requisition_review"})
    for name, args in (("prepare", {"stem": STEM, "surface": "implement"}), ("start", {"stem": STEM}),
                       ("check", {"stem": STEM}), ("write_handoff", {"text": "x"}),
                       ("retry", {"stem": STEM, "lessons": "x"}), ("file_box", {"summary": "x"}),
                       ("notify", {"message": "x"})):
        with pytest.raises(Refusal, match=f"call {prepared['call']} is open"):
            host.tool(name, args)
    assert host.tool("submit", {"call": prepared["call"]}) == {"state": "pending"}
    assert host.tool("status")["open_calls"][0]["call_id"] == prepared["call"]


def test_submit_refuses_a_call_prepare_never_issued(repo):
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    with pytest.raises(Refusal, match="never issued"):
        host.tool("submit", {"call": "f" * 32})


def test_session_limits(repo):
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    path = repo / "config.yaml"
    path.write_text(path.read_text().replace("max_tickets: 5", "max_tickets: 1"))
    host.tool("start", {"stem": STEM})
    with pytest.raises(Refusal, match="max_tickets"):
        host.tool("start", {"stem": STEM})
    host.clock.now += timedelta(minutes=241)
    with pytest.raises(Refusal, match="max_minutes"):
        host.tool("list_eligible")
    assert host.tool("write_handoff", {"text": "out of time"})["number"] == 1  # still served


def test_prepare_refuses_arguments_outside_its_rows_and_an_unstarted_ticket(repo):
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    with pytest.raises(Refusal, match="prepare has no 'triage' call for these arguments"):
        host.tool("prepare", {"stem": STEM, "surface": "triage"})
    with pytest.raises(Refusal, match="is not running"):
        host.tool("prepare", {"stem": STEM, "surface": "implement"})
    with pytest.raises(Refusal, match="bad arguments for prepare"):
        host.tool("prepare", {"stem": STEM})


def test_the_tool_verb_prints_one_json_object(repo, capsys):
    host = FakeHost("claude", repo)
    assert _tool(host.tools(), "nope", "{}") == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and "no tool" in out["refused"] and "begin_session" in out["do_instead"]
    assert _tool(host.tools(), "status", "[1]") == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert _tool(host.tools(), "status", "{}") == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_a_storm_hold_keeps_the_ticket_out_until_release_hold(repo):
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    config = load_config(None, cwd=repo)
    box = storm_producer(root=config.state_dir / BOX_DIR, fs=LocalFileSystem(),
                         journal=Journal(config.state_dir, host.clock), clock=host.clock)
    for n in range(STORM_THRESHOLD + 1):
        box.enqueue(message_class="failure_report", origin=STEM, stage="implement", outcome="gate_failed",
                    summary="the same failure", occurrence_id=f"arrival-{n}")
    [(hold, stem)] = host.tool("status")["holds"].items()
    assert stem == STEM and host.tool("list_eligible")["eligible"] == []
    with pytest.raises(Refusal, match="is not live"):
        host.tool("release_hold", {"hold_id": "nope", "lessons": "x"})
    assert host.tool("release_hold", {"hold_id": hold, "lessons": "the failure was a flaky runner"})["released"]
    assert host.tool("status")["holds"] == {}
    assert [t["stem"] for t in host.tool("list_eligible")["eligible"]] == [STEM]
    assert "flaky runner" in (repo / "tickets" / STEM / "decisions.md").read_text()


def test_file_box_and_notify(repo):
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    filed = host.tool("file_box", {"summary": "test_calc.py has no runner"})
    assert [m["id"] for m in host.tool("read_box")["pending"]] == [filed["id"]]
    with pytest.raises(Refusal, match="message_class 'note' is unknown"):
        host.tool("file_box", {"summary": "x", "message_class": "note"})
    assert host.tool("notify", {"message": "log in again"}) == {"sent": False}  # the fixture sets no notify argv


# --- eligibility and the re-offer draw (moved from test_drain.py with drain) -----------------------


def git(repo, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def commit_ticket(repo, stem: str, *, priority: str = "P2", depends: str = "none", state: str = "confirmed",
                  stuck: str = "20m", commit: bool = True) -> None:
    text = (FIXTURE / "tickets" / STEM / "ticket.md").read_text()
    text = (text.replace("state: confirmed", f"state: {state}").replace("priority: P2", f"priority: {priority}")
            .replace("## Depends on\nnone", f"## Depends on\n{depends}").replace("- stuck: 20m", f"- stuck: {stuck}"))
    rel = f"tickets/{stem}/ticket.md"
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text)
    if commit:
        git(repo, "add", rel)
        git(repo, "commit", "-q", "-m", f"tempo({stem}): ticket", "--only", "--", rel)


def journal(host: FakeHost) -> Journal:
    return Journal(load_config(None, cwd=host.repo).state_dir, host.clock)


def eligible(host: FakeHost) -> list[str]:
    return [t["stem"] for t in host.tool("list_eligible")["eligible"]]


def test_list_eligible_orders_by_depends_then_priority_then_age(repo):
    commit_ticket(repo, "parent", priority="P3")
    commit_ticket(repo, "child", priority="P0", depends="- parent")
    commit_ticket(repo, "other", priority="P1")
    commit_ticket(repo, "aaa-new")
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    j = journal(host)
    for stem in (STEM, "aaa-new"):  # the age anchor: each stem's first authoring event
        j.append(EventType.SIGNAL, {"signal": INTAKE_SIGNAL, "source": "human", "state": "confirmed",
                                    "commit": "0" * 40}, ticket=stem)
        host.clock.now += timedelta(hours=1)
    assert eligible(host) == ["other", STEM, "aaa-new", "parent"]  # child waits on its parent
    j.append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket="parent")
    assert eligible(host) == ["child", "other", STEM, "aaa-new"]


def test_list_eligible_skips_drafts_uncommitted_and_over_ceiling_tickets(repo):
    commit_ticket(repo, "rough", state="draft")
    commit_ticket(repo, "loose", commit=False)
    commit_ticket(repo, "too-long", stuck="200m")
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    assert eligible(host) == [STEM]


def test_a_depends_cycle_is_refused_with_a_paved_road(repo):
    commit_ticket(repo, "aaa", depends="- bbb")
    commit_ticket(repo, "bbb", depends="- aaa")
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    with pytest.raises(Refusal, match="cycle.*drop the edge"):
        host.tool("list_eligible")


def test_a_spent_retry_cap_is_never_offered(repo):
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    j = journal(host)
    j.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket=STEM)
    j.append(EventType.STATE_TRANSITION, {"to": "gate_failed", "stage": "check"}, ticket=STEM)
    for _ in range(6):  # the shipped default retry cap
        j.append(EventType.CAP_CONSUMED, {"cap": "retry", "ticket_sha": "x"}, ticket=STEM)
    assert eligible(host) == []


def test_a_premise_park_holds_until_a_ticket_edit_and_that_reoffer_draws_no_retry(repo):
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    j = journal(host)
    sha = git(repo, "rev-parse", f"HEAD:tickets/{STEM}/ticket.md").strip()
    j.append(EventType.STATE_TRANSITION, {"to": "running", "ticket_sha": sha}, ticket=STEM)
    j.append(EventType.STATE_TRANSITION, {"to": "premise_failed", "stage": "review"}, ticket=STEM)
    assert eligible(host) == [] and host.tool("status")["parked"] == [STEM]
    path = repo / "tickets" / STEM / "ticket.md"
    path.write_text(path.read_text().replace("subtract(5, 3) == 2", "subtract(5, 3) == 2, as answered"))
    git(repo, "commit", "-q", "-am", "answer the premise")
    assert eligible(host) == [STEM]
    host.tool("start", {"stem": STEM})
    assert caps.draws(journal(host).read(), STEM, "retry") == 0
