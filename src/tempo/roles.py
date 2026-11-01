"""Canonical role definitions: the single source the host layouts and the hook policy both read."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from importlib.resources import files

import yaml

PREFIX = "tempo-"
TOOL_CLASSES = frozenset({"read", "search", "write", "spawn", "tempo"})
# AGENT_PLAN.md section 4: the orchestrator's MCP tool surface.
TEMPO_TOOLS = (
    "status", "list_eligible", "read_ticket", "read_harvest", "read_journal_window",
    "prepare_inputs", "accept", "implement", "run_checks", "merge", "park", "reject",
    "split", "file_box", "escalate_tier", "notify",
)
_KEYS = {"role", "description", "tools", "writes", "creates", "spawns", "reply"}
_FRONT = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)


@dataclass(frozen=True)
class Role:
    id: str
    description: str
    tools: tuple[str, ...]
    writes: tuple[str, ...]
    creates: tuple[str, ...]
    spawns: tuple[str, ...]
    reply: str | None
    body: str

    @property
    def agent_name(self) -> str:
        return PREFIX + self.id.replace("_", "-")

    @property
    def subagent(self) -> bool:
        return self.reply is not None


def parse(text: str) -> Role:
    match = _FRONT.match(text)
    if not match:
        raise ValueError("role file needs a --- frontmatter block")
    meta = yaml.safe_load(match.group(1))
    unknown = set(meta) - _KEYS
    if unknown:
        raise ValueError(f"unknown role keys: {sorted(unknown)}")
    tools = tuple(meta["tools"])
    if not set(tools) <= TOOL_CLASSES:
        raise ValueError(f"unknown tool classes: {sorted(set(tools) - TOOL_CLASSES)}")
    return Role(
        id=meta["role"],
        description=meta["description"],
        tools=tools,
        writes=tuple(meta.get("writes", ())),
        creates=tuple(meta.get("creates", ())),
        spawns=tuple(meta.get("spawns", ())),
        reply=meta.get("reply"),
        body=match.group(2).strip() + "\n",
    )


@cache
def roles() -> dict[str, Role]:
    found = {}
    for entry in sorted(files("tempo").joinpath("agents").iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".md"):
            role = parse(entry.read_text(encoding="utf-8"))
            if entry.name != f"{role.id}.md":
                raise ValueError(f"{entry.name} declares role {role.id}")
            found[role.id] = role
    for role in found.values():
        missing = set(role.spawns) - found.keys()
        if missing:
            raise ValueError(f"{role.id} spawns unknown roles {sorted(missing)}")
        if role.subagent and role.spawns:
            raise ValueError(f"subagent {role.id} may not spawn")
        if role.subagent and "spawn" in role.tools:
            raise ValueError(f"subagent {role.id} may not hold the spawn tool")
    return found


def by_agent_name(name: str | None) -> Role | None:
    if not name:
        return None
    bare = name.rsplit(":", 1)[-1]  # plugin-scoped names arrive as "<plugin>:<agent>"
    return next((r for r in roles().values() if r.agent_name == bare), None)


def protocol() -> str:
    return files("tempo").joinpath("rules", "protocol.md").read_text(encoding="utf-8")
