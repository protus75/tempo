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
from eval import diagnose, harness
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


async def call(rig, driver, ticket, *, surface='implement', attempt=0, expected=600,
               stuck=1200, sequence=None, stage=None):
    return await driver.run(stage or probe(surface), None, ticket=ticket, attempt=attempt,
        workspace=rig.root, tier='medium', effort='low', stuck_budget=stuck,
        expected_budget=expected, scope_fence=('tempo/thing.py',) if surface == 'implement' else (),
        run_seq=sequence)


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
        result = await call(rig, ctx.driver, ticket.stem, surface=surface,
                            expected=ticket.expected_minutes * 60)
        assert result.outcome == 'ok'
        req, detector, meter = seen[-1]
        assert req.surface == surface and req.ticket == ticket.stem
        assert req.worktree == (rig.root if surface == 'implement' else None)
        assert detector.observe.fence == (('tempo/thing.py',) if surface == 'implement' else ())
        assert detector.expected_seconds == ticket.expected_minutes * 60
        assert detector.stuck_seconds == 1200
        assert meter.tools == ({"i1"} if surface == "implement" else set())
        assert (result.cost.provider, result.cost.model, result.cost.usd) == (
            ('codex', 'x-med', 1.5) if surface == 'implement' else ('claude', 'c-max', .42))

    for surface, expected, stuck, boundary in (('author', 450, 900, 675), ('triage', 300, 600, 450)):
        result = await call(rig, ctx.driver, None, surface=surface, expected='surface',
                            stuck=stuck, sequence=7)
        assert result.outcome == 'ok'
        req, detector, _ = seen[-1]
        assert req.ticket is req.worktree is None and detector.observe.fence == ()
        assert (detector.expected_seconds, detector.stuck_seconds) == (expected, stuck)
        rig.time.advance(boundary - .01)
        assert detector.region == 'healthy'
        rig.time.advance(.01)
        assert detector.region == 'soft'
        rig.time.advance(stuck - boundary)
        assert detector.region == 'stuck'

    before = len(seen)
    with pytest.raises(ValueError, match='expected budget'):
        await call(rig, ctx.driver, ticket.stem, expected=None)
    assert len(seen) == before
    with pytest.raises(ValueError, match='ticket-owned'):
        await call(rig, ctx.driver, ticket.stem, expected='surface')

    # Same invocation's retries and nested reviewed Author proposals share the detector.
    (tmp_path / 'context.txt').write_text('context')
    calls = []
    async def nested(artifact, seq):
        d = ctx.driver.detector
        rig.exec.reply = '{"verdict":"approve","summary":"reviewed","findings":[]}'
        verdict = await review_ticket(ctx.driver, repo=tmp_path, plan='', stem='candidate',
            text=TICKET, specs_dir=SPECS, tier='medium', stem_slot='author', run_seq=8,
            attempt=0, call_seq=seq, expected_budget=ctx.driver.detector.expected_seconds)
        assert verdict.verdict == 'approve' and ctx.driver.detector is d
        calls.append(d)
        from tempo.gates import GateReport
        return GateReport(code='requisition_review', verdict='pass')
    rig.exec.reply = '{"text":"done"}'
    assert (await call(rig, ctx.driver, None, surface='author', expected='surface', stuck=900,
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
        verdict = await review_ticket(ctx.driver, repo=tmp_path, plan='', stem='candidate',
            text=TICKET, specs_dir=SPECS, tier='medium', stem_slot=ticket.stem, run_seq=0,
            attempt=1, call_seq=seq, expected_budget=ticket.expected_minutes * 60)
        assert verdict.verdict == 'approve' and ctx.driver.detector is parent
        assert seen[-1][0].surface == 'requisition_review' and seen[-1][1] is parent
        from tempo.gates import GateReport
        return GateReport(code='requisition_review', verdict='pass')
    rig.exec.reply = '{"text":"done"}'
    assert (await call(rig, ctx.driver, ticket.stem, surface='rework', attempt=1,
                       stage=probe('rework', nested_rework))).outcome == 'ok'

    # Check's separate requisition calls carry the seeding parent's budget authority.
    standalone = []
    for seq in (1, 2):
        verdict = await review_ticket(ctx.driver, repo=tmp_path, plan='', stem='candidate',
            text=TICKET, specs_dir=SPECS, tier='medium', stem_slot='seeding', run_seq=0,
            attempt=0, call_seq=seq, expected_budget=ticket.expected_minutes * 60)
        assert verdict.verdict == 'approve'
        standalone.append(ctx.driver.detector)
        assert seen[-1][0].surface == 'requisition_review' and seen[-1][0].worktree is None
        assert ctx.driver.detector.expected_seconds == ticket.expected_minutes * 60
        assert ctx.driver.detector.observe.fence == ()
        assert any(e.key == f'llm/seeding/0/requisition_review/0/{seq}' for e in rig.journal.read())
    assert standalone[0] is standalone[1] and standalone[0].calls == 2
    assert standalone[0].bases == {('claude', 'c-max'): .42}


@pytest.mark.asyncio
async def test_production_soft_band_notifies_once_without_kill(tmp_path, monkeypatch):
    deliveries = []
    class NotificationExec:
        async def run(self, argv, **kwargs):
            assert kwargs['cwd'] == tmp_path
            assert 'CLAUDE_KEY' not in kwargs['env'] and 'CODEX_KEY' not in kwargs['env']
            deliveries.append(argv)
            return 0, 'delivered', ''

    monkeypatch.setattr(cli, 'SubprocessExec', NotificationExec)
    config = WATCH_CONFIG.replace('limits: {concurrency: 1}',
                                 'limits: {concurrency: 1, est_cost_per_call_usd: 0.1}').replace(
                                     '[{provider: codex}, {provider: claude}]', '[{provider: claude}]') + 'notify: [notify]\n'
    rig, ctx, ticket = await composed(tmp_path, monkeypatch, config=config)
    invoke = providers.CliAdapter.invoke
    detectors = []
    count = 0

    async def spinning(self, req, model, **kwargs):
        nonlocal count
        count += 1
        d = kwargs['consumer'].detector
        detectors.append(d)
        rig.exec.reply = 'invalid' if count % 3 else '{"text":"done"}'
        if count % 3 == 1:
            rig.time.advance(90)
        if count % 3 == 2:
            (tmp_path / 'tempo/thing.py').write_text(str(count))
        return await invoke(self, req, model, **kwargs)

    monkeypatch.setattr(providers.CliAdapter, 'invoke', spinning)
    killed = []
    monkeypatch.setattr(ctx.driver.llm, 'abort_current', lambda: killed.append('killed'))
    for seq in (0, 1):
        result = await call(rig, ctx.driver, ticket.stem, surface='implement', expected=60, stuck=180,
                            attempt=seq)
        assert result.outcome == 'ok' and result.cost.attempts == 3
        batch = detectors[seq * 3:(seq + 1) * 3]
        assert batch[0] is batch[1] is batch[2] and batch[0].warned
        keys = [e.key for e in rig.journal.read() if e.key == f'notify/work/spiral-warning/{seq}']
        assert keys == [f'notify/work/spiral-warning/{seq}'] * 2
        assert batch[0].progress_epoch == 1
        repeated = await call(rig, ctx.driver, ticket.stem, surface='implement', expected=60,
                              stuck=180, attempt=seq)
        assert repeated.cost.usd == result.cost.usd and repeated.cost.provider == result.cost.provider
        assert ctx.driver.detector is batch[0]
        assert len(deliveries) == seq + 1
        rig.transition(ticket.stem, 'gate_failed')
    assert detectors[0] is not detectors[3] and len(deliveries) == 2 and killed == []
    for seq in (0, 1):
        events = [e for e in rig.journal.read() if e.key == f'notify/work/spiral-warning/{seq}']
        assert [e.type for e in events] == [EventType.EFFECT_INTENT, EventType.EFFECT_COMPLETION]
    assert not any('stuck-past-threshold' in (e.key or '') for e in rig.journal.read())


@pytest.mark.asyncio
async def test_eval_owned_budgets_preserve_synthetic_identities(tmp_path, monkeypatch):
    rig, ctx, _ = await composed(tmp_path, monkeypatch)
    invoke = providers.CliAdapter.invoke
    observed = []
    async def events(self, req, model, **kwargs):
        d = kwargs['consumer'].detector
        observed.append((req, d))
        rig.exec.reply = ('{"verdict":"approve","summary":"approved","findings":[]}'
                         if req.surface == 'review' else '{"verdict":"escalate","lessons":["Try stronger reasoning."]}')
        if len(observed) == 1:
            rig.exec.reply = 'invalid JSON'
        return await invoke(self, req, model, **kwargs)
    monkeypatch.setattr(providers.CliAdapter, 'invoke', events)
    fixture = next(f for f in harness.load_fixtures() if f.expected.defect_class == 'none')
    assert '## Time budget' not in fixture.ticket
    for attempt in (1, 2):
        await harness.run_baseline(config=ctx.config, driver=ctx.driver, journal=rig.journal,
            fixtures=[fixture], workspace=tmp_path)
        req, d = observed[-1]
        assert req.ticket == f'baseline-{fixture.name}' and req.worktree is None
        assert (d.expected_seconds, d.stuck_seconds, d.observe.fence) == (600, 1200, ())
        assert any(e.key == f'llm/baseline-{fixture.name}/0/review/{attempt}/1' for e in rig.journal.read())
        assert rig.checkout.config.state_dir / f'spools/baseline-{fixture.name}/{attempt}/review/call-01/prompt.md' in rig.fs.files
        assert any('/providers/' in str(p) and f'/baseline-{fixture.name}/' in str(p)
                   for p in rig.fs.files)
        if attempt == 1:
            assert observed[0][1] is observed[1][1]
            assert any(e.key == f'llm/baseline-{fixture.name}/0/review/1/2' for e in rig.journal.read())
        rig.time.advance(899.99)
        assert d.region == 'healthy'
        rig.time.advance(.01)
        assert d.region == 'soft'
        rig.time.advance(300)
        assert d.region == 'stuck'

    cases = diagnose.load_cases()[:2]
    assert all('## Time budget' not in case.ticket for case in cases)
    # The actual eval's second call sees the deadline clipped to 400 seconds.
    progress_calls = []
    def progress(line):
        progress_calls.append(line)
        if len(progress_calls) == 1:
            rig.time.advance(2000)
    report = await diagnose.run_eval(config=ctx.config, driver=ctx.driver, journal=rig.journal,
        cases=cases, clock=rig.time, report_path=tmp_path / 'diagnosis-report.json', progress=progress)
    assert len(report.cases) == 2
    for (req, d), case, budget in zip(observed[-2:], cases, (600, 400)):
        assert req.ticket == f'diagnose-eval-{case.name}' and req.worktree is None
        assert (d.expected_seconds, d.stuck_seconds, d.observe.fence) == (budget / 2, budget, ())
        assert any(e.key == f'llm/diagnose-eval-{case.name}/0/diagnose/1/1' for e in rig.journal.read())
        assert rig.checkout.config.state_dir / f'spools/diagnose-eval-{case.name}/1/diagnose/call-01/prompt.md' in rig.fs.files

    # Actual eval entry points keep their unchanged hard timeout even for silent calibration calls.
    for mode, budget in (('baseline', 1200), ('diagnosis', 400)):
        entered, cleaned = asyncio.Event(), asyncio.Event()
        target = f'baseline-{fixture.name}' if mode == 'baseline' else f'diagnose-eval-{cases[1].name}'
        async def hung(self, req, model, **kwargs):
            if req.ticket != target:
                return await events(self, req, model, **kwargs)
            observed.append((req, kwargs['consumer'].detector))
            kwargs['consumer'].consume({'type': 'assistant', 'message': {'id': 'message', 'content': [
                {'type': 'tool_use', 'id': 'hung-tool', 'name': 'Read', 'input': {}}]}})
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        monkeypatch.setattr(providers.CliAdapter, 'invoke', hung)
        if mode == 'baseline':
            task = asyncio.create_task(harness.run_baseline(config=ctx.config, driver=ctx.driver,
                journal=rig.journal, fixtures=[fixture], workspace=tmp_path))
        else:
            progress_calls.clear()
            task = asyncio.create_task(diagnose.run_eval(config=ctx.config, driver=ctx.driver,
                journal=rig.journal, cases=cases, clock=rig.time,
                report_path=tmp_path / 'diagnosis-timeout-report.json', progress=progress))
        await asyncio.wait_for(entered.wait(), 5)
        req, d = observed[-1]
        assert req.ticket == target and req.worktree is None and d.observe.fence == ()
        assert (d.expected_seconds, d.stuck_seconds) == (budget / 2, budget)
        assert d.call.tools == {'hung-tool'}
        rig.time.advance(budget * .75 - .01)
        await turn()
        assert d.region == 'healthy' and not task.done()
        rig.time.advance(.01)
        await turn()
        assert d.region == 'soft' and not task.done()
        rig.time.advance(budget / 4)
        result = await asyncio.wait_for(task, 5)
        assert cleaned.is_set() and d.region == 'stuck'
        if mode == 'baseline':
            assert result['scores']['unscored'] == [{'fixture': fixture.name, 'outcome': 'timeout'}]
            key = f'llm/{target}/0/review/3/1'
        else:
            assert result.cases[1].outcome == 'timeout'
            key = f'llm/{target}/0/diagnose/2/1'
        assert [e.type for e in rig.journal.read() if e.key == key] == [EventType.EFFECT_INTENT]


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
    write(root, 'work', text().replace('expected: 10m', 'expected: 1m').replace('stuck: 20m', 'stuck: 2m'))
    run = asyncio.create_task(rig.owner.run())
    writer = None
    try:
        await until(rig, connection.done)
        reader, writer = connection.result()
        ctx = rig.owner.writers['work'].ctx
        assert ctx.driver.llm._exec is ctx.exec_ is rig.exec
        assert ctx.driver.detector.call.tools == {'tool'}
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
