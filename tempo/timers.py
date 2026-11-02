"""Journal-backed absolute deadlines."""

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from tempo.journal import EventType, Journal, render_ts
from tempo.seams import Clock, Sleep


class TimerBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    timer_id: str = Field(min_length=1)
    deadline: str

    @field_validator("deadline")
    @classmethod
    def absolute_utc(cls, value: str) -> str:
        moment = datetime.fromisoformat(value)
        if render_ts(moment) != value:
            raise ValueError("use the journal's aware-UTC isoformat rendering")
        return value


@dataclass(frozen=True)
class Timer:
    body: TimerBody
    ticket: str | None

    @property
    def deadline(self) -> datetime:
        return datetime.fromisoformat(self.body.deadline)


def _invalid(record: object, reason: str) -> ValueError:
    return ValueError(f"invalid timer record {record!r}: {reason}; "
                      "restore valid journal evidence before restart")


def _timer(body: object, ticket: str | None, key: str | None = None) -> Timer:
    try:
        parsed = TimerBody.model_validate(body)
    except ValidationError as exc:
        raise _invalid(body, str(exc)) from exc
    if key is not None or (ticket is not None and not isinstance(ticket, str)):
        raise _invalid(body, "supply a string or null owning ticket and a null key")
    return Timer(parsed, ticket)


class Timers:
    """One lock-held lifetime; construction is idle and projections are memory only."""

    def __init__(self, *, journal: Journal, clock: Clock, sleep: Sleep) -> None:
        self.journal, self.clock, self.sleep = journal, clock, sleep
        self._pending: dict[str, Timer] = {}
        self._fired: dict[str, Timer] = {}
        self._loaded = False

    @property
    def pending(self):
        return MappingProxyType(self._pending)

    @property
    def fired(self):
        return MappingProxyType(self._fired)

    def reconstruct(self) -> None:
        pending, fired = {}, {}
        for event in self.journal.read():
            if event.type not in {EventType.TIMER_ARMED, EventType.TIMER_FIRED}:
                continue
            timer = _timer(event.body, event.ticket, event.key)
            identity = timer.body.timer_id
            previous = pending.get(identity, fired.get(identity))
            if previous is not None and previous != timer:
                raise _invalid(event, "conflicting identity; use a fresh id for a new deadline or ticket")
            if event.type == EventType.TIMER_ARMED:
                if identity not in fired:
                    pending[identity] = timer
            else:
                if previous is None:
                    raise _invalid(event, "fire without a matching arm")
                fired[identity] = timer
                pending.pop(identity, None)
        # A failed fold never publishes a partial recovery.
        self._pending, self._fired = pending, fired
        self._loaded = True

    def _load(self) -> None:
        if not self._loaded:
            self.reconstruct()

    def arm(self, timer_id: str, deadline: datetime, *, ticket: str | None) -> Timer:
        timer = _timer({"timer_id": timer_id, "deadline": render_ts(deadline)}, ticket)
        self._load()
        previous = self._pending.get(timer_id, self._fired.get(timer_id))
        if previous is not None:
            if previous != timer:
                raise ValueError(f"timer id {timer_id!r} already owns another deadline or ticket; "
                                 "use a fresh id for a new deadline")
            return previous
        self.journal.append(EventType.TIMER_ARMED, timer.body.model_dump(), ticket=ticket, key=None)
        self._pending[timer_id] = timer
        return timer

    def fire_due(self) -> tuple[Timer, ...]:
        self._load()
        now = self.clock()
        due = sorted((timer for timer in self._pending.values() if now >= timer.deadline),
                     key=lambda timer: (timer.deadline, timer.body.timer_id))
        for timer in due:
            self.journal.append(EventType.TIMER_FIRED, timer.body.model_dump(),
                                ticket=timer.ticket, key=None)
            self._fired[timer.body.timer_id] = timer
            del self._pending[timer.body.timer_id]
        return tuple(due)

    async def wait_next(self) -> tuple[Timer, ...]:
        """Explicit wait owned, cancelled and awaited by the caller before unlocking."""
        self._load()
        while self._pending:
            delay = (min(timer.deadline for timer in self._pending.values()) - self.clock()).total_seconds()
            if delay <= 0:
                return self.fire_due()
            await self.sleep(delay)
        return ()
