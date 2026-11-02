"""Artifact base model and stage result types (TEMPO_PLAN.md sections 4, 5)."""

from dataclasses import dataclass
import re
from typing import Annotated, Literal, get_args
from tempo.llm import AgentTier, AgentEffort

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

ARTIFACT_SCHEMA_VERSION = 1

Outcome = Literal[
    "ok",
    "already_satisfied",
    "invalid_artifact",
    "gate_failed",
    "premise_failed",
    "timeout",
    "infra_error",
    "budget_exceeded",
]
OUTCOMES: frozenset[str] = frozenset(get_args(Outcome))

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class Finding(_Strict):
    code: NonBlank
    path: str | None = None
    line: Annotated[int, Field(ge=1)] | None = None
    message: NonBlank
    paved_road: NonBlank  # required: a finding that cannot say what to do instead fails gate-lint
    # Section 7: a requisition_review finding names whether the spec or the authoring is at fault.
    kind: Literal["spec_gap", "authoring_error"] | None = None
    unit: str | None = None

    @model_validator(mode="after")
    def _unit_only_for_spec_gap(self) -> "Finding":
        if self.unit is not None and self.kind != "spec_gap":
            raise ValueError("unit is non-null only for kind spec_gap")
        return self


class Harvest(_Strict):
    """The closed, ticket-plane record extracted from one failed run."""

    attempt: Annotated[int, Field(ge=0)]
    stage: Literal["implement", "check", "review", "merge"] | None
    terminal: str
    findings: list[Finding]
    reason: str | None
    diff_stat: str
    stage_log_tail: str
    events_tail: str
    wall_seconds: float | None
    usd: float | None
    run_record: str | None

    @field_validator("terminal")
    @classmethod
    def _terminal_state(cls, value: str) -> str:
        from tempo.journal import TERMINAL_STATES

        if value not in TERMINAL_STATES:
            raise ValueError(f"terminal {value!r} is not one of {sorted(TERMINAL_STATES)}")
        return value

    @model_validator(mode="after")
    def _reason_only_without_findings(self) -> "Harvest":
        if self.findings and self.reason is not None:
            raise ValueError("reason is set only when findings is empty")
        return self


class Artifact(_Strict):
    """Base of every stage-emitted artifact except the ticket (section 5 versioning policy)."""

    artifact_schema_version: Annotated[int, Field(ge=0)] = ARTIFACT_SCHEMA_VERSION
    produced_by_spec_version: int
    produced_at_sha: NonBlank

    @field_validator("artifact_schema_version")
    @classmethod
    def _refuse_newer(cls, v: int) -> int:
        if v > ARTIFACT_SCHEMA_VERSION:
            raise ValueError(
                f"artifact_schema_version {v} is newer than this engine's {ARTIFACT_SCHEMA_VERSION};"
                " upgrade tempo to a release that reads it"
            )
        return v


SHAKEOUT_REPORT = "shakeout-report.json"
SHAKEOUT_SPEC_VERSION = 1


class ShakeoutEntry(_Strict):
    member: NonBlank
    group: NonBlank
    planted_fault: NonBlank
    expected: NonBlank
    observed: NonBlank
    producing_run: NonBlank
    auditor: list[NonBlank]
    green: bool

    @model_validator(mode="after")
    def _consistent(self) -> "ShakeoutEntry":
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", self.member):
            raise ValueError("member must be a snake-case id")
        if not re.fullmatch(r"[a-z][a-z0-9_-]*/[0-9]+", self.producing_run):
            raise ValueError("producing_run must be <bench stem>/<run seq>")
        if self.green != (self.observed == self.expected and not self.auditor):
            raise ValueError("green must match the observation and empty auditor")
        return self


class ShakeoutReport(Artifact):
    entries: list[ShakeoutEntry]

    @model_validator(mode="after")
    def _unique_members(self) -> "ShakeoutReport":
        ids = [entry.member for entry in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("member ids must be unique")
        return self


DAEMON_SOAK_REPORT = "daemon-soak-report.json"
DAEMON_SOAK_SPEC_VERSION = 1
DAEMON_SOAK_MEMBERS = (
    "worker_killed_mid_run",
    "conflict_resolution_rungs",
    "semantic_conflict_integration_red",
)
_DAEMON_SOAK_EXPECTED = {
    "worker_killed_mid_run": ("abandoned_alerted_then_merged", "alert"),
    "conflict_resolution_rungs": ("mechanical_and_rework_main_green", "resolved"),
    "semantic_conflict_integration_red": ("integration_red_main_green", "refused"),
}


class DaemonSoakEntry(_Strict):
    """One closed member's observation (TEMPO_PLAN.md 19.P3.daemon-soak)."""

    member: Literal["worker_killed_mid_run", "conflict_resolution_rungs",
                    "semantic_conflict_integration_red"]
    planted_fault: NonBlank
    expected: NonBlank
    observed: NonBlank
    disposition: Literal["alert", "resolved", "refused"]
    producing_run: str
    auditor: list[NonBlank]
    green: bool

    @model_validator(mode="after")
    def _consistent(self) -> "DaemonSoakEntry":
        from tempo.tickets import stem_findings

        stem, separator, sequence = self.producing_run.rpartition("/")
        if not separator or not re.fullmatch(r"[0-9]+", sequence) or stem_findings(stem):
            raise ValueError("producing_run must be <ticket stem>/<nonnegative run sequence>")
        if (self.expected, self.disposition) != _DAEMON_SOAK_EXPECTED[self.member]:
            raise ValueError(f"{self.member} requires expected/disposition {_DAEMON_SOAK_EXPECTED[self.member]}")
        if self.green != (self.observed == self.expected and not self.auditor):
            raise ValueError("green must match the observation and empty auditor")
        return self


class DaemonSoakReport(Artifact):
    entries: list[DaemonSoakEntry]

    @model_validator(mode="after")
    def _ordered_members(self) -> "DaemonSoakReport":
        if tuple(entry.member for entry in self.entries) != DAEMON_SOAK_MEMBERS:
            raise ValueError(f"entries must contain exactly {DAEMON_SOAK_MEMBERS}, in order")
        return self


RELIABILITY_BATTERY_REPORT = "reliability-battery-report.json"
RELIABILITY_BATTERY_SPEC_VERSION = 1
RELIABILITY_BATTERY_MEMBERS = (
    "classified_quota_exhaustion",
    "all_candidates_cooling_recovery",
    "unclassified_failure_preservation",
)
_RELIABILITY_BATTERY_EXPECTED = (
    "quota_classified_cooldown_persisted",
    "all_cooling_held_timer_fired_recovered",
    "unknown_failure_unclassified",
)


class ReliabilityBatteryEntry(_Strict):
    member: Literal["classified_quota_exhaustion", "all_candidates_cooling_recovery",
                    "unclassified_failure_preservation"]
    planted_fault: NonBlank
    expected: NonBlank
    observed: NonBlank
    producing_run: str
    auditor: list[NonBlank]
    green: bool

    @model_validator(mode="after")
    def _consistent(self) -> "ReliabilityBatteryEntry":
        from tempo.tickets import stem_findings

        stem, separator, sequence = self.producing_run.rpartition("/")
        if not separator or not re.fullmatch(r"[0-9]+", sequence) or stem_findings(stem):
            raise ValueError("producing_run must be <ticket stem>/<nonnegative run sequence>")
        if self.expected != _RELIABILITY_BATTERY_EXPECTED[RELIABILITY_BATTERY_MEMBERS.index(self.member)]:
            raise ValueError(f"{self.member} requires its fixed expected observation")
        if self.green != (self.observed == self.expected and not self.auditor):
            raise ValueError("green must match the observation and empty auditor")
        return self


class ReliabilityBatteryReport(Artifact):
    entries: list[ReliabilityBatteryEntry]

    @model_validator(mode="after")
    def _ordered_members(self) -> "ReliabilityBatteryReport":
        if tuple(entry.member for entry in self.entries) != RELIABILITY_BATTERY_MEMBERS:
            raise ValueError(f"entries must contain exactly {RELIABILITY_BATTERY_MEMBERS}, in order")
        return self


@dataclass(frozen=True)
class Cost:
    tokens: int | None = None  # None when a cli stream reports no usage
    seconds: float = 0.0
    attempts: int = 0
    usd: float = 0.0
    provider: str | None = None  # None for non-LLM effects
    model: str | None = None


@dataclass(frozen=True)
class StageResult:
    outcome: Outcome
    artifact: Artifact | None
    findings: list[Finding]
    cost: Cost

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"outcome {self.outcome!r} is not one of {sorted(OUTCOMES)}")


ReviewVerdictName = Literal["approve", "snag", "rma"]
DiagnosisVerdictName = Literal["retry", "escalate", "split", "reject", "abandon-human"]
DIAGNOSIS_VERDICTS = get_args(DiagnosisVerdictName)
DIAGNOSIS_MAX_LESSONS = 5
DIAGNOSIS_LESSON_CHARS = 300


class DiagnosisReply(_Strict):
    verdict: DiagnosisVerdictName
    lessons: Annotated[list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1,
                                                              max_length=DIAGNOSIS_LESSON_CHARS)]],
                       Field(min_length=1, max_length=DIAGNOSIS_MAX_LESSONS)]


class Diagnosis(Artifact):
    stem: NonBlank
    attempt: Annotated[int, Field(ge=0)]
    terminal: str
    stage: str | None
    verdict: DiagnosisVerdictName
    lessons: list[str]
    mechanical: str | None
    spec_version: NonBlank
    provider: str | None
    model: str | None


# specs/review.md's closed finding codes: `ticket` is the rma code, the rest are snag codes.
REVIEW_SNAG_CODES = frozenset({"logic", "acceptance", "scope", "leak"})


class ReviewVerdict(_Strict):
    """The review surface's model-facing reply (specs/review.md); the stage stamps provenance around it."""

    verdict: ReviewVerdictName
    summary: NonBlank
    findings: list[Finding]

    @model_validator(mode="after")
    def _findings_match_verdict(self) -> "ReviewVerdict":
        codes = {f.code for f in self.findings}
        if self.verdict == "approve" and self.findings:
            raise ValueError("approve carries an empty findings list; reply snag to block on a finding")
        if self.verdict == "snag" and not (codes and codes <= REVIEW_SNAG_CODES):
            raise ValueError(f"snag needs at least one finding, every code one of {sorted(REVIEW_SNAG_CODES)}")
        if self.verdict == "rma" and not (codes and codes <= {"ticket"}):
            raise ValueError("rma needs at least one finding, every code `ticket`")
        return self


class AuthorIdentity(_Strict):
    provider: NonBlank
    model: NonBlank
    tier: AgentTier


class Expected(_Strict):
    expected_verdict: Literal["approve", "snag"]
    defect_class: Literal["logic", "hidden_info_leak", "acceptance_mismatch", "scope_escape", "none"]
    defect: str  # the planted defect in one sentence; empty for a clean fixture
    author: AuthorIdentity

    @model_validator(mode="after")
    def _clean_iff_approve(self) -> "Expected":
        clean = self.defect_class == "none"
        if clean != (self.expected_verdict == "approve") or (self.defect != "" if clean else not self.defect.strip()):
            raise ValueError("a clean fixture is defect_class none, verdict approve, empty defect; a planted one"
                             " names its class, expects snag, and describes the defect")
        return self


REVIEW_BASELINE_REPORT = "review-baseline-report.json"
FiniteUSD = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class BaselineResult(_Strict):
    name: NonBlank
    expected: Expected
    outcome: Outcome
    verdict: ReviewVerdictName | None

    @model_validator(mode="after")
    def _scored(self):
        if (self.outcome == "ok") != (self.verdict is not None):
            raise ValueError("ok requires a scored verdict; other outcomes require null")
        return self


class GraphProblem(_Strict):
    id: NonBlank
    summary: NonBlank


class GraphTicket(_Strict):
    stem: NonBlank
    problem_id: NonBlank
    depends: list[NonBlank]
    goal: NonBlank


class AuthorGraph(_Strict):
    problems: Annotated[list[GraphProblem], Field(min_length=1)]
    tickets: Annotated[list[GraphTicket], Field(min_length=1)]

    @model_validator(mode="after")
    def _closed_graph(self):
        from tempo.tickets import stem_findings

        problems = [p.id for p in self.problems]
        stems = [t.stem for t in self.tickets]
        if len(set(problems)) != len(problems) or len(set(stems)) != len(stems):
            raise ValueError("problem ids and ticket stems must be unique")
        if {t.problem_id for t in self.tickets} != set(problems):
            raise ValueError("tickets must cover exactly the graph problems")
        edges = {t.stem: t.depends for t in self.tickets}
        for t in self.tickets:
            if stem_findings(t.stem) or len(t.depends) != len(set(t.depends)) or not set(t.depends) <= set(stems):
                raise ValueError("use valid stems and unique resolving dependencies")
        visiting, visited = set(), set()
        def visit(stem):
            if stem in visiting:
                raise ValueError("author graph must be acyclic")
            if stem in visited:
                return
            visiting.add(stem)
            for dep in edges[stem]:
                visit(dep)
            visiting.remove(stem)
            visited.add(stem)
        for stem in stems:
            visit(stem)
        return self


class BaselineRoute(_Strict):
    provider: NonBlank
    model: NonBlank


class BaselineSurface(_Strict):
    spec_major: Annotated[int, Field(ge=1)]
    rows: dict[AgentTier, BaselineRoute]

    @model_validator(mode="after")
    def _tiers(self):
        if set(self.rows) != set(get_args(AgentTier)):
            raise ValueError("identity requires all four tier rows")
        return self


class BaselineIdentity(_Strict):
    review: BaselineSurface
    author: BaselineSurface


class BaselineExercised(_Strict):
    surface: Literal["review"]
    tier: AgentTier
    effort: AgentEffort
    spec_version: NonBlank

    @field_validator("spec_version")
    @classmethod
    def _version(cls, value):
        if not re.fullmatch(r"[1-9][0-9]*\.[0-9]+", value):
            raise ValueError("use a major.minor spec version")
        return value


class ClassScore(_Strict):
    planted: Annotated[int, Field(ge=0)]
    caught: Annotated[int, Field(ge=0)]


class Unscored(_Strict):
    fixture: NonBlank
    outcome: Outcome


class BaselineScores(_Strict):
    planted: Annotated[int, Field(ge=0)]
    clean: Annotated[int, Field(ge=0)]
    catch_rate: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None
    known_bad_false_approve: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None
    clean_false_snag: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None
    by_class: dict[Literal["logic", "hidden_info_leak", "acceptance_mismatch", "scope_escape"], ClassScore]
    missed: list[NonBlank]
    false_snagged: list[NonBlank]
    unscored: list[Unscored]


class BaselineSignalBody(_Strict):
    signal: Literal["review_baseline"]
    verdict: Literal["GO", "NO_GO"]
    identity: BaselineIdentity
    exercised: BaselineExercised
    fixture_authors: list[AuthorIdentity]
    scores: BaselineScores


class BaselineSignal(_Strict):
    v: Literal[1]
    type: Literal["signal"]
    ts: NonBlank
    ticket: str | None
    key: str | None
    body: BaselineSignalBody

    @field_validator("v", mode="before")
    @classmethod
    def _integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("signal v must be the integer 1")
        return value

    @field_validator("ts")
    @classmethod
    def _timestamp(cls, value):
        from datetime import datetime
        from tempo.journal import render_ts
        if render_ts(datetime.fromisoformat(value)) != value:
            raise ValueError("use the journal UTC timestamp rendering")
        return value


class ReviewBaselineReport(_Strict):
    schema_version: Literal[1]
    complete: bool
    cap_usd: Literal[5.0]
    spent_usd: FiniteUSD
    results: list[BaselineResult]
    scores: BaselineScores
    author_graph: AuthorGraph | None
    verdict_signal: BaselineSignal

    @field_validator("schema_version", mode="before")
    @classmethod
    def _integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("schema_version must be the integer 1")
        return value

    @model_validator(mode="after")
    def _consistent(self):
        from eval.harness import GO_MIN_DEFECTS, GO_MIN_CLEAN, MIN_CATCH_RATE, MAX_FALSE_SNAG_RATE, score

        names = [r.name for r in self.results]
        if len(set(names)) != len(names):
            raise ValueError("fixture names must be unique")
        calculated = score(self.results)
        if self.scores.model_dump() != calculated or self.verdict_signal.body.scores.model_dump() != calculated:
            raise ValueError("report and signal scores must equal results")
        complete = (self.scores.planted >= GO_MIN_DEFECTS and self.scores.clean >= GO_MIN_CLEAN
                    and all(r.verdict is not None for r in self.results) and self.author_graph is not None)
        if self.complete != complete:
            raise ValueError("complete requires the GO floors, every verdict, and a completed graph")
        body = self.verdict_signal.body
        authors = sorted({r.expected.author for r in self.results}, key=lambda a: (a.provider, a.model, a.tier))
        if body.fixture_authors != authors:
            raise ValueError("signal fixture authors must match the results")
        if int(body.exercised.spec_version.split('.')[0]) != body.identity.review.spec_major:
            raise ValueError("exercised review spec major must match identity")
        reviewer = body.identity.review.rows[body.exercised.tier]
        if any((a.provider, a.model) == (reviewer.provider, reviewer.model) for a in authors):
            raise ValueError("fixture author must differ from reviewer")
        if body.verdict == "GO" and (not self.complete or self.scores.catch_rate < MIN_CATCH_RATE
                                     or self.scores.clean_false_snag > MAX_FALSE_SNAG_RATE):
            raise ValueError("GO requires complete evidence and passing numerical boundaries")
        return self


HOST_LOOP_REPORT = "host-loop-report.json"
EXIT_RECEIPT = "exit-receipt.json"
HOST_LOOP_MEMBERS = ("machine_ticket_merge", "report_to_regression_bug_loop", "escape_attribution")
HOST_LOOP_EXPECTED = dict(zip(HOST_LOOP_MEMBERS, (
    "machine_ticket_merged", "report_replayed_regression_bug_merged",
    "escape_attributed_by_squash_trailers")))


class HostLoopEntry(_Strict):
    member: Literal["machine_ticket_merge", "report_to_regression_bug_loop", "escape_attribution"]
    producing_run: NonBlank
    expected: NonBlank
    observed: NonBlank
    engine_plane_edits: list[NonBlank]
    auditor: list[NonBlank]
    green: bool

    @model_validator(mode="after")
    def _consistent(self):
        from tempo.tickets import stem_findings
        stem, separator, sequence = self.producing_run.rpartition("/")
        if not separator or not re.fullmatch(r"[0-9]+", sequence) or stem_findings(stem):
            raise ValueError("producing_run must be <ticket stem>/<nonnegative run sequence>")
        if self.expected != HOST_LOOP_EXPECTED[self.member]:
            raise ValueError("use the member's fixed expected observation")
        if self.green != (self.observed == self.expected and not self.engine_plane_edits and not self.auditor):
            raise ValueError("green requires matching observation, no engine edits and no auditor findings")
        return self


class HostLoopReport(_Strict):
    schema_version: Literal[1]
    entries: list[HostLoopEntry]

    @field_validator("schema_version", mode="before")
    @classmethod
    def _integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("schema_version must be integer 1")
        return value

    @model_validator(mode="after")
    def _ordered(self):
        merges = self.entries[:-2]
        if (len(merges) < 3 or any(e.member != HOST_LOOP_MEMBERS[0] for e in merges)
                or tuple(e.member for e in self.entries[-2:]) != HOST_LOOP_MEMBERS[1:]
                or len({e.producing_run for e in merges}) != len(merges)):
            raise ValueError("use three distinct merge runs followed by regression and escape members")
        return self


class ExitReads(_Strict):
    machine_ticket_merge: Literal["pass", "fail"]
    report_to_regression_bug_loop: Literal["pass", "fail"]
    escape_attribution: Literal["pass", "fail"]
    go_grade_report: Literal["pass", "fail"]


class ExitReceipt(_Strict):
    schema_version: Literal[1]
    reads: ExitReads
    host_loop_digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    baseline_report_digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    verdict_signal: BaselineSignal

    @field_validator("schema_version", mode="before")
    @classmethod
    def _integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("schema_version must be integer 1")
        return value
