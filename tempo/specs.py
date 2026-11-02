"""Prompt-spec renderer, spec lint, the `Plan contract` resolver, and PLAN LINT (TEMPO_PLAN.md sections 8, 13).

Host content reaches a prompt only through the DATA channel: a spec template names each injected
input as a lone `{{name}}` line inside a delimited data block, and the renderer quotes every
delimiter occurrence in a payload before the rendered prompt's data blocks are validated.
"""

import hashlib
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from tempo.artifacts import Finding
from tempo.gates import ENGINE_GATE_CODES
from tempo.llm import AgentEffort, AgentTier

# Section 8: one bound at every tier and effort, so climbing the ladder never shrinks it.
RENDER_BOUND_CHARS = 400_000
# An authored ticket's base Implement render must fit this fraction of the bound.
REQ_RENDER_HEADROOM = 0.75

# Built, never spelled: a file carrying the raw delimiter can never be injected as Context (section 13).
DATA_DELIM = "<<" + "tempo-data:"
QUOTED_DELIM = "[tempo-data:"

SPEC_SECTIONS = ("Role", "Task", "Inputs", "Output format", "On-failure")
SPEC_MAX_LINES = 200
# Section 5: the LLM stage names plus `diagnose` and `requisition_review`; config load extends with host surfaces.
ENGINE_LLM_SURFACES: frozenset[str] = frozenset(
    {"author", "implement", "review", "rework", "triage", "retro", "diagnose", "requisition_review"}
)

_NAME = r"[a-z][a-z0-9_]*"
_PLACEHOLDER = re.compile(r"\{\{(.*?)\}\}")
_BEGIN = re.compile(rf"{re.escape(DATA_DELIM)}begin ({_NAME})>>")
_END = re.compile(rf"{re.escape(DATA_DELIM)}end ({_NAME})>>")
_PLACEHOLDER_LINE = re.compile(rf"^\{{\{{({_NAME})\}}\}}$", re.MULTILINE)
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def data_open(name: str) -> str:
    return f"{DATA_DELIM}begin {name}>>"


def data_close(name: str) -> str:
    return f"{DATA_DELIM}end {name}>>"


def quote_payload(text: str) -> str:
    """The ONE rendering boundary's delimiter quoting: the model sees `[tempo-data:`, the source stays byte-exact."""
    return text.replace(DATA_DELIM, QUOTED_DELIM)


class SpecError(Exception):
    def __init__(self, findings: list[Finding]) -> None:
        super().__init__("; ".join(f"{f.code}: {f.message}" for f in findings))
        self.findings = findings


class RenderOverBound(Exception):
    """The mechanical pre-call short-circuit: ticket-text arithmetic, never implementer failure."""

    def __init__(self, finding: Finding) -> None:
        super().__init__(finding.message)
        self.finding = finding


class PlanContractError(Exception):
    def __init__(self, finding: Finding) -> None:
        super().__init__(finding.message)
        self.finding = finding


# --- spec lint + render -------------------------------------------------------------------------


class SpecMeta(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    llm_surface: str
    consumes: Annotated[str, StringConstraints(min_length=1)]
    emits: Annotated[str, StringConstraints(min_length=1)] | dict[str, str]
    tier: AgentTier
    effort: AgentEffort
    gates: list[str]
    version: Annotated[str, StringConstraints(pattern=r"^\d+\.\d+$")]


@dataclass(frozen=True)
class Spec:
    meta: SpecMeta
    body: str
    inputs: tuple[str, ...]  # data-block names, in template order


def _f(code: str, message: str, road: str, line: int | None = None) -> Finding:
    return Finding(code=code, message=message, paved_road=road, line=line)


def _blocks(lines: list[str], *, template: bool) -> tuple[list[str], list[Finding]]:
    """Walk data blocks; in a template each holds exactly one `{{name}}` line. Returns block names + findings."""
    names: list[str] = []
    out: list[Finding] = []
    open_at: tuple[str, int] | None = None
    for n, line in enumerate(lines, 1):
        begin, end = _BEGIN.fullmatch(line), _END.fullmatch(line)
        if begin or end:
            if begin and open_at is None:
                open_at = (begin.group(1), n)
            elif end and open_at is not None and end.group(1) == open_at[0]:
                if template and (n - open_at[1] != 2 or lines[n - 2] != "{{%s}}" % open_at[0]):
                    out.append(_f("data_block", f"data block {open_at[0]!r} does not hold exactly its one"
                                  f" placeholder line", f"put the lone line {{{{{open_at[0]}}}}} between its"
                                  " begin and end delimiters", open_at[1]))
                names.append(open_at[0])
                open_at = None
            else:
                out.append(_f("data_block", "unpaired, nested, or mismatched data-block delimiter",
                              f"open with the line {data_open('name')}, close with {data_close('name')}, never nest",
                              n))
        elif DATA_DELIM in line:
            out.append(_f("data_block", "data-block delimiter outside a begin/end line",
                          "spell the delimiter only as a whole begin/end line; payloads are quoted by the renderer",
                          n))
        if template and open_at is None:
            for m in _PLACEHOLDER.finditer(line):
                out.append(_f("data_outside_block", f"placeholder {m.group(0)!r} folds data into instructions",
                              "inject content only as a lone {{name}} line inside its data block", n))
    if open_at is not None:
        out.append(_f("data_block", f"data block {open_at[0]!r} is never closed",
                      f"close it with its end delimiter for {open_at[0]!r}", open_at[1]))
    if len(set(names)) != len(names):
        out.append(_f("data_block", "a data-block name repeats", "give every injected input one uniquely named block"))
    return names, out


def lint_spec(
    text: str,
    *,
    surfaces: Collection[str] = ENGINE_LLM_SURFACES,
    gate_codes: Collection[str] = ENGINE_GATE_CODES,
    max_lines: int = SPEC_MAX_LINES,
) -> list[Finding]:
    """Every spec defect, each with a paved road (empty = the spec passes)."""
    return _parse_spec(text, surfaces, gate_codes, max_lines)[1]


def load_spec(
    text: str,
    *,
    surfaces: Collection[str] = ENGINE_LLM_SURFACES,
    gate_codes: Collection[str] = ENGINE_GATE_CODES,
    max_lines: int = SPEC_MAX_LINES,
) -> Spec:
    spec, findings = _parse_spec(text, surfaces, gate_codes, max_lines)
    if findings or spec is None:
        raise SpecError(findings)
    return spec


def _parse_spec(
    text: str, surfaces: Collection[str], gate_codes: Collection[str], max_lines: int
) -> tuple[Spec | None, list[Finding]]:
    out: list[Finding] = []
    if (n := len(text.splitlines())) > max_lines:
        out.append(_f("spec_size", f"spec is {n} lines, over the {max_lines}-line budget",
                      "cut prose or move host detail into host docs rendered as data"))
    fm = _FRONTMATTER.match(text)
    if fm is None:
        return None, out + [_f("spec_frontmatter", "spec has no YAML frontmatter between --- fences",
                               "open the file with a --- fenced frontmatter block")]
    meta = None
    try:
        raw = yaml.safe_load(fm.group(1))
        meta = SpecMeta.model_validate(raw)
    except yaml.YAMLError as e:
        out.append(_f("spec_frontmatter", f"frontmatter is not YAML: {e}", "fix the YAML syntax"))
    except ValidationError as e:
        for err in e.errors():
            out.append(_f("spec_frontmatter", f"{'.'.join(map(str, err['loc'])) or '<root>'}: {err['msg']}",
                          "use exactly the fields llm_surface, consumes, emits, tier, effort, gates, version"))
    if meta is not None:
        if meta.llm_surface not in surfaces:
            out.append(_f("spec_vocab", f"llm_surface {meta.llm_surface!r} is not in the closed vocabulary",
                          f"use one of {sorted(surfaces)}"))
        for g in meta.gates:
            if g not in gate_codes:
                out.append(_f("spec_vocab", f"gate {g!r} is not in the closed vocabulary",
                              f"use codes from {sorted(gate_codes)}"))
    body = text[fm.end():]
    lines = body.splitlines()
    headings = tuple(line[3:].strip() for line in lines if line.startswith("## "))
    if headings != SPEC_SECTIONS:
        out.append(_f("spec_sections", f"sections are {list(headings)}",
                      f"use exactly the sections {list(SPEC_SECTIONS)}, in that order, each once"))
    names, block_findings = _blocks(lines, template=True)
    offset = text[: fm.end()].count("\n")  # report file lines, not body lines
    out += [f.model_copy(update={"line": f.line + offset}) if f.line else f for f in block_findings]
    if out or meta is None:
        return None, out
    return Spec(meta=meta, body=body, inputs=tuple(names)), []


def validate_data_blocks(rendered: str) -> list[Finding]:
    """The rendered prompt's data/instruction boundary: every delimiter is a paired begin/end line."""
    return _blocks(rendered.splitlines(), template=False)[1]


def render(spec: Spec, inputs: Mapping[str, str]) -> str:
    """Render `spec` with each input quoted into its data block, refusing an over-bound prompt."""
    if set(inputs) != set(spec.inputs):
        raise SpecError([_f("render_inputs", f"inputs {sorted(inputs)} do not match the spec's data blocks"
                            f" {sorted(spec.inputs)}", "supply exactly one value per data block")])
    # One pass: a payload is inserted literally, never re-scanned for another input's placeholder.
    rendered = _PLACEHOLDER_LINE.sub(lambda m: quote_payload(inputs[m.group(1)]).removesuffix("\n"), spec.body)
    if findings := validate_data_blocks(rendered):
        raise SpecError(findings)
    check_render_bound(rendered)
    return rendered


def check_render_bound(rendered: str) -> None:
    if len(rendered) > RENDER_BOUND_CHARS:
        raise RenderOverBound(_f("render_over_bound",
                                 f"rendered prompt is {len(rendered)} characters, over the bound {RENDER_BOUND_CHARS}",
                                 "shrink the inputs or split the ticket"))


# --- Plan contract resolver ---------------------------------------------------------------------

PLAN_UNITS = ("19.L", "19.I") + tuple(f"19.P{n}" for n in range(7))
REFUSED_SECTIONS = frozenset({22})  # uncited rationale appendix: never resolves
ENTRY_PARTS = ("Owner", "Records", "Observable", "Tests")  # section 19 SPEC DEPTH
_SECTION_HEAD = re.compile(r"## (\d+)\. ")
_UNIT_HEAD = re.compile(r"### (\d+\.\d+|19\.\S+) ")
_PLAN_ID = re.compile(r"section (\d+(?:\.\d+)?)|(19\.(?:L|I|P[0-6](?:\.[a-z0-9][a-z0-9-]*)?))")
_CANONICAL_PLAN_ID = re.compile(r"(?:0|[1-9]\d*)(?:\.[1-9]\d*)?|19\.(?:L|I|P[0-6](?:\.[a-z0-9][a-z0-9-]*)?)")
_ENTRY_ID = re.compile(r"19\.P([0-6])\.([a-z0-9][a-z0-9-]*)")


def plan_id(text: str) -> str:
    """Canonical id for a `Plan contract` bullet: `section N[.k]` -> `N[.k]`; `19.<unit>` verbatim."""
    m = _PLAN_ID.fullmatch(text.strip())
    if m is None:
        raise PlanContractError(_f("plan_contract", f"{text!r} is not a plan id",
                                   "cite `section N`, `section N.k`, or a unit id `19.L` / `19.I` /"
                                   " `19.P<n>` / `19.P<n>.<stem>`"))
    if m.group(1) is not None:
        return ".".join(str(int(p)) for p in m.group(1).split("."))
    return m.group(2)


@dataclass(frozen=True)
class _Head:
    line: int
    pid: str  # "" for a heading carrying no id
    level: int
    section: str  # the enclosing `## N.` id ("" outside numbered sections)


def _heads(lines: list[str]) -> list[_Head]:
    """Every `##`/`###` heading outside fenced blocks, with its enclosing numbered section."""
    heads: list[_Head] = []
    fence: str | None = None
    section = ""
    for i, line in enumerate(lines):
        stripped = line.rstrip("\n")
        if m := re.match(r"(`{3,}|~{3,})", stripped):
            if fence is None:
                fence = m.group(1)
            elif stripped.strip() == fence[0] * len(stripped.strip()) and len(stripped.strip()) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        if m := _SECTION_HEAD.match(stripped):
            section = str(int(m.group(1)))
            heads.append(_Head(i, section, 2, section))
        elif stripped.startswith("## "):
            section = ""
            heads.append(_Head(i, "", 2, section))
        elif m := _UNIT_HEAD.match(stripped):
            heads.append(_Head(i, m.group(1), 3, section))
        elif stripped.startswith("### "):
            heads.append(_Head(i, "", 3, section))
    return heads


def _units(plan: str) -> tuple[dict[str, list[str]], list[_Head]]:
    """Map every heading id to its verbatim slices (a list: duplicates are a lint defect)."""
    lines = plan.splitlines(keepends=True)
    heads = _heads(lines)
    found: dict[str, list[str]] = {}
    for k, h in enumerate(heads):
        if not h.pid:
            continue
        # A section runs to the next `##`; a subsection or 19 unit to the next `###` or `##`.
        end = next((x.line for x in heads[k + 1:] if x.level <= h.level), len(lines))
        found.setdefault(h.pid, []).append("".join(lines[h.line:end]))
    return found, heads


def resolve_plan_contract(plan: str, ids: Iterable[str]) -> str:
    """Resolve canonical plan ids to verbatim plan bytes, deduplicated in first-citation order."""
    found, _ = _units(plan)
    seen: list[str] = []
    for pid in ids:
        if _CANONICAL_PLAN_ID.fullmatch(pid) is None:
            raise PlanContractError(_f("plan_contract", f"{pid!r} is not a canonical plan id",
                                       "canonicalize a `Plan contract` bullet with `plan_id` first"))
        if not pid.startswith("19.") and int(pid.split(".")[0]) in REFUSED_SECTIONS:
            raise PlanContractError(_f("plan_contract", f"section {pid} never resolves (uncited rationale)",
                                       "cite the section or unit that states the rule itself"))
        slices = found.get(pid, [])
        if len(slices) != 1:
            raise PlanContractError(_f("plan_contract",
                                       f"{pid!r} matches {len(slices)} plan headings, not exactly one",
                                       "cite an id with exactly one `## N.` or `### <id>` heading"))
        if pid not in seen:
            seen.append(pid)
    for pid in seen:
        if not pid.startswith("19.") and "." in pid and pid.split(".")[0] in seen:
            raise PlanContractError(_f("plan_contract", f"section {pid.split('.')[0]} is cited with its own"
                                       f" subsection {pid}", "cite the whole section or its subsections, not both"))
    return "".join(found[pid][0] for pid in seen)


def unit_sha(plan: str, uid: str) -> str:
    """Hash exactly the slice the Plan contract resolver injects; missing units are nameable."""
    if uid not in _units(plan)[0]:
        return "absent"
    return hashlib.sha256(resolve_plan_contract(plan, [uid]).encode()).hexdigest()


def without_unit(plan: str, uid: str) -> str:
    """The plan with one `###` unit's verbatim slice removed (unchanged when the unit is absent or duplicated)."""
    slices = _units(plan)[0].get(uid, [])
    return plan.replace(slices[0], "", 1) if len(slices) == 1 else plan


def required_units(plan: str, stem: str, plan_contract: Iterable[str]) -> tuple[str, ...]:
    """Own non-exit entry then cited registry entries, even when their headings are absent."""
    entries = {f"19.P{phase}.{name}" for name, (phase, row) in registry_rows(plan).items()
               if not row.get("exit")}
    own = next((uid for uid in entries if uid.rsplit(".", 1)[1] == stem), None)
    return tuple(dict.fromkeys([*([own] if own is not None else []),
                               *(uid for uid in plan_contract if uid in entries)]))


def hardenable_units(plan: str, stem: str, plan_contract: Iterable[str]) -> tuple[str, ...]:
    """Own entry, cited entries, then cited phases' non-exit rows, even when their headings are absent."""
    entries = {f"19.P{phase}.{name}": f"19.P{phase}" for name, (phase, row) in registry_rows(plan).items()
               if not row.get("exit")}
    cites = tuple(plan_contract)
    return tuple(dict.fromkeys([*required_units(plan, stem, cites),
                               *(uid for cite in cites for uid, phase in entries.items() if phase == cite)]))


def entry_unit_gap(plan: str, uid: str) -> str | None:
    """Why a registry row's entry unit cannot govern its seed (missing or lacking a SPEC DEPTH part); None when it can."""
    slices = _units(plan)[0].get(uid, [])
    if len(slices) != 1:
        return f"entry unit {uid} is missing"
    missing = [p for p in ENTRY_PARTS if not re.search(rf"^- \*\*{p}:\*\*[ \t]*\S", slices[0], re.M)]
    return f"entry unit {uid} lacks its {', '.join(missing)} part(s)" if missing else None


def registry_rows(plan: str) -> dict[str, tuple[int, dict]]:
    """Every parseable registry row: stem -> (phase, row). Malformed registries are lint's to report."""
    rows: dict[str, tuple[int, dict]] = {}
    for phase, body, _ in _REGISTRY.findall(plan):
        try:
            reg = yaml.safe_load(body)
        except yaml.YAMLError:
            continue
        seeds = reg.get("seeds") if isinstance(reg, dict) else None
        if isinstance(seeds, dict):
            rows.update({stem: (int(phase), row) for stem, row in seeds.items() if isinstance(row, dict)})
    return rows


# --- PLAN LINT ----------------------------------------------------------------------------------

CITABLE_SECTIONS = tuple(range(1, 19)) + (20,)
MAX_ADMISSION_PAYLOADS = 2
_REGISTRY = re.compile(r"^# BEGIN_REGISTRY_P(\d+)\n(.*?)^# END_REGISTRY_P(\d+)$", re.DOTALL | re.MULTILINE)


def lint_plan(plan: str) -> list[Finding]:
    """Keep the plan renderable as per-ticket seed contracts (section 8). Empty = green."""
    out: list[Finding] = []
    found, heads = _units(plan)
    for pid, slices in found.items():
        if len(slices) > 1:
            out.append(_f("plan_heading", f"heading id {pid} appears {len(slices)} times",
                          "give every section, subsection, and 19 unit exactly one heading"))
    rows = registry_rows(plan)
    for h in heads:
        if h.level != 3 or not h.pid:
            continue
        if h.pid.startswith("19."):
            entry = _ENTRY_ID.fullmatch(h.pid)
            if h.section != "19" or not (h.pid in PLAN_UNITS or (entry and rows.get(entry.group(2), (None,))[0]
                                                                  == int(entry.group(1)))):
                out.append(_f("plan_unit", f"`### {h.pid}` is not a citable unit id", f"name section-19 units only"
                              f" {list(PLAN_UNITS)} or `19.P<n>.<stem>` for a row of that phase's registry"))
        elif h.pid.split(".")[0] != h.section:
            out.append(_f("plan_unit", f"`### {h.pid}` sits outside its own `## {h.pid.split('.')[0]}.` section",
                          "number a subsection after the section that contains it"))
    for pid in PLAN_UNITS + tuple(map(str, CITABLE_SECTIONS)):
        if not found.get(pid):
            out.append(_f("plan_unit", f"plan id {pid} has no heading", f"restore the heading for {pid}"))
    for pid, slices in found.items():
        if _ENTRY_ID.fullmatch(pid):
            missing = [p for p in ENTRY_PARTS if not re.search(rf"^- \*\*{p}:\*\*[ \t]*\S", slices[0], re.M)]
            if missing:
                out.append(_f("plan_entry", f"entry unit {pid} lacks its {', '.join(missing)} part(s)",
                              "state each SPEC DEPTH part as a non-empty `- **<Part>:**` bullet (section 19)"))
    return out + _lint_registries(plan, found)


def _cite_resolves(c: object, found: Mapping[str, list[str]]) -> bool:
    if isinstance(c, int):
        return c in CITABLE_SECTIONS and bool(found.get(str(c)))
    if c == "19.L":
        return True
    return (isinstance(c, str) and re.fullmatch(r"[1-9]\d*\.[1-9]\d*", c) is not None
            and int(c.split(".")[0]) in CITABLE_SECTIONS and bool(found.get(c)))


def _lint_registries(plan: str, found: Mapping[str, list[str]]) -> list[Finding]:
    out: list[Finding] = []
    begins = re.findall(r"^# BEGIN_REGISTRY_P(\d+)$", plan, re.MULTILINE)
    blocks = _REGISTRY.findall(plan)
    if len(blocks) != len(begins) or any(b != e for b, _, e in blocks):
        out.append(_f("registry_parse", "a BEGIN_REGISTRY_P<n> block has no matching END_REGISTRY_P<n>",
                      "close each registry with its own # END_REGISTRY_P<n> line"))
    for phase, body, _ in blocks:
        where = f"REGISTRY_P{phase}"
        try:
            reg = yaml.safe_load(body)
        except yaml.YAMLError as e:
            out.append(_f("registry_parse", f"{where} is not YAML: {e}", "fix the registry YAML"))
            continue
        admissions = reg.get("admissions") if isinstance(reg, dict) else None
        seeds = reg.get("seeds") if isinstance(reg, dict) else None
        if not (isinstance(admissions, list) and all(isinstance(a, list) for a in admissions)
                and isinstance(seeds, dict) and all(isinstance(v, dict) for v in seeds.values())):
            out.append(_f("registry_parse", f"{where} lacks an `admissions` list of lists and a `seeds` mapping",
                          "declare admissions: [[stem, ...], ...] and seeds: {stem: {...}}"))
            continue
        admitted: list[str] = [s for a in admissions for s in a]
        for a in admissions:
            if not 1 <= len(a) <= MAX_ADMISSION_PAYLOADS:
                out.append(_f("registry_admission", f"{where} admission {a} has {len(a)} payloads",
                              f"admit 1-{MAX_ADMISSION_PAYLOADS} seeds per admission; split the rest"))
        dupes = sorted({s for s in admitted if admitted.count(s) > 1})
        if dupes:
            out.append(_f("registry_coverage", f"{where} admits {dupes} more than once", "admit each seed once"))
        if set(admitted) != set(seeds):
            out.append(_f("registry_coverage", f"{where} admissions and seeds differ: unadmitted"
                          f" {sorted(set(seeds) - set(admitted))}, undeclared {sorted(set(admitted) - set(seeds))}",
                          "admit every seed exactly once and declare every admitted stem under seeds"))
        for stem, row in seeds.items():
            cites = row.get("cite", [])
            for c in cites if isinstance(cites, list) else [cites]:
                if not _cite_resolves(c, found):
                    out.append(_f("registry_cite", f"{where} seed {stem!r} cites {c!r}, which does not resolve",
                                  f"cite an integer section from {list(CITABLE_SECTIONS)}, a quoted"
                                  " `\"N.k\"` subsection, or `\"19.L\"`"))
    return out
