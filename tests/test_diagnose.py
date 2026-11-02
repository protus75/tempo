"""Diagnosis through the production run and drain composition."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tempo import runner
from tempo.__main__ import main
from tempo.artifacts import Diagnosis, DiagnosisReply, Finding, StageResult
from tempo.caps import consume, draws
from tempo.config import load_config
from tempo.journal import EventType, Journal
from tempo.llm import FakeLLM
from tempo.specs import data_close, data_open
from tempo.stages import StagesRun
from tests.test_stages import ENV, SNAG, STEM, agent, git, verdict
from tests.test_terminal import author, clock, diagnosis_reply, repo  # noqa: F401 -- fixture


def history(repo: Path):
    return [e for e in Journal(repo / ".tempo" / "state", clock).read() if e.ticket == STEM]


def invoke(repo: Path, script: list, verb: str = "run"):
    llm = FakeLLM(script)
    argv = [verb, STEM] if verb == "run" else [verb]
    code = main(argv, cwd=repo, env=ENV, clock=clock,
                pipeline=lambda c: runner.bind(c, llm))
    return code, llm


def diagnosis(repo: Path) -> Diagnosis:
    return Diagnosis.model_validate_json((repo / "tickets" / STEM / "diagnosis.json").read_text())


@pytest.mark.parametrize("reply", [
    {"verdict": "maybe", "lessons": ["do this"]},
    {"verdict": "retry", "lessons": []},
    {"verdict": "retry", "lessons": ["   "]},
    {"verdict": "retry", "lessons": ["x" * 301]},
    {"verdict": "retry", "lessons": ["x"] * 6},
    {"verdict": "retry", "lessons": ["x"], "extra": "forbidden"},
])
def test_reply_schema_refuses_invalid_shape(reply):
    with pytest.raises(ValidationError):
        DiagnosisReply.model_validate(reply)


def test_review_snag_orders_harvest_draw_effect_signal_and_terminal(repo):
    author(repo)
    lesson = "Read the review finding and change the word to ok."
    code, llm = invoke(repo, [agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG]),
                              diagnosis_reply(lesson)])
    assert code == runner.EXIT_TICKET
    events = history(repo)
    harvest = next(i for i, e in enumerate(events) if e.type == EventType.EFFECT_COMPLETION
                   and e.key == f"ticket-plane/{STEM}/0/harvest")
    draw = next(i for i, e in enumerate(events) if e.type == EventType.CAP_CONSUMED
                and e.body["cap"] == "decisions")
    effect = next(i for i, e in enumerate(events) if e.type == EventType.EFFECT_COMPLETION
                  and e.key and "/diagnose/" in e.key)
    signal = next(i for i, e in enumerate(events) if e.type == EventType.SIGNAL
                  and e.body.get("signal") == "diagnosis")
    terminal = next(i for i, e in enumerate(events) if e.type == EventType.STATE_TRANSITION
                    and e.body.get("to") == "gate_failed")
    assert harvest < draw < effect < signal < terminal
    assert events[draw].body == {"cap": "decisions", "ticket_sha": git(
        repo, "rev-parse", f"HEAD:tickets/{STEM}/ticket.md").strip()}
    assert events[signal].body == {"signal": "diagnosis", "attempt": 0, "verdict": "retry",
                                   "lessons": [lesson], "mechanical": None}
    assert events[terminal].body == {"to": "gate_failed", "stage": "review", "reason": "logic",
                                     "dispatch": "retry"}
    requests = [r for r in llm.requests if r.surface == "diagnose"]
    assert len(requests) == 1 and requests[0].tier == "medium"
    assert SNAG["message"] in requests[0].rendered
    assert (repo / "tickets" / STEM / "ticket.md").read_text() in requests[0].rendered
    record = diagnosis(repo)
    assert (record.stem, record.attempt, record.verdict, record.lessons, record.mechanical) == (
        STEM, 0, "retry", [lesson], None)
    assert f"tempo({STEM}): diagnosis" in git(repo, "log", "--format=%s", "main")


def test_invalid_verdict_reprompts_then_fails_closed_with_one_draw(repo):
    author(repo)
    cap = load_config(None, cwd=repo).caps.retry
    bad = json.dumps({"verdict": "maybe", "lessons": ["try again"]})
    code, llm = invoke(repo, [agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG])]
                       + [bad] * (cap + 1))
    assert code == runner.EXIT_TICKET
    requests = [r for r in llm.requests if r.surface == "diagnose"]
    assert len(requests) == cap + 1
    assert "invalid_artifact" in requests[1].rendered and "maybe" not in requests[1].rendered
    assert draws(history(repo), STEM, "decisions") == 1
    record = diagnosis(repo)
    assert record.verdict == "abandon-human" and record.lessons == []
    assert record.mechanical.startswith("no schema-valid verdict")


@pytest.mark.parametrize("outcome,findings,expected", [
    ("gate_failed", [], "workspace gone"),
])
def test_missing_workspace_and_no_call_short_circuits(repo, monkeypatch, outcome, findings, expected):
    author(repo)

    async def stage_seam(ctx, ticket):
        return StagesRun(attempt=0, results={"implement": StageResult(
            outcome=outcome, artifact=None, findings=findings)})

    monkeypatch.setattr(runner, "run_stages", stage_seam)
    code, llm = invoke(repo, [])
    assert code == runner.EXIT_TICKET and llm.requests == []
    assert draws(history(repo), STEM, "decisions") == 0
    signals = [e.body for e in history(repo) if e.type == EventType.SIGNAL and e.body.get("signal") == "diagnosis"]
    assert [s["mechanical"] for s in signals] == ([expected] if expected else [])


@pytest.mark.parametrize("cap,expected", [("decisions", "decisions cap spent"),
                                          ("infra", "infra cap spent")])
def test_spent_caps_short_circuit_after_infra_draw(repo, cap, expected):
    author(repo)
    # The infra case spends its sole unit on this terminal; the diagnosis case arrives already spent.
    if cap == "infra":
        path = repo / "config.yaml"
        path.write_text(path.read_text() + "\ncaps: {infra: 1}\n")
    else:
        consume(Journal(repo / ".tempo" / "state", clock), STEM, "decisions", "old-ticket-sha")
        # Default cap is six, so fill the remaining lineage draws.
        for _ in range(load_config(None, cwd=repo).caps.decisions - 1):
            consume(Journal(repo / ".tempo" / "state", clock), STEM, "decisions", "old-ticket-sha")
    code, llm = invoke(repo, [RuntimeError("provider failed")])
    assert code == runner.EXIT_TICKET
    assert [r.surface for r in llm.requests if r.surface == "diagnose"] == []
    assert draws(history(repo), STEM, "decisions") == (6 if cap == "decisions" else 0)
    assert diagnosis(repo).mechanical == expected
    assert next(e.body["mechanical"] for e in history(repo) if e.type == EventType.SIGNAL
                and e.body.get("signal") == "diagnosis") == expected


def test_drain_renders_two_attempts_lessons_and_no_spooled_raw_marker(repo):
    (repo / "tempo" / "thing.py").write_text("ok base\n")
    git(repo, "commit", "-am", "green verification base")
    author(repo)
    marker = "PRIVATE_SPOOLED_FIRST_ATTEMPT_MARKER"
    code, llm = invoke(repo, [RuntimeError(marker), diagnosis_reply("Avoid the first dead end."),
                              agent({"tempo/thing.py": "nope\n"}), diagnosis_reply("Write ok in thing.py."),
                              agent({"tempo/thing.py": "ok\n"}), verdict()], verb="drain")
    assert code == 0
    renders = [r.rendered for r in llm.requests if r.surface == "implement"]
    assert len(renders) == 3
    block = renders[2].split(data_open("ticket"), 1)[1].split(data_close("ticket"), 1)[0]
    criteria = block.split("## Acceptance criteria", 1)[1].split("## Verification", 1)[0]
    assert "> Avoid the first dead end." in criteria
    assert "> Write ok in thing.py." in criteria
    assert marker not in renders[1] and marker not in renders[2]


def test_no_lessons_keeps_raw_harvest_reason(repo):
    author(repo)
    marker = "RAW_REASON_WITHOUT_LESSONS"
    cap = load_config(None, cwd=repo).caps.retry
    bad = json.dumps({"verdict": "maybe", "lessons": ["not used"]})
    code, _ = invoke(repo, [RuntimeError(marker)] + [bad] * (cap + 1))
    assert code == runner.EXIT_TICKET and diagnosis(repo).lessons == []
    code, llm = invoke(repo, [agent({"tempo/thing.py": "ok\n"}), verdict()])
    assert code == 0
    render = next(r.rendered for r in llm.requests if r.surface == "implement")
    assert f"> RuntimeError: {marker}" in render


@pytest.mark.parametrize("workspace", [True, False])
def test_render_over_bound_calls_rework_but_not_diagnosis(repo, monkeypatch, workspace):
    from tempo import daemon
    from tests.test_drain import mechanical_first
    from tests.test_rework import order
    author(repo)
    mechanical_first(monkeypatch, STEM, workspace=workspace)
    calls = []
    apply = daemon.apply_rework
    async def observed(ctx, ticket, findings, **kwargs):
        calls.append((ctx, ticket, findings, kwargs))
        return await apply(ctx, ticket, findings, **kwargs)
    async def forbidden(*args, **kwargs):
        pytest.fail("prompt arithmetic called diagnosis")
    monkeypatch.setattr(daemon, "apply_rework", observed)
    monkeypatch.setattr(runner, "diagnose", forbidden)
    code, llm = invoke(repo, [order()] if workspace else [])
    assert code == runner.EXIT_TICKET and len(calls) == 1
    assert calls[0][2][0].code == "render_over_bound" and calls[0][3]["attempt"] == 0
    assert [r.surface for r in llm.requests] == (["rework"] if workspace else [])
    events = history(repo)
    assert not any(e.type == EventType.CAP_CONSUMED for e in events)
    assert events[-1].body == {"to": "premise_failed", "stage": "implement", "reason": "render_over_bound"}
    if workspace:
        record = json.loads((repo / f"tickets/{STEM}/attempts/0/harvest.json").read_text())
        assert "shrink or split" in record["findings"][-1]["paved_road"]
    else:
        from tempo.box import Box
        from tempo.seams import LocalFileSystem
        messages = Box(repo / ".tempo/state/box", LocalFileSystem()).messages()
        assert len(messages) == 1 and "no usable workspace" in messages[0].summary
        assert "shrink or split" in messages[0].summary
