"""The `drain` scaffold verb (TEMPO_PLAN.md sections 9, 11.2, 18, 19.P1) against a temp checkout.

The stage-dispatch seam is scripted per stem; everything else -- lockfile, reconcile, intake, the tickets-dir
re-scan, the journal cap fold, the ceilings -- is the production path.
"""

from tests.test_stages import adopt_conduct

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tempo.__main__ import build_control, main
from dataclasses import replace
from tempo.box import Box
from tempo.caps import draws
from tempo.drain import CEILING, HALT_SIGNAL, PREMISE
from tempo.git import Git
from tempo.journal import EventType, Journal
from tempo.lockfile import Lockfile, LockHeld
from tempo.seams import LocalFileSystem, SubprocessExec
from tempo.tickets import intake
from tests.test_cli import CONFIG, ENV, git_out, ticket, write, with_retro_driver
from tests.test_terminal import repo as rework_repo
from tests.test_merge import repo as admission_repo


def test_drain_keeps_inline_admission(admission_repo, monkeypatch):
    from tests.test_merge import test_bootstrap_pipeline_keeps_inline_admission
    test_bootstrap_pipeline_keeps_inline_admission(admission_repo, monkeypatch, "drain")

START = datetime(2026, 1, 1, tzinfo=UTC)


class Clock:
    """A fake clock a test (or a scripted stage) can advance."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


class Script:
    """Scripted stage seam: each stem's terminals in order (default `merged`), plus per-stem side effects."""

    def __init__(self, outcomes: dict[str, list[str]] | None = None, hooks: dict | None = None) -> None:
        self.outcomes = {stem: list(seq) for stem, seq in (outcomes or {}).items()}
        self.hooks = hooks or {}
        self.calls: list[str] = []
        self.lock_held: list[bool] = []

    def __call__(self, checkout):
        async def dispatch(t):
            self.calls.append(t.stem)
            contender = Lockfile(checkout.config.state_dir, instance_id="contender", clock=checkout.clock)
            try:
                contender.acquire()
            except LockHeld:
                self.lock_held.append(True)
            else:
                contender.release()
                self.lock_held.append(False)
            if hook := self.hooks.get(t.stem):
                hook(checkout)
            seq = self.outcomes.get(t.stem, [])
            terminal = seq.pop(0) if seq else "merged"
            checkout.journal.append(EventType.STATE_TRANSITION, {"to": terminal, "stage": "review"}
                                    if terminal != "merged" else {"to": terminal}, ticket=t.stem)
            return terminal

        return with_retro_driver(checkout, dispatch)


def make_root(tmp_path: Path, caps: str = "") -> Path:
    root = tmp_path / "repo"
    (root / "tempo").mkdir(parents=True)
    (root / "tempo" / "thing.py").write_text("")
    (root / "config.yaml").write_text(CONFIG + caps)
    (root / ".gitignore").write_text(".tempo/\n")
    adopt_conduct(root)
    git_out(root, "init", "-b", "main")
    git_out(root, "add", ".")
    git_out(root, "commit", "-m", "base")
    return root


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return make_root(tmp_path)


def drain(root: Path, script: Script, clock: Clock | None = None) -> int:
    return main(["drain"], cwd=root, env=ENV, clock=clock or Clock(), pipeline=script)


def journal(root: Path) -> Journal:
    return Journal(root / ".tempo" / "state", Clock())


def confirmed(depends: str = "none", priority: str = "P2") -> str:
    """A ticket as the ticket-plane lane commits it: frontmatter already stamped, no intake needed."""
    return ticket(depends).replace("---\npriority: P2", f"---\nstate: confirmed\nsource: human\npriority: {priority}",
                                   1)


def commit_ticket(root: Path, stem: str, text: str) -> None:
    """A ticket-plane commit straight to main (a seeding ticket's output): no intake, no authoring event."""
    write(root, stem, text)
    git_out(root, "add", f"tickets/{stem}/ticket.md")
    git_out(root, "commit", "-m", f"tempo({stem}): ticket", "--only", "--", f"tickets/{stem}/ticket.md")


def intake_at(root: Path, stem: str, at: datetime) -> None:
    """Hand-author and intake one stem with the journal clock at `at` (its authoring-commit event)."""
    write(root, stem, ticket())
    clock = Clock()
    clock.now = at
    g = Git(SubprocessExec(), env=ENV, timeout=30.0)
    asyncio.run(intake(root, g, Journal(root / ".tempo" / "state", clock), LocalFileSystem()))


def retry_draws(root: Path, stem: str) -> int:
    return draws(journal(root).read(), stem, "retry")


def tos(root: Path, stem: str) -> list[str]:
    return [e.body["to"] for e in journal(root).read() if e.type == EventType.STATE_TRANSITION and e.ticket == stem]


# --- the premise park ----------------------------------------------------------------------------


def test_a_premise_failed_stem_is_never_reoffered_in_its_own_invocation(root, capsys):
    commit_ticket(root, "false-premise", confirmed())
    script = Script({"false-premise": [PREMISE]})

    assert drain(root, script) == 0
    assert script.calls == ["false-premise"]  # budget left, yet no re-offer
    assert retry_draws(root, "false-premise") == 0
    out = capsys.readouterr().out
    assert "false-premise: premise_failed at review; parked until its committed ticket.md changes" in out
    assert "editing tickets/false-premise/ticket.md and committing it" in out


def test_a_premise_park_holds_across_invocations_until_a_ticket_edit_lands(root):
    commit_ticket(root, "false-premise", confirmed())
    script = Script({"false-premise": [PREMISE]})
    drain(root, script)

    # The next invocation skips it: the ticket is unchanged.
    assert drain(root, script) == 0
    assert script.calls == ["false-premise"]

    # The operator answers the premise in the working tree; the next drain's intake commits it.
    path = root / "tickets" / "false-premise" / "ticket.md"
    before = git_out(root, "rev-parse", "HEAD:tickets/false-premise/ticket.md")
    path.write_text(path.read_text().replace("`thing()` returns ok.", "`thing()` returns ok, as answered."))
    assert drain(root, script) == 0
    assert git_out(root, "rev-parse", "HEAD:tickets/false-premise/ticket.md") != before
    assert script.calls == ["false-premise", "false-premise"]
    assert retry_draws(root, "false-premise") == 0
    assert tos(root, "false-premise")[-1] == "merged"


# --- ordering -----------------------------------------------------------------------------------


def test_a_depends_edge_runs_parent_first(root, capsys):
    write(root, "aaa-child", ticket(depends="- zzz-parent"))
    write(root, "zzz-parent", ticket())
    script = Script()
    assert drain(root, script) == 0
    assert script.calls == ["zzz-parent", "aaa-child"]
    assert script.lock_held == [True, True]  # the one lock-holding process dispatches every ticket
    assert "aaa-child" in capsys.readouterr().out


def test_a_child_whose_parent_merges_mid_invocation_runs_in_the_same_invocation(root):
    # The child outranks its parent: only the mid-invocation merge can make it eligible.
    commit_ticket(root, "parent", confirmed(priority="P3"))
    commit_ticket(root, "child", confirmed(depends="- parent", priority="P0"))
    commit_ticket(root, "other", confirmed(priority="P1"))
    script = Script()
    assert drain(root, script) == 0
    assert script.calls == ["other", "parent", "child"]


def test_equal_priority_dispatches_older_authoring_event_first_and_no_event_last(root):
    # Stem order alone would give aaa-none, aaa-new, zzz-old: the age term must override it.
    intake_at(root, "zzz-old", START)
    intake_at(root, "aaa-new", START + timedelta(hours=1))
    commit_ticket(root, "aaa-none", confirmed())
    script = Script()
    assert drain(root, script) == 0
    assert script.calls == ["zzz-old", "aaa-new", "aaa-none"]


def test_priority_outranks_age(root):
    intake_at(root, "old-p3", START)
    commit_ticket(root, "new-p0", confirmed(priority="P0"))
    script = Script()
    assert drain(root, script) == 0
    assert script.calls == ["new-p0", "old-p3"]


# --- park, re-offer, and the journal-derived retry budget -------------------------------------------


def test_a_red_independent_ticket_parks_and_the_next_still_runs(tmp_path, capsys):
    root = make_root(tmp_path, "caps: {retry: 1}\n")
    write(root, "aaa-red", ticket())
    write(root, "bbb-green", ticket())

    def findings(checkout):
        (checkout.repo / "tickets" / "aaa-red" / "review.md").write_text("reject: thing() is wrong\n")

    script = Script({"aaa-red": ["gate_failed", "gate_failed"]}, {"aaa-red": findings})
    assert drain(root, script) == 0  # quiescence with a parked red still exits zero
    # Eligible work runs ahead of the re-offer; the one retry unit is then spent.
    assert script.calls == ["aaa-red", "bbb-green", "aaa-red"]
    out = capsys.readouterr().out
    parked = out.split("parked:\n", 1)[1].split("\n\n", 1)[0]
    assert "aaa-red: gate_failed at review; retry cap spent (1/1)" in parked
    assert "tickets/aaa-red/review.md" in parked  # the red is reported with where its findings live
    assert "bbb-green" not in parked


def test_red_then_green_on_its_reoffer_merges_and_unblocks_its_dependent_in_one_invocation(root):
    write(root, "flaky", ticket())
    write(root, "dependent", ticket(depends="- flaky"))
    write(root, "bystander", ticket())
    script = Script({"flaky": ["gate_failed", "merged"]})
    assert drain(root, script) == 0
    assert script.calls == ["bystander", "flaky", "flaky", "dependent"]
    assert retry_draws(root, "flaky") == 1
    assert tos(root, "flaky")[-1] == "merged" and tos(root, "dependent")[-1] == "merged"
    draw = next(e for e in journal(root).read() if e.type == EventType.CAP_CONSUMED)
    blob = git_out(root, "rev-parse", "HEAD:tickets/flaky/ticket.md").strip()
    assert draw.ticket == "flaky" and draw.body == {"cap": "retry", "ticket_sha": blob}
    # The draw precedes the re-offer's run.
    events = [(e.type, e.body.get("to")) for e in journal(root).read() if e.ticket == "flaky"]
    assert events.index((EventType.CAP_CONSUMED, None)) == events.index((EventType.STATE_TRANSITION, "running"), 2) - 1


def test_a_spent_retry_cap_stays_parked_and_is_never_reoffered(root, capsys):
    commit_ticket(root, "spent", confirmed())
    j = journal(root)
    j.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket="spent")
    j.append(EventType.STATE_TRANSITION, {"to": "gate_failed", "stage": "check"}, ticket="spent")
    for _ in range(6):  # the shipped default retry cap
        j.append(EventType.CAP_CONSUMED, {"cap": "retry", "ticket_sha": "x"}, ticket="spent")
    script = Script()
    assert drain(root, script) == 0
    assert script.calls == []
    assert "spent: gate_failed at check; retry cap spent (6/6)" in capsys.readouterr().out


def test_a_retry_unit_drawn_in_one_invocation_stays_spent_in_the_next(tmp_path, capsys):
    root = make_root(tmp_path, "caps: {retry: 2}\n")
    write(root, "red", ticket())
    script = Script({"red": ["gate_failed"] * 3})
    assert drain(root, script) == 0
    assert script.calls == ["red"] * 3  # the first attempt plus both retry units
    assert retry_draws(root, "red") == 2
    capsys.readouterr()

    again = Script()
    assert drain(root, again) == 0
    assert again.calls == []  # budget re-derived from the journal: nothing carried in memory refills it
    assert "retry cap spent (2/2)" in capsys.readouterr().out


def test_the_budget_fold_counts_only_retry_named_draws(tmp_path):
    root = make_root(tmp_path, "caps: {retry: 3}\n")
    commit_ticket(root, "red", confirmed())
    j = journal(root)
    j.append(EventType.STATE_TRANSITION, {"to": "gate_failed", "stage": "review"}, ticket="red")
    for _ in range(2):
        j.append(EventType.CAP_CONSUMED, {"cap": "retry", "ticket_sha": "old"}, ticket="red")
    for _ in range(5):  # other caps in the vocabulary never perturb the retry budget
        j.append(EventType.CAP_CONSUMED, {"cap": "diagnosis", "ticket_sha": "old"}, ticket="red")
    j.append(EventType.CAP_CONSUMED, {"cap": "retry", "ticket_sha": "old"}, ticket="someone-else")
    script = Script({"red": ["gate_failed"]})
    assert drain(root, script) == 0
    assert script.calls == ["red"]  # exactly the one unit 3 - 2 leaves
    assert retry_draws(root, "red") == 3


# --- true quiescence: the tickets-dir re-scan ----------------------------------------------------------


def test_a_ticket_committed_during_the_invocation_runs_in_the_same_invocation(root):
    write(root, "seeder", ticket())

    def seed(checkout):
        commit_ticket(checkout.repo, "seeded", confirmed())

    script = Script(hooks={"seeder": seed})
    assert drain(root, script) == 0
    assert script.calls == ["seeder", "seeded"]


def test_drain_merges_without_scanning_the_box(root, monkeypatch):
    box = Box(root / ".tempo" / "state" / "box", LocalFileSystem())
    id, _ = box.enqueue(message_class="suggestion", origin="test", summary="pending observation")
    write(root, "work", ticket())
    requests = []
    monkeypatch.setattr("tempo.triage.triage_pass", lambda *args: requests.append(args))
    from tempo.daemon import storm_producer
    def arrival(checkout):
        producer = storm_producer(root=box.root, fs=box.fs, journal=checkout.journal, clock=checkout.clock)
        producer.enqueue(message_class="suggestion", origin="test", summary="pending observation",
                         occurrence_id="drain-observation")
    script = Script(hooks={"work": arrival})
    assert drain(root, script) == 0
    assert script.calls == ["work"]
    assert requests == []
    assert box.get(id).status == "pending"
    assert box.get(id).verdict is box.get(id).resolution is None
    assert any(e.body.get("kind") == "storm_occurrence" for e in journal(root).read())
    assert not any(e.type == EventType.SIGNAL and e.body.get("signal") == "triage_pass"
                   for e in journal(root).read())


def test_an_uncommitted_ticket_file_is_not_eligible(root):
    write(root, "seeder", ticket())

    def author_only(checkout):
        write(checkout.repo, "loose", confirmed())  # authored, never committed to the ticket plane

    script = Script(hooks={"seeder": author_only})
    assert drain(root, script) == 0
    assert script.calls == ["seeder"]


# --- the safety envelope ----------------------------------------------------------------------------


def test_the_runtime_ceiling_stops_the_next_dispatch_and_exits_1(root, capsys):
    write(root, "aaa-first", ticket())
    write(root, "bbb-second", ticket())
    clock = Clock()

    def overrun(checkout):
        clock.now = START + timedelta(hours=13)  # past the shipped 12h, mid-stage

    script = Script(hooks={"aaa-first": overrun})
    assert drain(root, script, clock) == 1
    assert script.calls == ["aaa-first"]
    assert tos(root, "aaa-first") == ["running", "merged"]  # the in-flight stage reached its terminal
    assert tos(root, "bbb-second") == []
    out = capsys.readouterr().out
    assert CEILING in out and "HALTED" in out and "quiescent:" not in out
    assert "uv run python -m tempo drain" in out
    assert "bbb-second" in out.split("eligible, not admitted:\n", 1)[1].split("\n\n", 1)[0]
    halts = [e.body for e in journal(root).read() if e.type == EventType.SIGNAL
             and e.body.get("signal") == HALT_SIGNAL]
    assert halts == [{"signal": HALT_SIGNAL, "ceiling": CEILING, "hours": 12, "merged": ["aaa-first"]}]


def test_a_stuck_budget_over_the_per_ticket_ceiling_parks_at_dispatch(root, capsys):
    write(root, "too-long", ticket().replace("- stuck: 20m", "- stuck: 200m"))
    write(root, "fits", ticket())
    script = Script()
    assert drain(root, script) == 0
    assert script.calls == ["fits"]
    assert tos(root, "too-long") == []
    out = capsys.readouterr().out
    assert "too-long: `## Time budget` stuck 200m exceeds drain.max_ticket_minutes (90m)" in out
    assert "lower `- stuck:`" in out


# --- fail closed and exit codes ---------------------------------------------------------------------


def test_an_empty_queue_reports_and_exits_zero(root, capsys):
    script = Script()
    assert drain(root, script) == 0
    assert script.calls == []
    assert "nothing eligible" in capsys.readouterr().out


def test_a_depends_cycle_is_refused_with_a_paved_road(root, capsys):
    write(root, "aaa", confirmed(depends="- bbb"))
    write(root, "bbb", confirmed(depends="- aaa"))
    git_out(root, "add", "tickets")
    git_out(root, "commit", "-m", "cycle")
    script = Script()
    assert drain(root, script) == 2
    assert script.calls == []
    err = capsys.readouterr().err
    assert "cycle" in err and "drop the edge" in err


def test_drafts_never_run_and_are_listed(root, capsys):
    commit_ticket(root, "rough", confirmed().replace("state: confirmed", "state: draft"))
    script = Script()
    assert drain(root, script) == 0
    assert script.calls == []
    assert "rough" in capsys.readouterr().out.split("drafts awaiting confirm:\n", 1)[1]


def test_lock_contention_stops_the_drain_with_exit_2(root):
    write(root, "add-thing", ticket())
    holder = Lockfile(root / ".tempo" / "state", instance_id="other", clock=Clock())
    holder.acquire()
    try:
        script = Script()
        assert drain(root, script) == 2
    finally:
        holder.release()
    assert script.calls == []


def test_drain_reconciles_on_entry(root):
    write(root, "add-thing", ticket())
    journal(root).append(EventType.STATE_TRANSITION, {"to": "running"}, ticket="dead-run")
    assert drain(root, Script()) == 0
    assert tos(root, "dead-run") == ["running", "abandoned"]




def mechanical_first(monkeypatch, stem, *, workspace=True):
    from tempo import runner, stages
    from tempo.artifacts import Finding, StageResult
    original = runner.run_stages
    called = []
    async def first(ctx, ticket):
        if ticket.stem == stem and not called:
            called.append(ticket.stem)
            if workspace:
                await stages.prepare_worktree(ctx, ticket.stem)
            return stages.StagesRun(attempt=0, results={"implement": StageResult(
                outcome="premise_failed", artifact=None, findings=[Finding(
                    code="render_over_bound", message="too large", paved_road="shrink or split and rerun drain")])})
        return await original(ctx, ticket)
    monkeypatch.setattr(runner, "run_stages", first)
    return called


@pytest.mark.parametrize("mechanical", [False, True])
def test_rework_update_reenters_on_fresh_content(rework_repo, monkeypatch, mechanical):
    import json
    from tempo import runner
    from tempo.caps import consume
    from tempo.llm import FakeLLM
    from tests.test_rework import order, requisition
    from tests.test_stages import ENV, SNAG, STEM, agent, verdict
    from tests.test_terminal import author, clock
    repo = rework_repo
    author(repo)
    consume(Journal(repo / ".tempo/state", clock), STEM, "infra", "prior")
    fresh = []
    def update(req):
        text = (repo / f"tickets/{STEM}/ticket.md").read_text().strip()
        revised = text.replace("holds the word ok", "holds ok after reviewed narrowing")
        fresh.append(revised)
        return order("update", [(STEM, revised)])
    if mechanical:
        mechanical_first(monkeypatch, STEM)
        script = []
    else:
        script = [agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG]),
                  json.dumps({"verdict": "split", "lessons": ["Narrow the work."]})]
    llm = FakeLLM([*script, update, requisition(), agent({"tempo/thing.py": "ok\n"}), verdict()])
    assert main(["drain"], cwd=repo, env=ENV, clock=clock,
                pipeline=lambda c: runner.bind(c, llm)) == 0
    events = Journal(repo / ".tempo/state", clock).read()
    history = [e for e in events if e.ticket == STEM]
    terminals = [e for e in history if e.type == EventType.STATE_TRANSITION and e.body.get("to") != "running"]
    producing = terminals[0].body
    assert producing == ({"to": "premise_failed", "stage": "implement", "reason": "render_over_bound"}
                         if mechanical else {"to": "gate_failed", "stage": "review", "reason": "logic", "dispatch": "retry"})
    rendered = [r for r in llm.requests if r.surface == "implement"][-1].rendered
    assert "holds ok after reviewed narrowing" in rendered and "holds the word ok" not in rendered.split("## Prior attempts", 1)[0]
    assert git_out(repo, "show", f"HEAD:tickets/{STEM}/ticket.md") == fresh[0]
    assert draws(events, STEM, "retry") == (0 if mechanical else 1)
    assert draws(events, STEM, "infra") == 1
    assert draws(events, STEM, "diagnosis") == (0 if mechanical else 1)
    if not mechanical:
        draw = next(e for e in history if e.type == EventType.CAP_CONSUMED and e.body["cap"] == "retry")
        assert draw.body == {"cap": "retry", "ticket_sha": git_out(repo, "rev-parse", f"HEAD:tickets/{STEM}/ticket.md").strip()}
        runs = [e for e in history if e.body.get("to") == "running"]
        assert history.index(terminals[0]) < history.index(draw) < history.index(runs[1])


def test_rework_split_successors_release_original_dependents(root):
    commit_ticket(root, "original", confirmed())
    commit_ticket(root, "dependent", confirmed(depends="- original", priority="P0"))
    def split(checkout):
        for stem, deps in [("piece-one", "none"), ("piece-two", "none"),
                           ("leaf-a", "none"), ("leaf-b", "none")]:
            commit_ticket(root, stem, confirmed(deps))
        for stem, successors in [("original", ["piece-one", "piece-two"]), ("piece-one", ["leaf-a", "leaf-b"])]:
            checkout.journal.append(EventType.SIGNAL, {"signal": "supersedes", "successors": successors}, ticket=stem)
        checkout.journal.append(EventType.STATE_TRANSITION, {"to": "rejected"}, ticket="piece-one")
    script = Script({"original": ["rejected"], "leaf-a": ["already_satisfied"],
                     "leaf-b": ["gate_failed", "already_satisfied"]}, {"original": split})
    assert drain(root, script) == 0
    assert script.calls == ["original", "leaf-a", "leaf-b", "piece-two", "leaf-b", "dependent"]
    assert retry_draws(root, "original") == 0 and retry_draws(root, "leaf-b") == 1
    assert "piece-one" not in script.calls and script.calls.count("original") == 1


@pytest.mark.parametrize("mechanical", [False, True])
def test_rework_split_preserves_dispatch_terminal_invariant(rework_repo, monkeypatch, mechanical):
    import json
    from tempo import runner
    from tempo.llm import FakeLLM
    from tests.test_rework import order, requisition, successors
    from tests.test_stages import ENV, SNAG, STEM, agent, verdict
    from tests.test_terminal import author, clock, diagnosis_reply
    repo = rework_repo
    (repo / "tempo/thing.py").write_text("ok base\n")
    git_out(repo, "add", "tempo/thing.py")
    git_out(repo, "commit", "-m", "green base")
    author(repo)
    author(repo, stem="dependent", depends=f"- {STEM}")
    def split(req):
        return order("split", successors((repo / f"tickets/{STEM}/ticket.md").read_text().strip()))
    if mechanical:
        mechanical_first(monkeypatch, STEM)
        first = []
    else:
        first = [agent({"tempo/thing.py": "ok\n"}), verdict("snag", [SNAG]),
                 json.dumps({"verdict": "split", "lessons": ["Split the work."]})]
    llm = FakeLLM([*first, split, requisition(), requisition(),
                  agent({}, outcome="already_satisfied"), diagnosis_reply(), agent({}, outcome="already_satisfied"), diagnosis_reply(), agent({}, outcome="already_satisfied"), diagnosis_reply()])
    returns, held = [], []
    def bind(checkout):
        writer = runner.bind(checkout, llm)
        async def dispatch(ticket):
            value = await writer(ticket)
            returns.append((ticket.stem, value))
            contender = Lockfile(checkout.config.state_dir, instance_id="contender", clock=clock)
            with pytest.raises(LockHeld):
                contender.acquire()
            held.append(ticket.stem)
            assert not writer.ctx.worktree(ticket.stem).exists()
            return value
        return dispatch
    assert main(["drain"], cwd=repo, env=ENV, clock=clock, pipeline=bind) == 0
    assert returns == [(STEM, "rejected"), ("piece-one", "already_satisfied"),
                       ("piece-two", "already_satisfied"), ("dependent", "already_satisfied")]
    events = Journal(repo / ".tempo/state", clock).read()
    history = [e for e in events if e.ticket == STEM]
    producing = next(e for e in history if e.body.get("to") == ("premise_failed" if mechanical else "gate_failed"))
    assert producing.body == ({"to": "premise_failed", "stage": "implement", "reason": "render_over_bound"}
                              if mechanical else {"to": "gate_failed", "stage": "review", "reason": "logic"})
    mapping = next(e for e in history if e.body.get("signal") == "supersedes")
    retirement = next(e for e in history if e.body == {"to": "rejected"})
    assert history.index(mapping) < history.index(producing) < history.index(retirement)
    assert history[-1] == retirement and draws(events, STEM, "retry") == 0
    assert held == [s for s, _ in returns]
    # The unchanged seam still rejects a producing return after a later retirement.
    from tempo.drain import _Drain
    from tempo.config import load_config
    from tempo.tickets import validate_ticket
    checkout = runner.Checkout(repo, load_config(None, cwd=repo), ENV, SubprocessExec(),
                               Git(SubprocessExec(), env=ENV, timeout=30), Journal(repo / ".tempo/state", clock),
                               LocalFileSystem(), clock)
    candidate = validate_ticket("dependent", (repo / "tickets/dependent/ticket.md").read_text(), repo)
    async def mismatch(ticket):
        checkout.journal.append(EventType.STATE_TRANSITION, {"to": "rejected"}, ticket=ticket.stem)
        return "gate_failed"
    with pytest.raises(ValueError, match="but journaled 'rejected'"):
        asyncio.run(_Drain(checkout, mismatch)._run_one(candidate, False, "blob"))


@pytest.mark.parametrize("action", ["split", "update", "escalate", "exhausted", "refusal", "publication", "missing"])
def test_render_over_bound_dispatches_rework_without_diagnosis(rework_repo, monkeypatch, action):
    import json
    from tempo import daemon, runner
    from tempo.llm import FakeLLM
    from tempo.status import reject_queue
    from tests.test_rework import order, requisition, successors
    from tests.test_stages import ENV, STEM, agent, verdict
    from tests.test_terminal import author, clock, diagnosis_reply
    repo = rework_repo
    author(repo)
    if action == "exhausted":
        path = repo / f"tickets/{STEM}/ticket.md"
        path.write_text(path.read_text().replace("kind: feature", "kind: feature\nagent_tier: max\nagent_effort: max"))
    mechanical_first(monkeypatch, STEM, workspace=action != "missing")
    calls = []
    apply = daemon.apply_rework
    async def observed(*args, **kwargs):
        calls.append(kwargs)
        return await apply(*args, **kwargs)
    monkeypatch.setattr(daemon, "apply_rework", observed)
    def reply(req):
        text = (repo / f"tickets/{STEM}/ticket.md").read_text().strip()
        if action in {"split", "publication"}:
            return order("split", successors(text))
        if action == "update":
            return order("update", [(STEM, text.replace("holds the word ok", "holds ok after shrinking"))])
        return order("escalate") if action != "refusal" else order("update", [(STEM, "invalid")])
    if action == "publication":
        commit = Git.commit
        async def failed(self, root, message, **kwargs):
            if message.endswith(": rework"):
                raise RuntimeError("publication failed")
            return await commit(self, root, message, **kwargs)
        monkeypatch.setattr(Git, "commit", failed)
    script = [] if action == "missing" else [reply]
    if action in {"split", "publication"}:
        script += [requisition(), requisition()]
    if action == "update":
        script += [requisition(), agent({"tempo/thing.py": "ok\n"}), verdict()]
    if action == "split":
        (repo / "tempo/thing.py").write_text("ok base\n")
        git_out(repo, "add", "tempo/thing.py")
        git_out(repo, "commit", "-m", "green base")
        script += [agent({}, outcome="already_satisfied"), diagnosis_reply(), agent({}, outcome="already_satisfied"), diagnosis_reply()]
    llm = FakeLLM(script)
    returned = []
    def bind(checkout):
        writer = runner.bind(checkout, llm)
        writer.ctx.driver.retry_cap = 0
        async def dispatch(ticket):
            value = await writer(ticket)
            returned.append((ticket.stem, value))
            return value
        return with_retro_driver(checkout, dispatch)
    assert main(["drain"], cwd=repo, env=ENV, clock=clock, pipeline=bind) == 0
    events = Journal(repo / ".tempo/state", clock).read()
    original = [e for e in events if e.ticket == STEM]
    producing = next(e for e in original if e.body.get("to") == "premise_failed")
    assert producing.body == {"to": "premise_failed", "stage": "implement", "reason": "render_over_bound"}
    assert len(calls) == 1 and calls[0]["attempt"] == 0
    assert not any(e.type == EventType.CAP_CONSUMED for e in original)
    assert not any(e.body.get("signal") in {"diagnosis", "reject_arrival"} for e in original)
    assert STEM not in reject_queue(events)
    assert returned[0] == (STEM, "rejected" if action == "split" else "premise_failed")
    if action == "split":
        assert original[-1].body == {"to": "rejected"}
        assert original.index(producing) < len(original) - 1
    elif action == "update":
        assert [s for s, _ in returned] == [STEM, STEM]
        assert [r.surface for r in llm.requests] == ["rework", "requisition_review", "implement", "review"]
    else:
        assert original[-1] == producing and not any(e.body.get("signal") == "supersedes" for e in original)
        assert [s for s, _ in returned] == [STEM]
        if action == "missing":
            assert llm.requests == []
        if action in {"escalate", "exhausted", "refusal", "publication"}:
            harvest = json.loads((repo / f"tickets/{STEM}/attempts/0/harvest.json").read_text())
            assert all(f["paved_road"] for f in harvest["findings"]) and len(harvest["findings"]) >= 2


# --- dormant dispatch pause checkpoint ----------------------------------------------------------


def pause_checkout(root):
    from tempo.config import load_config
    from tempo.runner import Checkout
    process = SubprocessExec()
    checkout = Checkout(root, load_config(None, cwd=root), ENV, process,
                    Git(process, env=ENV, timeout=30), journal(root), LocalFileSystem(), Clock())
    return replace(checkout, control=build_control(checkout))


class Pause:
    def __init__(self, at=2):
        self.at = at
        self.calls = 0
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def __call__(self):
        self.calls += 1
        if self.calls == self.at:
            self.entered.set()
            await self.release.wait()


async def paused_drain(root, script, checkpoint):
    from tempo.drain import drain as run_drain
    checkout = pause_checkout(root)
    return await run_drain(checkout, script(checkout), before_dispatch=checkpoint)


def prior_failure(root, *, terminal="gate_failed", extra=None):
    blob = git_out(root, "rev-parse", "HEAD:tickets/work/ticket.md").strip()
    j = journal(root)
    j.append(EventType.STATE_TRANSITION, {"to": "running", "ticket_sha": blob}, ticket="work")
    j.append(EventType.STATE_TRANSITION, {"to": terminal, **(extra or {})}, ticket="work")
    return blob


def offer_events(root):
    return [e for e in journal(root).read() if e.type in {EventType.CAP_CONSUMED, EventType.STATE_TRANSITION}
            or e.body.get("signal") == "reject_verdict"]


@pytest.mark.asyncio
async def test_pause_precedes_fresh_offer_accounting(root):
    commit_ticket(root, "work", confirmed())
    pause, script = Pause(), Script()
    task = asyncio.create_task(paused_drain(root, script, pause))
    await pause.entered.wait()
    assert offer_events(root) == [] and script.calls == []
    pause.release.set()
    assert (await task).merged == ["work"]
    blob = git_out(root, "rev-parse", "HEAD:tickets/work/ticket.md").strip()
    events = offer_events(root)
    assert [e.body for e in events] == [{"to": "running", "ticket_sha": blob}, {"to": "merged"}]
    assert all(e.ticket == "work" and e.key is None for e in events)
    assert script.lock_held == [True] and retry_draws(root, "work") == 0


@pytest.mark.asyncio
async def test_pause_precedes_retry_cap_draw(root):
    commit_ticket(root, "work", confirmed())
    rung = {"tier": "high", "effort": "max"}
    blob = prior_failure(root, extra={"rung": rung})
    before = offer_events(root)
    pause, script = Pause(), Script()
    task = asyncio.create_task(paused_drain(root, script, pause))
    await pause.entered.wait()
    assert offer_events(root) == before and script.calls == []
    pause.release.set()
    assert (await task).merged == ["work"]
    draw, running, terminal = offer_events(root)[len(before):]
    assert draw.type == EventType.CAP_CONSUMED
    assert draw.ticket == "work" and draw.key is None
    assert draw.body == {"cap": "retry", "ticket_sha": blob, "rung": rung}
    assert running.body == {"to": "running", "ticket_sha": blob}
    assert running.ticket == "work" and running.key is None and terminal.body == {"to": "merged"}
    assert retry_draws(root, "work") == 1


@pytest.mark.asyncio
async def test_pause_precedes_machine_keep(root):
    commit_ticket(root, "work", confirmed())
    blob = prior_failure(root)
    journal(root).append(EventType.SIGNAL, {"signal": "reject_arrival"}, ticket="work")
    pause, script = Pause(), Script()
    before = offer_events(root)
    task = asyncio.create_task(paused_drain(root, script, pause))
    await pause.entered.wait()
    assert offer_events(root) == before and script.calls == []
    pause.release.set()
    assert (await task).merged == ["work"]
    keep, draw, running, terminal = offer_events(root)[len(before):]
    assert keep.type == EventType.SIGNAL and keep.ticket == "work" and keep.key is None
    assert keep.body == {"signal": "reject_verdict", "verdict": "keep", "actor": "machine"}
    assert draw.body == {"cap": "retry", "ticket_sha": blob}
    assert running.body == {"to": "running", "ticket_sha": blob} and terminal.body == {"to": "merged"}
    assert retry_draws(root, "work") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["settled", "budget", "depends", "priority", "content"])
async def test_pause_release_rechecks_eligibility_and_budget(root, change):
    commit_ticket(root, "work", confirmed(priority="P1"))
    if change == "budget":
        prior_failure(root)
    if change == "priority":
        commit_ticket(root, "other", confirmed(priority="P2"))
    pause, script = Pause(), Script()
    task = asyncio.create_task(paused_drain(root, script, pause))
    await pause.entered.wait()
    assert script.calls == []
    if change == "settled":
        journal(root).append(EventType.STATE_TRANSITION, {"to": "already_satisfied"}, ticket="work")
    elif change == "budget":
        for _ in range(6):
            journal(root).append(EventType.CAP_CONSUMED, {"cap": "retry", "ticket_sha": "prior"}, ticket="work")
    elif change == "depends":
        commit_ticket(root, "work", confirmed(depends="- missing"))
    elif change == "priority":
        commit_ticket(root, "other", confirmed(priority="P0"))
    else:
        commit_ticket(root, "work", confirmed().replace("`thing()` returns ok.", "`thing()` returns revised ok."))
    pause.release.set()
    await task
    assert script.calls == (["other", "work"] if change == "priority" else ["work"] if change == "content" else [])
    if change in {"settled", "budget", "depends"}:
        assert not any(e.body.get("to") == "running" for e in offer_events(root)[2 if change == "budget" else 0:])
    if change == "content":
        [running] = [e for e in offer_events(root) if e.body.get("to") == "running"]
        assert running.body["ticket_sha"] == git_out(root, "rev-parse", "HEAD:tickets/work/ticket.md").strip()
    if change == "budget":
        assert retry_draws(root, "work") == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_pause_checkpoint_failure_spends_no_retry(root, cancel):
    commit_ticket(root, "work", confirmed())
    prior_failure(root)
    before = journal(root).read()
    pause, script = Pause(), Script()
    async def checkpoint():
        await pause()
        if pause.calls == pause.at:
            raise ValueError("checkpoint failed")
    task = asyncio.create_task(paused_drain(root, script, checkpoint))
    await pause.entered.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        pause.release.set()
        with pytest.raises(ValueError, match="checkpoint failed"):
            await task
    assert script.calls == [] and journal(root).read() == before and retry_draws(root, "work") == 0
    lock = Lockfile(root / ".tempo/state", instance_id="after-cleanup", clock=Clock())
    lock.acquire()
    lock.release()
    assert (await paused_drain(root, script, None)).merged == ["work"]
    assert retry_draws(root, "work") == 1


@pytest.mark.asyncio
async def test_pause_preserves_free_premise_reoffers(root):
    commit_ticket(root, "work", confirmed())
    prior_failure(root, terminal="premise_failed")
    commit_ticket(root, "work", confirmed().replace("`thing()` returns ok.", "`thing()` returns revised ok."))
    before = offer_events(root)
    pause, script = Pause(), Script()
    task = asyncio.create_task(paused_drain(root, script, pause))
    await pause.entered.wait()
    assert offer_events(root) == before and script.calls == []
    pause.release.set()
    assert (await task).merged == ["work"]
    assert retry_draws(root, "work") == 0
    running, terminal = offer_events(root)[len(before):]
    assert running.body == {"to": "running", "ticket_sha": git_out(root, "rev-parse", "HEAD:tickets/work/ticket.md").strip()}
    assert terminal.body == {"to": "merged"}


@pytest.mark.asyncio
async def test_drain_uses_supplied_control(root, monkeypatch):
    from tempo import __main__ as cli
    from tempo.drain import drain as run_drain
    from tempo.runner import Refusal
    checkout = pause_checkout(root)
    consumer = checkout.control
    trace = []
    def forbidden(*args, **kwargs):
        pytest.fail("drain constructed a fallback consumer")
    monkeypatch.setattr(cli, "build_control", forbidden)
    acquire, release = Lockfile.acquire, Lockfile.release
    def acquired(lock):
        acquire(lock)
        trace.append("lock")
    def released(lock):
        assert trace[-1] == "retire"
        trace.append("unlock")
        release(lock)
    monkeypatch.setattr(Lockfile, "acquire", acquired)
    monkeypatch.setattr(Lockfile, "release", released)
    for name in ("publish", "checkpoint", "retire"):
        original = getattr(consumer, name)
        if name == "checkpoint":
            async def checked(_original=original):
                assert trace[0] == "lock" and "unlock" not in trace
                trace.append("consume")
                await _original()
            monkeypatch.setattr(consumer, name, checked)
        else:
            def called(_original=original, _name=name):
                assert trace[0] == "lock" and "unlock" not in trace
                _original()
                trace.append(_name)
            monkeypatch.setattr(consumer, name, called)
    assert (await run_drain(checkout, Script()(checkout))).merged == []
    assert trace[:2] == ["lock", "publish"] and "consume" in trace
    assert trace[-2:] == ["retire", "unlock"]
    assert (checkout.config.state_dir / "control/active.json").read_bytes() == b"null\n"
    trace.clear()
    with pytest.raises(Refusal, match="build_control.*Checkout.control"):
        await run_drain(replace(checkout, control=None), Script()(checkout))
    assert trace == []


def test_retro_hook_is_default_off(root):
    from tempo.drain import _Drain, drain as run_drain
    commit_ticket(root, "work", confirmed())
    checkout = pause_checkout(root)
    script = Script()
    dispatch = script(checkout)
    assert _Drain(checkout, dispatch).retro is None
    assert asyncio.run(run_drain(checkout, dispatch)).merged == ["work"]
    assert not list((root / "tickets/retro").glob("*.md"))
    assert not any((e.key or "").startswith("llm/retro/") for e in checkout.journal.read())


@pytest.mark.asyncio
async def test_selected_retro_remains_killable_through_existing_control(root):
    from tempo import runner
    from tempo.control import ControlRequest, publish_request
    from tempo.drain import drain as run_drain
    from tempo.llm import FakeLLM, HANG
    from tempo.retro import Retro
    from tests.test_daemon_admission import turn
    commit_ticket(root, "work", confirmed())
    ticks = asyncio.Queue()
    async def sleep(seconds):
        if seconds == .1:
            await ticks.get()
        else:
            await asyncio.Event().wait()
    checkout = replace(pause_checkout(root), sleep=sleep, control=None)
    checkout = replace(checkout, control=build_control(checkout))
    llm = FakeLLM([HANG])
    writer = runner.bind(checkout, llm)
    async def dispatch(ticket):
        checkout.journal.append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket=ticket.stem)
        return "merged"
    dispatch.stages = writer.stages
    dispatch.abort_current = writer.abort_current
    dispatch._retro = Retro(checkout, dispatch)
    async with asyncio.timeout(5):
        task = asyncio.create_task(run_drain(checkout, dispatch))
        while not llm.requests:
            await turn()
        assert llm.requests[0].surface == "retro"
        publish_request(checkout.config.state_dir,
            ControlRequest("stop-retro", checkout.control.inbox.lifecycle_id, "kill", None), checkout.fs)
        ticks.put_nowait(None)
        report = await task
    assert report.killed and report.exit_code == 1 and llm.aborted == 1
    assert not list((root / "tickets/retro").glob("*.md"))
    assert not any(e.body.get("kind") == "retro_failed" for e in checkout.journal.read())


@pytest.mark.parametrize("scenario", ["quiescence", "no-merge", "failure", "due"])
def test_drain_retro_due_quiescence_and_pre_exit(tmp_path, monkeypatch, scenario):
    from tempo import __main__ as cli, runner
    from tempo.retro import Retro
    from tests.test_providers import claude_ok

    root = make_root(tmp_path)
    config = (root / "config.yaml").read_text().replace("review: {}", "  - {tier: medium, surface: retro, candidates: [{provider: claude}]}\nreview: {}")
    (root / "config.yaml").write_text(config)
    git_out(root, "add", "config.yaml")
    git_out(root, "commit", "-m", "retro route")
    commit_ticket(root, "feature", confirmed(priority="P0"))
    clock = Clock()
    if scenario == "due":
        for n in range(25):
            journal(root).append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket=f"prior-{n}")
    invocations, dispatches, contexts, prompts = [], [], [], []
    original_hook = Retro.__call__
    async def hook(self, *, forced=False):
        invocations.append((forced, list(dispatches)))
        contender = Lockfile(self.checkout.config.state_dir, instance_id="retro-contender", clock=clock)
        with pytest.raises(LockHeld):
            contender.acquire()
        return await original_hook(self, forced=forced)
    monkeypatch.setattr(Retro, "__call__", hook)

    class Process(SubprocessExec):
        async def run(self, argv, **kwargs):
            if argv[0] == "git":
                return await super().run(argv, **kwargs)
            if argv[1:] == ["--version"]:
                return 0, "1.2.3", ""
            if argv[0] == "pnpm":
                return 0, "1.2.3", ""
            assert argv[0] == "claude"
            prompt = kwargs["stdin_path"].read_text()
            if prompt == "Reply with the single word ok.":
                return 0, claude_ok("ok"), ""
            prompts.append((list(dispatches), prompt))
            assert "current retro window" in prompt
            if scenario == "failure":
                return 1, "", "scripted retro outage"
            out = claude_ok('{"proposals": []}')
            if kwargs.get("on_spawn"):
                kwargs["on_spawn"](123)
            if kwargs.get("on_stdout_line"):
                for line in out.splitlines(keepends=True):
                    kwargs["on_stdout_line"](line)
            return 0, out, ""
    monkeypatch.setattr(cli, "SubprocessExec", Process)

    async def drive(ctx, ticket, **kwargs):
        dispatches.append(ticket.stem)
        contexts.append(ctx)
        if scenario == "no-merge":
            terminal = "already_satisfied"
            body = {"to": terminal}
        else:
            path = f"work/{ticket.stem}.py"
            ctx.fs.write(root / path, b"ok\n")
            await ctx.git.add(root, [path])
            await ctx.git.commit(root, "admitted " + ticket.stem, only=[path])
            terminal = "merged"
            body = {"to": terminal, "commit": await ctx.git.rev_parse(root, "HEAD")}
        ctx.driver.journal.append(EventType.STATE_TRANSITION, body, ticket=ticket.stem)
        return terminal
    monkeypatch.setattr(runner, "drive", drive)
    prepared = runner.prepare_pipeline
    owners = []
    async def prepare(checkout):
        writer = await prepared(checkout)
        assert writer.stages.driver.llm.session is checkout.control.providers
        owners.append(writer.stages.driver)
        return writer
    monkeypatch.setattr(runner, "prepare_pipeline", prepare)
    seen = []
    run_driver = __import__('tempo.driver', fromlist=['Driver']).Driver.run
    async def recorded(self, stage, *args, **kwargs):
        if stage.surface == "retro":
            assert self in owners and kwargs["ticket"] is None
            assert kwargs["workspace"] == root
            seen.append(self)
        return await run_driver(self, stage, *args, **kwargs)
    monkeypatch.setattr("tempo.driver.Driver.run", recorded)
    assert main(["drain"], cwd=root, env=ENV, clock=clock, pipeline=runner.pipeline) == 0
    assert invocations[0] == (False, [])
    if scenario == "no-merge":
        assert seen == prompts == []
    elif scenario == "failure":
        assert dispatches == ["feature"] and len(seen) == 1
        assert [order for order, _ in prompts] == [["feature"]]
        failures = [e for e in journal(root).read() if e.body.get("kind") == "retro_failed"]
        assert len(failures) == len(seen)
    else:
        assert seen and len(seen) == len(prompts)
        if scenario == "due":
            assert prompts[0][0] == [] and prompts[-1][0] == ["feature"]
        else:
            assert prompts[0][0] == ["feature"]
        assert list((root / "tickets/retro").glob("*.md"))


def machine_keeps(root, stem="held"):
    return [e for e in journal(root).read() if e.ticket == stem
            and e.body == {"signal": "reject_verdict", "verdict": "keep", "actor": "machine"}]


def test_a_premise_reoffer_after_ticket_edit_draws_no_retry(root):
    commit_ticket(root, "work", confirmed())
    prior_failure(root, terminal="premise_failed", extra={"routed": "reject_queue"})
    commit_ticket(root, "work", confirmed().replace("returns ok.", "returns revised ok."))
    script = Script()
    assert drain(root, script) == 0 and script.calls == ["work"]
    assert len(machine_keeps(root, "work")) == 1 and retry_draws(root, "work") == 0


