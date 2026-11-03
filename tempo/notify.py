"""Effect-backed notification delivery: the `notify` tool and the stuck-call watchdog."""

from collections.abc import Sequence

from tempo.effects import Effects
from tempo.seams import NotificationFailed, Notifications

NOTIFY_TIMEOUT_S = 30.0


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


