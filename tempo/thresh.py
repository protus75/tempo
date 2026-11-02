"""Provider admission and journal-backed breakers (19.P3.thresh-runtime).

One instance owns a session's slots across surfaces. Breaker truth is folded from
the journal; neither queue state nor counters survive the session. Providers owns
selection policy; the production callers own budget accounting.
"""

import asyncio
import math
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from tempo.config import Config
from tempo.journal import EventType, Journal, render_ts
from tempo.providers import FailureClass, ProviderCallError, ProviderSetupError, Served
from tempo.seams import Clock, ExecutableNotFound, Sleep

SPILL_WAIT_SECONDS = 60
PROVIDER_CAP_WAIT = "provider_cap_wait"
PROVIDER_CALL_OUTCOME = "provider_call_outcome"
_COUNTED = frozenset({"outage", "unclassified"})


class _Signal(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    provider: str
    call_key: str


class _CapWait(_Signal):
    signal: Literal["provider_cap_wait"] = PROVIDER_CAP_WAIT
    started_at: str
    waited_seconds: float = Field(ge=0, allow_inf_nan=False)
    disposition: Literal["admitted", "cancelled", "unavailable"]


class _CallOutcome(_Signal):
    signal: Literal["provider_call_outcome"] = PROVIDER_CALL_OUTCOME
    failure_class: FailureClass | None
    open_until: str | None


@dataclass(frozen=True)
class Admission:
    served: Served
    waited_seconds: float


@dataclass(frozen=True)
class UnavailableRoute:
    providers: tuple[str, ...]
    open_until: datetime | None
    waited_seconds: float
    paved_road: str


@dataclass(frozen=True)
class CallResult:
    admission: Admission
    result: Any


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


@dataclass(eq=False)
class _Waiter:
    ready: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _Slots:
    cap: int
    used: int = 0
    queue: deque[_Waiter] = field(default_factory=deque)

    def free(self) -> bool:
        return self.used < self.cap and not self.queue

    def wake(self) -> None:
        if self.queue and self.used < self.cap:
            self.queue[0].ready.set()


class Thresh:
    def __init__(self, config: Config, *, journal: Journal, clock: Clock, sleep: Sleep) -> None:
        self._journal, self._clock, self._sleep = journal, clock, sleep
        self._breaker = config.circuit_breaker
        self._slots = {p.name: _Slots(p.limits.concurrency) for p in config.providers}
        self.unavailable = lambda name: (False, None, None)
        self.on_outcome = lambda served, failure: None
        self._waiting: dict[str, datetime] = {}

    def occupancy(self, provider: str) -> tuple[int, int, int]:
        slots = self._slots[provider]
        return slots.used, len(slots.queue), slots.cap

    def configure(self, config):
        self._breaker = config.circuit_breaker
        for provider in config.providers:
            slots = self._slots.setdefault(provider.name, _Slots(provider.limits.concurrency))
            slots.cap = provider.limits.concurrency
            slots.wake()

    def live_wait(self, prefix: str) -> float:
        return sum(max(0, (self._clock() - started).total_seconds())
                   for key, started in self._waiting.items() if key.startswith(prefix))

    def breakers(self) -> dict[str, Breaker]:
        return fold_breakers(self._journal, self._clock())

    def record_outcome(self, provider: str, failure_class: FailureClass | None, *,
                       ticket: str | None, call_key: str) -> None:
        now = self._clock()
        state = fold_breakers(self._journal, now).get(provider, Breaker())
        opening = None
        if failure_class in _COUNTED and state.streak + 1 == self._breaker.k:
            opening = now + timedelta(minutes=self._breaker.cooldown_minutes)
            if state.open_until is not None:
                opening = max(opening, state.open_until)
        body = _CallOutcome(provider=provider, call_key=call_key, failure_class=failure_class,
                            open_until=render_ts(opening) if opening else None)
        self._journal.append(EventType.SIGNAL, body.model_dump(), ticket=ticket, key=None)

    def _select(self, route: Sequence[Served], estimate: float, waited: float) -> Served | UnavailableRoute:
        states = self.breakers()
        excluded = {s.provider.name: self.unavailable(s.provider.name) for s in route}
        closed = [s for s in route if states.get(s.provider.name, Breaker()).open_until is None
                  and not excluded[s.provider.name][0]]
        if not closed:
            deadlines = [d for s in route for d in (
                states.get(s.provider.name, Breaker()).open_until, excluded[s.provider.name][1]) if d is not None]
            deadline = min(deadlines) if deadlines else None
            names = tuple(s.provider.name for s in route)
            roads = [road for _, _, road in excluded.values() if road]
            road = '; '.join(roads) or f"repair the configured route for {', '.join(names)}"
            if deadline:
                road = f"wait until {render_ts(deadline)} or {road}"
            return UnavailableRoute(names, deadline, waited, road)
        selected = closed[0]
        if (selected is route[0] and not self._slots[selected.provider.name].free()
                and estimate > SPILL_WAIT_SECONDS):
            selected = next((s for s in closed[1:] if self._slots[s.provider.name].free()), selected)
        return selected

    def _wait_record(self, provider: str, started: datetime, disposition: str, *,
                     ticket: str | None, call_key: str) -> float:
        waited = max(0.0, (self._clock() - started).total_seconds())
        body = _CapWait(provider=provider, call_key=call_key, started_at=render_ts(started),
                        waited_seconds=waited, disposition=disposition)
        self._journal.append(EventType.SIGNAL, body.model_dump(), ticket=ticket, key=None)
        return waited

    async def admit(self, route: Sequence[Served], *, ticket: str | None, call_key: str,
                    projected_wait_seconds: float) -> Admission | UnavailableRoute:
        if not route:
            raise ProviderSetupError("empty provider route; configure at least one candidate in config.yaml")
        if not math.isfinite(projected_wait_seconds) or projected_wait_seconds < 0:
            raise ValueError("projected cap wait must be finite and nonnegative; supply seconds for the primary")
        waited = 0.0
        while True:
            selected = self._select(route, projected_wait_seconds, waited)
            if isinstance(selected, UnavailableRoute):
                return selected
            name = selected.provider.name
            slots = self._slots[name]
            if slots.free():
                slots.used += 1
                return Admission(selected, waited)
            waiter, started = _Waiter(), self._clock()
            slots.queue.append(waiter)
            self._waiting[call_key] = started
            slots.wake()
            try:
                while True:
                    await waiter.ready.wait()
                    # A notified waiter remains cancellable before reserving a slot.
                    await self._sleep(0)
                    waiter.ready.clear()
                    if slots.queue[0] is not waiter or slots.used >= slots.cap:
                        continue
                    unavailable = (self.breakers().get(name, Breaker()).open_until is not None
                                   or self.unavailable(name)[0])
                    waited += self._wait_record(name, started, "unavailable" if unavailable else "admitted",
                                                ticket=ticket, call_key=call_key)
                    slots.queue.popleft()
                    self._waiting.pop(call_key, None)
                    if not unavailable:
                        slots.used += 1
                    slots.wake()
                    if unavailable:
                        break
                    return Admission(selected, waited)
            except BaseException as exc:
                slots.queue.remove(waiter)
                self._waiting.pop(call_key, None)
                slots.wake()
                if isinstance(exc, asyncio.CancelledError):
                    self._wait_record(name, started, "cancelled", ticket=ticket, call_key=call_key)
                raise

    def release(self, admission: Admission) -> None:
        slots = self._slots[admission.served.provider.name]
        slots.used -= 1
        slots.wake()

    async def run(self, route: Sequence[Served], *, ticket: str | None, call_key: str,
                  projected_wait_seconds: float,
                  effect: Callable[[Callable[[], Awaitable[Any]]], Awaitable[Any]],
                  operation: Callable[[Served], Awaitable[Any]]) -> CallResult | UnavailableRoute:
        admission = await self.admit(route, ticket=ticket, call_key=call_key,
                                     projected_wait_seconds=projected_wait_seconds)
        if isinstance(admission, UnavailableRoute):
            return admission

        async def executed() -> Any:
            try:
                result = await operation(admission.served)
            except (asyncio.CancelledError, ProviderSetupError, ExecutableNotFound):
                raise
            except Exception as exc:
                failure = (exc.failure_class or "unclassified") if isinstance(exc, ProviderCallError) else "unclassified"
                self.record_outcome(admission.served.provider.name, failure, ticket=ticket, call_key=call_key)
                self.on_outcome(admission.served, failure)
                raise
            self.record_outcome(admission.served.provider.name, None, ticket=ticket, call_key=call_key)
            self.on_outcome(admission.served, None)
            return result

        try:
            return CallResult(admission, await effect(executed))
        finally:
            self.release(admission)
