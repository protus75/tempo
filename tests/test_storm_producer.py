"""Direct dormant arrival wiring and preservation of the production composition."""

import json
from dataclasses import asdict
from unittest.mock import Mock

import pytest

from tempo import __main__ as cli, control, daemon, journal as journal_module, runner
from tempo.box import Box, BoxError, Resolution, Verdict
from tempo.effects import Effects
from tempo.journal import EventType, Journal, JournalCorruption
from tempo.llm import FakeLLM
from tempo.seams import LocalFileSystem
from tempo.storm import StormLedger
from tests.test_daemon_composition import CoreRig
from tests.test_journal import FakeClock, T0
from tests.test_mergequeue import ctx as admission_context  # noqa: F401
from tests.test_stages import STEM, TICKET, agent, verdict
from tests.test_storm import SIG, snapshot
from tempo import storm

ARRIVAL = dict(message_class="failure_report", origin="one", summary="failed path/one 123",
               stage="implement", outcome="gate_failed")


def compose(state, clock=None, fs=None):
    clock = clock or FakeClock(T0)
    journal = Journal(state, clock)
    box = storm.storm_producer(root=state / "box", fs=fs or LocalFileSystem(),
                                journal=journal, clock=clock)
    return box, journal, clock


def test_new_and_dedup_arrivals_record_occurrences(tmp_path, monkeypatch):
    order = []

    class OrderedFS(LocalFileSystem):
        def write(self, path, data):
            order.append("queue")
            assert len(journal.read()) == len(calls)
            assert journal.read()[-1].body == dict(kind="storm_occurrence", **calls[-1])
            super().write(path, data)

    box, journal, clock = compose(tmp_path, fs=OrderedFS())
    original = StormLedger.record
    calls = []

    def arrival(self, **kwargs):
        calls.append(kwargs)
        order.append("occurrence")
        return original(self, **kwargs)

    # Recompose so the bound callback is the observed predecessor writer.
    monkeypatch.setattr(StormLedger, "record", arrival)
    box = storm.storm_producer(root=box.root, fs=box.fs, journal=journal, clock=clock)
    first = box.enqueue(**ARRIVAL, occurrence_id="producer/1")
    assert first == (f"box-000001-{SIG[:8]}", True)
    before = snapshot(box.root)
    assert box.enqueue(**ARRIVAL, occurrence_id="producer/2") == (first[0], False)
    assert snapshot(box.root) == before and order == ["occurrence", "queue", "occurrence"]
    assert calls == [dict(signature=SIG, occurrence_id=f"producer/{n}",
                          emitting_stage="implement", emitting_origin="one") for n in (1, 2)]
    for n, event in enumerate(journal.read(), 1):
        assert asdict(event) == dict(v=1, type="signal", ts=T0.isoformat(), ticket=None,
            key=f"storm-occurrence/{SIG}/producer/{n}",
            body=dict(kind="storm_occurrence", **calls[n - 1]))
    message = box.get(first[0])
    assert message.seq == 1 and message.status == "pending"
    assert message.verdict is message.resolution is None
    assert [p.name for p in box.root.iterdir()] == [f"000001-{SIG[:8]}.json"]
    second, created = box.enqueue(**{**ARRIVAL, "origin": "two"}, occurrence_id="producer/3")
    assert created and box.get(second).seq == 2 and len(journal.read()) == 3


def test_arrival_replay_survives_restart_and_roll(tmp_path, monkeypatch):
    box, journal, clock = compose(tmp_path)
    id, _ = box.enqueue(**ARRIVAL, occurrence_id="stable")
    first = journal.read()[0]
    clock.advance(1)
    assert box.enqueue(**ARRIVAL, occurrence_id="stable") == (id, False)
    monkeypatch.setattr(journal_module, "ROLL_BYTES", 1)
    box.enqueue(**ARRIVAL, occurrence_id="second")
    clock.advance(3601)
    box.enqueue(**ARRIVAL, occurrence_id="third")
    box, journal, _ = compose(tmp_path, clock)
    before = snapshot(tmp_path)
    assert box.enqueue(**ARRIVAL, occurrence_id="stable") == (id, False)
    assert snapshot(tmp_path) == before and journal.read()[0] == first
    assert len(list(journal.dir.glob("*.jsonl"))) == 3
    ledger = StormLedger(journal=journal, clock=clock)
    assert ledger.count(SIG) == 1 and len(journal.read()) == 3
    # Corrupt replay identity, while leaving the queue and original event untouched.
    journal.append(EventType.SIGNAL, {**first.body, "emitting_origin": "different"}, key=first.key)
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match="fresh id.*repair the producing evidence"):
        box.enqueue(**ARRIVAL, occurrence_id="stable")
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("incoming", [
    ARRIVAL,
    dict(message_class="suggestion", origin="bootstrap-ingest", summary="suggestion"),
    dict(message_class="bug_report", origin="host-app", summary="crash",
         bug_origin="self_diagnosed", has_repro=True),
])
def test_dedup_preserves_resolved_message_and_incoming_identity(tmp_path, incoming):
    box, journal, clock = compose(tmp_path)
    id, _ = box.enqueue(**incoming, occurrence_id="first")
    message = box.get(id)
    # Stored diagnostic fields are not the arriving producer's identity.
    box._write(message.model_copy(update={"stage": "check", "outcome": "gate_failed",
                                          "origin": "stored", "summary": "old diagnostic"}))
    verdict = Verdict(verdict="decision", produced_by_spec_version="1", rationale="recorded")
    resolution = Resolution(kind="decision", link="known")
    box.record_verdict(id, verdict)
    box.resolve(id, resolution)
    before = snapshot(box.root)
    assert box.enqueue(**{**incoming, "summary": "new diagnostic", "reason": incoming["summary"]},
                       occurrence_id="second") == (id, False)
    assert snapshot(box.root) == before
    assert box.get(id).verdict == verdict and box.get(id).resolution == resolution
    assert journal.read()[-1].body == dict(kind="storm_occurrence", signature=message.signature,
        occurrence_id="second", emitting_stage=incoming.get("stage"), emitting_origin=incoming["origin"])
    assert StormLedger(journal=journal, clock=clock).count(message.signature) == 2


@pytest.mark.parametrize("damage", ["missing", "blank", "non-string", "pair", "class", "summary",
                                    "queue", "read", "callback", "ledger", "journal", "append"])
def test_invalid_arrivals_and_callback_failures_do_not_publish(tmp_path, monkeypatch, damage):
    box, journal, _ = compose(tmp_path)
    kwargs = dict(ARRIVAL, occurrence_id="new")
    callback = Mock(wraps=box._arrival)
    box._arrival = callback
    expected = BoxError
    if damage == "missing":
        del kwargs["occurrence_id"]
    elif damage == "blank":
        kwargs["occurrence_id"] = " \t\n"
    elif damage == "non-string":
        kwargs["occurrence_id"] = 1
    elif damage == "pair":
        kwargs["outcome"] = None
    elif damage == "class":
        kwargs["message_class"] = "bug_report"
    elif damage == "summary":
        kwargs["summary"] = " "
    elif damage == "queue":
        box.root.mkdir()
        (box.root / "bad.json").write_text("{}")
    elif damage == "read":
        monkeypatch.setattr(box, "messages", Mock(side_effect=OSError("read failure")))
        expected = OSError
    elif damage == "callback":
        callback.side_effect = OSError("callback failure")
        expected = OSError
    elif damage == "ledger":
        journal.append(EventType.SIGNAL, dict(kind="storm_occurrence"))
        expected = ValueError
    elif damage == "journal":
        journal.append(EventType.SIGNAL, dict(kind="handoff_written"))
        next(journal.dir.glob("*.jsonl")).write_bytes(b"{}\n")
        expected = JournalCorruption
    else:
        monkeypatch.setattr(journal, "append", Mock(side_effect=OSError("append failure")))
        expected = OSError
    before = snapshot(tmp_path)
    with pytest.raises(expected):
        box.enqueue(**kwargs)
    assert snapshot(tmp_path) == before
    assert callback.call_count == (1 if damage in {"callback", "ledger", "journal", "append"} else 0)


@pytest.mark.parametrize("when", ["before-occurrence", "after-occurrence", "before-queue", "after-queue"])
def test_enqueue_crash_replay_finishes_once(tmp_path, monkeypatch, when):
    class Crash(BaseException):
        pass

    box, journal, clock = compose(tmp_path)
    append, write = journal.append, box.fs.write

    def crash(*args, **kwargs):
        if when == "after-occurrence":
            append(*args, **kwargs)
        elif when == "after-queue":
            write(*args, **kwargs)
        raise Crash()

    with monkeypatch.context() as patch:
        patch.setattr(journal if "occurrence" in when else box.fs,
                      "append" if "occurrence" in when else "write", crash)
        with pytest.raises(Crash):
            box.enqueue(**ARRIVAL, occurrence_id="stable")
    assert len(journal.read()) == (0 if when == "before-occurrence" else 1)
    assert len(box.messages()) == (1 if when == "after-queue" else 0)
    clock.advance(5)
    box, journal, _ = compose(tmp_path, clock)
    id, created = box.enqueue(**ARRIVAL, occurrence_id="stable")
    assert created == (when != "after-queue")
    assert len(box.messages()) == len(journal.read()) == 1
    assert journal.read()[0].ts == (clock().isoformat() if when == "before-occurrence" else T0.isoformat())
    before = snapshot(tmp_path)
    assert box.enqueue(**ARRIVAL, occurrence_id="stable") == (id, False)
    assert snapshot(tmp_path) == before


def test_box_operations_without_arrivals_are_idle(tmp_path, monkeypatch):
    clock = Mock(return_value=T0)
    journal = Journal(tmp_path, clock)
    read, append = Mock(wraps=journal.read), Mock(wraps=journal.append)
    monkeypatch.setattr(journal, "read", read)
    monkeypatch.setattr(journal, "append", append)
    fs = Mock(wraps=LocalFileSystem())
    box = storm.storm_producer(root=tmp_path / "box", fs=fs, journal=journal, clock=clock)
    assert box.root == tmp_path / "box" and box.fs is fs
    read.assert_not_called()
    append.assert_not_called()
    clock.assert_not_called()
    assert snapshot(tmp_path) == {} and fs.mock_calls == []
    unbound = Box(box.root, fs)
    id, _ = unbound.enqueue(**ARRIVAL)
    assert unbound.enqueue(**ARRIVAL) == (id, False)
    callback = Mock(side_effect=AssertionError("non-arrival called hook"))
    box._arrival = callback
    assert len(box.messages()) == len(box.pending()) == 1 and box.get(id).id == id
    box.record_verdict(id, Verdict(verdict="decision", produced_by_spec_version="1", rationale="known"))
    box.resolve(id, Resolution(kind="decision", link="known"))
    assert box.pending() == []
    callback.assert_not_called()
    read.assert_not_called()
    append.assert_not_called()
    clock.assert_not_called()


def assert_production_reachable(tmp_path, monkeypatch):
    hook = Mock(wraps=storm.storm_producer)
    monkeypatch.setattr(storm, "storm_producer", hook)
    rig = CoreRig(tmp_path)
    hook.assert_called_once()
    assert rig.journal.read() == [] and rig.exec.calls == []
    return hook


@pytest.mark.parametrize("verb", ["run"])
def test_storm_producer_hook_is_active(tmp_path, monkeypatch, admission_context, verb):
    composition = tmp_path / "composition"
    composition.mkdir()
    hook = assert_production_reachable(composition, monkeypatch)
    source = admission_context
    source.fs.write(source.repo / f"tickets/{STEM}/ticket.md", TICKET.format(bypass="").encode())
    implement = agent({"tempo/thing.py": "ok\n"})

    def report(request):
        reply = json.loads(implement(request).removeprefix("```json\n").removesuffix("\n```"))
        reply["second_problems"] = [{"summary": "ordinary production arrival"}]
        return "```json\n" + json.dumps(reply) + "\n```"

    llm = FakeLLM([report, verdict(), '{"proposals": []}'])  # drain: the quiescence retro follows the merge
    assert cli.main([verb, STEM] if verb == "run" else [verb], cwd=source.repo, env=source.env,
                    clock=source.driver.clock, pipeline=lambda c: runner.bind(c, llm)) == 0
    messages = Box(source.config.state_dir / "box", source.fs).messages()
    assert len(messages) == 1 and messages[0].summary == "ordinary production arrival"
    occurrences = [e for e in source.driver.journal.read() if e.body.get("kind") == "storm_occurrence"]
    assert len(occurrences) == 1
    assert occurrences[0].body["emitting_stage"] == "implement"
    assert occurrences[0].body["emitting_origin"] == STEM
    assert hook.call_count > 1

    def forbidden(*args, **kwargs):
        pytest.fail("arrival producer invoked notify/dispatch")

    monkeypatch.setattr(Effects, "run", forbidden)
    monkeypatch.setattr(control, "publish_request", forbidden)
    monkeypatch.setattr(daemon.DaemonAdmission, "dispatch", forbidden)
    box, journal, clock = compose(tmp_path / "direct")
    for n in range(12):
        box.enqueue(**ARRIVAL, occurrence_id=str(n))
    assert list(StormLedger(journal=journal, clock=clock).holds().values()) == ["one"]
    assert len(box.messages()) == 2 and StormLedger(journal=journal, clock=clock).count(SIG) == 12
    assert len([e for e in journal.read() if e.body.get("kind") == "storm_breaker_trip"]) == 1
    assert len([e for e in journal.read() if e.body.get("kind") == "storm_occurrence"]) == 13
    assert set(p.name for p in (tmp_path / "direct").iterdir()) == {"box", "journal"}
