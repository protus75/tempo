"""Phase 0 exit read (TEMPO_PLAN.md 19.P0): a toy echo stage -- consumes a stub artifact,
emits one -- runs end to end under the driver with the fake LLM."""

import asyncio
import json
import textwrap
from datetime import UTC, datetime
from pathlib import Path

from tempo.artifacts import Artifact, Finding
from tempo.config import load_config
from tempo.driver import Driver, LlmStage
from tempo.gates import GateReport
from tempo.llm import FakeLLM

CONFIG = textwrap.dedent(
    """\
    schema_version: 1
    state_dir: .tempo
    providers:
      - name: fake
        kind: cli
        package: test-cli
        models_by_tier: {low: fake, medium: fake, high: fake, max: fake}
        limits: {concurrency: 1}
    routing:
      - {tier: medium, surface: echo, candidates: [{provider: fake, model: fake}]}
    review: {}
    merge: {}
    engine_plane_safety_inventory: [specs/]
    """
)


class Stub(Artifact):
    text: str


class Echo(Artifact):
    echoed: str


class EchoesInput:
    """The stage's one gate: the emitted artifact must echo the consumed text verbatim."""

    code = "run_record"

    def __init__(self, consumed: Stub) -> None:
        self.consumed = consumed

    def check(self, artifact: Echo, workspace: Path) -> GateReport:
        if artifact.echoed == self.consumed.text:
            return GateReport(code=self.code, verdict="pass")
        finding = Finding(
            code=self.code,
            message=f"echoed {artifact.echoed!r}, consumed {self.consumed.text!r}",
            paved_road="echo the consumed text exactly",
        )
        return GateReport(code=self.code, verdict="fail", findings=[finding])


def render(consumed: Stub, findings: list[Finding]) -> str:
    prompt = f"Reply with one JSON Echo object whose `echoed` is: {consumed.text}"
    for f in findings:
        prompt += f"\n[{f.code}] {f.message} -- {f.paved_road}"
    return prompt


def echo_reply(req_text: str, echoed: str) -> str:
    assert "Reply with one JSON Echo object" in req_text
    return "```json\n" + json.dumps({"produced_by_spec_version": 1, "produced_at_sha": "abc123", "echoed": echoed}) + "\n```"


async def no_sleep_wins(seconds: float) -> None:
    await asyncio.Event().wait()


def run_echo(tmp_path: Path, llm: FakeLLM, consumed: Stub):
    (tmp_path / "config.yaml").write_text(CONFIG)
    config = load_config(None, cwd=tmp_path)
    driver = Driver.from_config(
        config, llm=llm, env={}, clock=lambda: datetime(2026, 10, 5, tzinfo=UTC), sleep=no_sleep_wins
    )
    stage = LlmStage(surface="echo", emits=Echo, gates=[EchoesInput(consumed)], render=render)
    result = asyncio.run(
        driver.run(
            stage,
            consumed,
            ticket="t-echo",
            attempt=1,
            workspace=tmp_path,
            tier="medium",
            effort="medium",
            stuck_budget=600.0)
    )
    return result, config.state_dir


STUB = Stub(produced_by_spec_version=1, produced_at_sha="abc123", text="hello")


def test_echo_stage_runs_end_to_end(tmp_path):
    llm = FakeLLM([lambda req: echo_reply(req.rendered, "hello")])
    result, state = run_echo(tmp_path, llm, STUB)

    assert result.outcome == "ok"
    assert result.artifact == Echo(produced_by_spec_version=1, produced_at_sha="abc123", echoed="hello")
    assert result.findings == []
    assert len(llm.requests) == 1
    assert llm.requests[0].worktree is None  # echo is not a writing surface
    call = state / "spools" / "t-echo" / "1" / "echo" / "call-01"
    assert (call / "prompt.md").read_text() == llm.requests[0].rendered
    assert "hello" in (call / "output.txt").read_text()
    events = [json.loads(line)["event"] for line in (state / "engine.log").read_text().splitlines()]
    assert events == ["stage_start", "llm_call", "stage_end"]


def test_echo_stage_recovers_from_a_gate_failure_by_reprompting(tmp_path):
    llm = FakeLLM([lambda req: echo_reply(req.rendered, "goodbye"), lambda req: echo_reply(req.rendered, "hello")])
    result, _ = run_echo(tmp_path, llm, STUB)

    assert result.outcome == "ok"
    assert result.artifact.echoed == "hello"
    assert "echo the consumed text exactly" in llm.requests[1].rendered
