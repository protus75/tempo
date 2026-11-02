"""The `status` projection (TEMPO_PLAN.md section 13 touchpoint 5): a deterministic, write-free fold of the journal.

A projection, never an authority: no gate or dispatch decision reads its rendering.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from tempo.journal import EventType, Event
from tempo.retro import Window, project_window
from tempo.scorecard import Scorecard, project_scorecard
from tempo.tickets import INTAKE_SIGNAL


@dataclass(frozen=True)
class Status:
    merged: tuple[str, ...]
    in_flight: tuple[str, ...]
    blocked: tuple[tuple[str, str], ...]  # (stem, the non-merged terminal its last run ended in)
    intake: tuple[tuple[str, str, str, str], ...]  # (stem, source, state, commit) per intake commit
    reject: tuple[tuple[str, Mapping], ...] = ()
    tombstones: tuple[tuple[str, str, int], ...] = ()
    reopens: tuple[Mapping, ...] = ()
    scorecard: Scorecard = field(default_factory=lambda: project_scorecard(project_window(())))
    provenance: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


def reject_queue(events: Iterable[Event]) -> dict[str, Mapping]:
    """Latest routed terminal or legacy arrival still awaiting an operator verdict."""
    terminals: dict[str, Mapping] = {}
    waiting: dict[str, Mapping] = {}
    for e in events:
        if e.ticket is None:
            continue
        if e.type == EventType.STATE_TRANSITION and e.body.get("to") != "running":
            terminals[e.ticket] = e.body
            waiting.pop(e.ticket, None)
            if e.body.get("routed") == "reject_queue":
                waiting[e.ticket] = e.body
        elif e.type == EventType.SIGNAL and e.body.get("signal") == "reject_arrival":
            if e.ticket in terminals:
                waiting[e.ticket] = terminals[e.ticket]
        elif e.type == EventType.SIGNAL and e.body.get("signal") == "reject_verdict":
            waiting.pop(e.ticket, None)
    return waiting


def last_states(events: Iterable[Event]) -> dict[str, str]:
    """Each stem's latest run-state transition target."""
    return {e.ticket: e.body["to"] for e in events
            if e.type == EventType.STATE_TRANSITION and e.ticket is not None and "to" in e.body}


def project(events: Iterable[Event], *, box=None, policy=None,
            window: Window | None = None, provenance: Mapping[str, str] | None = None) -> Status:
    events = list(events)
    last = last_states(events)
    intake = tuple(
        (e.ticket, e.body["source"], e.body["state"], e.body["commit"]) for e in events
        if e.type == EventType.SIGNAL and e.body.get("signal") == INTAKE_SIGNAL and e.ticket is not None
    )
    return Status(
        merged=tuple(sorted(s for s, to in last.items() if to == "merged")),
        in_flight=tuple(sorted(s for s, to in last.items() if to == "running")),
        blocked=tuple(sorted((s, to) for s, to in last.items() if to not in ("merged", "running"))),
        intake=intake,
        reject=tuple(sorted(reject_queue(events).items())),
        tombstones=tuple((m.id, m.message_class, m.reports) for m in
                         box.confirmed_tombstones(policy)) if box is not None else (),
        reopens=tuple(e.body for e in events if e.type == EventType.SIGNAL
                      and e.body.get("kind") == "tombstone_auto_reopened"),
        scorecard=project_scorecard(window if window is not None else project_window(events)),
        provenance=MappingProxyType(dict(provenance or {})),
    )


def render(status: Status) -> str:
    def block(name: str, lines: Iterable[str]) -> str:
        return f"{name}:\n" + ("\n".join(f"- {line}" for line in lines) or "(none)")

    card = status.scorecard
    totals = [f"boundary: {card.boundary}", f"merged tickets: {card.merged_ticket_count}"]
    totals.extend(f"{kind}: {count}" for kind, count in sorted(card.signal_counts.items()))
    totals.append(f"gate failures: {card.gate_failure_count}")
    table = ["## Surface scorecard",
        "| Surface | Evaluated tickets | Catches | Escapes | Bypass count | Catch rate | Escape rate | Prune candidate |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    for row in card.surfaces:
        surface = row.surface.replace("|", "\\|").replace("\n", " ")
        table.append(f"| {surface} | {row.evaluated_tickets} | {row.catches} | {row.escapes} | "
                     f"{row.bypass_count} | {row.catch_rate:.4f} | {row.escape_rate:.4f} | "
                     f"{str(row.prune_candidate).lower()} |")
    return "\n\n".join((
        block("merged", status.merged),
        block("in flight", status.in_flight),
        block("blocked", (f"{stem}: {to}" for stem, to in status.blocked)),
        block("intake", (f"{stem}: source: {source}, state: {state}, commit {commit[:12]}"
                         for stem, source, state, commit in status.intake)),
        block("Reject queue", (f"{stem}: {body['to']} at {body.get('stage', 'unknown')} -- "
                               f"confirm {stem} after editing, or reject {stem}"
                               for stem, body in status.reject)),
        block("Confirmed-class tombstones", (f"{id}: {cls}, {reports} re-reports"
                                              for id, cls, reports in status.tombstones)),
        block("Tombstone reopens", (f"{body['box_id']}: reopened at {body['reports']} re-reports"
                                    for body in status.reopens)),
        block("current-window scorecard", totals),
        block("window provenance", (f"{key}: {value}" for key, value in sorted(status.provenance.items()))),
        "\n".join(table),
    )) + "\n"
