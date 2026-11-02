import asyncio
import json
import textwrap
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from tempo.artifacts import Artifact, Finding
from tempo.config import load_config
from tempo.driver import Driver, LlmStage
from tempo.effects import Effects, effect
from tempo.journal import EventType, Journal, run_seq
from tempo.llm import FakeLLM, LLMResult
from tempo.llmeffect import llm_key

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

NOW = datetime(2026, 10, 5, tzinfo=UTC)


def clock() -> datetime:
    return NOW


class FakeSleep:
    async def __call__(self, seconds: float) -> None:
        await asyncio.Event().wait()  # the stuck budget never fires here


class Stub(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


class Echo(Artifact):
    echoed: str


def render(consumed: Stub, findings: list[Finding]) -> str:
    return f"Echo as JSON: {consumed.text}" + "".join(f"\n[{f.code}] {f.message}" for f in findings)


ECHO = LlmStage(surface="implement", emits=Echo, gates=[], render=render)


def echo_json(echoed: str) -> str:
    return json.dumps({"produced_by_spec_version": 1, "produced_at_sha": "abc123", "echoed": echoed})


def build(tmp_path: Path, llm: FakeLLM) -> tuple[Driver, Path]:
    (tmp_path / "config.yaml").write_text(CONFIG)
    config = load_config(None, cwd=tmp_path)
    driver = Driver.from_config(
        config, llm=llm, env={"TEMPO_TEST_KEY": SECRET}, clock=clock, sleep=FakeSleep()
    )
    return driver, config.state_dir


def run(driver: Driver, ticket: str | None = "t-echo", attempt: int = 1):
    return asyncio.run(
        driver.run(ECHO, Stub(text="hi"), ticket=ticket, attempt=attempt, workspace=Path("."),
                   tier="medium", effort="low", stuck_budget=600.0, expected_budget=300.0, scope_fence=())
    )


def metered(text: str, usd: float = 0.5) -> LLMResult:
    return LLMResult(text=text, input_tokens=10, output_tokens=5, provider="claude", model="sonnet", usd=usd)


def llm_completions(state_dir: Path):
    return [e for e in Journal(state_dir, clock).read()
            if e.type == EventType.EFFECT_COMPLETION and e.key.startswith("llm/")]


def terminal(state_dir: Path, ticket: str, to: str) -> None:
    Journal(state_dir, clock).append(EventType.STATE_TRANSITION, {"to": to}, ticket=ticket)


def test_key_is_the_universal_llm_shape():
    assert llm_key("t-1", 3, "implement", 2, 1) == "llm/t-1/3/implement/2/1"


def test_each_call_journals_exactly_one_cost_bearing_completion(tmp_path):
    llm = FakeLLM([metered(echo_json(""), usd=0.25), metered(echo_json("hi"), usd=0.5)])
    driver, state_dir = build(tmp_path, llm)
    result = run(driver)

    assert result.outcome == "ok"
    done = llm_completions(state_dir)
    assert len(done) == len(llm.requests) == 1  # the first valid reply ends the attempt
    assert done[0].key == "llm/t-echo/0/implement/1/1" and done[0].ticket == "t-echo"
    assert done[0].body["cost"] == {"usd": 0.25, "input_tokens": 10, "output_tokens": 5,
                                    "provider": "claude", "model": "sonnet"}


def test_reprompt_takes_a_fresh_key_and_its_own_cost_event(tmp_path):
    llm = FakeLLM(["not json", metered(echo_json("hi"))])
    driver, state_dir = build(tmp_path, llm)
    assert run(driver).outcome == "ok"

    done = llm_completions(state_dir)
    assert [e.key for e in done] == ["llm/t-echo/0/implement/1/1", "llm/t-echo/0/implement/1/2"]
    assert len(done) == len(llm.requests) == 2
    assert all("cost" in e.body for e in done)


def test_same_sequence_reentry_replays_without_recalling(tmp_path):
    llm = FakeLLM([metered(echo_json("hi"))])
    driver, state_dir = build(tmp_path, llm)
    first = run(driver)

    replayed = run(driver)  # no intervening terminal: same run_seq, same keys

    assert len(llm.requests) == 1  # the exhausted script would raise on a re-call
    assert replayed.outcome == first.outcome == "ok"
    assert replayed.artifact == first.artifact
    assert len(llm_completions(state_dir)) == 1  # replay journals no second cost event


def test_replay_survives_a_restart(tmp_path):
    driver, state_dir = build(tmp_path, FakeLLM([metered(echo_json("hi"))]))
    run(driver)

    fresh_llm = FakeLLM([])
    restarted, _ = build(tmp_path, fresh_llm)
    assert run(restarted).outcome == "ok"
    assert fresh_llm.requests == []
    assert len(llm_completions(state_dir)) == 1


def test_a_terminal_advances_run_seq_so_the_rerun_is_real_work(tmp_path):
    llm = FakeLLM([metered(echo_json("hi")), metered(echo_json("hi"))])
    driver, state_dir = build(tmp_path, llm)
    run(driver)
    terminal(state_dir, "t-echo", "gate_failed")

    run(driver)

    assert len(llm.requests) == 2
    assert [e.key for e in llm_completions(state_dir)] == ["llm/t-echo/0/implement/1/1",
                                                           "llm/t-echo/1/implement/1/1"]


def test_run_seq_counts_only_this_stems_terminals():
    j_events = [
        ("t-a", "running"), ("t-a", "gate_failed"), ("t-b", "merged"), ("t-a", "running"), ("t-a", "abandoned"),
    ]

    class Ev:
        def __init__(self, ticket, to):
            self.type, self.ticket, self.body = EventType.STATE_TRANSITION, ticket, {"to": to}

    events = [Ev(t, to) for t, to in j_events]
    assert run_seq(events, "t-a") == 2
    assert run_seq(events, "t-b") == 1
    assert run_seq(events, "t-c") == 0


def test_ticketless_surface_keys_on_its_surface_name(tmp_path):
    driver, state_dir = build(tmp_path, FakeLLM([metered(echo_json("hi"))]))
    run(driver, ticket=None)
    (done,) = llm_completions(state_dir)
    assert done.key == "llm/implement/0/implement/1/1" and done.ticket is None


def test_recorded_result_is_scrubbed_before_the_completion_and_the_return(tmp_path):
    llm = FakeLLM([metered(f"leak {SECRET}"), metered(echo_json("hi"))])
    driver, state_dir = build(tmp_path, llm)
    run(driver)

    journal_bytes = b"".join(p.read_bytes() for p in (state_dir / "journal").glob("*.jsonl"))
    assert SECRET.encode() not in journal_bytes
    assert llm_completions(state_dir)[0].body["result"]["text"] == "leak [REDACTED:TEMPO_TEST_KEY]"


def test_failed_call_journals_no_completion_and_no_cost(tmp_path):
    driver, state_dir = build(tmp_path, FakeLLM([RuntimeError("boom")]))
    assert run(driver).outcome == "infra_error"
    events = [e for e in Journal(state_dir, clock).read() if (e.key or "").startswith("llm/")]
    assert [e.type for e in events] == ["effect_intent"]


def test_effect_decorator_keys_on_its_arguments_and_records_cost(tmp_path):
    j = Journal(tmp_path, clock)
    calls = []

    @effect(key=lambda name, n: f"greet/{name}/{n}", cost=lambda r: {"usd": r["usd"]})
    async def greet(name: str, n: int) -> dict:
        calls.append((name, n))
        return {"text": f"hi {name}", "usd": 0.1}

    effects = Effects(j)
    first = asyncio.run(greet(effects, "a", 1, ticket="t"))
    again = asyncio.run(greet(effects, "a", 1, ticket="t"))
    asyncio.run(greet(effects, "a", 2, ticket="t"))

    assert first == again == {"text": "hi a", "usd": 0.1}
    assert calls == [("a", 1), ("a", 2)]
    done = [e for e in j.read() if e.type == EventType.EFFECT_COMPLETION]
    assert [(e.key, e.ticket, e.body["cost"]) for e in done] == [("greet/a/1", "t", {"usd": 0.1}),
                                                                 ("greet/a/2", "t", {"usd": 0.1})]


def test_singleton_effect_takes_a_bare_string_key(tmp_path):
    j = Journal(tmp_path, clock)

    @effect(key="retro/1")
    async def retro() -> str:
        return "done"

    assert asyncio.run(retro(Effects(j), ticket=None)) == "done"
    (done,) = [e for e in j.read() if e.type == EventType.EFFECT_COMPLETION]
    assert done.key == "retro/1" and "cost" not in done.body
