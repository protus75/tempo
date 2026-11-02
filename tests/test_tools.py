"""Tool refusals and the session token (AGENT_PLAN.md sections 4.1, 4.6)."""

import json
from datetime import timedelta

import pytest

from tempo.__main__ import _tool
from tempo.checkout import Refusal
from tempo.lockfile import LOCK_STALE_S
from tests.fixturerepo import STEM, materialize
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
                       ("check", {"stem": STEM}), ("write_handoff", {"text": "x"})):
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


def test_prepare_refuses_a_surface_outside_the_p4_flow_and_an_unstarted_ticket(repo):
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    with pytest.raises(Refusal, match="no tool in P4"):
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
