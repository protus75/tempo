"""Platform-neutral verdicts for every governed agent call. Hooks normalize; this module decides.

Every refusal names its paved road (what to do instead).
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from tempo import contracts
from tempo.roles import Role, roles

OUTBOX = ".tempo/outbox"
STATE = ".tempo/state"
SHELL_META = set(";&|<>$`(){}\n\r")
READ_ONLY_COMMANDS = {"cat", "head", "tail", "wc", "ls", "rg", "grep", "nl"}
READ_ONLY_GIT = {"diff", "show", "log", "status", "ls-files", "grep", "blame", "rev-parse"}
# Flags that make an otherwise read-only command execute or write.
UNSAFE_FLAG_PREFIXES = ("--pre", "--output", "--ext-diff", "--textconv", "--open-files-in-pager")


@dataclass(frozen=True)
class Caller:
    """Who is calling. `role` is a tempo role id, "developer", "foreign", or "ambiguous"."""

    role: str
    subagent: bool

    @property
    def tempo_role(self) -> Role | None:
        return roles().get(self.role)


@dataclass(frozen=True)
class Call:
    tool: str
    kind: str  # read | search | write | spawn | spawn_wait | spawn_message | shell | tempo | other
    paths: tuple[str, ...] = ()
    command: str | None = None
    spawn_target: str | None = None
    spawn_prompt: str | None = None


@dataclass(frozen=True)
class Verdict:
    allow: bool
    reason: str = ""
    record: dict = field(default_factory=dict)  # side data for the adapter, e.g. a pending manifest


ALLOW = Verdict(True)


def deny(reason: str) -> Verdict:
    return Verdict(False, reason)


def relative(path: str, root: Path) -> str | None:
    """Repo-relative POSIX path, or None if it escapes the root."""
    p = Path(path)
    p = p if p.is_absolute() else root / p
    try:
        rel = p.resolve(strict=False).relative_to(root.resolve())
    except ValueError:
        return None
    return rel.as_posix()


def matches(rel: str, pattern: str) -> bool:
    """Segment-wise glob: `*` never crosses a `/`."""
    a, b = PurePosixPath(rel).parts, PurePosixPath(pattern).parts
    return len(a) == len(b) and all(fnmatchcase(x, y) for x, y in zip(a, b))


def decide(caller: Caller, call: Call, root: Path, env: dict[str, str]) -> Verdict:
    if call.kind in ("spawn", "spawn_message") and caller.subagent:
        return deny("tempo allows one level of subagents: a subagent never spawns or messages agents. "
                    "Write what you found to your reply file and stop; the orchestrator decides what runs next.")
    if caller.role == "ambiguous":
        if call.kind in ("read", "search"):
            return ALLOW
        return deny("tempo cannot tell which subagent is calling (more than one is active). "
                    "Keep at most one subagent thread open; set agents.max_concurrent_threads_per_session = 1.")
    role = caller.tempo_role
    if caller.role == "implement":
        return _implement(call, root, env)
    if role is None:
        return _developer(caller, call, root)
    if role.subagent:
        return _subagent(role, call, root)
    return _orchestrator(role, call, root)


def _developer(caller: Caller, call: Call, root: Path) -> Verdict:
    # Ungoverned sessions keep their tools; only spawning a tempo subagent is constrained.
    if call.kind == "spawn":
        target = _target(call)
        if target is not None:
            return _spawn(target, call, root)
    return ALLOW


def _subagent(role: Role, call: Call, root: Path) -> Verdict:
    if call.kind in ("read", "search"):
        return ALLOW
    if call.kind == "write":
        return _write(role, call, root)
    if call.kind == "shell":
        return _read_only_shell(call.command)
    return deny(f"{role.agent_name} may only read, search, and write its reply file "
                f"({role.writes[0]}); '{call.tool}' is outside that set.")


def _orchestrator(role: Role, call: Call, root: Path) -> Verdict:
    if call.kind in ("read", "search", "tempo", "spawn_wait"):
        return ALLOW
    if call.kind == "write":
        return _write(role, call, root)
    if call.kind == "spawn":
        target = _target(call)
        if target is None or target.id not in role.spawns:
            names = ", ".join(roles()[r].agent_name for r in role.spawns)
            return deny(f"the orchestrator spawns only {names}.")
        return _spawn(target, call, root)
    if call.kind == "spawn_message":
        return deny("never send prose to a running subagent; spawn a fresh one with the next attempt's manifest.")
    if call.kind == "shell":
        return deny("the orchestrator runs no shell commands; use the tempo tool that owns the action "
                    "(run_checks, implement, merge, status, ...).")
    return deny(f"'{call.tool}' is not in the orchestrator's tool surface; use a tempo tool.")


def _implement(call: Call, root: Path, env: dict[str, str]) -> Verdict:
    if call.kind in ("spawn", "spawn_message"):
        return deny("implement runs with multi-agent disabled; do the work in this session.")
    if call.kind != "write":
        return ALLOW
    try:
        fence = json.loads(env.get("TEMPO_FENCE", ""))
    except json.JSONDecodeError:
        fence = None
    if not isinstance(fence, list) or not fence:
        return deny("implement has no scope fence (TEMPO_FENCE unset); only the implement tool starts this session.")
    for raw in call.paths or ("",):
        rel = relative(raw, root) if raw else None
        if rel is None or rel.startswith(("tickets/", ".tempo/")) or not any(
            rel == p.rstrip("/") or rel.startswith(p.rstrip("/") + "/") for p in fence
        ):
            return deny(f"'{raw}' is outside this ticket's scope fence {fence}; file the second problem "
                        "in the Suggestion Box instead.")
    return ALLOW


def _target(call: Call) -> Role | None:
    from tempo.roles import by_agent_name

    role = by_agent_name(call.spawn_target)
    return role if role is not None and role.subagent else None


def _spawn(target: Role, call: Call, root: Path) -> Verdict:
    road = ('the spawn prompt is exactly the manifest from prepare_inputs: '
            '{"subject": "...", "attempt": n, "inputs": ["<repo-relative path>", ...]}')
    prompt = (call.spawn_prompt or "").strip()
    if len(prompt) > contracts.MAX_PROMPT_CHARS:
        return deny(f"spawn prompt exceeds {contracts.MAX_PROMPT_CHARS} chars: content is never pasted; {road}.")
    try:
        manifest = contracts.Manifest.model_validate_json(prompt)
    except ValidationError as err:
        return deny(f"spawn prompt is not a manifest ({err.error_count()} errors); {road}.")
    for raw in manifest.inputs:
        rel = relative(raw, root)
        if rel is None or not (root / rel).exists():
            return deny(f"manifest input '{raw}' does not exist inside the repository; run prepare_inputs first.")
        if rel.startswith((OUTBOX + "/", STATE + "/")) or rel in (OUTBOX, STATE):
            return deny(f"manifest input '{raw}' points into {OUTBOX} or {STATE}; "
                        "call accept first and pass the accepted file under tickets/.")
    reply = root / OUTBOX / target.id / manifest.subject / str(manifest.attempt) / "reply.json"
    if reply.exists():
        return deny(f"{reply.relative_to(root)} already exists; spawn with attempt {manifest.attempt + 1}.")
    return Verdict(True, record={"role": target.id, "subject": manifest.subject, "attempt": manifest.attempt})


def _write(role: Role, call: Call, root: Path) -> Verdict:
    allowed = ", ".join(role.writes + role.creates)
    if not call.paths:
        return deny(f"could not read the target path of '{call.tool}'; {role.agent_name} writes only {allowed}.")
    for raw in call.paths:
        rel = relative(raw, root)
        if rel is None:
            return deny(f"'{raw}' is outside the repository; {role.agent_name} writes only {allowed}.")
        if any(matches(rel, p) for p in role.writes):
            continue
        if any(matches(rel, p) for p in role.creates):
            if (root / rel).exists():
                return deny(f"'{rel}' already exists; {role.agent_name} creates new ticket files only and "
                            "never edits an existing ticket.md. Record the change in decisions.md or split.")
            continue
        return deny(f"'{rel}' is not writable by {role.agent_name}; it writes only {allowed}.")
    return ALLOW


def _read_only_shell(command: str | None) -> Verdict:
    road = (f"subagents run only read-only commands ({', '.join(sorted(READ_ONLY_COMMANDS))}, "
            f"git {'/'.join(sorted(READ_ONLY_GIT))}) with no pipes, redirection, or substitution")
    if not command or SHELL_META & set(command):
        return deny(f"command refused; {road}.")
    try:
        argv = shlex.split(command)
    except ValueError:
        return deny(f"command refused; {road}.")
    if not argv or any(a.startswith(UNSAFE_FLAG_PREFIXES) for a in argv[1:]):
        return deny(f"command refused; {road}.")
    if argv[0] == "git":
        if len(argv) < 2 or argv[1] not in READ_ONLY_GIT:
            return deny(f"git subcommand refused; {road}.")
        return ALLOW
    if argv[0] not in READ_ONLY_COMMANDS:
        return deny(f"'{argv[0]}' refused; {road}.")
    return ALLOW


def check_reply(role: Role, text: str | None, root: Path, pending: set[tuple[str, int]]) -> Verdict:
    """A subagent's final message: bare `{written, status}` JSON whose files validate."""
    road = 'reply with only {"written": ["<reply.json path>"], "status": "ok"} -- no prose, no code fence'
    try:
        reply = contracts.Reply.model_validate_json((text or "").strip())
    except ValidationError:
        return deny(f"final reply is not the reply contract; {road}.")
    for raw in reply.written:
        rel = relative(raw, root)
        if rel is None or not any(matches(rel, p) for p in role.writes):
            return deny(f"'{raw}' is not {role.agent_name}'s reply file ({role.writes[0]}).")
        parts = PurePosixPath(rel).parts
        subject, attempt = parts[3], parts[4]
        if not attempt.isdigit() or (subject, int(attempt)) not in pending:
            return deny(f"'{rel}' does not match the manifest you were spawned with; write to "
                        f"{OUTBOX}/{role.id}/<subject>/<attempt>/reply.json using its subject and attempt.")
        path = root / rel
        if not path.is_file():
            return deny(f"'{rel}' was listed but not written; write it, then reply.")
        try:
            body = contracts.REPLIES[role.reply].model_validate_json(path.read_text(encoding="utf-8"))
        except ValidationError as err:
            first = err.errors()[0]
            where = ".".join(str(x) for x in first["loc"]) or "<root>"
            return deny(f"'{rel}' fails the {role.id} schema at {where}: {first['msg']}. Fix the file, then reply.")
        if isinstance(body, contracts.RetroReply):
            unknown = {r for p in body.proposals for r in p.proposed_roles} - roles().keys()
            if unknown:
                return deny(f"'{rel}' proposes unknown roles {sorted(unknown)}; use ids from {sorted(roles())}.")
    return Verdict(True, record={"written": reply.written, "status": reply.status})
