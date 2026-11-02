"""Gate protocol, runner, and gate-lint (TEMPO_PLAN.md sections 5, 7).

Severity never lives in a gate: the runner applies it from config (section 5 invariant 3).
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tempo.artifacts import Finding
from tempo.config import Config, ConfigSnapshot, Severity

ENGINE_GATE_CODES: frozenset[str] = frozenset(
    {
        "ticket_schema",
        "scope_fence",
        "verification",
        "run_record",
        "diff_budget",
        "post_rebase_regate",
        "bug_evidence",
        "core_drift",
        "correctness_review",
        "requisition_review",
    }
)

# Shipped MERGE-context default (section 15): every engine-shipped code is hard at merge.
DEFAULT_GATE_SEVERITY: Mapping[str, Severity] = {code: "hard" for code in ENGINE_GATE_CODES}


class GateContractError(Exception):
    """A gate broke the closed protocol; this is an engine defect, never the artifact's failure."""


class RegressionRun(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    rc: int | None
    regression_result: Literal["pass", "defect", "error"] | None
    tail: str


class BugEvidence(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    argv: list[str]
    head_sha: str
    base_sha: str
    overlay_paths: list[str]
    evidence_paths: list[str]
    head: RegressionRun | None = None
    base: RegressionRun | None = None


class GateReport(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    code: str
    verdict: Literal["pass", "fail"]
    findings: list[Finding] = []
    autofix_applied: bool = False
    bug_evidence: BugEvidence | None = Field(default=None, exclude_if=lambda v: v is None)


class Gate(Protocol):
    code: str

    def check(self, artifact: BaseModel, workspace: Path) -> GateReport: ...


@dataclass(frozen=True)
class GateRunResult:
    reports: list[GateReport]
    hard_failures: list[GateReport] = field(default_factory=list)
    soft_failures: list[GateReport] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.hard_failures

    @property
    def findings(self) -> list[Finding]:
        return [f for r in self.hard_failures + self.soft_failures for f in r.findings]


def merge_severity(config: Config | ConfigSnapshot | None) -> dict[str, Severity]:
    """The merge-context severity map: shipped all-hard default overlaid by `review.gate_severity`."""
    overrides = config.review.gate_severity if config is not None else {}
    return {**DEFAULT_GATE_SEVERITY, **overrides}


def run_gates(
    gates: Iterable[Gate],
    artifact: BaseModel,
    workspace: Path,
    *,
    severity: Mapping[str, Severity] | None = None,
) -> GateRunResult:
    severity = merge_severity(None) if severity is None else severity
    reports, hard, soft = [], [], []
    for gate in gates:
        if gate.code not in severity:
            raise GateContractError(
                f"gate {gate.code!r} has no severity; declare it under review.gate_severity"
                " (engine codes) or its review entry's severity (host checks)"
            )
        report = gate.check(artifact, workspace)
        if problems := _report_problems(gate, report):
            raise GateContractError("; ".join(problems))
        reports.append(report)
        if report.verdict == "fail":
            (hard if severity[gate.code] == "hard" else soft).append(report)
    return GateRunResult(reports=reports, hard_failures=hard, soft_failures=soft)


def lint_gate(
    gate: Gate,
    samples: Iterable[BaseModel],
    workspace: Path,
    *,
    codes: Collection[str] = ENGINE_GATE_CODES,
) -> list[str]:
    """Run `gate` over `samples` and return every protocol violation (empty = lint passes).

    The samples must include at least one the gate fails, so the paved roads are actually exercised.
    """
    problems = []
    if gate.code not in codes:
        problems.append(f"gate code {gate.code!r} is not in the closed vocabulary; use one of {sorted(codes)}")
    failed = False
    for i, sample in enumerate(samples):
        report = gate.check(sample, workspace)
        failed |= report.verdict == "fail"
        problems += [f"sample {i}: {p}" for p in _report_problems(gate, report)]
    if not failed:
        problems.append(f"gate {gate.code!r}: no failing sample; add one so its findings' paved roads are checked")
    return problems


def _report_problems(gate: Gate, report: GateReport) -> list[str]:
    # Re-validate from a dump: model_construct (or any bypass) must not smuggle a road-less finding through.
    try:
        report = GateReport.model_validate(report.model_dump())
    except ValidationError as e:
        return [
            f"gate {gate.code!r} report invalid at {'.'.join(map(str, err['loc']))}: {err['msg']};"
            " every finding needs a non-blank paved_road telling the agent what to do instead"
            for err in e.errors()
        ]
    problems = []
    if report.code != gate.code:
        problems.append(f"gate {gate.code!r} returned a report coded {report.code!r}; report its own code")
    if report.verdict == "fail" and not report.findings:
        problems.append(f"gate {gate.code!r} failed with no findings; emit a Finding with a paved_road")
    return problems
