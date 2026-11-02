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
from tempo.watchdog import Detector, EventConsumer
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
def test_event_consumer_preserves_tool_identity(tmp_path, name):
    tools = tool_events(name)
    if name == "claude":
        terminal = {"type": "result", "subtype": "success", "is_error": False, "result": "done"}
        expected = [*tools, terminal]
    else:
        terminal = {"type": "turn.completed"}
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
    assert result.text == "done"
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
                stuck_budget=600,
            )
            assert result.outcome == "ok" and result.artifact.text == "done"
        return "merged"

    monkeypatch.setattr(runner, "drive", drive)
    await rig.add("ordinary")
    await rig.drain()
    assert constructed and calls == [("codex", "implement"), ("claude", "review")]

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
    def __init__(self, root, *, stuck=3, argv=('notify',), clock=None):
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
            argv=argv, owner='work', ticket='work', identity='implement', log=self.log)
        self.waited = 0.0
        self.detector = Detector(stuck_minutes=stuck, clock=self.time, sleep=self.time.sleep,
            cap_wait=lambda: self.waited, log=self.log, notify=self.transport)

    def served(self, name='claude', model='model'):
        provider = next(p for p in self.cfg.providers if p.name == name)
        return Served(provider, model)

    def call(self, **kwargs):
        return self.detector.start_call(self.served(**kwargs))


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
            meter.consume(event)
            observed.append(event)

        return await client.call(provider_request('claude', tmp_path), consumer=EventConsumer(consume))

    result = await rig.detector.watch(start, served=served, abort=client.abort_current)
    assert result.text == 'done'
    assert observed == [json.loads(line) for line in stream.splitlines()]


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
    rig.call()
    # Consume the existing admission value, not a new detector journal record.
    admission = Admission(rig.served(name='codex'), 50.0)
    rig.time.advance(139)
    rig.waited = admission.waited_seconds
    assert d.active_seconds == 89 and d.decide() == 'healthy'
    # The provider_cap_wait record can feed the same injected cumulative exclusion seam.
    rig.journal.append(EventType.SIGNAL, {'signal': PROVIDER_CAP_WAIT, 'provider': 'codex',
        'call_key': 'call', 'started_at': rig.time().isoformat(), 'waited_seconds': 50.0,
        'disposition': 'admitted'}, ticket='work')
    d.cap_wait = lambda: sum(e.body['waited_seconds'] for e in rig.journal.read()
                            if e.body.get('signal') == PROVIDER_CAP_WAIT)
    rig.time.advance(90)
    assert d.decide() == 'healthy' and d.active_seconds == 179 and rig.sent == []
    rig.time.advance(1)
    assert d.decide() == 'stuck'
    await d.flush_notifications()
    d.decide()
    await d.flush_notifications()
    assert len(rig.sent) == 1
    completed = [e.key for e in rig.journal.read() if e.type == EventType.EFFECT_COMPLETION]
    assert completed == ['notify/work/stuck-past-threshold/implement']
    next_run = DetectorRig(tmp_path, clock=rig.time)
    next_run.call()
    next_run.time.advance(180)
    next_run.detector.decide()
    await next_run.detector.flush_notifications()
    assert next_run.sent == []  # Stuck keys omit run sequence and Effects suppress replay.
    unset = DetectorRig(tmp_path / 'unset', argv=None)
    unset.call()
    unset.time.advance(180)
    unset.detector.decide()
    await unset.detector.flush_notifications()
    assert unset.sent == [] and unset.journal.read() == []
    assert 'stuck_budget_kill' in unset.log.path.read_text() and 'push is off' in unset.log.path.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['claude', 'codex'])
async def test_watchdog_hard_timeout_group_cleanup(tmp_path, name):
    rig = DetectorRig(tmp_path, stuck=1)
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

    task = asyncio.create_task(d.watch(hung, served=rig.served(name=name), abort=abort))
    await started.wait()
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
    assert d.stuck_warned

    # Repeat without caller cancellation to prove the existing timeout outcome.
    fresh = DetectorRig(tmp_path / 'timeout', stuck=1)
    async def silent(_):
        await asyncio.Event().wait()
    timeout = asyncio.create_task(fresh.detector.watch(silent,
        served=fresh.served(name=name), abort=abort))
    await turn()
    fresh.time.advance(60)
    result = await timeout
    assert result.outcome == 'timeout' and fresh.detector.active_seconds == 60
    assert (result.provider, result.model) == (name, 'model')


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
