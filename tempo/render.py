"""`tempo render`: the host files generated from config.yaml (AGENT_PLAN.md sections 4.4.2, 5).

Hook files and `tempo:protocol` blocks are written for every host. Model-bearing files (the
orchestrator rules, the start command, and one agent file per reachable surface and model, plus effort
on Codex) are written only for reachable hosts, those with an `orchestrator.models` entry. Stale
generated `tempo-*` files are deleted. `drift` is the one staleness check, for `render --check`,
`doctor`, and `begin_session`.
"""

import json
from pathlib import Path
from string import Template

from tempo import hostfiles
from tempo.call import CODEX_EFFORT, SURFACES, agent_name, resolve_model
from tempo.caps import LEVELS
from tempo.config import Config, ConfigSnapshot
from tempo.hosts import HOSTS, LAYOUTS, hook_config
from tempo.providers import WRITING_SURFACES
from tempo.seams import FileSystem

RULES = Path(__file__).resolve().parent / "rules"
ORCHESTRATOR = "tempo-orchestrator"
_TOOLS = {
    "claude": ("Read, Grep, Glob, Write", "Read, Grep, Glob, Write, Edit, MultiEdit, Bash"),
    "copilot": ('["read", "search", "edit"]', '["read", "search", "edit", "runCommands"]'),
}
_WRITES = ("Read only: the reply file is the one file you write.",
           "Edit files only inside the read root, your ticket worktree, and never its"
           " `tickets/<stem>/ticket.md`. Run build and test commands with their working directory inside"
           " the worktree. Tempo commits your changes.")


def _rule(name: str, **values: str) -> str:
    return Template((RULES / name).read_text()).substitute(values)


def _front(fields: dict[str, str]) -> str:
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in fields.items()) + "---\n\n"


def reachable(config: Config | ConfigSnapshot) -> tuple[str, ...]:
    return tuple(h for h in HOSTS if h in config.orchestrator.models)


def _agent_file(host: str, name: str, surface: str, model: str, effort: str) -> str:
    writes = surface in WRITING_SURFACES
    body = _rule("agent.md", surface=surface, writes=_WRITES[writes])
    description = f"tempo {surface} subagent. Spawned only by the tempo orchestrator."
    if host == "codex":
        return "".join(f"{k} = {json.dumps(v)}\n" for k, v in (
            ("name", name), ("description", description), ("model", model),
            ("model_reasoning_effort", effort), ("developer_instructions", body)))
    return _front({"name": name, "description": description, "model": model,
                   "tools": _TOOLS[host][writes]}) + body


def files(config: Config | ConfigSnapshot) -> dict[str, str]:
    """Every whole generated file, by repo-relative path."""
    out = {}
    for host in HOSTS:
        out[LAYOUTS[host].hooks] = json.dumps(hook_config(host), indent=2) + "\n"
    for host in reachable(config):
        layout = LAYOUTS[host]
        model = config.orchestrator.models[host]
        rules = _rule("orchestrator.md")
        out[layout.orchestrator] = _front({"name": ORCHESTRATOR, "description": "Runs tempo tickets.",
                                           "model": model}) + rules
        start = {"description": "Run tempo in this chat.", "model": model}
        if host == "copilot":
            start["agent"] = ORCHESTRATOR
        if host == "codex":
            start = {"name": "tempo-run", "description": start["description"]}  # a skill names no model
        out[layout.start] = _front(start) + _rule("tempo-run.md", rules=layout.orchestrator)
        for surface in SURFACES:
            for tier in LEVELS:
                for effort in (LEVELS if host == "codex" else ("medium",)):
                    resolved = resolve_model(config, host, surface, tier)
                    name = agent_name(host, surface, resolved, effort)
                    rel = f"{layout.agents}/{name}{layout.agent_suffix}"
                    if rel not in out:
                        out[rel] = _agent_file(host, name, surface, resolved, CODEX_EFFORT[effort])
    return out


def _instructions(repo: Path) -> dict[str, bytes]:
    """Each host's instruction file with its `tempo:protocol` block rendered in."""
    template = (RULES / "protocol.md").read_bytes()
    out = {}
    for host in HOSTS:
        rel = LAYOUTS[host].instructions
        path = repo / rel
        current = path.read_bytes() if path.is_file() else b""
        out[rel] = hostfiles.render(current, template=template, name="protocol")
    return out


def _stale(repo: Path, expected: dict[str, bytes]) -> list[str]:
    found = set()
    for host in HOSTS:
        layout = LAYOUTS[host]
        agents = repo / layout.agents
        if agents.is_dir():
            found |= {p.relative_to(repo).as_posix() for p in agents.glob(f"tempo-*{layout.agent_suffix}")}
        found |= {rel for rel in (layout.orchestrator, layout.start) if (repo / rel).is_file()}
    return sorted(found - set(expected))


def _expected(repo: Path, config: Config | ConfigSnapshot) -> dict[str, bytes]:
    return {**{rel: text.encode() for rel, text in files(config).items()}, **_instructions(repo)}


def drift(repo: Path, config: Config | ConfigSnapshot, only: set[str] | None = None) -> list[str]:
    """Paths whose bytes differ from a fresh render, or stale ones; empty when current."""
    expected = _expected(repo, config)
    changed = [rel for rel, data in expected.items()
               if not (repo / rel).is_file() or (repo / rel).read_bytes() != data]
    out = sorted(changed + _stale(repo, expected))
    return [rel for rel in out if only is None or rel in only]


def write(repo: Path, config: Config | ConfigSnapshot, fs: FileSystem) -> list[str]:
    """Write every changed generated file and delete stale ones; returns the paths touched."""
    expected = _expected(repo, config)
    stale = _stale(repo, expected)
    touched = []
    for rel, data in expected.items():
        path = repo / rel
        if not path.is_file() or path.read_bytes() != data:
            fs.write(path, data)
            touched.append(rel)
    for rel in stale:
        (repo / rel).unlink()
    return sorted(touched + stale)
