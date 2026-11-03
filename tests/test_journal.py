import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from tempo import journal as journal_mod
from tempo.journal import Event, Journal, JournalCorruption


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


T0 = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)


def line(type_: str, n: int, ts: str = "2026-08-04T12:00:00+00:00") -> str:
    return json.dumps(
        {"v": 1, "type": type_, "ts": ts, "ticket": None, "key": None, "body": {"n": n}}
    )


def seed(state_dir, name: str, *lines: str) -> None:
    d = state_dir / "journal"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("".join(l + "\n" for l in lines))


def test_append_round_trips_envelope(tmp_path):
    j = Journal(tmp_path, FakeClock(T0))
    ev = j.append("signal", {"kind": "handoff_written"}, ticket="t-1", key="k-1")
    assert ev == Event(
        v=1,
        type="signal",
        ts="2026-08-04T12:00:00+00:00",
        ticket="t-1",
        key="k-1",
        body={"kind": "handoff_written"},
    )
    assert j.read() == [ev]


def test_first_append_creates_ordered_segment_under_state_dir(tmp_path):
    Journal(tmp_path, FakeClock(T0)).append("signal", {"signal": "handoff_written"})
    assert [p.name for p in (tmp_path / "journal").iterdir()] == ["000001-20260804.jsonl"]


def test_on_disk_line_has_fixed_envelope_shape(tmp_path):
    Journal(tmp_path, FakeClock(T0)).append("state_transition", {"to": "running"}, ticket="t")
    raw = (tmp_path / "journal" / "000001-20260804.jsonl").read_text()
    assert raw.endswith("\n") and raw.count("\n") == 1
    assert list(json.loads(raw)) == ["v", "type", "ts", "ticket", "key", "body"]


def test_ts_is_pinned_aware_utc_isoformat_and_sorts_chronologically(tmp_path):
    clock = FakeClock(T0)
    j = Journal(tmp_path, clock)
    j.append("signal", {"signal": "handoff_written"})
    clock.advance(0.5)
    j.append("signal", {"signal": "handoff_written"})
    clock.advance(0.5)
    j.append("signal", {"signal": "handoff_written"})
    stamps = [e.ts for e in j.read()]
    assert stamps == [
        "2026-08-04T12:00:00+00:00",
        "2026-08-04T12:00:00.500000+00:00",
        "2026-08-04T12:00:01+00:00",
    ]
    assert stamps == sorted(stamps)


def test_non_utc_aware_clock_is_rendered_as_utc(tmp_path):
    plus2 = timezone(timedelta(hours=2))
    j = Journal(tmp_path, lambda: datetime(2026, 8, 4, 14, 0, tzinfo=plus2))
    assert j.append("signal", {"signal": "handoff_written"}).ts == "2026-08-04T12:00:00+00:00"


def test_naive_clock_is_refused(tmp_path):
    j = Journal(tmp_path, lambda: datetime(2026, 8, 4, 12, 0))
    with pytest.raises(ValueError, match="aware"):
        j.append("signal", {"signal": "handoff_written"})
    assert not (tmp_path / "journal").exists()


def test_reads_every_event_in_order_across_multiple_preseeded_segments(tmp_path):
    # Created out of order on disk; 000010 must sort after 000002 (zero-padded).
    seed(tmp_path, "000010-20260806.jsonl", line("signal", 5), line("signal", 6))
    seed(tmp_path, "000001-20260804.jsonl", line("signal", 1), line("signal", 2))
    seed(tmp_path, "000002-20260805.jsonl", line("timer_armed", 3), line("timer_fired", 4))
    j = Journal(tmp_path, FakeClock(T0))
    assert [e.body["n"] for e in j.read()] == [1, 2, 3, 4, 5, 6]
    assert [[e.body["n"] for e in seg] for seg in j.read_segments()] == [[1, 2], [3, 4], [5, 6]]
    assert all(isinstance(seg, tuple) for seg in j.read_segments())


def test_append_goes_to_newest_segment_without_rolling(tmp_path):
    seed(tmp_path, "000001-20260801.jsonl", line("signal", 1))
    seed(tmp_path, "000002-20260802.jsonl", line("signal", 2))
    j = Journal(tmp_path, FakeClock(T0))
    j.append("signal", {"signal": "handoff_written", "n": 3})
    names = sorted(p.name for p in (tmp_path / "journal").iterdir())
    assert names == ["000001-20260801.jsonl", "000002-20260802.jsonl"]
    assert [[e.body["n"] for e in seg] for seg in j.read_segments()] == [[1], [2, 3]]


def test_missing_journal_reads_empty(tmp_path):
    j = Journal(tmp_path, FakeClock(T0))
    assert j.read() == []
    assert list(j.read_segments()) == []


def test_torn_final_line_is_skipped_and_rest_still_reads(tmp_path):
    j = Journal(tmp_path, FakeClock(T0))
    for n in range(3):
        j.append("signal", {"signal": "handoff_written", "n": n})
    seg = tmp_path / "journal" / "000001-20260804.jsonl"
    with seg.open("ab") as f:
        f.write(b'{"v": 1, "type": "sig')
    assert [e.body["n"] for e in j.read()] == [0, 1, 2]


def test_unterminated_final_line_is_torn_even_if_parseable(tmp_path):
    seg_dir = tmp_path / "journal"
    seg_dir.mkdir()
    (seg_dir / "000001-20260804.jsonl").write_text(line("signal", 1) + "\n" + line("signal", 2))
    assert [e.body["n"] for e in Journal(tmp_path, FakeClock(T0)).read()] == [1]


def test_writer_truncates_torn_tail_before_appending(tmp_path):
    seg = tmp_path / "journal" / "000001-20260804.jsonl"
    seed(tmp_path, seg.name, line("signal", 1))
    with seg.open("ab") as f:
        f.write(b'{"v": 1, "ty')
    j = Journal(tmp_path, FakeClock(T0))
    j.append("signal", {"signal": "handoff_written", "n": 2})
    assert [e.body["n"] for e in j.read()] == [1, 2]
    assert all(json.loads(l) for l in seg.read_text().splitlines())


def test_torn_tail_in_a_rolled_segment_is_corruption(tmp_path):
    seed(tmp_path, "000001-20260804.jsonl", line("signal", 1))
    with (tmp_path / "journal" / "000001-20260804.jsonl").open("a") as f:
        f.write('{"v": 1, "ty')
    seed(tmp_path, "000002-20260805.jsonl", line("signal", 2))
    with pytest.raises(JournalCorruption, match="000001-20260804.jsonl"):
        Journal(tmp_path, FakeClock(T0)).read()


def test_malformed_mid_segment_line_is_corruption(tmp_path):
    seed(tmp_path, "000001-20260804.jsonl", line("signal", 1), "{not json", line("signal", 2))
    with pytest.raises(JournalCorruption, match="line 2"):
        Journal(tmp_path, FakeClock(T0)).read()


def test_malformed_terminated_final_line_is_corruption(tmp_path):
    seed(tmp_path, "000001-20260804.jsonl", line("signal", 1), "{not json")
    with pytest.raises(JournalCorruption):
        Journal(tmp_path, FakeClock(T0)).read()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.update(type="mystery"),
        lambda e: e.update(v=2),
        lambda e: e.update(v="1"),
        lambda e: e.pop("key"),
        lambda e: e.update(extra=1),
        lambda e: e.update(body=[]),
        lambda e: e.update(ticket=7),
        lambda e: e.update(ts="2026-08-04T12:00:00"),
    ],
    ids=["unknown-type", "newer-v", "str-v", "missing-key", "extra-field", "list-body", "int-ticket", "naive-ts"],
)
def test_envelope_violations_are_corruption(tmp_path, mutate):
    ev = json.loads(line("signal", 1))
    mutate(ev)
    seed(tmp_path, "000001-20260804.jsonl", json.dumps(ev))
    with pytest.raises(JournalCorruption):
        Journal(tmp_path, FakeClock(T0)).read()


def test_unrecognized_file_in_journal_dir_is_corruption(tmp_path):
    seed(tmp_path, "000001-20260804.jsonl", line("signal", 1))
    seed(tmp_path, "notes.jsonl", line("signal", 2))
    with pytest.raises(JournalCorruption, match="notes.jsonl"):
        Journal(tmp_path, FakeClock(T0)).read()


def test_duplicate_segment_sequence_is_corruption(tmp_path):
    seed(tmp_path, "000001-20260804.jsonl", line("signal", 1))
    seed(tmp_path, "000001-20260805.jsonl", line("signal", 2))
    with pytest.raises(JournalCorruption, match="000001"):
        Journal(tmp_path, FakeClock(T0)).read()


@pytest.mark.parametrize("type_", ["mystery", "checkpoint"])
def test_writer_refuses_types_outside_the_emittable_set(tmp_path, type_):
    with pytest.raises(ValueError, match=type_):
        Journal(tmp_path, FakeClock(T0)).append(type_, {})


def test_writer_refuses_non_object_body(tmp_path):
    with pytest.raises(ValueError, match="body"):
        Journal(tmp_path, FakeClock(T0)).append("signal", ["x"])


@pytest.mark.parametrize("field", ["ticket", "key"])
@pytest.mark.parametrize("value", [1, ["t"]])
def test_writer_refuses_non_string_ticket_or_key(tmp_path, field, value):
    j = Journal(tmp_path, FakeClock(T0))
    with pytest.raises(ValueError, match=field):
        j.append("signal", {"signal": "handoff_written"}, **{field: value})
    assert not (tmp_path / "journal").exists()

    j.append("signal", {"signal": "handoff_written", "n": 1})
    before = j.read()
    with pytest.raises(ValueError, match=field):
        j.append("signal", {"signal": "handoff_written"}, **{field: value})
    assert j.read() == before


def test_append_fsyncs_before_returning(tmp_path, monkeypatch):
    seg = tmp_path / "journal" / "000001-20260804.jsonl"
    synced: list[str] = []
    real_fsync = journal_mod.os.fsync

    def recording_fsync(fd):
        real_fsync(fd)
        synced.append(seg.read_text() if seg.exists() else "")

    monkeypatch.setattr(journal_mod.os, "fsync", recording_fsync)
    Journal(tmp_path, FakeClock(T0)).append("effect_intent", {"n": 1}, key="k")
    assert any('"effect_intent"' in text for text in synced)


def test_run_sequence_counts_mid_run_drought_but_not_runless_parks(tmp_path):
    from tempo.journal import EventType, run_seq
    journal = Journal(tmp_path, FakeClock(T0))
    park = {'to': 'infra_error', 'reason': 'provider_drought',
            'provider_drought': {'tier': 'high', 'surface': 'implement', 'providers': ['claude']}}
    journal.append(EventType.STATE_TRANSITION, park, ticket='work')
    assert run_seq(journal.read(), 'work') == 0
    journal.append(EventType.STATE_TRANSITION, {'to': 'running'}, ticket='work')
    journal.append(EventType.STATE_TRANSITION, park, ticket='work')
    journal.append(EventType.STATE_TRANSITION, park, ticket='work')
    assert run_seq(journal.read(), 'work') == 1
    journal.append(EventType.STATE_TRANSITION, {'to': 'running'}, ticket='work')
    journal.append(EventType.STATE_TRANSITION, {'to': 'abandoned'}, ticket='work')
    assert run_seq(journal.read(), 'work') == 2
