"""Direct construction evidence; activation must migrate the dormancy assertion."""

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tempo import rework as module
from tempo.artifacts import Artifact, Finding
from tempo.caps import CAPS, capability, consume, draws
from tempo.git import GitError
from tempo.journal import EventType
from tempo.llm import FakeLLM, HANG, LLMResult
from tempo.mergequeue import ConflictHandoff, MergeQueue
from tempo.rework import (REWORK_ORDER, SUPERSEDES, ReworkOrder, dead_dependencies, record_supersedes,
                          rework, settled_dependencies, successor_leaves, supersedes_maps)
from tempo.specs import (DATA_DELIM, QUOTED_DELIM, RENDER_BOUND_CHARS, data_close, data_open,
                         lint_spec, load_spec, render, validate_data_blocks)
from tempo.stages import KNOWN_ARTIFACTS
from tempo.tickets import ticket_path, validate_ticket
from tests.test_mergequeue import commit, conflict, ctx, import_closure
from tests.test_stages import TICKET

run = asyncio.run
SNAG = Finding(code="scope", path="tempo/thing.py", line=1, message="narrow the work",
               paved_road="retain only the first buildable piece", kind="authoring_error")


async def original(ctx, *, source="human", payload=""):
    text = TICKET.format(bypass="agent_tier: high\nagent_effort: low\n").replace("source: human", f"source: {source}")
    if source == "seed":
        ctx.fs.write(ctx.repo / "TEMPO_PLAN.md", b"# Plan\n## 19. Construction\n### 19.L Authoring\nRules.\n### 19.P3 Phase\nRules.\n")
        text = text.replace("## Goal / Why", "## Plan contract\n- 19.L\n- 19.P3\n\n## Goal / Why")
    text = text.replace("`thing.py` holds the word ok.", "`thing.py` holds the word ok." + payload)
    ctx.fs.write(ctx.repo / ticket_path("original"), text.encode())
    await commit(ctx, ctx.repo, [ticket_path("original"), "TEMPO_PLAN.md"])
    return validate_ticket("original", text, ctx.repo), text


def order(action="escalate", proposals=()):
    return json.dumps({"action": action, "tickets": [{"stem": stem, "ticket": text} for stem, text in proposals]})


def requisition(verdict="approve", finding=SNAG):
    return json.dumps({"verdict": verdict, "summary": "judged each proposal against the shipped grammar",
                       "findings": [] if verdict == "approve" else [finding.model_dump()]})


async def invoke(ctx, ticket, text, script, *, attempt=0, workspace=None, handoff=None, snags=None):
    ctx.driver.llm = FakeLLM(script)
    result = await rework(ctx, ticket, text, snags or [SNAG], attempt=attempt,
                          workspace=workspace or ctx.repo, tier="max", effort="medium",
                          stuck_budget=42.0, handoff=handoff)
    return result, ctx.driver.llm.requests


def signals(ctx, signal=None):
    return [e for e in ctx.driver.journal.read() if e.type == EventType.SIGNAL
            and (signal is None or e.body.get("signal") == signal)]


def ticket_bytes(ctx):
    return {str(p.relative_to(ctx.repo)): p.read_bytes() for p in (ctx.repo / "tickets").rglob("*") if p.is_file()}


def no_orders(ctx):
    assert signals(ctx, REWORK_ORDER) == signals(ctx, SUPERSEDES) == []


def successors(text, *, second_dep="piece-one"):
    first = text.replace("`thing.py` holds the word ok.", "First portion of `thing.py` holds ok.")
    second = text.replace("`thing.py` holds the word ok.", "Second portion of `thing.py` holds ok.")
    second = second.replace("## Depends on\nnone", f"## Depends on\n- {second_dep}")
    return [("piece-one", first), ("piece-two", second)]


@pytest.mark.parametrize("source", ["human", "seed", "box:suggestion"])
def test_update_order_is_reviewed_without_ticket_mutation(ctx, monkeypatch, source):
    async def scenario():
        ticket, text = await original(ctx, source=source)
        revised = text.replace("`thing.py` holds the word ok.", "Narrowed `thing.py` holds ok.")
        before, head = ticket_bytes(ctx), await ctx.git.rev_parse(ctx.repo, "HEAD")
        validations, reviews = [], []
        validate, review = module.validate_ticket, module.review_ticket
        def checked(stem, proposal, repo, *args):
            validations.append((stem, repo))
            return validate(stem, proposal, repo, *args)
        async def reviewed(*args, **kwargs):
            reviews.append(kwargs)
            return await review(*args, **kwargs)
        monkeypatch.setattr(module, "validate_ticket", checked)
        monkeypatch.setattr(module, "review_ticket", reviewed)
        result, requests = await invoke(ctx, ticket, text, [order("update", [(ticket.stem, revised)]), requisition()])
        assert result.outcome == "ok", result.findings
        assert isinstance(result.artifact, Artifact) and isinstance(result.artifact, ReworkOrder)
        assert result.artifact.action == "update" and [p.stem for p in result.artifact.tickets] == [ticket.stem]
        assert len(validations) == len(reviews) == 1 and validations[0][1] != ctx.repo
        assert not reviews[0]["repo"].exists()
        parsed = validate(ticket.stem, result.artifact.tickets[0].ticket, ctx.repo)
        assert parsed.frontmatter == ticket.frontmatter
        assert [r.surface for r in requests] == ["rework", "requisition_review"]
        assert "Original stem: original" in requests[0].rendered
        assert all(r.worktree is None for r in requests)
        assert revised.strip() in requests[1].rendered and "original_ticket" in requests[1].rendered
        assert ticket_bytes(ctx) == before and await ctx.git.rev_parse(ctx.repo, "HEAD") == head
        assert await ctx.git.status_porcelain(ctx.repo) == ""
        assert signals(ctx, SUPERSEDES) == []
    run(scenario())


@pytest.mark.parametrize("source", ["human", "seed", "box:failure_report"])
def test_split_order_requires_fresh_buildable_successors(ctx, monkeypatch, source):
    async def scenario():
        ticket, text = await original(ctx, source=source)
        predecessor = "predecessor"
        ctx.fs.write(ctx.repo / ticket_path(predecessor), text.encode())
        await commit(ctx, ctx.repo, [ticket_path(predecessor)])
        text = text.replace("## Depends on\nnone", "## Depends on\n- predecessor")
        ctx.fs.write(ctx.repo / ticket_path(ticket.stem), text.encode())
        await commit(ctx, ctx.repo, [ticket_path(ticket.stem)])
        ticket = validate_ticket(ticket.stem, text, ctx.repo)
        first = text
        second = text.replace("- predecessor\n", "- predecessor\n- piece-one\n", 1)
        proposals = [("piece-one", first), ("piece-two", second)]
        before, head = ticket_bytes(ctx), await ctx.git.rev_parse(ctx.repo, "HEAD")
        reviews = []
        review = module.review_ticket
        async def reviewed(*args, **kwargs):
            tree = kwargs["repo"]
            assert tree != ctx.repo and all((tree / ticket_path(s)).is_file() for s, _ in proposals)
            assert (tree / "tempo/thing.py").is_file() and (tree / ticket_path(predecessor)).is_file()
            parsed = validate_ticket(kwargs["stem"], kwargs["text"], tree)
            assert parsed.frontmatter == ticket.frontmatter and predecessor in parsed.depends
            reviews.append(kwargs)
            return await review(*args, **kwargs)
        monkeypatch.setattr(module, "review_ticket", reviewed)
        result, requests = await invoke(ctx, ticket, text, [order("split", proposals), requisition(), requisition()])
        assert result.outcome == "ok", result.findings
        assert result.artifact.action == "split" and {p.stem for p in result.artifact.tickets} == {s for s, _ in proposals}
        assert [r["stem"] for r in reviews] == ["piece-one", "piece-two"]
        assert len({r["call_seq"] for r in reviews}) == 2 and all(not r["repo"].exists() for r in reviews)
        assert [r.surface for r in requests] == ["rework", "requisition_review", "requisition_review"]
        assert ticket_bytes(ctx) == before and await ctx.git.rev_parse(ctx.repo, "HEAD") == head
        assert await ctx.git.status_porcelain(ctx.repo) == "" and signals(ctx, SUPERSEDES) == []
    run(scenario())


@pytest.mark.parametrize("case", ["unknown", "extra", "proposal-extra", "blank", "wrong-type", "update-none",
    "update-two", "split-one", "split-duplicate", "escalate-ticket", "wrong-original", "split-original",
    "existing-dir", "journal-identity", "reserved", "invalid-ticket", "source", "state", "tier", "effort",
    "extra-frontmatter", "missing-context", "missing-dependency", "self-dependency", "sibling-cycle",
    "existing-cycle", "original-dependency", "snag", "rma", "split-snag", "split-rma"])
def test_rework_refuses_invalid_or_unreviewed_orders(ctx, case):
    async def scenario():
        ticket, text = await original(ctx)
        reply = json.loads(order("update", [(ticket.stem, text)]))
        schema = case in {"unknown", "extra", "proposal-extra", "blank", "wrong-type", "update-none",
                          "update-two", "split-one", "split-duplicate", "escalate-ticket"}
        if case == "unknown": reply["action"] = "repair"
        if case == "extra": reply["rung"] = {"tier": "max", "effort": "max"}
        if case == "proposal-extra": reply["tickets"][0]["approval"] = True
        if case == "blank": reply["tickets"][0]["ticket"] = "   "
        if case == "wrong-type": reply["tickets"][0]["stem"] = 4
        if case == "update-none": reply["tickets"] = []
        if case == "update-two": reply["tickets"].append({"stem": "new-ticket", "ticket": text})
        if case == "split-one": reply["action"] = "split"
        if case == "split-duplicate": reply["action"] = "split"; reply["tickets"] *= 2
        if case == "escalate-ticket": reply["action"] = "escalate"
        if case == "wrong-original": reply["tickets"][0]["stem"] = "different"
        if case == "invalid-ticket": reply["tickets"][0]["ticket"] = "no grammar"
        for name, old, new in [("source", "human", "seed"), ("state", "confirmed", "draft"),
                                ("tier", "high", "max"), ("effort", "low", "max")]:
            if case == name:
                key = "agent_" + name if name in {"tier", "effort"} else name
                reply["tickets"][0]["ticket"] = text.replace(f"{key}: {old}", f"{key}: {new}")
        if case == "extra-frontmatter": reply["tickets"][0]["ticket"] = text.replace("kind: feature", "kind: feature\nrung: max")
        if case == "missing-context": reply["tickets"][0]["ticket"] = text.replace("- tempo/thing.py", "- absent.py", 1)
        if case in {"missing-dependency", "self-dependency", "existing-cycle"}:
            dep = {"missing-dependency": "absent", "self-dependency": ticket.stem, "existing-cycle": "predecessor"}[case]
            if case == "existing-cycle":
                ctx.fs.write(ctx.repo / ticket_path(dep), text.replace("## Depends on\nnone", "## Depends on\n- original").encode())
                await commit(ctx, ctx.repo, [ticket_path(dep)])
            reply["tickets"][0]["ticket"] = text.replace("## Depends on\nnone", f"## Depends on\n- {dep}")
        if case in {"split-original", "existing-dir", "journal-identity", "reserved", "sibling-cycle", "original-dependency", "split-snag", "split-rma"}:
            proposals = successors(text)
            if case == "split-original": proposals[0] = (ticket.stem, text)
            if case == "existing-dir": (ctx.repo / "tickets/piece-one").mkdir()
            if case == "journal-identity": ctx.driver.journal.append(EventType.EFFECT_COMPLETION, {}, ticket="piece-one")
            if case == "reserved": proposals[0] = ("decisions", text)
            if case == "sibling-cycle": proposals[0] = ("piece-one", text.replace("## Depends on\nnone", "## Depends on\n- piece-two"))
            if case == "original-dependency": proposals[0] = ("piece-one", text.replace("## Depends on\nnone", "## Depends on\n- original"))
            reply = json.loads(order("split", proposals))
        before = ticket_bytes(ctx)
        ctx.driver.retry_cap = 0 if case not in {"snag", "rma", "split-snag", "split-rma"} else 1
        def assert_unaccepted(req):
            no_orders(ctx)
            return requisition("rma" if case in {"rma", "split-rma"} else "snag")
        script = [json.dumps(reply)]
        if case in {"snag", "rma", "split-snag", "split-rma"}:
            if case == "split-rma": script.append(requisition())
            script.append(assert_unaccepted)
            if case not in {"rma", "split-rma"}:
                if case == "split-snag": script.append(requisition())
                script.extend([json.dumps(reply), requisition()])
                if case == "split-snag": script.append(requisition())
        result, requests = await invoke(ctx, ticket, text, script)
        if case in {"snag", "split-snag"}:
            assert result.outcome == "ok", result.findings
            retry = [r for r in requests if r.surface == "rework"][1].rendered
            assert SNAG.message in retry and SNAG.paved_road in retry
            assert len(signals(ctx, REWORK_ORDER)) == 1
        else:
            assert result.outcome == ("invalid_artifact" if schema else "gate_failed"), result.findings
            assert result.artifact is None and result.findings and all(f.paved_road for f in result.findings)
            no_orders(ctx)
            assert len(requests) == {"rma": 2, "split-rma": 3}.get(case, 1)
            if case in {"rma", "split-rma"}: assert result.findings[0].code == "requisition_rma"
        assert ticket_bytes(ctx) == before and signals(ctx, SUPERSEDES) == []
        assert not list((ctx.config.state_dir / "rework").glob("*/*/*/tickets"))
    run(scenario())


def test_escalate_order_does_not_edit_capability_or_draw_caps(ctx):
    async def scenario():
        ticket, text = await original(ctx)
        consume(ctx.driver.journal, ticket.stem, "retry", "blob", rung={"tier": "max", "effort": "high"})
        before = ctx.driver.journal.read()
        caps = {cap: draws(before, ticket.stem, cap) for cap in CAPS}
        saved = ticket_bytes(ctx)
        result, requests = await invoke(ctx, ticket, text, [order()])
        after = ctx.driver.journal.read()
        assert result.outcome == "ok" and result.artifact.action == "escalate" and result.artifact.tickets == []
        assert ticket_bytes(ctx) == saved and capability(ticket, before) == capability(ticket, after) == ("max", "high")
        assert {cap: draws(after, ticket.stem, cap) for cap in CAPS} == caps
        assert [e for e in after if e.type in {EventType.CAP_CONSUMED, EventType.STATE_TRANSITION}] == [
            e for e in before if e.type in {EventType.CAP_CONSUMED, EventType.STATE_TRANSITION}]
        assert [(r.tier, r.effort, r.worktree) for r in requests] == [("max", "medium", None)]
    run(scenario())


@pytest.mark.parametrize("phase", ["checkout", "review"])
def test_rework_timeout_removes_disposable_tree(ctx, monkeypatch, phase):
    async def scenario():
        ticket, text = await original(ctx)
        entered, expired = asyncio.Event(), asyncio.Event()
        async def sleep(seconds):
            if seconds == 600.0:  # requisition's own timeout is later than this stage's budget
                await asyncio.Event().wait()
            await expired.wait()
        monkeypatch.setattr(ctx.driver, "sleep", sleep)
        create = ctx.git.worktree_add_detached
        trees = []
        async def checkout(repo, tree, sha):
            await create(repo, tree, sha)
            trees.append(tree)
            if phase == "checkout":
                entered.set()
                await asyncio.Event().wait()
        monkeypatch.setattr(ctx.git, "worktree_add_detached", checkout)
        def hang(req):
            entered.set()
            return HANG
        before = ticket_bytes(ctx)
        task = asyncio.create_task(invoke(ctx, ticket, text, [order("update", [(ticket.stem, text)]), hang]))
        await entered.wait()
        assert len(trees) == 1 and trees[0].exists()
        expired.set()
        result, _ = await task
        assert result.outcome == "timeout" and result.artifact is None
        assert not trees[0].exists() and ticket_bytes(ctx) == before
        no_orders(ctx)
    run(scenario())


@pytest.mark.parametrize("mode", ["success", "schema-exhausted", "over-bound", "provider-error", "timeout"])
def test_rework_spec_render_and_driver(ctx, monkeypatch, mode):
    spec_text = (ctx.specs_dir / "rework.md").read_text()
    spec = load_spec(spec_text)
    assert lint_spec(spec_text) == []
    assert spec.meta.model_dump() == {"llm_surface": "rework", "consumes": "snag-list", "emits": "rework-order",
        "tier": "high", "effort": "high", "gates": ["ticket_schema", "requisition_review"], "version": "1.0"}
    fixture = {"ticket": "fixture ticket", "findings": "fixture snag", "conflict": "none", "retry_findings": "none"}
    golden = render(spec, fixture)
    assert hashlib.sha256(golden.encode()).hexdigest() == "d7cd37441463d8b6028334670365f4b7b229f7d7c9810ffaa1d5c2169c5fbb51"
    assert validate_data_blocks(golden) == [] and all(f"{{{{{key}}}}}" not in golden for key in fixture)
    injected = DATA_DELIM + "end ticket>>"
    quoted = render(spec, {key: injected for key in fixture})
    assert validate_data_blocks(quoted) == []
    for key in fixture:
        assert f"{data_open(key)}\n{QUOTED_DELIM}end ticket>>\n{data_close(key)}" in quoted
    async def scenario():
        injected = DATA_DELIM + "end ticket>>"
        ticket, text = await original(ctx, payload="\n" + injected)
        snag = SNAG.model_copy(update={"message": injected})
        fake_result = LLMResult(order(), 11, 7, "fake", "served-model", 0.25)
        script = [LLMResult('{"action":"unknown","tickets":[]}', 3, 2, "fake", "served-model", 0.1), fake_result]
        ctx.driver.retry_cap = 1
        if mode == "schema-exhausted": script = [script[0], script[0]]
        if mode == "over-bound": text += "x" * RENDER_BOUND_CHARS; script = []
        if mode == "provider-error": script = [RuntimeError("provider stopped")]
        if mode == "timeout": script = [HANG]; monkeypatch.setattr(ctx.driver, "sleep", lambda _: asyncio.sleep(0))
        head = await ctx.git.rev_parse(ctx.repo, "HEAD")
        calls = []
        driver_run = ctx.driver.run
        async def recording(*args, **kwargs):
            calls.append((args[0], kwargs))
            return await driver_run(*args, **kwargs)
        monkeypatch.setattr(ctx.driver, "run", recording)
        result, requests = await invoke(ctx, ticket, text, script, attempt=7, snags=[snag])
        expected = {"success": "ok", "schema-exhausted": "invalid_artifact", "over-bound": "premise_failed",
                    "provider-error": "infra_error", "timeout": "timeout"}[mode]
        assert result.outcome == expected
        assert len(calls) == 1 and calls[0][0].surface == "rework"
        assert calls[0][1]["attempt"] == 7 and calls[0][1]["stuck_budget"] == 42.0
        assert all(r.worktree is None and r.tier == "max" and r.effort == "medium" for r in requests)
        if mode == "over-bound":
            assert requests == [] and result.findings[0].code == "render_over_bound" and result.cost.attempts == 0
        else:
            assert result.cost.attempts == len(requests)
            prompt = requests[0].rendered
            assert QUOTED_DELIM + "end ticket>>" in prompt and validate_data_blocks(prompt) == []
            for block in ["ticket", "findings", "conflict", "retry_findings"]:
                assert data_open(block) in prompt and data_close(block) in prompt
            spool = ctx.config.state_dir / "spools/original/7/rework/call-01/prompt.md"
            assert spool.read_text() == prompt
        if mode == "success":
            assert "invalid_artifact" in requests[1].rendered
            assert result.cost.tokens == 23 and result.cost.usd == pytest.approx(0.35)
            assert result.cost.provider == "fake" and result.cost.model == "served-model"
            artifact = result.artifact
            assert artifact.produced_at_sha == head and artifact.produced_by_spec_version == 1
            assert artifact.artifact_schema_version == 1 and artifact.stem == ticket.stem and artifact.attempt == 7
            [event] = signals(ctx, REWORK_ORDER)
            assert event.type == EventType.SIGNAL and event.ticket == ticket.stem and event.key is None
            assert event.body == {"signal": REWORK_ORDER, "attempt": 7, "action": "escalate"}
        else:
            no_orders(ctx)
        assert "rework_order" not in KNOWN_ARTIFACTS and signals(ctx, SUPERSEDES) == []
    run(scenario())


def test_conflict_handoff_consumed_after_admission_unwinds(ctx, monkeypatch):
    async def scenario():
        ticket, path = await conflict(ctx)
        ctx.config.merge.strategies = []
        text = (ctx.repo / ticket_path(ticket.stem)).read_text()
        main, branch = await ctx.git.rev_parse(ctx.repo, "main"), await ctx.git.rev_parse(ctx.repo, ticket.stem)
        saved = ticket_bytes(ctx)
        chronology = []
        git_call = ctx.git._call
        async def observed(root, *args, **kwargs):
            result = await git_call(root, *args, **kwargs)
            if args == ("rebase", "--abort"):
                chronology.append("aborted")
            return result
        monkeypatch.setattr(ctx.git, "_call", observed)
        queue = MergeQueue(ctx, escalate=lambda _: None)
        queue.offer(ticket, attempt=0)
        [handoff] = await queue.process()
        assert isinstance(handoff, ConflictHandoff) and handoff.approval_invalidated is True
        assert queue.active is None and not queue._slot.locked()
        chronology.append("released")
        assert handoff.conflicted_paths == [path] and handoff.reviewed_sha == branch
        def model_call(req):
            assert queue.active is None and not queue._slot.locked()
            chronology.append("rework")
            assert handoff.model_dump_json() in req.rendered
            assert handoff.findings[0].message in req.rendered and path in req.rendered
            assert '"approval_invalidated":true' in req.rendered and "fresh" in req.rendered
            return order("update", [(ticket.stem, text)])
        result, _ = await invoke(ctx, ticket, text, [model_call, requisition()],
                                  workspace=ctx.worktree(ticket.stem), handoff=handoff)
        assert result.outcome == "ok" and chronology == ["aborted", "released", "rework"]
        assert result.artifact.produced_at_sha == branch and handoff.approval_invalidated is True
        assert await ctx.git.rev_parse(ctx.repo, "main") == main and await ctx.git.rev_parse(ctx.repo, ticket.stem) == branch
        assert ticket_bytes(ctx) == saved and await ctx.git.status_porcelain(ctx.worktree(ticket.stem)) == ""
        assert not any(e.type == EventType.STATE_TRANSITION for e in ctx.driver.journal.read())
        mismatch = handoff.model_copy(update={"stem": "another"})
        refused, requests = await invoke(ctx, ticket, text, [], attempt=1, handoff=mismatch)
        assert refused.outcome == "gate_failed" and requests == [] and refused.findings[0].paved_road
        for paths in [["z", "a"], ["a", "a"]]:
            with pytest.raises(ValidationError): ConflictHandoff(stem=ticket.stem, reviewed_sha=branch, conflicted_paths=paths, findings=[])
        with pytest.raises(ValidationError):
            ConflictHandoff(stem=ticket.stem, reviewed_sha=branch, conflicted_paths=[], findings=[], approval_invalidated=False)
    run(scenario())


def test_supersedes_record_after_publication_is_idempotent(ctx):
    async def scenario():
        ticket, text = await original(ctx)
        result, _ = await invoke(ctx, ticket, text, [order("split", successors(text)), requisition(), requisition()])
        assert result.outcome == "ok" and signals(ctx, SUPERSEDES) == []
        accepted = result.artifact
        assert await record_supersedes(ctx, accepted) and signals(ctx, SUPERSEDES) == []
        for p in accepted.tickets:
            ctx.fs.write(ctx.repo / ticket_path(p.stem), p.ticket.encode())
        with pytest.raises(GitError):
            await ctx.git.commit(ctx.repo, "failed successor publication without staged proposals")
        assert await record_supersedes(ctx, accepted) and signals(ctx, SUPERSEDES) == []
        await commit(ctx, ctx.repo, [ticket_path(accepted.tickets[0].stem)])
        assert await record_supersedes(ctx, accepted) and signals(ctx, SUPERSEDES) == []
        p = accepted.tickets[1]
        ctx.fs.write(ctx.repo / ticket_path(p.stem), (p.ticket + "\nchanged after review").encode())
        await commit(ctx, ctx.repo, [ticket_path(p.stem)])
        assert await record_supersedes(ctx, accepted) and signals(ctx, SUPERSEDES) == []
        ctx.fs.write(ctx.repo / ticket_path(p.stem), p.ticket.encode())
        await commit(ctx, ctx.repo, [ticket_path(p.stem)])
        published = await ctx.git.rev_parse(ctx.repo, "HEAD")
        assert await record_supersedes(ctx, accepted) == []
        [event] = signals(ctx, SUPERSEDES)
        assert event.ticket == ticket.stem and event.key is None and event.type == EventType.SIGNAL
        assert event.body == {"signal": SUPERSEDES, "successors": ["piece-one", "piece-two"]}
        assert await ctx.git.rev_parse(ctx.repo, "HEAD") == published
        count = len(ctx.driver.journal.read())
        assert await record_supersedes(ctx, accepted) == [] and len(ctx.driver.journal.read()) == count
        conflicting = accepted.model_copy(update={"tickets": [accepted.tickets[0], accepted.tickets[1].model_copy(update={"stem": "piece-three"})]})
        assert (await record_supersedes(ctx, conflicting))[0].paved_road
        for action in ["update", "escalate"]:
            other = ReworkOrder(action=action, tickets=[{"stem": ticket.stem, "ticket": text}] if action == "update" else [],
                stem=ticket.stem, attempt=1, produced_by_spec_version=1, produced_at_sha=published)
            assert await record_supersedes(ctx, other) == []
        self_edge = accepted.model_copy(update={"stem": "piece-one"})
        assert (await record_supersedes(ctx, self_edge))[0].paved_road
        # piece-three -> original -> piece-one -> piece-three would be a transitive cycle.
        ctx.driver.journal.append(EventType.SIGNAL, {"signal": SUPERSEDES, "successors": ["piece-three"]}, ticket="piece-one")
        cyclic = accepted.model_copy(update={"stem": "piece-three", "tickets": [
            accepted.tickets[0].model_copy(update={"stem": ticket.stem}), accepted.tickets[1]]})
        assert (await record_supersedes(ctx, cyclic))[0].paved_road
        assert len(signals(ctx, SUPERSEDES)) == 2
    run(scenario())


@pytest.mark.parametrize("leaf_state", ["merged", "already_satisfied", "running", "gate_failed", "infra_error",
                                        "timeout", "premise_failed", "rejected", "abandoned"])
def test_supersedes_dependency_folds(ctx, leaf_state):
    journal = ctx.driver.journal
    for stem, next_stems in [("original", ["piece-one", "piece-two"]), ("piece-one", ["leaf-a", "leaf-b"])]:
        journal.append(EventType.SIGNAL, {"signal": SUPERSEDES, "successors": next_stems}, ticket=stem, key=None)
    journal.append(EventType.SIGNAL, {"signal": "drain_handoff", "successors": ["ignore"]}, ticket="original")
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


def test_rework_is_reachable_from_production():
    root = Path(__file__).resolve().parents[1]
    sources = {"tempo." + p.stem: p.read_text() for p in (root / "tempo").glob("*.py")}
    def reachable(material):
        assert "tempo.rework" in import_closure("tempo.__main__", material)
    reachable(sources)
    removed = {name: "\n".join((line[:len(line) - len(line.lstrip())] + "pass")
               if line.lstrip().startswith("from tempo.rework import") else line
               for line in source.splitlines()) for name, source in sources.items()}
    with pytest.raises(AssertionError):
        reachable(removed)
    for spelling in ["import tempo.rework as rework", "from tempo import rework as stage"]:
        reachable({**removed, "tempo.runner": removed["tempo.runner"] + "\n" + spelling})


def test_admission_never_invokes_rework_inline(ctx, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("admission must never invoke Rework inline")
    monkeypatch.setattr(module, "rework", forbidden)
    async def scenario():
        ticket, _ = await conflict(ctx)
        ctx.config.merge.strategies = []
        queue = MergeQueue(ctx, escalate=lambda _: None)
        queue.offer(ticket, attempt=0)
        [result] = await queue.process()
        assert isinstance(result, ConflictHandoff) and ctx.driver.llm.requests == []
        assert not queue._slot.locked() and queue.active is None
        no_orders(ctx)
    run(scenario())
