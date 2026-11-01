import json
from pathlib import Path

import pytest

from tempo.hooks import STOP_RETRIES, State, handle

MANIFEST = {"subject": "add-widget", "attempt": 1, "inputs": ["tickets/add-widget/ticket.md"]}
REPLY_PATH = ".tempo/outbox/correctness_review/add-widget/1/reply.json"
APPROVE = {"verdict": "approve", "summary": "Meets every criterion.", "findings": []}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    ticket = tmp_path / "tickets" / "add-widget" / "ticket.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text("# add-widget\n")
    return tmp_path


def run(host, event, payload, root, **env):
    out, code = handle(host, event, {"cwd": str(root), **payload}, env)
    assert code == 0
    return json.loads(out) if out else None


def denied(result) -> str | None:
    if result is None:
        return None
    if "hookSpecificOutput" in result:
        return result["hookSpecificOutput"]["permissionDecisionReason"]
    return result.get("permissionDecisionReason") or result.get("reason")


# -- payload builders, one shape per host --------------------------------------------------------

def claude_tool(tool, args, agent=None):
    payload = {"tool_name": tool, "tool_input": args}
    if agent:
        payload |= {"agent_id": "a1", "agent_type": agent}
    return payload


def copilot_tool(tool, args, agent=None):
    payload = {"toolName": tool, "toolArgs": json.dumps(args)}
    if agent:
        payload |= {"agentId": "c1", "agentType": agent, "parentAgentId": "root"}
    return payload


def codex_tool(tool, args):
    return {"tool_name": tool, "tool_input": args, "session_id": "parent"}


def codex_start(root, agent):
    run("codex", "SubagentStart", {"agent_id": "s1", "agent_type": agent}, root)


# -- one level of subagents ----------------------------------------------------------------------

@pytest.mark.parametrize("payload_host", [
    ("claude", claude_tool("Agent", {"subagent_type": "tempo-triage", "prompt": json.dumps(MANIFEST)}, "tempo-author")),
    ("copilot", copilot_tool("task", {"agent_type": "tempo-triage", "prompt": json.dumps(MANIFEST)}, "tempo-author")),
    ("claude", claude_tool("Agent", {"subagent_type": "Explore", "prompt": "look"}, "Explore")),
])
def test_subagent_never_spawns(root, payload_host):
    host, payload = payload_host
    assert "one level" in denied(run(host, "PreToolUse", payload, root))


def test_codex_subagent_never_spawns(root):
    codex_start(root, "tempo-author")
    result = run("codex", "PreToolUse", codex_tool("spawn_agent", {"agent_type": "tempo-triage", "message": "{}"}), root)
    assert "one level" in denied(result)


def test_codex_two_active_subagents_fail_closed(root):
    codex_start(root, "tempo-author")
    run("codex", "SubagentStart", {"agent_id": "x2", "agent_type": "tempo-triage"}, root)
    result = run("codex", "PreToolUse", codex_tool("apply_patch", {"input": "*** Begin Patch\n*** Add File: a.txt\n"}), root)
    assert "cannot tell" in denied(result)


def test_spawned_orchestrator_is_still_a_subagent(root):
    payload = claude_tool("Agent", {"subagent_type": "tempo-triage", "prompt": json.dumps(MANIFEST)}, "tempo-orchestrator")
    assert "one level" in denied(run("claude", "PreToolUse", payload, root))


# -- input is paths ------------------------------------------------------------------------------

def orchestrator_spawn(root, prompt, target="tempo-correctness-review"):
    payload = claude_tool("Agent", {"subagent_type": target, "prompt": prompt})
    return run("claude", "PreToolUse", payload, root, TEMPO_ROLE="orchestrator")


def test_manifest_spawn_allowed_and_recorded(root):
    assert orchestrator_spawn(root, json.dumps(MANIFEST)) is None
    assert State(root).pending("correctness_review") == {("add-widget", 1)}


@pytest.mark.parametrize("prompt", [
    "Please review add-widget; the diff is below ...",
    json.dumps(MANIFEST | {"notes": "extra advice"}),
    json.dumps(MANIFEST | {"inputs": ["tickets/missing.md"]}),
    json.dumps(MANIFEST | {"inputs": [".tempo/outbox/author/x/1/reply.json"]}),
    json.dumps(MANIFEST | {"inputs": ["../outside.md"]}),
    "x" * 5000,
])
def test_bad_spawn_prompts_refused(root, prompt):
    (root / ".tempo/outbox/author/x/1").mkdir(parents=True)
    (root / ".tempo/outbox/author/x/1/reply.json").write_text("{}")
    assert denied(orchestrator_spawn(root, prompt))


def test_orchestrator_spawns_only_tempo_subagents(root):
    assert "spawns only" in denied(orchestrator_spawn(root, json.dumps(MANIFEST), target="general-purpose"))


def test_existing_reply_forces_new_attempt(root):
    path = root / REPLY_PATH
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    assert "attempt 2" in denied(orchestrator_spawn(root, json.dumps(MANIFEST)))


def test_developer_session_unconstrained_except_tempo_spawns(root):
    assert run("claude", "PreToolUse", claude_tool("Bash", {"command": "rm -rf build"}), root) is None
    assert run("claude", "PreToolUse", claude_tool("Agent", {"subagent_type": "Explore", "prompt": "x"}), root) is None
    bad = claude_tool("Agent", {"subagent_type": "tempo-triage", "prompt": "triage msg 4"})
    assert denied(run("claude", "PreToolUse", bad, root))


# -- output is files -----------------------------------------------------------------------------

@pytest.mark.parametrize("host,payload", [
    ("claude", claude_tool("Write", {"file_path": "src/app.py", "content": "x"}, "tempo-correctness-review")),
    ("copilot", copilot_tool("create", {"path": "src/app.py", "file_text": "x"}, "tempo-correctness-review")),
    ("claude", claude_tool("Write", {"file_path": ".tempo/outbox/author/add-widget/1/reply.json"}, "tempo-correctness-review")),
])
def test_subagent_write_outside_allowlist_refused(root, host, payload):
    assert "not writable" in denied(run(host, "PreToolUse", payload, root))


def test_codex_patch_outside_allowlist_refused(root):
    codex_start(root, "tempo-correctness-review")
    patch = f"*** Begin Patch\n*** Add File: {REPLY_PATH}\n+{{}}\n*** Update File: src/app.py\n*** End Patch\n"
    assert "not writable" in denied(run("codex", "PreToolUse", codex_tool("apply_patch", {"input": patch}), root))


@pytest.mark.parametrize("host", ["claude", "copilot", "codex"])
def test_subagent_reply_write_allowed(root, host):
    args = {"file_path": REPLY_PATH, "path": REPLY_PATH}
    if host == "codex":
        codex_start(root, "tempo-correctness-review")
        payload = codex_tool("apply_patch", {"input": f"*** Begin Patch\n*** Add File: {REPLY_PATH}\n+{{}}\n"})
    elif host == "claude":
        payload = claude_tool("Write", args, "tempo-correctness-review")
    else:
        payload = copilot_tool("create", args, "tempo-correctness-review")
    assert run(host, "PreToolUse", payload, root) is None


def test_subagent_has_no_tempo_tools_or_web(root):
    for tool in ("mcp__tempo__merge", "WebFetch"):
        assert denied(run("claude", "PreToolUse", claude_tool(tool, {}, "tempo-triage"), root))


@pytest.mark.parametrize("command,ok", [
    ("git diff main...add-widget", True),
    ("rg -n widget src", True),
    ("cat tickets/add-widget/ticket.md > /tmp/x", False),
    ("git -c core.pager=sh diff", False),
    ("rg --pre=sh x", False),
    ("python -c 'open(\"a\",\"w\")'", False),
    ("git diff --output=evil.txt", False),
])
def test_codex_subagent_shell_is_read_only(root, command, ok):
    codex_start(root, "tempo-correctness-review")
    result = run("codex", "PreToolUse", codex_tool("Bash", {"command": ["bash", "-lc", command]}), root)
    assert (result is None) == ok


def test_orchestrator_write_scope(root):
    def write(path):
        payload = claude_tool("Edit", {"file_path": path})
        return run("claude", "PreToolUse", payload, root, TEMPO_ROLE="orchestrator")

    assert write("tickets/add-widget/decisions.md") is None
    assert write("tickets/orchestrator/handoff.md") is None
    assert write("tickets/new-split-1/ticket.md") is None
    assert "already exists" in denied(write("tickets/add-widget/ticket.md"))
    assert denied(write("src/app.py"))
    assert denied(run("claude", "PreToolUse", claude_tool("Bash", {"command": "ls"}), root, TEMPO_ROLE="orchestrator"))
    assert denied(run("claude", "PreToolUse", claude_tool("SendMessage", {"to": "a1"}), root, TEMPO_ROLE="orchestrator"))


def test_implement_bound_to_fence(root):
    def patch(path, **env):
        payload = codex_tool("apply_patch", {"input": f"*** Begin Patch\n*** Update File: {path}\n"})
        return run("codex", "PreToolUse", payload, root, TEMPO_ROLE="implement", **env)

    fence = json.dumps(["src/widget"])
    assert patch("src/widget/core.py", TEMPO_FENCE=fence) is None
    assert "scope fence" in denied(patch("src/other.py", TEMPO_FENCE=fence))
    assert denied(patch("tickets/add-widget/ticket.md", TEMPO_FENCE=json.dumps(["tickets"])))
    assert "TEMPO_FENCE" in denied(patch("src/widget/core.py"))


# -- reply contract at stop ----------------------------------------------------------------------

def stop(root, host, message):
    if host == "copilot":
        payload = {"agentId": "s1", "agentType": "tempo-correctness-review", "response": message}
    else:
        payload = {"agent_id": "s1", "agent_type": "tempo-correctness-review", "last_assistant_message": message}
    return run(host, "SubagentStop", payload, root)


def write_reply(root, body):
    path = root / REPLY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body))


@pytest.mark.parametrize("host", ["claude", "copilot", "codex"])
def test_valid_reply_accepted(root, host):
    orchestrator_spawn(root, json.dumps(MANIFEST))
    write_reply(root, APPROVE)
    assert stop(root, host, json.dumps({"written": [REPLY_PATH], "status": "ok"})) is None
    assert State(root).pending("correctness_review") == set()


@pytest.mark.parametrize("message,body", [
    ("Looks good to me!", APPROVE),
    ("Done.\n" + json.dumps({"written": [REPLY_PATH], "status": "ok"}), APPROVE),
    (json.dumps({"written": [REPLY_PATH], "status": "ok"}), {"verdict": "approve", "summary": "x", "findings": [
        {"code": "logic", "path": None, "line": None, "message": "m", "paved_road": "p"}]}),
    (json.dumps({"written": [REPLY_PATH], "status": "ok"}), {"verdict": "snag"}),
    (json.dumps({"written": [".tempo/outbox/correctness_review/other/1/reply.json"], "status": "ok"}), APPROVE),
    (json.dumps({"written": [], "status": "ok"}), APPROVE),
])
def test_bad_replies_blocked(root, message, body):
    orchestrator_spawn(root, json.dumps(MANIFEST))
    write_reply(root, body)
    result = stop(root, "claude", message)
    assert result["decision"] == "block" and result["reason"]


def test_stop_retries_bounded(root):
    orchestrator_spawn(root, json.dumps(MANIFEST))
    for _ in range(STOP_RETRIES):
        assert stop(root, "claude", "prose")["decision"] == "block"
    assert stop(root, "claude", "prose") is None  # spent: accept() records invalid_artifact


def test_failed_status_stops(root):
    assert stop(root, "claude", json.dumps({"written": [], "status": "failed"})) is None


def test_codex_stop_clears_active_marker(root):
    codex_start(root, "tempo-correctness-review")
    orchestrator_spawn(root, json.dumps(MANIFEST))
    write_reply(root, APPROVE)
    stop(root, "codex", json.dumps({"written": [REPLY_PATH], "status": "ok"}))
    assert State(root).active() == []


def test_unparseable_call_fails_closed(root):
    out, _ = handle("claude", "PreToolUse", {"cwd": str(root), "tool_name": "Write", "tool_input": None,
                                             "agent_id": "a", "agent_type": "tempo-author"}, {})
    assert "permissionDecision" in out
