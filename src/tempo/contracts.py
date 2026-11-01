"""Closed shapes for everything that crosses an agent boundary: manifests, replies, reply files."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SUBJECT = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$"
MAX_PROMPT_CHARS = 4096
MAX_INPUTS = 32


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Manifest(_Strict):
    subject: str = Field(pattern=SUBJECT)
    attempt: int = Field(ge=1, le=99)
    inputs: list[str] = Field(min_length=1, max_length=MAX_INPUTS)


class Reply(_Strict):
    written: list[str]
    status: Literal["ok", "failed"]

    @model_validator(mode="after")
    def _consistent(self):
        if self.status == "ok" and not self.written:
            raise ValueError("status ok needs at least one written path")
        if self.status == "failed" and self.written:
            raise ValueError("status failed carries no written paths")
        return self


class AuthorReply(_Strict):
    stem: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")
    ticket: str = Field(min_length=1)


class RequisitionFinding(_Strict):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    paved_road: str = Field(min_length=1)
    kind: Literal["spec_gap", "authoring_error"]
    unit: str | None = None
    path: str | None = None
    line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _unit(self):
        if self.kind == "authoring_error" and self.unit is not None:
            raise ValueError("authoring_error findings have unit null")
        return self


class RequisitionReply(_Strict):
    verdict: Literal["approve", "snag", "rma"]
    summary: str = Field(min_length=1)
    findings: list[RequisitionFinding]

    @model_validator(mode="after")
    def _findings(self):
        if (self.verdict == "approve") != (not self.findings):
            raise ValueError("findings are empty exactly for approve")
        return self


class ReviewFinding(_Strict):
    code: Literal["logic", "acceptance", "scope", "leak", "ticket"]
    path: str | None
    line: int | None = Field(ge=1)
    message: str = Field(min_length=1)
    paved_road: str = Field(min_length=1)


class ReviewReply(_Strict):
    verdict: Literal["approve", "snag", "rma"]
    summary: str = Field(min_length=1)
    findings: list[ReviewFinding]

    @model_validator(mode="after")
    def _findings(self):
        codes = {f.code for f in self.findings}
        if self.verdict == "approve" and self.findings:
            raise ValueError("approve carries no findings")
        if self.verdict == "snag" and not codes & {"logic", "acceptance", "scope", "leak"}:
            raise ValueError("snag needs a logic, acceptance, scope, or leak finding")
        if self.verdict == "rma" and "ticket" not in codes:
            raise ValueError("rma needs a ticket finding")
        return self


class TriageReply(_Strict):
    verdict: Literal["author", "tombstone", "decision"]
    link: str | None
    summary: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    evidence: list[str]
    reopen_after_days: int | None = Field(ge=1)

    @model_validator(mode="after")
    def _shape(self):
        if (self.verdict == "tombstone") != (self.link is not None):
            raise ValueError("link is set exactly for tombstone")
        if (self.verdict == "author") != (self.reopen_after_days is None):
            raise ValueError("reopen_after_days is null exactly for author")
        return self


class RetroProposal(_Strict):
    fixed_failure: str = Field(min_length=1)
    overcorrection_risk: str = Field(min_length=1)
    proposed_roles: list[str] = Field(min_length=1)


class RetroReply(_Strict):
    proposals: list[RetroProposal]


REPLIES: dict[str, type[BaseModel]] = {
    "author": AuthorReply,
    "requisition_review": RequisitionReply,
    "correctness_review": ReviewReply,
    "triage": TriageReply,
    "retro": RetroReply,
}
