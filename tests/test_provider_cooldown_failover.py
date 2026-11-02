"""Activation evidence through the existing production composition and injected seams."""

import asyncio
import json
from collections import deque
from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import BaseModel

from tempo import __main__ as cli, driver as driver_module, runner, stages
from tempo.audit import audit_journal
from tempo.artifacts import Harvest, StageResult
from tempo.box import Box
from tempo.config import snapshot_config
from tempo.driver import LlmStage
from tempo.effects import Effects
from tempo.journal import EventType, render_ts, run_seq
from tempo.llmeffect import llm_call
from tempo.providers import ADAPTERS, CodexAdapter, ProviderLLM, ProviderSession, candidates
from tempo.redact import Redactor
from tempo.timers import Timers
from tempo.tickets import intake
from tests.test_cli import root, write
from tests.test_daemon_composition import CoreRig
from tests.test_providers import CONFIG
from tests.test_restart_timers import repository, held
from tests.test_scheduler import turn, text
from tests.test_serve import graph, until, finish, terminals
from tempo.checkout import Refusal, SPECS_DIR, call_timeout


class Reply(BaseModel):
    text: str


async def prepared(rig):
    checkout = replace(rig.checkout, control=rig.core.control)
    return await runner.prepare_pipeline(checkout)


def registry(root, monkeypatch):
    monkeypatch.setitem(ADAPTERS, 'backup', CodexAdapter)
    third = '''  - name: backup
    kind: cli
    package: scripted-backup
    models_by_tier: {low: b-low, medium: b-med, high: b-high, max: b-max}
    limits: {concurrency: 1}
'''
    config = CONFIG.replace('routing:', third + 'routing:').replace(
        'candidates: [{provider: codex}, {provider: claude}]',
        'candidates: [{provider: codex}, {provider: claude}, {provider: backup, model: b-pinned}]')
    return CoreRig(root, config=config)


async def stage(rig, driver, stem='work', surface='implement', attempt=0, stuck=1200):
    return await driver.run(LlmStage(surface=surface, emits=Reply, gates=[], render=lambda *_: 'probe'),
        None, ticket=stem, attempt=attempt, workspace=rig.root, tier='medium', effort='low',
        stuck_budget=stuck)


def outcomes(rig):
    return [e for e in rig.journal.read() if e.body.get('signal') == 'provider_call_outcome']


def fail_process(rig, monkeypatch, message):
    original = rig.exec.run
    async def run(argv, **kwargs):
        prompt = rig.fs.files.get(kwargs.get('stdin_path'), b'').decode()
        if argv[0] in {'claude', 'codex'} and prompt != 'Reply with the single word ok.':
            return 1, '', message
        return await original(argv, **kwargs)
    monkeypatch.setattr(rig.exec, 'run', run)


@pytest.mark.asyncio
async def test_quota_failure_arms_cooldown_without_infra_draw(root, monkeypatch):
    rig = await repository(root, monkeypatch)
    ticket = await rig.add('quota')
    await intake(root, rig.checkout.git, rig.journal, rig.fs)
    writer = await prepared(rig)
    fail_process(rig, monkeypatch, 'usage limit reached')
    lock = held(rig)
    try:
        rig.transition(ticket.stem, 'running')
        assert await writer(ticket) == 'infra_error'
    finally:
        lock.release()
    session = writer.ctx.driver.llm.session
    deadline = rig.time() + timedelta(minutes=60)
    [arm] = [e for e in rig.journal.read() if e.type == EventType.TIMER_ARMED]
    assert arm.body == {'timer_id': f'provider-cooldown/codex/{render_ts(deadline)}',
                        'deadline': render_ts(deadline)}
    assert arm.ticket is arm.key is None
    assert len(outcomes(rig)) == 1 and outcomes(rig)[0].body['failure_class'] == 'quota_exhausted'
    assert not any(e.type == EventType.CAP_CONSUMED for e in rig.journal.read())
    assert not writer.ctx.worktree(ticket.stem).exists()
    [terminal] = terminals(rig, ticket.stem)
    assert terminal.body['reason'] == 'quota_exhausted' and 'dispatch' not in terminal.body
    harvest = Harvest.model_validate_json((root / 'tickets/quota/attempts/0/harvest.json').read_text())
    assert 'usage limit reached' in harvest.stage_log_tail
    assert '- provider: codex\n- model: x-med' in (root / 'tickets/quota/run.md').read_text()
    assert len(Box(rig.checkout.config.state_dir / 'box', rig.fs).messages()) == 1
    assert session.cooling() == {'codex': deadline} and audit_journal(rig.journal) == []


@pytest.mark.asyncio
async def test_ordered_failover_preserves_served_identity(tmp_path, monkeypatch):
    rig = registry(tmp_path, monkeypatch)
    writer = await prepared(rig)
    driver, session = writer.ctx.driver, writer.ctx.driver.llm.session
    rig.exec.reply = '{"text":"done"}'
    session.quota(driver.llm._config.providers[1])
    result = await stage(rig, driver)
    assert result.outcome == 'ok' and (result.provider, result.model) == ('claude', 'c-med')
    assert driver.detector.call.identity == ('claude', 'c-med')
    completion = next(e for e in reversed(rig.journal.read()) if e.type == EventType.EFFECT_COMPLETION)
    assert completion.body['served'] == {'provider': 'claude', 'model': 'c-med'} and completion.body['result']['model'] == 'c-med'
    argv = rig.exec.calls[-1][0]
    assert argv[0] == 'claude' and argv[argv.index('--model') + 1] == 'c-med'
    result = await stage(rig, driver, surface='review')
    assert result.model == 'c-max' and driver.detector.call.identity == ('claude', 'c-max')
    for _ in range(3):
        session.thresh.record_outcome('claude', 'outage', ticket='work', call_key='opening')
    result = await stage(rig, driver, attempt=1)
    assert (result.provider, result.model) == ('backup', 'b-pinned')
    assert driver.detector.call.identity == ('backup', 'b-pinned')
    session.quota(driver.llm._config.providers[2])
    refusal = session.drought(driver.llm._config, 'medium', 'implement')
    assert refusal.record['providers'] == ['codex', 'claude', 'backup']
    rig.time.advance(600)
    result = await stage(rig, driver, attempt=2)
    assert result.provider == 'claude'


@pytest.mark.asyncio
async def test_ordered_failover_preserves_served_identity_after_invalid_reply(tmp_path, monkeypatch):
    rig = CoreRig(tmp_path)
    writer = await prepared(rig)
    driver, session = writer.ctx.driver, writer.ctx.driver.llm.session
    original = rig.exec.run
    async def run(argv, **kwargs):
        prompt = rig.fs.files.get(kwargs.get('stdin_path'), b'').decode()
        if prompt != 'Reply with the single word ok.':
            if argv[0] == 'codex':
                session.quota(driver.llm._config.providers[1])
                rig.exec.reply = 'invalid JSON'
            elif argv[0] == 'claude':
                return 1, '', 'service unavailable'
        return await original(argv, **kwargs)
    monkeypatch.setattr(rig.exec, 'run', run)
    result = await stage(rig, driver)
    assert result.outcome == 'infra_error'
    assert (result.provider, result.model) == driver.detector.call.identity == ('claude', 'c-med')
    assert result.findings[0].code == 'outage'
    assert [e.body['provider'] for e in outcomes(rig)] == ['codex', 'claude']


@pytest.mark.asyncio
async def test_all_candidates_cooling_recovers_on_timer_fired(tmp_path, monkeypatch):
    called = []
    async def drive(ctx, ticket):
        called.append(ticket.stem)
        return 'merged'
    monkeypatch.setattr(runner, 'drive', drive)
    rig = CoreRig(tmp_path)
    await prepared(rig)
    session = rig.core.control.providers
    for provider in rig.checkout.config.providers:
        session.quota(provider)
    ticket = await rig.add('dry')
    assert await rig.core.scheduler.dispatch_next() is ticket
    assert called == []
    [park] = terminals(rig, 'dry')
    assert park.body == {'to': 'infra_error', 'reason': 'provider_drought',
                        'provider_drought': {'tier': 'medium', 'surface': 'implement',
                                              'providers': ['codex', 'claude']}}
    assert run_seq(rig.journal.read(), 'dry') == 0 and audit_journal(rig.journal) == []
    assert not any(e.type in {EventType.CAP_CONSUMED, EventType.EFFECT_INTENT} for e in rig.journal.read())
    deadline = min(session.cooling().values())
    wait = asyncio.create_task(session.wait_until(deadline))
    await turn()
    rig.time.advance((deadline - rig.time()).total_seconds() - 1)
    await turn()
    assert not wait.done() and await rig.core.scheduler.dispatch_next() is None
    rig.time.advance(1)
    await wait
    assert len([e for e in rig.journal.read() if e.type == EventType.TIMER_FIRED]) == 2
    assert await rig.core.scheduler.dispatch_next() is ticket and called == ['dry']
    assert not session.waits


@pytest.mark.asyncio
@pytest.mark.parametrize('nested', [False, True])
async def test_mid_run_drought_is_harvested_without_spine_draws(root, monkeypatch, nested):
    rig = await repository(root, monkeypatch)
    ticket = await rig.add('midrun')
    await intake(root, rig.checkout.git, rig.journal, rig.fs)
    writer = await prepared(rig)
    session = writer.ctx.driver.llm.session
    rig.exec.reply = '{"text":"earlier evidence"}'
    async def implement(ctx, ticket, *, attempt):
        await stages.prepare_worktree(ctx, ticket.stem)
        prior = await stage(rig, ctx.driver, stem=ticket.stem)
        for provider in ctx.config.providers:
            session.quota(provider)
        return StageResult(outcome='ok', artifact=stages.PackingSlip(produced_by_spec_version=1,
            produced_at_sha='fixture', stem=ticket.stem, branch=ticket.stem, outcome='ok',
            summary='earlier', run_record='tickets/midrun/run.md'), findings=[], provider=prior.provider, model=prior.model)
    async def check(ctx, ticket, slip, *, attempt):
        if nested:
            from tempo.requisition import review_ticket
            from tests.test_requisition import TICKET
            await review_ticket(ctx.driver, repo=root, stem='seed', text=TICKET.replace('context.txt', 'config.yaml'),
                specs_dir=SPECS_DIR, tier='medium', stem_slot=ticket.stem,
                run_seq=0, attempt=attempt, call_seq=1)
        return await stage(rig, ctx.driver, stem=ticket.stem, surface='review')
    monkeypatch.setattr(stages, 'implement', implement)
    monkeypatch.setattr(stages, 'check', check)
    rig.transition(ticket.stem, 'running')
    assert await writer(ticket) == 'infra_error'
    [terminal] = terminals(rig, ticket.stem)
    assert terminal.body['provider_drought']['surface'] == ('requisition_review' if nested else 'review')
    assert terminal.body['reason'] == 'provider_drought' and 'dispatch' not in terminal.body
    assert not any(e.type == EventType.CAP_CONSUMED or e.body.get('signal') in {'diagnosis', 'reject_arrival'}
                   for e in rig.journal.read())
    assert not writer.ctx.worktree(ticket.stem).exists()
    assert (root / 'tickets/midrun/attempts/0/harvest.json').exists()
    assert 'earlier evidence' in rig.fs.files[rig.checkout.config.state_dir / 'spools/midrun/0/implement/call-01/output.txt'].decode()
    assert run_seq(rig.journal.read(), ticket.stem) == 1 and audit_journal(rig.journal) == []


@pytest.mark.asyncio
async def test_single_candidate_cooldown_resumes_same_provider(tmp_path):
    rig = CoreRig(tmp_path, config=CONFIG.replace('candidates: [{provider: codex}, {provider: claude}]',
                                                 'candidates: [{provider: codex}]'))
    writer = await prepared(rig)
    session = writer.ctx.driver.llm.session
    session.quota(rig.checkout.config.providers[1])
    assert session.drought(writer.ctx.config, 'medium', 'implement').record['providers'] == ['codex']
    rig.time.advance(3600)
    rig.exec.reply = '{"text":"done"}'
    result = await stage(rig, writer.ctx.driver)
    assert result.outcome == 'ok' and result.provider == 'codex'
    assert len([e for e in rig.journal.read() if e.type == EventType.TIMER_FIRED]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('advance', [3599, 3600, 3601])
async def test_cooldown_reconstructs_and_rearms_new_windows(tmp_path, monkeypatch, advance):
    rig = CoreRig(tmp_path)
    await prepared(rig)
    session = rig.core.control.providers
    provider = rig.checkout.config.providers[1]
    session.quota(provider)
    first = next(iter(session.timers.pending))
    rig.time.advance(1)
    session.quota(provider)
    assert list(session.timers.pending) == [first]
    rig.time.advance(advance - 1)
    timers = Timers(journal=rig.journal, clock=rig.time, sleep=rig.time.sleep)
    resumed = ProviderSession(rig.checkout.config, journal=rig.journal, clock=rig.time,
                              sleep=rig.time.sleep, timers=timers)
    assert ('codex' in resumed.cooling()) == (advance < 3600)
    rig.time.advance(3601)
    resumed.quota(provider)
    second = next(iter(timers.pending))
    assert second != first and len([e for e in rig.journal.read() if e.type == EventType.TIMER_ARMED]) == 2
    append = rig.journal.append
    def failed(kind, *args, **kwargs):
        if kind in {EventType.TIMER_ARMED, EventType.TIMER_FIRED}:
            raise OSError('injected append failure')
        return append(kind, *args, **kwargs)
    monkeypatch.setattr(rig.journal, 'append', failed)
    with pytest.raises(OSError):
        resumed.quota(rig.checkout.config.providers[0])
    assert list(timers.pending) == [second]
    rig.time.advance(3600)
    with pytest.raises(OSError):
        resumed.cooling()
    assert list(timers.pending) == [second] and second not in timers.fired
    monkeypatch.setattr(rig.journal, 'append', append)
    assert resumed.cooling() == {}


@pytest.mark.asyncio
async def test_one_provider_payload_and_timers_per_session(root, monkeypatch):
    from tempo.drain import drain
    seen = []
    async def drive(ctx, ticket, **kwargs):
        seen.append(ctx.driver.llm.session)
        ctx.exec_.reply = '{"text":"done"}'
        for surface in ('implement', 'review', 'rework', 'diagnose', 'requisition_review', 'triage', 'author', 'retro'):
            ticketless = surface in {'triage', 'author', 'retro'}
            result = await ctx.driver.run(
                LlmStage(surface=surface, emits=Reply, gates=[], render=lambda *_: 'probe'), None,
                ticket=None if ticketless else ticket.stem, attempt=0, workspace=ctx.repo,
                tier='medium', effort='low', stuck_budget=1200,
                run_seq=len(seen) if ticketless else None)
            assert result.outcome == 'ok'
            if surface == 'implement':
                assert result.model == ('x-med' if len(seen) == 1 else 'x-refreshed')
        if len(seen) == 1:
            ctx.fs.write(ctx.repo / 'config.yaml',
                         (ctx.repo / 'config.yaml').read_text().replace('x-med', 'x-refreshed').encode())
        ctx.driver.journal.append(EventType.STATE_TRANSITION, {'to': 'already_satisfied'}, ticket=ticket.stem)
        return 'already_satisfied'
    monkeypatch.setattr(runner, 'drive', drive)
    rig = await repository(root, monkeypatch)
    await rig.add('first')
    await rig.add('second')
    checkout = replace(rig.checkout, control=cli.build_control(rig.checkout))
    report = await drain(checkout, runner.pipeline(checkout))
    assert report.exit_code == 0 and len(seen) == 2 and seen[0] is seen[1] is checkout.control.providers
    assert not seen[0].waits
    rig.fs.publish = __import__('tempo.seams', fromlist=['LocalFileSystem']).LocalFileSystem().publish
    rig.owner = cli.build_serve(rig.checkout)
    write(root, 'continuous', text())
    running = asyncio.create_task(rig.owner.run())
    try:
        await until(rig, lambda: bool(terminals(rig, 'continuous')))
        assert seen[-1] is rig.owner.control.providers and seen[-1] is not seen[0]
        assert seen[-1].timers is rig.owner.core.restart.timers
        refreshed = []
        async def triage_pass(checkout, llm):
            assert llm.session is seen[-1] and checkout.config is llm._config
            assert llm._timeout == call_timeout(checkout.config)
            refreshed.append(checkout.config)
            monkeypatch.setattr(rig.owner.box, 'pending', lambda: [])
        monkeypatch.setattr(cli.triage, 'triage_pass', triage_pass)
        rig.fs.write(root / 'config.yaml', (root / 'config.yaml').read_text().replace('c-max', 'c-live').encode())
        monkeypatch.setattr(rig.owner.box, 'pending', lambda: [object()])
        await until(rig, lambda: bool(refreshed))
        assert refreshed[0].providers[0].models_by_tier.max == 'c-live'
        assert rig.owner.checkout.config.providers[0].models_by_tier.max == 'c-max'
        wait = asyncio.create_task(seen[-1].wait_until(rig.time() + timedelta(hours=1)))
        await turn()
    finally:
        await finish(rig, running)
    await asyncio.gather(wait, return_exceptions=True)
    assert not seen[-1].waits


@pytest.mark.asyncio
async def test_provider_wait_excluded_from_ticket_and_watchdog_budgets(tmp_path):
    rig = CoreRig(tmp_path)
    writer = await prepared(rig)
    driver, session = writer.ctx.driver, writer.ctx.driver.llm.session
    rig.exec.reply = '{"text":"done"}'
    route = candidates(driver.llm._config, 'medium', 'implement')
    held_slots = [await session.thresh.admit([served], ticket=None, call_key='occupier', projected_wait_seconds=0)
                  for served in route]
    queued = asyncio.create_task(stage(rig, driver, stuck=30))
    for _ in range(10):
        await turn()
    assert session.thresh.occupancy('codex') == (1, 1, 1)
    assert driver.detector.call is None
    assert not [e for e in rig.journal.read() if e.type == EventType.EFFECT_INTENT]
    rig.time.advance(100)
    await turn()
    assert not queued.done() and driver.detector.active_seconds == 0
    session.thresh.release(held_slots[0])
    for _ in range(10):
        rig.time.advance(0)
        await turn()
    result = await queued
    assert result.outcome == 'ok' and driver.detector.active_seconds == 0
    replay = await stage(rig, driver, stuck=30)
    assert replay.outcome == 'ok' and len(outcomes(rig)) == 1
    session.thresh.release(held_slots[1])
    entered = asyncio.Event()
    async def hung(argv):
        if argv[0] in {'codex', 'claude'}:
            entered.set()
            await asyncio.Event().wait()
    rig.exec.hook = hung
    rig.exec.kill_group = lambda pgid: None
    executing = asyncio.create_task(stage(rig, driver, attempt=1, stuck=30))
    await entered.wait()
    rig.time.advance(31)
    assert (await executing).outcome == 'timeout'
    assert session.thresh.occupancy('codex') == (0, 0, 1)
    rig.exec.hook = None
    reserved = [await session.thresh.admit([served], ticket=None, call_key='occupier', projected_wait_seconds=0)
                for served in route]
    cancelled = asyncio.create_task(stage(rig, driver, stem='cancelled', stuck=30))
    for _ in range(10):
        await turn()
    rig.time.advance(55)
    await turn()
    assert not cancelled.done() and driver.detector.active_seconds == 0
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    wait = next(e.body for e in reversed(rig.journal.read()) if e.body.get('signal') == 'provider_cap_wait')
    assert wait['disposition'] == 'cancelled' and wait['waited_seconds'] == 55
    assert driver.detector.active_seconds == 0 and not session.thresh._waiting
    for admission in reserved:
        session.thresh.release(admission)
    assert all(session.thresh.occupancy(s.provider.name) == (0, 0, 1) for s in route)


@pytest.mark.asyncio
@pytest.mark.parametrize('surface', ['implement', 'author'])
@pytest.mark.parametrize('used,queued,cap,timeout', [(0, 0, 2, 600), (1, 0, 2, 600),
    (2, 0, 2, 600), (0, 1, 2, 60), (1, 1, 2, 60), (2, 5, 2, 120)])
async def test_production_projected_wait_uses_shared_occupancy_and_timeout(tmp_path, monkeypatch, used, queued, cap, timeout, surface):
    rig = CoreRig(tmp_path, config=CONFIG.replace('concurrency: 1', f'concurrency: {cap}')
                  + f'drain: {{max_ticket_minutes: {timeout // 60}}}\n')
    writer = await prepared(rig)
    client = writer.ctx.driver.llm
    assert client._timeout == call_timeout(writer.ctx.config) == timeout
    primary = candidates(client._config, 'medium', surface)[0]
    slots = client.session.thresh._slots[primary.provider.name]
    slots.used = used
    slots.queue.extend(object() for _ in range(queued))
    seen = []
    admit = client.session.thresh.admit
    async def capture(route, **kwargs):
        seen.append(kwargs['projected_wait_seconds'])
        slots.used, slots.queue = 0, deque()
        return await admit(route, **kwargs)
    monkeypatch.setattr(client.session.thresh, 'admit', capture)
    rig.exec.reply = '{"text":"done"}'
    assert (await stage(rig, writer.ctx.driver, surface=surface)).outcome == 'ok'
    assert seen == [((used + queued) // cap) * timeout]


@pytest.mark.asyncio
@pytest.mark.parametrize('minutes,cap,earlier,spill', [(1, 2, True, False), (1, 1, False, False),
                                                   (2, 1, False, True)])
async def test_production_spill_preserves_strict_sixty_second_boundary(tmp_path, minutes, cap, earlier, spill):
    from tempo.thresh import _Waiter
    rig = CoreRig(tmp_path, config=CONFIG.replace('concurrency: 1', f'concurrency: {cap}')
                  + f'drain: {{max_ticket_minutes: {minutes}}}\n')
    writer = await prepared(rig)
    client = writer.ctx.driver.llm
    primary = candidates(client._config, 'medium', 'implement')[0]
    slots = client.session.thresh._slots['codex']
    if earlier:
        slots.queue.append(_Waiter())
    else:
        reserved = await client.session.thresh.admit([primary], ticket=None, call_key='occupier', projected_wait_seconds=0)
    rig.exec.reply = '{"text":"done"}'
    task = asyncio.create_task(stage(rig, writer.ctx.driver))
    for _ in range(12):
        await turn()
    if spill:
        assert task.done() and task.result().provider == 'claude'
    else:
        assert not task.done()
    if earlier:
        slots.queue.popleft()
        slots.wake()
    else:
        client.session.thresh.release(reserved)
    for _ in range(12):
        rig.time.advance(0)
        await turn()
    result = await task
    assert result.provider == ('claude' if spill else 'codex')


@pytest.mark.asyncio
async def test_auth_failure_excludes_provider_until_fresh_successful_preflight(tmp_path, monkeypatch):
    rig = CoreRig(tmp_path, config=CONFIG + 'notify: [report-auth]\n')
    alerts = []
    class NotificationProcess:
        async def run(self, argv, **kwargs):
            alerts.append(argv)
            return 0, 'delivered', ''
    monkeypatch.setattr(driver_module, 'SubprocessExec', NotificationProcess)
    writer = await prepared(rig)
    driver, session = writer.ctx.driver, writer.ctx.driver.llm.session
    fail_process(rig, monkeypatch, '401 unauthorized')
    result = await stage(rig, driver)
    assert result.findings[0].code == 'auth_error' and session.exclusions() == {'codex'}
    assert outcomes(rig)[0].body['open_until'] is None
    assert len(alerts) == 1 and 'run codex login' in alerts[0][-1]
    assert any(e.type == EventType.EFFECT_COMPLETION and e.key == 'notify/work/auth_error/codex'
               for e in rig.journal.read())
    session.thresh.record_outcome('codex', None, ticket='older', call_key='old-success')
    rig.time.advance(10000)
    assert session.exclusions() == {'codex'}
    monkeypatch.undo()
    rig.exec.reply = '{"text":"done"}'
    fresh = await prepared(rig)
    assert fresh.ctx.driver.llm.session is session
    result = await stage(rig, fresh.ctx.driver, attempt=1)
    assert result.provider == 'claude' and fresh.ctx.driver.detector.call.identity == ('claude', 'c-med')
    fail_process(rig, monkeypatch, 'not logged in')
    assert (await stage(rig, fresh.ctx.driver, surface='review', attempt=1)).findings[0].code == 'auth_error'
    assert session.exclusions() == {'codex', 'claude'}
    before = len(rig.journal.read())
    drought = await stage(rig, fresh.ctx.driver, stem=None, surface='author', attempt=2)
    assert drought.provider_drought == {'tier': 'medium', 'surface': 'author', 'providers': ['claude', 'codex']}
    assert not session.timers.pending
    assert not any(e.type in {EventType.EFFECT_INTENT, EventType.CAP_CONSUMED}
                   for e in rig.journal.read()[before:])
    monkeypatch.undo()
    checkout = replace(rig.checkout, control=cli.build_control(rig.checkout))
    rig.exec.refuse_probe = True
    with pytest.raises(Refusal, match='provider preflight failed'):
        await runner.prepare_pipeline(checkout)
    rig.exec.refuse_probe = False
    restored = await runner.prepare_pipeline(checkout)
    assert restored.ctx.driver.llm.session.exclusions() == set()
    result = await stage(rig, restored.ctx.driver, attempt=2)
    assert result.provider == 'codex'


@pytest.mark.asyncio
async def test_auth_failure_excludes_provider_until_fresh_successful_preflight_queued_head(tmp_path, monkeypatch):
    rig = CoreRig(tmp_path)
    first, second = await prepared(rig), await prepared(rig)
    session = rig.core.control.providers
    alternate = candidates(first.ctx.config, 'medium', 'implement')[1]
    reserved = await session.thresh.admit([alternate], ticket=None, call_key='occupier', projected_wait_seconds=0)
    entered, release = asyncio.Event(), asyncio.Event()
    original = rig.exec.run
    async def run(argv, **kwargs):
        prompt = rig.fs.files.get(kwargs.get('stdin_path'), b'').decode()
        if argv[0] == 'codex' and prompt != 'Reply with the single word ok.':
            entered.set()
            await release.wait()
            return 1, '', '401 unauthorized'
        return await original(argv, **kwargs)
    monkeypatch.setattr(rig.exec, 'run', run)
    rig.exec.reply = '{"text":"done"}'
    executing = asyncio.create_task(stage(rig, first.ctx.driver, stem='first'))
    await entered.wait()
    queued = asyncio.create_task(stage(rig, second.ctx.driver, stem='queued'))
    for _ in range(10):
        await turn()
    assert session.thresh.occupancy('codex') == (1, 1, 1)
    rig.time.advance(13)
    release.set()
    assert (await executing).findings[0].code == 'auth_error'
    for _ in range(20):
        rig.time.advance(0)
        await turn()
    assert session.thresh.occupancy('codex') == (0, 0, 1)
    assert session.thresh.occupancy('claude') == (1, 1, 1) and not queued.done()
    waits = [e.body for e in rig.journal.read() if e.body.get('signal') == 'provider_cap_wait']
    assert len(waits) == 1 and waits[0]['disposition'] == 'unavailable' and waits[0]['waited_seconds'] == 13
    assert not any(e.type == EventType.EFFECT_INTENT and e.ticket == 'queued' for e in rig.journal.read())
    rig.time.advance(7)
    session.thresh.release(reserved)
    for _ in range(20):
        rig.time.advance(0)
        await turn()
    result = await queued
    assert result.provider == 'claude' and second.ctx.driver.detector.call.identity == ('claude', 'c-med')
    assert second.ctx.driver.detector.active_seconds == 0 and len(outcomes(rig)) == 2
    assert all(session.thresh.occupancy(name) == (0, 0, 1) for name in ('claude', 'codex'))
    assert session.exclusions() == {'codex'}


@pytest.mark.asyncio
async def test_auth_failure_excludes_provider_until_fresh_successful_preflight_failed_append(tmp_path, monkeypatch):
    rig = CoreRig(tmp_path)
    writer = await prepared(rig)
    session = rig.core.control.providers
    append = rig.journal.append
    def failed(kind, body, **kwargs):
        if body.get('signal') == 'provider_call_outcome':
            raise OSError('injected outcome append failure')
        return append(kind, body, **kwargs)
    monkeypatch.setattr(rig.journal, 'append', failed)
    fail_process(rig, monkeypatch, '401 unauthorized')
    assert (await stage(rig, writer.ctx.driver)).outcome == 'infra_error'
    assert session.exclusions() == set() and outcomes(rig) == []
    assert session.thresh.occupancy('codex') == (0, 0, 1)


@pytest.mark.asyncio
async def test_provider_wait_excluded_from_ticket_and_watchdog_budgets_ticketless_cooldown(tmp_path):
    rig = CoreRig(tmp_path)
    writer = await prepared(rig)
    session = rig.core.control.providers
    for provider in rig.checkout.config.providers:
        session.quota(provider)
    rig.exec.reply = '{"text":"done"}'
    pending = asyncio.create_task(stage(rig, writer.ctx.driver, stem=None, surface='author', stuck=30))
    for _ in range(10):
        await turn()
    assert session.waits and writer.ctx.driver.detector.call is None
    assert not any(e.type == EventType.EFFECT_INTENT for e in rig.journal.read())
    rig.time.advance(3600)
    result = await pending
    assert result.outcome == 'ok' and result.provider == 'claude'
    assert writer.ctx.driver.detector.active_seconds == 0
    assert not session.waits


@pytest.mark.asyncio
@pytest.mark.parametrize('message,kind', [('rate limit', 'rate_limited'), ('quota exhausted', 'quota_exhausted'),
    ('service unavailable', 'outage'), ('401 unauthorized', 'auth_error'),
    ('model not found', 'model_error'), ('unknown provider incident', 'unclassified')])
async def test_executed_failures_preserve_classification_and_infra_accounting(root, monkeypatch, message, kind):
    rig = await repository(root, monkeypatch)
    ticket = await rig.add('failure')
    await intake(root, rig.checkout.git, rig.journal, rig.fs)
    writer = await prepared(rig)
    fail_process(rig, monkeypatch, message)
    rig.transition(ticket.stem, 'running')
    assert await writer(ticket) == 'infra_error'
    first = outcomes(rig)[0]
    assert first.body['failure_class'] == kind
    caps = [e for e in rig.journal.read() if e.type == EventType.CAP_CONSUMED and e.body['cap'] == 'infra']
    assert len(caps) == (0 if kind == 'quota_exhausted' else 1)
    harvest = Harvest.model_validate_json((root / 'tickets/failure/attempts/0/harvest.json').read_text())
    assert message in harvest.stage_log_tail and len(harvest.stage_log_tail) <= 4000
    assert bool(harvest.findings) == (kind != 'unclassified')
    assert not writer.ctx.worktree(ticket.stem).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['drain', 'core', 'serve', 'run'])
@pytest.mark.parametrize('cooling', ['codex', 'claude'])
async def test_escalated_drought_uses_effective_route(root, monkeypatch, entry, cooling):
    from tempo import caps
    from tempo.config import load_config
    from tempo.drain import drain

    rig = await repository(root, monkeypatch)
    config = (root / 'config.yaml').read_text().replace(
        'candidates: [{provider: codex}, {provider: claude}]', 'candidates: [{provider: codex}]')
    rig.put_config(config)
    rig.checkout = replace(rig.checkout, config=load_config(None, cwd=root))
    if entry == 'serve':
        from tempo.seams import LocalFileSystem
        rig.fs.publish = LocalFileSystem().publish
        rig.owner = cli.build_serve(rig.checkout)
        control = rig.owner.control
    else:
        control = rig.core.control
    checkout = replace(rig.checkout, control=control)
    ticket = await rig.add('escalated')
    assert ticket.frontmatter.agent_tier == 'medium'
    caps.consume(rig.journal, ticket.stem, 'retry', 'fixture', rung={'tier': 'high', 'effort': 'high'})
    await runner.prepare_pipeline(checkout)
    session = control.providers
    session.quota(next(p for p in checkout.config.providers if p.name == cooling))
    calls = []

    async def drive(ctx, original, **kwargs):
        tier, effort = caps.capability(original, ctx.driver.journal.read())
        if not any(e.ticket == original.stem and e.body.get('to') == 'running' for e in rig.journal.read()):
            rig.transition(original.stem, 'running')
        rig.exec.reply = '{"text":"served"}'
        result = await ctx.driver.run(
            LlmStage(surface='implement', emits=Reply, gates=[], render=lambda *_: 'probe'), None,
            ticket=original.stem, attempt=run_seq(rig.journal.read(), original.stem),
            workspace=root, tier=tier, effort=effort, stuck_budget=1200)
        assert result.outcome == 'ok' and (result.provider, result.model) == ('claude', 'c-high')
        calls.append(original.stem)
        rig.transition(original.stem, 'already_satisfied')
        return 'already_satisfied'

    monkeypatch.setattr(runner, 'drive', drive)
    if entry == 'drain':
        task = asyncio.create_task(drain(checkout, runner.pipeline(checkout)))
    elif entry == 'serve':
        task = asyncio.create_task(rig.owner.run())
    elif entry == 'run':
        code = await runner.run_ticket(ticket.stem, checkout, runner.pipeline(checkout))
    else:
        assert await rig.core.scheduler.dispatch_next() is ticket

    if cooling == 'claude':
        await until(rig, lambda: any('provider_drought' in e.body for e in rig.journal.read()))
        [park] = [e for e in rig.journal.read() if 'provider_drought' in e.body]
        assert park.body['provider_drought'] == {'tier': 'high', 'surface': 'implement', 'providers': ['claude']}
        assert calls == [] and run_seq(rig.journal.read(), ticket.stem) == 0
        assert not any(e.body.get('to') == 'running' or e.type == EventType.EFFECT_INTENT
                       for e in rig.journal.read())
        if entry == 'core':
            assert await rig.core.scheduler.dispatch_next() is None
        rig.time.advance(3600)
        if entry == 'core':
            assert await rig.core.scheduler.dispatch_next() is ticket
        elif entry == 'run':
            assert code == runner.EXIT_TICKET
            code = await runner.run_ticket(ticket.stem, checkout, runner.pipeline(checkout))

    if entry in {'drain', 'serve'}:
        try:
            await until(rig, lambda: bool(calls))
            if entry == 'drain':
                await until(rig, task.done)
                assert (await task).exit_code == 0
        finally:
            if entry == 'serve':
                await finish(rig, task)
            elif not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    elif entry == 'run':
        assert code == runner.EXIT_TICKET
    assert calls == [ticket.stem]
    assert bool([e for e in rig.journal.read() if 'provider_drought' in e.body]) == (cooling == 'claude')
    assert audit_journal(rig.journal) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['ceiling', 'kill', 'pause-then-kill'])
async def test_drain_drought_wait_observes_runtime_and_control(root, monkeypatch, ending):
    from tempo.config import load_config
    from tempo.control import ControlRequest, publish_request
    from tempo.drain import drain
    from tempo.seams import LocalFileSystem

    rig = await repository(root, monkeypatch)
    rig.put_config((root / 'config.yaml').read_text() + 'drain: {max_runtime_hours: 1}\n')
    rig.checkout = replace(rig.checkout, config=load_config(None, cwd=root))
    await rig.add('waiting')
    rig.fs.publish = LocalFileSystem().publish
    sleeping, checkpoints = [], []
    started = rig.time()
    deadline = started + timedelta(hours=1)

    async def sleep(seconds):
        sleeping.append(seconds)
        assert 0 < seconds <= 0.1  # A quota window must not hide control or the drain ceiling.
        rig.time.advance(seconds)
        if ending != 'ceiling':
            verb = 'pause' if ending == 'pause-then-kill' and len(sleeping) == 1 else 'kill'
            publish_request(checkout.config.state_dir, ControlRequest(
                f'{verb}-{len(sleeping)}', checkout.control.inbox.lifecycle_id, verb, None), rig.fs)
        await turn()

    checkout = replace(rig.checkout, sleep=sleep, control=None)
    checkout = replace(checkout, control=cli.build_control(checkout))
    session = checkout.control.providers
    for provider in checkout.config.providers:
        session.quota(provider.model_copy(update={'limits': provider.limits.model_copy(
            update={'quota_window_minutes': 120})}))
    pending = dict(session.timers.pending)

    async def checkpoint():
        checkpoints.append(rig.time())
        if ending == 'ceiling' and len(checkpoints) == 1:
            rig.time.advance(3599.95)

    report = await drain(checkout, runner.pipeline(checkout),
                         before_dispatch=checkpoint)
    events = rig.journal.read()
    [park] = [e for e in events if 'provider_drought' in e.body]
    assert park.ticket == 'waiting' and run_seq(events, 'waiting') == 0
    assert not any(e.body.get('to') == 'running' or e.type in {
        EventType.EFFECT_INTENT, EventType.CAP_CONSUMED, EventType.TIMER_FIRED} for e in events)
    assert session.timers.pending == pending and not session.waits
    assert sleeping and len(checkpoints) >= 2
    if ending == 'ceiling':
        assert report.halted == '1h elapsed' and not report.killed and rig.time() == deadline
        assert sleeping == [pytest.approx(0.05)]
        [halt] = [e for e in events if e.body.get('signal') == 'drain_halted']
        assert halt.ts == render_ts(deadline)
    else:
        assert report.killed and report.halted is None and rig.time() < deadline
        accepted = [e.body['verb'] for e in events if e.body.get('kind') == 'control_decision'
                    and e.body['decision'] == 'accepted']
        assert accepted == (['pause', 'kill'] if ending == 'pause-then-kill' else ['kill'])
        assert sum(e.body.get('kind') == 'kill_applied' for e in events) == 1
    assert json.loads((checkout.config.state_dir / 'control/active.json').read_text()) is None
    assert audit_journal(rig.journal) == []
    lock = held(rig)
    lock.release()
