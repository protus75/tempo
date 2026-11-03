"""Per-host hook payload adapters and replies (AGENT_PLAN.md section 5)."""

import json

import pytest

from tempo import hosts
from tests.hostfakes import TOOLS, payload

READ = {name: TOOLS[name]["read"] for name in hosts.HOSTS}


@pytest.mark.parametrize("name", hosts.HOSTS)
def test_each_host_payload_parses_to_the_same_event(name):
    event = hosts.parse(name, payload(name, "PreToolUse", chat="c1", cwd="/r", kind="read", path="/r/a.py"))
    assert (event.host, event.event, event.chat, event.cwd) == (name, "pre_tool", "c1", "/r")
    assert event.tool.kind == "read" and event.tool.paths == ("/r/a.py",)
    spawn = hosts.parse(name, payload(name, "PreToolUse", chat="c1", cwd="/r", kind="spawn", target="tempo-x",
                                      prompt="call: 1"))
    assert (spawn.tool.kind, spawn.tool.agent, spawn.tool.prompt) == ("spawn", "tempo-x", "call: 1")


def test_caller_identity_per_host():
    for name, agent in (("claude", "tempo-review-m"), ("copilot", "tempo-review-m"), ("codex", None)):
        event = hosts.parse(name, payload(name, "SubagentStart", chat="c", cwd="/r", agent="tempo-review-m",
                                          agent_id="a1"))
        assert event.agent_type == agent  # Codex names no caller: tempo uses markers there


def test_codex_shell_and_patch():
    shell = hosts.parse("codex", payload("codex", "PreToolUse", chat="c", cwd="/r", kind="shell", command="cat a"))
    assert shell.tool.command == "cat a"
    patch = hosts.parse("codex", payload("codex", "PreToolUse", chat="c", cwd="/r", kind="write", path="/r/out.txt"))
    assert patch.tool.kind == "write" and patch.tool.paths == ("/r/out.txt",)


@pytest.mark.parametrize("name", hosts.HOSTS)
def test_malformed_payloads_do_not_parse(name):
    f = hosts.FIELDS[name]
    for body in ({}, {f.event: "PreToolUse", f.chat: "c"}, {f.event: "PreToolUse", f.chat: "c", f.cwd: "/r"},
                 {f.event: "PreToolUse", f.chat: "c", f.cwd: "/r", f.tool_name: READ[name],
                  f.tool_input: {"wrong": 1, **{k: 3 for k in ("file_path", "filePath", "path")}}}):
        assert hosts.parse(name, json.dumps(body).encode()) is None, body
    assert hosts.parse(name, b"\xff") is None


def test_a_spawn_naming_no_agent_parses_as_the_default_agent():
    # Captured on Claude: a nested spawn from a plain chat's subagent omits subagent_type.
    data = {"hook_event_name": "PreToolUse", "session_id": "c", "cwd": "/r", "agent_type": "general-purpose",
            "agent_id": "a1", "tool_name": "Agent", "tool_input": {"description": "d", "prompt": "hi"}}
    event = hosts.parse("claude", json.dumps(data).encode())
    assert event.tool.kind == "spawn" and event.tool.agent is None and event.tool.prompt == "hi"


def test_an_unknown_tool_is_kind_other():
    event = hosts.parse("claude", json.dumps({"hook_event_name": "PreToolUse", "session_id": "c", "cwd": "/r",
                                              "tool_name": "WebFetch", "tool_input": {}}).encode())
    assert event.tool.kind == "other" and event.tool.name == "WebFetch"


def test_reply_and_hook_files():
    assert hosts.reply(None, "") == ""
    assert json.loads(hosts.reply(False, "no"))["hookSpecificOutput"]["permissionDecision"] == "deny"
    claude = hosts.hook_config("claude")["hooks"]
    assert set(claude) == set(hosts.EVENTS)
    assert claude["PreToolUse"][0] == {"matcher": "*", "hooks": [{"type": "command", "command": "tempo hook claude"}]}
    assert hosts.hook_config("copilot")["version"] == 1
    assert hosts.hook_config("codex")["hooks"]["PreToolUse"][0]["hooks"] == [{"type": "command",
                                                                             "command": "tempo hook codex"}]
