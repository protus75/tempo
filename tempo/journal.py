"""Segmented append-only JSONL journal (D3; TEMPO_PLAN.md section 6).

The newest segment rolls synchronously on append at the size or age boundary.
"""

import json
import os
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, cast, get_args

from tempo.artifacts import OUTCOMES
from tempo.seams import LocalFileSystem

ROLL_BYTES = 64 * 1024 * 1024
ROLL_AGE = timedelta(hours=24)


class EventType(StrEnum):
    EFFECT_INTENT = "effect_intent"
    EFFECT_COMPLETION = "effect_completion"
    SIGNAL = "signal"
    TIMER_ARMED = "timer_armed"
    TIMER_FIRED = "timer_fired"
    CAP_CONSUMED = "cap_consumed"
    STATE_TRANSITION = "state_transition"
    CHECKPOINT = "checkpoint"


Dispatch = Literal["retry", "escalate", "reject_queue", "park"]

SIGNAL_NAMES: frozenset[str] = frozenset({
    "author_invoked", "checkpoint_push_failed", "control_decision", "dead_dependency",
    "diagnosis", "draft_confirmed", "drain_halted",
    "flake_detected", "flake_released", "handoff_written", "harvest_failed", "kill_applied",
    "merge_conflict_facts", "merge_red_streak", "merge_tree_mismatch", "provider_call_outcome",
    "recovery_alert", "reject_arrival", "reject_verdict", "reoffered", "requisition_verdict",
    "retro_failed", "retro_ticket_authored", "retro_prompt_spec_change_merged", "tombstone_auto_reopened",
    "rework_order", "session_begun", "stage_failed", "storm_breaker_trip", "storm_occurrence", "subagent_call_closed", "supersedes",
    "ticket_intake", "triage_pass", "watcher_parse_failure",
})


def dispatch_of(body: Mapping[str, Any]) -> Dispatch | None:
    """Read the promoted terminal field without silently classifying unknown dispatches."""
    if "dispatch" not in body:
        return None
    value = body["dispatch"]
    if not isinstance(value, str) or value not in get_args(Dispatch):
        raise ValueError(f"unknown dispatch {value!r}; use one of {get_args(Dispatch)} or omit dispatch")
    return cast(Dispatch, value)


# Current event-schema version per type; readers refuse newer.
EVENT_VERSIONS: dict[str, int] = {t.value: 1 for t in EventType}

# Reserved shape (D2): readable, never emitted in v1.
_NOT_EMITTED = frozenset({EventType.CHECKPOINT.value})

_ENVELOPE_KEYS = ("v", "type", "ts", "ticket", "key", "body")
_SEGMENT_NAME = re.compile(r"^(\d{6})-(\d{8})\.jsonl$")


# The terminal half of the closed RUN-STATE vocabulary (`running` is the one non-terminal).
TERMINAL_STATES: frozenset[str] = frozenset({"merged", "abandoned", "rejected"}) | (OUTCOMES - {"ok"})


def run_seq(events: Iterable[Any], stem: str) -> int:
    """The RUN SEQUENCE: count of the stem's prior terminal events, folded at run entry, never stored."""
    return len(run_terminals(events, stem))


def run_terminals(events: Iterable[Any], stem: str) -> list[Any]:
    terminals, running = [], False
    for event in events:
        if event.type != EventType.STATE_TRANSITION or event.ticket != stem:
            continue
        state = event.body.get('to')
        if state == 'running':
            running = True
        elif state in TERMINAL_STATES:
            if running or 'provider_drought' not in event.body:
                terminals.append(event)
            running = False
    return terminals


class JournalCorruption(Exception):
    """A journal line or segment violates the read law; never skipped."""


@dataclass(frozen=True, slots=True)
class Event:
    v: int
    type: str
    ts: str
    ticket: str | None
    key: str | None
    body: dict[str, Any]


def render_ts(moment: datetime) -> str:
    """The one pinned `ts` rendering: aware-UTC isoformat, so string order is time order."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"clock returned a naive datetime {moment!r}; the clock seam must be aware")
    return moment.astimezone(UTC).isoformat()


class Journal:
    def __init__(self, state_dir: Path, clock: Callable[[], datetime]) -> None:
        self.dir = Path(state_dir) / "journal"
        self._clock = clock
        self._fs = LocalFileSystem()
        self._tail_repaired = False

    def append(
        self, type: str, body: dict[str, Any], *, ticket: str | None = None, key: str | None = None
    ) -> Event:
        """Write one event and fsync it before returning (write-ahead)."""
        if type not in EVENT_VERSIONS or type in _NOT_EMITTED:
            emittable = sorted(set(EVENT_VERSIONS) - _NOT_EMITTED)
            raise ValueError(f"event type {type!r} is not emittable; use one of {emittable}")
        if not isinstance(body, dict):
            raise ValueError(f"event body must be a JSON object, got {body.__class__.__name__}")
        for field, value in (("ticket", ticket), ("key", key)):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{field} must be a string or None, got {value.__class__.__name__}")
        if type == EventType.SIGNAL:
            names = [body[field] for field in ("signal", "kind") if field in body]
            if len(names) != 1 or not isinstance(names[0], str) or names[0] not in SIGNAL_NAMES:
                raise ValueError("signal body must carry exactly one of signal or kind naming a member "
                                 "of SIGNAL_NAMES; use a listed name or add the name with its consumer")
        event = Event(EVENT_VERSIONS[type], type, render_ts(self._clock()), ticket, key, body)
        data = (json.dumps(asdict(event), separators=(",", ":")) + "\n").encode()

        now = datetime.fromisoformat(event.ts)
        segment = self._active_segment(now)
        if not self._tail_repaired:
            _truncate_torn_tail(segment)
            self._tail_repaired = True
        records = _parse_segment(segment, active=True)
        if segment.stat().st_size >= ROLL_BYTES or (
            records and now - datetime.fromisoformat(records[0].ts) >= ROLL_AGE
        ):
            segment = self._create_segment(int(segment.name[:6]) + 1, now)
        fd = os.open(segment, os.O_WRONLY | os.O_APPEND)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        return event

    def read_segments(self) -> Iterator[tuple[Event, ...]]:
        """The one parser: each segment in write order as its own ordered tuple.

        Only the active (newest) segment may end in a torn line, which is dropped.
        """
        segments = self._segments()
        for i, path in enumerate(segments):
            yield _parse_segment(path, active=i == len(segments) - 1)

    def read(self) -> list[Event]:
        return [event for segment in self.read_segments() for event in segment]

    def _segments(self) -> list[Path]:
        if not self.dir.is_dir():
            return []
        paths = sorted(self.dir.glob("*.jsonl"))
        seen: dict[str, str] = {}
        for path in paths:
            match = _SEGMENT_NAME.match(path.name)
            if match is None:
                raise JournalCorruption(
                    f"{path}: not a journal segment name (expected NNNNNN-YYYYMMDD.jsonl); "
                    "move it out of the journal directory"
                )
            seq = match.group(1)
            if seq in seen:
                raise JournalCorruption(
                    f"segments {seen[seq]} and {path.name} share sequence {seq}; "
                    "segment order is ambiguous -- restore the journal directory from backup"
                )
            seen[seq] = path.name
        return paths

    def _active_segment(self, now: datetime) -> Path:
        segments = self._segments()
        if segments:
            return segments[-1]
        return self._create_segment(1, now)

    def _create_segment(self, sequence: int, now: datetime) -> Path:
        segment = self.dir / f"{sequence:06d}-{now:%Y%m%d}.jsonl"
        self._fs.publish(segment, b"")
        return segment


def _truncate_torn_tail(segment: Path) -> None:
    """Writer's startup dual of torn-tail tolerance: never append after a partial record."""
    data = segment.read_bytes()
    if not data or data.endswith(b"\n"):
        return
    with segment.open("r+b") as f:
        f.truncate(data.rfind(b"\n") + 1)
        f.flush()
        os.fsync(f.fileno())


def _parse_segment(path: Path, *, active: bool) -> tuple[Event, ...]:
    data = path.read_bytes()
    lines = data.split(b"\n")
    # split leaves b"" after a terminating newline; anything else is an unterminated tail.
    tail = lines.pop()
    if tail and not active:
        raise JournalCorruption(
            f"{path}: torn final line in a rolled segment (line {len(lines) + 1}); "
            "rolled segments are immutable -- restore it from backup"
        )
    return tuple(_parse_line(path, n, raw) for n, raw in enumerate(lines, start=1))


def _parse_line(path: Path, lineno: int, raw: bytes) -> Event:
    where = f"{path}: line {lineno}"
    try:
        obj = json.loads(raw)
    except ValueError as exc:
        raise JournalCorruption(f"{where}: not valid JSON ({exc}); the journal is never hand-edited") from exc
    if not isinstance(obj, dict) or set(obj) != set(_ENVELOPE_KEYS):
        raise JournalCorruption(f"{where}: envelope must have exactly the keys {list(_ENVELOPE_KEYS)}")
    v, type_, ts, ticket, key, body = (obj[k] for k in _ENVELOPE_KEYS)
    if type_ not in EVENT_VERSIONS:
        raise JournalCorruption(f"{where}: unknown event type {type_!r}")
    if not isinstance(v, int) or isinstance(v, bool) or v < 1:
        raise JournalCorruption(f"{where}: event version must be a positive integer, got {v!r}")
    if v > EVENT_VERSIONS[type_]:
        raise JournalCorruption(
            f"{where}: {type_} v{v} is newer than this engine reads (v{EVENT_VERSIONS[type_]}); "
            "upgrade the engine"
        )
    if not isinstance(ts, str) or not _is_pinned_ts(ts):
        raise JournalCorruption(f"{where}: ts {ts!r} is not the pinned aware-UTC isoformat rendering")
    for name, value in (("ticket", ticket), ("key", key)):
        if value is not None and not isinstance(value, str):
            raise JournalCorruption(f"{where}: {name} must be a string or null, got {value!r}")
    if not isinstance(body, dict):
        raise JournalCorruption(f"{where}: body must be a JSON object")
    return Event(v, type_, ts, ticket, key, body)


def _is_pinned_ts(ts: str) -> bool:
    try:
        moment = datetime.fromisoformat(ts)
    except ValueError:
        return False
    return moment.utcoffset() == timedelta(0) and render_ts(moment) == ts
