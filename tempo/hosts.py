"""What differs per host (AGENT_PLAN.md section 5): file layout, hook payloads, and hook replies.

Payload fields and tool names are each host's documented shapes; the P4c live gate captures the real
payloads and corrects them here. A payload that does not parse is None, and every hook denies it.
"""

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from tempo.hostfiles import CONDUCT_FILES

HOSTS = ("claude", "copilot", "codex")
EVENTS = {"PreToolUse": "pre_tool", "PostToolUse": "post_tool", "SubagentStart": "subagent_start",
          "SubagentStop": "subagent_stop"}


@dataclass(frozen=True)
class Layout:
    instructions: str
    orchestrator: str
    start: str  # the /tempo-run command; on Codex a skill, since Codex has no project-level prompts
    agents: str
    agent_suffix: str
    hooks: str


LAYOUTS = {
    "claude": Layout(CONDUCT_FILES["claude"], ".claude/agents/tempo-orchestrator.md", ".claude/commands/tempo-run.md",
                     ".claude/agents", ".md", ".claude/settings.json"),
    "copilot": Layout(CONDUCT_FILES["copilot"], ".github/agents/tempo-orchestrator.agent.md",
                      ".github/prompts/tempo-run.prompt.md", ".github/agents", ".agent.md", ".github/hooks/tempo.json"),
    "codex": Layout(CONDUCT_FILES["codex"], ".codex/tempo-orchestrator.md", ".agents/skills/tempo-run/SKILL.md",
                    ".codex/agents", ".toml", ".codex/hooks.json"),
}


def agent_path(repo: Path, host: str, name: str) -> Path:
    layout = LAYOUTS[host]
    return repo / layout.agents / f"{name}{layout.agent_suffix}"


def hook_config(host: str) -> dict[str, Any]:
    """The host's hook file: every event runs `tempo hook <host>`, which reads the event from the payload."""
    command = {"type": "command", "command": f"tempo hook {host}"}
    if host == "copilot":
        return {"version": 1, "hooks": {event: [command] for event in EVENTS}}
    return {"hooks": {event: [({"matcher": "*"} if event.endswith("ToolUse") else {}) | {"hooks": [command]}]
                      for event in EVENTS}}


Kind = Literal["read", "search", "write", "shell", "spawn", "message", "stop", "wait", "noop", "other"]


@dataclass(frozen=True)
class ToolUse:
    kind: Kind
    name: str
    paths: tuple[str, ...] = ()
    command: str | None = None
    agent: str | None = None
    prompt: str | None = None
    background: bool = False


@dataclass(frozen=True)
class HookEvent:
    host: str
    event: Literal["pre_tool", "post_tool", "subagent_start", "subagent_stop"]
    chat: str
    cwd: str
    agent_type: str | None = None  # the calling subagent; None for a top-level chat (always None on Codex)
    agent_id: str | None = None
    tool: ToolUse | None = None
    message: str = ""
    model: str | None = None


@dataclass(frozen=True)
class _Tool:
    kind: Kind
    paths: tuple[str, ...] = ()
    command: str | None = None
    agent: str | None = None
    prompt: str | None = None
    background: str | None = None


@dataclass(frozen=True)
class _Fields:
    event: str
    chat: str
    cwd: str
    tool_name: str
    tool_input: str
    agent_type: str | None
    agent_id: str | None
    message: str
    model: str
    result: str | None = None  # PostToolUse: the tool's result


_SNAKE = _Fields("hook_event_name", "session_id", "cwd", "tool_name", "tool_input", "agent_type", "agent_id",
                 "last_assistant_message", "model")
FIELDS = {
    "claude": replace(_SNAKE, result="tool_response"),
    "copilot": _Fields("hookEventName", "sessionId", "cwd", "toolName", "toolInput", "agentType", "agentId",
                       "lastAssistantMessage", "model"),
    "codex": _Fields("hook_event_name", "session_id", "cwd", "tool_name", "tool_input", None, None,
                     "last_assistant_message", "model"),
}

TOOLS: dict[str, dict[str, _Tool]] = {
    "claude": {
        "Read": _Tool("read", ("file_path",)),
        "Grep": _Tool("search", ("path",)),
        "Glob": _Tool("search", ("path",)),
        "LS": _Tool("search", ("path",)),
        "Write": _Tool("write", ("file_path",)),
        "Edit": _Tool("write", ("file_path",)),
        "MultiEdit": _Tool("write", ("file_path",)),
        "NotebookEdit": _Tool("write", ("notebook_path",)),
        "Bash": _Tool("shell", command="command"),
        "Task": _Tool("spawn", agent="subagent_type", prompt="prompt", background="run_in_background"),
        "Agent": _Tool("spawn", agent="subagent_type", prompt="prompt", background="run_in_background"),
        "SendMessage": _Tool("message"),
        "TaskStop": _Tool("stop"),
        "TodoWrite": _Tool("noop"),
        "ToolSearch": _Tool("noop"),  # loads deferred tool schemas, TaskStop among them
        "SubagentHandback": _Tool("noop"),  # a subagent's final report to its chat
    },
    "copilot": {
        "read_file": _Tool("read", ("filePath",)),
        "list_dir": _Tool("search", ("path",)),
        "file_search": _Tool("search"),
        "grep_search": _Tool("search"),
        "create_file": _Tool("write", ("filePath",)),
        "replace_string_in_file": _Tool("write", ("filePath",)),
        "insert_edit_into_file": _Tool("write", ("filePath",)),
        "run_in_terminal": _Tool("shell", command="command"),
        "runSubagent": _Tool("spawn", agent="agentName", prompt="prompt"),
        "manage_todo_list": _Tool("noop"),
    },
    "codex": {
        "Bash": _Tool("shell", command="command"),
        "apply_patch": _Tool("write"),
        "read_file": _Tool("read", ("path",)),
        "list_dir": _Tool("search", ("dir_path",)),
        "grep_files": _Tool("search", ("path",)),
        "collaborationspawn_agent": _Tool("spawn", agent="agent_type", prompt="message"),
        "collaborationsend_message": _Tool("message"),
        "resume_agent": _Tool("message"),
        "wait": _Tool("wait"),
        "close_agent": _Tool("stop"),
        "update_plan": _Tool("noop"),
    },
}

_PATCH_PATH = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", re.M)


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _tool(host: str, name: str, args: Any) -> ToolUse | None:
    spec = TOOLS[host].get(name)
    if spec is None:
        return ToolUse(kind="other", name=name)
    if not isinstance(args, dict):
        return None
    paths: list[str] = []
    for field in spec.paths:
        if field in args:
            if (path := _str(args[field])) is None:
                return None
            paths.append(path)
    if host == "codex" and name == "apply_patch":
        patch = _str(args.get("command"))
        if patch is None or not (found := _PATCH_PATH.findall(patch)):
            return None
        paths = [p.strip() for p in found]
    command = None
    if spec.command is not None and (command := _str(args.get(spec.command))) is None:
        return None
    agent = prompt = None
    if spec.kind == "spawn":  # no agent named spawns the host's default agent
        agent, prompt = args.get(spec.agent), _str(args.get(spec.prompt))
        if prompt is None or (agent is not None and not isinstance(agent, str)):
            return None
    background = spec.background is not None and args.get(spec.background) is True
    return ToolUse(kind=spec.kind, name=name, paths=tuple(paths), command=command, agent=agent, prompt=prompt,
                   background=background)


def _result(value: Any) -> tuple[str, str | None]:
    """A spawn's result as (its text, the model that ran it); Claude's is `{content: [{type, text}],
    resolvedModel}`."""
    if not isinstance(value, dict) or not isinstance(content := value.get("content"), list):
        return "", None
    text = "".join(b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    return text, _str(value.get("resolvedModel"))


def parse(host: str, data: bytes) -> HookEvent | None:
    """The normalized event, or None when the payload does not parse."""
    if host not in FIELDS:
        return None
    f = FIELDS[host]
    try:
        payload = json.loads(data)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    event = EVENTS.get(payload.get(f.event))
    chat, cwd = _str(payload.get(f.chat)), _str(payload.get(f.cwd))
    if event is None or not chat or not cwd:
        return None
    agent_type = _str(payload.get(f.agent_type)) if f.agent_type else None
    agent_id = _str(payload.get(f.agent_id)) if f.agent_id else None
    tool = None
    if event in ("pre_tool", "post_tool"):
        name = _str(payload.get(f.tool_name))
        if not name or (tool := _tool(host, name, payload.get(f.tool_input, {}))) is None:
            return None
    message, model = _str(payload.get(f.message)) or "", _str(payload.get(f.model))
    if event == "post_tool" and f.result is not None:
        message, model = _result(payload.get(f.result))
    return HookEvent(host=host, event=event, chat=chat, cwd=cwd, agent_type=agent_type, agent_id=agent_id,
                     tool=tool, message=message, model=model)


def reply(allow: bool | None, reason: str) -> str:
    """A PreToolUse decision on stdout; None leaves the host's own permission rules in charge."""
    if allow is None:
        return ""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                              "permissionDecision": "allow" if allow else "deny",
                                              "permissionDecisionReason": reason}})
