"""`tempo render`: the host files generated from config.yaml (AGENT_PLAN.md sections 4.4.2, 5).

Hook files and the `tempo:protocol` and `tempo:session` blocks are written for every host. A hook file
keeps every key but the ones tempo owns (`hooks`, plus `version` on Copilot). Model-bearing files (the
orchestrator rules, the start command, and one agent file per reachable surface and model, plus effort
on Codex) are written only for reachable hosts, those with an `orchestrator.models` entry. Stale
generated `tempo-*` files are deleted. `drift` is the one staleness check, for `render --check`,
`doctor`, and `begin_session`. `init` sets a repo up from nothing (AGENT_PLAN.md section 6).
"""

import json
from pathlib import Path
from string import Template

from tempo import hostfiles
from tempo.call import CODEX_EFFORT, SURFACES, agent_name, resolve_model
from tempo.caps import LEVELS
from tempo.checkout import Refusal
from tempo.config import Config, ConfigSnapshot, load_config
from tempo.hosts import HOSTS, LAYOUTS, hook_config
from tempo.providers import WRITING_SURFACES
from tempo.seams import FileSystem

RULES = Path(__file__).resolve().parent / "rules"
CONFIG = "config.yaml"
TICKETS_DOC = "docs/tempo/tickets.md"
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
    """Each host's instruction file with its `tempo:protocol` and `tempo:session` blocks rendered in."""
    out = {}
    for host in HOSTS:
        rel = LAYOUTS[host].instructions
        path = repo / rel
        data = path.read_bytes() if path.is_file() else b""
        for name in ("session", "protocol"):
            data = hostfiles.render(data, template=(RULES / f"{name}.md").read_bytes(), name=name)
        out[rel] = data
    return out


def _hooks(repo: Path) -> dict[str, bytes | None]:
    """Each host's hook file with tempo's keys merged in; None when the file is not a JSON object."""
    out = {}
    for host in HOSTS:
        rel = LAYOUTS[host].hooks
        path = repo / rel
        current = {}
        if path.is_file():
            try:
                current = json.loads(path.read_bytes())
            except ValueError:
                current = None
        out[rel] = (None if not isinstance(current, dict)
                    else (json.dumps({**current, **hook_config(host)}, indent=2) + "\n").encode())
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


def _expected(repo: Path, config: Config | ConfigSnapshot) -> dict[str, bytes | None]:
    return {**{rel: text.encode() for rel, text in files(config).items()}, **_instructions(repo), **_hooks(repo)}


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
    if invalid := [rel for rel, data in expected.items() if data is None]:
        raise Refusal(f"{invalid[0]} is not a JSON object",
                      f"repair {invalid[0]} to a JSON object, or delete it, then rerun `tempo render`")
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


def init(repo: Path, config_path: Path | None, fs: FileSystem) -> list[str]:
    """Write a default config if there is none, then `tempo core`, `render`, and the ticket contract doc.

    Returns one report line per file written or skipped.
    """
    lines = []
    path = repo / (config_path or CONFIG)
    if not path.exists():
        fs.write(path, (RULES / CONFIG).read_bytes())
        lines.append(f"wrote: {config_path or CONFIG}")
    config = load_config(path, cwd=repo)
    conduct = hostfiles.conduct_paths(config, repo)
    before = {p: p.read_bytes() if p.is_file() else None for p in conduct}
    hostfiles.refresh(conduct, fs)
    touched = {p.relative_to(repo).as_posix() for p in conduct if before[p] != p.read_bytes()}
    touched |= set(write(repo, config, fs))
    lines += [f"wrote: {rel}" for rel in sorted(touched)]
    doc, shipped = repo / TICKETS_DOC, (RULES / "tickets.md").read_bytes()
    if not doc.exists():
        fs.write(doc, shipped)
        lines.append(f"wrote: {TICKETS_DOC}")
    elif doc.read_bytes() != shipped:
        lines.append(f"skipped: {TICKETS_DOC} differs from the shipped copy; merge it by hand")
    return lines
