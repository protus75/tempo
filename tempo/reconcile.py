"""Reconcile-on-entry (TEMPO_PLAN.md sections 6, 11, 18): reap orphaned in-flight runs before dispatch.

Called under the sole writer lock with no live run ownership, so an open run is provably dead.
Harvest precedes the `abandoned` terminal, recovery evidence and worktree removal.
"""

from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path

from tempo.git import Git
from tempo.journal import TERMINAL_STATES, Event, EventType, Journal, run_seq

OrphanHarvest = Callable[[str, int], Awaitable[None]]
RECOVERY_ALERT = "recovery_alert"


def orphans(events: Iterable[Event]) -> list[str]:
    """Stems whose run is still open: a `running`, or an intent with no completion, since their last terminal."""
    from tempo.mergequeue import held_admissions
    events = tuple(events)
    held = held_admissions(events)
    running: set[str] = set()
    intents: dict[str, set[str]] = {}
    for e in events:
        if e.ticket is None:
            continue
        if e.type == EventType.STATE_TRANSITION:
            if e.body.get("to") in TERMINAL_STATES:
                running.discard(e.ticket)
                intents.pop(e.ticket, None)
            elif e.body.get("to") == "running":
                running.add(e.ticket)
        elif e.type == EventType.EFFECT_INTENT:
            intents.setdefault(e.ticket, set()).add(e.key)
        elif e.type == EventType.EFFECT_COMPLETION:
            intents.get(e.ticket, set()).discard(e.key)
    return sorted((running | {stem for stem, keys in intents.items() if keys}) - held.keys())


async def reconcile(journal: Journal, git: Git, repo: Path, worktree_root: Path,
                    harvest: OrphanHarvest) -> list[str]:
    """Reap every orphan to `abandoned` and remove its worktree; return the reaped stems (empty: a no-op)."""
    reaped = orphans(journal.read())
    for stem in reaped:
        sequence = run_seq(journal.read(), stem)
        path = worktree_root / stem
        if path.exists():
            try:
                await harvest(stem, sequence)
            except Exception as e:
                journal.append(EventType.SIGNAL,
                               {"signal": "harvest_failed", "error": f"{type(e).__name__}: {e}"},
                               ticket=stem)
        journal.append(EventType.STATE_TRANSITION, {"to": "abandoned"}, ticket=stem)
        journal.append(EventType.SIGNAL,
                       {"kind": RECOVERY_ALERT, "disposition": "alert", "outcome": "abandoned",
                        "reason": "orphaned run", "run_seq": sequence}, ticket=stem)
        # Journal before wipe: a wipe that fails leaves a leftover the next run's teardown-and-create removes.
        if path.exists():
            await git.worktree_remove(repo, path)
    if reaped:
        await git.worktree_prune(repo)
    return reaped
