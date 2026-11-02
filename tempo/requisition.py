"""Read-only authored-ticket feasibility review (TEMPO_PLAN.md sections 7, 8, 19.L)."""

import hashlib
import re
from collections.abc import Collection
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, model_validator

from tempo.artifacts import Artifact, Finding, NonBlank
from tempo.driver import Driver, _StuckBudget, unwrap_fence
from tempo.gates import GateReport
from tempo.llm import AgentTier, LLMRequest, LLMResult
from tempo.llmeffect import llm_call
from tempo.providers import ProviderCallError, ProviderDrought
from tempo.specs import REQ_RENDER_HEADROOM, RENDER_BOUND_CHARS, RenderOverBound, hardenable_units, load_spec, render
from tempo.stages import implement_inputs
from tempo.tickets import Ticket, validate_ticket

REQUISITION_STUCK_S = 600.0


def validate_finding_units(findings: list[Finding], units: Collection[str]) -> None:
    """Surface schema rule: a spec gap names one of the judged ticket's hardenable units."""
    for finding in findings:
        if finding.kind == "spec_gap" and (finding.unit not in units if units else finding.unit is not None):
            raise ValueError(f"spec_gap unit must be one of {sorted(units)}" if units
                             else "spec_gap unit must be null when the ticket has no hardenable units")


class RequisitionReply(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    verdict: Literal["approve", "snag", "rma"]
    summary: NonBlank
    findings: list[Finding]

    @model_validator(mode="after")
    def _findings_match_verdict(self, info: ValidationInfo) -> "RequisitionReply":
        if bool(self.findings) == (self.verdict == "approve"):
            raise ValueError("findings must be empty exactly for approve")
        if any(f.kind is None for f in self.findings):
            raise ValueError("every finding needs a kind: spec_gap or authoring_error")
        validate_finding_units(self.findings, (info.context or {}).get("hardenable_units", ()))
        return self


class RequisitionVerdict(Artifact):
    stem: NonBlank
    ticket_sha: NonBlank
    verdict: Literal["approve", "snag", "rma"]
    summary: NonBlank
    findings: list[Finding]
    render_chars: Annotated[int, Field(ge=0)]
    mechanical: str | None
    spec_version: NonBlank
    provider: str | None
    model: str | None


def review_target(repo: Path, stem: str, text: str, siblings: Collection[str] = ()) -> Ticket:
    """Use the complete ticket admission predicate, including reserved stems."""
    return validate_ticket(stem, text, repo, siblings)


def _base_render(repo: Path, plan: str, ticket: Ticket, text: str, specs_dir: Path) -> str:
    spec = load_spec((specs_dir / "implement.md").read_text())
    inputs = implement_inputs(repo, plan, text, ticket, ())
    return render(spec, {**inputs, "retry_findings": "none"})


def base_render_chars(repo: Path, plan: str, ticket: Ticket, text: str, specs_dir: Path) -> int:
    """Measure the same first-attempt Implement prompt that production renders, at max effort."""
    try:
        return len(_base_render(repo, plan, ticket, text, specs_dir))
    except RenderOverBound as exc:
        # The renderer measured the complete prompt before refusing it.
        return int(re.search(r"rendered prompt is (\d+) characters", exc.finding.message).group(1))


async def review_ticket(driver: Driver, *, repo: Path, plan: str, stem: str, text: str,
                        specs_dir: Path, tier: AgentTier, stem_slot: str, run_seq: int,
                        attempt: int, call_seq: int, expected_budget: float | Literal['surface', 'eval'],
                        prior: str = "none",
                        siblings: Collection[str] = ()) -> RequisitionVerdict:
    ticket = review_target(repo, stem, text, siblings)
    units = hardenable_units(plan, stem, ticket.plan_contract)
    spec = load_spec((specs_dir / "requisition_review.md").read_text())
    ticket_bytes = text.encode()
    sha = hashlib.sha1(f"blob {len(ticket_bytes)}\0".encode() + ticket_bytes).hexdigest()
    try:
        base = _base_render(repo, plan, ticket, text, specs_dir)
        chars = len(base)
    except RenderOverBound as exc:
        base = None
        chars = int(re.search(r"rendered prompt is (\d+) characters", exc.finding.message).group(1))
    common = dict(produced_by_spec_version=int(spec.meta.version.split(".")[0]),
                  produced_at_sha=sha, stem=stem, ticket_sha=sha, render_chars=chars,
                  spec_version=spec.meta.version)

    def snag(message: str, mechanical: str, code: str = "requisition_review",
             road: str = "repair the ticket or review response and retry") -> RequisitionVerdict:
        return RequisitionVerdict(**common, verdict="snag", summary=message,
                                  findings=[Finding(code=code, message=message, paved_road=road,
                                                    kind="authoring_error")],
                                  mechanical=mechanical, provider=None, model=None)

    if chars > REQ_RENDER_HEADROOM * RENDER_BOUND_CHARS:
        return snag(f"base Implement render has {chars} characters, over authoring headroom",
                    "render over headroom", "render_feasibility", "shrink or split the ticket at authoring")
    assert base is not None

    try:
        prompt = render(spec, {"ticket": text, "plan_contract": "included in render",
                               "context": "included in render", "render": base,
                               "prior_review": prior, "retry_findings": "none"})
    except RenderOverBound as exc:
        return snag(str(exc), "review render over bound", road="shrink or split the ticket at authoring")

    name = f"requisition_review/call-{call_seq:02d}"
    spool_stem = f"{stem_slot}/{run_seq}"
    driver.spool.write(spool_stem, attempt, f"{name}/prompt.md", prompt)
    req = LLMRequest(surface="requisition_review", rendered=prompt, tier=tier,
                     effort=spec.meta.effort, ticket=None, worktree=None)
    if driver._active is None:
        driver.begin_watch(owner=stem_slot, ticket=None, run_sequence=run_seq,
            surface='requisition_review', workspace=repo, expected_budget=expected_budget,
            stuck_budget=REQUISITION_STUCK_S, scope_fence=(), attempt=attempt)
    try:
        recorded = await driver.race(
            lambda: llm_call(driver.effects, driver.watched_llm(), req, driver.redactor, ticket=stem,
                             stem=stem_slot, run_seq=run_seq, attempt=attempt, call_seq=call_seq),
            REQUISITION_STUCK_S,
        )
    except _StuckBudget:
        return snag("requisition review timed out", "timeout", road="retry the authoring review")
    except (ProviderDrought, ProviderCallError):
        raise
    except Exception as exc:
        message = f"requisition review provider failed: {type(exc).__name__}: {exc}"
        driver.spool.write(spool_stem, attempt, f"{name}/error.txt", message)
        return snag(message, "provider error", road="repair the provider and retry the authoring review")
    result = LLMResult(**recorded)
    driver.spool.write(spool_stem, attempt, f"{name}/output.txt", result.text)
    try:
        reply = RequisitionReply.model_validate_json(unwrap_fence(result.text),
                                                    context={"hardenable_units": units})
    except ValidationError as exc:
        message = f"invalid requisition review reply: {exc.error_count()} schema error(s)"
        return RequisitionVerdict(**common, verdict="snag", summary=message,
                                  findings=[Finding(code="requisition_review", message=message,
                                                    paved_road="reply with a schema-valid requisition verdict",
                                                    kind="authoring_error")],
                                  mechanical="invalid reply", provider=result.provider, model=result.model)
    return RequisitionVerdict(**common, verdict=reply.verdict, summary=reply.summary,
                              findings=reply.findings, mechanical=None,
                              provider=result.provider, model=result.model)


class RequisitionGate:
    code = "requisition_review"

    def check(self, artifact: RequisitionVerdict, workspace: Path) -> GateReport:
        return GateReport(code=self.code, verdict="pass" if artifact.verdict == "approve" else "fail",
                          findings=[] if artifact.verdict == "approve" else artifact.findings)
