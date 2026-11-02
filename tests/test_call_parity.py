"""The old child-process path and prepare -> subagent -> submit agree (AGENT_PLAN.md P4 exit).

Both run under a fixed clock and fixed ids. Prompts are not compared: the new prompt carries the read
root. The journal is compared with the new call-record fields removed.
"""

import asyncio
from pathlib import Path

import pytest

from tempo.config import load_config
from tempo.driver import Driver
from tempo.llm import FakeLLM, LLMResult
from tests.test_call import REVIEW, Clock, Stub, build, echo, init_repo, reply, spec

NEW_FIELDS = {"effect_intent": {"call_id", "deadline", "snapshot", "dir"},
              "effect_completion": {"model_source", "call"}}


def journal(events):
    return [(e.type, e.ticket, e.key, {k: v for k, v in e.body.items() if k not in NEW_FIELDS.get(e.type, ())})
            for e in events]


def outputs(state_dir: Path) -> dict[str, str]:
    root = state_dir / "spools"
    return {str(p.relative_to(root)): p.read_text() for p in sorted(root.rglob("output.txt"))}


def old_path(repo: Path, texts: list[str]):
    config = load_config(None, cwd=repo)
    llm = FakeLLM([LLMResult(text=t, provider="claude", model="opus") for t in texts])

    class Sleep:
        async def __call__(self, seconds):
            await asyncio.Event().wait()

    driver = Driver.from_config(config, llm=llm, env={}, clock=Clock(), sleep=Sleep())
    result = asyncio.run(driver.run(REVIEW, Stub(text="hi"), ticket="t-1", attempt=1, workspace=repo,
                                    tier="medium", effort="medium", stuck_budget=600.0))
    return result, journal(driver.journal.read()), outputs(config.state_dir)


def new_path(repo: Path, texts: list[str]):
    calls = build(repo)
    texts = list(texts)
    while True:
        prepared = asyncio.run(calls.prepare(spec(repo)))
        if prepared.state == "completed":
            result = asyncio.run(calls.submit(prepared.call, REVIEW)).result
            break
        reply(prepared, texts.pop(0))
        submitted = asyncio.run(calls.submit(prepared.call, REVIEW))
        if submitted.final:
            result = submitted.result
            break
    return result, journal(calls.journal.read()), outputs(calls.config.state_dir)


@pytest.mark.parametrize("texts", [
    [echo("hello")],
    [echo("nope"), echo("hello")],
    ["not json", echo("hello")],
    ["```json\n" + echo("hello") + "\n```"],
    [echo("nope"), "not json", echo("nope")],
], ids=["ok", "gate-then-ok", "invalid-then-ok", "fenced", "cap-spent"])
def test_old_and_new_paths_produce_identical_results_journal_and_outputs(tmp_path, texts):
    old_repo, new_repo = tmp_path / "old", tmp_path / "new"
    for path in (old_repo, new_repo):
        path.mkdir()
        init_repo(path)
    old_result, old_journal, old_outputs = old_path(old_repo, texts)
    new_result, new_journal, new_outputs = new_path(new_repo, texts)
    assert new_result == old_result
    assert new_journal == old_journal
    assert new_outputs == old_outputs

