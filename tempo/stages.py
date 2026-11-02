"""The Implement, Check, and Review stages (TEMPO_PLAN.md sections 4, 5, 9, 10, 13).

Implement and Review are prompt specs run through the one driver; Check is mechanical. This module
owns stage-evidence gathering and the stage-terminal OUTBOX lift (section 9's ownership law): each
stage writes its artifact into `tickets/<stem>/` inside the worktree, and at the stage terminal the
lift moves it to the canonical tickets dir as ONE ticket-plane commit `tempo(<stem>): <kind>`.

The run's terminal `state_transition` is not written here: that is the runner's (section 11.2).
"""

import asyncio
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from tempo.artifacts import OUTCOMES, Artifact, Diagnosis, DiagnosisReply, Finding, Harvest, NonBlank, Outcome, ReviewVerdict, StageResult
from tempo.box import BOX_DIR
from tempo.checkout import SPECS_DIR, Checkout
from tempo.storm import arrival_id
from tempo.config import Config, ConfigSnapshot, Severity
from tempo.control import protected_cleanup
from tempo.driver import Driver, LlmStage
from tempo.effects import Effects
from tempo.gates import BugEvidence, GateReport, RegressionRun, run_gates
from tempo.git import Git, GitError
from tempo.hostfiles import CORE_TEMPLATE, CoreDriftGate, committed_core
from tempo.journal import EventType, run_seq
from tempo.llm import LLM
from tempo.seams import ExecutableNotFound, FileSystem, ProcessExec
from tempo.specs import RenderOverBound, Spec, load_spec, render
from tempo.tickets import _HEADING, TICKET_FILE, TICKETS_DIR, Ticket, ticket_path

MAIN = "main"
# Section 7 diff budget: sized so every rendered review prompt fits the serving provider's bound.
DIFF_BUDGET_FILES = 30
DIFF_BUDGET_INSERTED = 1_500
# The mechanical Check has no prompt spec; this versions its own contract in the provenance slot.
CHECK_SPEC_VERSION = 1
OUTPUT_TAIL_CHARS = 2_000
RUN_RECORD_SECTIONS = (
    "Outcome",
    "Surprises / judgment calls",
    "Dead ends",
    "Second problems filed",
    "Resolved engine/model",
)

StageName = Literal["implement", "check", "review"]
# Section 11.2: the attempt-scoped re-entry block's heading, rendered in criteria-position.
PRIOR_ATTEMPTS = "## Prior attempts (attempt-scoped, informational, unverified, not reviewed)"
DIAGNOSIS_STUCK_S = 600.0


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


# --- artifacts ----------------------------------------------------------------------------------


class SecondProblem(_Strict):
    summary: NonBlank


class ImplementReply(_Strict):
    """The implement surface's model-facing reply (specs/implement.md); the stage stamps provenance."""

    outcome: Literal["ok", "already_satisfied", "premise_failed"]
    summary: NonBlank
    surprises: str
    dead_ends: str
    findings: list[Finding]
    second_problems: list[SecondProblem] = []

    @model_validator(mode="after")
    def _findings_match_outcome(self) -> "ImplementReply":
        if self.outcome == "premise_failed":
            if not self.findings or any(f.code != "premise" for f in self.findings):
                raise ValueError("premise_failed needs at least one finding, every code `premise`")
        elif self.findings:
            raise ValueError(f"{self.outcome} carries an empty findings list; reply premise_failed to stop")
        return self


class PackingSlip(Artifact):
    """Implement's artifact: the branch plus its run record (section 4)."""

    stem: NonBlank
    branch: NonBlank
    outcome: Literal["ok", "already_satisfied"]
    summary: NonBlank
    run_record: NonBlank  # repo-relative path of the lifted run.md


class Invoice(Artifact):
    """Check's artifact, persisted as `checks.json`: every mechanical gate report for one branch head."""

    stem: NonBlank
    passed: bool
    changed_files: list[str]
    inserted_lines: Annotated[int, Field(ge=0)]
    bypassed: list[str]  # gate codes the ticket's `gate_bypass` valve demoted to soft
    reports: list[GateReport]
    verification: list["CommandResult"] = []


class _Review(Artifact):
    stem: NonBlank
    reviewed_sha: NonBlank
    summary: NonBlank
    findings: list[Finding]
    spec_version: NonBlank
    provider: str | None
    model: str | None


class ApprovedInvoice(_Review):
    verdict: Literal["approve"]


class SnagList(_Review):
    verdict: Literal["snag"]


class Rma(_Review):
    verdict: Literal["rma"]


ReviewArtifact = Annotated[ApprovedInvoice | SnagList | Rma, Field(discriminator="verdict")]
EMITS_BY_VERDICT: Mapping[str, type[_Review]] = {"approve": ApprovedInvoice, "snag": SnagList, "rma": Rma}
_REVIEW = TypeAdapter(ReviewArtifact)
_REVIEW_JSON = re.compile(r"^```json\n(.*?)\n```$", re.DOTALL | re.MULTILINE)


def render_review(review: _Review) -> str:
    lines = [f"# Review: {review.verdict}", "", review.summary, "", "## Findings", ""]
    lines += [f"- [{f.code}] {f.path or '-'}:{f.line or '-'} {f.message} (do instead: {f.paved_road})"
              for f in review.findings] or ["none"]
    return "\n".join(lines + ["", "## Record", "", "```json", review.model_dump_json(indent=2), "```", ""])


def read_review(text: str) -> ApprovedInvoice | SnagList | Rma:
    """Parse `review.md`'s pinned record (the merge gate's on-disk input); the prose above it is not read."""
    m = _REVIEW_JSON.search(text)
    if m is None:
        raise ValueError("review.md carries no ```json record block; re-run Review to rewrite it")
    return _REVIEW.validate_json(m.group(1))


# --- context ------------------------------------------------------------------------------------


@dataclass
class StageBoundary:
    """Memory-only selection supplied by the lifetime; the CLI run path has no suspension policy."""

    select: Callable[[Ticket, StageName], bool] | None = None
    run: "StagesRun | None" = None
    ticket: Ticket | None = None
    next_stage: StageName | None = None
    active: asyncio.Task | None = None


@dataclass(frozen=True)
class StageContext:
    """The seams one ticket run's stages share. `env` is the parent env; children get it minus secrets."""

    repo: Path
    config: Config | ConfigSnapshot
    env: Mapping[str, str]
    exec_: ProcessExec
    git: Git
    fs: FileSystem
    driver: Driver
    specs_dir: Path
    boundary: StageBoundary = field(default_factory=StageBoundary, init=False, compare=False)

    def worktree(self, stem: str) -> Path:
        assert self.config.worktree_root is not None  # resolved at config load
        return self.config.worktree_root / stem

    async def abort_current(self) -> None:
        try:
            await self.driver.abort_current()
        finally:
            task = self.boundary.active
            if task is not None:
                if not task.done() and not task.cancelling():
                    task.cancel()
                cleanup = asyncio.gather(task, return_exceptions=True)
                await protected_cleanup(cleanup)
                [result] = cleanup.result()
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    raise result


class Stages:
    """Public access to the Driver already owned by the stage composition."""
    def __init__(self, ctx: StageContext):
        self._ctx = ctx

    @property
    def driver(self) -> Driver:
        return self._ctx.driver


def _lift_files(ctx: StageContext, stem: str, kind: str, only: str | None = None) -> list[Path]:
    outbox = ctx.worktree(stem) / TICKETS_DIR / stem
    canonical = ctx.repo / TICKETS_DIR / stem
    return sorted(p for p in outbox.rglob("*") if p.is_file()
                  and p.relative_to(outbox) != Path(TICKET_FILE)
                  and (only is None or p.relative_to(outbox).as_posix() == only)
                  and not ((c := canonical / p.relative_to(outbox)).is_file()
                           and c.read_bytes() == p.read_bytes()))


def _spec(ctx: StageContext, surface: str) -> Spec:
    return load_spec((ctx.specs_dir / f"{surface}.md").read_text())


def _major(spec: Spec) -> int:
    return int(spec.meta.version.split(".")[0])


def findings_text(findings: Sequence[Finding]) -> str:
    return "\n".join(f"- {f.code}: {f.message} (do instead: {f.paved_road})" for f in findings) or "none"


@dataclass(frozen=True)
class DiagnosisMaterial:
    ticket: str
    terminal: str
    stage: str | None
    harvest: Harvest | None
    run_record: str | None


def diagnose_stage(spec: Spec, material: DiagnosisMaterial) -> LlmStage:
    """The one pure diagnosis render, shared with the real-model evaluation."""
    inputs = {
        "ticket": material.ticket,
        "terminal": f"{material.terminal} at {material.stage or 'none'}",
        "harvest": material.harvest.model_dump_json() if material.harvest else "none",
        "run_record": material.run_record or "none",
    }

    def render_material(_: DiagnosisMaterial, findings: list[Finding]) -> str:
        return render(spec, {**inputs, "retry_findings": findings_text(findings)})

    return LlmStage(surface="diagnose", emits=DiagnosisReply, gates=[], render=render_material)


async def write_diagnosis(ctx: StageContext, ticket: Ticket, *, attempt: int, terminal: str,
                          stage: str | None, verdict: str, lessons: list[str],
                          mechanical: str | None, provider: str | None = None,
                          model: str | None = None) -> Diagnosis:
    spec = _spec(ctx, "diagnose")
    record = Diagnosis(
        produced_by_spec_version=_major(spec), produced_at_sha=await ctx.git.rev_parse(ctx.repo, ticket.stem),
        stem=ticket.stem, attempt=attempt, terminal=terminal, stage=stage, verdict=verdict,
        lessons=lessons, mechanical=mechanical, spec_version=spec.meta.version,
        provider=provider, model=model,
    )
    ctx.fs.write(ctx.worktree(ticket.stem) / TICKETS_DIR / ticket.stem / "diagnosis.json",
                 (record.model_dump_json(indent=2) + "\n").encode())
    await lift_outbox(ctx, ticket.stem, "diagnosis", attempt=attempt, only="diagnosis.json")
    return record


async def diagnose(ctx: StageContext, ticket: Ticket, material: DiagnosisMaterial, *, attempt: int) -> Diagnosis:
    spec = _spec(ctx, "diagnose")
    result = await ctx.driver.run(
        diagnose_stage(spec, material), material, ticket=ticket.stem, attempt=attempt,
        workspace=ctx.worktree(ticket.stem), tier=ticket.frontmatter.agent_tier,
        effort=ticket.frontmatter.agent_effort, stuck_budget=DIAGNOSIS_STUCK_S)
    reply = result.artifact
    if result.outcome == "ok" and isinstance(reply, DiagnosisReply):
        return await write_diagnosis(ctx, ticket, attempt=attempt, terminal=material.terminal,
                                     stage=material.stage, verdict=reply.verdict, lessons=list(reply.lessons),
                                     mechanical=None, provider=result.provider, model=result.model)
    return await write_diagnosis(ctx, ticket, attempt=attempt, terminal=material.terminal,
                                 stage=material.stage, verdict="abandon-human", lessons=[],
                                 mechanical=f"no schema-valid verdict ({result.outcome})",
                                 provider=result.provider, model=result.model)


def _short(result: StageResult, outcome: Outcome, findings: list[Finding], artifact: Artifact | None = None) -> StageResult:
    return StageResult(outcome=outcome, artifact=artifact, findings=findings,
                       provider=result.provider, model=result.model)


async def prepare_worktree(ctx: StageContext, stem: str) -> Path:
    """TEARDOWN-AND-CREATE (section 6): remove any worktree and branch a prior run left, then branch from main."""
    path = ctx.worktree(stem)
    if path.exists():
        await ctx.git.worktree_remove(ctx.repo, path)
    else:
        await ctx.git.worktree_prune(ctx.repo)
    try:
        await ctx.git.rev_parse(ctx.repo, f"refs/heads/{stem}")
    except GitError:
        pass  # no leftover branch
    else:
        await ctx.git.branch_delete(ctx.repo, stem)
    path.parent.mkdir(parents=True, exist_ok=True)
    await ctx.git.worktree_add(ctx.repo, path, stem, MAIN)
    return path


async def lift_outbox(ctx: StageContext, stem: str, kind: str, *, attempt: int,
                      only: str | None = None) -> str | None:
    """Move the worktree outbox into the canonical ticket dir as ONE ticket-plane commit; None if empty.

    `ticket.md` is never lifted: it stays read-only to the agent (section 10). Keyed with the run
    sequence, so a same-run re-entry never commits twice. A file byte-equal to its canonical copy is a prior
    run's artifact the worktree inherited from main, not this stage's output: it is never lifted.
    """
    outbox = ctx.worktree(stem) / TICKETS_DIR / stem
    files = _lift_files(ctx, stem, kind, only)
    if not files:
        return None

    async def commit() -> dict:
        rels = []
        for src in files:
            rel = f"{TICKETS_DIR}/{stem}/{src.relative_to(outbox).as_posix()}"
            ctx.fs.write(ctx.repo / rel, src.read_bytes())
            src.unlink()
            rels.append(rel)
        await ctx.git.add(ctx.repo, rels)
        await ctx.git.commit(ctx.repo, f"tempo({stem}): {kind}", only=rels)
        return {"kind": kind, "paths": rels, "commit": await ctx.git.rev_parse(ctx.repo, "HEAD")}

    key = "/".join(("ticket-plane", stem, str(attempt), kind))
    return (await ctx.driver.effects.run(commit, key=key, ticket=stem))["commit"]


# --- Implement ----------------------------------------------------------------------------------


def implement_inputs(repo: Path, ticket_text: str, ticket: Ticket,
                     context_files: Sequence[Path]) -> dict[str, str]:
    """The production Implement inputs, also used to measure a ticket at authoring."""
    paths = list(dict.fromkeys([*ticket.context, *(str(p) for p in context_files)]))
    context = "\n\n".join(f"### {p}\n{(repo / p).read_text()}" for p in paths) or "none"
    return {
        "ticket": ticket_text,
        "context": context,
    }


def _one_line(text: str) -> str:
    # A payload newline could forge a ticket section heading inside the rendered ticket block.
    return " ".join(text.split())


def _prior_findings(ctx: StageContext, stem: str, stage: str | None) -> list[Finding]:
    """The terminal stage's durable findings artifact in the canonical ticket dir; [] when it wrote none."""
    root = ctx.repo / TICKETS_DIR / stem
    if stage == "review" and (path := root / "review.md").is_file():
        return list(read_review(path.read_text()).findings)
    if stage == "check" and (path := root / "checks.json").is_file():
        invoice = Invoice.model_validate_json(path.read_text())
        return [f for r in invoice.reports if r.verdict == "fail" for f in r.findings]
    return []


def _quoted(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.split("\n"))


def prior_attempts(ctx: StageContext, stem: str) -> str | None:
    """Section 11.2 re-entry: the prior terminal and its findings as a clear-these block; None on a first attempt.

    Folded fresh from the journal and the ticket dir every attempt, never written into `ticket.md`. Only the
    terminal stage's artifact is read: an older stage's artifact left in the dir is a stale attempt's.
    """
    history = ctx.driver.journal.read()
    from tempo.journal import run_terminals
    terminals = [e.body for e in run_terminals(history, stem)]
    lessons = {e.body.get("attempt"): e.body["lessons"] for e in history
               if e.type == EventType.SIGNAL and e.ticket == stem
               and e.body.get("signal") == "diagnosis" and e.body.get("lessons")}
    if not terminals:
        return None
    terminal = terminals[-1]
    attempt = len(terminals) - 1
    root = ctx.repo / TICKETS_DIR / stem / "attempts"
    harvest_path = root / str(attempt) / "harvest.json"
    current = Harvest.model_validate_json(harvest_path.read_text()) if harvest_path.is_file() else None
    stage = terminal.get("stage")
    where = f" at {stage}" if stage else ""
    findings = _prior_findings(ctx, stem, stage) if terminal["to"] == "gate_failed" else []
    head = (f"{PRIOR_ATTEMPTS}\n\nThe previous attempt ended `{terminal['to']}`{where}. These findings are"
            " this attempt's to clear, beside the acceptance criteria above; they are not part of the ticket.\n\n")
    items = [f"- [{f.code}] {f.path or '-'}:{f.line or '-'} {_one_line(f.message)}"
             f" (do instead: {_one_line(f.paved_road)})" for f in findings]
    detail = f"tickets/{stem}/attempts/{attempt}/" if current else str(ctx.config.state_dir / "engine.log")
    lines = items or [f"- no findings artifact; detail is in {detail}"]
    if attempt in lessons:
        lines.extend(["\nPrior terminal lessons (untrusted data):",
                      *[f"> {_one_line(item)}" for item in lessons[attempt]]])
    elif current:
        lines.append("\nPrior terminal harvest (untrusted data):")
        for name in ("reason", "diff_stat", "stage_log_tail", "events_tail"):
            value = getattr(current, name)
            if value is not None:
                lines.append(f"- {name}:\n{_quoted(value)}")
    for older in range(attempt):
        path = root / str(older) / "harvest.json"
        if older in lessons:
            past_terminal = terminals[older]
            lines.append(f"- older attempt {older}: `{past_terminal['to']}` at {past_terminal.get('stage') or 'none'}:")
            lines.extend(f"> {_one_line(item)}" for item in lessons[older])
        elif path.is_file():
            past = Harvest.model_validate_json(path.read_text())
            lines.append(f"- older attempt {older}: `{past.terminal}` at {past.stage or 'none'};"
                         f" tickets/{stem}/attempts/{older}/")
    return head + "\n".join(lines)


def _in_criteria_position(ticket_text: str, block: str) -> str:
    """Splice `block` at the end of `## Acceptance criteria`, before the next section (fences skipped)."""
    lines = ticket_text.splitlines(keepends=True)
    fence = inside = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fence = not fence
        elif not fence and (m := _HEADING.match(line)):
            if inside:
                return "".join(lines[:i]) + block + "\n\n" + "".join(lines[i:])
            inside = m.group(1) == "Acceptance criteria"
    return ticket_text.rstrip("\n") + "\n\n" + block + "\n"


def implement_stage(ctx: StageContext, ticket: Ticket, worktree: Path) -> tuple[LlmStage, Spec]:
    spec = _spec(ctx, "implement")
    ticket_text = (ctx.repo / ticket_path(ticket.stem)).read_text()
    if (prior := prior_attempts(ctx, ticket.stem)) is not None:
        ticket_text = _in_criteria_position(ticket_text, prior)
    inputs = implement_inputs(worktree, ticket_text, ticket, ctx.config.context_files)

    def render_ticket(_: Ticket, findings: list[Finding]) -> str:
        return render(spec, {**inputs, "retry_findings": findings_text(findings)})

    return LlmStage(surface="implement", emits=ImplementReply, gates=[], render=render_ticket), spec


def run_record(reply: ImplementReply | None, result: StageResult, spec: Spec, second_problem_ids: Sequence[str],
               *, outcome: Outcome | None = None) -> str:
    fields = {
        "Outcome": outcome or reply.outcome,
        "Surprises / judgment calls": reply.surprises if reply else 'none',
        "Dead ends": reply.dead_ends if reply else 'none',
        "Second problems filed": "\n".join(
            f"- {id}: {_one_line(problem.summary)}"
            for id, problem in zip(second_problem_ids, reply.second_problems if reply else (), strict=True)
        ) or "none",
        "Resolved engine/model": f"- provider: {result.provider}\n- model: {result.model}\n"
                                 f"- spec: {spec.meta.llm_surface} {spec.meta.version}",
    }
    return "".join(f"## {name}\n\n{(fields[name].strip() or 'none')}\n\n" for name in RUN_RECORD_SECTIONS)


async def implement(ctx: StageContext, ticket: Ticket, *, attempt: int) -> StageResult:
    """Teardown-and-create the worktree, run the implement spec in it, and write + lift the run record."""
    stem = ticket.stem
    worktree = await prepare_worktree(ctx, stem)
    stage, spec = implement_stage(ctx, ticket, worktree)
    try:
        result = await ctx.driver.run(
            stage, ticket, ticket=stem, attempt=attempt, workspace=worktree,
            tier=ticket.frontmatter.agent_tier, effort=ticket.frontmatter.agent_effort,
            stuck_budget=ticket.stuck_minutes * 60.0)
    except RenderOverBound as e:
        # The pre-call short-circuit: ticket-text arithmetic, parked like premise_failed (section 8).
        return StageResult(outcome="premise_failed", artifact=None, findings=[e.finding])
    return await finish_implement(ctx, ticket, spec, result, attempt=attempt)


async def finish_implement(ctx: StageContext, ticket: Ticket, spec: Spec, result: StageResult, *,
                           attempt: int) -> StageResult:
    """After the call: file second problems, write + lift the run record, and issue the packing slip."""
    stem = ticket.stem
    worktree = ctx.worktree(stem)
    if result.outcome != "ok":
        if result.provider is not None:
            record = f'{TICKETS_DIR}/{stem}/run.md'
            ctx.fs.write(worktree / record, run_record(None, result, spec, [], outcome=result.outcome).encode())
            await lift_outbox(ctx, stem, 'run-record', attempt=attempt)
        return result
    reply = result.artifact
    assert isinstance(reply, ImplementReply)
    from tempo.storm import storm_producer

    box = storm_producer(root=ctx.config.state_dir / BOX_DIR, fs=ctx.fs,
                         journal=ctx.driver.journal, clock=ctx.driver.clock)
    second_problem_ids = [
        box.enqueue(message_class="suggestion", origin=stem, stage="implement",
                    outcome=reply.outcome, summary=problem.summary,
                    occurrence_id=arrival_id("second-problem", stem, attempt, index))[0]
        for index, problem in enumerate(reply.second_problems)
    ]
    record = f"{TICKETS_DIR}/{stem}/run.md"
    ctx.fs.write(worktree / record, run_record(reply, result, spec, second_problem_ids).encode())
    await lift_outbox(ctx, stem, "run-record", attempt=attempt)
    if reply.outcome == "premise_failed":
        return _short(result, "premise_failed", list(reply.findings))
    return _short(result, "ok", [], await packing_slip(ctx, spec, stem, reply))


async def packing_slip(ctx: StageContext, spec: Spec, stem: str, reply: ImplementReply) -> PackingSlip:
    return PackingSlip(
        produced_by_spec_version=_major(spec),
        produced_at_sha=await ctx.git.rev_parse(ctx.repo, stem),
        stem=stem, branch=stem, outcome=reply.outcome, summary=reply.summary,
        run_record=f"{TICKETS_DIR}/{stem}/run.md",
    )


# --- Check --------------------------------------------------------------------------------------


class CommandResult(_Strict):
    argv: list[str]
    rc: int | None  # None: the command could not run or timed out -- fails closed like a nonzero exit
    tail: str
    base_red: bool = False


class Evidence(_Strict):
    """Everything the mechanical gates judge, gathered once per branch head (the stage layer's job)."""

    stem: NonBlank
    head_sha: NonBlank
    claimed: Literal["ok", "already_satisfied"]
    scope_fence: list[str]
    changed_files: list[str]
    inserted_lines: int
    verification: list[CommandResult]
    run_record: str | None  # the lifted run.md text, None when absent
    core_files: dict[str, bytes | None] = {}
    core_template: bytes = CORE_TEMPLATE
    core_findings: list[Finding] = []
    is_bug: bool = False
    bug_evidence: BugEvidence | None = None
    bug_findings: list[Finding] = []


def regression_result(stdout: str, rc: int | None) -> Literal["pass", "defect", "error"] | None:
    """Decode the complete common protocol, never a runner's diagnostic or exit vocabulary."""
    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate keys")
        return result

    if len(stdout) > OUTPUT_TAIL_CHARS:
        return None
    try:
        result = json.loads(stdout, object_pairs_hook=unique)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(result, dict) or set(result) != {"regression_result"}:
        return None
    value = result["regression_result"]
    if not isinstance(value, str) or value not in {"pass", "defect", "error"}:
        return None
    if (value == "pass" and rc == 0) or (value == "defect" and rc is not None and rc > 0):
        return value
    return "error" if value == "error" else None


def _bug_finding(message: str) -> Finding:
    return Finding(code="bug_evidence", message=message,
                   paved_road="supply durable ticket evidence and carries matching only changed test/fixture files;"
                              " provide a carried reproducer checking prerequisites and emitting the regression"
                              " result protocol: pass at head, defect at the overlaid base")


async def _gather_bug(ctx: StageContext, ticket: Ticket, evidence: Evidence, *, attempt: int,
                      stage: str) -> tuple[BugEvidence, list[Finding]]:
    worktree = ctx.worktree(ticket.stem)
    base = await ctx.git.merge_base(ctx.repo, MAIN, ticket.stem)
    findings, overlay = [], set()
    for prefix in ticket.carries:
        path = Path(prefix)
        if not prefix or path.is_absolute() or ".." in path.parts or path.as_posix() != prefix.rstrip("/"):
            findings.append(_bug_finding(f"invalid carries prefix {prefix!r}"))
            continue
        matched = [p for p in evidence.changed_files if _in_fence(p, [prefix])]
        if not matched:
            findings.append(_bug_finding(f"carries prefix {prefix!r} matches no branch-changed file"))
        for rel in matched:
            if not {"tests", "test", "fixtures", "fixture"}.intersection(Path(rel).parts[:-1]):
                findings.append(_bug_finding(f"carries path {rel!r} is not a test/fixture file;"
                                             " implementation paths cannot be carried"))
                continue
            tree = await ctx.git._run(ctx.repo, "ls-tree", evidence.head_sha, "--", rel)
            if not tree.startswith(("100644 blob ", "100755 blob ")):
                findings.append(_bug_finding(f"carries path {rel!r} is not a committed regular file"))
                continue
            overlay.add(rel)
    custody = []
    for root in (ctx.repo, worktree):
        directory = root / TICKETS_DIR / ticket.stem / "evidence"
        custody.extend(p.relative_to(root).as_posix() for p in directory.rglob("*")
                       if p.is_file() and not p.is_symlink()
                       and p.resolve().is_relative_to(directory.resolve()))
    record = BugEvidence(argv=[ctx.driver.redactor.scrub(arg) for arg in ticket.regression],
                         head_sha=evidence.head_sha, base_sha=base,
                         overlay_paths=sorted(overlay), evidence_paths=sorted(set(custody)))
    if not custody:
        findings.append(_bug_finding("durable evidence is missing under the ticket evidence/ directory"))
    if not ticket.regression or not ticket.carries:
        findings.append(_bug_finding("parsed regression command or carries set is missing"))
    if findings:
        return record, findings

    async def execute(root: Path, name: str) -> RegressionRun:
        try:
            rc, out, err = await ctx.exec_.run(list(ticket.regression), cwd=root,
                                              env=dict(ctx.env),
                                              timeout=ticket.stuck_minutes * 60.0)
        except (TimeoutError, ExecutableNotFound, OSError) as error:
            rc, out, err = None, "", f"regression execution failed: {error}"
        decoded = regression_result(out, rc)
        ctx.driver.spool.write(ticket.stem, attempt, f"{stage}/regression-{name}.txt",
                               f"$ {' '.join(ticket.regression)}\n[exit {rc}]\n--- stdout\n{out}\n--- stderr\n{err}")
        return RegressionRun(rc=rc, regression_result=decoded,
                             tail=ctx.driver.redactor.scrub(out + err)[-OUTPUT_TAIL_CHARS:])

    head = await execute(worktree, "head")
    assert ctx.config.worktree_root is not None
    base_worktree = ctx.config.worktree_root / ".regression-base" / ticket.stem
    if base_worktree.exists():
        await ctx.git.worktree_remove(ctx.repo, base_worktree)
    base_worktree.parent.mkdir(parents=True, exist_ok=True)
    await ctx.git.worktree_add_detached(ctx.repo, base_worktree, base)
    try:
        # Restore committed blobs (including executable mode), never dirty checkout copies.
        for rel in overlay:
            if any(p.is_symlink() for p in (base_worktree / rel).parents if p.is_relative_to(base_worktree)):
                return record.model_copy(update={"head": head}), [_bug_finding("base overlay traverses a symlink")]
        await ctx.git.restore(base_worktree, sorted(overlay), source=evidence.head_sha)
        base_run = await execute(base_worktree, "base")
    except GitError as error:
        return record.model_copy(update={"head": head}), [
            _bug_finding(ctx.driver.redactor.scrub(f"cannot overlay regression inputs: {error}"))]
    finally:
        await ctx.git.worktree_remove(ctx.repo, base_worktree)
    return record.model_copy(update={"head": head, "base": base_run}), findings


def _inserted(diff: str) -> int:
    return sum(line.startswith("+") and not line.startswith("+++") for line in diff.splitlines())


async def gather_evidence(
    ctx: StageContext, ticket: Ticket, claimed: Literal["ok", "already_satisfied"], *, attempt: int,
    stage: str = "check",
) -> Evidence:
    stem = ticket.stem
    worktree = ctx.worktree(stem)
    env = dict(ctx.env)
    results = []
    red: list[tuple[int, list[str]]] = []
    timeout = ticket.stuck_minutes * 60.0
    for n, argv in enumerate(ticket.verification, 1):
        try:
            rc, out, err = await ctx.exec_.run(list(argv), cwd=worktree, env=env, timeout=timeout)
        except TimeoutError:
            rc, out, err = None, "", f"timed out after the ticket's stuck budget ({ticket.stuck_minutes}m)"
        except ExecutableNotFound as e:
            rc, out, err = None, "", str(e)
        ctx.driver.spool.write(stem, attempt, f"{stage}/verify-{n:02d}.txt",
                               f"$ {' '.join(argv)}\n[exit {rc}]\n--- stdout\n{out}\n--- stderr\n{err}")
        tail = ctx.driver.redactor.scrub((out + err)[-OUTPUT_TAIL_CHARS:])
        results.append(CommandResult(argv=list(argv), rc=rc, tail=tail))
        if rc != 0:
            red.append((n, list(argv)))
    if red:
        base = await ctx.git.merge_base(ctx.repo, MAIN, stem)
        assert ctx.config.worktree_root is not None
        base_worktree = ctx.config.worktree_root / ".base" / stem
        if base_worktree.exists():
            await ctx.git.worktree_remove(ctx.repo, base_worktree)
        base_worktree.parent.mkdir(parents=True, exist_ok=True)
        await ctx.git.worktree_add_detached(ctx.repo, base_worktree, base)
        try:
            from tempo.storm import storm_producer

            box = storm_producer(root=ctx.config.state_dir / BOX_DIR, fs=ctx.fs,
                                 journal=ctx.driver.journal, clock=ctx.driver.clock)
            for n, argv in red:
                try:
                    rc, out, err = await ctx.exec_.run(argv, cwd=base_worktree, env=env, timeout=timeout)
                except TimeoutError:
                    rc, out, err = None, "", f"timed out after the ticket's stuck budget ({ticket.stuck_minutes}m)"
                except ExecutableNotFound as e:
                    rc, out, err = None, "", str(e)
                ctx.driver.spool.write(stem, attempt, f"{stage}/verify-{n:02d}-base.txt",
                                       f"$ {' '.join(argv)}\n[exit {rc}]\n--- stdout\n{out}\n--- stderr\n{err}")
                if rc != 0:
                    results[n - 1] = results[n - 1].model_copy(update={"base_red": True})
                    summary = (f"`{' '.join(argv)}` fails on the merge base {base[:8]} too;"
                               " fix main, not this ticket")
                    if not any(m.message_class == "failure_report" and m.origin == stem
                               and m.outcome == "base_red"
                               and m.summary.startswith(f"`{' '.join(argv)}` fails on the merge base ")
                               for m in box.messages()):
                        box.enqueue(message_class="failure_report", origin=stem, stage=stage,
                                    outcome="base_red", summary=summary,
                                    occurrence_id=arrival_id("base-red", stem, attempt, stage, n))
        finally:
            await ctx.git.worktree_remove(ctx.repo, base_worktree)
    evidence = await gather_safety_evidence(ctx, ticket, claimed)
    bug, bug_findings = (await _gather_bug(ctx, ticket, evidence, attempt=attempt, stage=stage)
                         if ticket.frontmatter.kind == "bug" else (None, []))
    return evidence.model_copy(update={"verification": results, "bug_evidence": bug,
                                       "bug_findings": bug_findings})


async def gather_safety_evidence(
    ctx: StageContext, ticket: Ticket, claimed: Literal["ok", "already_satisfied"],
) -> Evidence:
    """Gather the committed metadata once, without verification or base-worktree execution."""
    stem = ticket.stem
    worktree = ctx.worktree(stem)
    run_md = ctx.repo / TICKETS_DIR / stem / "run.md"
    changed = await ctx.git.diff_names(ctx.repo, MAIN, stem)
    return Evidence(
        stem=stem,
        head_sha=await ctx.git.rev_parse(ctx.repo, stem),
        claimed=claimed,
        scope_fence=list(ticket.scope_fence),
        changed_files=changed,
        inserted_lines=_inserted(await ctx.git.diff(ctx.repo, MAIN, stem)),
        verification=[],
        is_bug=ticket.frontmatter.kind == "bug",
        run_record=run_md.read_text() if run_md.is_file() else None,
        **await committed_core(worktree, ctx.config, ctx.git),
    )


def _report(code: str, findings: list[Finding]) -> GateReport:
    return GateReport(code=code, verdict="fail" if findings else "pass", findings=findings)


def _in_fence(path: str, prefixes: Sequence[str]) -> bool:
    return any(path == p or path.startswith(p if p.endswith("/") else p + "/") for p in prefixes)


class ScopeFenceGate:
    """The implement stage's write allowlist (section 9): the committed diff within `## Scope fence`.

    The stem's own outbox is exempt except `ticket.md`, which stays read-only to the agent (section 10).
    """

    code = "scope_fence"

    def check(self, artifact: Evidence, workspace: Path) -> GateReport:
        outbox = f"{TICKETS_DIR}/{artifact.stem}/"
        outside = [p for p in artifact.changed_files
                   if not _in_fence(p, artifact.scope_fence)
                   and not (p.startswith(outbox) and p != outbox + TICKET_FILE)]
        return _report(self.code, [
            Finding(code=self.code, path=p, message=f"{p} is outside `## Scope fence`",
                    paved_road="revert the edit and commit; if the criteria force it, reply premise_failed"
                               " naming the file so the fence is widened")
            for p in outside
        ])

class VerificationGate:
    """The ticket's own `## Verification` commands, with base-red failures attributed to main."""

    code = "verification"

    def check(self, artifact: Evidence, workspace: Path) -> GateReport:
        findings = [
            Finding(code=self.code, message=f"`{' '.join(r.argv)}` exited {r.rc}: {r.tail.strip() or '(no output)'}",
                    paved_road=f"make `{' '.join(r.argv)}` exit 0 on the branch and commit the fix")
            for r in artifact.verification if r.rc != 0 and not r.base_red
        ]
        empty = not artifact.changed_files
        if empty and artifact.claimed == "ok":
            findings.append(Finding(code=self.code, message="the branch carries no committed change",
                                    paved_road="commit code on the branch, or reply already_satisfied if every"
                                               " criterion already holds"))
        if not empty and artifact.claimed == "already_satisfied":
            findings.append(Finding(code=self.code, message="already_satisfied was claimed but the branch carries a"
                                    f" diff: {', '.join(artifact.changed_files)}",
                                    paved_road="reply ok for a branch with changes; already_satisfied commits nothing"))
        return _report(self.code, findings)


class RunRecordGate:
    """`run.md` present with the fixed section set and a closed-vocab `## Outcome` (section 13)."""

    code = "run_record"

    def check(self, artifact: Evidence, workspace: Path) -> GateReport:
        path = f"{TICKETS_DIR}/{artifact.stem}/run.md"
        road = "re-run Implement so the engine writes run.md with every section of section 13"
        if artifact.run_record is None:
            return _report(self.code, [Finding(code=self.code, path=path, message="run.md is missing",
                                               paved_road=road)])
        sections: dict[str, list[str]] = {}
        current = None
        for line in artifact.run_record.splitlines():
            if line.startswith("## "):
                current = line[3:].strip()
                sections[current] = []
            elif current is not None:
                sections[current].append(line)
        findings = []
        if tuple(sections) != RUN_RECORD_SECTIONS:
            findings.append(Finding(code=self.code, path=path, message=f"run.md sections are {list(sections)}",
                                    paved_road=f"use exactly {list(RUN_RECORD_SECTIONS)}, in order"))
        outcome = "\n".join(sections.get("Outcome", [])).strip()
        if outcome not in OUTCOMES:
            findings.append(Finding(code=self.code, path=path, message=f"`## Outcome` {outcome!r} is not an outcome",
                                    paved_road=f"use one of {sorted(OUTCOMES)}"))
        return _report(self.code, findings)


class DiffBudgetGate:
    """A mechanical cap on the reviewable diff, so the review prompt fits its bound (section 7)."""

    code = "diff_budget"

    def check(self, artifact: Evidence, workspace: Path) -> GateReport:
        files, inserted = len(artifact.changed_files), artifact.inserted_lines
        if files <= DIFF_BUDGET_FILES and inserted <= DIFF_BUDGET_INSERTED:
            return _report(self.code, [])
        return _report(self.code, [Finding(
            code=self.code,
            message=f"diff is {files} files / {inserted} inserted lines, over the budget of"
                    f" {DIFF_BUDGET_FILES} files / {DIFF_BUDGET_INSERTED} lines",
            paved_road="split the ticket into smaller stems, each under the diff budget",
        )])


class BugEvidenceGate:
    """A new test proves a defect only when its carried reproducer reaches the defect on base."""

    code = "bug_evidence"

    def check(self, artifact: Evidence, workspace: Path) -> GateReport:
        if not artifact.is_bug:
            return _report(self.code, [])
        record = artifact.bug_evidence
        findings = list(artifact.bug_findings)
        if record is None:
            findings.append(_bug_finding("fresh regression evidence is missing"))
        elif not findings:
            if not record.evidence_paths:
                findings.append(_bug_finding("durable ticket evidence is missing"))
            if record.head_sha != artifact.head_sha:
                findings.append(_bug_finding("regression evidence pins a different candidate head"))
            if record.head is None or record.head.rc != 0 or record.head.regression_result != "pass":
                findings.append(_bug_finding("regression head did not complete with exit 0 and pass"))
            if (record.base is None or record.base.rc is None or record.base.rc <= 0
                    or record.base.regression_result != "defect"):
                findings.append(_bug_finding("overlaid base did not complete with a positive exit and defect"))
        return GateReport(code=self.code, verdict="fail" if findings else "pass", findings=findings,
                          bug_evidence=record)


CHECK_GATES: tuple[ScopeFenceGate | VerificationGate | RunRecordGate | DiffBudgetGate | CoreDriftGate | BugEvidenceGate, ...] = (
    ScopeFenceGate(), VerificationGate(), RunRecordGate(), DiffBudgetGate(), CoreDriftGate(), BugEvidenceGate(),
)


def check_severity(ctx: StageContext, ticket: Ticket) -> dict[str, Severity]:
    """Config severity with the ticket's `gate_bypass` valve applied: a bypassed code is soft for this ticket."""
    severity = dict(ctx.driver.severity)
    for bypass in ticket.frontmatter.gate_bypass:
        severity[bypass.code] = "soft"
    if ticket.frontmatter.source == "box:retro_finding":
        # Admission provenance is required even when the ticket demotes schema checks.
        severity["ticket_schema"] = "hard"
    return severity


async def check(ctx: StageContext, ticket: Ticket, slip: PackingSlip, *, attempt: int) -> StageResult:
    """Invoicing: run the mechanical gates on the branch head, write + lift `checks.json`."""
    stem = ticket.stem
    evidence = await gather_evidence(ctx, ticket, slip.outcome, attempt=attempt)
    gated = run_gates(CHECK_GATES, evidence, ctx.worktree(stem), severity=check_severity(ctx, ticket))
    passed = gated.passed
    invoice = Invoice(
        produced_by_spec_version=CHECK_SPEC_VERSION, produced_at_sha=evidence.head_sha, stem=stem,
        passed=passed, changed_files=evidence.changed_files, inserted_lines=evidence.inserted_lines,
        bypassed=sorted({b.code for b in ticket.frontmatter.gate_bypass}),
        reports=list(gated.reports), verification=evidence.verification,
    )
    ctx.fs.write(ctx.worktree(stem) / TICKETS_DIR / stem / "checks.json",
                 (invoice.model_dump_json(indent=2) + "\n").encode())
    try:
        await lift_outbox(ctx, stem, "checks", attempt=attempt)
    except GitError as error:
        finding = Finding(code="verification", message=f"checks lift failed: {error}",
                          paved_road="repair the ticket-plane Git failure and rerun Check")
        return StageResult(outcome="gate_failed", artifact=invoice.model_copy(update={"passed": False}),
                           findings=[finding])
    if not passed:
        hard = [f for r in gated.hard_failures for f in r.findings]
        return StageResult(outcome="gate_failed", artifact=invoice, findings=hard)
    outcome = "already_satisfied" if slip.outcome == "already_satisfied" else "ok"
    return StageResult(outcome=outcome, artifact=invoice, findings=gated.findings)


# --- Review -------------------------------------------------------------------------------------


def review_stage(ctx: StageContext, ticket_text: str, diff: str) -> tuple[LlmStage, Spec]:
    """specs/review.md wired unchanged: the ticket and diff render through the one review spec."""
    spec = _spec(ctx, "review")

    def render_diff(_: object, findings: list[Finding]) -> str:
        inputs = {"ticket": ticket_text, "diff": diff, "retry_findings": findings_text(findings)}
        return render(spec, inputs)

    return LlmStage(surface="review", emits=ReviewVerdict, gates=[], render=render_diff), spec


async def review(ctx: StageContext, ticket: Ticket, invoice: Invoice, *, attempt: int) -> StageResult:
    """Inspect: one separate-session review of `git diff main...<stem>`, pinned to the reviewed SHA.

    approve -> ok; snag and rma -> gate_failed (the review surface is a gate, section 11); the
    verdict-keyed artifact lands in `review.md` either way.
    """
    stem = ticket.stem
    reviewed = await ctx.git.rev_parse(ctx.repo, stem)
    diff = await ctx.git.diff(ctx.repo, MAIN, stem)
    stage, spec = review_stage(ctx, (ctx.repo / ticket_path(stem)).read_text(), diff)
    try:
        # The effective ticket capability applies to every call in this attempt.
        result = await ctx.driver.run(
            stage, invoice, ticket=stem, attempt=attempt, workspace=ctx.worktree(stem),
            tier=ticket.frontmatter.agent_tier, effort=ticket.frontmatter.agent_effort,
            stuck_budget=ticket.stuck_minutes * 60.0)
    except RenderOverBound as e:
        return StageResult(outcome="premise_failed", artifact=None, findings=[e.finding])
    return await finish_review(ctx, ticket, spec, result, reviewed, attempt=attempt)


async def finish_review(ctx: StageContext, ticket: Ticket, spec: Spec, result: StageResult, reviewed: str, *,
                        attempt: int) -> StageResult:
    """After the call: the verdict-keyed artifact, pinned to the reviewed SHA, lands in `review.md`."""
    stem = ticket.stem
    if result.outcome != "ok":
        return result
    verdict = result.artifact
    assert isinstance(verdict, ReviewVerdict)
    artifact = EMITS_BY_VERDICT[verdict.verdict](
        produced_by_spec_version=_major(spec), produced_at_sha=reviewed, stem=stem, reviewed_sha=reviewed,
        verdict=verdict.verdict, summary=verdict.summary, findings=list(verdict.findings),
        spec_version=spec.meta.version, provider=result.provider, model=result.model,
    )
    ctx.fs.write(ctx.worktree(stem) / TICKETS_DIR / stem / "review.md", render_review(artifact).encode())
    await lift_outbox(ctx, stem, "review", attempt=attempt)
    if verdict.verdict == "approve":
        return _short(result, "ok", [], artifact)
    return _short(result, "gate_failed", list(verdict.findings), artifact)


# --- composition --------------------------------------------------------------------------------


@dataclass(frozen=True)
class StagesRun:
    """Every stage result of one run, in order; the run stops at the first stage not `ok`."""

    attempt: int
    results: dict[StageName, StageResult] = field(default_factory=dict)

    @property
    def last(self) -> tuple[StageName, StageResult]:
        return list(self.results.items())[-1]


async def run_stages(ctx: StageContext, ticket: Ticket) -> StagesRun:
    """Implement -> Check -> Review for one ticket, single worker, in the process holding the lock.

    `already_satisfied`, proven by Check's green verification on an empty diff, stops before Review:
    there is no diff to review and the ticket settles as a no-op (section 5 invariant 4).
    """
    boundary = ctx.boundary
    run = boundary.run or StagesRun(attempt=run_seq(ctx.driver.journal.read(), ticket.stem))
    boundary.ticket, boundary.next_stage = ticket, None
    for stage in ("implement", "check", "review"):
        if stage in run.results:
            if run.results[stage].outcome != "ok":
                return run
            continue
        if boundary.select is not None and not boundary.select(ticket, stage):
            boundary.run, boundary.next_stage = run, stage
            return run
        if stage == "implement":
            operation = implement(ctx, ticket, attempt=run.attempt)
        elif stage == "check":
            slip = run.results["implement"].artifact
            assert isinstance(slip, PackingSlip)
            operation = check(ctx, ticket, slip, attempt=run.attempt)
        else:
            invoice = run.results["check"].artifact
            assert isinstance(invoice, Invoice)
            operation = review(ctx, ticket, invoice, attempt=run.attempt)
        try:
            if boundary.select is None:
                result = await operation
            else:
                boundary.active = asyncio.create_task(operation)
                try:
                    result = await asyncio.shield(boundary.active)
                except asyncio.CancelledError:
                    await ctx.abort_current()
                    raise
                finally:
                    boundary.active = None
        except Exception as exc:
            from tempo.providers import ProviderCallError
            if not isinstance(exc, ProviderCallError):
                raise
            findings = await ctx.driver.provider_failure(exc, owner=ticket.stem, ticket=ticket.stem,
                sequence=run.attempt, surface='requisition_review', workspace=ctx.worktree(ticket.stem))
            result = StageResult(outcome='infra_error', artifact=None, findings=findings)
        run.results[stage] = result
        if result.outcome != "ok":
            break
    return run

HARVEST_TAIL_CHARS = 4_000


def run_wall_seconds(ctx: StageContext, stem: str) -> float | None:
    """Wall-clock seconds since the stem's latest `running` transition; None when it never ran."""
    started = next((e.ts for e in reversed(ctx.driver.journal.read()) if e.type == EventType.STATE_TRANSITION
                    and e.ticket == stem and e.body.get("to") == "running"), None)
    return None if started is None else (ctx.driver.clock() - datetime.fromisoformat(started)).total_seconds()


async def harvest(
    ctx: StageContext, stem: str, *, attempt: int, stage: str | None, terminal: str,
    findings: list[Finding], wall_seconds: float | None, kind: str = "harvest",
) -> None:
    """Extract the allowlisted failed-run record and lift it through the one outbox lane."""
    spool = ctx.config.state_dir / "spools" / stem / str(attempt)
    errors = list(spool.rglob("error.txt")) if spool.is_dir() else []
    newest_error = max(errors, key=lambda p: (p.stat().st_mtime_ns, str(p))) if errors else None
    reason = None if findings else (newest_error.read_text(errors="replace") if newest_error else terminal)
    # Prompts are inputs, not stage logs: Review's prompt carries the unreviewed code diff.
    logs = sorted(p for p in spool.rglob("*") if p.is_file() and p.name != "prompt.md") if spool.is_dir() else []
    stage_log_tail = "".join(p.read_text(errors="replace") for p in logs)[-HARVEST_TAIL_CHARS:]
    providers = ctx.config.state_dir / "spools" / "providers" / stem
    events = list(providers.rglob("events.jsonl")) if providers.is_dir() else []
    newest_events = max(events, key=lambda p: (p.stat().st_mtime_ns, str(p))) if events else None
    events_tail = (newest_events.read_text(errors="replace") if newest_events else "")[-HARVEST_TAIL_CHARS:]
    record = f"tickets/{stem}/run.md"
    artifact = Harvest(
        attempt=attempt, stage=stage, terminal=terminal, findings=findings, reason=reason,
        diff_stat=await ctx.git.diff_stat(ctx.repo, "main", stem),
        stage_log_tail=stage_log_tail, events_tail=events_tail,
        wall_seconds=wall_seconds,
        run_record=record if (ctx.repo / record).is_file() else None,
    )
    path = ctx.worktree(stem) / "tickets" / stem / "attempts" / str(attempt) / "harvest.json"
    ctx.fs.write(path, (artifact.model_dump_json(indent=2) + "\n").encode())
    await lift_outbox(ctx, stem, kind, attempt=attempt, only=f"attempts/{attempt}/harvest.json")


async def harvest_orphan(checkout: Checkout, stem: str, attempt: int) -> None:
    """Use the run-terminal harvest with an effects context; an orphan makes no model call."""
    driver = Driver.from_config(checkout.config, llm=cast(LLM, None), env=checkout.env,
                                clock=checkout.clock, sleep=checkout.sleep, fs=checkout.fs)
    driver.journal = checkout.journal
    driver.effects = Effects(checkout.journal)
    ctx = StageContext(repo=checkout.repo, config=checkout.config, env=checkout.env, exec_=checkout.exec_,
                       git=checkout.git, fs=checkout.fs, driver=driver, specs_dir=SPECS_DIR)
    await harvest(ctx, stem, attempt=attempt, stage=None, terminal="abandoned", findings=[], wall_seconds=None)
