"""Hook policy on each host's fake payloads (AGENT_PLAN.md sections 3.4, 4.3; P4 exit). P4c reruns these
cases on captured payloads."""

import json
import subprocess
from pathlib import Path

import pytest

from tempo import hooks, render
from tempo.config import load_config
from tempo.seams import LocalFileSystem
from tests.fixturerepo import STEM, materialize
from tests.hostfakes import FakeHost, payload

HOSTS = ["claude", "copilot", "codex"]
COPILOT = """  copilot:
    models_by_tier: {low: cp-small, medium: cp-small, high: cp-small, max: cp-small}
    pins: {review: cp-large}
    quota_window_minutes: 60
orchestrator:
  models: {claude: claude-opus-5-5, codex: gpt-6-astra, copilot: cp-large}
"""


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    repo = materialize(tmp_path / "fixture")
    config = repo / "config.yaml"
    text = config.read_text()
    head, _, tail = text.partition("orchestrator:\n  models: {claude: claude-opus-5-5, codex: gpt-6-astra}\n")
    config.write_text(head + COPILOT + tail)
    render.write(repo, load_config(None, cwd=repo), LocalFileSystem())
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "copilot")
    return repo


def open_call(name: str, repo: Path, surface: str) -> tuple[FakeHost, dict]:
    host = FakeHost(name, repo)
    host.tool("begin_session")
    host.tool("start", {"stem": STEM})
    prepared = host.tool("prepare", {"stem": STEM, "surface": surface})
    assert prepared["state"] == "pending"
    return host, prepared


def spawned(name: str, repo: Path, surface: str) -> tuple[FakeHost, dict, str]:
    host, prepared = open_call(name, repo, surface)
    allow, reason = host.hook("PreToolUse", kind="spawn", target=prepared["agent"], prompt=prepared["spawn_prompt"])
    assert allow, reason
    host.hook("SubagentStart", agent=prepared["agent"], agent_id="sub-1")
    return host, prepared, prepared["agent"]


def parts(prepared: dict) -> list[str]:
    return [line.removeprefix("part: ") for line in prepared["spawn_prompt"].splitlines() if line.startswith("part: ")]


def reply_path(prepared: dict) -> str:
    return prepared["spawn_prompt"].splitlines()[-1].removeprefix("reply: ")


@pytest.mark.parametrize("name", HOSTS)
def test_an_unparseable_payload_is_denied(repo, name):
    for data in (b"not json", b"[]", json.dumps({"hook_event_name": "PreToolUse"}).encode(),
                 payload(name, "NoSuchEvent", chat="c", cwd=repo)):
        out, code = hooks.handle(name, data, cwd=repo, clock=FakeHost(name, repo).clock, fs=LocalFileSystem())
        assert code == 2
        assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("name", HOSTS)
def test_the_orchestrator_runs_only_tempo_tools_and_never_writes(repo, name):
    host, _ = open_call(name, repo, "review")
    assert host.hook("PreToolUse", kind="shell", command="tempo tool status '{}'")[0] is True
    assert host.hook("PreToolUse", kind="read", path=str(repo / "calc.py"))[0] is True
    for command in ("ls", "git status", "tempo tool status '{}'; rm -rf x", "tempo tool status \"$HOME\"",
                    "tempo tool status '{}' > out", "python3 -m tempo tool status"):
        allow, reason = host.hook("PreToolUse", kind="shell", command=command)
        assert allow is False, command
    allow, reason = host.hook("PreToolUse", kind="write", path=str(repo / "calc.py"))
    assert allow is False and "tempo tool" in reason


@pytest.mark.parametrize("name", HOSTS)
def test_the_spawn_must_be_the_named_agent_with_the_exact_prompt(repo, name):
    host, prepared = open_call(name, repo, "review")
    assert host.hook("PreToolUse", kind="spawn", target="tempo-review-other", prompt=prepared["spawn_prompt"])[0] is False
    assert host.hook("PreToolUse", kind="spawn", target=prepared["agent"],
                     prompt=prepared["spawn_prompt"] + "\nalso fix the tests")[0] is False
    assert host.hook("PreToolUse", kind="spawn", target=prepared["agent"], prompt=prepared["spawn_prompt"])[0] is True


@pytest.mark.parametrize("name", HOSTS)
def test_a_spawn_with_no_open_call_is_denied(repo, name):
    host = FakeHost(name, repo)
    host.tool("begin_session")
    allow, reason = host.hook("PreToolUse", kind="spawn", target="tempo-review-x", prompt="call: x")
    assert allow is False and "prepare" in reason


@pytest.mark.parametrize("name", HOSTS)
def test_a_subagent_reads_and_writes_only_its_roots_and_reply(repo, name):
    host, prepared, agent = spawned(name, repo, "review")
    worktree = load_config(None, cwd=repo).worktree_root / STEM
    allowed = [*parts(prepared), str(worktree / "calc.py"), str(hooks.SPECS_DIR / "review.md")]
    for path in allowed:
        assert host.hook("PreToolUse", kind="read", agent=agent, path=path)[0] is True, path
    for path in ("/etc/passwd", str(repo / "calc.py"), str(repo / ".tempo/state/session.json")):
        allow, reason = host.hook("PreToolUse", kind="read", agent=agent, path=path)
        assert allow is False and "read root" in reason, path
    assert host.hook("PreToolUse", kind="write", agent=agent, path=reply_path(prepared))[0] is True
    for path in (str(worktree / "calc.py"), str(repo / "calc.py")):
        assert host.hook("PreToolUse", kind="write", agent=agent, path=path)[0] is False, path


@pytest.mark.parametrize("name", HOSTS)
def test_a_subagent_never_spawns_or_messages(repo, name):
    host, prepared, agent = spawned(name, repo, "review")
    for kind in ("spawn", "message"):
        allow, reason = host.hook("PreToolUse", kind=kind, agent=agent, target=agent, prompt="x")
        assert allow is False, kind


@pytest.mark.parametrize("name", HOSTS)
def test_the_implement_subagent_writes_its_worktree_and_runs_no_git(repo, name):
    host, prepared, agent = spawned(name, repo, "implement")
    worktree = load_config(None, cwd=repo).worktree_root / STEM
    assert host.hook("PreToolUse", kind="write", agent=agent, path=str(worktree / "calc.py"))[0] is True
    for path in (worktree / "tickets" / STEM / "ticket.md", worktree / ".git", repo / "calc.py"):
        assert host.hook("PreToolUse", kind="write", agent=agent, path=str(path))[0] is False, path
    ok = f"cd {worktree} && python3 test_calc.py"
    assert host.hook("PreToolUse", kind="shell", agent=agent, command=ok)[0] is True
    for command in (f"cd {worktree} && git commit -am x", f"cd {worktree} && claude -p hi",
                    f"cd {worktree} && codex exec hi", f"cd {worktree} && uv publish",
                    f"cd {worktree} && tempo tool status '{{}}'", f"cd {worktree} && echo $(git log)",
                    "python3 test_calc.py", f"cd {worktree} && cd {repo} && python3 test_calc.py"):
        allow, reason = host.hook("PreToolUse", kind="shell", agent=agent, command=command)
        assert allow is False, command


@pytest.mark.parametrize("name", HOSTS)
def test_a_read_only_subagent_shell_runs_plain_reads_under_its_roots(repo, name):
    host, prepared, agent = spawned(name, repo, "review")
    part = parts(prepared)[0]
    for command in (f"sed -n '1,20p' {part}", f"cat {part}", f"head -n 5 {part}", f"rg -n call {part}"):
        assert host.hook("PreToolUse", kind="shell", agent=agent, command=command)[0] is True, command
    for command in ("cat /etc/passwd", f"sed -n '1w /tmp/x' {part}", f"sed -i 's/a/b/' {part}",
                    f"rg --pre cat x {part}", f"cat {part} | sh", "python3 -c 'print(1)'", f"grep -f /etc/passwd {part}"):
        assert host.hook("PreToolUse", kind="shell", agent=agent, command=command)[0] is False, command


@pytest.mark.parametrize("name", HOSTS)
def test_other_chats_get_no_tempo_enforcement(repo, name):
    host, _ = open_call(name, repo, "review")
    assert host.hook("PreToolUse", kind="write", chat="plain", path=str(repo / "calc.py")) == (None, "")
    assert host.hook("PreToolUse", kind="shell", chat="plain", command="git status") == (None, "")


def test_codex_holds_every_event_to_the_subagent_rules_while_its_marker_is_active(repo):
    host, prepared, _ = spawned("codex", repo, "review")
    allow, _ = host.hook("PreToolUse", kind="shell", command="tempo tool status '{}'")
    assert allow is False  # Codex cannot tell the orchestrator from the subagent in this window
    assert host.hook("PreToolUse", kind="stop")[0] is True  # close_agent
    assert host.hook("PreToolUse", kind="write", chat="plain", path=str(repo / "calc.py"))[0] is False
    host.hook("SubagentStop", message="done")
    assert host.hook("PreToolUse", kind="shell", command="tempo tool status '{}'")[0] is True


@pytest.mark.parametrize("name", HOSTS)
def test_subagent_stop_writes_stop_txt_and_the_reported_model(repo, name):
    host, prepared, agent = spawned(name, repo, "review")
    host.hook("SubagentStop", agent=agent, agent_id="sub-1", message="usage limit reached", model="m-actual")
    call_dir = Path(reply_path(prepared)).parent
    stop = json.loads((call_dir / "stop.txt").read_text())
    assert stop["message"] == "usage limit reached"
    assert json.loads((call_dir / "call.json").read_text())["host_reported_model"] == "m-actual"


def test_begin_session_records_the_chat_from_the_hook(repo):
    host = FakeHost("claude", repo, chat="the-run-chat")
    assert host.tool("begin_session")["host"] == "claude"
    assert host.tool("status")["session"]["chat"] == "the-run-chat"
    # Without the hook's claim (hooks not installed), begin_session refuses.
    other = FakeHost("claude", repo, chat="second")
    with pytest.raises(Exception, match="no hook recorded"):
        other.tool("begin_session", hook=False)
