"""Hook policy on payloads captured in the P4c live gate (AGENT_PLAN.md P4 exit; docs/livegate/claude.md).

Each case loads a captured payload, points it at a fresh fixture repo and chat, and runs it through
`tempo hook`. Mismatch and unparseable cases alter the captured payload. Submit of an unissued call is
covered at the tool level (tests/test_tools.py).
"""

import json
from pathlib import Path

import pytest

from tempo import hooks
from tempo.config import load_config
from tests.fixturerepo import STEM, materialize
from tests.hostfakes import FakeHost, run_hook

CAPTURED = Path(__file__).resolve().parent / "captured"


@pytest.fixture
def repo(tmp_path):
    return materialize(tmp_path / "fixture")


def captured(host: str, name: str, repo: Path, chat: str, **tool_input) -> bytes:
    text = (CAPTURED / host / f"{name}.json").read_text().replace("@REPO@", str(repo)).replace("@CHAT@", chat)
    body = json.loads(text)
    body.get("tool_input", {}).update(tool_input)
    return json.dumps(body).encode()


def decision(host: FakeHost, data: bytes) -> bool | None:
    out, _ = run_hook("claude", data, host.repo, host.clock)
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "allow" if out else None


def open_call(repo: Path, surface: str) -> tuple[FakeHost, dict]:
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    host.tool("start", {"stem": STEM})
    return host, host.tool("prepare", {"stem": STEM, "surface": surface})


def spawn(host: FakeHost, prepared: dict, **tool_input) -> bool | None:
    fields = {"prompt": prepared["spawn_prompt"], "subagent_type": prepared["agent"], **tool_input}
    return decision(host, captured("claude", "pre_orchestrator_spawn", host.repo, host.chat, **fields))


def as_subagent(host: FakeHost, name: str, prepared: dict, **tool_input) -> bytes:
    body = json.loads(captured("claude", name, host.repo, host.chat, **tool_input))
    body["agent_type"] = prepared["agent"]
    return json.dumps(body).encode()


def test_the_orchestrator_runs_tempo_tools_and_nothing_else(repo):
    host, _ = open_call(repo, "review")
    assert decision(host, captured("claude", "pre_orchestrator_tempo_tool", repo, host.chat)) is True
    assert decision(host, captured("claude", "pre_orchestrator_tempo_tool", repo, host.chat, command="ls")) is False
    assert decision(host, captured("claude", "pre_orchestrator_write", repo, host.chat)) is False
    assert decision(host, captured("claude", "pre_orchestrator_message", repo, host.chat)) is False
    assert decision(host, captured("claude", "pre_stop", repo, host.chat)) is True


def test_the_spawn_names_the_prepared_agent_and_prompt_in_the_foreground(repo):
    host, prepared = open_call(repo, "review")
    assert spawn(host, prepared, subagent_type="general-purpose") is False
    assert spawn(host, prepared, prompt=prepared["spawn_prompt"] + "\nand also") is False
    assert spawn(host, prepared, run_in_background=True) is False
    assert spawn(host, prepared) is True


def test_a_subagent_reads_and_writes_only_its_call(repo):
    host, prepared = open_call(repo, "review")
    assert spawn(host, prepared) is True
    run_hook("claude", as_subagent(host, "subagent_start", prepared), repo, host.clock)
    assert decision(host, as_subagent(host, "pre_subagent_read", prepared)) is True
    assert decision(host, as_subagent(host, "pre_subagent_read", prepared, file_path="/etc/passwd")) is False
    assert decision(host, as_subagent(host, "pre_subagent_write", prepared)) is True
    assert decision(host, as_subagent(host, "pre_subagent_write", prepared, file_path=str(repo / "calc.py"))) is False
    assert decision(host, as_subagent(host, "pre_subagent_handback", prepared)) is True
    assert decision(host, as_subagent(host, "pre_nested_spawn", prepared)) is False


def test_implement_works_in_its_worktree_and_runs_no_git_or_agent_cli(repo):
    host, prepared = open_call(repo, "implement")
    assert spawn(host, prepared) is True
    worktree = load_config(None, cwd=repo).worktree_root / STEM
    assert decision(host, as_subagent(host, "pre_implement_shell", prepared)) is True
    for command in (f"cd {worktree} && git status", f"cd {worktree} && claude --version"):
        assert decision(host, as_subagent(host, "pre_implement_shell", prepared, command=command)) is False
    assert decision(host, as_subagent(host, "pre_subagent_write", prepared, file_path=str(worktree / "calc.py"))) is True
    for path in ("/tmp/livegate-probe.txt", str(worktree / "tickets" / STEM / "ticket.md")):
        assert decision(host, as_subagent(host, "pre_subagent_write", prepared, file_path=path)) is False


def test_a_limit_hit_reaches_stop_txt_from_the_spawn_result(repo):
    host, prepared = open_call(repo, "review")
    assert spawn(host, prepared) is True
    run_hook("claude", as_subagent(host, "subagent_start", prepared), repo, host.clock)
    run_hook("claude", captured("claude", "post_spawn_limit_hit", repo, host.chat), repo, host.clock)
    call_dir = Path(prepared["spawn_prompt"].splitlines()[-1].removeprefix("reply: ")).parent
    assert "stopped at its 1-turn limit" in json.loads((call_dir / "stop.txt").read_text())["message"]
    assert json.loads((call_dir / "call.json").read_text())["host_reported_model"] == "claude-opus-5-5"
    assert not (load_config(None, cwd=repo).state_dir / hooks.HOOKS_DIR / hooks.ACTIVE).exists()


def test_an_unparseable_captured_payload_is_denied(repo):
    host, _ = open_call(repo, "review")
    for alter in ({"tool_input": "not an object"}, {"session_id": None}, {"hook_event_name": "Unknown"}):
        body = {**json.loads(captured("claude", "pre_subagent_write", repo, host.chat)), **alter}
        out, code = run_hook("claude", json.dumps(body).encode(), repo, host.clock)
        assert code == 2 and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny", alter


def test_codex_captured_payloads_parse():
    from tempo.hosts import parse
    kinds = {name: parse("codex", captured("codex", name, Path("/r"), "c")) for name in
             ("pre_shell", "pre_apply_patch", "pre_spawn", "pre_message", "subagent_stop")}
    assert kinds["pre_shell"].tool.command == "ls"
    assert kinds["pre_apply_patch"].tool.paths == ("probe.txt",)
    assert kinds["pre_spawn"].tool.kind == "spawn" and kinds["pre_message"].tool.kind == "message"
    assert kinds["subagent_stop"].message == "Hi!" and kinds["subagent_stop"].model == "gpt-5.6-sol"
