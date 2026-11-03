"""Failure-spine cap fold, terminal draws, and parked-stem eligibility."""

import pytest

from tempo.artifacts import StageResult
from tempo.__main__ import main
from tempo.caps import CAPS, IDENTICAL_K, capability, consume, draws, identical, next_rung, remaining, spent, spent_reason
from tempo.config import Caps, load_config
from tempo.journal import EventType, Journal
from tempo.tickets import validate_ticket
from tests.test_cli import git_out
from tests.support import Clock, commit_ticket, confirmed, journal, make_root
from tests.support import ENV, STEM, author, clock, repo


def test_fold_counts_only_named_cap_and_stem_across_ticket_revisions(tmp_path):
    j = Journal(tmp_path, clock)
    for body, stem in [
        ({"cap": "infra", "ticket_sha": "first"}, "red"),
        ({"cap": "infra", "ticket_sha": "second"}, "red"),
        ({"cap": "infra"}, "red"),
        ({"cap": "retry", "ticket_sha": "second"}, "red"),
        ({"cap": "infra", "ticket_sha": "second"}, "other"),
    ]:
        j.append(EventType.CAP_CONSUMED, body, ticket=stem)
    j.append(EventType.STATE_TRANSITION, {"to": "infra_error"}, ticket="red")
    events = j.read()
    config = Caps(infra=4, retry=2)
    assert CAPS == ("decisions", "retry", "infra", "premise_bounce")
    assert draws(events, "red", "infra") == 3
    assert draws(events, "red", "retry") == 1
    assert draws(events, "other", "infra") == 1
    assert remaining(config, events, "red", "infra") == 1
    assert spent(config, events, "red") is None
    j.append(EventType.CAP_CONSUMED, {"cap": "decisions"}, ticket="red")
    assert spent(Caps(decisions=1, retry=1, infra=3), j.read(), "red") == "decisions"
    assert spent_reason("infra") == "infra cap spent"


def test_invalid_cap_refuses_all_accounting_and_writer(tmp_path):
    j = Journal(tmp_path, clock)
    for operation in (
        lambda: draws(j.read(), "red", "unknown"),
        lambda: remaining(Caps(), j.read(), "red", "unknown"),
        lambda: consume(j, "red", "unknown", "sha"),
    ):
        with pytest.raises(ValueError, match="CAPS"):
            operation()
    assert j.read() == []
    consume(j, "red", "infra", "sha")
    event, = j.read()
    assert event.type == EventType.CAP_CONSUMED
    assert event.ticket == "red" and event.body == {"cap": "infra", "ticket_sha": "sha"}


def test_retry_rung_is_rejected_on_other_caps_and_operator_keep_resets_it(repo):
    author(repo)
    ticket = validate_ticket(STEM, (repo / "tickets" / STEM / "ticket.md").read_text(), repo)
    j = Journal(repo / ".tempo" / "state", clock)
    rung = {"tier": "high", "effort": "max"}
    with pytest.raises(ValueError, match="retry"):
        consume(j, STEM, "infra", "sha", rung=rung)
    assert capability(ticket, j.read()) == ("medium", "medium")
    consume(j, STEM, "retry", "sha", rung=rung)
    assert capability(ticket, j.read()) == ("high", "max")
    j.append(EventType.SIGNAL, {"signal": "reject_verdict", "verdict": "keep", "actor": "machine"}, ticket=STEM)
    assert capability(ticket, j.read()) == ("high", "max")
    j.append(EventType.SIGNAL, {"signal": "reject_verdict", "verdict": "keep", "actor": "operator"}, ticket=STEM)
    assert capability(ticket, j.read()) == ("medium", "medium")
    consume(j, STEM, "retry", "sha", rung={"tier": "medium", "effort": "high"})
    assert capability(ticket, j.read()) == ("medium", "high")


ROUTING = """schema_version: 1
state_dir: .tempo/state
review: {}
merge: {}
hosts:
  codex:
    models_by_tier: {low: A, medium: A, high: B, max: B}
    pins: {review: C, requisition_review: C}
orchestrator:
  models: {codex: C}
engine_plane_safety_inventory: [config.yaml]
"""


def test_next_rung_skips_the_same_host_model(tmp_path):
    (tmp_path / "config.yaml").write_text(ROUTING)
    config = load_config(None, cwd=tmp_path)
    assert next_rung(config, "codex", "low", "medium") == {"tier": "high", "effort": "medium"}
    assert next_rung(config, "codex", "high", "medium") == {"tier": "high", "effort": "high"}
    assert next_rung(config, "codex", "high", "max") is None


def test_identical_counts_the_last_k_minus_one_terminal_reasons(tmp_path):
    j = Journal(tmp_path, clock)
    for reason in ("logic", "scope", "scope"):
        j.append(EventType.STATE_TRANSITION, {"to": "gate_failed", "reason": reason}, ticket="red")
    j.append(EventType.STATE_TRANSITION, {"to": "gate_failed", "reason": "scope"}, ticket="other")
    events = j.read()
    assert IDENTICAL_K == 3
    assert identical(events, "red", "scope") and not identical(events, "red", "logic")
    assert not identical(events, "other", "scope")
