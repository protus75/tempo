"""The `drain` self-upgrade re-exec and the premise park (TEMPO_PLAN.md sections 11.2, 18, 19.P1), temp checkout.

The stage seam is scripted per stem (a merge commits its admitted paths to main for real); the handoff's process
seam is faked to run the child drain in-process, so the whole chain shares one journal and one git checkout.
"""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from tempo import journal as journal_mod
from tempo import lockfile as lockfile_mod
from tempo.__main__ import main
from tempo.drain import HANDOFF_SIGNAL, PREMISE, REEXEC
from tempo.journal import EventType, Journal
from tempo.lockfile import Lockfile, LockHeld
from tempo.seams import SubprocessExec
from tests.test_cli import ENV, git_out, with_retro_driver
from tests.test_drain import Clock, commit_ticket, confirmed, journal, make_root, retry_draws, tos


class Script:
    """Each stem's outcomes in order: a terminal name, or `merged:<path>` -- a real squash commit touching <path>."""

    def __init__(self, outcomes: dict[str, list[str]]) -> None:
        self.outcomes = {stem: list(seq) for stem, seq in outcomes.items()}
        self.calls: list[tuple[str, str]] = []  # (process, stem): which drain in the chain dispatched it
        self.process = "parent"

    def __call__(self, checkout):
        async def dispatch(t):
            self.calls.append((self.process, t.stem))
            seq = self.outcomes.get(t.stem, [])
            outcome = seq.pop(0) if seq else "merged:docs/x.md"
            if not outcome.startswith("merged:"):
                checkout.journal.append(EventType.STATE_TRANSITION, {"to": outcome, "stage": "implement"},
                                        ticket=t.stem)
                return outcome
            path = checkout.repo / outcome.removeprefix("merged:")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(path.read_text() + "x\n" if path.exists() else "x\n")
            git_out(checkout.repo, "add", str(path))
            git_out(checkout.repo, "commit", "-m", f"tempo({t.stem}): merge")
            commit = git_out(checkout.repo, "rev-parse", "HEAD").strip()
            checkout.journal.append(EventType.STATE_TRANSITION, {"to": "merged", "commit": commit}, ticket=t.stem)
            return "merged"

        return with_retro_driver(checkout, dispatch)


class Child:
    """The faked handoff seam: records the spawn, checks the handoff state, runs the child drain in-process."""

    def __init__(self, root: Path, script: Script, order: list[str], clock: Clock) -> None:
        self.root, self.script, self.order, self.clock = root, script, order, clock
        self.spawns: list[dict] = []

    async def run(self, argv, *, cwd, env, timeout, stdin_path=None):
        self.order.append("spawn")
        contender = Lockfile(self.root / ".tempo" / "state", instance_id="probe", clock=self.clock)
        try:
            contender.acquire()
        except LockHeld:
            free = False
        else:
            contender.release()
            free = True
        last = journal(self.root).read()[-1]
        self.spawns.append({"argv": list(argv), "cwd": cwd, "env": env, "timeout": timeout,
                            "stdin_path": stdin_path, "lock_free": free, "last_event": last})
        assert list(argv[:len(REEXEC)]) == list(REEXEC)
        self.script.process = f"child{len(self.spawns)}"
        # `uv run python -m tempo <args>` -> the module entry's own argv; a thread, as asyncio.run cannot nest.
        rc = await asyncio.to_thread(main, list(argv[5:]), cwd=self.root, env=ENV, clock=self.clock,
                                     pipeline=self.script, reexec=self)
        self.order.append(f"exit {rc}")
        return rc, "", ""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return make_root(tmp_path)


@pytest.fixture
def order(monkeypatch) -> list[str]:
    """The handoff's observable steps, in the order they happen across the whole chain."""
    steps: list[str] = []
    release, close, append = lockfile_mod.Lockfile.release, journal_mod.Journal.close, journal_mod.Journal.append

    def logged_release(self):
        steps.append("lock released")
        release(self)

    def logged_close(self):
        steps.append("journal closed")
        close(self)

    def logged_append(self, type, body, **kw):
        event = append(self, type, body, **kw)
        if body.get("signal") == HANDOFF_SIGNAL:
            steps.append("handoff journaled")
        return event

    monkeypatch.setattr(lockfile_mod.Lockfile, "release", logged_release)
    monkeypatch.setattr(journal_mod.Journal, "close", logged_close)
    monkeypatch.setattr(journal_mod.Journal, "append", logged_append)
    return steps


def drain(root: Path, script: Script, order: list[str], clock: Clock | None = None) -> tuple[int, Child]:
    clock = clock or Clock()
    child = Child(root, script, order, clock)
    return main(["drain"], cwd=root, env=ENV, clock=clock, pipeline=script, reexec=child), child


# --- the self-upgrade handoff --------------------------------------------------------------------


def test_a_self_upgrading_admission_hands_off_before_the_next_dispatch(tmp_path, order, capsys):
    root = make_root(tmp_path, caps="caps: {retry: 1}\n")
    commit_ticket(root, "red", confirmed(priority="P0"))
    commit_ticket(root, "upgrade", confirmed(priority="P1"))
    commit_ticket(root, "after", confirmed(priority="P2"))
    script = Script({"red": ["gate_failed", "gate_failed"], "upgrade": ["merged:tempo/thing.py"]})

    code, child = drain(root, script, order)

    assert code == 0
    # The parent stopped at the upgrade: `after` and the re-offer ran in the child, on the upgraded checkout.
    assert script.calls == [("parent", "red"), ("parent", "upgrade"), ("child1", "after"), ("child1", "red")]
    (spawn,) = child.spawns
    assert spawn["argv"] == ["uv", "run", "python", "-m", "tempo", "drain", "--parked", "red"]
    assert spawn["timeout"] is None and spawn["stdin_path"] is None
    assert spawn["cwd"] == root and spawn["env"] == ENV
    assert spawn["lock_free"]
    # The HANDOFF ORDER (section 18), then nothing in the parent after its child exits.
    assert order[:4] == ["handoff journaled", "journal closed", "lock released", "spawn"]
    assert order[-1] == "exit 0"
    handoff = spawn["last_event"]
    assert handoff.type == EventType.SIGNAL and handoff.body["signal"] == HANDOFF_SIGNAL
    assert handoff.ticket == "upgrade" and handoff.body["parked"] == ["red"]
    assert handoff.body["commit"] == next(e.body["commit"] for e in journal(root).read()
                                        if e.ticket == "upgrade" and e.body.get("to") == "merged")
    # The parked set survives: the child reports the parent's red; the parent printed nothing of its own.
    out = capsys.readouterr().out
    assert out.count("drain quiescent") == 1
    assert "red: gate_failed at implement" in out


def test_the_carried_parked_set_rides_every_handoff_in_the_chain(tmp_path, order):
    root = make_root(tmp_path, caps="caps: {retry: 1}\n")
    commit_ticket(root, "red", confirmed(priority="P0"))
    commit_ticket(root, "one", confirmed(priority="P1"))
    commit_ticket(root, "two", confirmed(priority="P2"))
    script = Script({"red": ["gate_failed", "gate_failed"], "one": ["merged:tempo/a.py"],
                     "two": ["merged:specs/b.md"]})

    code, child = drain(root, script, order)

    assert code == 0
    assert [s["argv"][len(REEXEC):] for s in child.spawns] == [["--parked", "red"], ["--parked", "red"]]
    assert script.calls == [("parent", "red"), ("parent", "one"), ("child1", "two"), ("child2", "red")]
    # One awaiting parent per self-upgrading admission; each exits with its child's code.
    assert order.count("spawn") == 2 and order[-2:] == ["exit 0", "exit 0"]


def test_an_admission_outside_the_engine_plane_does_not_re_exec(root, order):
    commit_ticket(root, "docs", confirmed(priority="P0"))
    commit_ticket(root, "lookalike", confirmed(priority="P1"))
    script = Script({"docs": ["merged:docs/guide.md"], "lookalike": ["merged:tempo-notes/x.md"]})

    code, child = drain(root, script, order)

    assert code == 0
    assert child.spawns == [] and HANDOFF_SIGNAL not in str([e.body for e in journal(root).read()])
    assert script.calls == [("parent", "docs"), ("parent", "lookalike")]


def test_retry_budget_spent_before_the_re_exec_stays_spent_in_the_child(tmp_path, order, capsys):
    root = make_root(tmp_path, caps="caps: {retry: 1}\n")
    commit_ticket(root, "red", confirmed(priority="P0"))
    commit_ticket(root, "gate", confirmed(priority="P1"))
    commit_ticket(root, "upgrade", confirmed(depends="- gate", priority="P2"))
    script = Script({"red": ["gate_failed", "gate_failed", "merged:docs/never.md"],
                     "gate": ["gate_failed", "merged:docs/gate.md"],
                     "upgrade": ["merged:tempo/thing.py"]})

    code, child = drain(root, script, order)

    assert code == 0
    # Both reds were re-offered (one unit each) BEFORE the upgrade admission handed off.
    assert script.calls == [("parent", "red"), ("parent", "gate"), ("parent", "red"), ("parent", "gate"),
                            ("parent", "upgrade")]
    assert len(child.spawns) == 1
    # The child re-derives the budget from the journal: `red`'s spent unit is never re-armed by the exec.
    assert retry_draws(root, "red") == 1
    assert ("child1", "red") not in script.calls
    assert "red: gate_failed at implement; retry cap spent (1/1)" in capsys.readouterr().out


# --- the premise park ----------------------------------------------------------------------------


def test_a_premise_failed_stem_is_never_reoffered_in_its_own_invocation(root, order, capsys):
    commit_ticket(root, "false-premise", confirmed())
    script = Script({"false-premise": [PREMISE]})

    code, _ = drain(root, script, order)

    assert code == 0
    assert script.calls == [("parent", "false-premise")]  # budget left, yet no re-offer
    assert retry_draws(root, "false-premise") == 0
    out = capsys.readouterr().out
    assert "false-premise: premise_failed at implement; parked until its committed ticket.md changes" in out
    assert "editing tickets/false-premise/ticket.md and committing it" in out


def test_a_premise_park_holds_across_invocations_until_a_ticket_edit_lands(root, order):
    commit_ticket(root, "false-premise", confirmed())
    script = Script({"false-premise": [PREMISE]})
    drain(root, script, order)

    # The next invocation skips it: the ticket is unchanged.
    assert drain(root, script, order)[0] == 0
    assert script.calls == [("parent", "false-premise")]

    # The operator answers the premise in the working tree; the next drain's intake commits it.
    path = root / "tickets" / "false-premise" / "ticket.md"
    before = git_out(root, "rev-parse", "HEAD:tickets/false-premise/ticket.md")
    path.write_text(path.read_text().replace("`thing()` returns ok.", "`thing()` returns ok, per the plan."))
    assert drain(root, script, order)[0] == 0
    assert git_out(root, "rev-parse", "HEAD:tickets/false-premise/ticket.md") != before
    assert script.calls == [("parent", "false-premise"), ("parent", "false-premise")]
    assert retry_draws(root, "false-premise") == 0
    assert tos(root, "false-premise")[-1] == "merged"


def test_a_seed_stems_premise_road_names_the_plan_first(root, order, capsys):
    plan = root / "TEMPO_PLAN.md"
    plan.write_text(plan.read_text() + "\n## 19. Phases\n\n### 19.L Law\n\nLaw.\n\n### 19.P1 Phase 1\n\nP1.\n")
    git_out(root, "commit", "-m", "plan", "--", "TEMPO_PLAN.md")
    seed = confirmed().replace("source: human", "source: seed", 1).replace(
        "## Context", "## Plan contract\n- 19.L\n- 19.P1\n\n## Context", 1)
    commit_ticket(root, "seeded", seed)
    drain(root, Script({"seeded": [PREMISE]}), order)

    out = capsys.readouterr().out
    assert "seeded: premise_failed" in out
    assert "fix it in the plan and commit that first" in out


# --- the handoff's seam surfaces -----------------------------------------------------------------


def test_the_unbounded_handoff_spawn_streams_through_inherited_stdio(tmp_path, capfd):
    argv = [sys.executable, "-c", "import sys; print('to the operator'); print('err', file=sys.stderr)"]
    rc, out, err = asyncio.run(SubprocessExec().run(argv, cwd=tmp_path, env=dict(os.environ), timeout=None))
    assert (rc, out, err) == (0, "", "")
    streamed = capfd.readouterr()
    assert streamed.out == "to the operator\n" and streamed.err == "err\n"


def test_a_closed_journal_handle_refuses_writes(tmp_path):
    j = Journal(tmp_path, Clock())
    j.append(EventType.SIGNAL, {"signal": HANDOFF_SIGNAL})
    j.close()
    with pytest.raises(RuntimeError, match="closed"):
        j.append(EventType.SIGNAL, {"signal": "drain_handoff"})
    assert len(j.read()) == 1
