"""Single-flight dispatch projection (TEMPO_PLAN.md section 9)."""

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Set

from tempo.backlog import SETTLED, authored_at, settled_dependencies, sort_key, supersedes_maps
from tempo.journal import Journal
from tempo.status import last_states, reject_queue
from tempo.tickets import Ticket


class Scheduler:
    def __init__(
        self, journal: Journal, dispatch: Callable[[Ticket], Awaitable[object]], *,
        quarantined: Callable[[], Set[str]], drought_parked: Callable[[], Set[str]],
        completed_unmerged: Callable[[], int], max_unmerged: int,
    ) -> None:
        self.journal = journal
        self.dispatch = dispatch
        self.quarantined = quarantined
        self.drought_parked = drought_parked
        self.completed_unmerged = completed_unmerged
        self.max_unmerged = max_unmerged
        self.pending: dict[str, Ticket] = {}
        self.active: Ticket | None = None
        self._slot = asyncio.Lock()
        self.continuations: Callable[[], Iterable[Ticket]] = lambda: ()
        self.next_stage: Callable[[Ticket], str] = lambda _: "implement"
        self.select: Callable[[Ticket, str], bool] = lambda _ticket, _stage: True

    def update(self, ticket: Ticket) -> None:
        self.pending[ticket.stem] = ticket

    def remove(self, stem: str) -> None:
        self.pending.pop(stem, None)

    async def dispatch_next(self) -> Ticket | None:
        """Concurrent requests wait for completion, then select from current projections."""
        async with self._slot:
            events = self.journal.read()
            last = last_states(events)
            held = set(reject_queue(events)) | self.quarantined() | self.drought_parked()
            if self.completed_unmerged() >= self.max_unmerged:
                return None
            settled = settled_dependencies(events)
            maps = supersedes_maps(events)
            authored = authored_at(events)
            retained = {t.stem: t for t in self.continuations()}
            candidates = {**self.pending, **retained}
            ready = [t for t in candidates.values()
                     if t.frontmatter.state == "confirmed"
                     and last.get(t.stem) not in SETTLED | {"rejected"}
                     and (last.get(t.stem) != "running" or t.stem in retained)
                     and t.stem not in held and t.stem not in maps and set(t.depends) <= settled
                     and self.select(t, self.next_stage(t))]
            if not ready:
                return None
            ticket = min(ready, key=lambda t: sort_key(t, authored))
            self.remove(ticket.stem)
            self.active = ticket
            try:
                await self.dispatch(ticket)
            finally:
                self.active = None
            return ticket
