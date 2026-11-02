"""Failure-spine cap fold, terminal draws, and parked-stem eligibility."""

import pytest

from tempo import runner
from tempo.artifacts import StageResult
from tempo.__main__ import main
from tempo.caps import CAPS, capability, consume, draws, remaining, spent, spent_reason
from tempo.config import Caps
from tempo.journal import EventType, Journal
from tempo.llm import FakeLLM
from tempo.stages import StagesRun
from tempo.tickets import validate_ticket
from tests.test_cli import git_out
from tests.test_drain import Clock, Script, commit_ticket, confirmed, drain, journal, make_root
from tests.test_terminal import ENV, STEM, author, clock, diagnosis_reply, repo


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
    assert CAPS == ("diagnosis", "retry", "infra", "premise_bounce")
    assert draws(events, "red", "infra") == 3
    assert draws(events, "red", "retry") == 1
    assert draws(events, "other", "infra") == 1
    assert remaining(config, events, "red", "infra") == 1
    assert spent(config, events, "red") is None
    j.append(EventType.CAP_CONSUMED, {"cap": "diagnosis"}, ticket="red")
    assert spent(Caps(diagnosis=1, retry=1, infra=3), j.read(), "red") == "diagnosis"
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


def test_crashing_implement_draws_infra_immediately_before_terminal(repo):
    author(repo)
    llm = FakeLLM([RuntimeError("provider crashed"), diagnosis_reply()])
    assert main(["run", STEM], cwd=repo, env=ENV, clock=clock,
                pipeline=lambda c: runner.bind(c, llm)) == runner.EXIT_TICKET
    assert [request.surface for request in llm.requests if request.surface != "diagnose"] == ["implement"]
    events = [e for e in Journal(repo / ".tempo" / "state", clock).read() if e.ticket == STEM]
    terminal = next(i for i, e in enumerate(events)
                    if e.type == EventType.STATE_TRANSITION and e.body.get("to") == "infra_error")
    sha = git_out(repo, "rev-parse", f"HEAD:tickets/{STEM}/ticket.md").strip()
    infra = next(i for i, e in enumerate(events) if e.type == EventType.CAP_CONSUMED and e.body["cap"] == "infra")
    assert infra < terminal
    assert events[infra].body == {"cap": "infra", "ticket_sha": sha}
    assert all((e.type == EventType.CAP_CONSUMED and e.body["cap"] == "diagnosis")
               or (e.type == EventType.SIGNAL and e.body.get("signal") == "diagnosis")
               or (e.key is not None and ("/diagnose/" in e.key or e.key.endswith("/diagnosis")))
               for e in events[infra + 1:terminal])
    assert draws(events, STEM, "infra") == 1


@pytest.mark.parametrize("outcome,expected_draws", [("timeout", 1), ("gate_failed", 0)])
def test_other_terminals_draw_only_when_infra(repo, monkeypatch, outcome, expected_draws):
    author(repo)

    async def stage_seam(ctx, ticket):
        return StagesRun(attempt=0, results={"implement": StageResult(
            outcome=outcome, artifact=None, findings=[])})

    monkeypatch.setattr(runner, "run_stages", stage_seam)
    assert main(["run", STEM], cwd=repo, env=ENV, clock=clock,
                pipeline=lambda c: runner.bind(c, FakeLLM([]))) == runner.EXIT_TICKET
    events = [e for e in Journal(repo / ".tempo" / "state", clock).read() if e.ticket == STEM]
    assert events[-1].body == {"to": outcome, "stage": "implement", "reason": outcome,
                               "dispatch": "reject_queue", "routed": "reject_queue"}
    assert draws(events, STEM, "infra") == expected_draws
    if expected_draws:
        sha = git_out(repo, "rev-parse", f"HEAD:tickets/{STEM}/ticket.md").strip()
        assert next(e.body for e in events if e.type == EventType.CAP_CONSUMED) == {"cap": "infra", "ticket_sha": sha}


@pytest.mark.parametrize("cap", ["infra", "diagnosis"])
def test_spent_cap_parks_even_with_retry_remaining(tmp_path, capsys, cap):
    retry_limit = 1 if cap == "diagnosis" else 3
    root = make_root(tmp_path, f"caps: {{{cap}: 2, retry: {retry_limit}}}\n")
    commit_ticket(root, "red", confirmed())
    j = journal(root)
    j.append(EventType.STATE_TRANSITION, {"to": "gate_failed", "stage": "check"}, ticket="red")
    for sha in ("old", "new"):
        consume(j, "red", cap, sha)
    script = Script()
    assert drain(root, script, Clock()) == 0
    assert script.calls == []
    assert f"red: gate_failed at check; {cap} cap spent (2/2)" in capsys.readouterr().out
    assert draws(j.read(), "red", "retry") == 0
