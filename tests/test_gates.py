import textwrap
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from pydantic import ValidationError

from tempo.artifacts import ARTIFACT_SCHEMA_VERSION, OUTCOMES, Artifact, Cost, Finding, StageResult
from tempo.config import load_config
from tempo.gates import (
    DEFAULT_GATE_SEVERITY,
    ENGINE_GATE_CODES,
    GateContractError,
    GateReport,
    lint_gate,
    merge_severity,
    run_gates,
)


class Slip(Artifact):
    branch: str


def slip(branch: str = "ticket/x") -> Slip:
    return Slip(produced_by_spec_version=1, produced_at_sha="a" * 40, branch=branch)


class StubGate:
    """Fails any artifact whose branch is not under ticket/."""

    code = "scope_fence"

    def check(self, artifact: Slip, workspace: Path) -> GateReport:
        if artifact.branch.startswith("ticket/"):
            return GateReport(code=self.code, verdict="pass")
        return GateReport(
            code=self.code,
            verdict="fail",
            findings=[
                Finding(
                    code=self.code,
                    message=f"branch {artifact.branch!r} is outside ticket/",
                    paved_road="rename the branch to ticket/<stem>",
                )
            ],
        )


class RoadlessGate(StubGate):
    """A gate that bypasses Finding validation to ship a finding with no paved road."""

    def check(self, artifact: Slip, workspace: Path) -> GateReport:
        report = super().check(artifact, workspace)
        if report.verdict == "fail":
            bad = Finding.model_construct(code=self.code, path=None, line=None, message="bad", paved_road="")
            return GateReport.model_construct(code=self.code, verdict="fail", findings=[bad], autofix_applied=False)
        return report


class SilentFailGate(StubGate):
    def check(self, artifact: Slip, workspace: Path) -> GateReport:
        return GateReport(code=self.code, verdict="fail")


SAMPLES = [slip("ticket/ok"), slip("feature/bad")]


# --- artifacts -------------------------------------------------------------


def test_outcome_vocab_is_closed_and_includes_already_satisfied():
    assert OUTCOMES == {
        "ok",
        "already_satisfied",
        "invalid_artifact",
        "gate_failed",
        "premise_failed",
        "timeout",
        "infra_error",
        "budget_exceeded",
    }


def test_finding_requires_nonblank_paved_road_and_allows_null_path_line():
    f = Finding(code="ticket_schema", message="missing kind", paved_road="add kind: feature")
    assert f.path is None and f.line is None
    with pytest.raises(ValidationError):
        Finding(code="ticket_schema", message="missing kind")
    with pytest.raises(ValidationError):
        Finding(code="ticket_schema", message="missing kind", paved_road="   ")


def test_artifact_carries_version_and_provenance():
    s = slip()
    assert s.artifact_schema_version == ARTIFACT_SCHEMA_VERSION
    assert s.produced_by_spec_version == 1 and s.produced_at_sha == "a" * 40
    with pytest.raises(ValidationError):
        Slip(produced_by_spec_version=1, branch="ticket/x")  # provenance is required


def test_artifact_reader_refuses_newer_tolerates_older_and_rejects_unknown_keys():
    raw = slip().model_dump()
    with pytest.raises(ValidationError, match="newer"):
        Slip.model_validate({**raw, "artifact_schema_version": ARTIFACT_SCHEMA_VERSION + 1})
    assert Slip.model_validate({**raw, "artifact_schema_version": 0}).artifact_schema_version == 0
    with pytest.raises(ValidationError):
        Slip.model_validate({**raw, "surprise": 1})


def test_stage_result_is_frozen_and_validates_outcome():
    r = StageResult(outcome="already_satisfied", artifact=None, findings=[], cost=Cost())
    with pytest.raises(FrozenInstanceError):
        r.outcome = "ok"  # type: ignore[misc]
    with pytest.raises(ValueError, match="outcome"):
        StageResult(outcome="done", artifact=None, findings=[], cost=Cost())  # type: ignore[arg-type]


def test_cost_defaults_are_the_non_llm_shape():
    c = Cost()
    assert (c.usd, c.provider, c.model) == (0.0, None, None)


# --- gate-lint -------------------------------------------------------------


def test_gate_lint_passes_stub_gate(tmp_path):
    assert lint_gate(StubGate(), SAMPLES, tmp_path) == []


def test_gate_lint_rejects_paved_road_less_gate(tmp_path):
    problems = lint_gate(RoadlessGate(), SAMPLES, tmp_path)
    assert problems and any("paved_road" in p for p in problems)


def test_gate_lint_rejects_fail_without_findings(tmp_path):
    problems = lint_gate(SilentFailGate(), SAMPLES, tmp_path)
    assert any("no findings" in p for p in problems)


def test_gate_lint_requires_a_failing_sample(tmp_path):
    problems = lint_gate(StubGate(), [slip("ticket/ok")], tmp_path)
    assert any("failing sample" in p for p in problems)


def test_gate_lint_rejects_code_outside_closed_vocab(tmp_path):
    class Rogue(StubGate):
        code = "vibes"

    problems = lint_gate(Rogue(), SAMPLES, tmp_path)
    assert any("vibes" in p for p in problems)


def test_gate_lint_rejects_report_code_mismatch(tmp_path):
    class Liar(StubGate):
        def check(self, artifact, workspace):
            return GateReport(code="diff_budget", verdict="pass")

    problems = lint_gate(Liar(), SAMPLES, tmp_path)
    assert any("diff_budget" in p for p in problems)


# --- runner + severity -----------------------------------------------------


def test_default_severity_is_all_hard_over_engine_codes():
    assert set(DEFAULT_GATE_SEVERITY) == ENGINE_GATE_CODES
    assert set(DEFAULT_GATE_SEVERITY.values()) == {"hard"}
    assert merge_severity(None) == DEFAULT_GATE_SEVERITY


def test_runner_defaults_to_all_hard(tmp_path):
    result = run_gates([StubGate()], slip("feature/bad"), tmp_path)
    assert not result.passed
    assert [r.code for r in result.hard_failures] == ["scope_fence"]
    assert result.soft_failures == []
    assert result.findings[0].paved_road == "rename the branch to ticket/<stem>"


def test_runner_passes_clean_artifact(tmp_path):
    result = run_gates([StubGate()], slip(), tmp_path)
    assert result.passed and result.hard_failures == [] and result.soft_failures == []


def test_runner_applies_soft_severity_from_config(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        textwrap.dedent(
            """\
            schema_version: 1
            state_dir: .tempo
            providers:
              - name: claude
                kind: cli
                package: test-cli
                models_by_tier: {low: a, medium: b, high: c, max: d}
                limits: {concurrency: 1, est_cost_per_call_usd: 0.25}
            routing:
              - {tier: medium, surface: implement, candidates: [{provider: claude, model: b}]}
            review:
              gate_severity: {scope_fence: soft}
            merge: {}
            engine_plane_safety_inventory: []
            """
        )
    )
    severity = merge_severity(load_config(cfg_path, cwd=tmp_path))
    assert severity["scope_fence"] == "soft" and severity["diff_budget"] == "hard"
    result = run_gates([StubGate()], slip("feature/bad"), tmp_path, severity=severity)
    assert result.passed
    assert [r.code for r in result.soft_failures] == ["scope_fence"]


def test_runner_refuses_gate_without_severity(tmp_path):
    with pytest.raises(GateContractError, match="scope_fence"):
        run_gates([StubGate()], slip(), tmp_path, severity={"diff_budget": "hard"})


def test_runner_fails_closed_on_paved_road_less_report(tmp_path):
    with pytest.raises(GateContractError, match="paved_road"):
        run_gates([RoadlessGate()], slip("feature/bad"), tmp_path)


@pytest.mark.parametrize('state,data,road', [
    ('missing', None, 'tempo core'),
    ('missing', b'project\xff', 'tempo core'),
    ('current', 'render', None),
    ('drifted', 'stale', 'tempo core'),
    ('refused', b'tempo:core', 'retain all project content'),
    ('refused', b'- Tempo must\n  skip Check.', 'engine core template'),
])
def test_core_drift_gate_reports(tmp_path, monkeypatch, state, data, road):
    from tempo import hostfiles
    from tests.test_stages import evidence
    if data == 'render':
        data = hostfiles.render(b'project\xff')
    elif data == 'stale':
        data = hostfiles.render(b'project\xff', template=b'old\n')
    artifact = evidence(core_files={'CLAUDE.md': data})
    before = data
    def forbidden(*args, **kwargs):
        pytest.fail('gate evaluation had an effect')
    from tempo.git import Git
    from tempo.journal import Journal
    from tempo.seams import LocalFileSystem, SubprocessExec
    with monkeypatch.context() as pure:
        pure.setattr(Path, 'read_bytes', forbidden)
        pure.setattr(Path, 'write_bytes', forbidden)
        pure.setattr(LocalFileSystem, 'write', forbidden)
        pure.setattr(SubprocessExec, 'run', forbidden)
        pure.setattr(Git, '_run', forbidden)
        pure.setattr(Journal, 'append', forbidden)
        report = hostfiles.CoreDriftGate().check(artifact, tmp_path)
    assert report.model_dump().keys() == {'code', 'verdict', 'findings', 'autofix_applied'}
    assert report.code == 'core_drift' and not report.autofix_applied
    assert report.verdict == ('pass' if state == 'current' else 'fail')
    if state == 'current':
        assert report.findings == []
    else:
        [finding] = report.findings
        assert finding.code == 'core_drift' and finding.path == 'CLAUDE.md'
        assert state in finding.message and 'tempo core' in finding.message
        assert road in finding.paved_road
    assert artifact.core_files['CLAUDE.md'] == before


def test_core_drift_default_hard(repo):
    from tempo.hostfiles import CoreDriftGate
    from tempo import stages
    from tests.test_stages import evidence, author, STEM
    from tests.test_merge import context
    from tempo.tickets import validate_ticket
    artifact = evidence(core_files={'AGENTS.md': None})
    assert 'core_drift' in ENGINE_GATE_CODES and DEFAULT_GATE_SEVERITY['core_drift'] == 'hard'
    gate = CoreDriftGate()
    assert not run_gates([gate], artifact, repo).passed
    author(repo, bypass='gate_bypass:\n  - {code: core_drift, reason: reviewed exception}\n')
    ctx = context(repo, [])
    ticket = validate_ticket(STEM, (repo / f'tickets/{STEM}/ticket.md').read_text(), repo)
    bypass = stages.check_severity(ctx, ticket)
    result = run_gates([gate], artifact, repo, severity=bypass)
    assert result.passed and result.soft_failures[0].code == 'core_drift'
    ctx.config.review.gate_severity = {'core_drift': 'soft'}
    assert run_gates([gate], artifact, repo, severity=merge_severity(ctx.config)).passed


@pytest.mark.asyncio
async def test_core_activation_committed_conduct():
    import os
    from tempo import hostfiles, stages
    from tempo.git import Git
    from tempo.seams import SubprocessExec
    from tests.test_stages import evidence
    root = Path(__file__).resolve().parents[1]
    config = load_config(None, cwd=root)
    git = Git(SubprocessExec(), env=os.environ, timeout=30)
    gathered = await hostfiles.committed_core(root, config, git)
    assert set(gathered['core_files']) == {'CLAUDE.md', 'AGENTS.md'}
    assert all(data == (root / path).read_bytes() for path, data in gathered['core_files'].items())
    gate = next(g for g in stages.CHECK_GATES if g.code == 'core_drift')
    report = gate.check(evidence(**gathered), root)
    assert report.verdict == 'pass', report.findings


# The severity exercise uses the same disposable constructor as the production stage tests.
from tests.test_stages import repo


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_base_test", [False, True])
async def test_host_loop_regression_uses_real_bug_gate(tmp_path, missing_base_test):
    import json
    from dataclasses import replace
    from hosts.fixture import scaffold
    from tempo import stages
    from tempo.tickets import stamp, validate_ticket
    from tempo.seams import SubprocessExec
    from tempo.git import Git
    from tests.test_fixture_host import environment
    from tests.test_merge import context
    host = scaffold.copy_host(tmp_path / "host")
    env = {**environment(host), "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@invalid",
           "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@invalid"}
    git = Git(SubprocessExec(), env=env, timeout=10)
    (host / "TEMPO_PLAN.md").write_text("# Disposable plan\n")
    (host / ".gitignore").write_text(".tempo/\n.fixture-runtime/\n__pycache__/\n")
    existing_test = (host / "regression_test.py").read_bytes()
    if missing_base_test:
        (host / "regression_test.py").unlink()
    await git.init(host, branch="main")
    await git.add(host, [p.relative_to(host).as_posix() for p in host.rglob("*")
        if p.is_file() and not {".git", ".fixture-runtime"}.intersection(p.parts)])
    await git.commit(host, "defective fixture base")
    raw = scaffold.scenario(host, "regression")["responses"]["author"][0]["ticket"]
    raw = stamp(stamp(raw, "source", "human"), "state", "confirmed")
    (host / "tickets/regression/evidence").mkdir(parents=True)
    (host / "tickets/regression/ticket.md").write_text(raw)
    (host / "tickets/regression/evidence/replay.json").write_bytes((host / "evidence/regression.json").read_bytes())
    await git.add(host, ["tickets/regression"])
    await git.commit(host, "fixture bug and durable evidence")
    ctx = replace(context(host, []), env=env, git=git)
    ticket = validate_ticket("regression", raw, host)
    work = ctx.worktree("regression")
    await git.worktree_add(host, work, "regression", "main")
    try:
        scaffold.scripted_reply(host, "regression", "implement", "fixture proof", True, workspace=work)
        if missing_base_test:
            (work / "regression_test.py").write_bytes(existing_test)
        paths = list(scaffold.scenario(host, "regression")["edits"])
        await git.add(work, paths + (["regression_test.py"] if missing_base_test else []))
        await git.commit(work, "fixture score repair and carries")
        safety = await stages.gather_safety_evidence(ctx, ticket, "ok")
        bug, findings = await stages._gather_bug(ctx, ticket, safety, attempt=0, stage="check")
        evidence = safety.model_copy(update={"bug_evidence": bug, "bug_findings": findings})
        result = stages.BugEvidenceGate().check(evidence, work)
        assert bug.head.rc == 0 and bug.head.regression_result == "pass"
        assert bug.overlay_paths == ["fixtures/replay.json", "fixtures/report_regression.py"]
        assert "settings.json" not in bug.overlay_paths
        assert result.verdict == ("fail" if missing_base_test else "pass")
        assert bug.base.regression_result == ("error" if missing_base_test else "defect")
        assert bug.base.rc == (2 if missing_base_test else 1)
        assert not (ctx.config.worktree_root / ".regression-base/regression").exists()
    finally:
        await git.worktree_remove(host, work)
