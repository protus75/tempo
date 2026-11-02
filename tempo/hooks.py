"""Hook policy for all three hosts (AGENT_PLAN.md sections 3.4, 4.3).

`decide` is pure: a normalized event (hosts.parse) and the state it reads give allow, deny, or no
opinion. The orchestrator chat may run only `tempo tool` commands, read, and spawn the one agent its
open call names with exactly that call's spawn prompt. A `tempo-*` subagent reads only under its call's
read root, the engine specs, and its call's parts; writes only its reply (implement also writes its
worktree, never its `ticket.md`); and never spawns, messages, or resumes an agent. On Codex, which names
no caller, every event while a subagent marker is active is held to the subagent rules. Every other chat
gets no tempo enforcement. A payload that does not parse is always denied.

`handle` loads the state, applies the side effects (the begin_session claim, the subagent marker, and
`stop.txt`), and renders the host's reply.
"""

import json
import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tempo.call import open_calls
from tempo.checkout import SPECS_DIR
from tempo.config import load_config
from tempo.hosts import HookEvent, parse, reply
from tempo.journal import Journal, render_ts
from tempo.lockfile import Sessions
from tempo.seams import Clock, FileSystem

HOOKS_DIR = "hooks"
CLAIM = "claim.json"
ACTIVE = "active.json"
AGENT_CLIS = frozenset({"claude", "codex", "copilot", "gemini", "aider", "cursor-agent", "opencode", "amp"})
DENIED_BINARIES = frozenset({"git", "gh", "tempo"}) | AGENT_CLIS
PUBLISH = frozenset({("npm", "publish"), ("pnpm", "publish"), ("yarn", "publish"), ("uv", "publish"),
                     ("twine", "upload"), ("cargo", "publish"), ("poetry", "publish"), ("flit", "publish"),
                     ("hatch", "publish"), ("gem", "push")})
READ_COMMANDS = frozenset({"cat", "sed", "head", "tail", "rg", "grep"})
# Options that take a value, per read command; any other option is a flag.
_VALUED = {"head": {"-n", "-c"}, "tail": {"-n", "-c"}, "grep": {"-e", "-m", "-A", "-B", "-C"},
           "rg": {"-e", "-m", "-A", "-B", "-C", "-g", "--glob", "-t", "--type", "-T", "--type-not", "-M"},
           "sed": set(), "cat": set()}
_SED_FLAGS = {"-n", "-E", "-r", "--quiet", "--silent"}
_OPERATORS = set("|;&<>()\n")


@dataclass(frozen=True)
class HookState:
    session: dict[str, Any] | None  # the orchestrator session record
    call: dict[str, Any] | None  # the session's open call (call.json), at most one
    active: dict[str, Any] | None  # the running subagent's marker, while its call is open
    specs: Path = SPECS_DIR


Decision = tuple[bool | None, str]
ALLOW: Decision = (True, "tempo allows this")


def _deny(reason: str) -> Decision:
    return False, reason


# --- paths and commands --------------------------------------------------------------------------


def _resolve(path: str, cwd: str) -> Path:
    return Path(os.path.realpath(Path(cwd) / path))


def _under(path: Path, roots: list[Path]) -> bool:
    return any(path == r or path.is_relative_to(r) for r in roots)


def _roots(call: dict[str, Any], state: HookState) -> list[Path]:
    return [Path(os.path.realpath(p)) for p in (call["read_root"], state.specs, Path(call["reply"]).parent)]


def _writable(path: Path, call: dict[str, Any]) -> bool:
    if path == Path(os.path.realpath(call["reply"])):
        return True
    if call["surface"] != "implement":
        return False
    worktree = Path(os.path.realpath(call["read_root"]))
    ticket = worktree / "tickets" / call["ticket"] / "ticket.md"
    return path.is_relative_to(worktree) and path != ticket and ".git" not in path.relative_to(worktree).parts


def unquoted(command: str) -> str | None:
    """The reason a command is not one plain argv: an operator outside quotes, or `$`/backtick outside
    single quotes. None when it is plain."""
    quote = None
    for char in command:
        if quote == "'":
            quote = None if char == "'" else quote
            continue
        if char in "$`":
            return "command substitution and variables are not allowed"
        if quote == '"':
            quote = None if char == '"' else quote
        elif char in "'\"":
            quote = char
        elif char in _OPERATORS:
            return f"shell operator {char!r} is not allowed"
    return "unbalanced quotes" if quote else None


def _argv(command: str) -> list[str] | str:
    if (reason := unquoted(command)) is not None:
        return reason
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return f"command does not parse: {exc}"
    return argv or "empty command"


def tempo_tool(command: str) -> list[str] | None:
    argv = _argv(command)
    if isinstance(argv, str) or len(argv) < 3 or Path(argv[0]).name != "tempo" or argv[1] != "tool":
        return None
    return argv


def read_shell(command: str, cwd: str, roots: list[Path]) -> str | None:
    """None when `command` is one plain read (cat, sed -n, head, tail, rg, grep) under `roots`."""
    argv = _argv(command)
    if isinstance(argv, str):
        return argv
    name = argv[0]
    if name not in READ_COMMANDS:
        return f"`{name}` is not a read command; use cat, sed -n, head, tail, rg, or grep"
    operands, flags, explicit_pattern = [], [], False
    tokens = iter(argv[1:])
    for token in tokens:
        if token == "--":
            operands.extend(tokens)
            break
        if token.startswith("-") and token != "-":
            option = token.split("=", 1)[0]
            if name == "rg" and option in ("--pre", "--pre-glob"):
                return "rg --pre runs a command"
            if name in ("grep", "rg") and option in ("-f", "--file"):
                return "pattern files are not allowed"
            if name == "sed" and token not in _SED_FLAGS:
                return f"sed option {token} is not allowed; use sed -n"
            if option in ("-e", "--regexp"):
                explicit_pattern = True
            if token in _VALUED[name] and "=" not in token:
                next(tokens, None)
            flags.append(token)
            continue
        operands.append(token)
    if name == "sed":
        if not {"-n", "--quiet", "--silent"} & set(flags) or not operands:
            return "sed must be sed -n with a script"
        script, operands = operands[0], operands[1:]
        if any(c in script for c in "wWe"):
            return "sed scripts with w, W, or e are not allowed"
    if name in ("grep", "rg") and not explicit_pattern:
        if not operands:
            return f"{name} needs a pattern"
        operands = operands[1:]
    paths = operands or ([cwd] if name in ("grep", "rg") else [])
    if outside := [p for p in paths if not _under(_resolve(p, cwd), roots)]:
        return f"reads outside the call's read roots: {', '.join(outside)}"
    return None


def implement_shell(command: str, cwd: str, worktree: Path) -> str | None:
    """None when `command` runs inside `worktree` and names no git, agent CLI, gh, tempo, or publish."""
    if "$(" in command or "`" in command:
        return "command substitution is not allowed"
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError as exc:
        return f"command does not parse: {exc}"
    words = [w for t in tokens for w in t.split()]
    names = [Path(w).name for w in words]
    if denied := sorted(DENIED_BINARIES & set(names)):
        return f"the implement subagent may not run {', '.join(denied)}; tempo commits your work"
    if any(pair in PUBLISH for pair in zip(names, names[1:])):
        return "publishing is not allowed"
    root = Path(os.path.realpath(worktree))
    here = Path(os.path.realpath(cwd))
    for i, token in enumerate(tokens):
        if token == "cd":
            if i + 1 >= len(tokens):
                return "cd needs a directory inside the worktree"
            target = _resolve(tokens[i + 1], str(here))
            if not _under(target, [root]):
                return f"cd outside the worktree: {tokens[i + 1]}"
            if i == 0:
                here = target
    if not _under(here, [root]):
        return f"run commands inside the worktree: start with `cd {root} &&`"
    return None


# --- policy --------------------------------------------------------------------------------------


def _role(event: HookEvent, state: HookState) -> str | None:
    if event.agent_type is not None and event.agent_type.startswith("tempo-"):
        return "subagent"
    if event.host == "codex" and state.active is not None and state.active["host"] == "codex":
        return "subagent"
    if state.session is not None and event.chat == state.session["chat"]:
        return "orchestrator" if event.agent_type is None else "foreign"
    return None


def _subagent(event: HookEvent, state: HookState) -> Decision:
    call, tool = state.call, event.tool
    assert tool is not None
    if call is None:
        return _deny("no tempo call is open for this subagent")
    if event.agent_type is not None and event.agent_type != call["agent"]:
        return _deny(f"the open call runs {call['agent']}, not {event.agent_type}")
    roots = _roots(call, state)
    if tool.kind in ("read", "search"):
        paths = tool.paths or ((event.cwd,) if tool.kind == "search" else ())
        if outside := [p for p in paths if not _under(_resolve(p, event.cwd), roots)]:
            return _deny(f"read outside the call's read root {call['read_root']}: {', '.join(outside)}")
        return ALLOW
    if tool.kind == "write":
        if not tool.paths:
            return _deny("a write must name its file")
        if bad := [p for p in tool.paths if not _writable(_resolve(p, event.cwd), call)]:
            return _deny(f"write only {call['reply']}" + (" and files in the worktree" if call["surface"] == "implement"
                                                          else "") + f"; denied: {', '.join(bad)}")
        return ALLOW
    if tool.kind == "shell":
        assert tool.command is not None
        if call["surface"] == "implement":
            reason = implement_shell(tool.command, event.cwd, Path(call["read_root"]))
        else:
            reason = read_shell(tool.command, event.cwd, roots)
        return _deny(reason) if reason else ALLOW
    if tool.kind == "noop":
        return ALLOW
    if tool.kind in ("wait", "stop") and event.agent_type is None:
        return ALLOW  # Codex: the orchestrator's wait and close_agent during the subagent window
    return _deny(f"a tempo subagent may not use {tool.name}: no spawn, message, resume, or other tools")


def _orchestrator(event: HookEvent, state: HookState) -> Decision:
    tool = event.tool
    assert tool is not None
    if tool.kind in ("read", "search", "noop", "wait", "stop"):
        return ALLOW
    if tool.kind == "shell":
        assert tool.command is not None
        if tempo_tool(tool.command) is None:
            return _deny("the orchestrator runs only `tempo tool <name> '<json>'` commands")
        return ALLOW
    if tool.kind == "spawn":
        call = state.call
        if call is None:
            return _deny("no call is open; run `tempo tool prepare` first")
        if state.active is not None:
            return _deny(f"call {call['call_id']} already has a running subagent; wait for it, then submit")
        if tool.agent != call["agent"]:
            return _deny(f"spawn {call['agent']}, the agent prepare named, not {tool.agent}")
        if (tool.prompt or "").strip() != call["spawn_prompt"]:
            return _deny("the spawn prompt must be exactly the spawn_prompt that prepare returned")
        return ALLOW
    return _deny(f"the orchestrator may not use {tool.name}; every action is a tempo tool")


def decide(event: HookEvent, state: HookState) -> Decision:
    role = _role(event, state)
    if event.event != "pre_tool" or role is None:
        return None, ""
    if role == "foreign":
        return _deny("only tempo-* subagents run under the tempo orchestrator")
    return _subagent(event, state) if role == "subagent" else _orchestrator(event, state)


# --- handler -------------------------------------------------------------------------------------


def find_repo(*starts: str | Path) -> Path | None:
    """The tempo repo: the nearest folder with config.yaml and a .git directory (a worktree has a .git file)."""
    for start in starts:
        for folder in (Path(start), *Path(start).parents):
            if (folder / "config.yaml").is_file() and (folder / ".git").is_dir():
                return folder
    return None


def _load(repo: Path, clock: Clock, fs: FileSystem) -> tuple[HookState, Path]:
    config = load_config(None, cwd=repo)
    session = Sessions(config.state_dir, clock=clock, fs=fs).read()
    calls = [c for c in open_calls(Journal(config.state_dir, clock).read())
             if session is not None and c["session"] == session["token"]]
    call = calls[0] if calls else None
    hooks = Path(config.state_dir) / HOOKS_DIR
    marker = json.loads((hooks / ACTIVE).read_text()) if (hooks / ACTIVE).is_file() else None
    active = marker if call is not None and marker is not None and marker["call_id"] == call["call_id"] else None
    return HookState(session=session, call=call, active=active), hooks


def _write(fs: FileSystem, path: Path, record: dict[str, Any]) -> None:
    fs.write(path, (json.dumps(record, indent=2) + "\n").encode())


def handle(host: str, data: bytes, *, cwd: Path, clock: Clock, fs: FileSystem,
           load: Callable[[Path, Clock, FileSystem], tuple[HookState, Path]] = _load) -> tuple[str, int]:
    """Run one hook: (stdout, exit code). Exit 2 denies on every host; it carries every parse failure."""
    event = parse(host, data)
    if event is None:
        return reply(False, "tempo denies a hook payload it cannot parse"), 2
    repo = find_repo(cwd, event.cwd)
    if repo is None:
        return reply(None, ""), 0
    state, hooks = load(repo, clock, fs)
    tool = event.tool
    if (event.event == "pre_tool" and tool is not None and tool.kind == "shell" and event.agent_type is None
            and (argv := tempo_tool(tool.command or "")) is not None and argv[2] == "begin_session"
            and _role(event, state) in (None, "orchestrator")):
        _write(fs, hooks / CLAIM, {"host": host, "chat": event.chat, "at": render_ts(clock())})
    call = state.call
    subagent = event.agent_type is not None and event.agent_type.startswith("tempo-")
    if event.event == "subagent_start" and call is not None and (subagent or host == "codex"):
        _write(fs, hooks / ACTIVE, {"host": host, "agent_id": event.agent_id, "call_id": call["call_id"]})
    if event.event == "subagent_stop" and call is not None and (subagent or state.active is not None):
        call_dir = Path(call["reply"]).parent
        _write(fs, call_dir / "stop.txt", {"stopped_at": render_ts(clock()), "message": event.message})
        if event.model is not None:
            record = json.loads((call_dir / "call.json").read_text())
            _write(fs, call_dir / "call.json", {**record, "host_reported_model": event.model})
        (hooks / ACTIVE).unlink(missing_ok=True)
    allow, reason = decide(event, state)
    return reply(allow, reason), 0
