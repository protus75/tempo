"""Invariant auditor coverage over direct journal fixtures and the production drain."""

from tempo.audit import INVARIANTS, audit, audit_journal
from tempo.journal import Event, EventType, Journal
from tests.test_drain_reentry import drain
from tests.test_stages import agent, implement_reply, verdict
from tests.test_terminal import author, clock, diagnosis_reply, repo  # noqa: F401 -- fixture


COMMIT = "a" * 40


def event(type_: str, *, ticket: str | None = "ticket", key: str | None = None,
          body: dict | None = None, ts: str = "2026-10-05T00:00:00+00:00") -> Event:
    return Event(1, type_, ts, ticket, key, body or {})


def test_a_production_drain_with_a_merge_and_parked_red_journal_is_green(repo):
    author(repo)
    author(repo, "red")
    premise = {"code": "premise", "message": "generated", "paved_road": "edit the generator"}
    code, _ = drain(repo, [agent({"tempo/thing.py": "ok\n"}), verdict(),
                            implement_reply("premise_failed", [premise]), diagnosis_reply()])

    assert code == 0
    assert audit_journal(Journal(repo / ".tempo" / "state", clock)) == []


def test_each_invariant_reports_its_own_planted_violation():
    fixtures = {
        "one_terminal_per_run": (
            event(EventType.STATE_TRANSITION, body={"to": "running"}),
            event(EventType.STATE_TRANSITION, body={"to": "merged", "commit": COMMIT}),
            event(EventType.STATE_TRANSITION, body={"to": "gate_failed"}),
        ),
        "declared_cap": (event(EventType.CAP_CONSUMED, body={"cap": "unknown"}),),
        "merged_effects_paired": (
            event(EventType.STATE_TRANSITION, body={"to": "running"}),
            event(EventType.EFFECT_INTENT, key="effect"),
            event(EventType.STATE_TRANSITION, body={"to": "merged", "commit": COMMIT}),
        ),
        "merged_carries_commit": (
            event(EventType.STATE_TRANSITION, body={"to": "running"}),
            event(EventType.STATE_TRANSITION, body={"to": "merged", "commit": "short"}),
        ),
        "closed_run_states": (event(EventType.STATE_TRANSITION, body={"to": "unknown"}),),
        "segment_ts_monotonic": (
            event(EventType.SIGNAL, ts="2026-10-05T01:00:00+00:00"),
            event(EventType.SIGNAL, ts="2026-10-05T00:00:00+00:00"),
        ),
    }

    assert tuple(fixtures) == INVARIANTS
    for name, events in fixtures.items():
        assert [v.invariant for v in audit([events])] == [name]


def test_exempt_runs_and_null_settlement_are_green():
    events = (
        event(EventType.STATE_TRANSITION, ticket="failed", body={"to": "running"}),
        event(EventType.EFFECT_INTENT, ticket="failed", key="unpaired"),
        event(EventType.STATE_TRANSITION, ticket="failed", body={"to": "gate_failed"}),
        event(EventType.STATE_TRANSITION, ticket="orphan", body={"to": "running"}),
        event(EventType.STATE_TRANSITION, ticket="killed", body={"to": "running"}),
        event(EventType.STATE_TRANSITION, ticket="killed", body={"to": "gate_failed"}),
        event(EventType.STATE_TRANSITION, ticket="killed", body={"to": "rejected"}),
        event(EventType.STATE_TRANSITION, ticket="settled", body={"to": "running"}),
        event(EventType.STATE_TRANSITION, ticket="settled", body={"to": "merged", "commit": None}),
    )

    assert audit([events]) == []


def test_timestamp_monotonicity_is_scoped_to_each_segment():
    first = (event(EventType.SIGNAL, ts="2026-10-05T02:00:00+00:00"),)
    second = (event(EventType.SIGNAL, ts="2026-10-05T01:00:00+00:00"),)

    assert audit([first, second]) == []


def test_provider_drought_is_runless_only_without_an_open_run():
    park = event(EventType.STATE_TRANSITION, body={'to': 'infra_error', 'reason': 'provider_drought',
        'provider_drought': {'tier': 'medium', 'surface': 'implement', 'providers': ['codex']}})
    running = event(EventType.STATE_TRANSITION, body={'to': 'running'})
    assert audit([(park, running, park, park)]) == []
    duplicate = event(EventType.STATE_TRANSITION, body={'to': 'infra_error', 'reason': 'provider_drought'})
    assert [v.invariant for v in audit([(running, park, duplicate)])] == ['one_terminal_per_run']
