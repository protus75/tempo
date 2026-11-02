"""Journal-backed per-host circuit breakers (AGENT_PLAN.md section 4.6).

Breaker truth is folded from the journal's `provider_call_outcome` signals; no counter survives the
session. A streak of `circuit_breaker.k` counted failures opens the host for `cooldown_minutes`.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from tempo.config import CircuitBreaker
from tempo.journal import EventType, Journal, render_ts
from tempo.providers import FailureClass
from tempo.seams import Clock

PROVIDER_CALL_OUTCOME = "provider_call_outcome"
_COUNTED = frozenset({"outage", "unclassified"})


class _CallOutcome(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    signal: Literal["provider_call_outcome"] = PROVIDER_CALL_OUTCOME
    provider: str
    call_key: str
    failure_class: FailureClass | None
    open_until: str | None


@dataclass
class Breaker:
    streak: int = 0
    open_until: datetime | None = None

    def expire(self, now: datetime) -> None:
        if self.open_until is not None and now >= self.open_until:
            self.streak = 0
            self.open_until = None


def fold_breakers(journal: Journal, now: datetime) -> dict[str, Breaker]:
    states: dict[str, Breaker] = {}
    for event in journal.read():
        if event.type != EventType.SIGNAL or event.body.get("signal") != PROVIDER_CALL_OUTCOME:
            continue
        outcome = _CallOutcome.model_validate(event.body)
        state = states.setdefault(outcome.provider, Breaker())
        state.expire(datetime.fromisoformat(event.ts))
        state.streak = state.streak + 1 if outcome.failure_class in _COUNTED else 0
        if outcome.open_until is not None:
            deadline = datetime.fromisoformat(outcome.open_until)
            # Calls admitted before the opening may finish afterwards, even out of order.
            state.open_until = max(state.open_until, deadline) if state.open_until else deadline
    for state in states.values():
        state.expire(now)
    return states


def record_outcome(journal: Journal, clock: Clock, breaker: CircuitBreaker, provider: str,
                   failure_class: FailureClass | None, *, ticket: str | None, call_key: str) -> None:
    now = clock()
    state = fold_breakers(journal, now).get(provider, Breaker())
    opening = None
    if failure_class in _COUNTED and state.streak + 1 == breaker.k:
        opening = now + timedelta(minutes=breaker.cooldown_minutes)
        if state.open_until is not None:
            opening = max(opening, state.open_until)
    body = _CallOutcome(provider=provider, call_key=call_key, failure_class=failure_class,
                        open_until=render_ts(opening) if opening else None)
    journal.append(EventType.SIGNAL, body.model_dump(), ticket=ticket, key=None)
