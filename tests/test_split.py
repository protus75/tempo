"""Split proposal checks and supersession (AGENT_PLAN.md section 4.1, "Box, split, confirm, and retro")."""

import asyncio

import pytest

from tempo.backlog import (SUPERSEDES, dead_dependencies, settled_dependencies, successor_leaves,
                           supersedes_maps)
from tempo.git import GitError
from tempo.journal import EventType
from tempo.split import TicketProposal, check_stems, check_tickets, record_supersedes
from tempo.tickets import ticket_path, validate_ticket
from tests.test_mergequeue import commit, ctx  # noqa: F401 -- fixture
from tests.test_stages import TICKET

run = asyncio.run


async def original(ctx, *, source="human"):
    text = TICKET.format(bypass="agent_tier: high\nagent_effort: low\n").replace("source: human", f"source: {source}")
    ctx.fs.write(ctx.repo / ticket_path("original"), text.encode())
    await commit(ctx, ctx.repo, [ticket_path("original")])
    return validate_ticket("original", text, ctx.repo), text


def proposals(text, *, second_dep="piece-one"):
    first = text.replace("`thing.py` holds the word ok.", "First portion of `thing.py` holds ok.")
    second = text.replace("`thing.py` holds the word ok.", "Second portion of `thing.py` holds ok.")
    second = second.replace("## Depends on\nnone", f"## Depends on\n- {second_dep}")
    return [TicketProposal(stem="piece-one", ticket=first), TicketProposal(stem="piece-two", ticket=second)]


def signals(ctx, signal=None):
    return [e for e in ctx.driver.journal.read() if e.type == EventType.SIGNAL
            and (signal is None or e.body.get("signal") == signal)]


def test_split_stems_must_be_two_or_more_and_fresh(ctx):
    async def scenario():
        ticket, text = await original(ctx)
        fresh = proposals(text)
        assert check_stems(ctx, ticket, fresh) == []
        assert check_stems(ctx, ticket, fresh[:1])[0].message == "a split needs at least two distinct successor stems"
        assert check_stems(ctx, ticket, [fresh[0], fresh[0]])
        for stem in ("original", "retro", "Bad_Stem"):
            assert check_stems(ctx, ticket, [fresh[0].model_copy(update={"stem": stem}), fresh[1]]), stem
        ctx.driver.journal.append(EventType.SIGNAL, {"signal": "session_begun"}, ticket="piece-two")
        assert [f.message for f in check_stems(ctx, ticket, fresh)] == ["successor 'piece-two' is not fresh"]
    run(scenario())


@pytest.mark.parametrize("case", ["ok", "source", "state", "tier", "depends-original", "invalid"])
def test_split_tickets_are_valid_beside_each_other_and_keep_the_original_fields(ctx, case):
    async def scenario():
        ticket, text = await original(ctx)
        made = proposals(text, second_dep="original" if case == "depends-original" else "piece-one")
        edits = {"source": ("source: human", "source: box:suggestion"),
                 "state": ("state: confirmed", "state: draft"),
                 "tier": ("agent_tier: high", "agent_tier: low"),
                 "invalid": ("priority: P2", "priority: PX")}
        if case in edits:
            made[0] = made[0].model_copy(update={"ticket": made[0].ticket.replace(*edits[case])})
        tree = ctx.config.worktree_root / "original.split"
        await ctx.git.worktree_add_detached(ctx.repo, tree, "HEAD")
        for p in made:
            ctx.fs.write(tree / ticket_path(p.stem), p.ticket.encode())
        findings = check_tickets(ticket, made, tree)
        if case == "ok":
            assert findings == []
        elif case == "depends-original":
            assert [f.message for f in findings] == ["a successor depends on the superseded original"]
        elif case == "invalid":
            assert findings and findings[0].path == ticket_path("piece-one")
        else:
            key = {"tier": "agent_tier"}.get(case, case)
            assert [f.message.split(":")[0] for f in findings] == [f"proposal must preserve {key}"]
    run(scenario())


def test_supersedes_record_after_publication_is_idempotent(ctx):
    async def scenario():
        ticket, text = await original(ctx)
        accepted = proposals(text)
        assert await record_supersedes(ctx, ticket.stem, accepted) and signals(ctx, SUPERSEDES) == []
        for p in accepted:
            ctx.fs.write(ctx.repo / ticket_path(p.stem), p.ticket.encode())
        with pytest.raises(GitError):
            await ctx.git.commit(ctx.repo, "failed successor publication without staged proposals")
        assert await record_supersedes(ctx, ticket.stem, accepted) and signals(ctx, SUPERSEDES) == []
        await commit(ctx, ctx.repo, [ticket_path(accepted[0].stem)])
        assert await record_supersedes(ctx, ticket.stem, accepted) and signals(ctx, SUPERSEDES) == []
        p = accepted[1]
        ctx.fs.write(ctx.repo / ticket_path(p.stem), (p.ticket + "\nchanged after review").encode())
        await commit(ctx, ctx.repo, [ticket_path(p.stem)])
        assert await record_supersedes(ctx, ticket.stem, accepted) and signals(ctx, SUPERSEDES) == []
        ctx.fs.write(ctx.repo / ticket_path(p.stem), p.ticket.encode())
        await commit(ctx, ctx.repo, [ticket_path(p.stem)])
        published = await ctx.git.rev_parse(ctx.repo, "HEAD")
        assert await record_supersedes(ctx, ticket.stem, accepted) == []
        [event] = signals(ctx, SUPERSEDES)
        assert event.ticket == ticket.stem and event.key is None and event.type == EventType.SIGNAL
        assert event.body == {"signal": SUPERSEDES, "successors": ["piece-one", "piece-two"]}
        assert await ctx.git.rev_parse(ctx.repo, "HEAD") == published
        count = len(ctx.driver.journal.read())
        assert await record_supersedes(ctx, ticket.stem, accepted) == [] and len(ctx.driver.journal.read()) == count
        conflicting = [accepted[0], accepted[1].model_copy(update={"stem": "piece-three"})]
        assert (await record_supersedes(ctx, ticket.stem, conflicting))[0].paved_road
        assert (await record_supersedes(ctx, "piece-one", accepted))[0].paved_road  # a self-edge
        # piece-three -> original -> piece-one -> piece-three would be a transitive cycle.
        ctx.driver.journal.append(EventType.SIGNAL, {"signal": SUPERSEDES, "successors": ["piece-three"]}, ticket="piece-one")
        cyclic = [accepted[0].model_copy(update={"stem": ticket.stem}), accepted[1]]
        assert (await record_supersedes(ctx, "piece-three", cyclic))[0].paved_road
        assert len(signals(ctx, SUPERSEDES)) == 2
    run(scenario())


@pytest.mark.parametrize("leaf_state", ["merged", "already_satisfied", "running", "gate_failed", "infra_error",
                                        "timeout", "premise_failed", "rejected", "abandoned"])
def test_supersedes_dependency_folds(ctx, leaf_state):
    journal = ctx.driver.journal
    for stem, next_stems in [("original", ["piece-one", "piece-two"]), ("piece-one", ["leaf-a", "leaf-b"])]:
        journal.append(EventType.SIGNAL, {"signal": SUPERSEDES, "successors": next_stems}, ticket=stem, key=None)
    journal.append(EventType.SIGNAL, {"signal": "handoff_written", "successors": ["ignore"]}, ticket="original")
    for stem, state in [("original", "rejected"), ("piece-one", "abandoned"), ("piece-two", "merged"),
                         ("leaf-a", "already_satisfied"), ("leaf-b", "rejected"), ("leaf-b", leaf_state)]:
        journal.append(EventType.STATE_TRANSITION, {"to": state, "stem": "wrong-envelope"}, ticket=stem)
    events = journal.read()
    maps = supersedes_maps(iter(events))
    assert maps == {"original": ("piece-one", "piece-two"), "piece-one": ("leaf-a", "leaf-b")}
    assert successor_leaves("original", maps) == {"leaf-a", "leaf-b", "piece-two"}
    settled, dead = settled_dependencies(iter(events)), dead_dependencies(iter(events))
    assert "piece-two" in settled and "leaf-a" in settled and "wrong-envelope" not in settled | dead
    assert ("original" in settled) == ("piece-one" in settled) == (leaf_state in {"merged", "already_satisfied"})
    assert ("original" in dead) == ("piece-one" in dead) == (leaf_state in {"rejected", "abandoned"})
    # These are the reusable predicates for an existing original dependent; no producer changes.
    assert ({"original"} <= settled) == (leaf_state in {"merged", "already_satisfied"})
    assert bool({"original"} & dead) == (leaf_state in {"rejected", "abandoned"})
