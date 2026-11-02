import ast
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from tempo.config import load_config
from tempo.effects import Effects
from tempo.journal import EventType, Journal
from tempo.llmeffect import llm_key
from tempo.providers import ProviderCallError, ProviderSetupError, Served
from tempo.seams import ExecutableNotFound
from tempo.thresh import (
    PROVIDER_CALL_OUTCOME,
    PROVIDER_CAP_WAIT,
    SPILL_WAIT_SECONDS,
    Admission,
    CallResult,
    Thresh,
    UnavailableRoute,
)

ROOT = Path(__file__).resolve().parents[1]


class Time:
    def __init__(self):
        self.now = datetime(2026, 10, 6, tzinfo=UTC)
        self.sleeps = []

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        assert seconds == 0  # queue notifications, never timed polling or expiry sleeps
        await asyncio.sleep(0)


class Rig:
    def __init__(self, path, *, cap=1, k=3):
        path.mkdir(parents=True, exist_ok=True)
        raw = {
            "schema_version": 1, "state_dir": "state",
            "providers": [{"name": name, "kind": "cli", "package": "fixture",
                           "models_by_tier": dict.fromkeys(("low", "medium", "high", "max"), name + "-model"),
                           "limits": {"concurrency": cap if name == "a" else 1,
                                      "est_cost_per_call_usd": 1.0, "quota_window_minutes": 60}}
                          for name in ("a", "b", "c")],
            "routing": [{"tier": "medium", "surface": "implement",
                         "candidates": [{"provider": "a"}, {"provider": "b", "model": "pinned"}, {"provider": "c"}]}],
            "review": {}, "merge": {}, "engine_plane_safety_inventory": [],
            "circuit_breaker": {"k": k, "cooldown_minutes": 10},
        }
        (path / "config.yaml").write_text(yaml.safe_dump(raw))
        self.config = load_config(None, cwd=path)
        self.time = Time()
        self.journal = Journal(self.config.state_dir, self.time)
        self.thresh = self.fresh()
        self.effects = Effects(self.journal)
        row = self.config.routing[0]
        providers = {p.name: p for p in self.config.providers}
        self.route = tuple(Served(providers[c.provider], c.model or providers[c.provider].models_by_tier.medium)
                           for c in row.candidates)
        self.calls = []

    def fresh(self):
        return Thresh(self.config, journal=self.journal, clock=self.time, sleep=self.time.sleep)

    def signals(self, signal):
        return [e for e in self.journal.read() if e.type == EventType.SIGNAL and e.body.get("signal") == signal]

    def record(self, name, failure, key="result", ticket="t"):
        self.thresh.record_outcome(name, failure, ticket=ticket, call_key=key)

    def open(self, name):
        for i in range(self.config.circuit_breaker.k):
            self.record(name, "outage", f"opening/{name}/{i}")

    async def admit(self, route=None, *, estimate=0, key="admission", ticket="t"):
        return await self.thresh.admit(self.route if route is None else route, ticket=ticket, call_key=key,
                                       projected_wait_seconds=estimate)

    async def call(self, key, *, route=None, estimate=0, gate=None, error=None, ticket="t", surface="implement"):
        call_key = llm_key(ticket or surface, 0, surface, 1, key)

        async def operation(served):
            self.calls.append((key, served))
            if gate is not None:
                await gate.wait()
            if error is not None:
                raise error
            return {"provider": served.provider.name, "model": served.model, "usd": 1.0}

        async def effect(action):
            return await self.effects.run(action, key=call_key, ticket=ticket, cost=lambda r: {"usd": r["usd"]})

        return await self.thresh.run(self.route if route is None else route, ticket=ticket, call_key=call_key,
                                     projected_wait_seconds=estimate, effect=effect, operation=operation)


async def settle():
    # Bound scheduler turns make lost notifications fail promptly without wall-clock timers.
    for _ in range(12):
        await asyncio.sleep(0)


def clean(rig):
    assert all(s.used == 0 and not s.queue for s in rig.thresh._slots.values())


def wait_body(name, key, started, seconds, disposition):
    return {"signal": PROVIDER_CAP_WAIT, "provider": name, "call_key": key,
            "started_at": started.isoformat(), "waited_seconds": seconds, "disposition": disposition}


def test_per_provider_concurrency_caps(tmp_path):
    async def scenario():
        r = Rig(tmp_path, cap=2)
        gates = [asyncio.Event() for _ in range(4)]
        first = asyncio.create_task(r.call(1, gate=gates[0], surface="implement"))
        second = asyncio.create_task(r.call(2, gate=gates[1], surface="review"))
        await settle()
        third = asyncio.create_task(r.call(3, gate=gates[2], surface="author", ticket=None))
        independent = asyncio.create_task(r.call(4, route=r.route[1:], gate=gates[3]))
        await settle()
        assert [k for k, _ in r.calls] == [1, 2, 4]
        assert r.thresh._slots["a"].used == 2 and r.thresh._slots["b"].used == 1
        assert len(r.thresh._slots["a"].queue) == 1
        assert len([e for e in r.journal.read() if e.type == EventType.EFFECT_INTENT]) == 3
        # The effect callback itself observes a reserved slot before Effects writes intent.
        probe = await r.admit(r.route[2:])
        r.thresh.release(probe)
        before = r.journal.read()

        async def effect(action):
            assert r.thresh._slots["c"].used == 1
            assert r.journal.read() == before
            return await r.effects.run(action, key="llm/probe/0/review/1/1", ticket=None)

        async def operation(served):
            assert r.journal.read()[-1].type == EventType.EFFECT_INTENT
            return served.model

        result = await r.thresh.run(r.route[2:], ticket=None, call_key="llm/probe/0/review/1/1",
                                    projected_wait_seconds=0, effect=effect, operation=operation)
        assert result.admission.served == r.route[2] and result.admission.waited_seconds == 0
        gates[0].set()
        await settle()
        assert [k for k, _ in r.calls] == [1, 2, 4, 3]
        assert r.thresh._slots["a"].used == 2
        for gate in gates:
            gate.set()
        await asyncio.gather(first, second, third, independent)
        clean(r)
    asyncio.run(scenario())


def test_cap_wait_is_fifo_and_journaled(tmp_path):
    async def scenario():
        r = Rig(tmp_path)
        held = await r.admit(r.route[:1])
        started = r.time()
        one = asyncio.create_task(r.admit(r.route[:1], key="llm/t/0/review/1/1"))
        await settle()
        r.time.advance(2)
        two_start = r.time()
        two = asyncio.create_task(r.admit(r.route[:1], key="llm/triage/0/triage/1/1", ticket=None))
        await settle()
        r.time.advance(3)
        r.thresh.release(held)
        await settle()
        assert one.done() and not two.done()
        first = await one
        assert first.waited_seconds == 5 and first.served == r.route[0]
        r.time.advance(4)
        r.thresh.release(first)
        await settle()
        second = await two
        assert second.waited_seconds == 7
        events = r.signals(PROVIDER_CAP_WAIT)
        assert [(e.type, e.ticket, e.key) for e in events] == [(EventType.SIGNAL, "t", None), (EventType.SIGNAL, None, None)]
        assert [e.body for e in events] == [
            wait_body("a", "llm/t/0/review/1/1", started, 5, "admitted"),
            wait_body("a", "llm/triage/0/triage/1/1", two_start, 7, "admitted")]
        r.thresh.release(second)
        clean(r)
    asyncio.run(scenario())


@pytest.mark.parametrize("alternate", ["free", "full", "all_open", "repeat", "cancel"])
def test_cap_wait_rechecks_open_breaker_before_admission(tmp_path, alternate):
    async def scenario():
        r = Rig(tmp_path, k=1)
        held_a = await r.admit(r.route[:1])
        held_b = await r.admit(r.route[1:2]) if alternate in {"full", "repeat", "cancel"} else None
        # A prior waiter in the new queue must stay ahead of the reselected caller.
        earlier = asyncio.create_task(r.admit(r.route[1:2], key="earlier")) if held_b else None
        await settle()
        started = r.time()
        task = asyncio.create_task(r.call(1, route=r.route[:2], estimate=60))
        await settle()
        assert r.calls == []
        r.time.advance(5)
        r.open("a")
        if alternate == "all_open":
            r.open("b")
        r.thresh.release(held_a)
        await settle()
        waits = r.signals(PROVIDER_CAP_WAIT)
        assert len(waits) == 1
        assert waits[0].body == wait_body("a", "llm/t/0/implement/1/1", started, 5, "unavailable")
        assert waits[0].ticket == "t" and waits[0].key is None
        assert r.thresh._slots["a"].used == 0 and not r.thresh._slots["a"].queue
        if alternate == "all_open":
            result = await task
            assert isinstance(result, UnavailableRoute) and result.waited_seconds == 5
            assert result.providers == ("a", "b") and r.calls == []
            assert not any(e.type in {EventType.EFFECT_INTENT, EventType.CAP_CONSUMED,
                                      EventType.EFFECT_COMPLETION, EventType.STATE_TRANSITION} for e in r.journal.read())
        elif alternate == "free":
            result = await task
            assert result.admission.served == r.route[1] and result.admission.waited_seconds == 5
            events = r.journal.read()
            assert events.index(waits[0]) < next(i for i, e in enumerate(events) if e.type == EventType.EFFECT_INTENT)
        else:
            assert not task.done() and r.calls == []
            assert len(r.thresh._slots["b"].queue) == 2
            r.thresh.release(held_b)
            await settle()
            assert earlier.done() and not task.done()
            first_b = await earlier
            r.time.advance(7)
            if alternate == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert r.signals(PROVIDER_CAP_WAIT)[-1].body["disposition"] == "cancelled"
                assert r.signals(PROVIDER_CAP_WAIT)[-1].body["waited_seconds"] == 7
                r.thresh.release(first_b)
            elif alternate == "repeat":
                # Opening the second queue and expiring the first requeues at a's tail.
                r.open("b")
                r.time.advance(593)  # a expires at 605; b stays open to 612
                hold_again = await r.admit(r.route[:1])
                ahead_a = asyncio.create_task(r.admit(r.route[:1], key="ahead-a"))
                await settle()
                r.thresh.release(first_b)
                await settle()
                assert len(r.thresh._slots["a"].queue) == 2
                assert not task.done()
                r.time.advance(2)
                r.thresh.release(hold_again)
                await settle()
                assert ahead_a.done() and not task.done()
                r.time.advance(3)
                r.thresh.release(await ahead_a)
                await settle()
                result = await task
                assert result.admission.served == r.route[0]
                assert result.admission.waited_seconds == 610
                own = [e for e in r.signals(PROVIDER_CAP_WAIT) if e.body["call_key"] == "llm/t/0/implement/1/1"]
                assert [(e.body["provider"], e.body["waited_seconds"], e.body["disposition"]) for e in own] == [
                    ("a", 5, "unavailable"), ("b", 600, "unavailable"), ("a", 5, "admitted")]
            else:
                r.thresh.release(first_b)
                await settle()
                result = await task
                assert result.admission.served == r.route[1] and result.admission.waited_seconds == 12
        clean(r)
    asyncio.run(scenario())


@pytest.mark.parametrize("error", [None, "cancel_active", RuntimeError("unexpected"), ProviderSetupError("setup"),
                                    ExecutableNotFound("fixture"),
                                    ProviderCallError("a", "down", rc=1, stderr_tail="down", failure_class="outage"),
                                    asyncio.CancelledError()])
def test_cap_wait_cancellation_and_call_unwind_release_slots(tmp_path, error):
    async def scenario():
        r = Rig(tmp_path)
        gate = asyncio.Event()
        active = asyncio.create_task(r.call(1, gate=gate, error=None if error == "cancel_active" else error))
        await settle()
        start = r.time()
        queued = asyncio.create_task(r.call(2, route=r.route[:1], ticket=None))
        await settle()
        r.time.advance(9)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        [wait] = r.signals(PROVIDER_CAP_WAIT)
        assert wait.body == wait_body("a", "llm/implement/0/implement/1/2", start, 9, "cancelled")
        assert wait.ticket is None and wait.key is None
        assert r.thresh._slots["a"].used == 1 and not r.thresh._slots["a"].queue
        if error == "cancel_active":
            active.cancel()
        else:
            gate.set()
        if error is None:
            assert isinstance(await active, CallResult)
        else:
            with pytest.raises(asyncio.CancelledError if error == "cancel_active" else type(error)):
                await active
        outcomes = r.signals(PROVIDER_CALL_OUTCOME)
        assert len(outcomes) == (0 if error == "cancel_active" or isinstance(error, (asyncio.CancelledError, ProviderSetupError, ExecutableNotFound)) else 1)
        assert [key for key, _ in r.calls] == [1]  # executed failures never retry or migrate inline
        clean(r)
        assert isinstance(await r.call(3, route=r.route[:1]), CallResult)
        clean(r)
    asyncio.run(scenario())


@pytest.mark.parametrize("estimate", [59, 60, 61])
@pytest.mark.parametrize("state", ["free", "full", "open", "queued", "blocked", "primary_open_free", "primary_open_full"])
def test_spill_requires_available_candidate_and_wait_over_sixty_seconds(tmp_path, estimate, state):
    async def scenario():
        r = Rig(tmp_path, k=1)
        assert SPILL_WAIT_SECONDS == 60
        held = []
        earlier = None
        if state.startswith("primary_open"):
            r.open("a")
            if state.endswith("full"):
                held.append(await r.admit(r.route[1:2]))
        else:
            held.append(await r.admit(r.route[:1]))
            if state in {"full", "queued"}:
                held.append(await r.admit(r.route[1:2]))
            if state == "open":
                r.open("b")
            if state == "blocked":
                r.open("b")
                held.append(await r.admit(r.route[2:]))
            if state == "queued":
                earlier = asyncio.create_task(r.admit(r.route[1:2], key="earlier"))
                await settle()
                r.thresh.release(held.pop())  # b has capacity but a prior waiter has not resumed
        task = asyncio.create_task(r.admit(estimate=estimate))
        # Select before the notified earlier b waiter reserves, to prove no queue bypass.
        await settle()
        if state == "primary_open_free":
            result = await task
            assert result.served == r.route[1]
        elif state == "primary_open_full":
            assert not task.done() and len(r.thresh._slots["b"].queue) == 1
            assert not r.thresh._slots["c"].queue and r.thresh._slots["c"].used == 0
            r.thresh.release(held.pop())
            await settle()
            result = await task
            assert result.served == r.route[1]  # primary estimate never spills the later queue
        elif estimate > 60 and state != "blocked":
            result = await task
            assert result.served == r.route[1 if state == "free" else 2]
        else:
            assert not task.done() and len(r.thresh._slots["a"].queue) == 1
            r.thresh.release(held.pop(0))
            await settle()
            result = await task
            assert result.served == r.route[0]
        assert result.waited_seconds == 0
        r.thresh.release(result)
        for admission in held:
            r.thresh.release(admission)
        if earlier:
            r.thresh.release(await earlier)
        clean(r)
    asyncio.run(scenario())


def test_breaker_counts_only_consecutive_outage_and_unclassified(tmp_path):
    r = Rig(tmp_path)
    for reset in (None, "rate_limited", "quota_exhausted", "auth_error", "model_error"):
        r.record("a", "outage")
        r.record("a", "unclassified")
        assert r.thresh.breakers()["a"].streak == 2
        assert r.thresh.breakers()["a"].open_until is None
        r.record("b", "outage")
        r.record("b", None)
        assert r.thresh.breakers()["a"].streak == 2
        r.record("a", reset)
        assert r.thresh.breakers()["a"].streak == 0
    r.record("a", "outage")
    r.record("a", "unclassified")
    assert r.thresh.breakers()["a"].open_until is None
    r.record("a", "outage")
    assert r.thresh.breakers()["a"].open_until == r.time() + timedelta(minutes=10)
    deadline = r.thresh.breakers()["a"].open_until
    r.time.advance(1)
    r.record("a", "unclassified")
    assert r.thresh.breakers()["a"].open_until == deadline
    assert r.signals(PROVIDER_CALL_OUTCOME)[-1].body["open_until"] is None
    assert r.thresh.breakers()["b"].open_until is None
    assert all(e.key is None and e.ticket == "t" for e in r.signals(PROVIDER_CALL_OUTCOME))


@pytest.mark.parametrize("older_failure", [None, "outage", "unclassified"])
def test_breaker_rebuilds_from_journal_and_releases_at_deadline(tmp_path, older_failure):
    async def scenario():
        r = Rig(tmp_path, cap=2, k=1)
        # Both calls are admitted before opening; the older one finishes after it.
        older_gate, newer_gate = asyncio.Event(), asyncio.Event()
        error = (ProviderCallError("a", "older failure", rc=1, stderr_tail="", failure_class=older_failure)
                 if older_failure else None)
        old = asyncio.create_task(r.call(1, route=r.route[:1], gate=older_gate, error=error))
        new = asyncio.create_task(r.call(2, route=r.route[:1], gate=newer_gate,
                                         error=ProviderCallError("a", "down", rc=1, stderr_tail="", failure_class="outage")))
        await settle()
        r.time.advance(4)
        opening_time = r.time()
        newer_gate.set()
        await settle()
        with pytest.raises(ProviderCallError):
            await new
        deadline = opening_time + timedelta(minutes=10)
        [opening] = r.signals(PROVIDER_CALL_OUTCOME)
        assert opening.ts == opening_time.isoformat() and opening.ticket == "t" and opening.key is None
        assert opening.body == {"signal": PROVIDER_CALL_OUTCOME, "provider": "a",
                                "call_key": "llm/t/0/implement/1/2", "failure_class": "outage",
                                "open_until": deadline.isoformat()}
        r.time.advance(3)
        older_gate.set()
        if older_failure:
            with pytest.raises(ProviderCallError):
                await old
        else:
            await old
        assert r.signals(PROVIDER_CALL_OUTCOME)[-1].body["open_until"] is None
        fresh = r.fresh()
        assert fresh.breakers()["a"].open_until == deadline
        assert fresh.breakers()["a"].streak == (2 if older_failure else 0)
        r.time.now = deadline - timedelta(microseconds=1)
        result = await fresh.admit(r.route[:1], ticket=None, call_key="before", projected_wait_seconds=0)
        assert isinstance(result, UnavailableRoute)
        r.time.now = deadline
        at = await fresh.admit(r.route[:1], ticket=None, call_key="equal", projected_wait_seconds=0)
        assert isinstance(at, Admission) and fresh.breakers()["a"].streak == 0
        fresh.release(at)
        r.time.advance(1)
        after = await fresh.admit(r.route[:1], ticket=None, call_key="after", projected_wait_seconds=0)
        fresh.release(after)
        # A fresh streak opens from this completion clock, not the original opening.
        fresh.record_outcome("a", "unclassified", ticket=None, call_key="new-failure")
        assert fresh.breakers()["a"].open_until == r.time() + timedelta(minutes=10)
        clean(r)
    asyncio.run(scenario())


def test_open_route_refuses_without_effect_or_cap_draw(tmp_path):
    async def scenario():
        r = Rig(tmp_path, k=1)
        r.open("a")
        r.time.advance(10)
        r.open("b")
        before = r.journal.read()
        refused = await r.call(1, route=r.route[:2])
        assert isinstance(refused, UnavailableRoute)
        assert refused.providers == ("a", "b") and refused.waited_seconds == 0
        assert refused.open_until == r.time() + timedelta(seconds=590)
        assert all(word in refused.paved_road for word in ("wait", refused.open_until.isoformat(), "repair", "route", "a", "b"))
        assert r.calls == [] and r.journal.read() == before
        clean(r)
        # A closed full provider is available: queue rather than refuse or spill past it.
        held = await r.admit(r.route[2:])
        queued = asyncio.create_task(r.call(2, estimate=1000))
        await settle()
        assert not queued.done() and len(r.thresh._slots["c"].queue) == 1
        r.thresh.release(held)
        await settle()
        served = await queued
        assert served.admission.served == r.route[2]
        r.time.now = refused.open_until
        assert (await r.call(3, route=r.route[:2])).admission.served == r.route[0]
        clean(r)
        assert not any(e.type in {EventType.CAP_CONSUMED, EventType.STATE_TRANSITION,
                                  EventType.TIMER_ARMED, EventType.TIMER_FIRED} for e in r.journal.read())
    asyncio.run(scenario())


def test_effect_replay_does_not_record_another_provider_outcome(tmp_path):
    async def scenario():
        r = Rig(tmp_path)
        first = await r.call(1)
        before = r.journal.read()
        r.effects = Effects(r.journal)  # completion replay also survives reconstruction
        second = await r.call(1)
        assert first == second and r.calls == [(1, r.route[0])]
        assert r.journal.read() == before
        [outcome] = r.signals(PROVIDER_CALL_OUTCOME)
        assert outcome.body == {"signal": PROVIDER_CALL_OUTCOME, "provider": "a",
                                "call_key": "llm/t/0/implement/1/1", "failure_class": None, "open_until": None}
        assert outcome.key is None and outcome.ticket == "t"
        assert [e.type for e in before] == [EventType.EFFECT_INTENT, EventType.SIGNAL, EventType.EFFECT_COMPLETION]
        assert before[-1].body["cost"] == {"usd": 1.0}
        clean(r)
    asyncio.run(scenario())


def cli_import_closure(sources):
    pending, reached = ["tempo", "tempo.__main__"], set()
    while pending:
        module = pending.pop()
        if module in reached or module not in sources:
            continue
        reached.add(module)
        pending.extend(".".join(module.split(".")[:i]) for i in range(1, len(module.split("."))))
        for node in ast.walk(ast.parse(sources[module])):
            if isinstance(node, ast.Import):
                pending.extend(alias.name for alias in node.names if alias.name == "tempo" or alias.name.startswith("tempo."))
            elif isinstance(node, ast.ImportFrom):
                if node.module == "tempo":
                    pending.extend("tempo." + alias.name for alias in node.names)
                elif node.module and node.module.startswith("tempo."):
                    pending.append(node.module)
                    pending.extend(node.module + "." + alias.name for alias in node.names)
    return reached


def test_thresh_is_active():
    sources = {}
    for path in (ROOT / "tempo").rglob("*.py"):
        parts = path.relative_to(ROOT).with_suffix("").parts
        sources[".".join(parts[:-1] if parts[-1] == "__init__" else parts)] = path.read_text()

    def assert_dormant(graph):
        reached = cli_import_closure(graph)
        assert "tempo.providers" in reached  # traverse beyond the entry point
        assert "tempo.thresh" in reached

    assert_dormant(sources)
    for statement in ("import tempo.thresh", "from tempo import thresh", "from tempo.thresh import Thresh"):
        for owner in ("tempo.providers", "tempo"):
            wired = {**sources, 'tempo.providers': sources['tempo.providers'].replace(
                'from tempo.thresh import Thresh', '').replace('from tempo.thresh import UnavailableRoute', '')}
            wired[owner] += '\n' + statement
            assert_dormant(wired)
            wired[owner] = wired[owner].removesuffix('\n' + statement)
            with pytest.raises(AssertionError):
                assert_dormant(wired)
