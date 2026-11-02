"""Lineage-scoped failure-spine cap accounting (TEMPO_PLAN.md section 11.2)."""

from collections.abc import Iterable

from tempo.config import Caps, Config, ConfigSnapshot
from tempo.journal import Event, EventType, Journal
from tempo.tickets import Ticket

CAPS = ("decisions", "retry", "infra", "premise_bounce")
LEVELS = ("low", "medium", "high", "max")


def _check(cap: str) -> None:
    if cap not in CAPS:
        raise ValueError(f"cap {cap!r} is not one of CAPS {CAPS}")


def lineage(events: Iterable[Event], stem: str) -> tuple[Event, ...]:
    """The one lineage window for both cap counts and effective capability."""
    history = tuple(events)
    keep = next((i for i, e in reversed(list(enumerate(history)))
                 if e.type == EventType.SIGNAL and e.ticket == stem
                 and e.body.get("signal") == "reject_verdict" and e.body.get("verdict") == "keep"
                 and e.body.get("actor") == "operator"), -1)
    return history[keep + 1:]


def draws(events: Iterable[Event], stem: str, cap: str) -> int:
    """Count this stem's draws for one cap across all ticket content revisions."""
    _check(cap)
    return sum(e.type == EventType.CAP_CONSUMED and e.ticket == stem and e.body.get("cap") == cap
               for e in lineage(events, stem))


def remaining(caps_config: Caps | ConfigSnapshot, events: Iterable[Event], stem: str, cap: str) -> int:
    _check(cap)
    return getattr(caps_config, cap) - draws(events, stem, cap)


def spent(caps_config: Caps | ConfigSnapshot, events: Iterable[Event], stem: str) -> str | None:
    # Materialize once: callers may pass a journal iterator, not only a list.
    history = tuple(events)
    return next((cap for cap in CAPS if remaining(caps_config, history, stem, cap) <= 0), None)


def consume(journal: Journal, stem: str, cap: str, ticket_sha: str,
            *, rung: dict[str, str] | None = None) -> None:
    _check(cap)
    if rung is not None and cap != "retry":
        raise ValueError("a rung belongs only on a retry cap draw")
    body = {"cap": cap, "ticket_sha": ticket_sha}
    if rung is not None:
        body["rung"] = rung
    journal.append(EventType.CAP_CONSUMED, body, ticket=stem)


def capability(ticket: Ticket, events: Iterable[Event]) -> tuple[str, str]:
    """The authored capability or the latest retry rung inside the operator keep fold."""
    rung = next((e.body["rung"] for e in reversed(lineage(events, ticket.stem))
                 if e.type == EventType.CAP_CONSUMED and e.ticket == ticket.stem
                 and e.body.get("cap") == "retry" and "rung" in e.body), None)
    if rung is None:
        return ticket.frontmatter.agent_tier, ticket.frontmatter.agent_effort
    return rung["tier"], rung["effort"]


def next_rung(config: Config | ConfigSnapshot, host: str, tier: str, effort: str) -> dict[str, str] | None:
    """Move to the host's next distinct Implement model, then increase effort."""
    from tempo.call import resolve_model

    current = resolve_model(config, host, "implement", tier)
    for higher in LEVELS[LEVELS.index(tier) + 1:]:
        if resolve_model(config, host, "implement", higher) != current:
            return {"tier": higher, "effort": effort}
    if effort != "max":
        return {"tier": tier, "effort": LEVELS[LEVELS.index(effort) + 1]}
    return None


def spent_reason(cap: str) -> str:
    return f"{cap} cap spent"
