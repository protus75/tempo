"""The ticket backlog (TEMPO_PLAN.md section 9): which committed tickets are eligible and in what order, the run
allocation that starts one, and the ticket-plane draft confirm and dead-dependency report."""

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

import yaml

from tempo import caps
from tempo.box import BOX_DIR
from tempo.checkout import Checkout, Refusal
from tempo.effects import Effects
from tempo.journal import TERMINAL_STATES, Event, EventType, Journal
from tempo.status import last_states, reject_queue
from tempo.storm import arrival_id
from tempo.tickets import (INTAKE_SIGNAL, Ticket, depends_cycle, parse_ticket, split_frontmatter, stamp,
                           ticket_path, validate_ticket)

# `already_satisfied` settles the stem as a no-op (section 7): it satisfies `depends` and is never re-offered.
SETTLED = frozenset({"merged", "already_satisfied"})
# A `rejected` stem is retired (section 11.2): never dispatched again.
RETIRED = frozenset({"rejected"})
PRIORITY = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

PREMISE = "premise_failed"
SUPERSEDES = "supersedes"


def premise_parked(events: Iterable[Event], stem: str, ticket_sha: str) -> bool:
    """The premise park (section 18): the stem's last run ended `premise_failed` against this same `ticket.md`."""
    last: str | None = None
    ran_sha: str | None = None
    for e in events:
        if e.type == EventType.STATE_TRANSITION and e.ticket == stem:
            last = e.body.get("to")
            if last == "running":
                ran_sha = e.body.get("ticket_sha")
    return last == PREMISE and ran_sha == ticket_sha


def authored_at(events: Iterable[Event]) -> dict[str, str]:
    """Each stem's age anchor (section 9): the `ts` of its FIRST ticket-plane authoring-commit event."""
    first: dict[str, str] = {}
    for e in events:
        if e.type == EventType.SIGNAL and e.body.get("signal") == INTAKE_SIGNAL and e.ticket is not None:
            first.setdefault(e.ticket, e.ts)
    return first


def sort_key(ticket: Ticket, authored: Mapping[str, str]) -> tuple:
    """(priority, age, stem): older first; a stem with no authoring event has no seniority and sorts last."""
    ts = authored.get(ticket.stem)
    return (PRIORITY[ticket.frontmatter.priority], ts is None, ts or "", ticket.stem)


def supersedes_maps(events: Iterable[Event]) -> dict[str, tuple[str, ...]]:
    return {e.ticket: tuple(e.body["successors"]) for e in events
            if e.type == EventType.SIGNAL and e.ticket is not None and e.body.get("signal") == SUPERSEDES}


def successor_leaves(stem: str, maps: Mapping[str, tuple[str, ...]]) -> frozenset[str]:
    if stem not in maps:
        return frozenset({stem})
    return frozenset(leaf for successor in maps[stem] for leaf in successor_leaves(successor, maps))


def settled_dependencies(events: Iterable[Event]) -> frozenset[str]:
    history = tuple(events)
    maps, states = supersedes_maps(history), last_states(history)
    return frozenset(stem for stem in states.keys() | maps.keys()
                     if all(states.get(leaf) in SETTLED for leaf in successor_leaves(stem, maps)))


def dead_dependencies(events: Iterable[Event]) -> frozenset[str]:
    history = tuple(events)
    maps, states = supersedes_maps(history), last_states(history)
    return frozenset(stem for stem in states.keys() | maps.keys()
                     if any(states.get(leaf) in {"rejected", "abandoned"}
                            for leaf in successor_leaves(stem, maps)))


def eligible(tickets: Mapping[str, Ticket], events: Sequence[Event]) -> list[Ticket]:
    """Confirmed, unsettled, unsuperseded tickets whose depends are settled, in (priority, age) order."""
    last = last_states(events)
    open_ = {s: t for s, t in tickets.items()
             if t.frontmatter.state == "confirmed" and last.get(s) not in SETTLED | RETIRED}
    edges = {s: list(t.depends) for s, t in open_.items()}
    for stem in sorted(open_):
        if cycle := depends_cycle(stem, edges):
            raise Refusal(f"`## Depends on` cycle among committed tickets: {' -> '.join(cycle)}",
                          f"edit one ticket on the cycle to drop the edge that inverts the intended order,"
                          f" then `drain` again")
    settled = settled_dependencies(events)
    maps = supersedes_maps(events)
    authored = authored_at(events)
    return sorted((t for t in open_.values() if t.stem not in maps and set(t.depends) <= settled),
                  key=lambda t: sort_key(t, authored))


def start_run(journal: Journal, stem: str, sha: str, *, reoffer: bool) -> None:
    """Allocate one run. A re-offer keeps a reject-queued stem and consumes one `retry` unit, except after a
    premise park or a provider drought."""
    if reoffer:
        history = journal.read()
        if stem in reject_queue(history):
            journal.append(EventType.SIGNAL, {"signal": "reject_verdict", "verdict": "keep", "actor": "machine"},
                           ticket=stem)
        body = next(e.body for e in reversed(history)
                    if e.type == EventType.STATE_TRANSITION and e.ticket == stem
                    and e.body.get("to") in TERMINAL_STATES)
        if body.get("to") != PREMISE and 'provider_drought' not in body:
            caps.consume(journal, stem, "retry", sha, rung=body.get("rung"))
    # The `ticket.md` the run answers: a `premise_failed` verdict parks the stem until this changes.
    journal.append(EventType.STATE_TRANSITION, {"to": "running", "ticket_sha": sha}, ticket=stem)


async def confirm_draft(stem: str, request_id: str, ctx) -> None:
    rel = ticket_path(stem)
    text = await ctx.git._run(ctx.repo, "show", f"main:{rel}")
    ticket = validate_ticket(stem, text, ctx.repo)
    if ticket.frontmatter.state not in ("draft", "confirmed") or (ctx.repo / rel).read_text() != text:
        raise ValueError("restore the committed draft before publishing a fresh confirmation")
    async def commit():
        if ticket.frontmatter.state == "confirmed":
            return {"commit": (await ctx.git._run(ctx.repo, "log", "-1", "--format=%H", "main", "--", rel)).strip()}
        ctx.fs.write(ctx.repo / rel, stamp(text, "state", "confirmed").encode())
        await ctx.git.add(ctx.repo, [rel])
        await ctx.git.commit(ctx.repo, f"tempo({stem}): confirmed", only=[rel])
        return {"commit": await ctx.git.rev_parse(ctx.repo, "HEAD")}
    result = await Effects(ctx.driver.journal).run(commit, key=f"ticket-plane/{stem}/confirmed/{request_id}", ticket=stem)
    if not any(e.ticket == stem and e.body.get("signal") == "draft_confirmed"
               and e.body.get("commit") == result["commit"] for e in ctx.driver.journal.read()):
        ctx.driver.journal.append(EventType.SIGNAL, {"signal": "draft_confirmed", **result}, ticket=stem)


def ticket_state(path: Path) -> str | None:
    split = split_frontmatter(path.read_text())
    return yaml.safe_load(split[0]).get("state") if split else None


async def dead_dependents(dead: str, checkout: Checkout) -> None:
    history = checkout.journal.read()
    last = last_states(history)
    deaths = dead_dependencies(history)
    from tempo.storm import storm_producer

    box = storm_producer(root=checkout.config.state_dir / BOX_DIR, fs=checkout.fs,
                         journal=checkout.journal, clock=checkout.clock)
    for path in sorted((checkout.repo / "tickets").glob("*/ticket.md")):
        stem = path.parent.name
        if stem == dead or ticket_state(path) != "confirmed" or last.get(stem) in {"merged", "already_satisfied", "rejected"}:
            continue
        ticket = parse_ticket(stem, path.read_text(), checkout.repo)
        for dependency in sorted(set(ticket.depends) & deaths):
            if not any(e.type == EventType.SIGNAL and e.ticket == stem
                   and e.body.get("signal") == "dead_dependency" and e.body.get("dead") == dependency
                       for e in history):
                box.enqueue(message_class="failure_report", origin=stem, stage="depends", outcome="rejected",
                            summary=f"{stem} depends on {dependency}, which was rejected or abandoned; re-wire, re-scope, or reject it",
                            occurrence_id=arrival_id("dead-dependency", stem, dependency))
                checkout.journal.append(EventType.SIGNAL, {"signal": "dead_dependency", "dead": dependency}, ticket=stem)
