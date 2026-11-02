"""Reject arrival, release verbs, and premise accounting through the CLI."""

import asyncio

import pytest

from tempo import runner
from tempo.artifacts import Finding, StageResult
from tempo.__main__ import main
from tempo.caps import CAPS, draws, remaining
from tempo.config import Caps
from tempo.journal import EventType
from tempo.llm import FakeLLM
from tempo.status import reject_queue
from tempo.stages import StagesRun
from tests.test_cli import ENV, git_out
from tests.test_drain import Clock, Script, commit_ticket, confirmed, drain, journal, make_root
from tests.test_terminal import author, clock, diagnosis_reply, implement_reply, repo
from tests.test_stages import STEM


def _events(root, stem):
    return [e for e in journal(root).read() if e.ticket == stem]


def _queued(root, stem, cap="retry"):
    j = journal(root)
    j.append(EventType.STATE_TRANSITION, {"to": "gate_failed", "stage": "check", "routed": "reject_queue"},
             ticket=stem)
    j.append(EventType.CAP_CONSUMED, {"cap": cap, "ticket_sha": "old"}, ticket=stem)


def test_two_premise_terminals_draw_and_route_after_cap(repo, capsys):
    (repo / "config.yaml").write_text((repo / "config.yaml").read_text() + "\ncaps: {premise_bounce: 2}\n")
    author(repo)
    finding = {"code": "premise", "message": "false premise", "paved_road": "edit ticket"}
    for attempt in range(2):
        answers = [implement_reply("premise_failed", [finding])]
        if attempt == 0:
            answers.append(diagnosis_reply())
        assert main(["run", STEM], cwd=repo, env=ENV, clock=clock,
                    pipeline=lambda c, a=answers: runner.bind(c, FakeLLM(a))) == 1
        if attempt == 0:
            path = repo / "tickets" / STEM / "ticket.md"
            path.write_text(path.read_text().replace("`thing.py` holds the word ok.",
                                                     "`thing.py` holds the word ok, after correction."))
            git_out(repo, "add", f"tickets/{STEM}/ticket.md")
            git_out(repo, "commit", "-m", "edit ticket")
    events = _events(repo, STEM)
    terminals = [(i, e) for i, e in enumerate(events) if e.type == EventType.STATE_TRANSITION
                 and e.body.get("to") == "premise_failed"]
    assert len(terminals) == 2
    assert draws(events, STEM, "premise_bounce") == 2
    assert sum(e.type == EventType.CAP_CONSUMED and e.body.get("cap") == "premise_bounce"
               for e in events[:terminals[0][0]]) == 1
    assert terminals[-1][1].body["routed"] == "reject_queue"
    assert main(["status"], cwd=repo, env=ENV, clock=clock) == 0
    assert f"Reject queue:\n- {STEM}: premise_failed" in capsys.readouterr().out


def test_over_bound_premise_draws_nothing(repo, monkeypatch):
    author(repo)

    async def over_bound(ctx, ticket):
        return StagesRun(attempt=0, results={"implement": StageResult(
            outcome="premise_failed", artifact=None, findings=[Finding(
                code="render_over_bound", message="too long", paved_road="edit ticket")])})

    monkeypatch.setattr(runner, "run_stages", over_bound)
    assert main(["run", STEM], cwd=repo, env=ENV, clock=clock,
                pipeline=lambda c: runner.bind(c, FakeLLM([]))) == 1
    assert draws(_events(repo, STEM), STEM, "premise_bounce") == 0
    assert STEM not in reject_queue(journal(repo).read())


def test_keep_bounds_all_caps_but_machine_keep_does_not(tmp_path, capsys):
    root = make_root(tmp_path)
    commit_ticket(root, "held", confirmed())
    j = journal(root)
    j.append(EventType.CAP_CONSUMED, {"cap": "infra"}, ticket="held")
    j.append(EventType.SIGNAL, {"signal": "reject_verdict", "verdict": "keep", "actor": "machine"}, ticket="held")
    assert draws(j.read(), "held", "infra") == 1
    _queued(root, "held")
    assert main(["confirm", "held"], cwd=root, env=ENV, clock=Clock()) == 0
    assert "held" not in reject_queue(j.read())
    for cap in CAPS:
        assert remaining(Caps(), j.read(), "held", cap) == getattr(Caps(), cap)
    assert main(["confirm", "held"], cwd=root, env=ENV, clock=Clock()) == 2
    assert "edit the ticket" in capsys.readouterr().err
    path = root / "tickets/held/ticket.md"
    path.write_text(path.read_text().replace("`thing()` returns ok.", "`thing()` returns ok after editing."))
    _queued(root, "held")
    assert main(["confirm", "held"], cwd=root, env=ENV, clock=Clock()) == 0


def test_drain_skips_queue_and_marks_legacy_once(tmp_path, capsys):
    root = make_root(tmp_path, "caps: {retry: 1}\n")
    commit_ticket(root, "held", confirmed())
    _queued(root, "held")
    script = Script()
    assert drain(root, script) == 0
    assert script.calls == []
    out = capsys.readouterr().out
    assert "retry cap spent (1/1)" in out
    assert "confirm held" in out and "reject held" in out
    commit_ticket(root, "legacy", confirmed())
    j = journal(root)
    j.append(EventType.STATE_TRANSITION, {"to": "gate_failed", "stage": "check"}, ticket="legacy")
    j.append(EventType.CAP_CONSUMED, {"cap": "retry"}, ticket="legacy")
    assert drain(root, script) == 0
    assert drain(root, script) == 0
    assert sum(e.body.get("signal") == "reject_arrival" for e in _events(root, "legacy")) == 1
    assert main(["confirm", "legacy"], cwd=root, env=ENV, clock=Clock()) == 0


def test_reject_stamps_notifies_dependents_and_is_idempotent(tmp_path, capsys):
    root = make_root(tmp_path)
    commit_ticket(root, "parent", confirmed())
    commit_ticket(root, "child", confirmed(depends="- parent"))
    _queued(root, "parent")
    assert main(["reject", "parent"], cwd=root, env=ENV, clock=Clock()) == 0
    assert "state: rejected" in git_out(root, "show", "HEAD:tickets/parent/ticket.md")
    assert git_out(root, "log", "-1", "--format=%s").strip() == "tempo(parent): rejected"
    events = _events(root, "parent")
    assert events[-2].body == {"signal": "reject_verdict", "verdict": "kill", "actor": "operator"}
    assert events[-1].body == {"to": "rejected"}
    assert sum(e.body.get("signal") == "dead_dependency" for e in _events(root, "child")) == 1
    box = list((root / ".tempo/state/box").glob("*.json"))
    assert len(box) == 1 and "failure_report" in box[0].read_text()
    count = len(journal(root).read())
    assert main(["reject", "parent"], cwd=root, env=ENV, clock=Clock()) == 0
    assert "already rejected" in capsys.readouterr().out
    assert len(journal(root).read()) == count and len(list((root / ".tempo/state/box").glob("*.json"))) == 1
    journal(root).append(EventType.STATE_TRANSITION, {"to": "gate_failed"}, ticket="ghost")
    before = git_out(root, "rev-parse", "HEAD")
    assert main(["reject", "ghost"], cwd=root, env=ENV, clock=Clock()) == 0
    assert git_out(root, "rev-parse", "HEAD") == before
    journal(root).append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket="done")
    assert main(["reject", "done"], cwd=root, env=ENV, clock=Clock()) == 2


def test_draft_confirm_commits_and_next_drain_dispatches(tmp_path):
    root = make_root(tmp_path)
    commit_ticket(root, "rough", confirmed().replace("state: confirmed", "state: draft", 1))
    assert main(["confirm", "rough"], cwd=root, env=ENV, clock=Clock()) == 0
    assert "state: confirmed" in git_out(root, "show", "HEAD:tickets/rough/ticket.md")
    assert git_out(root, "log", "-1", "--format=%s").strip() == "tempo(rough): confirmed"
    assert any(e.body.get("signal") == "draft_confirmed" for e in _events(root, "rough"))
    script = Script()
    assert drain(root, script) == 0
    assert script.calls == ["rough"]


def test_dirless_ghost_can_be_kept(tmp_path):
    root = make_root(tmp_path)
    journal(root).append(EventType.STATE_TRANSITION,
                         {"to": "gate_failed", "stage": "check", "routed": "reject_queue"}, ticket="ghost")
    assert main(["confirm", "ghost"], cwd=root, env=ENV, clock=Clock()) == 0
    assert _events(root, "ghost")[-1].body["ticket_sha"] is None


@pytest.mark.parametrize("leaf_state", ["rejected", "abandoned"])
def test_dead_dependencies_resolve_through_supersedes(tmp_path, leaf_state):
    from tempo.config import load_config
    from tempo.git import Git
    from tempo.seams import LocalFileSystem, SubprocessExec
    root = make_root(tmp_path)
    for stem, depends in [("original", "none"), ("one", "none"), ("two", "none"),
                           ("leaf", "none"), ("leaf-other", "none"), ("dependent", "- original")]:
        commit_ticket(root, stem, confirmed(depends))
    j = journal(root)
    for stem, successors in [("original", ["one", "two"]), ("one", ["leaf", "leaf-other"])]:
        j.append(EventType.SIGNAL, {"signal": "supersedes", "successors": successors}, ticket=stem)
    assert main(["reject", "original"], cwd=root, env=ENV, clock=Clock()) == 0
    assert not any(e.body.get("signal") == "dead_dependency" for e in _events(root, "dependent"))
    assert not list((root / ".tempo/state/box").glob("*.json"))
    if leaf_state == "rejected":
        assert main(["reject", "leaf"], cwd=root, env=ENV, clock=Clock()) == 0
    else:
        j.append(EventType.STATE_TRANSITION, {"to": "abandoned"}, ticket="leaf")
        checkout = runner.Checkout(root, load_config(None, cwd=root), ENV, SubprocessExec(),
                                   Git(SubprocessExec(), env=ENV, timeout=30), j, LocalFileSystem(), Clock())
        asyncio.run(runner._dead_dependents("leaf", checkout))
    [signal] = [e for e in _events(root, "dependent") if e.body.get("signal") == "dead_dependency"]
    assert signal.body == {"signal": "dead_dependency", "dead": "original"}
    boxes = list((root / ".tempo/state/box").glob("*.json"))
    assert len(boxes) == 1 and "failure_report" in boxes[0].read_text()
    before = len(j.read())
    assert main(["reject", "original"], cwd=root, env=ENV, clock=Clock()) == 0
    assert len(j.read()) == before and len(list((root / ".tempo/state/box").glob("*.json"))) == 1
