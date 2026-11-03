"""Durable Suggestion Box and decision registry formats (plan section 12)."""

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated, Literal, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from tempo.journal import EventType, Journal
from tempo.inbox import CAPS, EvidencePaths, ReportDescriptor, SelfReport, bounded
from tempo.seams import FileSystem

BOX_DIR = "box"
MessageClass = Literal["suggestion", "failure_report", "override_report", "retro_finding", "bug_report"]
MESSAGE_CLASSES = get_args(MessageClass)
DECISIONS_DIR = "tickets/decisions"


class BoxError(Exception):
    """A box message or registry record is missing or invalid."""


class RereportCallbackRequired(BoxError):
    """The reopen signal must be durable before the queue is reopened."""


def normalize(reason: str) -> str:
    kept = " ".join(token for token in reason.split() if "/" not in token)
    return " ".join(re.sub(r"\d+", "", kept).split())


def signature(*fields: str, reason: str) -> str:
    payload = json.dumps([*fields, normalize(reason)], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


NonBlank = Annotated[str, Field(min_length=1, pattern=r"\S")]


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class Verdict(_Strict):
    verdict: Literal["author", "tombstone", "decision"]
    produced_by_spec_version: NonBlank
    rationale: NonBlank


class Resolution(_Strict):
    kind: Literal["ticket", "tombstone", "decision"]
    link: NonBlank


class Message(_Strict):
    id: NonBlank
    seq: Annotated[int, Field(ge=1)]
    signature: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    message_class: MessageClass
    summary: NonBlank
    origin: NonBlank
    stage: str | None
    outcome: str | None
    bug_origin: Literal["self_diagnosed", "player"] | None
    has_repro: bool | None
    status: Literal["pending", "resolved"]
    verdict: Verdict | None
    resolution: Resolution | None
    retro_report_key: NonBlank | None = None
    fixed_failure: NonBlank | None = None
    overcorrection_risk: NonBlank | None = None
    proposed_spec_paths: list[Annotated[str, Field(pattern=r"^tempo/specs/[a-z0-9_-]+\.md$")]] = []
    reports: Annotated[int, Field(ge=0)] = 0
    reopen: bool = False
    report: ReportDescriptor | None = None
    report_arrivals: list[NonBlank] = []

    @model_validator(mode="after")
    def _coherent(self) -> "Message":
        if self.report is not None and (self.message_class != "bug_report"
                or self.bug_origin != self.report.report.origin
                or self.has_repro != (self.report.custody.replay is not None)):
            raise ValueError("host report descriptor must agree with bug origin and copied replay")
        if self.report_arrivals and self.report is None:
            raise ValueError("report arrival receipts require host report custody")
        if (self.stage is None) != (self.outcome is None):
            raise ValueError("stage and outcome must both be set or both be null")
        if (self.bug_origin is not None and self.has_repro is not None) != (self.message_class == "bug_report"):
            raise ValueError("bug_origin and has_repro are required exactly for bug_report")
        if (self.status == "resolved") != (self.resolution is not None):
            raise ValueError("resolution is required exactly when resolved")
        evidence = (self.retro_report_key, self.fixed_failure, self.overcorrection_risk)
        if self.message_class == "retro_finding":
            if any(value is None for value in evidence) or not self.proposed_spec_paths:
                raise ValueError("retro_finding requires completed report and proposal evidence")
            if re.fullmatch(r"retro/[0-9]+", self.retro_report_key) is None:
                raise ValueError("retro_report_key must name a completed retro/<seq> effect")
        elif any(value is not None for value in evidence) or self.proposed_spec_paths:
            raise ValueError("proposal evidence is allowed only for retro_finding")
        return self


class RereportJournal:
    """Write-ahead receipts make each collapsing arrival count once across crashes."""

    def __init__(self, journal: Journal) -> None:
        self.journal = journal

    def __call__(self, message: Message) -> None:
        body = {"kind": "tombstone_auto_reopened", "box_id": message.id,
                "signature": message.signature, "reports": message.reports}
        key = f"tombstone-reopen/{message.id}/{message.reports}"
        prior = [e for e in self.journal.read() if e.key == key]
        if prior:
            if any(e.type != EventType.SIGNAL or e.body != body or e.ticket is not None for e in prior):
                raise BoxError("conflicting reopen evidence; restore the journal-backed reopen signal")
            return
        self.journal.append(EventType.SIGNAL, body, key=key)

    def record(self, message: Message, occurrence_id: str, apply: Callable[[int], None]) -> None:
        history = self.journal.read()
        key = f"box-rereport/{message.id}/{occurrence_id}"
        events = [e for e in history if e.key == key]
        if any(e.type == EventType.EFFECT_COMPLETION for e in events):
            return
        intent = next((e for e in events if e.type == EventType.EFFECT_INTENT), None)
        if intent is not None and (set(intent.body) != {"reports"}
                or type(intent.body["reports"]) is not int or intent.body["reports"] < 1):
            raise BoxError("invalid re-report receipt; restore its journal-backed occurrence evidence")
        if message.resolution is None or message.resolution.kind != "tombstone":
            if intent is not None and message.reopen and message.reports >= intent.body["reports"]:
                self.journal.append(EventType.EFFECT_COMPLETION,
                                    {"result": {"reports": intent.body["reports"]}}, key=key)
            return
        arrivals = [e for e in history if e.type == EventType.SIGNAL
                    and e.body.get("kind") == "storm_occurrence"
                    and e.body.get("signature") == message.signature]
        original = arrivals[0] if arrivals else None
        arrival = next((e for e in arrivals if e.body["occurrence_id"] == occurrence_id), None)
        tombstone = next((e for e in history if e.type == EventType.EFFECT_COMPLETION
                          and e.key == f"ticket-plane/decisions/{message.resolution.link}"), None)
        if arrival is not None and (arrival == original or
                (tombstone is not None and history.index(arrival) < history.index(tombstone))):
            return
        if intent is None:
            reserved = [e.body["reports"] for e in history if e.type == EventType.EFFECT_INTENT
                        and e.key is not None and e.key.startswith(f"box-rereport/{message.id}/")]
            reports = max([message.reports, *reserved]) + 1
            intent = self.journal.append(EventType.EFFECT_INTENT, {"reports": reports}, key=key)
        apply(intent.body["reports"])
        self.journal.append(EventType.EFFECT_COMPLETION, {"result": {"reports": intent.body["reports"]}}, key=key)


class Box:
    def __init__(self, root: Path, fs: FileSystem, *,
                 arrival: Callable[..., object] | None = None,
                 recover: Callable[[], None] | None = None,
                 rereport: RereportJournal | None = None) -> None:
        self.root = root
        self.fs = fs
        self._arrival = arrival
        self._recover = recover
        self._rereport = rereport

    def messages(self) -> list[Message]:
        result = []
        for path in self.root.glob("*.json"):
            try:
                result.append(Message.model_validate_json(path.read_text()))
            except (OSError, ValidationError, ValueError) as exc:
                raise BoxError(f"invalid box file {path}: {exc}") from exc
        return sorted(result, key=lambda message: message.seq)

    def pending(self) -> list[Message]:
        return [message for message in self.messages() if message.status == "pending"]

    def get(self, id: str) -> Message:
        for message in self.messages():
            if message.id == id:
                return message
        raise BoxError(f"unknown box id {id}")

    def _path(self, message: Message) -> Path:
        return self.root / f"{message.seq:06d}-{message.signature[:8]}.json"

    def _write(self, message: Message) -> None:
        self.fs.write(self._path(message), message.model_dump_json().encode())

    def enqueue(
        self, *, message_class: MessageClass, origin: str, summary: str,
        stage: str | None = None, outcome: str | None = None, reason: str | None = None,
        bug_origin: Literal["self_diagnosed", "player"] | None = None, has_repro: bool | None = None,
        occurrence_id: str | None = None,
        retro_report_key: str | None = None, fixed_failure: str | None = None,
        overcorrection_risk: str | None = None, proposed_spec_paths: Sequence[str] = (),
    ) -> tuple[str, bool]:
        if self._arrival is not None and (not isinstance(occurrence_id, str) or not occurrence_id.strip()):
            raise BoxError("occurrence_id must be a nonblank producer-supplied string; "
                           "supply a fresh id for a distinct arrival and reuse it on retry")
        if (stage is None) != (outcome is None):
            raise BoxError("stage and outcome must both be set or both be null")
        fields = (message_class, origin) if stage is None else (message_class, origin, stage, outcome)
        digest = signature(*fields, reason=summary if reason is None else reason)
        try:
            message = Message(
                id=f"box-000001-{digest[:8]}", seq=1, signature=digest, message_class=message_class, summary=summary,
                origin=origin, stage=stage, outcome=outcome, bug_origin=bug_origin, has_repro=has_repro,
                status="pending", verdict=None, resolution=None,
                retro_report_key=retro_report_key, fixed_failure=fixed_failure,
                overcorrection_risk=overcorrection_risk, proposed_spec_paths=list(proposed_spec_paths),
            )
        except ValidationError as exc:
            raise BoxError(f"invalid box message: {exc}") from exc
        return self._enqueue(message, occurrence_id)

    def _enqueue(self, message: Message, occurrence_id: str | None) -> tuple[str, bool]:
        digest = message.signature
        existing = self.messages()
        if self._arrival is not None:
            self._arrival(signature=digest, occurrence_id=occurrence_id,
                          emitting_stage=message.stage, emitting_origin=message.origin)
            # An arrival may publish its trip report before this source message.
            existing = self.messages()
        for prior in existing:
            if prior.signature == digest:
                if message.report is not None and occurrence_id in prior.report_arrivals:
                    return prior.id, False
                self.record_rereport(prior.id, occurrence_id=occurrence_id)
                if message.report is not None:
                    prior = self.get(prior.id)
                    self._write(prior.model_copy(update={"report_arrivals": [*prior.report_arrivals, occurrence_id]}))
                return prior.id, False
        seq = max((prior.seq for prior in existing), default=0) + 1
        id = f"box-{seq:06d}-{digest[:8]}"
        message = message.model_copy(update={"id": id, "seq": seq})
        self._write(message)
        return id, True

    def report_custody(self, arrival: str) -> ReportDescriptor | None:
        if re.fullmatch(r"[a-zA-Z0-9._:-]+", arrival) is None or arrival in {".", ".."}:
            raise BoxError("invalid arrival identity; supply a stable single filename token")
        path = self.root / "custody" / arrival / "report.json"
        return ReportDescriptor.model_validate_json(path.read_bytes()) if path.exists() else None

    def custody_report(self, arrival: str, report, evidence: dict[str, bytes]) -> ReportDescriptor:
        if existing := self.report_custody(arrival):
            return existing
        folder = Path("custody") / arrival
        paths = {}
        for kind, data in evidence.items():
            if kind not in CAPS or len(data) > CAPS[kind]:
                raise BoxError("invalid custody evidence; supply validated bounded replay/log bytes")
            path = folder / ("replay.json" if kind == "replay" else "log.txt")
            self.fs.write(self.root / path, data)
            paths[kind] = path.as_posix()
        descriptor = ReportDescriptor(report=report, custody=EvidencePaths(**paths))
        # The manifest is the publication boundary for a complete custody snapshot.
        self.fs.write(self.root / folder / "report.json", descriptor.model_dump_json().encode())
        return descriptor

    def enqueue_report(self, descriptor: ReportDescriptor, *, occurrence_id: str) -> tuple[str, bool]:
        if not occurrence_id.strip():
            raise BoxError("supply a stable nonblank report arrival identity")
        for kind in CAPS:
            if (value := getattr(descriptor.custody, kind)) is not None:
                bounded(self.root, value, kind)
        report = descriptor.report
        fields = ([report.game_slug, report.anomaly_code, report.location, report.app_version]
                  if isinstance(report, SelfReport) else ["bug_report", "player", occurrence_id])
        digest = hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()
        message = Message(id=f"box-000001-{digest[:8]}", seq=1, signature=digest,
            message_class="bug_report", summary=report.summary, origin="host-report",
            stage=None, outcome=None, bug_origin=report.origin,
            has_repro=descriptor.custody.replay is not None, status="pending", verdict=None,
            resolution=None, report=descriptor, report_arrivals=[occurrence_id])
        return self._enqueue(message, occurrence_id)

    def recover(self) -> None:
        if self._recover is not None:
            self._recover()

    def record_rereport(self, id: str, *, occurrence_id: str | None = None) -> None:
        message = self.get(id)
        if self._rereport is None and (message.resolution is None or message.resolution.kind != "tombstone"):
            return

        def apply(reports: int) -> None:
            counted = message.model_copy(update={"reports": max(message.reports, reports)})
            self._write(counted)
            if counted.reports >= 3:
                if self._rereport is None:
                    raise RereportCallbackRequired(
                        "tombstone reopening requires a journal-backed callback; "
                        "supply rereport=RereportJournal(journal) to Box")
                self._rereport(counted)
                self._write(counted.model_copy(update={"status": "pending", "resolution": None,
                                                       "verdict": None, "reopen": True}))

        if self._rereport is None:
            apply(message.reports + 1)
        else:
            self._rereport.record(message, occurrence_id or f"report/{message.reports + 1}", apply)

    def confirmed_tombstones(self, policy=None) -> list[Message]:
        from tempo.config import BOX_POLICY_DEFAULTS
        from tempo.policy import policy_row

        rows = BOX_POLICY_DEFAULTS if policy is None else policy
        return [m for m in self.messages() if m.resolution is not None
                and m.resolution.kind == "tombstone"
                and rows[policy_row(m.message_class, m.bug_origin, m.has_repro)] == "confirmed"]

    def publish_storm_report(self, body: dict, count: int) -> tuple[str, bool]:
        return self.enqueue(
            message_class="failure_report", origin="storm-breaker/" + body["trip_id"],
            summary=(f"P0 storm breaker trip: signature {body['signature']}; trip {body['trip_id']}; "
                     f"{count} occurrences in one-hour window; emitting stage "
                     f"{body['emitting_stage']!r}, origin {body['emitting_origin']!r}"),
            reason="storm breaker trip", occurrence_id="storm-report/" + body["trip_id"],
        )

    def record_verdict(self, id: str, verdict: Verdict) -> None:
        message = self.get(id)
        self._write(message.model_copy(update={"verdict": verdict}))

    def resolve(self, id: str, resolution: Resolution) -> None:
        message = self.get(id)
        if message.status == "resolved":
            raise BoxError(f"box id {id} is already resolved")
        self._write(message.model_copy(update={"status": "resolved", "resolution": resolution}))


class DecisionRecord(_Strict):
    id: NonBlank
    kind: Literal["decision", "tombstone"]
    link: NonBlank
    reopen_after_days: Annotated[int, Field(ge=1)]


def record_path(id: str) -> Path:
    return Path(DECISIONS_DIR) / f"{id}.md"


def render_record(record: DecisionRecord, body: str) -> str:
    return f"---\n{yaml.safe_dump(record.model_dump(), sort_keys=False)}---\n{body}"


def parse_record(text: str) -> tuple[DecisionRecord, str]:
    if not text.startswith("---\n"):
        raise BoxError("decision record missing YAML frontmatter")
    front, separator, body = text[4:].partition("\n---\n")
    if not separator:
        raise BoxError("decision record missing closing YAML fence")
    try:
        return DecisionRecord.model_validate(yaml.safe_load(front)), body
    except (yaml.YAMLError, ValidationError) as exc:
        raise BoxError(f"invalid decision record: {exc}") from exc


def read_registry(repo: Path) -> list[tuple[DecisionRecord, str]]:
    records = []
    for path in (repo / DECISIONS_DIR).glob("*.md"):
        try:
            records.append(parse_record(path.read_text()))
        except (OSError, BoxError) as exc:
            raise BoxError(f"invalid decision record {path}: {exc}") from exc
    return sorted(records, key=lambda item: item[0].id)
