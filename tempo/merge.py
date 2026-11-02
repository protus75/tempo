"""Inline merge and explicit serve admission (TEMPO_PLAN.md sections 7, 9, 10).

Per admission, all before main moves: restore the worktree's ticket plane -> rebase onto main ->
re-run the mechanical hard set on the rebased candidate -> require the pinned review approval ->
squash-merge with trailers -> journal `to: merged` -> delete the worktree and branch. Run/drain
admit inline; serve offers through AdmissionBoundary and its serial MergeQueue consumer.

A refusal leaves main untouched and the branch in place; journaling a non-ok terminal is the
runner's (section 11.2).
"""

import asyncio
import re
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tempo.daemon import PauseConsumer
    from tempo.mergequeue import ConflictHandoff, MergeQueue

from pydantic import BaseModel, ConfigDict

from tempo.artifacts import Artifact, Finding, NonBlank, StageResult
from tempo.gates import GateReport, run_gates
from tempo.git import RebaseRefused
from tempo.journal import Event, EventType
from tempo.stages import (
    CHECK_GATES,
    MAIN,
    ApprovedInvoice,
    StageContext,
    check_severity,
    gather_evidence,
    read_review,
)
from tempo.tickets import TICKETS_DIR, Ticket, TicketInvalid, ticket_path, validate_ticket

# Versions the merge contract in the provenance slot, like the mechanical Check.
MERGE_SPEC_VERSION = 1


def compose_pipeline(ctx: StageContext, *, escalate: Callable[[Event], None],
                     control: "PauseConsumer | None") -> "MergeQueue":
    """Compose admission without running it; CLI run dispatch still merges inline."""
    # The queue's priority helpers import drain, which imports runner and this module.
    from tempo.mergequeue import MergeQueue

    from tempo.runner import Refusal

    if control is None:
        raise Refusal("admission control consumer absent",
                      "construct it with `build_control` at the composition root and supply it as `Checkout.control`")
    return MergeQueue(ctx, escalate=escalate, control=control)


class Candidate(BaseModel):
    """What the merge-only gates judge: the rebased branch plus main's ticket and pinned review."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    stem: NonBlank
    reviewed_sha: NonBlank  # the branch head Review saw, before the rebase
    changed_files: list[str]
    ticket_text: str
    review_text: str | None  # main's `review.md`, None when absent
    provenance_findings: list[Finding] = []


class Admission(Artifact):
    """The admitted code commit and its pinned approval."""

    stem: NonBlank
    commit: NonBlank
    reviewed_sha: NonBlank


class TicketSchemaGate:
    """Main's `ticket.md` still passes the section 13 grammar: a hand edit mid-run is re-judged here."""

    code = "ticket_schema"

    def check(self, artifact: Candidate, workspace: Path) -> GateReport:
        try:
            validate_ticket(artifact.stem, artifact.ticket_text, workspace)
        except TicketInvalid as e:
            return GateReport(code=self.code, verdict="fail",
                              findings=[f.model_copy(update={"code": self.code}) for f in e.findings])
        return GateReport(code=self.code, verdict="fail" if artifact.provenance_findings else "pass",
                          findings=artifact.provenance_findings)


class PostRebaseGate:
    """The branch carries code only (section 10): a committed `tickets/**` path never rides the code lane."""

    code = "post_rebase_regate"

    def check(self, artifact: Candidate, workspace: Path) -> GateReport:
        findings = [
            Finding(code=self.code, path=p, message=f"the branch commits ticket-plane file {p}",
                    paved_road="leave outbox files uncommitted; the driver lifts them")
            for p in artifact.changed_files if p.startswith(f"{TICKETS_DIR}/")
        ]
        return GateReport(code=self.code, verdict="fail" if findings else "pass", findings=findings)


class ApprovalGate:
    """`correctness_review` participates as the pinned approval, never a re-executed call (section 9).

    The approval must name the pre-rebase head: it carries across a clean rebase, never across new commits.
    """

    code = "correctness_review"

    def check(self, artifact: Candidate, workspace: Path) -> GateReport:
        path = f"{TICKETS_DIR}/{artifact.stem}/review.md"
        road = f"re-run `run {artifact.stem}` so Review approves the current branch head"
        if artifact.review_text is None:
            return self._fail(path, "no review.md on main", road)
        try:
            review = read_review(artifact.review_text)
        except ValueError as e:
            return self._fail(path, f"review.md does not parse: {e}", road)
        if not isinstance(review, ApprovedInvoice):
            return self._fail(path, f"the pinned verdict is {review.verdict}, not approve", road)
        if review.reviewed_sha != artifact.reviewed_sha:
            return self._fail(path, f"the approval pins {review.reviewed_sha}, the branch head is"
                                    f" {artifact.reviewed_sha}", road)
        return GateReport(code=self.code, verdict="pass")

    def _fail(self, path: str, message: str, road: str) -> GateReport:
        return GateReport(code=self.code, verdict="fail",
                          findings=[Finding(code=self.code, path=path, message=message, paved_road=road)])


MERGE_GATES = (TicketSchemaGate(), PostRebaseGate(), ApprovalGate())


class AdmissionBoundary:
    """Explicit, idle composition and memory-only delivery."""

    def __init__(self, ctx: StageContext, *, mode: str = "inline",
                 queue: "MergeQueue | None" = None, waiting=nullcontext) -> None:
        from tempo.runner import Refusal

        if mode not in ("inline", "daemon") or (mode == "daemon" and queue is None):
            raise Refusal(f"invalid admission setup: mode={mode!r}",
                          "select inline or supply the composed MergeQueue with daemon mode at serve composition")
        self.ctx, self.mode, self.queue, self.waiting = ctx, mode, queue, waiting
        self.results: dict[tuple[str, int], asyncio.Future] = {}

    async def __call__(self, ticket: Ticket, *, attempt: int) -> "StageResult | ConflictHandoff":
        ctx = self.ctx
        if self.mode == "inline":
            return await merge(ctx, ticket, attempt=attempt)
        if findings := retro_bridge_findings(ctx, ticket):
            return _refused(findings)
        reviewed = await ctx.git.rev_parse(ctx.repo, ticket.stem)
        paths = await ctx.git.diff_names(ctx.repo, MAIN, ticket.stem)
        candidate = read_candidate(ctx, ticket, reviewed, paths)
        reports = run_gates((PostRebaseGate(),), candidate, ctx.repo, severity={"post_rebase_regate": "hard"})
        if reports.hard_failures:
            return _refused([f for r in reports.hard_failures for f in r.findings])
        assert self.queue is not None
        key = ticket.stem, attempt
        result = asyncio.get_running_loop().create_future()
        self.results[key] = result
        try:
            # Single-flight dispatch retains this run's snapshot until its receipt is consumed.
            self.queue.ctx = ctx
            self.queue.offer(ticket, attempt=attempt)
            async with self.waiting():
                return await result
        finally:
            self.results.pop(key, None)

    def deliver(self, ticket: Ticket, attempt: int, result: "StageResult | ConflictHandoff") -> bool:
        future = self.results.get((ticket.stem, attempt))
        if future is None:
            return False
        if not future.done():
            future.set_result(result)
        return True


def squash_message(ticket: Ticket, reviewed_sha: str) -> str:
    """Subject `tempo(<stem>): <Goal line>` plus the trailers reconcile maps a commit back by (section 10)."""
    goal = next(line.strip() for line in ticket.sections["Goal / Why"].splitlines() if line.strip())
    return f"tempo({ticket.stem}): {goal}\n\ntempo-ticket: {ticket.stem}\ntempo-reviewed-sha: {reviewed_sha}\n"


def _refused(findings: list[Finding]) -> StageResult:
    return StageResult(outcome="gate_failed", artifact=None, findings=findings)


def read_candidate(ctx: StageContext, ticket: Ticket, reviewed: str, changed_files: list[str]) -> Candidate:
    stem = ticket.stem
    review_md = ctx.repo / TICKETS_DIR / stem / "review.md"
    return Candidate(
        stem=stem, reviewed_sha=reviewed, changed_files=changed_files,
        ticket_text=(ctx.repo / ticket_path(stem)).read_text(),
        review_text=review_md.read_text() if review_md.is_file() else None,
        provenance_findings=retro_bridge_findings(ctx, ticket),
    )


def retro_bridge(ctx: StageContext, ticket: Ticket) -> dict | None:
    if ticket.frontmatter.source != "box:retro_finding":
        return None
    key = f"retro-ticket/{ticket.stem}"
    bridges = [e for e in ctx.driver.journal.read() if e.key == key or
               (e.body.get("kind") == "retro_ticket_authored" and e.body.get("ticket_stem") == ticket.stem)]
    fields = {"kind", "box_id", "retro_report_key", "ticket_stem"}
    if len(bridges) == 1:
        event = bridges[0]
        body = event.body
        if (event.type == EventType.SIGNAL and event.key == key and event.ticket == ticket.stem
                and set(body) == fields and body.get("kind") == "retro_ticket_authored"
                and body.get("ticket_stem") == ticket.stem
                and isinstance(body.get("box_id"), str) and re.fullmatch(r"box-[0-9]{6,}-[0-9a-f]{8}", body["box_id"])
                and isinstance(body.get("retro_report_key"), str)
                and re.fullmatch(r"retro/[0-9]+", body["retro_report_key"])):
            return body
    raise ValueError("retro admission requires one unique matching Author bridge; "
                     "restore the Author-produced retro-ticket/<stem> provenance from the journal before retrying")


def retro_bridge_findings(ctx: StageContext, ticket: Ticket) -> list[Finding]:
    try:
        retro_bridge(ctx, ticket)
    except ValueError as exc:
        return [Finding(code="ticket_schema", path=ticket_path(ticket.stem), message=str(exc),
                        paved_road="restore the unique Author-produced retro-ticket/<stem> bridge and retry admission")]
    return []


async def merge(ctx: StageContext, ticket: Ticket, *, attempt: int) -> StageResult:
    """Admit one reviewed branch to main, or refuse it with findings and main untouched."""
    stem = ticket.stem
    worktree = ctx.worktree(stem)
    reviewed = await ctx.git.rev_parse(ctx.repo, stem)

    # Lifted outbox copies show as tracked deletions in a worktree born from a main that already held
    # them, and a dirty tree refuses the rebase. Restoring to the branch's committed ticket plane (which
    # the branch never changes) makes the rebase land it on main's content; restoring main's content
    # BEFORE the rebase would itself dirty the index against the older branch head.
    await ctx.git.restore(worktree, [TICKETS_DIR], source=stem)
    try:
        await ctx.git.rebase(worktree, MAIN)
    except RebaseRefused as e:
        return _refused([Finding(code="post_rebase_regate", message=f"rebase onto main refused: {e.err.strip()}",
                                 paved_road=f"re-run `run {stem}` so Implement re-branches from current main")])

    evidence = await gather_evidence(ctx, ticket, "ok", attempt=attempt, stage="merge")
    candidate = read_candidate(ctx, ticket, reviewed, evidence.changed_files)
    severity = check_severity(ctx, ticket)
    gated = [run_gates(CHECK_GATES, evidence, worktree, severity=severity),
             run_gates(MERGE_GATES, candidate, ctx.repo, severity=severity)]
    if hard := [f for g in gated for r in g.hard_failures for f in r.findings]:
        return _refused(hard)

    admission = await write_squash(ctx, ticket, reviewed, attempt=attempt)
    await retire(ctx, stem)
    soft = [f for g in gated for f in g.findings]
    return StageResult(outcome="ok", artifact=admission, findings=soft)


async def write_squash(ctx: StageContext, ticket: Ticket, reviewed: str, *, attempt: int) -> Admission:
    """The shared admission Effect and sole merged terminal; retirement follows tree validation."""
    stem = ticket.stem
    bridge = retro_bridge(ctx, ticket)
    checked_tree = (await ctx.git.rev_parse(ctx.worktree(stem), "HEAD^{tree}")
                    if bridge is not None and ctx.worktree(stem).exists() else None)

    async def squash() -> dict:
        await ctx.git.merge_squash(ctx.repo, stem)
        await ctx.git.commit(ctx.repo, squash_message(ticket, reviewed))
        return {"commit": await ctx.git.rev_parse(ctx.repo, MAIN)}

    commit = (await ctx.driver.effects.run(squash, key="/".join(("merge", stem, str(attempt))), ticket=stem))["commit"]
    if (bridge is not None and
            (checked_tree is None or await ctx.git.rev_parse(ctx.repo, "main^{tree}") == checked_tree)):
        paths = sorted(p for p in (await ctx.git._run(ctx.repo, "diff-tree", "--no-commit-id", "--name-only",
                                                     "-r", commit)).splitlines()
                       if re.fullmatch(r"specs/[^/]+\.md", p))
        if paths:
            body = {"kind": "retro_prompt_spec_change_merged", "box_id": bridge["box_id"],
                    "retro_report_key": bridge["retro_report_key"], "ticket_stem": stem,
                    "squash_sha": commit, "changed_spec_paths": paths}
            key = f"retro-prompt-spec-change/{stem}/{commit}"
            prior = [e for e in ctx.driver.journal.read() if e.key == key]
            if prior and any(e.type != EventType.SIGNAL or e.body != body or e.ticket != stem for e in prior):
                raise ValueError("conflicting retro merge evidence; restore the merge-owned admission signal")
            if not prior:
                ctx.driver.journal.append(EventType.SIGNAL, body, ticket=stem, key=key)
    body = {"to": "merged", "commit": commit, "reviewed_sha": reviewed}
    if not any(e.type == EventType.STATE_TRANSITION and e.ticket == stem and e.body == body
               for e in ctx.driver.journal.read()):
        ctx.driver.journal.append(EventType.STATE_TRANSITION, body, ticket=stem)
    return Admission(produced_by_spec_version=MERGE_SPEC_VERSION,
                     produced_at_sha=commit,
                     stem=stem, commit=commit, reviewed_sha=reviewed)


async def retire(ctx: StageContext, stem: str) -> None:
    await ctx.git.worktree_remove(ctx.repo, ctx.worktree(stem))
    await ctx.git.branch_delete(ctx.repo, stem)
