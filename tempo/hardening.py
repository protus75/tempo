"""The shared hardening round-state fold (TEMPO_PLAN.md section 11.4)."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from tempo.journal import TERMINAL_STATES, Event, EventType

RoundState = Literal["open", "closed"]
ROUND_SIGNAL = "hardening_round"


@dataclass(frozen=True)
class HardeningRound:
    number: int
    hardener: str
    units: dict[str, str]
    filed_by: str
    gaps: list[dict]
    position: int


def rounds(events: Iterable[Event]) -> tuple[HardeningRound, ...]:
    """Round identity and coverage come solely from the journaled records."""
    return tuple(HardeningRound(e.body["round"], e.ticket, e.body["units"], e.body["filed_by"],
                                e.body["gaps"], position)
                 for position, e in enumerate(events)
                 if e.type == EventType.SIGNAL and e.body.get("signal") == ROUND_SIGNAL)


ROUND_STATES: dict[str, RoundState] = {
    "running": "open",
    "merged": "closed",
    "already_satisfied": "closed",
    "rejected": "closed",
    "premise_failed": "closed",
    "gate_failed": "open",
    "invalid_artifact": "open",
    "timeout": "open",
    "infra_error": "open",
    "abandoned": "open",
    "budget_exceeded": "open",
}


def round_state(events: Iterable[Event], round_number: int) -> RoundState:
    """The first closing terminal closes the round forever, regardless of later runs."""
    history = list(events)
    record = next((r for r in rounds(history) if r.number == round_number), None)
    if record is None:
        raise ValueError(f"unknown hardening round {round_number}; restore its hardening_round record")
    for event in history[record.position + 1:]:
        if event.type == EventType.STATE_TRANSITION and event.ticket == record.hardener:
            state = event.body.get("to")
            if state in TERMINAL_STATES and (
                    ROUND_STATES[state] == "closed" or event.body.get("routed") == "reject_queue"):
                return "closed"
    return "open"


def open_round(events: Iterable[Event]) -> HardeningRound | None:
    history = list(events)
    return next((r for r in rounds(history) if round_state(history, r.number) == "open"), None)


def hardener_round(events: Iterable[Event], stem: str) -> HardeningRound | None:
    return next((r for r in rounds(events) if r.hardener == stem), None)
