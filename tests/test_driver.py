import asyncio
import json
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from tempo.artifacts import Artifact, Finding
from tempo.config import load_config
from tempo.driver import Driver, LlmStage, unwrap_fence
from tempo.enginelog import EngineLog
from tempo.gates import GateReport
from tempo.journal import Journal
from tempo.llm import HANG, FakeLLM, LLMRequest, LLMResult
from tempo.redact import Redactor

SECRET = "sk-live-0123456789abcdef"

CONFIG = textwrap.dedent(
    """\
    schema_version: 1
    state_dir: .tempo
    providers:
      - name: claude
        kind: cli
        package: test-cli
        auth: TEMPO_TEST_KEY
        models_by_tier: {low: haiku, medium: sonnet, high: opus, max: opus}
        limits: {concurrency: 1, est_cost_per_call_usd: 0.25}
    routing:
      - {tier: medium, surface: implement, candidates: [{provider: claude, model: sonnet}]}
    review: {}
    merge: {}
    engine_plane_safety_inventory: [specs/]
    """
)


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 5, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class FakeSleep:
    """Never waits on the wall clock: a sleep either returns at once (deadline hit) or blocks forever."""

    def __init__(self, *, fires: bool) -> None:
        self.fires, self.calls = fires, []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if not self.fires:
            await asyncio.Event().wait()


class Stub(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


class Echo(Artifact):
    echoed: str


def render(consumed: Stub, findings: list[Finding]) -> str:
    prompt = f"Echo this text back as JSON: {consumed.text}"
    for f in findings:
        prompt += f"\n[{f.code}] {f.message} -- {f.paved_road}"
    return prompt


ECHO = LlmStage(surface="implement", emits=Echo, gates=[], render=render)


def echo_json(echoed: str) -> str:
    return json.dumps({"produced_by_spec_version": 1, "produced_at_sha": "abc123", "echoed": echoed})


class MustSayHello:
    code = "run_record"

    def check(self, artifact, workspace: Path) -> GateReport:
        if artifact.echoed == "hello":
            return GateReport(code=self.code, verdict="pass")
        finding = Finding(code=self.code, message="echoed text is not 'hello'", paved_road="echo exactly 'hello'")
        return GateReport(code=self.code, verdict="fail", findings=[finding])


def build(tmp_path: Path, llm: FakeLLM, *, fires: bool = False, env=None) -> tuple[Driver, Path]:
    (tmp_path / "config.yaml").write_text(CONFIG)
    config = load_config(None, cwd=tmp_path)
    clock = FakeClock()
    driver = Driver.from_config(
        config,
        llm=llm,
        env={"TEMPO_TEST_KEY": SECRET} if env is None else env,
        clock=clock,
        sleep=FakeSleep(fires=fires),
    )
    return driver, config.state_dir


def run(driver: Driver, stage: LlmStage = ECHO, text: str = "hi", **kw):
    kw = {"ticket": "t-echo", "attempt": 1, "workspace": Path("."), "tier": "medium", "effort": "low"} | kw
    kw.setdefault("stuck_budget", 600.0)
    kw.setdefault("expected_budget", 300.0)
    kw.setdefault("scope_fence", ())
    return asyncio.run(driver.run(stage, Stub(text=text), **kw))


def spool(state_dir: Path, stem: str = "t-echo", attempt: int = 1) -> Path:
    return state_dir / "spools" / stem / str(attempt)


def log_events(state_dir: Path) -> list[dict]:
    return [json.loads(line) for line in (state_dir / "engine.log").read_text().splitlines()]


def test_echo_stage_with_empty_gate_list_emits_validated_artifact(tmp_path):
    llm = FakeLLM([echo_json("hi")])
    driver, state = build(tmp_path, llm)
    result = run(driver)
    assert result.outcome == "ok"
    assert result.artifact == Echo(produced_by_spec_version=1, produced_at_sha="abc123", echoed="hi")
    assert result.findings == []
    assert result.cost.attempts == 1
    assert result.cost.provider == "fake"
    req = llm.requests[0]
    assert req.budget_usd is None
    assert (req.surface, req.tier, req.effort, req.ticket) == ("implement", "medium", "low", "t-echo")
    assert (spool(state) / "implement" / "call-01" / "prompt.md").read_text() == req.rendered
    assert (spool(state) / "implement" / "call-01" / "output.txt").read_text() == echo_json("hi")
    events = [e["event"] for e in log_events(state)]
    assert events[0] == "stage_start" and events[-1] == "stage_end"
    # Diagnostics go to the engine log; the journal holds only the LLM effect's intent + completion.
    journal = [(e.type, e.key) for e in Journal(state, FakeClock()).read()]
    key = "llm/t-echo/0/implement/1/1"
    assert journal == [("effect_intent", key), ("effect_completion", key)]


def test_prompt_is_on_disk_before_the_call_executes(tmp_path):
    seen = {}

    def inspect(req: LLMRequest) -> str:
        path = spool(state) / "implement" / "call-01" / "prompt.md"
        seen["prompt"] = path.read_text() if path.exists() else None
        return echo_json("hi")

    driver, state = build(tmp_path, FakeLLM([inspect]))
    run(driver)
    assert seen["prompt"] is not None and "Echo this text back" in seen["prompt"]


def test_raising_call_leaves_prompt_and_error_spooled_and_terminals_infra_error(tmp_path):
    driver, state = build(tmp_path, FakeLLM([RuntimeError("provider exploded")]))
    result = run(driver)
    assert result.outcome == "infra_error"
    assert result.findings == []  # unclassified: no finding (section 6)
    assert "Echo this text back" in (spool(state) / "implement" / "call-01" / "prompt.md").read_text()
    assert "provider exploded" in (spool(state) / "implement" / "call-01" / "error.txt").read_text()
    assert "llm_error" in [e["event"] for e in log_events(state)]


def test_hung_call_is_aborted_at_stuck_budget_and_prompt_survives(tmp_path):
    llm = FakeLLM([HANG])  # cancellation-resistant until abort_current
    driver, state = build(tmp_path, llm, fires=True)

    async def bounded():
        async with asyncio.timeout(5):  # wall-clock guard: a driver that skips abort_current deadlocks here
            return await driver.run(
                ECHO, Stub(text="hi"), ticket="t-echo", attempt=1, workspace=Path("."),
                tier="medium", effort="low", stuck_budget=90.0,
                expected_budget=300.0, scope_fence=())

    result = asyncio.run(bounded())
    assert result.outcome == "timeout"
    assert llm.aborted == 1
    assert driver.sleep.calls == [90.0]
    assert (spool(state) / "implement" / "call-01" / "prompt.md").exists()
    events = [e["event"] for e in log_events(state)]
    assert "stuck_budget_kill" in events


def test_stuck_budget_is_per_stage_not_per_call(tmp_path):
    def slow(req: LLMRequest) -> str:
        driver.clock.now += timedelta(seconds=50)  # leaves 40s of the 90s budget for the re-prompt
        return echo_json("nope")

    llm = FakeLLM([slow, HANG])
    driver, _ = build(tmp_path, llm, fires=True)
    stage = LlmStage(surface="implement", emits=Echo, gates=[MustSayHello()], render=render)
    result = run(driver, stage, stuck_budget=90.0)
    assert result.outcome == "timeout"
    assert driver.sleep.calls == [90.0, 40.0]


def test_schema_invalid_output_is_reprompted_with_the_validation_finding(tmp_path):
    llm = FakeLLM(["not json at all", '{"echoed": "hi"}', echo_json("hi")])
    driver, state = build(tmp_path, llm)
    result = run(driver)
    assert result.outcome == "ok"
    assert result.cost.attempts == 3
    second = llm.requests[1].rendered
    assert "[invalid_artifact]" in second
    assert "produced_at_sha" in llm.requests[2].rendered  # the missing field is named
    assert (spool(state) / "implement" / "call-03" / "prompt.md").read_text() == llm.requests[2].rendered


def test_single_surrounding_fence_is_stripped_before_validation(tmp_path):
    driver, _ = build(tmp_path, FakeLLM([f"```json\n{echo_json('hi')}\n```\n"]))
    assert run(driver).outcome == "ok"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('```\n{"a": 1}\n```', '{"a": 1}'),
        ('  \n```json\n{"a": 1}\n```\n  ', '{"a": 1}'),
        ('{"a": 1}', '{"a": 1}'),
        ('````\n```json\n{"a": 1}\n```\n````', '```json\n{"a": 1}\n```'),  # exactly ONE layer
        ('prose first\n```json\n{"a": 1}\n```', 'prose first\n```json\n{"a": 1}\n```'),
    ],
)
def test_unwrap_fence(raw, expected):
    assert unwrap_fence(raw) == expected


def test_hard_gate_failure_reprompts_same_workspace_with_findings(tmp_path):
    stage = LlmStage(surface="implement", emits=Echo, gates=[MustSayHello()], render=render)
    llm = FakeLLM([echo_json("hi"), echo_json("hello")])
    driver, _ = build(tmp_path, llm)
    result = run(driver, stage, workspace=tmp_path / "wt")
    assert result.outcome == "ok" and result.artifact.echoed == "hello"
    assert "[run_record] echoed text is not 'hello' -- echo exactly 'hello'" in llm.requests[1].rendered
    assert [r.worktree for r in llm.requests] == [tmp_path / "wt"] * 2  # the SAME workspace


def test_failing_review_reprompts_with_its_findings(tmp_path):
    finding = Finding(code="review", message="needs revision", paved_road="revise it")

    async def review(artifact, call_seq):
        return GateReport(code="review", verdict="fail" if call_seq == 1 else "pass",
                          findings=[finding] if call_seq == 1 else [])

    stage = LlmStage(surface="implement", emits=Echo, gates=[], render=render, review=review)
    driver, _ = build(tmp_path, FakeLLM([echo_json("hi"), echo_json("hi")]))
    result = run(driver, stage)
    assert result.outcome == "ok"
    assert "[review] needs revision -- revise it" in driver.llm.requests[1].rendered


def test_terminal_review_finding_returns_all_hard_findings_after_one_call(tmp_path):
    sync = Finding(code="run_record", message="sync failed", paved_road="fix sync")
    terminal = Finding(code="stop", message="review refused", paved_road="stop now")

    class SyncGate:
        code = "run_record"

        def check(self, artifact, workspace):
            return GateReport(code=self.code, verdict="fail", findings=[sync])

    async def review(artifact, call_seq):
        return GateReport(code="review", verdict="fail", findings=[terminal])

    stage = LlmStage(surface="implement", emits=Echo, gates=[SyncGate()], render=render,
                     review=review, terminal_findings=frozenset({"stop"}))
    llm = FakeLLM([echo_json("hi")])
    driver, _ = build(tmp_path, llm)
    result = run(driver, stage)
    assert result.outcome == "gate_failed"
    assert result.findings == [sync, terminal]
    assert len(llm.requests) == 1


def test_stage_without_hooks_keeps_its_request_shape(tmp_path):
    llm = FakeLLM([echo_json("hi")])
    driver, _ = build(tmp_path, llm)
    assert run(driver).outcome == "ok"
    assert [(request.surface, request.ticket, request.worktree) for request in llm.requests] == [
        ("implement", "t-echo", Path("."))
    ]


def test_reprompts_are_bounded_by_the_retry_cap(tmp_path):
    stage = LlmStage(surface="implement", emits=Echo, gates=[MustSayHello()], render=render)
    llm = FakeLLM([echo_json("no")] * 20)
    driver, state = build(tmp_path, llm)
    assert driver.retry_cap == 6  # shipped caps.retry default
    result = run(driver, stage)
    assert result.outcome == "gate_failed"
    assert len(llm.requests) == 1 + 6  # the first call plus six re-prompts
    assert [f.code for f in result.findings] == ["run_record"]
    assert not (spool(state) / "implement" / "call-08").exists()


def test_exhausted_invalid_output_terminals_invalid_artifact(tmp_path):
    llm = FakeLLM(["garbage"] * 7)
    driver, _ = build(tmp_path, llm)
    result = run(driver)
    assert result.outcome == "invalid_artifact"
    assert [f.code for f in result.findings] == ["invalid_artifact"]


def test_soft_gate_failure_passes_with_findings(tmp_path):
    (tmp_path / "config.yaml").write_text(CONFIG.replace("review: {}", "review: {gate_severity: {run_record: soft}}"))
    config = load_config(None, cwd=tmp_path)
    clock = FakeClock()
    llm = FakeLLM([echo_json("hi")])
    driver = Driver.from_config(config, llm=llm, env={}, clock=clock, sleep=FakeSleep(fires=False))
    stage = LlmStage(surface="implement", emits=Echo, gates=[MustSayHello()], render=render)
    result = run(driver, stage)
    assert result.outcome == "ok"
    assert [f.code for f in result.findings] == ["run_record"]
    assert len(llm.requests) == 1


def test_cost_sums_calls(tmp_path):
    usage = LLMResult(text="junk", input_tokens=10, output_tokens=5, provider="p", model="m", usd=0.25)
    final = LLMResult(text=echo_json("hi"), input_tokens=3, output_tokens=2, provider="p", model="m", usd=0.25)
    driver, _ = build(tmp_path, FakeLLM([usage, final]))
    cost = run(driver).cost
    assert (cost.tokens, cost.attempts, cost.usd, cost.provider, cost.model) == (20, 2, 0.5, "p", "m")


def test_ticketless_surface_spools_under_its_surface_name(tmp_path):
    llm = FakeLLM([echo_json("hi")])
    driver, state = build(tmp_path, llm)
    stage = LlmStage(surface="triage", emits=Echo, gates=[], render=render)
    run(driver, stage, ticket=None)
    assert llm.requests[0].ticket is None
    assert (spool(state, "triage") / "triage" / "call-01" / "prompt.md").exists()


def test_surfaces_sharing_an_attempt_keep_separate_spools(tmp_path):
    llm = FakeLLM([echo_json("implemented"), echo_json("reviewed")])
    driver, state = build(tmp_path, llm)
    review = LlmStage(surface="review", emits=Echo, gates=[], render=render)

    assert run(driver, ECHO, "implemented").outcome == "ok"
    assert run(driver, review, "reviewed").outcome == "ok"

    implement = spool(state) / "implement" / "call-01"
    review_spool = spool(state) / "review" / "call-01"
    assert (implement / "prompt.md").read_text() == "Echo this text back as JSON: implemented"
    assert (implement / "output.txt").read_text() == echo_json("implemented")
    assert (review_spool / "prompt.md").read_text() == "Echo this text back as JSON: reviewed"
    assert (review_spool / "output.txt").read_text() == echo_json("reviewed")


def test_fake_llm_refuses_when_script_exhausted():
    with pytest.raises(AssertionError, match="script exhausted"):
        asyncio.run(FakeLLM([]).call(LLMRequest("implement", "x", "low", "low", None, None)))


def test_configured_secret_value_never_reaches_spool_or_engine_log(tmp_path):
    # CONFIG-to-writer path: the secret is named only by the provider's `auth` field and resolved from env.
    llm = FakeLLM(
        [
            f"I found {SECRET} in the environment",  # invalid -> re-prompt carries the echo back
            RuntimeError(f"auth failed for key {SECRET}"),
        ]
    )
    driver, state = build(tmp_path, llm)
    result = run(driver, text=f"payload with {SECRET}")
    assert result.outcome == "infra_error"
    assert SECRET in llm.requests[0].rendered  # the model still gets what it was sent
    files = [p for p in state.rglob("*") if p.is_file()]
    assert {p.name for p in files} >= {"prompt.md", "output.txt", "error.txt", "engine.log"}
    for p in files:
        assert SECRET not in p.read_text(), p
    assert "[REDACTED:TEMPO_TEST_KEY]" in (spool(state) / "implement" / "call-01" / "prompt.md").read_text()
    assert "[REDACTED:TEMPO_TEST_KEY]" in (spool(state) / "implement" / "call-02" / "error.txt").read_text()


def test_redactor_from_config_skips_unset_and_empty_values(tmp_path):
    (tmp_path / "config.yaml").write_text(CONFIG)
    config = load_config(None, cwd=tmp_path)
    assert Redactor.from_config(config, {}).scrub("abc") == "abc"
    assert Redactor.from_config(config, {"TEMPO_TEST_KEY": ""}).scrub("abc") == "abc"
    assert Redactor.from_config(config, {"TEMPO_TEST_KEY": "b"}).scrub("abc") == "a[REDACTED:TEMPO_TEST_KEY]c"


def test_redactor_replaces_longest_secret_first():
    r = Redactor({"SHORT": "abc", "LONG": "abcdef"})
    assert r.scrub("xabcdefx abc") == "x[REDACTED:LONG]x [REDACTED:SHORT]"


def test_engine_log_rotates_by_size(tmp_path):
    log = EngineLog(tmp_path / "engine.log", Redactor({}), FakeClock(), max_bytes=200, backups=3)
    for i in range(40):
        log.event("tick", n=i, pad="x" * 40)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["engine.log", "engine.log.1", "engine.log.2", "engine.log.3"]
    assert all(p.stat().st_size <= 200 for p in tmp_path.iterdir())
    assert json.loads((tmp_path / "engine.log").read_text().splitlines()[-1])["n"] == 39


@pytest.mark.parametrize("history", ["initial", "intent", "multiple", "empty", "no-match"])
def test_exit_read_window_materialization(tmp_path, monkeypatch, history):
    from dataclasses import asdict
    from types import SimpleNamespace
    from tempo.journal import EventType
    from tempo.seams import LocalFileSystem
    from tempo.stages import resolve_exit_window

    driver, state = build(tmp_path, FakeLLM([]))
    j = driver.journal
    if history != "empty":
        j.append(EventType.STATE_TRANSITION, {"to": "merged", "commit": "before"}, ticket="other")
    if history == "intent":
        j.append(EventType.EFFECT_INTENT, {}, key="retro/1")
    if history in {"multiple", "no-match"}:
        j.append(EventType.EFFECT_COMPLETION, {"result": {"commit": "first"}}, key="retro/1")
        j.append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket="old-window")
        j.append(EventType.EFFECT_COMPLETION, {"result": {"commit": "latest"}}, key="retro/2")
    if history not in {"empty", "no-match"}:
        body = {"kind": "drain_handoff", "nested": [None, {"exact": "value"}]}
        j.append(EventType.SIGNAL, body, ticket=None)
        j.append(EventType.STATE_TRANSITION, {"to": "gate_failed"}, ticket="unrelated")
        driver.clock.now -= timedelta(seconds=1)
        j.append(EventType.SIGNAL, {"kind": "drain_handoff", "ordinal": "second"}, ticket="foreign")
        driver.clock.now += timedelta(seconds=1)
        j.append(EventType.SIGNAL, body, ticket=None)
    snapshot = j.read()
    boundary = max((i for i, e in enumerate(snapshot)
                    if e.type == EventType.EFFECT_COMPLETION and (e.key or "").startswith("retro/")), default=-1)
    suffix = snapshot[boundary + 1:]
    canonical = {f"{kind}: current retro window": [asdict(e) for e in suffix if e.type == kind]
                 for kind in ("signal", "state_transition", "effect_completion")}
    declarations = "-   signal  :   current retro window  \n- state_transition: current retro window\n- effect_completion: current retro window\n"
    def ticket(stem, declaration=declarations):
        return SimpleNamespace(stem=stem, sections={"Exit-read window": declaration})
    consumed = resolve_exit_window(ticket("phase5-exit"), snapshot)
    assert consumed == resolve_exit_window(ticket("phase6-exit"), snapshot)
    writes = []
    fs = LocalFileSystem()
    def write(path, data):
        writes.append((path, data))
        fs.write(path, data)
    monkeypatch.setattr(driver.spool._fs, "write", write)
    # Later events cannot change any declaration in the already resolved snapshot.
    j.append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket="appended-later")
    j.append(EventType.SIGNAL, {"kind": "drain_handoff"}, ticket="appended-later")
    workspace = tmp_path / "exit-worktree"
    path = driver.materialize_exit_window(consumed, workspace)
    assert len(writes) == 1 and writes[0][0] == path
    assert json.loads(path.read_text()) == canonical
    assert all(set(event) == {"v", "type", "ts", "ticket", "key", "body"}
               for events in canonical.values() for event in events)
    if history not in {"empty", "no-match"}:
        signals = canonical["signal: current retro window"]
        assert signals[0] == signals[2] and signals[0]["ticket"] is None
        assert signals[1]["body"]["ordinal"] == "second" and signals[1]["ts"] < signals[0]["ts"]
        assert asdict(snapshot[-1]) == signals[-1]
    # The consumed exit read is self-contained even without the live journal directory.
    fs.replace(state, tmp_path / "offline-state")
    assert not state.exists() and json.loads(path.read_text()) == canonical
    assert resolve_exit_window(SimpleNamespace(stem="no-declaration", sections={}), snapshot) == {}
    assert driver.materialize_exit_window({}, workspace) is None
    for declaration in ("- unknown: current retro window", "- signal: since yesterday", "- signal: anything nonblank"):
        with pytest.raises(ValueError, match="EVENT_VERSIONS.*current retro window"):
            resolve_exit_window(ticket("phase5-exit", declaration), snapshot)
