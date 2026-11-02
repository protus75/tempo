"""Debounced last-known-good ticket cache (TEMPO_PLAN.md 19.P3.daemon-scheduler)."""

import asyncio
from collections.abc import AsyncIterable, Callable
from datetime import timedelta
from pathlib import Path

from tempo.journal import EventType, Journal
from tempo.seams import Clock, Sleep
from tempo.tickets import Ticket, TicketInvalid, parse_ticket, ticket_path

WATCHER_PARSE_FAILURE = "watcher_parse_failure"


class Watcher:
    def __init__(
        self, repo: Path, *, plan: str | None, read: Callable[[str], str | None],
        journal: Journal, publish: Callable[[Ticket], None], remove: Callable[[str], None],
        clock: Clock, sleep: Sleep, debounce: float,
    ) -> None:
        self.repo = repo
        self.plan = plan
        self.read = read
        self.journal = journal
        self.publish = publish
        self.remove = remove
        self.clock = clock
        self.sleep = sleep
        self.debounce = debounce
        self.cache: dict[str, Ticket] = {}
        self._waits: dict[str, asyncio.Task] = {}

    def change(self, stem: str) -> None:
        """Deliver a ticket-directory change; further edits restart this stem's wait."""
        if previous := self._waits.get(stem):
            previous.cancel()
        deadline = self.clock() + timedelta(seconds=self.debounce)

        async def consume() -> None:
            while (remaining := (deadline - self.clock()).total_seconds()) > 0:
                await self.sleep(remaining)
            text = self.read(stem)
            if text is None:
                self.cache.pop(stem, None)
                self.remove(stem)
                return
            try:
                ticket = parse_ticket(stem, text, self.repo, plan=self.plan)
            except TicketInvalid as e:
                self.journal.append(EventType.SIGNAL,
                                    {"signal": WATCHER_PARSE_FAILURE, "path": ticket_path(stem),
                                     "reason": str(e)}, ticket=stem, key=None)
                return
            self.cache[stem] = ticket
            self.publish(ticket)

        task = asyncio.create_task(consume())
        self._waits[stem] = task

        def finished(done: asyncio.Task) -> None:
            if self._waits.get(stem) is done:
                del self._waits[stem]

        task.add_done_callback(finished)

    async def run(self, changes: AsyncIterable[str]) -> None:
        try:
            async for stem in changes:
                self.change(stem)
            await asyncio.gather(*self._waits.values())
        finally:
            await self.close()

    async def close(self) -> None:
        waits = list(self._waits.values())
        for task in waits:
            task.cancel()
        await asyncio.gather(*waits, return_exceptions=True)
