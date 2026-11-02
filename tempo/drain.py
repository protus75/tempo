"""The `drain` scaffold verb (TEMPO_PLAN.md sections 9, 11.2, 18): run the ready queue to quiescence.

One ticket at a time, in the one process holding the single-writer lock, through the same dispatch seam as
`run <stem>`. A non-ok terminal parks its stem and the drain moves on; at quiescence each parked stem with
`retry` budget left is re-offered, one journaled `cap_consumed` unit per re-offer, findings-fed: the Implement
render folds the prior terminal's durable findings into criteria-position (section 11.2). Every selection re-scans
the committed tickets dir and re-folds the journal, so a merge or a ticket-plane commit landed mid-invocation
is visible to the very next pick. This is the eligibility sort's owner (section 9).

A `premise_failed` stem stays parked until its committed `ticket.md` changes.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta

from tempo import caps
from tempo.journal import TERMINAL_STATES, Event, EventType
from tempo.lockfile import Lockfile
from tempo.reconcile import reconcile
from tempo.backlog import PREMISE, RETIRED, SETTLED, eligible, premise_parked, settled_dependencies, start_run
from tempo.checkout import Checkout, Refusal
from tempo.runner import EXIT_MERGED, EXIT_TICKET, Dispatch
from tempo.stages import harvest_orphan
from tempo.status import last_states, reject_queue
from tempo.tickets import (
    TICKET_FILE, TICKETS_DIR, Ticket, TicketInvalid, _porcelain, intake,
    parse_ticket, ticket_path,
)

HALT_SIGNAL = "drain_halted"
CEILING = "drain.max_runtime_hours"


@dataclass(frozen=True)
class Parked:
    stem: str
    reason: str
    paved_road: str
    findings: tuple[str, ...] = ()


@dataclass
class Report:
    merged: list[str] = field(default_factory=list)
    parked: list[Parked] = field(default_factory=list)
    blocked: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    unadmitted: list[str] = field(default_factory=list)  # eligible but never run: only a halt leaves any
    invalid: list[tuple[str, str]] = field(default_factory=list)
    drafts: list[str] = field(default_factory=list)
    halted: str | None = None
    killed: bool = False

    @property
    def exit_code(self) -> int:
        # Section 18: quiescence is 0 whatever it parked; a ceiling halt is a non-quiescent stop.
        return EXIT_TICKET if self.halted or self.killed else EXIT_MERGED

    def render(self) -> str:
        def block(name: str, lines: Iterable[str]) -> str:
            return f"{name}:\n" + ("\n".join(f"- {line}" for line in lines) or "(none)")

        if self.killed:
            head = "drain stopped by kill -- NOT quiescent. Continue with `uv run python -m tempo drain`."
        elif self.halted:
            head = (f"drain HALTED by the {CEILING} ceiling ({self.halted}) -- NOT quiescent; no new ticket was"
                    " admitted. Continue with `uv run python -m tempo drain`.")
        elif not (self.merged or self.parked or self.blocked):
            head = "drain quiescent: nothing eligible (empty ready set)."
        else:
            head = "drain quiescent: nothing eligible, unparked, re-offerable, or newly authored."
        parked = (f"{p.stem}: {p.reason} -- {p.paved_road}"
                  + "".join(f"\n  - {f}" for f in p.findings) for p in self.parked)
        return "\n\n".join((
            head,
            block("merged this invocation", self.merged),
            block("parked", parked),
            block("eligible, not admitted", self.unadmitted),
            block("blocked on unmerged depends", (f"{s}: waits on {', '.join(d)}" for s, d in self.blocked)),
            block("invalid committed tickets", (f"{s}: {why}" for s, why in self.invalid)),
            block("drafts awaiting confirm", self.drafts),
        )) + "\n"


@dataclass(frozen=True)
class _Scan:
    tickets: dict[str, Ticket]
    invalid: dict[str, str]
    drafts: tuple[str, ...]


async def drain(
    checkout: Checkout, dispatch: Dispatch, *,
    before_dispatch: Callable[[], Awaitable[None]] | None = None,
) -> Report:
    consumer = checkout.control
    if consumer is None:
        raise Refusal("admission control consumer absent",
                      "construct it with `build_control` at the composition root and supply it as `Checkout.control`")
    lock = Lockfile(checkout.config.state_dir, instance_id=await checkout.git.describe(checkout.repo),
                    clock=checkout.clock)
    lock.acquire()
    try:
        consumer.publish()

        async def checkpoint() -> None:
            await consumer.checkpoint()
            if (consumer.projection.lifecycle_id == consumer.inbox.lifecycle_id
                    and consumer.projection.kill_requested):
                return
            if before_dispatch is not None:
                await before_dispatch()
            # An injected preparation wait can allow a new request to arrive.
            await consumer.checkpoint()

        assert checkout.config.worktree_root is not None  # resolved at config load
        await reconcile(checkout.journal, checkout.git, checkout.repo, checkout.config.worktree_root,
                        lambda stem, attempt: harvest_orphan(checkout, stem, attempt))
        consumer.providers.timers.reconstruct()
        consumer.providers.timers.fire_due()
        if hasattr(dispatch, 'prepare'):
            await dispatch.prepare()
        await intake(checkout.repo, checkout.git, checkout.journal, checkout.fs)
        run = _Drain(checkout, dispatch, before_dispatch=checkpoint)
        run.retro = getattr(dispatch, "_retro", None)
        report = await run.run()
        # A kill accepted during the final preparation still stops the drain.
        await consumer.checkpoint()
        if run._stopping():
            await consumer.apply_kill(dispatch.abort_current)
            report.killed = True
        return report
    finally:
        try:
            try:
                await consumer.providers.close()
            finally:
                consumer.retire()
        finally:
            lock.release()


class _Drain:
    def __init__(self, checkout: Checkout, dispatch: Dispatch, *,
                 before_dispatch: Callable[[], Awaitable[None]] | None = None) -> None:
        self.c = checkout
        self.dispatch = dispatch
        self.before_dispatch = before_dispatch
        self.deadline = checkout.clock() + timedelta(hours=checkout.config.drain.max_runtime_hours)
        self.over_budget: dict[str, Parked] = {}  # parked at dispatch by the per-ticket ceiling
        self.storm_waiting = False
        self.provider_deadlines = []
        self.report = Report()
        self.retro = None

    async def run(self) -> Report:
        while True:
            if hasattr(self.dispatch, 'prepare'):
                from dataclasses import replace
                from tempo.config import snapshot_config
                self.c = replace(self.c, config=snapshot_config(self.c.control.load_config()))
            if self.before_dispatch is not None:
                await self.before_dispatch()
            if self._stopping():
                return self.report
            if self.retro is not None:
                if hasattr(self.dispatch, 'prepare'):
                    await self.dispatch.prepare()
                await self._run_retro(forced=False)
            material = self._offer_material()
            scan = await self._scan()
            shas = {s: await self._sha(s) for s in scan.tickets}
            prepared_events = self.c.journal.read()
            # A control wait may outlive this preparation. Compare ticket bytes again
            # before using its SHAs; changed content must return through the git seam.
            if self.before_dispatch is not None:
                await self.before_dispatch()
            if self._stopping():
                return self.report
            events = self.c.journal.read()
            if material != self._offer_material() or events != prepared_events:
                continue
            last = last_states(events)
            held = {s for s in scan.tickets if last.get(s) == PREMISE and premise_parked(events, s, shas[s])}
            pick, reoffer = self._select(scan, events, last, held)
            if pick is None:
                if self.provider_deadlines:
                    now = self.c.clock()
                    if now >= self.deadline:
                        return self._halt(scan, events, last, held)
                    deadline = min(min(self.provider_deadlines), self.deadline)
                    # Re-enter the control checkpoint while routing is unavailable.
                    await self.c.control.sleep(min(0.1, max(0, (deadline - now).total_seconds())))
                    continue
                if self.storm_waiting:
                    if self.c.clock() >= self.deadline:
                        return self._halt(scan, events, last, held)
                    await self.c.control.sleep(0.1)
                    continue
                if self.retro is not None:
                    before = self.c.journal.read()
                    await self._run_retro(forced=True)
                    if self.c.journal.read() != before:
                        continue
                self._settle(scan, events, last, held)
                return self.report
            if self.c.clock() >= self.deadline:
                return self._halt(scan, events, last, held)
            await self._run_one(pick, reoffer, shas[pick.stem])
            if self._stopping():
                return self.report

    def _stopping(self) -> bool:
        consumer = self.c.control
        return (consumer is not None and consumer.projection.lifecycle_id == consumer.inbox.lifecycle_id
                and consumer.projection.kill_requested)

    def _offer_material(self) -> dict[str, bytes]:
        repo = self.c.repo
        paths = list((repo / TICKETS_DIR).glob(f"*/{TICKET_FILE}"))
        return {p.relative_to(repo).as_posix(): p.read_bytes() for p in paths}

    async def _scan(self) -> _Scan:
        """Committed tickets only: a ticket file dirty in the working tree is not yet on the ticket plane."""
        repo = self.c.repo
        dirty = {p for _, p in _porcelain(await self.c.git.status_porcelain(repo))}
        tickets: dict[str, Ticket] = {}
        invalid: dict[str, str] = {}
        drafts: list[str] = []
        for path in sorted((repo / TICKETS_DIR).glob(f"*/{TICKET_FILE}")):
            stem = path.parent.name
            rel = ticket_path(stem)
            if any(p == rel or (p.endswith("/") and rel.startswith(p)) for p in dirty):
                continue
            try:
                ticket = parse_ticket(stem, path.read_text(), repo)
            except TicketInvalid as e:
                invalid[stem] = "; ".join(f"[{f.code}] {f.message} ({f.paved_road})" for f in e.findings)
                continue
            if ticket.frontmatter.state == "draft":
                drafts.append(stem)
            tickets[stem] = ticket
        return _Scan(tickets, invalid, tuple(drafts))

    async def _sha(self, stem: str) -> str:
        """The committed `ticket.md` blob: the content identity a premise park and a retry draw are keyed to."""
        return await self.c.git.rev_parse(self.c.repo, f"HEAD:{ticket_path(stem)}")

    def _select(
        self, scan: _Scan, events: list[Event], last: Mapping[str, str], held: set[str],
    ) -> tuple[Ticket | None, bool]:
        """The next dispatch: fresh eligible work first, then re-offers, each in (priority, age) order."""
        ready = eligible(scan.tickets, events)
        awaiting = reject_queue(events)
        fresh, reoffers = [], []
        storm_held = set(self.c.control.storm_holds().values()) if self.c.control is not None else set()
        self.storm_waiting = False
        self.provider_deadlines = []
        for t in ready:
            budget = caps.spent(self.c.config.caps, events, t.stem) is None
            if t.stem in storm_held:
                if (t.stem not in self.over_budget
                        and t.stuck_minutes <= self.c.config.drain.max_ticket_minutes
                        and (last.get(t.stem) is None or (t.stem not in held
                             and budget))):
                    self.storm_waiting = True
                continue
            if t.stem in awaiting and not budget:
                continue
            if t.stem in self.over_budget:
                continue
            if t.stuck_minutes > self.c.config.drain.max_ticket_minutes:
                self.over_budget[t.stem] = Parked(
                    t.stem, f"`## Time budget` stuck {t.stuck_minutes}m exceeds drain.max_ticket_minutes"
                    f" ({self.c.config.drain.max_ticket_minutes}m); never dispatched",
                    f"lower `- stuck:` in {ticket_path(t.stem)} to at most"
                    f" {self.c.config.drain.max_ticket_minutes}m (split the ticket if it cannot fit)")
                continue
            session = self.c.control.providers
            if session.active and budget and t.stem not in held:
                tier, _ = caps.capability(t, events)
                latest = next((e.body for e in reversed(events)
                               if e.type == EventType.STATE_TRANSITION and e.ticket == t.stem), {})
                route = latest.get('provider_drought', {'tier': tier, 'surface': 'implement'})
                drought = session.drought(self.c.config, route['tier'], route['surface'])
                if drought:
                    session.park(t.stem, drought)
                    if drought.deadline is not None:
                        self.provider_deadlines.append(drought.deadline)
                    continue
            if last.get(t.stem) is None:
                fresh.append(t)
            elif t.stem not in held and budget:
                reoffers.append(t)
        if fresh:
            return fresh[0], False
        if reoffers:
            return reoffers[0], True
        return None, False

    async def _run_one(self, ticket: Ticket, reoffer: bool, sha: str) -> None:
        if self._stopping():
            return
        stem = ticket.stem
        if hasattr(self.dispatch, 'prepare'):
            await self.dispatch.prepare()
        start_run(self.c.journal, stem, sha, reoffer=reoffer)
        async def abort():
            await self.dispatch.abort_current()
        terminal = await self._await_controlled(asyncio.create_task(self.dispatch(ticket)), abort,
            self.c.control.sleep if self.c.control is not None else self.c.sleep)
        if self._stopping():
            return
        if terminal not in TERMINAL_STATES:
            raise ValueError(f"stage seam returned {terminal!r}, not a terminal state {sorted(TERMINAL_STATES)}")
        body = next(e.body for e in reversed(self.c.journal.read())
                    if e.type == EventType.STATE_TRANSITION and e.ticket == stem)
        if body["to"] != terminal:
            raise ValueError(f"stage seam returned {terminal!r} for {stem} but journaled {body['to']!r};"
                             " the seam must journal the run's single terminal transition")
        if terminal == "merged":
            self.report.merged.append(stem)

    async def _run_retro(self, *, forced):
        await self._await_controlled(asyncio.create_task(self.retro(forced=forced)),
                                     self.retro.abort_current, self.c.sleep)

    async def _await_controlled(self, task, abort, sleep):
        from tempo.control import protected_cleanup

        consumer = self.c.control
        async def control() -> None:
            while not task.done():
                await sleep(0.1)
                consumer.inbox.consume()
                if self._stopping():
                    await consumer.apply_kill(abort, task)
                    return

        monitor = asyncio.create_task(control()) if consumer is not None else None
        owned = (task, monitor) if monitor is not None else (task,)
        try:
            await asyncio.wait(owned, return_when=asyncio.FIRST_COMPLETED)
            if monitor is not None and self._stopping():
                await asyncio.shield(monitor)
                self.report.killed = True
                return
            if monitor is not None and monitor.done():
                monitor.result()
            return task.result()
        finally:
            async def cleanup() -> None:
                failed_stop = False
                try:
                    if self._stopping():
                        try:
                            await consumer.apply_kill(abort, task)
                        except BaseException:
                            failed_stop = True
                            raise
                finally:
                    failed_control = failed_stop or (monitor is not None and monitor.done()
                        and not monitor.cancelled() and monitor.exception() is not None)
                    for pending in owned:
                        # Neither a failed decision nor a failed abort authorizes
                        # dispatch cancellation. Observe its ordinary cleanup instead.
                        if pending is task and failed_control:
                            continue
                        if not pending.done() and not pending.cancelling():
                            pending.cancel()
                    results = await asyncio.gather(*owned, return_exceptions=True)
                    for result in results:
                        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                            raise result
            await protected_cleanup(asyncio.create_task(cleanup()))

    def _settle(self, scan: _Scan, events: list[Event], last: Mapping[str, str], held: set[str]) -> None:
        settled = settled_dependencies(events)
        awaiting = reject_queue(events)
        for stem in sorted(scan.tickets):
            if (last.get(stem) not in SETTLED | RETIRED | {None, "running"}
                    and caps.spent(self.c.config.caps, events, stem) is not None
                    and stem not in awaiting):
                body = next(e.body for e in reversed(events)
                            if e.type == EventType.STATE_TRANSITION and e.ticket == stem)
                if body.get("to") == PREMISE and "render_over_bound" in body.get("reason", "").split(","):
                    continue
                if 'provider_drought' in body:
                    continue
                if body.get("routed") != "reject_queue":
                    self.c.journal.append(EventType.SIGNAL, {"signal": "reject_arrival"}, ticket=stem)
                    awaiting[stem] = body
        for stem, t in sorted(scan.tickets.items()):
            if t.frontmatter.state != "confirmed" or last.get(stem) in SETTLED | RETIRED:
                continue
            if stem in self.over_budget:
                self.report.parked.append(self.over_budget[stem])
            elif unmet := tuple(d for d in t.depends if d not in settled):
                self.report.blocked.append((stem, unmet))
            elif (to := last.get(stem)) is None:
                self.report.unadmitted.append(stem)
            else:
                body = next(e.body for e in reversed(events)
                            if e.type == EventType.STATE_TRANSITION and e.ticket == stem)
                where = f" at {body['stage']}" if "stage" in body else ""
                spent_cap = caps.spent(self.c.config.caps, events, stem)
                drawn = caps.draws(events, stem, spent_cap or "retry")
                cap_limit = getattr(self.c.config.caps, spent_cap or "retry")
                if stem in held:
                    why, road = f"{to}{where}; parked until its committed ticket.md changes", self._premise_road(t)
                elif spent_cap is None:  # only a halt leaves budget unspent: the continuing drain re-offers it
                    why, road = (f"{to}{where}; {cap_limit - drawn} of {cap_limit} retry units left",
                                 "`uv run python -m tempo drain` re-offers it")
                else:
                    why, road = (f"{to}{where}; {caps.spent_reason(spent_cap)} ({drawn}/{cap_limit})",
                                 "read the findings below and the engine log; fix the cause in the plan (or the"
                                 " ticket if provably not the plan's) and author a successor stem -- a"
                                 " `ticket.md` edit never re-arms the cap")
                if stem in awaiting:
                    road = (f"edit {ticket_path(stem)}, then "
                            f"uv run python -m tempo confirm {stem}; or retire it with "
                            f"uv run python -m tempo reject {stem}")
                self.report.parked.append(Parked(stem, why, road, self._findings(stem)))
        self.report.invalid = sorted(scan.invalid.items())
        self.report.drafts = list(scan.drafts)

    def _premise_road(self, t: Ticket) -> str:
        """`source`-keyed (section 18): the release is the same commit, only where the fix originates differs."""
        body = next(e.body for e in reversed(self.c.journal.read())
                    if e.type == EventType.STATE_TRANSITION and e.ticket == t.stem)
        if "render_over_bound" in body.get("reason", "").split(","):
            return "shrink or split the committed ticket text and rerun drain"
        rel = ticket_path(t.stem)
        return f"answer the premise findings below by editing {rel} and committing it -- the content change releases it"

    def _findings(self, stem: str) -> tuple[str, ...]:
        """Where the parked stem's detail lives: its durable ticket-plane artifacts (section 10)."""
        root = self.c.repo / TICKETS_DIR / stem
        return tuple(p.relative_to(self.c.repo).as_posix() for p in sorted(root.rglob("*"))
                     if p.is_file() and p.name != TICKET_FILE)

    def _halt(self, scan: _Scan, events: list[Event], last: Mapping[str, str], held: set[str]) -> Report:
        hours = self.c.config.drain.max_runtime_hours
        self.c.journal.append(EventType.SIGNAL, {"signal": HALT_SIGNAL, "ceiling": CEILING, "hours": hours,
                                                 "merged": list(self.report.merged)})
        self._settle(scan, events, last, held)
        self.report.halted = f"{hours}h elapsed"
        return self.report
