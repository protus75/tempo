"""Status surfaces confirmed policy classes using write-free queue and journal reads."""

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from tempo import __main__ as cli
from tempo.__main__ import _status, main
from tempo.box import Box, Resolution
from tempo.storm import storm_producer
from tempo.git import Git
from tempo.journal import EventType, Journal
from tempo.llm import FakeLLM
from tempo.lockfile import Lockfile
from tempo.mergequeue import MergeQueue
from tempo.retro import Retro, RetroArtifact, measurements, production_evidence, project_window, render_report
from tempo.seams import LocalFileSystem, SubprocessExec
from tempo.specs import load_spec
from tempo.status import project, render
from tests.test_cli import ENV
from tests.support import Clock, make_root
from tests.support import PROPOSAL
from tests.test_retro import completed, merged, rig
from tests.test_scorecard import invoice, review, rows
from tempo.checkout import SPECS_DIR


def test_status_surfaces_confirmed_class_tombstones(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    clock = Clock()
    journal = Journal(root / ".tempo/state", clock)
    fs = LocalFileSystem()
    box = storm_producer(root=root / ".tempo/state/box", fs=fs, journal=journal, clock=clock)
    ids = []
    for cls in ("failure_report", "retro_finding", "suggestion"):
        evidence = {**PROPOSAL, "retro_report_key": "retro/1"} if cls == "retro_finding" else {}
        id, _ = box.enqueue(message_class=cls, origin=cls, summary="observed concern", occurrence_id=cls, **evidence)
        box.resolve(id, Resolution(kind="tombstone", link=f"tombstone-{id}"))
        ids.append(id)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    def forbidden(*args, **kwargs):
        raise AssertionError("status tried to write")
    monkeypatch.setattr(LocalFileSystem, "write", forbidden)
    monkeypatch.setattr(Journal, "append", forbidden)
    git = Git(SubprocessExec(), env=ENV, timeout=30)
    provenance = {"head": asyncio.run(git.rev_parse(root, "HEAD")),
                  "spec_version": load_spec((SPECS_DIR / "retro.md").read_text()).meta.version,
                  "at": clock().isoformat()}
    status = project(journal.read(), box=box, provenance=provenance)
    assert status.tombstones == tuple((m.id, m.message_class, 0) for m in box.messages()[:2])
    output = render(status)
    assert all(id in output for id in ids[:2]) and ids[2] not in output
    assert main(["status"], cwd=root, env=ENV, clock=clock) == 0
    assert capsys.readouterr().out == output
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    monkeypatch.undo()
    for n in range(3):
        box.record_rereport(ids[0], occurrence_id=f"fresh/{n}")
    before = journal.read()
    current = project(before, box=Box(box.root, fs))
    assert current.tombstones == ((ids[1], "retro_finding", 0),)
    assert current.reopens == ({"kind": "tombstone_auto_reopened", "box_id": ids[0],
                                "signature": box.get(ids[0]).signature, "reports": 3},)
    assert ids[0] in render(current) and journal.read() == before


def test_status_current_window_scorecard(tmp_path):
    c, writer, _, _ = rig(tmp_path)
    asyncio.run(invoice(c, writer, "old"))
    asyncio.run(review(c, writer, "old", "approve"))
    merged(c.journal, "old")
    c.journal.append(EventType.EFFECT_INTENT, {}, key="retro/1")
    initial = asyncio.run(_status(c))
    assert initial.scorecard.boundary == "initial"
    assert rows(initial.scorecard)["scope_fence"].catches == 1
    completed(c.journal, 1)
    for n in range(25):
        inv = asyncio.run(invoice(c, writer, f"current-{n}", bypass=True))
        assert inv.passed and "scope_fence" in inv.bypassed
        if n == 23:
            assert not rows(asyncio.run(_status(c)).scorecard)["scope_fence"].prune_candidate
    completion = next(e for e in reversed(c.journal.read())
                      if e.type == EventType.EFFECT_COMPLETION and (e.key or "").endswith("/checks"))
    c.journal.append(completion.type, completion.body, ticket=completion.ticket, key=completion.key)
    c.journal.append(EventType.EFFECT_INTENT, {}, ticket="unfinished", key="ticket-plane/unfinished/0/checks")
    c.fs.write(c.repo / "tickets/current-24/checks.json", b"mutable bytes are not evidence")
    asyncio.run(review(c, writer, "current-24", "snag"))
    asyncio.run(review(c, writer, "current-24", "approve", attempt=1))
    head = asyncio.run(c.git.rev_parse(c.repo, "HEAD"))
    merged(c.journal, "current-24", head)
    merged(c.journal, "current-24", head)
    c.journal.append(EventType.SIGNAL, {"signal": "rework_order", "attempt": 1}, ticket="current-24")
    MergeQueue(writer.ctx, escalate=lambda event: None, control=None)._facts("current-24", set(), set(), red=["red.py"])
    c.journal.append(EventType.STATE_TRANSITION, {"to": "gate_failed", "reason": "diff_budget"}, ticket="failed")
    c.journal.append(EventType.EFFECT_INTENT, {}, key="retro/2")
    status = asyncio.run(_status(c))
    card = status.scorecard
    assert card.boundary == "retro/1" and card.merged_ticket_count == 1
    assert card.signal_counts == {"override": 1, "rework": 1, "gate-failure": 1, "integration-red": 1}
    assert card.gate_failure_count == 1
    result = rows(card)
    assert list(result) == sorted(result) and "unfinished" not in result
    assert result["scope_fence"].evaluated_tickets == result["scope_fence"].bypass_count == 25
    assert result["scope_fence"].prune_candidate and result["scope_fence"].catch_rate == 0
    assert result["review"].catches == 1 and result["review"].catch_rate == 1
    assert result["diff_budget"].catches == 1
    assert all(row.escapes == row.escape_rate == 0 for row in card.surfaces)
    window = project_window(c.journal.read())
    evidence = asyncio.run(production_evidence(window, c))
    retro_text = render_report(window, evidence, dict(status.provenance), [], RetroArtifact(proposals=[]))
    assert render(status).split("## Surface scorecard\n")[1] == retro_text.split("## Surface scorecard\n\n")[1]
    with pytest.raises(FrozenInstanceError):
        status.scorecard = initial.scorecard
    with pytest.raises(TypeError):
        status.provenance["head"] = "changed"
    completed(c.journal, 2)
    c.journal.append(EventType.EFFECT_INTENT, {}, key="retro/3")
    empty = asyncio.run(_status(c)).scorecard
    assert empty.boundary == "retro/2" and empty.surfaces == ()
    assert empty.merged_ticket_count == empty.gate_failure_count == 0
    assert all(value == 0 for value in empty.signal_counts.values())
    assert render(project([])).endswith("| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |\n")


def test_status_production_provenance(tmp_path, monkeypatch, capsys):
    c, _, _, _ = rig(tmp_path)
    spec_text = (SPECS_DIR / "retro.md").read_text().replace('version: "1.2"', 'version: "7.3"')
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "retro.md").write_text(spec_text)
    monkeypatch.setattr(cli, "SPECS_DIR", specs)
    head = asyncio.run(c.git.rev_parse(c.repo, "HEAD"))
    expected = {"head": head, "spec_version": load_spec(spec_text).meta.version, "at": c.clock().isoformat()}
    assert expected["spec_version"] == "7.3"
    projected = []
    def capture(events, **kwargs):
        assert kwargs["provenance"] == expected
        assert kwargs["window"].evidence == {**measurements(project_window(events)), "check_observations": []}
        result = project(events, **kwargs)
        projected.append(result)
        return result
    monkeypatch.setattr(cli, "project", capture)
    assert main(["status"], cwd=c.repo, env=ENV, clock=c.clock) == 0
    output = capsys.readouterr().out
    assert output == render(projected[0])
    assert dict(projected[0].provenance) == expected
    for key, value in expected.items():
        assert f"- {key}: {value}\n" in output


@pytest.mark.parametrize("populated", [False, True])
def test_status_preserves_sections_and_is_write_free(tmp_path, monkeypatch, capsys, populated):
    from tempo.effects import Effects
    from tempo.seams import CommandNotifications

    c, writer, _, _ = rig(tmp_path)
    box = storm_producer(root=c.config.state_dir / "box", fs=c.fs, journal=c.journal, clock=c.clock)
    if populated:
        asyncio.run(invoice(c, writer, "checked"))
        merged(c.journal, "done")
        c.journal.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket="active")
        c.journal.append(EventType.STATE_TRANSITION,
            {"to": "gate_failed", "stage": "check", "routed": "reject_queue"}, ticket="held")
        c.journal.append(EventType.SIGNAL, {"signal": "ticket_intake", "source": "human", "state": "confirmed",
            "commit": "123456789abcdef"}, ticket="new")
        id, _ = box.enqueue(message_class="failure_report", origin="test", summary="visible", occurrence_id="original")
        box.resolve(id, Resolution(kind="tombstone", link="retired"))
        reopened, _ = box.enqueue(message_class="failure_report", origin="other", summary="reopened", occurrence_id="other")
        box.resolve(reopened, Resolution(kind="tombstone", link="other"))
        for n in range(3):
            box.record_rereport(reopened, occurrence_id=f"report/{n}")
        legacy = ("merged:\n- done\n\nin flight:\n- active\n\nblocked:\n- held: gate_failed\n\n"
            "intake:\n- new: source: human, state: confirmed, commit 123456789abc\n\n"
            "Reject queue:\n- held: gate_failed at check -- confirm held after editing, or reject held\n\n"
            f"Confirmed-class tombstones:\n- {id}: failure_report, 0 re-reports\n\n"
            f"Tombstone reopens:\n- {reopened}: reopened at 3 re-reports\n")
    else:
        legacy = ("merged:\n(none)\n\nin flight:\n(none)\n\nblocked:\n(none)\n\nintake:\n(none)\n\n"
            "Reject queue:\n(none)\n\nConfirmed-class tombstones:\n(none)\n\n"
            "Tombstone reopens:\n(none)\n")
    before = {p: p.read_bytes() for p in c.repo.rglob("*") if p.is_file()}
    def forbidden(*args, **kwargs):
        pytest.fail("status invoked a write or active engine seam")
    for name in ("write", "publish", "replace"):
        monkeypatch.setattr(LocalFileSystem, name, forbidden)
    monkeypatch.setattr(Journal, "append", forbidden)
    monkeypatch.setattr(Lockfile, "acquire", forbidden)
    monkeypatch.setattr(Effects, "run", forbidden)
    monkeypatch.setattr(FakeLLM, "call", forbidden)
    monkeypatch.setattr(CommandNotifications, "notify", forbidden)
    monkeypatch.setattr(Retro, "__call__", forbidden)
    for name in ("enqueue", "record_rereport", "record_verdict", "resolve", "recover"):
        monkeypatch.setattr(Box, name, forbidden)
    git_run = Git._run
    async def read_only(git, repo, *args):
        assert args[0] in {"show", "rev-parse"}
        return await git_run(git, repo, *args)
    monkeypatch.setattr(Git, "_run", read_only)
    assert main(["status"], cwd=c.repo, env=ENV, clock=c.clock) == 0
    first = capsys.readouterr().out
    assert first.split("\n\ncurrent-window scorecard:")[0] + "\n" == legacy
    assert main(["status"], cwd=c.repo, env=ENV, clock=c.clock) == 0
    assert capsys.readouterr().out == first
    assert {p: p.read_bytes() for p in c.repo.rglob("*") if p.is_file()} == before
