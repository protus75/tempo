import asyncio
from dataclasses import asdict
from pathlib import Path

import pytest

from tempo import journal as journal_module, runner, stages as stage_module
from tempo.audit import audit_journal
from tempo.git import Git
from tempo.journal import EVENT_VERSIONS, SIGNAL_NAMES, TERMINAL_STATES, EventType, Journal, render_ts, run_seq
from tempo.llm import FakeLLM
from tempo.reconcile import RECOVERY_ALERT, orphans, reconcile
from tempo.seams import SubprocessExec
from tempo.status import last_states
from tests.test_cli import ENV, Clock, Stages, cli, git_out, journal, root, ticket, write  # noqa: F401

STATE = Path(".tempo") / "state"


@pytest.mark.parametrize("release", [False, True])
def test_live_hold_is_not_an_orphan(tmp_path, release):
    from tests.test_mergequeue import ctx as context_fixture
    from tests.test_supervised_merge_hold import hold, confirm
    ctx = context_fixture.__wrapped__(tmp_path)
    async def exercise():
        ticket, queue, held = await hold(ctx)
        if release:
            confirm(queue, held)
            await queue.control.inbox.settle_admissions(queue.confirm_draft)
        ctx.driver.journal.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket="ordinary")
        ctx.driver.journal.append(EventType.EFFECT_INTENT, {}, ticket="incomplete", key="merge-regate/incomplete/0/unfinished")
        harvested = []
        async def harvest(stem, attempt): harvested.append((stem, attempt))
        before = ctx.driver.journal.read()
        assert orphans(before) == ["incomplete", "ordinary"]
        assert await reconcile(ctx.driver.journal, ctx.git, ctx.repo, ctx.config.worktree_root, harvest) == ["incomplete", "ordinary"]
        assert harvested == [] and ctx.worktree(ticket.stem).exists()
        assert not any(e.ticket == ticket.stem and e.body.get("to") in TERMINAL_STATES for e in ctx.driver.journal.read())
        settled = ctx.driver.journal.read()
        assert await reconcile(ctx.driver.journal, ctx.git, ctx.repo, ctx.config.worktree_root, harvest) == []
        assert ctx.driver.journal.read() == settled and queue.held[ticket.stem][2] == held.hold_id
    asyncio.run(exercise())


def git() -> Git:
    return Git(SubprocessExec(), env=ENV, timeout=30.0)


def orphan_worktree(root: Path, stem: str) -> Path:
    """What an interrupted run leaves: its branch checked out in a worktree under the worktree root."""
    path = root / STATE / "worktrees" / stem
    path.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(git().worktree_add(root, path, stem, "main"))
    (path / "half-done.py").write_text("")  # uncommitted work the dead run never finished
    return path


def reap(root: Path) -> list[str]:
    async def harvest(stem: str, attempt: int) -> None:
        pass

    return asyncio.run(reconcile(journal(root), git(), root, root / STATE / "worktrees", harvest))


def test_running_with_no_terminal_is_reaped_to_abandoned_on_the_next_run(root):
    write(root, "add-thing", ticket())
    journal(root).append(EventType.STATE_TRANSITION, {"to": "running"}, ticket="dead-run")
    path = orphan_worktree(root, "dead-run")
    stages = Stages()
    assert cli(root, "run", "add-thing", stages=stages) == 0
    events = journal(root).read()
    transitions = [(e.ticket, e.body["to"]) for e in events if e.type == EventType.STATE_TRANSITION]
    # Reaped BEFORE the next run's own dispatch.
    assert transitions.index(("dead-run", "abandoned")) < transitions.index(("add-thing", "running"))
    [alert] = [e for e in events if e.ticket == "dead-run" and e.body.get("kind") == RECOVERY_ALERT]
    assert alert.body == recovery_body(0)
    assert events.index(alert) < next(i for i, e in enumerate(events)
                                     if e.ticket == "add-thing" and e.body.get("to") == "running")
    assert last_states(events)["dead-run"] == "abandoned"
    assert run_seq(events, "dead-run") == 1  # the reap is a terminal: a re-run takes a fresh sequence
    assert not path.exists()
    assert str(path) not in git_out(root, "worktree", "list")


def test_an_open_effect_intent_is_an_orphan(root):
    j = journal(root)
    j.append(EventType.EFFECT_INTENT, {}, ticket="dead-run", key="dead-run/1/implement")
    path = orphan_worktree(root, "dead-run")
    assert reap(root) == ["dead-run"]
    assert last_states(j.read()) == {"dead-run": "abandoned"}
    assert not path.exists()


def test_reconcile_is_idempotent(root):
    j = journal(root)
    j.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket="dead-run")
    j.append(EventType.EFFECT_INTENT, {}, ticket="dead-run", key="dead-run/0/implement")
    assert reap(root) == ["dead-run"]
    assert reap(root) == []
    assert run_seq(j.read(), "dead-run") == 1


def test_a_clean_journal_is_a_no_op(root):
    j = journal(root)
    j.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket="done")
    j.append(EventType.EFFECT_INTENT, {}, ticket="done", key="done/0/implement")
    j.append(EventType.EFFECT_COMPLETION, {"result": "ok"}, ticket="done", key="done/0/implement")
    j.append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket="done")
    # A run that crashed mid-effect but still journaled its terminal is history, not an orphan.
    j.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket="red")
    j.append(EventType.EFFECT_INTENT, {}, ticket="red", key="red/0/implement")
    j.append(EventType.STATE_TRANSITION, {"to": "infra_error", "stage": "implement"}, ticket="red")
    left = orphan_worktree(root, "red")  # a non-ok terminal's worktree stays for the operator
    before = j.read()
    assert reap(root) == []
    assert j.read() == before
    assert left.exists()


def test_an_empty_journal_is_a_no_op(root):
    assert reap(root) == []
    assert journal(root).read() == []


def test_reap_without_a_worktree_still_journals_abandoned(root):
    j = journal(root)
    j.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket="dead-run")
    assert reap(root) == ["dead-run"]
    assert last_states(j.read()) == {"dead-run": "abandoned"}


def test_a_reaped_stem_reruns(root):
    write(root, "add-thing", ticket())
    assert cli(root, "run", "add-thing", stages=Stages("gate_failed")) == 1
    Journal(root / STATE, Clock()).append(EventType.STATE_TRANSITION, {"to": "running"}, ticket="add-thing")
    stages = Stages()
    assert cli(root, "run", "add-thing", stages=stages) == 0
    assert stages.calls == ["add-thing"]
    assert last_states(journal(root).read())["add-thing"] == "merged"


def recovery_body(sequence):
    return {"kind": "recovery_alert", "disposition": "alert", "outcome": "abandoned",
            "reason": "orphaned run", "run_seq": sequence}


def test_recovery_alert_records_producing_run(root):
    assert RECOVERY_ALERT == "recovery_alert" and RECOVERY_ALERT in SIGNAL_NAMES
    j = journal(root)
    for terminal in ("infra_error", "rejected"):
        j.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket="prior")
        j.append(EventType.STATE_TRANSITION, {"to": terminal}, ticket="prior")
    sequences = {"first": 0, "intent": 0, "prior": 2}
    for stem in sequences:
        if stem != "intent":
            j.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket=stem)
        j.append(EventType.EFFECT_INTENT, {}, ticket=stem, key=f"{stem}/unfinished")
        orphan_worktree(root, stem)
    before = j.read()
    harvested = []

    async def harvest(stem, sequence):
        assert sequence == run_seq(j.read(), stem) == sequences[stem]
        harvested.append((stem, sequence))

    assert asyncio.run(reconcile(j, git(), root, root / STATE / "worktrees", harvest)) == sorted(sequences)
    assert harvested == sorted(sequences.items())
    events = Journal(root / STATE, Clock()).read()
    assert events[:len(before)] == before
    for stem, sequence in sequences.items():
        terminal, alert = [e for e in events[len(before):] if e.ticket == stem]
        assert terminal.type == EventType.STATE_TRANSITION and terminal.key is None
        assert terminal.body == {"to": "abandoned"}
        assert asdict(alert) == {
            "v": EVENT_VERSIONS[EventType.SIGNAL], "type": EventType.SIGNAL,
            "ts": render_ts(Clock()()), "ticket": stem, "key": None,
            "body": recovery_body(sequence),
        }
        assert type(alert.body["run_seq"]) is int and alert.body["run_seq"] >= 0
        assert run_seq(events, stem) == sequence + 1
    assert [(v.invariant, v.ticket, v.detail) for v in audit_journal(j)] == [
        ("one_terminal_per_run", "intent", "terminal 'abandoned' has no open run")]


@pytest.mark.parametrize("harvest_error", [False, True])
def test_recovery_alert_precedes_worktree_removal(root, monkeypatch, harvest_error):
    j, g = journal(root), git()
    for stem in ("existing", "missing"):
        j.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket=stem)
    path = orphan_worktree(root, "existing")
    trace, syncs = [], []
    append, remove, prune, fsync = j.append, g.worktree_remove, g.worktree_prune, journal_module.os.fsync

    def synced(fd):
        fsync(fd)
        syncs.append(fd)

    def appended(type, body, **kwargs):
        count = len(syncs)
        event = append(type, body, **kwargs)
        assert len(syncs) > count and journal(root).read()[-1] == event
        trace.append((event.ticket, body.get("to", body.get("kind", body.get("signal")))))
        return event

    async def harvest(stem, sequence):
        assert stem == "existing" and sequence == 0
        trace.append((stem, "harvest"))
        if harvest_error:
            raise ValueError("scripted harvest failure")

    async def removed(repo, worktree):
        terminal, alert = journal(root).read()[-2:]
        assert terminal.body == {"to": "abandoned"} and terminal.ticket == worktree.name
        assert alert.body == recovery_body(0) and alert.ticket == worktree.name and alert.key is None
        trace.append((worktree.name, "remove"))
        await remove(repo, worktree)

    async def pruned(repo):
        trace.append((None, "prune"))
        await prune(repo)

    monkeypatch.setattr(journal_module.os, "fsync", synced)
    monkeypatch.setattr(j, "append", appended)
    monkeypatch.setattr(g, "worktree_remove", removed)
    monkeypatch.setattr(g, "worktree_prune", pruned)
    assert asyncio.run(reconcile(j, g, root, root / STATE / "worktrees", harvest)) == ["existing", "missing"]
    assert trace == [("existing", "harvest")] + (
        [("existing", "harvest_failed")] if harvest_error else []) + [
        ("existing", "abandoned"), ("existing", "recovery_alert"), ("existing", "remove"),
        (None, "prune"), ("missing", "abandoned"), ("missing", "recovery_alert"), (None, "prune")]
    errors = [e for e in j.read() if e.body.get("signal") == "harvest_failed"]
    if harvest_error:
        [error] = errors
        assert error.ticket == "existing" and error.key is None
        assert error.body == {"signal": "harvest_failed", "error": "ValueError: scripted harvest failure"}
    else:
        assert errors == []
    assert not path.exists() and audit_journal(j) == []


def test_recovery_alert_is_once_per_reaped_run(root):
    j = journal(root)

    async def harvest(stem, sequence):
        pytest.fail("missing worktree invoked harvest")

    def recover(handle):
        return asyncio.run(reconcile(handle, git(), root, root / STATE / "worktrees", harvest))

    j.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket="dead")
    assert recover(j) == ["dead"]
    closed = j.read()
    assert recover(j) == [] and j.read() == closed
    j.close()
    recovered = journal(root)
    assert recover(recovered) == [] and recovered.read() == closed
    recovered.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket="dead")
    assert recover(recovered) == ["dead"]
    assert [e.body for e in recovered.read() if e.body.get("kind") == RECOVERY_ALERT] == [
        recovery_body(0), recovery_body(1)]
    assert run_seq(recovered.read(), "dead") == 2 and audit_journal(recovered) == []


@pytest.mark.parametrize("failure", ["alert", "after-alert", "cleanup"])
def test_recovery_alert_failures_preserve_terminal_history(root, monkeypatch, failure):
    j, g = journal(root), git()
    j.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket="dead")
    path = orphan_worktree(root, "dead")
    error = OSError("scripted recovery failure")
    trace = []
    append = j.append

    def appended(type, body, **kwargs):
        if body.get("kind") == RECOVERY_ALERT and failure in {"alert", "after-alert"}:
            if failure == "after-alert":
                append(type, body, **kwargs)
            raise error
        return append(type, body, **kwargs)

    async def harvest(stem, sequence):
        trace.append("harvest")

    async def removed(*args):
        trace.append("remove")
        raise error

    async def pruned(*args):
        pytest.fail("failed recovery reached prune")

    monkeypatch.setattr(j, "append", appended)
    monkeypatch.setattr(g, "worktree_remove", removed)
    monkeypatch.setattr(g, "worktree_prune", pruned)
    with pytest.raises(OSError) as caught:
        asyncio.run(reconcile(j, g, root, root / STATE / "worktrees", harvest))
    assert caught.value is error
    assert trace == (["harvest", "remove"] if failure == "cleanup" else ["harvest"])
    events = journal(root).read()
    assert events[1].body == {"to": "abandoned"} and events[1].key is None
    assert [e.body for e in events if e.type == EventType.SIGNAL] == (
        [] if failure == "alert" else [recovery_body(0)])
    assert path.exists() and orphans(events) == [] and run_seq(events, "dead") == 1
    assert reap(root) == [] and journal(root).read() == events and path.exists()
    assert audit_journal(j) == []


@pytest.mark.parametrize("verb", ["run", "drain"])
def test_recovery_alert_does_not_change_disposition(root, verb):
    j = journal(root)
    write(root, "work", ticket())
    for stem in ("history", "work"):
        j.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket=stem)
        j.append(EventType.EFFECT_INTENT, {}, ticket=stem, key=f"{stem}/incomplete")
        orphan_worktree(root, stem)
    j.append(EventType.STATE_TRANSITION, {"to": "infra_error"}, ticket="history")
    before = j.read()
    assert reap(root) == ["work"]
    assert j.read() == before + j.read()[-2:]
    terminal, alert = j.read()[-2:]
    assert terminal.body == {"to": "abandoned"} and alert.body == recovery_body(0)
    assert terminal.ticket == alert.ticket == "work" and terminal.key is alert.key is None
    assert (root / STATE / "worktrees/history").exists()
    assert not (root / STATE / "box").exists()
    assert audit_journal(j) == []
    stages = Stages()

    def prepare(checkout):
        callback = stages(checkout)
        ctx = runner.bind(checkout, FakeLLM([])).ctx

        async def dispatch(work):
            assert run_seq(checkout.journal.read(), work.stem) == 1
            fresh = await stage_module.prepare_worktree(ctx, work.stem)
            assert fresh.exists() and not (fresh / "half-done.py").exists()
            return await callback(work)

        dispatch.stages = callback.stages
        return dispatch

    assert cli(root, *(["run", "work"] if verb == "run" else ["drain"]), stages=prepare) == 0
    assert stages.calls == ["work"] and stages.lock_held == [True]
    assert last_states(j.read()) == {"history": "infra_error", "work": "merged"}
