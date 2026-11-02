"""Explicit flake detection and journal-derived quarantine."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from tempo.box import Box
from tempo.journal import EventType, Journal


class FlakeError(ValueError):
    """Evidence or history is invalid; the refusal includes its repair road."""


def _nonblank(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


@dataclass(frozen=True)
class RerunEvidence:
    test_id: str
    first_result: str
    rerun_result: str
    bare_rerun: bool
    same_workspace: bool
    unchanged_code: bool


@dataclass(frozen=True)
class FlakeIdentity:
    test_id: str
    signature: str
    box_id: str


@dataclass(frozen=True)
class GreenRerunEvidence:
    """Caller observation, never a durable record or a request to execute checks."""

    test_id: str
    fix_stem: str
    observed_at: datetime
    result: str
    observed: bool
    same_workspace: bool
    unchanged_code: bool


@dataclass
class Quarantine:
    detected: dict[str, FlakeIdentity]
    active: dict[str, FlakeIdentity]

    @property
    def tests(self) -> set[str]:
        return {identity.test_id for identity in self.active.values()}


class Flake:
    """The caller holds the writer lock and supplies evidence, never check execution."""

    def __init__(self, *, journal: Journal, box: Box, cap: int,
                 escalate: Callable[[str], None]) -> None:
        self.journal, self.box, self.cap, self.escalate = journal, box, cap, escalate

    def quarantine(self) -> Quarantine:
        detected, active, released = {}, {}, {}
        road = "repair the flake evidence against its original detection identity; never overwrite history"
        for event in self.journal.read():
            body = event.body
            kind = body.get("kind")
            reserved = isinstance(event.key, str) and event.key.startswith(("flake/", "flake-release/"))
            if kind not in ("flake_detected", "flake_released") and not reserved:
                continue
            fields = {"kind", "test_id", "signature", "box_id"}
            if kind == "flake_released":
                fields.add("fix_stem")
            if (kind not in ("flake_detected", "flake_released")
                    or event.type != EventType.SIGNAL or event.ticket is not None
                    or set(body) != fields
                    or not all(_nonblank(body.get(field)) for field in fields)
                    or re.fullmatch(r"[0-9a-f]{64}", body["signature"]) is None):
                raise FlakeError(f"malformed flake record; {road}")
            identity = FlakeIdentity(body["test_id"], body["signature"], body["box_id"])
            box_id = identity.box_id
            key = (f"flake/{box_id}" if kind == "flake_detected"
                   else f"flake-release/{box_id}/{body['fix_stem']}")
            if event.key != key:
                raise FlakeError(f"malformed flake key; {road}")
            if kind == "flake_detected":
                if box_id in detected:
                    if detected[box_id] != identity:
                        raise FlakeError(f"conflicting detection identity; {road}")
                    continue
                detected[box_id] = active[box_id] = identity
            else:
                if detected.get(box_id) != identity:
                    raise FlakeError(f"release has no matching detection identity; {road}")
                if box_id in released and released[box_id] != body["fix_stem"]:
                    raise FlakeError(f"conflicting release fix identity; {road}")
                released[box_id] = body["fix_stem"]
                active.pop(box_id, None)
        return Quarantine(detected, active)

    def release(self, *, box_id: str, evidence: GreenRerunEvidence | None = None) -> Quarantine:
        """Resolve the report to its fix ticket, merge it, then supply its named green rerun.

        Pending and non-ticket resolutions are idle; insufficient observations refuse
        with that same road. Recorded releases replay without fresh observations.
        """
        projection = self.quarantine()
        road = "resolve the report to its fix ticket, merge that fix, and supply the named test's green rerun"
        identity = projection.detected.get(box_id)
        if identity is None:
            raise FlakeError("unknown detection identity; repair the source detection evidence")
        message = self.box.get(identity.box_id)
        if ((message.id, message.signature, message.origin) !=
                (identity.box_id, identity.signature, identity.test_id)
                or (message.message_class, message.stage, message.outcome) !=
                ("failure_report", "check", "gate_failed")):
            raise FlakeError("Box identity mismatch; repair the source report against its detection evidence")
        events = self.journal.read()
        resolution = message.resolution
        fix = (resolution.link if message.status == "resolved" and resolution is not None
               and resolution.kind == "ticket" else None)
        if box_id not in projection.active:
            recorded = next(event.body["fix_stem"] for event in events
                            if event.type == EventType.SIGNAL
                            and event.body.get("kind") == "flake_released"
                            and event.body.get("box_id") == box_id)
            if fix != recorded:
                raise FlakeError("conflicting release resolution; repair the source Box resolution evidence")
            return projection
        if fix is None:
            return projection
        merges = [datetime.fromisoformat(event.ts) for event in events
                  if event.type == EventType.STATE_TRANSITION and event.ticket == fix
                  and event.body.get("to") == "merged"]
        if (not _nonblank(fix) or not merges or not isinstance(evidence, GreenRerunEvidence)
                or evidence.test_id != identity.test_id or evidence.fix_stem != fix
                or evidence.result != "pass" or evidence.observed is not True
                or evidence.same_workspace is not True or evidence.unchanged_code is not True
                or not isinstance(evidence.observed_at, datetime)
                or evidence.observed_at.utcoffset() is None
                or evidence.observed_at < max(merges)):
            raise FlakeError(f"insufficient release evidence; {road}")
        self.journal.append(EventType.SIGNAL, {
            "kind": "flake_released", "test_id": identity.test_id,
            "signature": identity.signature, "box_id": identity.box_id, "fix_stem": fix,
        }, ticket=None, key=f"flake-release/{identity.box_id}/{fix}")
        return self.quarantine()

    def detect(self, *, test_id: str, evidence: RerunEvidence,
               summary: str, reason: str, occurrence_id: str | None = None) -> Quarantine:
        if (not _nonblank(test_id) or not isinstance(evidence, RerunEvidence)
                or evidence.test_id != test_id or evidence.first_result != "fail"
                or evidence.rerun_result != "pass" or evidence.bare_rerun is not True
                or evidence.same_workspace is not True or evidence.unchanged_code is not True
                or not _nonblank(summary) or not _nonblank(reason)):
            raise FlakeError("insufficient flake evidence; provide one named test's unchanged-workspace "
                             "fail-then-pass observation on a bare rerun, description and failure reason")
        projection = self.quarantine()
        box_id, _ = self.box.enqueue(message_class="failure_report", origin=test_id, stage="check",
                                     outcome="gate_failed", summary=summary, reason=reason,
                                     occurrence_id=occurrence_id)
        identity = FlakeIdentity(test_id, self.box.get(box_id).signature, box_id)
        if box_id in projection.detected:
            if projection.detected[box_id] != identity:
                raise FlakeError("conflicting detection identity; repair the evidence against the original "
                                 "Box report rather than overwrite history")
            return projection
        if test_id not in projection.tests and len(projection.tests) >= self.cap:
            self.escalate("quarantine-cap crossed; fix and release existing quarantines before admitting "
                          "another test; the new test remains blocking")
            return projection
        self.journal.append(EventType.SIGNAL, {
            "kind": "flake_detected", "test_id": test_id,
            "signature": identity.signature, "box_id": box_id,
        }, ticket=None, key=f"flake/{box_id}")
        return self.quarantine()
