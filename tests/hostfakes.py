"""Host fakes (AGENT_PLAN.md P4 step e): each host's hook payloads, a fake host that runs them through
`tempo hook`, and a fake subagent. Field and tool names match the P4c live-gate captures (tests/captured)."""

import asyncio
import io
import json
import os
import shlex
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

from tempo.__main__ import main
from tempo.checkout import Checkout
from tempo.config import load_config
from tempo.git import Git
from tempo.hosts import FIELDS
from tempo.journal import Journal
from tempo.seams import LocalFileSystem, SubprocessExec
from tempo.tools import Tools

ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/nonexistent",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_AUTHOR_DATE": "2026-11-02T12:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-11-02T12:00:00+00:00",
}

# kind -> (tool name, how its input is built)
TOOLS = {
    "claude": {"read": "Read", "search": "Grep", "write": "Write", "shell": "Bash", "spawn": "Task",
               "message": "SendMessage", "stop": "TaskStop"},
    "copilot": {"read": "read_file", "search": "list_dir", "write": "create_file", "shell": "run_in_terminal",
                "spawn": "runSubagent", "message": "send_to_agent", "stop": "stop_agent"},
    "codex": {"read": "read_file", "search": "grep_files", "write": "apply_patch", "shell": "Bash",
              "spawn": "collaborationspawn_agent", "message": "collaborationsend_message", "stop": "close_agent"},
}


def tool_input(host: str, kind: str, *, path: str | None = None, command: str | None = None,
               target: str | None = None, prompt: str | None = None) -> dict:
    if kind in ("read", "search"):
        field = {"claude": {"read": "file_path", "search": "path"}, "copilot": {"read": "filePath", "search": "path"},
                 "codex": {"read": "path", "search": "path"}}[host][kind]
        return {field: path} if path is not None else {"pattern": "x"}
    if kind == "write":
        if host == "codex":
            return {"command": f"*** Begin Patch\n*** Add File: {path}\n+x\n*** End Patch"}
        return {{"claude": "file_path", "copilot": "filePath"}[host]: path, "content": "x"}
    if kind == "shell":
        return {"command": command}
    if kind == "spawn":
        return {"claude": {"subagent_type": target, "prompt": prompt, "description": "tempo call"},
                "copilot": {"agentName": target, "prompt": prompt},
                "codex": {"agent_type": target, "message": prompt}}[host]
    return {}


def payload(host: str, event: str, *, chat: str, cwd: Path | str, agent: str | None = None,
            agent_id: str | None = None, kind: str | None = None, message: str | None = None,
            model: str | None = None, **tool) -> bytes:
    f = FIELDS[host]
    body: dict = {f.event: event, f.chat: chat, f.cwd: str(cwd)}
    if agent is not None and f.agent_type is not None:
        body[f.agent_type] = agent
    if agent_id is not None and f.agent_id is not None:
        body[f.agent_id] = agent_id
    if kind is not None:
        body[f.tool_name] = TOOLS[host][kind]
        body[f.tool_input] = tool_input(host, kind, **tool)
    if message is not None:
        body[f.message] = message
    if model is not None:
        body[f.model] = model
    return json.dumps(body).encode()


def run_hook(host: str, data: bytes, repo: Path, clock) -> tuple[str, int]:
    """`tempo hook <host>` as the host runs it: the CLI entry, so its engine log write is exercised too."""
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        code = main(["hook", host], cwd=repo, clock=clock, stdin=data)
    return out.getvalue().strip(), code


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 2, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class FakeHost:
    """One host's VS Code chat: its hooks run through `tempo hook`, its shell runs `tempo tool` in-process."""

    def __init__(self, host: str, repo: Path, *, clock: Clock | None = None, chat: str = "chat-1") -> None:
        self.host, self.repo, self.chat = host, repo, chat
        self.clock = clock or Clock()
        self.launched: list[tuple[str, int]] = []
        self.ids = (f"{n:032x}" for n in range(1, 10_000))
        self.token: str | None = None

    def hook(self, event: str, **kw) -> tuple[bool | None, str]:
        out, _ = run_hook(self.host, payload(self.host, event, chat=kw.pop("chat", self.chat),
                                             cwd=kw.pop("cwd", self.repo), **kw), self.repo, self.clock)
        if not out:
            return None, ""
        decision = json.loads(out)["hookSpecificOutput"]
        return decision["permissionDecision"] == "allow", decision["permissionDecisionReason"]

    def tools(self) -> Tools:
        config = load_config(None, cwd=self.repo)
        exec_ = SubprocessExec()
        checkout = Checkout(repo=self.repo, config=config, env=ENV, exec_=exec_,
                            git=Git(exec_, env=ENV, timeout=30.0), journal=Journal(config.state_dir, self.clock),
                            fs=LocalFileSystem(), clock=self.clock)

        def launch(id: str, fd: int) -> None:
            self.launched.append((id, os.dup(fd)))

        class Quiet:
            async def notify(self, argv):
                return None

        return Tools(checkout, launch=launch, notifications=Quiet(), new_id=lambda: next(self.ids))

    def run_jobs(self) -> None:
        """The detached workers: each runs once the tool that launched it has released the writer lock."""
        while self.launched:
            id, fd = self.launched.pop(0)
            try:
                asyncio.run(self.tools().run_job(id))
            finally:
                os.close(fd)

    def tool(self, name: str, args: dict | None = None, *, hook: bool = True) -> dict:
        """The orchestrator runs `tempo tool <name> '<json>'`: the shell hook first, then the tool."""
        args = dict(args or {})
        if self.token is not None and name not in ("begin_session",):
            args.setdefault("session", self.token)
        if hook:
            allow, reason = self.hook("PreToolUse", kind="shell",
                                      command=f"tempo tool {name} {shlex.quote(json.dumps(args))}")
            assert allow is not False, reason
        result = asyncio.run(self.tools().run(name, args))
        self.run_jobs()
        if name == "begin_session":
            self.token = result["session"]
        return result

    def until_done(self, result: dict, name: str, args: dict) -> dict:
        while result.get("state") in ("pending", "running"):
            result = self.tool(name, args)
        return result

    def subagent(self, prepared: dict, reply: str, *, edits: dict[str, str] | None = None,
                 shell: list[str] = (), message: str = "done", model: str | None = None) -> None:
        """Spawn the call's agent as the orchestrator, then act as it: read each part, edit, reply, stop."""
        agent = prepared["agent"]
        allow, reason = self.hook("PreToolUse", kind="spawn", target=agent, prompt=prepared["spawn_prompt"])
        assert allow, reason
        assert self.hook("SubagentStart", agent=agent, agent_id="sub-1") == (None, "")
        lines = prepared["spawn_prompt"].splitlines()
        parts = [line.removeprefix("part: ") for line in lines if line.startswith("part: ")]
        out = next(line.removeprefix("reply: ") for line in lines if line.startswith("reply: "))
        root = Path(Path(parts[0]).read_text().splitlines()[0].removeprefix("tempo read root: "))
        for part in parts:
            allow, reason = self.hook("PreToolUse", kind="read", agent=agent, path=part)
            assert allow, reason
        for rel, text in (edits or {}).items():
            allow, reason = self.hook("PreToolUse", kind="write", agent=agent, path=str(root / rel))
            assert allow, reason
            (root / rel).write_text(text)
        for command in shell:
            allow, reason = self.hook("PreToolUse", kind="shell", agent=agent, command=command, cwd=root)
            assert allow, reason
        allow, reason = self.hook("PreToolUse", kind="write", agent=agent, path=out)
        assert allow, reason
        Path(out).write_text(reply)
        assert self.hook("SubagentStop", agent=agent, agent_id="sub-1", message=message, model=model) == (None, "")
