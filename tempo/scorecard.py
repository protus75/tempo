"""Pure journal-window surface accounting."""

from dataclasses import dataclass
from collections.abc import Mapping
from types import MappingProxyType

from pydantic import ValidationError

from tempo.artifacts import ReviewVerdict
from tempo.driver import unwrap_fence
from tempo.gates import ENGINE_GATE_CODES
from tempo.journal import EventType
from tempo.retro import Window, measurements


@dataclass(frozen=True)
class SurfaceScorecardRow:
    surface: str
    evaluated_tickets: int
    catches: int
    escapes: int
    bypass_count: int
    catch_rate: float
    escape_rate: float
    prune_candidate: bool


@dataclass(frozen=True)
class Scorecard:
    boundary: str
    merged_ticket_count: int
    signal_counts: Mapping[str, int]
    gate_failure_count: int
    surfaces: tuple[SurfaceScorecardRow, ...]


def project_scorecard(window: Window) -> Scorecard:
    """Fold collected observations and existing findings; never perform an effect."""
    evidence = window.evidence or measurements(window)
    evaluated, caught, bypasses, pending, used_bypasses = {}, {}, {}, {}, {}
    seen = set()
    escapes = {}

    def observe(surface, ticket, failed=False, bypassed=False):
        evaluated.setdefault(surface, set()).add(ticket)
        caught.setdefault(surface, set())
        bypasses.setdefault(surface, 0)
        if failed and not bypassed:
            pending.setdefault(ticket, set()).add(surface)

    for event in window.events:
        stem, body = event.ticket, event.body
        if event.type == EventType.EFFECT_COMPLETION:
            if event.key in seen:
                continue
            seen.add(event.key)
            if (event.key or "").startswith("escape-attribution/"):
                result = body["result"]
                for match in result["matches"]:
                    for surface in match["surfaces"]:
                        escapes.setdefault(surface, set()).add((result["box_id"], match["ticket"]))
            used = set()
            for ticket, code, verdict, bypassed in window.completed_checks.get(event.key, ()):
                observe(code, ticket, verdict == "fail", bypassed)
                used_bypasses[ticket, code] = bypassed
                if bypassed and (ticket, code) not in used:
                    bypasses[code] += 1
                    used.add((ticket, code))
            parts = (event.key or "").split("/")
            if len(parts) == 6 and parts[0] == "llm" and parts[3] == "review" and stem:
                try:
                    verdict = ReviewVerdict.model_validate_json(unwrap_fence(body["result"]["text"]))
                except (KeyError, ValidationError):
                    continue  # A schema-invalid call is not a review verdict.
                observe("review", stem, verdict.verdict in {"snag", "rma"})
        elif event.type == EventType.STATE_TRANSITION and stem:
            state = body.get("to")
            if state == "gate_failed":
                # A gate terminal's reason is the runner's comma-separated finding codes.
                for code in body.get("reason", "").split(","):
                    if code in ENGINE_GATE_CODES and not used_bypasses.get((stem, code), False):
                        observe(code, stem, True)
            if state in {"merged", "gate_failed", "abandoned", "rejected"}:
                for surface in pending.get(stem, ()):
                    caught[surface].add(stem)

    rows = []
    for surface in sorted(evaluated.keys() | escapes.keys()):
        count, catches = len(evaluated.get(surface, ())), len(caught.get(surface, ()))
        escaped = len(escapes.get(surface, ()))
        rows.append(SurfaceScorecardRow(surface, count, catches, escaped, bypasses.get(surface, 0),
            catches / count if count else 0, escaped / count if count else 0,
            count >= 25 and catches == escaped == 0))
    signals = MappingProxyType(dict(evidence["spikes"]))
    return Scorecard(window.boundary, evidence["merged_count"],
                     signals, signals["gate-failure"], tuple(rows))
