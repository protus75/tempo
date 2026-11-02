"""Deterministic capability and Reject routing through the production drain."""

import json

import pytest

from tempo import runner
from tempo.__main__ import main
from tempo.caps import consume, draws, next_rung, remaining
from tempo.config import load_config
from tempo.journal import EventType, Journal
from tempo.llm import FakeLLM
from tests.test_stages import ENV, SNAG, STEM, agent, git, verdict
from tests.test_terminal import author, clock, repo  # noqa: F401 -- fixture


ROUTING = """schema_version: 1
state_dir: .tempo/state
providers:
  - name: fake
    kind: cli
    package: test-cli
    auth: FAKE_KEY
    models_by_tier: {low: A, medium: A, high: B, max: B}
    limits: {concurrency: 1}
routing:
  - {tier: low, surface: implement, candidates: [{provider: fake}]}
  - {tier: medium, surface: implement, candidates: [{provider: fake}]}
  - {tier: high, surface: implement, candidates: [{provider: fake}]}
  - {tier: max, surface: implement, candidates: [{provider: fake}]}
  - {tier: low, surface: review, candidates: [{provider: fake}]}
  - {tier: medium, surface: review, candidates: [{provider: fake}]}
  - {tier: high, surface: review, candidates: [{provider: fake}]}
  - {tier: max, surface: review, candidates: [{provider: fake}]}
review: {}
merge: {}
engine_plane_safety_inventory: [config.yaml]
"""


def diagnosis(verdict_: str) -> str:
    return json.dumps({"verdict": verdict_, "lessons": ["Apply the review finding."]})


def events(repo):
    return [e for e in Journal(repo / ".tempo" / "state", clock).read() if e.ticket == STEM]


def terminals(repo):
    return [e.body for e in events(repo) if e.type == EventType.STATE_TRANSITION
            and e.body.get("to") == "gate_failed"]


def run_drain(repo, script):
    llm = FakeLLM(script)
    code = main(["drain"], cwd=repo, env=ENV, clock=clock,
                pipeline=lambda c: runner.bind(c, llm))
    assert code == 0
    return llm


def test_next_rung_skips_same_model_and_missing_routes(repo):
    (repo / "config.yaml").write_text(ROUTING)
    config = load_config(None, cwd=repo)
    assert next_rung(config, "low", "medium") == {"tier": "high", "effort": "medium"}
    assert next_rung(config, "high", "medium") == {"tier": "high", "effort": "high"}
    assert next_rung(config, "high", "max") is None
    (repo / "config.yaml").write_text(ROUTING.replace(
        "  - {tier: medium, surface: implement, candidates: [{provider: fake}]}\n", "").replace(
        "  - {tier: medium, surface: review, candidates: [{provider: fake}]}\n", ""))
    assert next_rung(load_config(None, cwd=repo), "low", "medium") == {"tier": "high", "effort": "medium"}


def test_escalation_rung_reaches_next_implement_without_editing_ticket(repo):
    (repo / "config.yaml").write_text(ROUTING)
    author(repo)
    path = repo / "tickets" / STEM / "ticket.md"
    before = path.read_bytes()
    llm = run_drain(repo, [agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG]), diagnosis("escalate"),
                           agent({"tempo/thing.py": "ok\n"}), verdict()])
    first, = terminals(repo)
    assert first == {"to": "gate_failed", "stage": "review", "reason": "logic", "dispatch": "escalate",
                     "rung": {"tier": "high", "effort": "medium"}}
    retry, = [e.body for e in events(repo) if e.type == EventType.CAP_CONSUMED and e.body["cap"] == "retry"]
    assert retry["rung"] == first["rung"]
    implements = [r for r in llm.requests if r.surface == "implement"]
    assert [(r.tier, r.effort) for r in implements] == [("medium", "medium"), ("high", "medium")]
    reviews = [r for r in llm.requests if r.surface == "review"]
    assert [(r.tier, r.effort) for r in reviews] == [("medium", "medium"), ("high", "medium")]
    assert path.read_bytes() == before


@pytest.mark.parametrize("verdict_", ["reject", "abandon-human"])
def test_reject_verdicts_auto_keep_once_and_draw_down(repo, verdict_):
    author(repo)
    run_drain(repo, [agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG]), diagnosis(verdict_),
                     agent({"tempo/thing.py": "ok\n"}), verdict()])
    assert terminals(repo)[0] == {"to": "gate_failed", "stage": "review", "reason": "logic",
                                   "dispatch": "reject_queue", "routed": "reject_queue"}
    keeps = [e for e in events(repo) if e.type == EventType.SIGNAL
             and e.body.get("signal") == "reject_verdict"]
    assert [e.body for e in keeps] == [{"signal": "reject_verdict", "verdict": "keep", "actor": "machine"}]
    assert draws(events(repo), STEM, "retry") == 1
    assert remaining(load_config(None, cwd=repo).caps, events(repo), STEM, "retry") == 5


def test_exhausted_ladder_routes_and_auto_keeps(repo):
    (repo / "config.yaml").write_text(ROUTING)
    author(repo)
    path = repo / "tickets" / STEM / "ticket.md"
    path.write_text(path.read_text().replace("kind: feature", "kind: feature\nagent_tier: high\nagent_effort: max"))
    run_drain(repo, [agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG]), diagnosis("escalate"),
                     agent({"tempo/thing.py": "ok\n"}), verdict()])
    assert terminals(repo)[0]["dispatch"] == "reject_queue"
    assert terminals(repo)[0]["routed"] == "reject_queue"
    assert sum(e.body.get("actor") == "machine" for e in events(repo) if e.type == EventType.SIGNAL) == 1
    assert draws(events(repo), STEM, "retry") == 1


def test_same_retry_wall_climbs_on_second_terminal(repo):
    (repo / "config.yaml").write_text(ROUTING)
    author(repo)
    run_drain(repo, [agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG]), diagnosis("retry"),
                     agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG]), diagnosis("retry"),
                     agent({"tempo/thing.py": "ok\n"}), verdict()])
    first, second = terminals(repo)
    assert (first["dispatch"], second["dispatch"], second["rung"]) == (
        "retry", "escalate", {"tier": "high", "effort": "medium"})


def test_three_identical_reasons_climb_even_after_different_verdict(repo):
    (repo / "config.yaml").write_text(ROUTING)
    author(repo)
    llm = run_drain(repo, [agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG]), diagnosis("retry"),
                           agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG]), diagnosis("escalate"),
                           agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG]), diagnosis("retry"),
                           agent({"tempo/thing.py": "ok\n"}), verdict()])
    first, second, third = terminals(repo)
    assert [t["reason"] for t in (first, second, third)] == ["logic"] * 3
    assert third["dispatch"] == "escalate"
    assert third["rung"] == {"tier": "high", "effort": "high"}
    assert [(r.tier, r.effort) for r in llm.requests if r.surface == "implement"][-1] == ("high", "high")


def test_same_retry_wall_at_top_routes_to_queue(repo):
    (repo / "config.yaml").write_text(ROUTING)
    author(repo)
    path = repo / "tickets" / STEM / "ticket.md"
    path.write_text(path.read_text().replace("kind: feature", "kind: feature\nagent_tier: high\nagent_effort: max"))
    run_drain(repo, [agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG]), diagnosis("retry"),
                     agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG]), diagnosis("retry"),
                     agent({"tempo/thing.py": "ok\n"}), verdict()])
    first, second = terminals(repo)
    assert first["dispatch"] == "retry"
    assert second["dispatch"] == "reject_queue" and second["routed"] == "reject_queue"
    assert "rung" not in second


def test_operator_confirm_resets_effective_rung(repo):
    (repo / "config.yaml").write_text(ROUTING)
    author(repo)

    def run_one(script):
        llm = FakeLLM(script)
        assert main(["run", STEM], cwd=repo, env=ENV, clock=clock,
                    pipeline=lambda c: runner.bind(c, llm)) == 1
        return llm

    run_one([agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG]), diagnosis("escalate")])
    rung = terminals(repo)[-1]["rung"]
    sha = git(repo, "rev-parse", f"HEAD:tickets/{STEM}/ticket.md").strip()
    consume(Journal(repo / ".tempo" / "state", clock), STEM, "retry", sha, rung=rung)
    second = run_one([agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG]), diagnosis("reject")])
    assert next(r for r in second.requests if r.surface == "implement").tier == "high"
    assert main(["confirm", STEM], cwd=repo, env=ENV, clock=clock) == 0
    third = run_one([agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG]), diagnosis("reject")])
    assert (next(r for r in third.requests if r.surface == "implement").tier,
            next(r for r in third.requests if r.surface == "implement").effort) == ("medium", "medium")


@pytest.mark.parametrize("failure", [None, "review", "commit", "partial"])
def test_split_dispatches_reviewed_rework(repo, monkeypatch, failure):
    from tests.test_rework import order, requisition, successors
    author(repo)
    def split(req):
        assert req.surface == "rework"
        text = (repo / f"tickets/{STEM}/ticket.md").read_text().strip()
        return order("split", successors(text))
    if failure in {"commit", "partial"}:
        from tempo.git import Git
        commit = Git.commit
        async def refuse(self, root, message, **kwargs):
            if message.endswith(": rework"):
                if failure == "partial":
                    await commit(self, root, message, only=["tickets/piece-one/ticket.md"])
                raise RuntimeError("publication refused")
            return await commit(self, root, message, **kwargs)
        monkeypatch.setattr(Git, "commit", refuse)
    script = [agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG]), diagnosis("split"), split,
              requisition("rma") if failure == "review" else requisition()]
    if failure != "review":
        script.append(requisition())
    llm = FakeLLM(script)
    assert main(["run", STEM], cwd=repo, env=ENV, clock=clock,
                pipeline=lambda c: runner.bind(c, llm)) == 1
    history = events(repo)
    producing, = terminals(repo)
    expected = {"to": "gate_failed", "stage": "review", "reason": "logic"}
    if failure:
        expected.update(dispatch="reject_queue", routed="reject_queue")
        assert not any(e.body.get("signal") == "supersedes" or e.body.get("to") == "rejected" for e in history)
        harvest = json.loads((repo / f"tickets/{STEM}/attempts/0/harvest.json").read_text())
        assert len(harvest["findings"]) >= 2 and all(f["paved_road"] for f in harvest["findings"])
        assert git(repo, "status", "--porcelain") == ""
        assert not (repo / "tickets/piece-one/ticket.md").exists()
        assert not (repo / "tickets/piece-two/ticket.md").exists()
        # Keep the original parked and exercise the next drain's real intake.
        j = Journal(repo / ".tempo/state", clock)
        for _ in range(load_config(None, cwd=repo).caps.retry):
            consume(j, STEM, "retry", "old")
        before = [e for e in j.read() if e.body.get("signal") == "ticket_intake"]
        def pipeline(checkout):
            async def forbidden(ticket):
                pytest.fail("following drain dispatched refused split output")
            return forbidden
        assert main(["drain"], cwd=repo, env=ENV, clock=clock, pipeline=pipeline) == 0
        assert [e for e in j.read() if e.body.get("signal") == "ticket_intake"] == before
        assert git(repo, "status", "--porcelain") == ""
    else:
        mapping = next(e for e in history if e.body.get("signal") == "supersedes")
        terminal = next(e for e in history if e.body == expected)
        retirement = next(e for e in history if e.body == {"to": "rejected"})
        assert history.index(mapping) < history.index(terminal) < history.index(retirement)
        assert all((repo / f"tickets/{s}/ticket.md").is_file() for s in ("piece-one", "piece-two"))
    assert producing == expected
    assert draws(history, STEM, "diagnosis") == 1 and draws(history, STEM, "retry") == 0
    assert not (repo / f".tempo/state/worktrees/{STEM}").exists()


@pytest.mark.parametrize("action", ["escalate", "exhausted", "update"])
def test_rework_escalation_preserves_starting_capability_and_caps(repo, action):
    from tests.test_rework import order, requisition
    (repo / "config.yaml").write_text(ROUTING)
    author(repo)
    # Intake happens before the scripted model call; journal capability is deliberately higher than birth.
    starting = (repo / f"tickets/{STEM}/ticket.md").read_text()
    if action == "exhausted":
        starting = starting.replace("kind: feature", "kind: feature\nagent_tier: high\nagent_effort: max")
        (repo / f"tickets/{STEM}/ticket.md").write_text(starting)
    else:
        consume(Journal(repo / ".tempo/state", clock), STEM, "retry", "prior",
                rung={"tier": "high", "effort": "medium"})
    def reply(req):
        assert req.surface == "rework" and req.tier == "high"
        assert req.effort == ("max" if action == "exhausted" else "medium")
        current = (repo / f"tickets/{STEM}/ticket.md").read_text().strip()
        if action == "update":
            return order("update", [(STEM, current.replace("holds the word ok", "holds ok after narrowing"))])
        return order("escalate")
    script = [agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG]), diagnosis("split"), reply]
    if action == "update":
        script.append(requisition())
    if action == "escalate":
        script += [agent({"tempo/thing.py": "ok\n"}), verdict()]
        llm = run_drain(repo, script)
    else:
        llm = FakeLLM(script)
        assert main(["run", STEM], cwd=repo, env=ENV, clock=clock,
                    pipeline=lambda c: runner.bind(c, llm)) == 1
    terminal = terminals(repo)[0]
    if action == "exhausted":
        assert terminal["dispatch"] == terminal["routed"] == "reject_queue" and "rung" not in terminal
    elif action == "update":
        assert terminal["dispatch"] == "retry" and "routed" not in terminal and "rung" not in terminal
        text = (repo / f"tickets/{STEM}/ticket.md").read_text()
        assert "agent_tier: high" not in text and "agent_effort: high" not in text
        assert "holds ok after narrowing" in text
        assert draws(events(repo), STEM, "retry") == 1
    else:
        assert terminal["dispatch"] == "escalate" and terminal["rung"] == {"tier": "high", "effort": "high"}
        assert "routed" not in terminal
        retry = [e.body for e in events(repo) if e.type == EventType.CAP_CONSUMED and e.body["cap"] == "retry"]
        assert len(retry) == 2 and retry[-1]["rung"] == terminal["rung"]
        assert [(r.tier, r.effort) for r in llm.requests if r.surface == "implement"] == [
            ("high", "medium"), ("high", "high")]
        assert "agent_tier: high" not in (repo / f"tickets/{STEM}/ticket.md").read_text()
    assert draws(events(repo), STEM, "diagnosis") == 1
