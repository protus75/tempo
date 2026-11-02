"""Completed production receipts travel through the existing consumer and admission."""

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from tempo import runner, triage
from tempo.__main__ import build_control
from tempo.author import record_retro_ticket
from tempo.box import Box, BoxError, Resolution
from tempo.config import load_config
from tempo.effects import Effects
from tempo.journal import EventType, Journal
from tempo.llm import FakeLLM
from tempo.merge import write_squash
from tempo.retro import Retro, project_window
from tempo.specs import entry_unit_gap, required_units, resolve_plan_contract
from tempo.storm import arrival_id
from tempo.tickets import validate_ticket
from tests.test_drain import pause_checkout
from tests.test_stages import implement_reply, verdict
from tests.test_triage import author_reply, requisition_reply, reply, root_with_box

PROPOSAL = {"fixed_failure": "Observed café failure", "overcorrection_risk": "Rejecting useful inputs",
            "proposed_spec_paths": ["specs/review.md", "specs/author.md"]}


def test_retro_box_entry_unit():
    plan = (Path(__file__).resolve().parents[1] / "TEMPO_PLAN.md").read_text()
    unit = "19.P5.retro-box-activation"
    assert required_units(plan, "retro-box-activation", ()) == (unit,)
    for stem in ("retro-box-activation", "phase5-continue"):
        assert required_units(plan, stem, [unit, unit]).count(unit) == 1
    assert resolve_plan_contract(plan, [unit, unit]).count(f"### {unit} ") == 1
    assert entry_unit_gap(plan, unit) is None


@pytest.mark.asyncio
async def test_completed_retro_proposals_reach_prompt_spec_merge(tmp_path, monkeypatch):
    root, box = root_with_box(tmp_path)
    c = pause_checkout(root)
    c = replace(c, config=load_config(None, cwd=root), control=build_control(c))
    for path in PROPOSAL["proposed_spec_paths"]:
        c.fs.write(root / path, b"fixture spec before\n")
    await c.git.add(root, PROPOSAL["proposed_spec_paths"])
    await c.git.commit(root, "fixture specs")
    other = {**PROPOSAL, "proposed_spec_paths": list(reversed(PROPOSAL["proposed_spec_paths"]))}
    authored = json.loads(author_reply("repair-spec"))
    authored["ticket"] = authored["ticket"].replace("`tempo/thing.py`", "`specs/review.md`").replace(
        "## Scope fence\n- tempo/thing.py", "## Scope fence\n- specs/")
    authored["ticket"] = authored["ticket"].replace("uv run pytest", "test -f specs/review.md")
    llm = FakeLLM([json.dumps({"proposals": [PROPOSAL, PROPOSAL, other]}), reply("author"),
                   json.dumps(authored), requisition_reply("approve"), reply("decision")])
    writer = runner.bind(c, llm)
    hook = Retro(c, writer)
    c.journal.append(EventType.STATE_TRANSITION, {"to": "merged", "commit": None}, ticket="history")
    window = project_window(c.journal.read())
    c.journal.append(EventType.EFFECT_INTENT, {}, key="retro/1")
    await hook.emit_proposals("retro/1")
    assert box.messages() == []
    append = c.journal.append
    def appended(type_, body, **kwargs):
        if body.get("kind") == "storm_occurrence":
            assert any(e.type == EventType.EFFECT_COMPLETION and e.key == "retro/1" for e in c.journal.read())
        return append(type_, body, **kwargs)
    monkeypatch.setattr(c.journal, "append", appended)
    receipt = await hook(forced=True)
    assert receipt["commit"] == await c.git.rev_parse(root, "HEAD")
    messages = box.messages()
    assert len(messages) == 2
    for message, proposal in zip(messages, [PROPOSAL, other], strict=True):
        canonical = json.dumps(proposal, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        pid = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        assert message.origin == f"retro-proposal/retro/1/{pid}"
        assert message.retro_report_key == "retro/1"
        assert {key: getattr(message, key) for key in proposal} == proposal
        occurrence, = [e for e in c.journal.read() if e.body.get("kind") == "storm_occurrence"
                       and e.body["signature"] == message.signature]
        assert occurrence.body == {"kind": "storm_occurrence", "signature": message.signature,
            "occurrence_id": arrival_id("retro-proposal", "retro/1", pid),
            "emitting_stage": None, "emitting_origin": message.origin}
    history = c.journal.read()
    await hook.commit_report(writer.stages.driver, window, "must not replace the receipt")
    assert await hook(forced=True) is None
    assert c.journal.read() == history and box.messages() == messages
    await triage.triage_pass(c, llm)
    assert box.get(messages[0].id).resolution == Resolution(kind="ticket", link="repair-spec")
    text = (root / "tickets/repair-spec/ticket.md").read_text()
    ticket = validate_ticket("repair-spec", text, root)
    assert ticket.frontmatter.source == "box:retro_finding" and ticket.frontmatter.state == "draft"
    bridge, = [e for e in c.journal.read() if e.body.get("kind") == "retro_ticket_authored"]
    assert bridge.type == EventType.SIGNAL and bridge.key == "retro-ticket/repair-spec"
    assert bridge.ticket == "repair-spec" and bridge.body == {"kind": "retro_ticket_authored",
        "box_id": messages[0].id, "retro_report_key": "retro/1", "ticket_stem": "repair-spec"}
    commit, = [e for e in c.journal.read() if e.type == EventType.EFFECT_COMPLETION
               and e.key == "ticket-plane/repair-spec/author"]
    assert c.journal.read().index(commit) < c.journal.read().index(bridge)
    for surface in ("triage", "author"):
        prompt = next(r.rendered for r in llm.requests if r.surface == surface)
        for value in (PROPOSAL["fixed_failure"], PROPOSAL["overcorrection_risk"], "retro/1"):
            assert value in prompt or json.dumps(value)[1:-1] in prompt
    before = c.journal.read()
    record_retro_ticket(c.journal, messages[0], ticket.stem)
    assert c.journal.read() == before
    with pytest.raises(BoxError, match="restore the Author-produced bridge"):
        record_retro_ticket(c.journal, messages[1], ticket.stem)
    await runner.verdict(ticket.stem, c, kill=False)
    implementation = FakeLLM([implement_reply(), verdict()])
    call = implementation.call
    async def scripted(request):
        if request.surface == "implement":
            for path in PROPOSAL["proposed_spec_paths"]:
                c.fs.write(request.worktree / path, b"fixture spec repaired\n")
            await c.git.add(request.worktree, PROPOSAL["proposed_spec_paths"])
            await c.git.commit(request.worktree, "repair the observed prompt failure")
        return await call(request)
    monkeypatch.setattr(implementation, "call", scripted)
    admitted = runner.bind(c, implementation)
    assert await runner.run_ticket(ticket.stem, c, admitted) == 0
    sha = await c.git.rev_parse(root, "main")
    signal, = [e for e in c.journal.read() if e.body.get("kind") == "retro_prompt_spec_change_merged"]
    assert signal.type == EventType.SIGNAL and signal.ticket == ticket.stem
    assert signal.key == f"retro-prompt-spec-change/{ticket.stem}/{sha}"
    assert signal.body == {"kind": "retro_prompt_spec_change_merged", "box_id": messages[0].id,
        "retro_report_key": "retro/1", "ticket_stem": ticket.stem, "squash_sha": sha,
        "changed_spec_paths": sorted(PROPOSAL["proposed_spec_paths"])}
    assert (await c.git._run(root, "show", "--name-only", "--format=", sha)).splitlines() == sorted(PROPOSAL["proposed_spec_paths"])
    ctx = admitted.ctx
    ctx.driver.journal = Journal(c.config.state_dir, c.clock)
    ctx.driver.effects = Effects(ctx.driver.journal)
    history = ctx.driver.journal.read()
    reviewed = next(e.body["reviewed_sha"] for e in history if e.type == EventType.STATE_TRANSITION
                    and e.ticket == ticket.stem and e.body.get("to") == "merged")
    replay = await write_squash(ctx, ticket, reviewed, attempt=0)
    assert replay.commit == sha and await c.git.rev_parse(root, "main") == sha
    assert ctx.driver.journal.read() == history
