"""Journal-backed per-host circuit breakers (AGENT_PLAN.md section 4.6)."""

from datetime import UTC, datetime, timedelta

from tempo.config import CircuitBreaker
from tempo.journal import EventType, Journal
from tempo.thresh import PROVIDER_CALL_OUTCOME, fold_breakers, record_outcome


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 2, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def rig(tmp_path, k=3):
    clock = Clock()
    journal = Journal(tmp_path, clock)

    def record(failure, host="claude"):
        record_outcome(journal, clock, CircuitBreaker(k=k, cooldown_minutes=10), host, failure,
                       ticket="t", call_key=f"llm/t/0/review/1/{len(journal.read())}")
    return clock, journal, record


def test_k_counted_failures_open_the_host_for_the_cooldown(tmp_path):
    clock, journal, record = rig(tmp_path)
    record("outage")
    record("unclassified")
    assert fold_breakers(journal, clock()).get("claude").open_until is None
    record("outage")
    opening = journal.read()[-1]
    assert opening.type == EventType.SIGNAL and opening.key is None and opening.ticket == "t"
    assert opening.body == {"signal": PROVIDER_CALL_OUTCOME, "provider": "claude",
                            "call_key": "llm/t/0/review/1/2", "failure_class": "outage",
                            "open_until": "2026-11-02T00:10:00+00:00"}
    assert fold_breakers(journal, clock())["claude"].open_until == clock() + timedelta(minutes=10)


def test_success_and_uncounted_failures_reset_the_streak(tmp_path):
    clock, journal, record = rig(tmp_path, k=2)
    record("outage")
    record(None)
    record("outage")
    record("quota_exhausted")
    record("outage")
    assert fold_breakers(journal, clock())["claude"].streak == 1
    assert all(e.body["open_until"] is None for e in journal.read())


def test_an_opening_expires_and_hosts_are_independent(tmp_path):
    clock, journal, record = rig(tmp_path, k=1)
    record("outage", host="codex")
    assert fold_breakers(journal, clock())["codex"].open_until is not None
    assert "claude" not in fold_breakers(journal, clock())
    clock.now += timedelta(minutes=10)
    state = fold_breakers(journal, clock())["codex"]
    assert (state.streak, state.open_until) == (0, None)
