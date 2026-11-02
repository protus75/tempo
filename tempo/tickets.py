"""Ticket contract and runner intake (TEMPO_PLAN.md section 13).

Intake on the CLI run path: at invocation it lints every pending hand-authored
`tickets/<stem>/ticket.md` and ticket-plane-commits the clean ones, one commit per stem.
"""

import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator

from tempo.artifacts import Finding, NonBlank
from tempo.gates import ENGINE_GATE_CODES
from tempo.git import Git
from tempo.journal import EventType, Journal
from tempo.seams import FileSystem

TICKETS_DIR = "tickets"
TICKET_FILE = "ticket.md"
STEM = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
RESERVED_STEMS = frozenset({"decisions", "retro"})  # sibling dirs that match the stem regex (sections 14, 15)
INTAKE_SIGNAL = "ticket_intake"
CODE = "ticket_schema"

Level = Literal["low", "medium", "high", "max"]
Source = Annotated[str, StringConstraints(pattern=r"^(human|box:[a-z][a-z0-9_]*)$")]


class GateBypass(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    code: NonBlank
    reason: NonBlank

    @field_validator("code")
    @classmethod
    def _known_code(cls, v: str) -> str:
        if v not in ENGINE_GATE_CODES:
            raise ValueError(f"{v!r} is not a gate code; use one of {sorted(ENGINE_GATE_CODES)}")
        return v


class Frontmatter(BaseModel):
    """The closed frontmatter list: only fields the scheduler, a gate, or authoring/triage policy reads."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    state: Literal["draft", "confirmed", "rejected", "merged"]
    source: Source
    priority: Literal["P0", "P1", "P2", "P3"]
    kind: Literal["bug", "feature", "chore"]
    agent_tier: Level = "medium"
    agent_effort: Level = "medium"
    gate_bypass: list[GateBypass] = Field(default_factory=list)


REQUIRED_SECTIONS = (
    "Depends on",
    "Context",
    "Goal / Why",
    "Scope in / Scope out",
    "Scope fence",
    "Acceptance criteria",
    "Verification",
    "Definition of rejected",
    "Time budget",
)
OPTIONAL_SECTIONS = ("On-demand", "Regression")
SECTIONS = REQUIRED_SECTIONS + OPTIONAL_SECTIONS
BANNED_ADJECTIVES = re.compile(r"\b(improved|better|cleaner)\b", re.IGNORECASE)
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_HEADING = re.compile(r"^## (.+?)\s*$")
_ITEM = re.compile(r"^(?:-|\d+\.)\s+(.*)$")
_BUDGET = re.compile(r"^(expected|stuck):\s*(\d+)m$")


@dataclass(frozen=True)
class Ticket:
    stem: str
    frontmatter: Frontmatter
    sections: Mapping[str, str]
    depends: tuple[str, ...]
    context: tuple[str, ...]
    on_demand: tuple[str, ...]
    scope_fence: tuple[str, ...]
    verification: tuple[tuple[str, ...], ...]
    expected_minutes: int
    stuck_minutes: int
    regression: tuple[str, ...] = ()
    carries: tuple[str, ...] = ()


class TicketInvalid(Exception):
    def __init__(self, findings: list[Finding]) -> None:
        super().__init__("; ".join(f.message for f in findings))
        self.findings = findings


class IntakeRefused(Exception):
    """Intake as a whole cannot commit safely; nothing was committed."""

    def __init__(self, finding: Finding) -> None:
        super().__init__(f"{finding.message} -- {finding.paved_road}")
        self.finding = finding


def ticket_path(stem: str) -> str:
    return f"{TICKETS_DIR}/{stem}/{TICKET_FILE}"


def stem_findings(stem: str) -> list[Finding]:
    path = f"{TICKETS_DIR}/{stem}/"
    if not STEM.fullmatch(stem):
        return [_f(f"stem {stem!r} does not match {STEM.pattern}", "rename the ticket dir to lowercase kebab-case,"
                   " 2-64 characters, starting with a letter or digit", path)]
    if stem in RESERVED_STEMS:
        return [_f(f"stem {stem!r} is a reserved tickets/ sibling dir", "pick a stem other than"
                   f" {sorted(RESERVED_STEMS)}", path)]
    return []


def _f(message: str, road: str, path: str | None = None) -> Finding:
    return Finding(code=CODE, path=path, message=message, paved_road=road)


# --- parse --------------------------------------------------------------------------------------


def split_frontmatter(text: str) -> tuple[str, str] | None:
    m = _FRONTMATTER.match(text)
    return (m.group(1), text[m.end():]) if m else None


def _load_frontmatter(raw: str, path: str) -> tuple[dict | None, list[Finding]]:
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return None, [_f(f"frontmatter is not valid YAML: {e}", "fix the YAML between the `---` fences", path)]
    if not isinstance(data, dict):
        return None, [_f("frontmatter is not a YAML mapping", "write `key: value` lines between the `---` fences",
                         path)]
    return data, []


def _sections(body: str, path: str) -> tuple[dict[str, str], list[Finding]]:
    sections: dict[str, list[str]] = {}
    findings = []
    current: list[str] | None = None
    fence = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        if not fence and (m := _HEADING.match(line)):
            name = m.group(1)
            if name not in SECTIONS:
                findings.append(_f(f"unknown body section `## {name}`",
                                   f"use only the section 13 sections: {', '.join(SECTIONS)}", path))
            elif name in sections:
                findings.append(_f(f"duplicate body section `## {name}`", "merge the two into one section", path))
            current = sections.setdefault(name, [])
            continue
        if current is not None:
            current.append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}, findings


def _bullets(text: str, name: str, path: str) -> tuple[list[str], list[Finding]]:
    items, findings = [], []
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line.startswith("- "):
            findings.append(_f(f"`## {name}` line {line.strip()!r} is not a `- ` bullet",
                               f"write `## {name}` as one `- ` bullet per entry", path))
            continue
        items.append(line[2:].strip().strip("`").strip())
    return items, findings


def _fenced_commands(text: str, name: str, path: str) -> tuple[list[tuple[str, ...]], list[str], list[Finding]]:
    """One fenced block of argv-parseable commands; returns (commands, the lines outside the fence, findings)."""
    road = f"put the commands in ONE ``` fenced block under `## {name}`, one argv command per line, no shell syntax"
    lines = text.splitlines()
    opens = [i for i, line in enumerate(lines) if line.strip().startswith("```")]
    if len(opens) != 2:
        return [], [], [_f(f"`## {name}` needs exactly one fenced command block", road, path)]
    commands, findings = [], []
    for line in lines[opens[0] + 1:opens[1]]:
        if not line.strip():
            continue
        try:
            argv = shlex.split(line, posix=True)
        except ValueError as e:
            findings.append(_f(f"`## {name}` command {line!r} is not argv-parseable: {e}", road, path))
            continue
        if not argv or not argv[0].strip():
            findings.append(_f(f"`## {name}` command has no executable", road, path))
            continue
        commands.append(tuple(argv))
    if not commands and not findings:
        findings.append(_f(f"`## {name}` fenced block holds no command", road, path))
    outside = [line for i, line in enumerate(lines) if not opens[0] <= i <= opens[1] and line.strip()]
    return commands, outside, findings


def _repo_path(value: str, name: str, path: str) -> list[Finding]:
    p = Path(value)
    if not value or p.is_absolute() or ".." in p.parts:
        return [_f(f"`## {name}` entry {value!r} is not a repo-relative path",
                   "write a path relative to the repo root, without `..`", path)]
    return []


def _criteria(text: str) -> list[str]:
    items: list[str] = []
    for line in text.splitlines():
        if m := _ITEM.match(line):
            items.append(m.group(1))
        elif line.strip() and items:
            items[-1] += " " + line.strip()
        elif line.strip():
            items.append(line.strip())
    return items


def parse_ticket(stem: str, text: str, repo: Path) -> Ticket:
    """Validate one ticket against the full section 13 grammar; raise TicketInvalid with every finding.

    `repo` resolves `Context` / `On-demand` existence and `Depends on` stems (existing ticket dirs).
    """
    path = ticket_path(stem)
    findings = stem_findings(stem)
    split = split_frontmatter(text)
    if split is None:
        raise TicketInvalid(findings + [_f("ticket.md has no frontmatter",
                                           "open the file with a `---` fenced YAML frontmatter block", path)])
    data, errs = _load_frontmatter(split[0], path)
    if data is None:
        raise TicketInvalid(findings + errs)
    fm = None
    try:
        fm = Frontmatter.model_validate(data)
    except ValidationError as e:
        allowed = ", ".join(Frontmatter.model_fields)
        findings += [
            _f(f"frontmatter `{'.'.join(map(str, err['loc']))}`: {err['msg']}",
               f"frontmatter carries only {allowed}; fix the value to its closed vocabulary (section 13) or"
               " move non-scheduling detail into the body", path)
            for err in e.errors()
        ]

    sections, errs = _sections(split[1], path)
    findings += errs
    for name in REQUIRED_SECTIONS:
        if not sections.get(name):
            findings.append(_f(f"missing or empty body section `## {name}`", f"add a non-empty `## {name}` section",
                               path))

    tickets_root = repo / TICKETS_DIR
    depends: list[str] = []
    if text_ := sections.get("Depends on"):
        items, errs = (["none"], []) if text_.strip() == "none" else _bullets(text_, "Depends on", path)
        findings += errs
        if items != ["none"]:
            for dep in items:
                if dep == stem:
                    findings.append(_f(f"`## Depends on` names the ticket itself ({dep!r})",
                                       "drop the self-edge", path))
                elif not STEM.fullmatch(dep) or not (tickets_root / dep / TICKET_FILE).is_file():
                    findings.append(_f(f"`## Depends on` stem {dep!r} does not resolve to an existing ticket",
                                       f"name an existing `{TICKETS_DIR}/<stem>/` dir, or `none`", path))
                else:
                    depends.append(dep)

    context: list[str] = []
    if text_ := sections.get("Context"):
        items, errs = _bullets(text_, "Context", path)
        findings += errs
        for item in items:
            if errs := _repo_path(item, "Context", path):
                findings += errs
            elif Path(item).parent == Path("specs") and item.endswith(".md"):
                findings.append(_f(f"`## Context` cites the prompt spec {item}; prompt-spec files are never Context",
                                   "drop it from Context; the engine renders its own prompt spec", path))
            elif not (repo / item).exists():
                findings.append(_f(f"`## Context` path {item!r} does not exist",
                                   "Context lists EXISTING read-first files; name a file this ticket creates in"
                                   " `## Scope fence` and `## Verification` only", path))
            else:
                context.append(item)

    fence: list[str] = []
    if text_ := sections.get("Scope fence"):
        items, errs = _bullets(text_, "Scope fence", path)
        findings += errs
        for item in items:
            findings += (errs := _repo_path(item, "Scope fence", path))
            if not errs:
                fence.append(item)

    on_demand: list[str] = []
    if text_ := sections.get("On-demand"):
        items, errs = _bullets(text_, "On-demand", path)
        findings += errs
        for item in items:
            if errs := _repo_path(item, "On-demand", path):
                findings += errs
            elif not (repo / item).is_file():
                findings.append(_f(f"`## On-demand` path {item!r} does not exist",
                                   "list only EXISTING files the implementer reads from the worktree", path))
            elif not any(item == f or item.startswith(f.rstrip("/") + "/") for f in fence):
                findings.append(_f(f"`## On-demand` path {item!r} is not covered by `## Scope fence`",
                                   "fence it, or move a read-only reference to `## Context`", path))
            elif item in context:
                findings.append(_f(f"`## On-demand` path {item!r} is also in `## Context`",
                                   "keep it in exactly one of the two sections", path))
            else:
                on_demand.append(item)

    if text_ := sections.get("Acceptance criteria"):
        if m := BANNED_ADJECTIVES.search(text_):
            findings.append(_f(f"`## Acceptance criteria` uses the unmeasurable adjective {m.group(1)!r}",
                               "state a measurable outcome (\"exits 0 when...\", \"is unchanged\")", path))
        for item in _criteria(text_):
            if "`" not in item:
                findings.append(_f(f"acceptance criterion {item!r} names no check",
                                   "name the `## Verification` command or the observable artifact that checks it,"
                                   " in backticks", path))

    commands: list[tuple[str, ...]] = []
    if text_ := sections.get("Verification"):
        commands, outside, errs = _fenced_commands(text_, "Verification", path)
        findings += errs
        if outside:
            findings.append(_f("`## Verification` has text outside its fenced block",
                               "keep only the fenced command block in `## Verification`", path))

    regression, carries = (), ()
    kind = fm.kind if fm is not None else data.get("kind")
    if "Regression" in sections:
        if kind != "bug":
            findings.append(_f("`## Regression` is present on a non-bug ticket",
                               "drop `## Regression`, or set `kind: bug` if this ticket fixes a defect", path))
        regression, carries, errs = _regression(sections["Regression"], path)
        findings += errs
    elif kind == "bug":
        findings.append(_f("a `kind: bug` ticket has no `## Regression` section",
                           "add `## Regression`: one fenced reproducing command plus `- carries: <path-prefix>`"
                           " bullets", path))

    expected = stuck = 0
    if text_ := sections.get("Time budget"):
        items, errs = _bullets(text_, "Time budget", path)
        findings += errs
        budget = {m.group(1): int(m.group(2)) for item in items if (m := _BUDGET.match(item))}
        if len(items) != 2 or set(budget) != {"expected", "stuck"} or 0 in budget.values():
            findings.append(_f("`## Time budget` is not an expected/stuck minutes pair",
                               "write exactly two bullets, `- expected: <int>m` and `- stuck: <int>m`", path))
        else:
            expected, stuck = budget["expected"], budget["stuck"]
            if stuck <= expected:
                findings.append(_f(f"`## Time budget` stuck: {stuck}m must exceed expected: {expected}m",
                                   "set stuck greater than expected (the stage deadline is the stuck budget)",
                                   path))

    if findings or fm is None:
        raise TicketInvalid(findings)
    return Ticket(stem=stem, frontmatter=fm, sections=sections, depends=tuple(depends), context=tuple(context),
                  on_demand=tuple(on_demand), scope_fence=tuple(fence),
                  verification=tuple(commands), expected_minutes=expected, stuck_minutes=stuck,
                  regression=regression, carries=carries)


def _regression(text: str, path: str) -> tuple[tuple[str, ...], tuple[str, ...], list[Finding]]:
    commands, outside, findings = _fenced_commands(text, "Regression", path)
    if len(commands) > 1:
        findings.append(_f("`## Regression` holds more than one command",
                           "reduce it to the ONE command that reproduces the defect", path))
    carries = [line for line in outside if re.fullmatch(r"- carries: \S+", line.strip())]
    if not carries or len(carries) != len(outside):
        findings.append(_f("`## Regression` needs `- carries: <path-prefix>` bullets and nothing else outside the"
                           " fence", "add one `- carries: <path-prefix>` bullet per branch-added test/fixture path"
                           " the command needs", path))
    prefixes = tuple(line.strip().removeprefix("- carries: ") for line in carries)
    for prefix in prefixes:
        findings += _repo_path(prefix, "Regression carries", path)
    return (commands[0] if len(commands) == 1 else ()), prefixes, findings


def depends_cycle(stem: str, edges: Mapping[str, Sequence[str]]) -> list[str] | None:
    """A dependency path from `stem` back to itself, or None."""
    stack: list[tuple[str, list[str]]] = [(d, [stem, d]) for d in edges.get(stem, ())]
    seen: set[str] = set()
    while stack:
        node, trail = stack.pop()
        if node == stem:
            return trail
        if node in seen:
            continue
        seen.add(node)
        stack += [(d, trail + [d]) for d in edges.get(node, ())]
    return None


def _graph(repo: Path) -> dict[str, list[str]]:
    """Every on-disk ticket's `Depends on` stems, read leniently: a malformed ticket contributes no edges."""
    edges: dict[str, list[str]] = {}
    root = repo / TICKETS_DIR
    for ticket in sorted(root.glob(f"*/{TICKET_FILE}")):
        split = split_frontmatter(ticket.read_text())
        if split is None:
            continue
        sections, _ = _sections(split[1], "")
        items, _ = _bullets(sections.get("Depends on", ""), "Depends on", "")
        edges[ticket.parent.name] = [i for i in items if i != "none"]
    return edges


def validate_ticket(stem: str, text: str, repo: Path) -> Ticket:
    """The full intake lint: grammar and acyclicity."""
    ticket = parse_ticket(stem, text, repo)
    edges = _graph(repo)
    edges[stem] = list(ticket.depends)
    if cycle := depends_cycle(stem, edges):
        raise TicketInvalid([_f(f"`## Depends on` closes a cycle: {' -> '.join(cycle)}",
                                "drop the edge that inverts the intended order", ticket_path(stem))])
    return ticket


# --- intake -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class IntakeResult:
    committed: tuple[str, ...]
    refused: Mapping[str, list[Finding]]


def _porcelain(out: str) -> list[tuple[str, str]]:
    entries = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        status, path = line[:2], line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        entries.append((status, path.strip('"')))
    return entries


def _pending(repo: Path, entries: list[tuple[str, str]]) -> dict[str, bool]:
    """Pending ticket stem -> True when the stem is NEW (no committed predecessor)."""
    pending: dict[str, bool] = {}
    for status, path in entries:
        if status[1] == "D" or status[0] == "D":
            continue
        new = status == "??" or status[0] == "A"
        if path.endswith("/") and status == "??":
            # An untracked dir collapses its contents into one porcelain line.
            for ticket in (repo / path).glob(f"**/{TICKET_FILE}"):
                rel = ticket.relative_to(repo).as_posix()
                if _is_ticket_path(rel):
                    pending[rel.split("/")[1]] = True
        elif _is_ticket_path(path):
            pending[path.split("/")[1]] = new
    return pending


def _is_ticket_path(path: str) -> bool:
    parts = path.split("/")
    return len(parts) == 3 and parts[0] == TICKETS_DIR and parts[2] == TICKET_FILE


def stamp(text: str, key: str, value: str) -> str:
    """Set one frontmatter key in place, leaving every other line byte-identical."""
    raw, body = split_frontmatter(text)  # type: ignore[misc]
    lines = raw.splitlines()
    line = f"{key}: {value}"
    hits = [i for i, existing in enumerate(lines) if re.match(rf"{key}\s*:", existing)]
    if hits:
        lines[hits[0]] = line
    else:
        lines.append(line)
    return "---\n" + "\n".join(lines) + "\n---\n" + body


def _intake_claims(stem: str, text: str, new: bool) -> tuple[str, list[Finding]]:
    """Apply the fail-closed `source` stamp and the human auto-confirm; return the text to commit."""
    path = ticket_path(stem)
    split = split_frontmatter(text)
    if split is None:
        return text, []  # the grammar lint reports it
    data, errs = _load_frontmatter(split[0], path)
    if data is None:
        return text, errs
    if not new:
        # An established stem keeps the source it was committed with; intake never restamps it.
        return text, []
    source = data.get("source", "human")
    if source != "human":
        return text, [_f(f"new stem claims `source: {source}`; only the machine writes `box:<class>`",
                         "drop the `source:` line (intake stamps `source: human`) -- machine-originated work"
                         " enters through the Suggestion Box", path)]
    state = data.get("state", "draft")
    if state not in ("draft", "confirmed"):
        return text, [_f(f"new stem is authored `state: {state}`",
                         "author new work as `state: draft` or `confirmed` (human intake auto-confirms)", path)]
    return stamp(stamp(text, "source", "human"), "state", "confirmed"), []


async def intake(repo: Path, git: Git, journal: Journal, fs: FileSystem) -> IntakeResult:
    """Validate pending hand-authored ticket files and ticket-plane-commit each clean one.

    The caller holds the single-writer lock. A refused ticket stays on disk, uncommitted, untouched.
    """
    entries = _porcelain(await git.status_porcelain(repo))
    pending = _pending(repo, entries)
    pending_paths = {ticket_path(s) for s in pending}
    if stray := sorted(p for st, p in entries if st[0] not in " ?" and p not in pending_paths):
        raise IntakeRefused(_f(f"staged changes outside pending ticket files: {', '.join(stray)}",
                               "unstage them; a ticket-plane commit carries only `tickets/<stem>/ticket.md`"))
    committed: list[str] = []
    refused: dict[str, list[Finding]] = {}
    for stem in sorted(pending):
        rel = ticket_path(stem)
        text = (repo / rel).read_text()
        stamped, findings = _intake_claims(stem, text, pending[stem])
        try:
            if findings:
                raise TicketInvalid(findings)
            fm = validate_ticket(stem, stamped, repo).frontmatter
        except TicketInvalid as e:
            refused[stem] = e.findings
            continue
        if stamped != text:
            fs.write(repo / rel, stamped.encode())
        await git.add(repo, [rel])
        # Pathspec-limited: a refused ticket left staged must never ride along in another stem's commit.
        await git.commit(repo, f"tempo({stem}): ticket", only=[rel])
        sha = await git.rev_parse(repo, "HEAD")
        journal.append(EventType.SIGNAL, {"signal": INTAKE_SIGNAL, "source": fm.source, "state": fm.state,
                                          "new": pending[stem], "commit": sha}, ticket=stem)
        committed.append(stem)
    return IntakeResult(committed=tuple(committed), refused=refused)


# --- new ----------------------------------------------------------------------------------------


def template() -> str:
    """The `new <stem>` skeleton: every required section, empty, so the synchronous lint lists what to fill."""
    body = "".join(f"\n## {name}\n{'none' if name == 'Depends on' else ''}\n" for name in REQUIRED_SECTIONS)
    return "---\nstate: confirmed\nsource: human\npriority: P2\nkind: feature\n---\n" + body
