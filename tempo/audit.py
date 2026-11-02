"""Journal invariants for the test-harness ladder (TEMPO_PLAN.md section 15)."""

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field

from tempo.caps import CAPS
from tempo.journal import Event, EventType, Journal, TERMINAL_STATES


INVARIANTS = (
    "one_terminal_per_run",
    "declared_cap",
    "merged_effects_paired",
    "merged_carries_commit",
    "closed_run_states",
    "segment_ts_monotonic",
)

_COMMIT = re.compile(r"[0-9a-fA-F]{40}")


@dataclass(frozen=True)
class Violation:
    invariant: str
    ticket: str | None
    detail: str


@dataclass
class _Run:
    terminal: str | None = None
    intents: list[str | None] = field(default_factory=list)
    completions: set[str | None] = field(default_factory=set)


def audit(segments: Iterable[tuple[Event, ...]], caps: Collection[str] = CAPS) -> list[Violation]:
    """Fold the journal's ordered segments, returning every invariant violation."""
    violations: list[Violation] = []
    runs: dict[str, _Run] = {}

    for segment in segments:
        previous_ts: str | None = None
        for event in segment:
            if previous_ts is not None and event.ts < previous_ts:
                violations.append(Violation(
                    "segment_ts_monotonic", event.ticket,
                    f"timestamp {event.ts!r} precedes {previous_ts!r}",
                ))
            previous_ts = event.ts

            if event.type == EventType.CAP_CONSUMED and event.body.get("cap") not in caps:
                violations.append(Violation(
                    "declared_cap", event.ticket,
                    f"cap {event.body.get('cap')!r} is not declared",
                ))

            if event.type == EventType.STATE_TRANSITION:
                state = event.body.get("to")
                if state != "running" and state not in TERMINAL_STATES:
                    violations.append(Violation(
                        "closed_run_states", event.ticket,
                        f"state {state!r} is not in the closed run-state vocabulary",
                    ))
                if event.ticket is not None:
                    if state == "running":
                        runs[event.ticket] = _Run()
                    elif state in TERMINAL_STATES:
                        run = runs.get(event.ticket)
                        if 'provider_drought' in event.body and (run is None or run.terminal is not None):
                            continue
                        if run is None:
                            if state != "rejected":
                                violations.append(Violation(
                                    "one_terminal_per_run", event.ticket,
                                    f"terminal {state!r} has no open run",
                                ))
                        elif run.terminal is not None:
                            if state != "rejected":
                                violations.append(Violation(
                                    "one_terminal_per_run", event.ticket,
                                    f"run already ended {run.terminal!r} before {state!r}",
                                ))
                        else:
                            run.terminal = state

                        if state == "merged":
                            commit = event.body.get("commit")
                            valid_commit = isinstance(commit, str) and bool(_COMMIT.fullmatch(commit))
                            if "commit" not in event.body:
                                violations.append(Violation(
                                    "merged_carries_commit", event.ticket,
                                    "merged transition has no commit",
                                ))
                            elif not valid_commit:
                                violations.append(Violation(
                                    "merged_carries_commit", event.ticket,
                                    "merged transition carries an invalid commit",
                                ))
                            if run is not None:
                                for key in run.intents:
                                    if key not in run.completions:
                                        violations.append(Violation(
                                            "merged_effects_paired", event.ticket,
                                            f"effect intent {key!r} has no completion",
                                        ))

            if event.ticket is not None and event.type == EventType.EFFECT_INTENT:
                if (run := runs.get(event.ticket)) is not None:
                    run.intents.append(event.key)
            elif event.ticket is not None and event.type == EventType.EFFECT_COMPLETION:
                if (run := runs.get(event.ticket)) is not None:
                    run.completions.add(event.key)

    return violations


def audit_journal(journal: Journal, caps: Collection[str] = CAPS) -> list[Violation]:
    """Audit through the journal's one parser."""
    return audit(journal.read_segments(), caps)
