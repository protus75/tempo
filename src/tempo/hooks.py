"""Host hook adapters: normalize Claude Code, GitHub Copilot, and Codex payloads onto `policy`.

Invoked as `tempo hook <host> <event>` with the host's JSON payload on stdin.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from pathlib import Path

from tempo import policy
from tempo.policy import Call, Caller, Verdict
from tempo.roles import TEMPO_TOOLS, by_agent_name, roles

HOSTS = ("claude", "copilot", "codex")
STOP_RETRIES = 2

TOOL_KINDS = {
    "claude": {
        "Read": "read", "LS": "read", "Grep": "search", "Glob": "search",
        "Write": "write", "Edit": "write", "MultiEdit": "write", "NotebookEdit": "write",
        "Agent": "spawn", "Task": "spawn", "SendMessage": "spawn_message", "Bash": "shell",
    },
    "copilot": {
        "view": "read", "read": "read", "grep": "search", "glob": "search",
        "create": "write", "edit": "write", "write": "write",
        "task": "spawn", "bash": "shell", "powershell": "shell",
    },
    "codex": {
        "read_file": "read", "list_dir": "read", "view_image": "read", "grep_files": "search",
        "apply_patch": "write", "Edit": "write", "Write": "write",
        "spawn_agent": "spawn", "send_input": "spawn_message", "resume_agent": "spawn_message",
        "wait": "spawn_wait", "close_agent": "spawn_wait",
        "Bash": "shell", "shell": "shell", "shell_command": "shell", "exec_command": "shell", "local_shell": "shell",
    },
}
_TEMPO_TOOL = re.compile(r"^(?:mcp__tempo__|tempo[/-])(?P<name>[a-z_]+)$")
_PATCH_PATH = re.compile(r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+)$", re.M)
_TARGET_KEYS = ("subagent_type", "agent_type", "agentType", "agent_name", "agentName", "agent", "name")
_PROMPT_KEYS = ("prompt", "message", "task", "input")
_PATH_KEYS = ("file_path", "notebook_path", "path", "filePath")


def _get(payload: Mapping, *keys: str):
    for key in keys:
        if payload.get(key) not in (None, ""):
            return payload[key]
    return None


def _args(payload: Mapping) -> dict:
    args = _get(payload, "tool_input", "toolArgs", "tool_args")
    if isinstance(args, str):  # Copilot may deliver toolArgs as a JSON string
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return {"input": args}
    return args if isinstance(args, dict) else {}


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def classify(host: str, payload: Mapping) -> Call:
    tool = str(_get(payload, "tool_name", "toolName") or "")
    args = _args(payload)
    named = _TEMPO_TOOL.match(tool)
    if named and named["name"] in TEMPO_TOOLS:
        return Call(tool, "tempo")
    kind = TOOL_KINDS[host].get(tool, "other")
    if kind == "write":
        paths = [str(p) for p in (_get(args, *_PATH_KEYS),) if p]
        if not paths:  # apply_patch carries its targets inside the patch text
            paths = [m.strip() for s in _strings(args) for m in _PATCH_PATH.findall(s)]
        return Call(tool, kind, paths=tuple(paths))
    if kind == "shell":
        command = args.get("command") or args.get("cmd")
        if isinstance(command, list):  # ["bash", "-lc", "<script>"] or a bare argv
            command = command[-1] if len(command) >= 3 and command[1] in ("-c", "-lc") else " ".join(command)
        return Call(tool, kind, command=command if isinstance(command, str) else None)
    if kind == "spawn":
        prompt = _get(args, *_PROMPT_KEYS)
        return Call(tool, kind, spawn_target=_get(args, *_TARGET_KEYS),
                    spawn_prompt=prompt if isinstance(prompt, str) else None)
    return Call(tool, kind)


def find_root(payload: Mapping, env: Mapping[str, str]) -> Path:
    start = Path(env.get("CLAUDE_PROJECT_DIR") or _get(payload, "cwd") or ".").resolve()
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists() or (candidate / ".tempo").is_dir():
            return candidate
    return start


class State:
    """Hook bookkeeping under .tempo/state/hooks: pending manifests, active Codex agents, stop retries."""

    def __init__(self, root: Path):
        self.dir = root / policy.STATE / "hooks"

    def _path(self, *parts: str) -> Path:
        path = self.dir.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def log(self, entry: dict) -> None:
        with self._path("log.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), **entry}) + "\n")

    def add_pending(self, role: str, subject: str, attempt: int) -> None:
        self._path("pending", role, f"{subject}@{attempt}").touch()

    def pending(self, role: str) -> set[tuple[str, int]]:
        folder = self.dir / "pending" / role
        found = set()
        for entry in folder.glob("*@*") if folder.is_dir() else ():
            subject, _, attempt = entry.name.rpartition("@")
            if attempt.isdigit():
                found.add((subject, int(attempt)))
        return found

    def clear_pending(self, role: str, written: list[str]) -> None:
        for raw in written:
            parts = Path(raw).parts
            if len(parts) >= 5:
                (self.dir / "pending" / role / f"{parts[-3]}@{parts[-2]}").unlink(missing_ok=True)

    def set_active(self, agent_id: str, agent_type: str | None) -> None:
        self._path("active", agent_id).write_text(agent_type or "", encoding="utf-8")

    def clear_active(self, agent_id: str) -> None:
        (self.dir / "active" / agent_id).unlink(missing_ok=True)

    def active(self) -> list[str]:
        folder = self.dir / "active"
        return [p.read_text(encoding="utf-8") for p in sorted(folder.iterdir())] if folder.is_dir() else []

    def bump_stop(self, agent_id: str) -> int:
        path = self._path("stops", agent_id)
        count = int(path.read_text() or 0) + 1 if path.exists() else 1
        path.write_text(str(count))
        return count


def caller(host: str, payload: Mapping, env: Mapping[str, str], state: State) -> Caller:
    agent_type = _get(payload, "agent_type", "agentType")
    named = by_agent_name(agent_type)
    if named is not None and named.subagent:
        return Caller(named.id, True)
    if host == "codex":
        # Codex reports the parent session for subagent tool calls, so the active-agent markers
        # written at SubagentStart are the only caller signal (AGENT_PLAN.md section 8.4).
        active = state.active()
        if len(active) > 1:
            return Caller("ambiguous", True)
        if active:
            role = by_agent_name(active[0])
            return Caller(role.id if role else "foreign", True)
    else:
        # Claude marks subagent calls with agent_id; Copilot with parentAgentId (its root agent has an agentId too).
        marker = payload.get("agent_id") if host == "claude" else _get(payload, "parentAgentId", "parent_agent_id")
        if marker:
            return Caller(named.id if named else "foreign", True)
    if named is not None:
        return Caller(named.id, False)  # e.g. `claude --agent tempo-orchestrator`
    role = env.get("TEMPO_ROLE", "")
    if role == "implement" or (role in roles() and not roles()[role].subagent):
        return Caller(role, False)
    return Caller("developer", False)


def _event(name: str) -> str:
    return name.replace("_", "").replace("-", "").lower()


def handle(host: str, event: str, payload: Mapping, env: Mapping[str, str]) -> tuple[str, int]:
    """Returns (stdout, exit code). Exit 0 always; decisions travel as JSON on stdout."""
    if host not in HOSTS:
        raise ValueError(f"unknown host {host!r}; expected one of {HOSTS}")
    root = find_root(payload, env)
    state = State(root)
    ev = _event(event)
    if ev == "pretooluse":
        try:
            who = caller(host, payload, env, state)
            call = classify(host, payload)
            verdict = policy.decide(who, call, root, dict(env))
        except Exception as err:  # fail closed: an unparseable call is refused
            who, call = Caller("unknown", False), Call(str(payload.get("tool_name", "?")), "other")
            verdict = Verdict(False, f"tempo hook error ({type(err).__name__}: {err}); report it and stop.")
        if verdict.allow and verdict.record:
            state.add_pending(**verdict.record)
        if not verdict.allow:
            state.log({"host": host, "event": "deny", "role": who.role, "tool": call.tool, "reason": verdict.reason})
            return _deny(host, verdict.reason), 0
        return "", 0
    if ev == "subagentstart":
        agent_id = _get(payload, "agent_id", "agentId")
        if host == "codex" and agent_id:
            state.set_active(str(agent_id), _get(payload, "agent_type", "agentType", "agentName"))
        return "", 0
    if ev == "subagentstop":
        return _stop(host, payload, state, root)
    raise ValueError(f"unhandled event {event!r}")


def _stop(host: str, payload: Mapping, state: State, root: Path) -> tuple[str, int]:
    agent_id = str(_get(payload, "agent_id", "agentId") or "unknown")
    role = by_agent_name(_get(payload, "agent_type", "agentType"))
    if role is None or not role.subagent:
        state.clear_active(agent_id)
        return "", 0
    text = _get(payload, "last_assistant_message", "response", "lastAssistantMessage")
    verdict = policy.check_reply(role, text if isinstance(text, str) else None, root, state.pending(role.id))
    if verdict.allow:
        state.clear_pending(role.id, verdict.record["written"])
        state.clear_active(agent_id)
        return "", 0
    state.log({"host": host, "event": "reply_refused", "role": role.id, "agent": agent_id, "reason": verdict.reason})
    if state.bump_stop(agent_id) > STOP_RETRIES:
        # Retries spent: let it stop; `accept` records invalid_artifact for this attempt.
        state.clear_active(agent_id)
        return "", 0
    return json.dumps({"decision": "block", "reason": verdict.reason}), 0


def _deny(host: str, reason: str) -> str:
    if host == "copilot":
        return json.dumps({"permissionDecision": "deny", "permissionDecisionReason": reason})
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}})
