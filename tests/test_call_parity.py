"""The old child-process path and prepare -> subagent -> submit agree (AGENT_PLAN.md P4 exit).

Both run under a fixed clock and fixed ids. Prompts are not compared: the new prompt carries the read
root. The journal is compared with the new call-record fields removed, and without the breaker's
`provider_call_outcome` signals: chupa's provider path recorded one per call, and the fake model behind
the old path records none.
"""

import asyncio
import subprocess
from pathlib import Path

import pytest

from tempo.config import load_config
from tempo.driver import Driver
from tempo.llm import FakeLLM, LLMResult
from tempo.journal import Journal
from tempo.stages import Invoice, review
from tempo.thresh import PROVIDER_CALL_OUTCOME
from tempo.tickets import validate_ticket
from tests.fixturerepo import STEM, materialize
from tests.hostfakes import ENV, FakeHost
from tests.test_fixture_flow import APPROVE_DIFF, APPROVE_TICKET, IMPLEMENTED, SUBTRACT, TEST, call
from tests.test_call import REVIEW, Clock, Stub, build, echo, init_repo, reply, spec


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout

NEW_FIELDS = {"effect_intent": {"call_id", "deadline", "snapshot", "dir"},
              "effect_completion": {"model_source", "call"}}


def journal(events):
    return [(e.type, e.ticket, e.key, {k: v for k, v in e.body.items() if k not in NEW_FIELDS.get(e.type, ())})
            for e in events if e.body.get("signal") != PROVIDER_CALL_OUTCOME]


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


def _through_check(repo: Path) -> FakeHost:
    """The tool path up to review, with the same subagent replies in every fixture copy."""
    host = FakeHost("claude", repo)
    host.tool("begin_session")
    host.tool("start", {"stem": STEM})
    call(host, "requisition_review", APPROVE_TICKET)
    call(host, "implement", IMPLEMENTED, edits={"calc.py": SUBTRACT, "test_calc.py": TEST})
    host.tool("check", {"stem": STEM})
    return host


def _events(repo: Path, host: FakeHost):
    return Journal(load_config(None, cwd=repo).state_dir, host.clock).read()


def test_review_through_the_tools_matches_the_old_stage(tmp_path, monkeypatch):
    """The tool layer rebuilds review's stage from call.json and runs the same artifact writer: the lifted
    `review.md`, the main branch, and the journal match the old stage driven by the fake model."""
    for name in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        monkeypatch.setenv(name, ENV[name])
    new_repo, old_repo = materialize(tmp_path / "new"), materialize(tmp_path / "old")
    new, old = _through_check(new_repo), _through_check(old_repo)
    assert git(new_repo, "rev-parse", STEM) == git(old_repo, "rev-parse", STEM)
    marks = {new_repo: len(_events(new_repo, new)), old_repo: len(_events(old_repo, old))}

    call(new, "review", APPROVE_DIFF)

    tools = old.tools()
    tools.driver.llm = FakeLLM([LLMResult(text=APPROVE_DIFF, provider="claude", model="claude-opus-5-5")])
    ticket = validate_ticket(STEM, (old_repo / "tickets" / STEM / "ticket.md").read_text(), old_repo)
    invoice = Invoice.model_validate_json((old_repo / "tickets" / STEM / "checks.json").read_text())
    asyncio.run(review(tools.ctx, ticket, invoice, attempt=0))

    rel = f"tickets/{STEM}/review.md"
    assert (new_repo / rel).read_bytes() == (old_repo / rel).read_bytes()
    assert git(new_repo, "rev-parse", "main") == git(old_repo, "rev-parse", "main")
    after = journal(_events(new_repo, new)[marks[new_repo]:])
    assert after == journal(_events(old_repo, old)[marks[old_repo]:])
    assert [key for _, _, key, _ in after] == [f"llm/{STEM}/0/review/0/1", f"llm/{STEM}/0/review/0/1",
                                               f"ticket-plane/{STEM}/0/review", f"ticket-plane/{STEM}/0/review"]
