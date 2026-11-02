import asyncio
import json
import os

import pytest
from pydantic import BaseModel

from tempo import runner
from tempo.driver import LlmStage
from tempo.effects import Effects
from tempo.enginelog import EngineLog
from tempo.journal import EventType, Journal
from tempo.notify import WatchdogNotifications
from tempo.providers import CliAdapter, ProviderCallError, ProviderLLM, ProviderSetupError, Served, resolve
from tempo.redact import Redactor
from tempo.thresh import Admission, PROVIDER_CAP_WAIT
from tempo.watchdog import Detector, EventConsumer, ScopeObservation
from tests.test_daemon_composition import CoreRig
from tests.test_providers import (PythonProviderExec, claude_ok, codex_ok, config, jsonl,
                                 provider_request, python_client)
from tests.test_scheduler import Time, turn


def tool_events(name):
    if name == "claude":
        block = {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"file_path": "a.py"}}
        return [{"type": "assistant", "message": {"id": "msg-1", "content": [block]}},
                {"type": "assistant", "message": {"id": "msg-1", "content": [block]}}]
    item = {"id": "tool-1", "type": "command_execution", "command": "cat a.py"}
    return [{"type": kind, "item": {**item, "status": status}}
            for kind, status in (("item.started", "in_progress"), ("item.updated", "in_progress"),
                                 ("item.completed", "completed"))]


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_event_consumer_receives_before_terminal(tmp_path, name):
    cfg = config(tmp_path)

    async def scenario():
        connection = asyncio.get_running_loop().create_future()

        def connected(reader, writer):
            connection.set_result((reader, writer))

        server = await asyncio.start_server(connected, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        first, second = tool_events(name)[:2]
        terminal = (claude_ok if name == "claude" else codex_ok)()
        # Hold first after an incomplete line, then after two complete tool events.
        line = jsonl(first).encode()
        script = (
            "import os, socket\n"
            f"os.write(1, {line[:8]!r})\n"
            f"gate = socket.create_connection(('127.0.0.1', {port}))\n"
            "gate.recv(1)\n"
            f"os.write(1, {line[8:] + jsonl(second).encode()!r})\n"
            "gate.recv(1)\n"
            f"os.write(1, {terminal.encode()!r})\n"
        )
        process = PythonProviderExec(script)
        client = python_client(cfg, process, tmp_path)
        events, delivered = [], asyncio.Event()

        def consume(event):
            events.append(event)
            if len(events) == 2:
                delivered.set()

        task = asyncio.create_task(client.call(provider_request(name, tmp_path), consumer=EventConsumer(consume)))
        writer = None
        try:
            _, writer = await asyncio.wait_for(connection, 5)
            assert events == [] and not task.done()
            writer.write(b"1")
            await writer.drain()
            await asyncio.wait_for(delivered.wait(), 5)
            assert events == [first, second] and not task.done()
            assert list((cfg.state_dir / "spools").rglob("events.jsonl")) == []
            writer.write(b"2")
            await writer.drain()
            result = await asyncio.wait_for(task, 5)
            assert result.text == ("done" if name == "claude" else "patched")
            assert events == [first, second, *[json.loads(line) for line in terminal.splitlines()]]
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["claude", "codex"])
@pytest.mark.parametrize("with_usage", [True, False])
def test_event_consumer_preserves_tool_identity_and_optional_usage(tmp_path, name, with_usage):
    tools = tool_events(name)
    if name == "claude":
        terminal = {"type": "result", "subtype": "success", "is_error": False,
                    "result": "done", "total_cost_usd": 0.42}
        if with_usage:
            terminal["usage"] = {"input_tokens": 10, "output_tokens": 7}
        expected = [*tools, terminal]
    else:
        terminal = {"type": "turn.completed"}
        if with_usage:
            terminal["usage"] = {"input_tokens": 10, "output_tokens": 7}
        expected = [*tools, {"type": "item.completed", "item": {
            "id": "message-1", "type": "agent_message", "text": "done"}}, terminal]
    script = f"import os; os.write(1, {jsonl(*expected).encode()!r})"
    client = python_client(config(tmp_path), PythonProviderExec(script), tmp_path)
    events = []
    result = asyncio.run(client.call(provider_request(name, tmp_path), consumer=EventConsumer(events.append)))
    assert events == expected
    ids = ([event["message"]["content"][0]["id"] for event in events[:len(tools)]] if name == "claude"
           else [event["item"]["id"] for event in events[:len(tools)]])
    assert ids == ["tool-1"] * len(tools)
    assert ("usage" in events[-1]) is with_usage
    assert (result.input_tokens, result.output_tokens) == ((10, 7) if with_usage else (None, None))
    assert result.usd == (0.42 if name == "claude" else 1.5)
    assert not list(tmp_path.rglob("journal"))


async def production_activation(tmp_path, monkeypatch, component):
    calls, constructed = [], []
    invoke, construct = CliAdapter.invoke, component.__init__

    async def observe(self, req, model, **kwargs):
        if req.surface != "preflight":
            assert isinstance(kwargs.get("consumer"), EventConsumer)
            calls.append((self.provider.name, req.surface))
        return await invoke(self, req, model, **kwargs)

    def bound(self, *args, **kwargs):
        constructed.append(self)
        construct(self, *args, **kwargs)

    monkeypatch.setattr(CliAdapter, "invoke", observe)
    monkeypatch.setattr(component, "__init__", bound)
    rig = CoreRig(tmp_path)
    rig.exec.reply = '{"text": "done"}'

    class Reply(BaseModel):
        text: str

    async def drive(ctx, ticket):
        assert isinstance(ctx.driver.llm, ProviderLLM)
        for surface in ("implement", "review"):
            result = await ctx.driver.run(
                LlmStage(surface=surface, emits=Reply, gates=[], render=lambda *_: "probe"), None,
                ticket=ticket.stem, attempt=0, workspace=tmp_path, tier="medium", effort="low",
                stuck_budget=600, expected_budget=ticket.expected_minutes * 60,
                scope_fence=ticket.scope_fence if surface == "implement" else (),
            )
            assert result.outcome == "ok" and result.artifact.text == "done"
        return "merged"

    monkeypatch.setattr(runner, "drive", drive)
    await rig.add("ordinary")
    await rig.drain()
    assert constructed and calls == [("codex", "implement"), ("claude", "review")]
    assert not any("spiral" in str(event.body) for event in rig.journal.read())

    def forbidden(*args, **kwargs):
        raise AssertionError("activated watchdog component refused")

    monkeypatch.setattr(component, "__init__", forbidden)
    await rig.add("activated")
    with pytest.raises(AssertionError):
        await rig.drain()  # Removing either activated component must break the same graph.


@pytest.mark.asyncio
async def test_event_stream_is_dormant(tmp_path, monkeypatch):
    await production_activation(tmp_path, monkeypatch, EventConsumer)


@pytest.mark.asyncio
async def test_detector_is_dormant(tmp_path, monkeypatch):
    await production_activation(tmp_path, monkeypatch, Detector)


class DetectorRig:
    def __init__(self, root, *, expected=1, stuck=3, argv=('notify',), sequence=0, clock=None):
        root.mkdir(exist_ok=True)
        self.root, self.time = root, clock or Time()
        self.cfg = config(root)
        self.journal = Journal(root / 'state', self.time)
        self.log = EngineLog(root / 'engine.log', Redactor({}), self.time)
        self.sent = []
        rig = self

        class Delivery:
            async def notify(self, argv):
                rig.sent.append(argv)
                return {'rc': 0}

        self.transport = WatchdogNotifications(effects=Effects(self.journal), notifications=Delivery(),
            argv=argv, owner='work', ticket='work', run_sequence=sequence, identity='implement', log=self.log)
        self.files, self.waited = {}, 0.0
        self.observation = ScopeObservation(('out/', 'TEMPO_PLAN.md#19.P4.owned'), lambda: self.files.copy())
        self.detector = Detector(expected_minutes=expected, stuck_minutes=stuck,
            clock=self.time, sleep=self.time.sleep, observe=self.observation,
            cap_wait=lambda: self.waited, log=self.log, notify=self.transport)

    def served(self, name='claude', model='model', estimate=None):
        provider = next(p for p in self.cfg.providers if p.name == name).model_copy(deep=True)
        provider.limits.est_cost_per_call_usd = estimate
        return Served(provider, model)

    def call(self, **kwargs):
        return self.detector.start_call(self.served(**kwargs))

    def snapshot_disk(self):
        self.files = {str(p.relative_to(self.root)): p.read_bytes()
                      for p in self.root.rglob('*') if p.is_file()}


def terminal(cost, usage=None):
    event = {'type': 'result', 'subtype': 'success', 'is_error': False,
             'result': 'done', 'total_cost_usd': cost}
    if usage is not None:
        event['usage'] = usage
    return event


@pytest.mark.parametrize('seconds,region', [(0, 'healthy'), (90, 'soft'), (180, 'stuck')])
def test_spend_resets_only_on_observed_scope_mutation(tmp_path, seconds, region):
    rig = DetectorRig(tmp_path)
    d = rig.detector
    rig.time.advance(seconds)
    meter = rig.call(estimate=1)
    meter.consume(terminal(2))
    assert d.region == region and d.spend == 2
    for event in tool_events('claude') + tool_events('codex'):
        meter.consume(event)
        assert d.spend == 2
    outside = tmp_path / 'outside.py'
    outside.write_text('requested work happened outside the fence')
    rig.snapshot_disk()
    d.decide()
    assert d.spend == 2
    path = tmp_path / 'out/file.py'
    path.parent.mkdir()
    for mutation in ('create', 'same', 'change', 'delete'):
        if mutation == 'delete':
            path.unlink()
        else:
            path.write_text('new' if mutation == 'change' else 'old')
        rig.snapshot_disk()
        d.decide()
        assert d.spend == (1 if mutation == 'same' else 0)
        rig.call(estimate=1)
    assert d.bases == {}  # Progress never completes a call or learns its basis.

    plan = tmp_path / 'TEMPO_PLAN.md'
    plan.write_text('## 19. Registry\n### 19.P4.owned Owned\ncontent\n### 19.P4.other Other\nunrelated\n')
    rig.snapshot_disk()
    d.decide()
    rig.call(estimate=1)
    plan.write_text(plan.read_text().replace('unrelated', 'different unrelated bytes'))
    rig.snapshot_disk()
    d.decide()
    assert d.spend == 1
    plan.write_text(plan.read_text().replace('content', 'changed content'))
    rig.snapshot_disk()
    d.decide()
    assert d.spend == 0 and d.region == region
    rig.call(estimate=1)
    plan.write_text('## 19. Registry\n### 19.P4.other Other\nunrelated\n')
    rig.snapshot_disk()
    d.decide()
    assert d.spend == 0  # Removing the admitted unit is progress too.


def test_spend_metering_and_threshold(tmp_path):
    rig = DetectorRig(tmp_path)
    d = rig.detector
    for cost, spend, verdict in [(1, 1, False), (2, 3, False), (.5, 3.5, True)]:
        meter = rig.call()
        assert meter.basis == (None if cost == 1 else 1)
        for event in tool_events('claude'):
            meter.consume(event)
        event = terminal(cost, {'input_tokens': 10, 'output_tokens': 7})
        meter.consume(event)
        meter.consume(event)
        assert d.spend == spend and d.spiral is verdict
        meter.complete()
        assert d.bases == {('claude', 'model'): 1}
    assert d.tokens == 51 and meter.tools == {'tool-1'}
    # The first total survives retries and progress; the active basis never uses its own cost.
    rig.files['out/new'] = b'progress'
    d.decide()
    assert d.spend == 0 and d.bases[('claude', 'model')] == 1
    meter = rig.call(estimate=8)
    assert meter.basis == 1 and not d.spiral
    meter.consume(terminal(2))
    assert d.spend == 2 and meter.basis == 1 and not d.spiral
    meter.complete()
    assert d.bases[('claude', 'model')] == 1
    other = rig.call(model='other')
    assert other.basis is None and d.spend == 2
    other.consume(terminal(4))
    other.complete()
    assert rig.call(model='other').basis == 4
    assert rig.call().basis == 1 and d.spend == 6
    provider = rig.call(name='codex', estimate=5)
    assert provider.basis == 5 and d.spend == 11
    for event in tool_events('codex'):
        provider.consume(event)
    for _ in range(2):
        provider.consume({'type': 'turn.completed', 'usage': {'input_tokens': 100, 'output_tokens': 3}})
    assert d.spend == 11 and provider.tools == {'tool-1'}
    assert d.tokens == 154 and ('codex', 'model') not in d.bases
    fresh = DetectorRig(tmp_path / 'fresh')
    assert fresh.detector.spend == 0 and fresh.detector.tokens is None and fresh.detector.bases == {}
    assert fresh.call().basis is None
    fresh.detector.call.consume(terminal(0))
    fresh.detector.call.complete()
    zero = fresh.call()
    assert zero.basis == 0
    zero.consume(terminal(.5))
    assert fresh.detector.spiral and fresh.detector.spend == .5
    assert fresh.detector.tokens is None
    zero.consume({'type': 'result', 'usage': {}})
    assert fresh.detector.tokens is None
    with pytest.raises(ProviderSetupError, match='estimate|est_cost'):
        fresh.call(name='codex')
    cfg = rig.cfg.model_copy(deep=True)
    next(p for p in cfg.providers if p.name == 'codex').limits.est_cost_per_call_usd = None
    with pytest.raises(ProviderSetupError, match='est_cost_per_call_usd is required'):
        python_client(cfg, PythonProviderExec(''), tmp_path)

    snapshots = DetectorRig(tmp_path / 'snapshots')
    meter = snapshots.call(estimate=1)
    for usage in ({'input_tokens': 10}, {'input_tokens': 10, 'output_tokens': 5}):
        event = {'type': 'assistant', 'message': {'id': 'message', 'usage': usage, 'content': []}}
        meter.consume(event)
        meter.consume(event)
    meter.consume(terminal(.5, {'input_tokens': 10, 'output_tokens': 5}))
    assert snapshots.detector.tokens == 15 and snapshots.detector.spend == .5
    # A cumulative increase charges only the new cost, even after scope progress.
    snapshots.files['out/file'] = b'new bytes'
    meter.consume(terminal(.75))
    meter.consume(terminal(.75))
    assert snapshots.detector.spend == .25 and meter.metered == .75
    meter.complete()
    assert snapshots.detector.bases == {('claude', 'model'): .75}


@pytest.mark.asyncio
async def test_missing_cost_preserves_adapter_refusal(tmp_path):
    cfg = config(tmp_path)
    served = resolve(cfg, 'high', 'implement')
    served.provider.limits.est_cost_per_call_usd = None
    stream = terminal(None)
    client = python_client(cfg, PythonProviderExec(f'import os; os.write(1, {jsonl(stream).encode()!r})'), tmp_path)
    rig = DetectorRig(tmp_path / 'detector')
    with pytest.raises(ProviderCallError, match='reported no cost'):
        await rig.detector.watch(lambda consumer: client.call(provider_request('claude', tmp_path), consumer=consumer),
                                 served=served, abort=client.abort_current)
    assert rig.detector.bases == {} and rig.detector.spend == 0


@pytest.mark.asyncio
async def test_meter_ignores_non_object_event_fields(tmp_path):
    rig = DetectorRig(tmp_path)
    served = resolve(rig.cfg, 'high', 'implement')
    events = [
        {'type': 'system', 'subtype': 'permission_denied', 'message': 'Read denied'},
        {'type': 'assistant', 'message': {'id': 'bad', 'content': 'text', 'usage': 'unknown'}},
        {'type': 'turn.completed', 'item': 'command', 'usage': 'unknown'},
    ]
    for value in (None, False, 1, 'text', []):
        events.extend([
            {'type': 'assistant', 'message': value},
            {'type': 'item.started', 'item': value},
            {'type': 'turn.completed', 'usage': value},
            {'type': 'assistant', 'message': {'content': value, 'usage': value}},
        ])
    events.append({'type': 'assistant', 'message': {'content': {'type': 'tool_use', 'id': 'ignored'}}})
    events.extend(tool_events('claude') + tool_events('codex'))
    events.append({'type': 'assistant', 'message': {'id': 'msg-1',
        'content': [None, 'text', 1, [], {'type': 'tool_use', 'id': 'tool-2'}],
        'usage': {'input_tokens': 10, 'output_tokens': 7}}})
    stream = jsonl(*events) + claude_ok()
    client = python_client(rig.cfg, PythonProviderExec(f'import os; os.write(1, {stream.encode()!r})'), tmp_path)
    observed = []

    async def start(meter):
        def consume(event):
            epoch = rig.detector.progress_epoch
            rig.files['out/file'] = str(len(observed)).encode()
            meter.consume(event)
            assert rig.detector.progress_epoch == epoch + 1
            observed.append(event)

        return await client.call(provider_request('claude', tmp_path), consumer=EventConsumer(consume))

    result = await rig.detector.watch(start, served=served, abort=client.abort_current)
    assert result.text == 'done' and result.usd == 0.42
    assert observed == [json.loads(line) for line in stream.splitlines()]
    assert rig.detector.call.tools == {'tool-1', 'tool-2'}
    assert rig.detector.call.usage['msg-1'] == {'input_tokens': 10, 'output_tokens': 7}
    assert rig.detector.tokens == 122
    assert rig.detector.total_usd == rig.detector.spend == 0.42
    assert rig.detector.bases == {('claude', served.model): 0.42}


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['claude', 'codex'])
async def test_watched_result_and_capture_are_preserved(tmp_path, name):
    rig = DetectorRig(tmp_path)
    served = resolve(rig.cfg, 'high' if name == 'claude' else 'medium', 'implement')
    stream = jsonl(*tool_events(name)) + (claude_ok() if name == 'claude' else codex_ok())
    client = python_client(rig.cfg, PythonProviderExec(f'import os; os.write(1, {stream.encode()!r})'), tmp_path)
    request = provider_request(name, tmp_path)
    result = await rig.detector.watch(lambda consumer: client.call(request, consumer=consumer),
                                      served=served, abort=client.abort_current)
    ordinary = await client.call(request)
    assert result == ordinary
    assert rig.detector.total_usd == result.usd and rig.detector.spend == result.usd
    assert rig.detector.bases == ({(name, served.model): result.usd} if name == 'claude' else {})
    calls = sorted((rig.cfg.state_dir / 'spools/providers' / request.ticket).iterdir())
    assert len(calls) == 2
    for capture in calls:
        assert {p.name for p in capture.iterdir()} == {'prompt.md', 'events.jsonl', 'stderr.txt'}
        assert (capture / 'events.jsonl').read_text() == stream
    assert rig.journal.read() == []


@pytest.mark.asyncio
async def test_watchdog_regions_and_notify_once(tmp_path):
    rig = DetectorRig(tmp_path)
    d = rig.detector
    rig.call(estimate=1).consume(terminal(4))
    assert d.spiral and d.region == 'healthy' and not d.pending and rig.sent == []
    # Consume the existing admission value, not a new detector journal record.
    admission = Admission(rig.served(name='codex', estimate=1), 50.0)
    rig.time.advance(139)
    rig.waited = admission.waited_seconds
    assert d.active_seconds == 89 and d.decide() == 'healthy'
    rig.time.advance(1)
    assert d.decide() == 'soft'
    await d.flush_notifications()
    assert d.warned and len(rig.sent) == 1
    for _ in range(2):
        rig.files['out/file'] = str(_).encode()
        d.decide()
        rig.call(estimate=1).consume(terminal(4))
        await d.flush_notifications()
    assert len(rig.sent) == 1 and d.warned
    # The provider_cap_wait record can feed the same injected cumulative exclusion seam.
    rig.journal.append(EventType.SIGNAL, {'signal': PROVIDER_CAP_WAIT, 'provider': 'codex',
        'call_key': 'call', 'started_at': rig.time().isoformat(), 'waited_seconds': 50.0,
        'disposition': 'admitted'}, ticket='work')
    d.cap_wait = lambda: sum(e.body['waited_seconds'] for e in rig.journal.read()
                            if e.body.get('signal') == PROVIDER_CAP_WAIT)
    rig.time.advance(89)
    assert d.decide() == 'soft' and d.active_seconds == 179
    rig.time.advance(1)
    assert d.decide() == 'stuck'
    await d.flush_notifications()
    completed = [e.key for e in rig.journal.read() if e.type == EventType.EFFECT_COMPLETION]
    assert completed == ['notify/work/spiral-warning/0', 'notify/work/stuck-past-threshold/implement']
    next_run = DetectorRig(tmp_path, sequence=1, clock=rig.time)
    next_run.call(estimate=1).consume(terminal(4))
    next_run.time.advance(90)
    next_run.detector.decide()
    await next_run.detector.flush_notifications()
    assert len(next_run.sent) == 1
    next_run.time.advance(90)
    next_run.detector.decide()
    await next_run.detector.flush_notifications()
    assert len(next_run.sent) == 1  # Stuck keys omit run sequence and Effects suppress replay.
    assert 'notify/work/spiral-warning/1' in [e.key for e in next_run.journal.read()]
    unset = DetectorRig(tmp_path / 'unset', argv=None)
    unset.call(estimate=1).consume(terminal(4))
    unset.time.advance(90)
    unset.detector.decide()
    await unset.detector.flush_notifications()
    assert unset.sent == [] and unset.journal.read() == []
    assert 'spiral_warning' in unset.log.path.read_text() and 'push is off' in unset.log.path.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize('name,estimate', [('claude', None), ('claude', 1), ('codex', 1)])
async def test_watchdog_hard_timeout_group_cleanup(tmp_path, name, estimate):
    # Expected==stuck is rejected by ticket grammar, not floored again at runtime.
    rig = DetectorRig(tmp_path, expected=1, stuck=1)
    d = rig.detector
    started, cleaning, finish = (asyncio.Event() for _ in range(3))
    trace = []

    async def hung(consumer):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            trace.append('cancelled')
            cleaning.set()
            await finish.wait()
            trace.append('reaped')

    def abort():
        trace.append('group-kill')

    task = asyncio.create_task(d.watch(hung, served=rig.served(name=name, estimate=estimate), abort=abort))
    await started.wait()
    assert d.spend == (estimate or 0) and d.call.basis == estimate and not d.spiral
    rig.waited = 20
    rig.time.advance(79)
    await turn()
    assert not task.done() and trace == [] and d.active_seconds == 59
    rig.time.advance(1)
    await cleaning.wait()
    assert trace == ['group-kill', 'cancelled'] and not task.done()
    # Even cancellation of the caller cannot abandon owned asynchronous cleanup.
    task.cancel()
    await turn()
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert trace == ['group-kill', 'cancelled', 'reaped']
    assert d.bases == {} and d.stuck_warned

    # Repeat without caller cancellation to prove the existing timeout outcome.
    fresh = DetectorRig(tmp_path / 'timeout', expected=1, stuck=1)
    async def silent(_):
        await asyncio.Event().wait()
    timeout = asyncio.create_task(fresh.detector.watch(silent,
        served=fresh.served(name=name, estimate=estimate), abort=abort))
    await turn()
    fresh.time.advance(60)
    result = await timeout
    assert result.outcome == 'timeout' and result.cost.seconds == 60
    assert result.cost.usd == (estimate or 0) and fresh.detector.bases == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['claude', 'codex'])
async def test_watchdog_real_group_cleanup(tmp_path, name):
    rig = DetectorRig(tmp_path)
    before = set(asyncio.all_tasks())
    connected = asyncio.get_running_loop().create_future()

    def connection(reader, writer):
        connected.set_result((reader, writer))

    server = await asyncio.start_server(connection, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    marker = tmp_path / 'grandchild-wrote'
    grandchild = (f'import socket; gate=socket.create_connection(("127.0.0.1", {port})); '
                  f'gate.recv(1); open({str(marker)!r}, "w").close()')
    script = ('import subprocess, sys, signal\n'
              f'subprocess.Popen([sys.executable, "-c", {grandchild!r}])\n'
              f'print({json.dumps(tool_events(name)[0])!r}, flush=True)\n'
              'signal.pause()\n')
    trace = []

    class Process(PythonProviderExec):
        def kill_group(self, pgid):
            trace.append('group-kill')
            super().kill_group(pgid)

        async def run(self, argv, **kwargs):
            try:
                return await super().run(argv, **kwargs)
            finally:
                trace.append('reaped')

    process = Process(script)
    client = python_client(rig.cfg, process, tmp_path)
    served = resolve(rig.cfg, 'high' if name == 'claude' else 'medium', 'implement')
    task = asyncio.create_task(rig.detector.watch(
        lambda consumer: client.call(provider_request(name, tmp_path), consumer=consumer),
        served=served, abort=client.abort_current))
    writer = None
    try:
        reader, writer = await asyncio.wait_for(connected, 5)
        assert client._active._pgid == process.pgids[-1]
        rig.time.advance(180)
        result = await asyncio.wait_for(task, 5)
        assert result.outcome == 'timeout'
        assert trace[0] == 'group-kill' and trace[-1] == 'reaped'
        assert await asyncio.wait_for(reader.read(), 5) == b''  # Grandchild was killed too.
        assert not marker.exists()
        with pytest.raises(ProcessLookupError):
            os.kill(process.pgids[-1], 0)
        assert client._active is None and client._adapters[name]._pgid is None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        server.close()
        await server.wait_closed()
    assert set(asyncio.all_tasks()) == before


# Pinned adapter objects: verdicts are explicit and require no external judge or fixture files.
SPIRAL_CORPUS = [
    ('claude', 1, [
        {'type': 'assistant', 'message': {'id': 'm', 'content': [
            {'type': 'tool_use', 'id': 'poll', 'name': 'Bash', 'input': {'command': 'poll'}}]}},
        {'type': 'assistant', 'message': {'id': 'm', 'content': [
            {'type': 'tool_use', 'id': 'poll', 'name': 'Bash', 'input': {'command': 'poll'}}]}},
        {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'done', 'total_cost_usd': 3.5},
        {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'done', 'total_cost_usd': 3.5},
    ], True, 3.5),
    ('claude', 1, [
        {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'done', 'total_cost_usd': 3},
        {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'done', 'total_cost_usd': 3},
    ], False, 3),
    ('claude', None, [
        {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'done', 'total_cost_usd': 50},
    ], False, 50),
    ('codex', 1, [
        {'type': 'item.started', 'item': {'id': 't', 'type': 'command_execution', 'command': 'poll'}},
        {'type': 'item.updated', 'item': {'id': 't', 'type': 'command_execution', 'command': 'poll'}},
        {'type': 'item.completed', 'item': {'id': 't', 'type': 'command_execution', 'command': 'poll'}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 100000, 'output_tokens': 10000}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 100000, 'output_tokens': 10000}},
    ], False, 1),
]


@pytest.mark.parametrize('name,estimate,events,verdict,spend', SPIRAL_CORPUS)
def test_spiral_corpus(tmp_path, name, estimate, events, verdict, spend):
    rig = DetectorRig(tmp_path)
    meter = rig.call(name=name, estimate=estimate)
    for event in events:
        meter.consume(event)
    assert rig.detector.spiral is verdict and rig.detector.spend == spend
