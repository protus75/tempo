"""Regression custody and admission proofs with disposable real Git repositories."""

import asyncio
import dataclasses
import json
from pathlib import Path

import pytest

from tempo import merge, stages
from tempo.gates import DEFAULT_GATE_SEVERITY
from tempo.mergequeue import MergeQueue
from tempo.seams import ExecutableNotFound
from tempo.tickets import validate_ticket
from tests.test_mergequeue import approve, commit, ctx, ready
from tests.test_stages import SECRET, STEM, TICKET

run = asyncio.run
PRODUCER = "tests/reproducer.py"
WRAPPER = '''import json
import sys
from pathlib import Path
try:
    text = Path('tempo/thing.py').read_text()
except OSError:
    print(json.dumps({'regression_result': 'error'}))
    sys.exit(1)
try:
    assert 'fixed' in text
except AssertionError:
    print(json.dumps({'regression_result': 'defect'}))
    sys.exit(1)
print(json.dumps({'regression_result': 'pass'}))
'''


async def bug_ready(ctx, *, wrapper=WRAPPER, carries=PRODUCER, custody=True, bypass=False):
    text = TICKET.format(bypass=(
        'gate_bypass: [{code: bug_evidence, reason: reviewed exception}]\n' if bypass else '')
    ).replace('kind: feature', 'kind: bug').replace(
        '## Scope fence\n- tempo/thing.py', '## Scope fence\n- tempo/thing.py\n- tests/').replace(
        '## Time budget', f'## Regression\n```\npython3 {PRODUCER}\n```\n- carries: {carries}\n\n## Time budget')
    path = ctx.repo / f'tickets/{STEM}'
    ctx.fs.write(path / 'ticket.md', text.encode())
    ctx.fs.write(path / 'run.md', ''.join(
        f"## {name}\n\n{'ok' if name == 'Outcome' else 'none'}\n\n"
        for name in stages.RUN_RECORD_SECTIONS).encode())
    if custody:
        ctx.fs.write(path / 'evidence/defect.txt', b'observed defective add\n')
    await commit(ctx, ctx.repo, [f'tickets/{STEM}'], 'bug intake')
    ticket = dataclasses.replace(validate_ticket(STEM, text, ctx.repo), verification=(("true",),))
    await ctx.git.worktree_add(ctx.repo, ctx.worktree(STEM), STEM, 'main')
    ctx.fs.write(ctx.worktree(STEM) / 'tempo/thing.py', b'ok fixed\n')
    ctx.fs.write(ctx.worktree(STEM) / PRODUCER, wrapper.encode())
    await commit(ctx, ctx.worktree(STEM), ['tempo/thing.py', PRODUCER], 'fix and new regression')
    await approve(ctx, ticket)
    return ticket


async def collect(ctx, ticket, *, stage='check'):
    evidence = await stages.gather_evidence(ctx, ticket, 'ok', attempt=0, stage=stage)
    report = stages.BugEvidenceGate().check(evidence, ctx.worktree(ticket.stem))
    return evidence, report


async def admit(ctx, ticket, mode):
    if mode == 'inline':
        return await merge.merge(ctx, ticket, attempt=0)
    queue = MergeQueue(ctx, escalate=lambda _: None)
    queue.offer(ticket, attempt=0)
    [result] = await queue.process()
    return result


async def checked(ctx, ticket):
    slip = stages.PackingSlip(produced_by_spec_version=1,
                              produced_at_sha=await ctx.git.rev_parse(ctx.repo, STEM),
                              stem=STEM, branch=STEM, outcome='ok', summary='fixed',
                              run_record=f'tickets/{STEM}/run.md')
    return await stages.check(ctx, ticket, slip, attempt=0)


def test_regression_overlay_proves_defect(ctx):
    async def scenario():
        ticket = await bug_ready(ctx)
        evidence, report = await collect(ctx, ticket)
        assert report.verdict == 'pass'
        record = report.bug_evidence
        assert record.head.rc == 0 and record.head.regression_result == 'pass'
        assert record.base.rc == 1 and record.base.regression_result == 'defect'
        assert record.argv == ['python3', PRODUCER] and record.overlay_paths == [PRODUCER]
        assert record.head_sha == evidence.head_sha
        assert record.base_sha == await ctx.git.merge_base(ctx.repo, 'main', STEM)
        assert not (ctx.repo / PRODUCER).exists()
        assert not (ctx.config.worktree_root / '.regression-base' / STEM).exists()
    run(scenario())


def test_passing_new_test_is_not_bug_evidence(ctx):
    async def scenario():
        ticket = await bug_ready(ctx, wrapper="print('{\"regression_result\":\"pass\"}')\n")
        _, report = await collect(ctx, ticket)
        assert report.verdict == 'fail' and report.bug_evidence.base.rc == 0
        assert all(f.code == 'bug_evidence' and f.paved_road for f in report.findings)
    run(scenario())


def test_missing_base_input_is_not_bug_evidence(ctx):
    async def scenario():
        ticket = await bug_ready(ctx, wrapper=WRAPPER.replace('tempo/thing.py', 'tempo/input.txt'))
        ctx.fs.write(ctx.worktree(STEM) / 'tempo/input.txt', b'fixed\n')
        await commit(ctx, ctx.worktree(STEM), ['tempo/input.txt'])
        _, report = await collect(ctx, ticket)
        assert report.verdict == 'fail'
        assert report.bug_evidence.head.regression_result == 'pass'
        assert report.bug_evidence.base.rc == 1 and report.bug_evidence.base.regression_result == 'error'
    run(scenario())


@pytest.mark.parametrize('stdout,rc,decoded', [
    (' {"regression_result":"pass"} \n', 0, 'pass'),
    ('{"regression_result":"defect"}', 7, 'defect'),
    ('{"regression_result":"error"}', 1, 'error'),
    ('', 1, None), ('not json', 1, None),
    ('{"regression_result":"defect"}' + ' ' * stages.OUTPUT_TAIL_CHARS, 1, None),
    ('{"regression_result":"defect","regression_result":"defect"}', 1, None),
    ('{"regression_result":"defect","extra":true}', 1, None),
    ('{"regression_result":"defect"} {}', 1, None),
    ('{"regression_result":"unknown"}', 1, None),
    ('{"regression_result":1}', 1, None), ('[]', 1, None),
    ('{"regression_result":"defect"}', -9, None),
    ('{"regression_result":"pass"}', 1, None),
    ('{"regression_result":"defect"}', 0, None),
])
def test_bug_gate_result_protocol(ctx, monkeypatch, stdout, rc, decoded):
    async def scenario():
        ticket = await bug_ready(ctx)
        original = ctx.exec_.run
        async def execute(argv, **kw):
            if argv == list(ticket.regression):
                return (0, '{"regression_result":"pass"}', '') if kw['cwd'] == ctx.worktree(STEM) else (rc, stdout, '')
            return await original(argv, **kw)
        monkeypatch.setattr(ctx.exec_, 'run', execute)
        _, report = await collect(ctx, ticket)
        assert report.bug_evidence.base.regression_result == decoded
        assert report.verdict == ('pass' if decoded == 'defect' else 'fail')
    run(scenario())


@pytest.mark.parametrize('value', ['defect', 'error', None])
def test_bug_gate_indistinguishable_failures(ctx, monkeypatch, value):
    async def scenario():
        ticket = await bug_ready(ctx)
        original = ctx.exec_.run
        async def execute(argv, **kw):
            if argv == list(ticket.regression):
                if kw['cwd'] == ctx.worktree(STEM):
                    return 0, '{"regression_result":"pass"}', ''
                return 1, json.dumps({'regression_result': value}) if value else '', 'identical diagnostic'
            return await original(argv, **kw)
        monkeypatch.setattr(ctx.exec_, 'run', execute)
        _, report = await collect(ctx, ticket)
        assert report.bug_evidence.base.rc == 1
        assert report.bug_evidence.base.tail.endswith('identical diagnostic')
        assert report.verdict == ('pass' if value == 'defect' else 'fail')
    run(scenario())


@pytest.mark.parametrize('error', ['missing-test', 'setup', 'collection', 'child'])
def test_bug_gate_wrapper_execution_errors(ctx, error):
    # The wrapper validates prerequisites and refuses unrelated errors before the defect assertion.
    scripts = {
        'missing-test': "from pathlib import Path; Path('tests/absent.py').read_text()",
        'setup': "raise RuntimeError('setup failed')",
        'collection': "compile('invalid python !', 'test.py', 'exec')",
        'child': "import subprocess; subprocess.run(['python3', '-c', 'raise RuntimeError()'], check=True)",
    }
    wrapper = f"try:\n    {scripts[error]}\nexcept Exception:\n    print('{{\"regression_result\":\"error\"}}')\n    raise SystemExit(1)\n" + WRAPPER
    async def scenario():
        ticket = await bug_ready(ctx, wrapper=wrapper)
        _, report = await collect(ctx, ticket)
        assert report.verdict == 'fail'
        assert report.bug_evidence.head.regression_result == report.bug_evidence.base.regression_result == 'error'
    run(scenario())


def test_bug_gate_requires_evidence(ctx):
    async def scenario():
        ticket = await bug_ready(ctx, custody=False)
        _, report = await collect(ctx, ticket)
        assert report.verdict == 'fail' and report.bug_evidence.head is None
        assert 'durable evidence' in report.findings[0].message
        safety = await stages.gather_safety_evidence(ctx, ticket, 'ok')
        assert stages.BugEvidenceGate().check(safety, ctx.repo).verdict == 'fail'
    run(scenario())


def test_bug_gate_rejects_red_head(ctx):
    async def scenario():
        ticket = await bug_ready(ctx)
        ctx.fs.write(ctx.worktree(STEM) / 'tempo/thing.py', b'ok broken\n')
        await commit(ctx, ctx.worktree(STEM), ['tempo/thing.py'])
        _, report = await collect(ctx, ticket)
        assert report.verdict == 'fail' and report.bug_evidence.head.regression_result == 'defect'
    run(scenario())


@pytest.mark.parametrize('carries', ['tests/absent', '../tests', '/tests', 'tempo/', '.', 'tests/link.py'])
def test_bug_gate_refuses_invalid_carries(ctx, carries):
    async def scenario():
        ticket = await bug_ready(ctx)
        if carries == 'tests/link.py':
            (ctx.worktree(STEM) / carries).symlink_to('../tempo/thing.py')
            await commit(ctx, ctx.worktree(STEM), [carries])
        ticket = dataclasses.replace(ticket, carries=(carries,))
        _, report = await collect(ctx, ticket)
        assert report.verdict == 'fail' and report.bug_evidence.head is None
        assert report.findings and all(f.paved_road for f in report.findings)
    run(scenario())


@pytest.mark.parametrize('error', [ExecutableNotFound('regression'), TimeoutError(), OSError('spawn')])
@pytest.mark.parametrize('where', ['head', 'base'])
def test_bug_gate_execution_failure_is_closed(ctx, monkeypatch, error, where):
    async def scenario():
        ticket = await bug_ready(ctx)
        original = ctx.exec_.run
        async def execute(argv, **kw):
            if argv == list(ticket.regression) and (kw['cwd'] == ctx.worktree(STEM)) == (where == 'head'):
                raise error
            return await original(argv, **kw)
        monkeypatch.setattr(ctx.exec_, 'run', execute)
        _, report = await collect(ctx, ticket)
        assert report.verdict == 'fail'
        assert getattr(report.bug_evidence, where).rc is None
        assert not (ctx.config.worktree_root / '.regression-base' / STEM).exists()
    run(scenario())


@pytest.mark.parametrize('case', ['valid', 'red-head', 'missing', 'non-bug'])
def test_bug_gate_check_wiring(ctx, monkeypatch, case):
    async def scenario():
        bug = case != 'non-bug'
        ticket = await bug_ready(ctx, custody=case != 'missing') if bug else await ready(ctx)
        if case == 'red-head':
            ctx.fs.write(ctx.worktree(STEM) / 'tempo/thing.py', b'ok broken\n')
            await commit(ctx, ctx.worktree(STEM), ['tempo/thing.py'])
        original = ctx.exec_.run
        calls = []
        async def execute(argv, **kw):
            if argv[0] == 'python3':
                calls.append(argv)
            return await original(argv, **kw)
        monkeypatch.setattr(ctx.exec_, 'run', execute)
        result = await checked(ctx, ticket)
        failed = case in ('red-head', 'missing')
        assert result.outcome == ('gate_failed' if failed else 'ok')
        invoice = stages.Invoice.model_validate_json((ctx.repo / f'tickets/{STEM}/checks.json').read_bytes())
        [report] = [r for r in invoice.reports if r.code == 'bug_evidence']
        assert report.verdict == ('fail' if failed else 'pass')
        assert DEFAULT_GATE_SEVERITY['bug_evidence'] == 'hard'
        assert len(calls) == (2 if case in ('valid', 'red-head') else 0)
        assert (report.bug_evidence is not None) == bug
        if case == 'valid':
            assert report.bug_evidence.head.regression_result == 'pass'
            assert report.bug_evidence.base.regression_result == 'defect'
    run(scenario())


@pytest.mark.parametrize('failure', [False, True])
def test_bug_gate_base_cleanup(ctx, monkeypatch, failure):
    async def scenario():
        ticket = await bug_ready(ctx)
        branch = await ctx.git.rev_parse(ctx.repo, STEM)
        before = await ctx.git.status_porcelain(ctx.worktree(STEM))
        original = ctx.exec_.run
        calls = []
        async def execute(argv, **kw):
            calls.append((argv, kw))
            if argv == list(ticket.regression):
                assert 'FAKE_KEY' not in kw['env'] and kw['env']['PATH'] == ctx.env['PATH']
                assert kw['timeout'] == ticket.stuck_minutes * 60.0
                if failure and kw['cwd'] != ctx.worktree(STEM):
                    raise TimeoutError(SECRET)
                rc, out, _ = await original(argv, **kw)
                return rc, out, SECRET
            return await original(argv, **kw)
        monkeypatch.setattr(ctx.exec_, 'run', execute)
        _, report = await collect(ctx, ticket, stage='merge-integration')
        assert report.verdict == ('fail' if failure else 'pass')
        assert await ctx.git.rev_parse(ctx.repo, STEM) == branch
        assert await ctx.git.status_porcelain(ctx.worktree(STEM)) == before
        assert (ctx.repo / 'tempo/thing.py').read_text() == 'ok base\n'
        assert not (ctx.repo / PRODUCER).exists()
        assert not (ctx.config.worktree_root / '.regression-base' / STEM).exists()
        operations = [argv[3:] for argv, _ in calls if argv[0] == 'git']
        assert any(op[:3] == ['worktree', 'add', '--detach'] for op in operations)
        assert any(op[:2] == ['worktree', 'remove'] for op in operations)
        assert ['worktree', 'prune'] in operations
        for name in ('head', 'base'):
            text = (ctx.config.state_dir / f'spools/{STEM}/0/merge-integration/regression-{name}.txt').read_text()
            assert SECRET not in text and 'stdout' in text and 'stderr' in text
        assert SECRET not in report.model_dump_json()
    run(scenario())


def test_bug_gate_capture_stays_bounded_after_scrubbing(ctx, monkeypatch):
    from tempo.redact import Redactor
    async def scenario():
        ticket = await bug_ready(ctx)
        monkeypatch.setattr(ctx.driver, 'redactor', Redactor({'short_key': 'x'}))
        original = ctx.exec_.run
        async def execute(argv, **kw):
            rc, out, err = await original(argv, **kw)
            return (rc, out, 'x' * 4000) if argv == list(ticket.regression) else (rc, out, err)
        monkeypatch.setattr(ctx.exec_, 'run', execute)
        _, report = await collect(ctx, ticket)
        assert report.verdict == 'pass'
        for result in (report.bug_evidence.head, report.bug_evidence.base):
            assert len(result.tail) <= stages.OUTPUT_TAIL_CHARS and 'x' not in result.tail
    run(scenario())


@pytest.mark.parametrize('mode', ['inline', 'queue'])
@pytest.mark.parametrize('case', ['valid', 'red-head', 'green-base', 'non-bug'])
def test_bug_gate_merge_wiring(ctx, monkeypatch, mode, case):
    async def scenario():
        ticket = await ready(ctx) if case == 'non-bug' else await bug_ready(ctx)
        check_path = ctx.repo / f'tickets/{STEM}/checks.json'
        assert (await checked(ctx, ticket)).outcome == 'ok'
        check_bytes = check_path.read_bytes()
        original = ctx.exec_.run
        calls = []
        async def execute(argv, **kw):
            if argv == ['python3', PRODUCER]:
                calls.append(kw['cwd'])
                if case == 'red-head' and kw['cwd'] == ctx.worktree(STEM):
                    return 1, '{"regression_result":"defect"}', ''
                if case == 'green-base' and kw['cwd'] != ctx.worktree(STEM):
                    return 0, '{"regression_result":"pass"}', ''
            return await original(argv, **kw)
        monkeypatch.setattr(ctx.exec_, 'run', execute)
        before = await ctx.git.rev_parse(ctx.repo, 'main')
        result = await admit(ctx, ticket, mode)
        if case in ('red-head', 'green-base'):
            assert result.outcome == 'gate_failed' and any(f.code == 'bug_evidence' for f in result.findings)
            assert await ctx.git.rev_parse(ctx.repo, 'main') == before
        else:
            assert result.outcome == 'ok', result.findings
            assert await ctx.git.rev_parse(ctx.repo, 'main') != before
        assert len(calls) == (0 if case == 'non-bug' else 2)
        assert check_path.read_bytes() == check_bytes
    run(scenario())


@pytest.mark.parametrize('mode', ['inline', 'queue'])
def test_bug_gate_merge_rebase_regates(ctx, monkeypatch, mode):
    async def scenario():
        ticket = await bug_ready(ctx)
        _, prior = await collect(ctx, ticket)
        assert prior.verdict == 'pass'
        # Main has acquired the fix. A fresh base now passes even though Check had proved a defect.
        ctx.fs.write(ctx.repo / 'tempo/thing.py', b'ok fixed\n')
        await commit(ctx, ctx.repo, ['tempo/thing.py'], 'main independently fixed')
        before = await ctx.git.rev_parse(ctx.repo, 'main')
        captured = []
        original = stages.BugEvidenceGate.check
        def checked(gate, evidence, workspace):
            report = original(gate, evidence, workspace)
            captured.append(report)
            return report
        monkeypatch.setattr(stages.BugEvidenceGate, 'check', checked)
        result = await admit(ctx, ticket, mode)
        assert result.outcome == 'gate_failed' and any(f.code == 'bug_evidence' for f in result.findings)
        assert await ctx.git.rev_parse(ctx.repo, 'main') == before
        [report] = captured
        assert report.bug_evidence.base_sha == before
        assert report.bug_evidence.head_sha == await ctx.git.rev_parse(ctx.repo, STEM)
        assert report.bug_evidence.head_sha != prior.bug_evidence.head_sha
        assert report.bug_evidence.base.regression_result == 'pass'
    run(scenario())


@pytest.mark.parametrize('mode', ['inline', 'queue'])
@pytest.mark.parametrize('bypass', [False, True])
def test_bug_gate_merge_report_custody(ctx, monkeypatch, mode, bypass):
    async def scenario():
        ticket = await bug_ready(ctx, custody=False, bypass=bypass)
        path = ctx.repo / f'tickets/{STEM}/checks.json'
        ctx.fs.write(path, b'CHECK REPORT MUST NOT BE READ OR REWRITTEN')
        await commit(ctx, ctx.repo, [f'tickets/{STEM}/checks.json'])
        read = Path.read_text
        def guarded(p, *args, **kw):
            if p.name == 'checks.json':
                pytest.fail('merge consumed Check report')
            return read(p, *args, **kw)
        monkeypatch.setattr(Path, 'read_text', guarded)
        before = await ctx.git.rev_parse(ctx.repo, 'main')
        result = await admit(ctx, ticket, mode)
        assert result.outcome == ('ok' if bypass else 'gate_failed'), result.findings
        assert (await ctx.git.rev_parse(ctx.repo, 'main') == before) != bypass
        assert path.read_bytes() == b'CHECK REPORT MUST NOT BE READ OR REWRITTEN'
        assert any(f.code == 'bug_evidence' for f in result.findings)
    run(scenario())
