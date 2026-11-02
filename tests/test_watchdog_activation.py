"""Watchdog evidence through the CLI core, serving graph, and actual eval callers."""

import asyncio
import json
import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from tempo import __main__ as cli, providers, runner, stages
from tempo.driver import LlmStage
from tempo.journal import EventType
from tempo.requisition import review_ticket
from tempo.watchdog import EventConsumer
from tests.test_cli import root, write
from tests.test_daemon_composition import CoreRig
from tests.test_providers import CONFIG, PythonProviderExec, claude_ok
from tests.test_requisition import TICKET
from tests.test_scheduler import text, turn
from tests.test_serve import graph, until, finish, terminals

SPECS = Path(__file__).resolve().parents[1] / 'specs'
WATCH_CONFIG = CONFIG.replace('review: {}', '''  - {tier: high, surface: review, candidates: [{provider: claude, model: c-max}]}
  - {tier: max, surface: review, candidates: [{provider: claude, model: c-max}]}
review: {}''')


class Reply(BaseModel):
    text: str


def probe(surface, review=None):
    return LlmStage(surface=surface, emits=Reply, gates=[], render=lambda *_: 'probe', review=review)


async def composed(tmp_path, monkeypatch, *, config=WATCH_CONFIG):
    captured = []

    async def capture(ctx, ticket):
        captured.append((ctx, ticket))
        return 'merged'

    monkeypatch.setattr(runner, 'drive', capture)
    rig = CoreRig(tmp_path, config=config)
    await rig.add('work')
    await rig.drain()
    ctx, ticket = captured[0]
    return rig, ctx, ticket


async def call(rig, driver, ticket, *, surface='implement', attempt=0,
               stuck=1200, sequence=None, stage=None):
    return await driver.run(stage or probe(surface), None, ticket=ticket, attempt=attempt,
        workspace=rig.root, tier='medium', effort='low', stuck_budget=stuck, run_seq=sequence)


@pytest.mark.asyncio
async def test_every_production_driver_surface_is_watched(tmp_path, monkeypatch):
    rig, ctx, ticket = await composed(tmp_path, monkeypatch)
    invoke = providers.CliAdapter.invoke
    seen = []

    async def observed(self, req, model, **kwargs):
        consumer = kwargs['consumer']
        assert isinstance(consumer, EventConsumer)
        seen.append((req, consumer.detector, consumer))
        return await invoke(self, req, model, **kwargs)

    monkeypatch.setattr(providers.CliAdapter, 'invoke', observed)
    rig.exec.reply = '{"text":"done"}'
    for surface in ('implement', 'review', 'rework', 'diagnose', 'retro'):
        result = await call(rig, ctx.driver, ticket.stem, surface=surface)
        assert result.outcome == 'ok'
        req, detector, meter = seen[-1]
        assert req.surface == surface and req.ticket == ticket.stem
        assert req.worktree == (rig.root if surface == 'implement' else None)
        assert detector.stuck_seconds == 1200
        assert (result.provider, result.model) == (('codex', 'x-med') if surface == 'implement'
                                                   else ('claude', 'c-max'))
        completion = [e for e in rig.journal.read() if e.type == EventType.EFFECT_COMPLETION
                      and e.key == f'llm/{ticket.stem}/0/{surface}/0/1']
        assert completion[-1].body['served'] == {'provider': result.provider, 'model': result.model}

    for surface, stuck in (('author', 900), ('triage', 600)):
        result = await call(rig, ctx.driver, None, surface=surface, stuck=stuck, sequence=7)
        assert result.outcome == 'ok'
        req, detector, _ = seen[-1]
        assert req.ticket is req.worktree is None
        assert detector.stuck_seconds == stuck
        rig.time.advance(stuck - .01)
        assert detector.region == 'healthy'
        rig.time.advance(.01)
        assert detector.region == 'stuck'

    # Same invocation's retries and nested reviewed Author proposals share the detector.
    (tmp_path / 'context.txt').write_text('context')
    calls = []
    async def nested(artifact, seq):
        d = ctx.driver.detector
        rig.exec.reply = '{"verdict":"approve","summary":"reviewed","findings":[]}'
        verdict = await review_ticket(ctx.driver, repo=tmp_path, stem='candidate',
            text=TICKET, specs_dir=SPECS, tier='medium', stem_slot='author', run_seq=8,
            attempt=0, call_seq=seq)
        assert verdict.verdict == 'approve' and ctx.driver.detector is d
        calls.append(d)
        from tempo.gates import GateReport
        return GateReport(code='requisition_review', verdict='pass')
    rig.exec.reply = '{"text":"done"}'
    assert (await call(rig, ctx.driver, None, surface='author', stuck=900,
                       sequence=8, stage=probe('author', nested))).outcome == 'ok'
    assert seen[-1][0].surface == 'requisition_review' and seen[-1][1] is calls[0]
    assert seen[-1][0].worktree is None
    assert any(e.key == 'llm/author/8/requisition_review/0/1' for e in rig.journal.read())
    assert (rig.checkout.config.state_dir / 'spools/author/8/0/requisition_review/call-01/prompt.md') in rig.fs.files

    # Provider-cap time is excluded using its existing admission record, without activating Thresh.
    d = calls[0]
    rig.time.advance(700)
    rig.journal.append(EventType.SIGNAL, {'signal': 'provider_cap_wait', 'provider': 'claude',
        'call_key': 'llm/author/8/author/0/1', 'started_at': d.started.isoformat(),
        'waited_seconds': 100, 'disposition': 'admitted'}, ticket=None)
    assert d.active_seconds == 600 and d.region == 'healthy'

    async def nested_rework(artifact, seq):
        parent = ctx.driver.detector
        rig.exec.reply = '{"verdict":"approve","summary":"reviewed","findings":[]}'
        verdict = await review_ticket(ctx.driver, repo=tmp_path, stem='candidate',
            text=TICKET, specs_dir=SPECS, tier='medium', stem_slot=ticket.stem, run_seq=0,
            attempt=1, call_seq=seq)
        assert verdict.verdict == 'approve' and ctx.driver.detector is parent
        assert seen[-1][0].surface == 'requisition_review' and seen[-1][1] is parent
        from tempo.gates import GateReport
        return GateReport(code='requisition_review', verdict='pass')
    rig.exec.reply = '{"text":"done"}'
    assert (await call(rig, ctx.driver, ticket.stem, surface='rework', attempt=1,
                       stage=probe('rework', nested_rework))).outcome == 'ok'

    # Separate requisition calls share one standalone detector.
    standalone = []
    for seq in (1, 2):
        verdict = await review_ticket(ctx.driver, repo=tmp_path, stem='candidate',
            text=TICKET, specs_dir=SPECS, tier='medium', stem_slot='seeding', run_seq=0,
            attempt=0, call_seq=seq)
        assert verdict.verdict == 'approve'
        standalone.append(ctx.driver.detector)
        assert seen[-1][0].surface == 'requisition_review' and seen[-1][0].worktree is None
        assert any(e.key == f'llm/seeding/0/requisition_review/0/{seq}' for e in rig.journal.read())
    assert standalone[0] is standalone[1]


@pytest.mark.asyncio
async def test_production_hard_timeout_group_kill_is_harvested(root, monkeypatch):
    rig = await graph(root, monkeypatch)
    rig.put_config((root / 'config.yaml').read_text() + 'caps: {infra: 1}\n')
    connection = asyncio.get_running_loop().create_future()
    def connected(reader, writer):
        connection.set_result((reader, writer))
    server = await asyncio.start_server(connected, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    marker = root / 'descendant-wrote'
    descendant = (f'import socket; s=socket.create_connection(("127.0.0.1", {port})); '
                  f's.recv(1); open({str(marker)!r}, "w").close()')
    script = ('import subprocess, sys, signal\n'
              f'subprocess.Popen([sys.executable, "-c", {descendant!r}])\n'
              'print(\'{"type":"item.started","item":{"id":"tool","type":"command_execution"}}\', flush=True)\n'
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
    scripted = rig.exec.run
    async def execute(argv, **kwargs):
        if argv[0] in ('claude', 'codex') and kwargs.get('stdin_path') is not None:
            if kwargs['stdin_path'].read_text() != providers.PROBE_PROMPT:
                assert kwargs.get('on_stdout_line') is not None
                return await process.run(argv, **kwargs)
        return await scripted(argv, **kwargs)
    monkeypatch.setattr(rig.exec, 'run', execute)
    monkeypatch.setattr(rig.exec, 'kill_group', process.kill_group)
    remove = rig.checkout.git.worktree_remove
    harvested = []
    async def removed(repo, path):
        harvest = root / 'tickets/work/attempts/0/harvest.json'
        data = json.loads(harvest.read_text())
        assert data['terminal'] == 'timeout' and data['stage'] == 'implement'
        assert trace[0] == 'group-kill' and trace[-1] == 'reaped'
        assert await rig.checkout.git._run(root, 'show', 'HEAD:tickets/work/attempts/0/harvest.json') == harvest.read_text()
        assert terminals(rig, 'work')[-1].body['to'] == 'timeout'
        harvested.append(data)
        await remove(repo, path)
    monkeypatch.setattr(rig.checkout.git, 'worktree_remove', removed)
    write(root, 'work', text().replace('stuck: 20m', 'stuck: 2m'))
    run = asyncio.create_task(rig.owner.run())
    writer = None
    try:
        await until(rig, connection.done)
        reader, writer = connection.result()
        ctx = rig.owner.writers['work'].ctx
        assert ctx.driver.llm._exec is ctx.exec_ is rig.exec
        rig.time.advance(120)
        await until(rig, lambda: bool(harvested) and not ctx.worktree('work').exists())
        assert await asyncio.wait_for(reader.read(), 5) == b''
        assert not marker.exists() and not ctx.worktree('work').exists()
        with pytest.raises(ProcessLookupError):
            os.kill(process.pgids[-1], 0)
        assert ctx.driver._active is None and ctx.driver.llm._active is None
        assert not list(ctx.config.state_dir.glob('spools/work/**/output.txt'))
    finally:
        await finish(rig, run)
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        server.close()
        await server.wait_closed()
