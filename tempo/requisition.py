"""Read-only authored-ticket feasibility review (TEMPO_PLAN.md sections 7, 8)."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tempo.artifacts import Artifact, Finding, NonBlank
from tempo.driver import LlmStage
from tempo.gates import GateReport
from tempo.specs import REQ_RENDER_HEADROOM, RENDER_BOUND_CHARS, RenderOverBound, Spec, load_spec, render
from tempo.stages import findings_text, implement_inputs
from tempo.tickets import Ticket, validate_ticket

REQUISITION_STUCK_S = 600.0


class RequisitionReply(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    verdict: Literal["approve", "snag", "rma"]
    summary: NonBlank
    findings: list[Finding]

    @model_validator(mode="after")
    def _findings_match_verdict(self) -> "RequisitionReply":
        if bool(self.findings) == (self.verdict == "approve"):
            raise ValueError("findings must be empty exactly for approve")
        if any(f.kind is None for f in self.findings):
            raise ValueError("every finding needs a kind: spec_gap or authoring_error")
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


def review_target(repo: Path, stem: str, text: str) -> Ticket:
    """Use the complete ticket admission predicate, including reserved stems."""
    return validate_ticket(stem, text, repo)


def _base_render(repo: Path, ticket: Ticket, text: str, specs_dir: Path) -> str:
    spec = load_spec((specs_dir / "implement.md").read_text())
    inputs = implement_inputs(repo, text, ticket, ())
    return render(spec, {**inputs, "retry_findings": "none"})


def base_render_chars(repo: Path, ticket: Ticket, text: str, specs_dir: Path) -> int:
    """Measure the same first-attempt Implement prompt that production renders, at max effort."""
    try:
        return len(_base_render(repo, ticket, text, specs_dir))
    except RenderOverBound as exc:
        # The renderer measured the complete prompt before refusing it.
        return int(re.search(r"rendered prompt is (\d+) characters", exc.finding.message).group(1))


@dataclass(frozen=True)
class Requisition:
    """One ticket's requisition review before its call: the verdict's shared fields and the prompt inputs."""

    spec: Spec
    text: str
    base: str | None  # the base Implement render; None when it is over the render bound
    common: dict

    def snag(self, message: str, mechanical: str, code: str = "requisition_review",
             road: str = "repair the ticket or review response and retry") -> RequisitionVerdict:
        return RequisitionVerdict(**self.common, verdict="snag", summary=message,
                                  findings=[Finding(code=code, message=message, paved_road=road,
                                                    kind="authoring_error")],
                                  mechanical=mechanical, provider=None, model=None)

    def precheck(self) -> RequisitionVerdict | None:
        """The mechanical snag decided before any call: a base Implement render over authoring headroom."""
        chars = self.common["render_chars"]
        if chars > REQ_RENDER_HEADROOM * RENDER_BOUND_CHARS:
            return self.snag(f"base Implement render has {chars} characters, over authoring headroom",
                             "render over headroom", "render_feasibility", "shrink or split the ticket at authoring")
        return None

    def render(self, findings: list[Finding]) -> str:
        assert self.base is not None
        return render(self.spec, {"ticket": self.text, "context": "included in render", "render": self.base,
                                  "retry_findings": findings_text(findings)})

    def stage(self) -> LlmStage:
        return LlmStage(surface="requisition_review", emits=RequisitionReply, gates=[],
                        render=lambda _, findings: self.render(findings))

    def verdict(self, reply: RequisitionReply, provider: str | None, model: str | None) -> RequisitionVerdict:
        return RequisitionVerdict(**self.common, verdict=reply.verdict, summary=reply.summary,
                                  findings=reply.findings, mechanical=None, provider=provider, model=model)

    def invalid(self, message: str, provider: str | None, model: str | None) -> RequisitionVerdict:
        return RequisitionVerdict(**self.common, verdict="snag", summary=message,
                                  findings=[Finding(code="requisition_review", message=message,
                                                    paved_road="reply with a schema-valid requisition verdict",
                                                    kind="authoring_error")],
                                  mechanical="invalid reply", provider=provider, model=model)


def requisition(repo: Path, stem: str, text: str, specs_dir: Path) -> Requisition:
    ticket = review_target(repo, stem, text)
    spec = load_spec((specs_dir / "requisition_review.md").read_text())
    ticket_bytes = text.encode()
    sha = hashlib.sha1(f"blob {len(ticket_bytes)}\0".encode() + ticket_bytes).hexdigest()
    try:
        base = _base_render(repo, ticket, text, specs_dir)
        chars = len(base)
    except RenderOverBound as exc:
        base = None
        chars = int(re.search(r"rendered prompt is (\d+) characters", exc.finding.message).group(1))
    common = dict(produced_by_spec_version=int(spec.meta.version.split(".")[0]),
                  produced_at_sha=sha, stem=stem, ticket_sha=sha, render_chars=chars,
                  spec_version=spec.meta.version)
    return Requisition(spec=spec, text=text, base=base, common=common)


class RequisitionGate:
    code = "requisition_review"

    def check(self, artifact: RequisitionVerdict, workspace: Path) -> GateReport:
        return GateReport(code=self.code, verdict="pass" if artifact.verdict == "approve" else "fail",
                          findings=[] if artifact.verdict == "approve" else artifact.findings)
