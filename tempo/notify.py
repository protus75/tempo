"""Effect-backed escalation delivery and journal-derived replay."""

import asyncio
import json
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path

from tempo.effects import Effects
from tempo.journal import Event, EventType, Journal
from tempo.seams import Clock, NotificationFailed, Notifications

NOTIFY_TIMEOUT_S = 30.0
# Failed commands previously appended an intent every 0.1s poll; retry per key, not per poll.
NOTIFY_RETRY_S = 60.0


def notification_key(owner: str, escalation: str, identity: str) -> str:
    return f"notify/{owner}/{escalation}/{identity}"


async def send(effects: Effects, notifications: Notifications, argv: Sequence[str], *,
               owner: str, escalation: str, identity: str, message: str,
               ticket: str | None):
    return await effects.run(lambda: notifications.notify([*argv, message]), ticket=ticket,
        key=notification_key(owner, escalation, identity))


class WatchdogNotifications:
    """Run-bound producer; transport alone owns the Effects and notification keys."""

    def __init__(self, *, effects: Effects, notifications: Notifications, argv: Sequence[str] | None,
                 owner: str, ticket: str | None, identity: str, log) -> None:
        self.effects, self.notifications, self.argv = effects, notifications, argv
        self.owner, self.ticket, self.identity = owner, ticket, identity
        self.log = log

    async def __call__(self, escalation: str) -> None:
        message = f'{self.owner}: {escalation}; inspect the run, then kill it, inject guidance, or let it cook'
        if self.argv is None:
            self.log.event('watchdog_notify_unset', owner=self.owner, escalation=escalation,
                           warning='push is off; set notify argv in config.yaml to deliver warnings')
            return
        try:
            await send(self.effects, self.notifications, self.argv, owner=self.owner, escalation=escalation,
                       identity=self.identity, message=message, ticket=self.ticket)
        except NotificationFailed as exc:
            self.log.event('notify_failed', owner=self.owner, escalation=escalation, error=str(exc))


def escalation(event: Event) -> tuple[str, str, str] | None:
    if event.type != EventType.SIGNAL:
        return None
    body, kind = event.body, event.body.get("kind")
    if kind == "storm_breaker_trip":
        identity = body["trip_id"]
        owner = body["emitting_origin"] or f"storm-breaker/{identity}"
    elif kind in {"merge_red_streak", "merge_tree_mismatch"}:
        identity, owner = body["hold_id"], event.ticket
    else:
        return None
    if not isinstance(owner, str) or not owner or not isinstance(identity, str) or not identity:
        raise ValueError("invalid escalation identity; repair the producing evidence and restart serve")
    return owner, kind, identity


def pending_escalations(events: Sequence[Event]) -> dict[str, Event]:
    completed = {e.key for e in events if e.type == EventType.EFFECT_COMPLETION}
    pending = {}
    for event in events:
        domain = escalation(event)
        if domain is not None:
            key = notification_key(*domain)
            if key not in completed:
                pending.setdefault(key, event)
    return pending


def _stamp(path: Path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


class NotificationReconciler:
    """One owned command at a time; maintenance never awaits external delivery."""

    def __init__(self, *, journal: Journal, clock: Clock, config_path: Path,
                 load: Callable, compose: Callable, log) -> None:
        self.journal, self.clock, self.config_path = journal, clock, config_path
        self.load, self.compose, self.log = load, compose, log
        self.config = self.effects = None
        self.config_stamp = self.journal_stamp = None
        self.pending: dict[str, Event] = {}
        self.retry_at = {}
        self.task: asyncio.Task | None = None

    def poll(self, *, startup: bool = False) -> None:
        if self.task is not None and self.task.done():
            task, self.task = self.task, None
            task.result()  # Unexpected errors retain serve's ordinary failure propagation.
        config_stamp = _stamp(self.config_path)
        if self.config is None or config_stamp != self.config_stamp:
            config = self.load()
            if self.config is not None and config.notify != self.config.notify:
                self.retry_at.clear()
            self.config, self.config_stamp = config, config_stamp
        if startup and self.config.notify is None:
            self.log.event("notify_unset", warning="push is off; escalation evidence remains journaled; "
                           "set notify argv in config.yaml to deliver pending evidence")
        journal_stamp = tuple((p.name, _stamp(p)) for p in sorted(self.journal.dir.glob("*.jsonl")))
        if self.effects is None or journal_stamp != self.journal_stamp:
            self.pending = pending_escalations(self.journal.read())
            self.journal_stamp = journal_stamp
            if self.effects is None:
                self.effects = Effects(self.journal)
        if self.task is not None or self.config.notify is None:
            return
        for key, event in self.pending.items():
            if key not in self.retry_at or self.clock() >= self.retry_at[key]:
                self.task = asyncio.create_task(self._deliver(key, event, self.config))
                break

    async def _deliver(self, key: str, event: Event, config) -> None:
        owner, kind, identity = escalation(event)
        message = json.dumps({**event.body, "owner": owner, "escalation": kind,
                              "identity": identity, "exit": "resume"}, sort_keys=True)
        try:
            # Transport outlives the producing run; a pending command is never an orphaned run.
            await send(self.effects, self.compose(config), config.notify, owner=owner,
                       escalation=kind, identity=identity, message=message, ticket=None)
        except NotificationFailed as exc:
            self.retry_at[key] = self.clock() + timedelta(seconds=NOTIFY_RETRY_S)
            self.log.event("notify_failed", key=key, error=str(exc))
        else:
            self.pending.pop(key, None)
            self.retry_at.pop(key, None)

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            finally:
                self.task = None
