"""Prompt-spec renderer and spec lint (TEMPO_PLAN.md sections 8, 13).

Host content reaches a prompt only through the DATA channel: a spec template names each injected
input as a lone `{{name}}` line inside a delimited data block, and the renderer quotes every
delimiter occurrence in a payload before the rendered prompt's data blocks are validated.
"""

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

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

