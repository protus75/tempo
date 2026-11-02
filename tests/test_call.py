"""prepare -> subagent -> submit (AGENT_PLAN.md sections 3.1, 3.2, 3.4)."""

import asyncio
import json
import subprocess
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from tempo import render as host_render
from tempo.artifacts import Artifact, Finding
from tempo.call import CALL_CLOSED, READ_PART_CHARS, Calls, CallSpec, agent_name
from tempo.checkout import Refusal
from tempo.config import load_config
from tempo.driver import LlmStage, Spool
from tempo.gates import GateReport, merge_severity
from tempo.git import Git
from tempo.journal import EventType, Journal
from tempo.redact import Redactor
from tempo.seams import LocalFileSystem, SubprocessExec
from tempo.timers import Timers

CONFIG = textwrap.dedent(
    """\
    schema_version: 1
    state_dir: .tempo/state
    hosts:
      claude:
        models_by_tier: {low: sonnet, medium: sonnet, high: sonnet, max: sonnet}
        pins: {review: opus}
        quota_window_minutes: 30
    orchestrator:
      models: {claude: opus}
    caps: {retry: 2}
    review: {}
    merge: {}
    engine_plane_safety_inventory: []
    """
)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 2, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Stub(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


class Echo(Artifact):
    echoed: str


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: str


def render(consumed: Stub, findings: list[Finding]) -> str:
    return f"Echo {consumed.text}" + "".join(f"\n[{f.code}] {f.message}" for f in findings)


class MustSayHello:
    code = "run_record"

    def check(self, artifact, workspace: Path) -> GateReport:
        if artifact.echoed == "hello":
            return GateReport(code=self.code, verdict="pass")
        return GateReport(code=self.code, verdict="fail", findings=[
            Finding(code=self.code, message="not hello", paved_road="echo hello")])


REVIEW = LlmStage(surface="review", emits=Echo, gates=[MustSayHello()], render=render)
IMPLEMENT = LlmStage(surface="implement", emits=Reply, gates=[], render=lambda c, f: "implement it")


def echo(text: str) -> str:
    return json.dumps({"produced_by_spec_version": 1, "produced_at_sha": "abc", "echoed": text})


def sh(cwd: Path, *argv: str) -> str:
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout


def init_repo(path: Path) -> Path:
    sh(path, "git", "init", "-q", "-b", "main")
    sh(path, "git", "config", "user.email", "t@t")
    sh(path, "git", "config", "user.name", "t")
    (path / "config.yaml").write_text(CONFIG)
    (path / ".gitignore").write_text(".tempo/\n")
    (path / "tickets" / "t-1").mkdir(parents=True)
    (path / "tickets" / "t-1" / "ticket.md").write_text("ticket\n")
    (path / "app.py").write_text("x = 1\n")
    host_render.write(path, load_config(None, cwd=path), LocalFileSystem())
    sh(path, "git", "add", "-A")
    sh(path, "git", "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path)


def build(repo: Path, *, session: str = "s1", clock: Clock | None = None, notices=None, ids=None) -> Calls:
    config = load_config(None, cwd=repo)
    clock = clock or Clock()
    journal = Journal(config.state_dir, clock)
    redactor = Redactor({})
    counter = iter(ids or (f"{n:032x}" for n in range(1, 100)))

    async def notify(failure, message):
        if notices is not None:
            notices.append((failure, message))

    return Calls(config=config, host="claude", session=session, repo=repo, journal=journal,
                 spool=Spool(config.state_dir / "spools", redactor, LocalFileSystem()), redactor=redactor,
                 git=Git(SubprocessExec(), env={"PATH": "/usr/bin:/bin"}, timeout=30.0), fs=LocalFileSystem(),
                 clock=clock, timers=Timers(journal=journal, clock=clock, sleep=asyncio.sleep),
                 severity=merge_severity(config), retry_cap=config.caps.retry,
                 notify=notify,
                 new_id=lambda: next(counter))


def spec(repo: Path, stage: LlmStage = REVIEW, **kw) -> CallSpec:
    kw = {"ticket": "t-1", "attempt": 1, "workspace": repo, "tier": "medium", "effort": "medium",
          "stuck_budget": 600.0} | kw
    return CallSpec(stage=stage, consumed=Stub(text="hi"), **kw)


def reply(prepared, text: str | None, *, message: str = "done", at: datetime | None = None) -> None:
    """The fake subagent: write the reply, then the SubagentStop hook writes stop.txt."""
    out = Path(prepared.reply)
    if text is not None:
        out.write_text(text)
    stopped = (at or datetime(2026, 11, 2, tzinfo=UTC)).isoformat()
    (out.parent / "stop.txt").write_text(json.dumps({"stopped_at": stopped, "message": message}))


def run(coro):
    return asyncio.run(coro)


def events(calls: Calls):
    return calls.journal.read()


def test_prepare_spools_parts_and_journals_the_intent(repo):
    calls = build(repo)
    prepared = run(calls.prepare(spec(repo)))
    assert prepared.state == "pending"
    assert prepared.agent == "tempo-review-opus"
    call_dir = Path(prepared.reply).parent
    assert call_dir == calls.config.state_dir / "spools" / "t-1" / "1" / "review" / "call-01"
    assert prepared.spawn_prompt == "\n".join([f"call: {prepared.call}",
                                               *(f"part: {p}" for p in prepared.parts), f"reply: {prepared.reply}"])
    text = "".join(Path(p).read_text() for p in prepared.parts)
    assert text == (call_dir / "prompt.md").read_text() == f"tempo read root: {repo}\n\nEcho hi"
    [intent] = events(calls)
    assert (intent.type, intent.key) == ("effect_intent", "llm/t-1/0/review/1/1")
    assert intent.body["call_id"] == prepared.call and intent.body["deadline"] == "2026-11-02T00:10:00+00:00"
    record = json.loads((call_dir / "call.json").read_text())
    assert (record["agent"], record["model"], record["session"]) == ("tempo-review-opus", "opus", "s1")


def test_prompt_splits_into_read_sized_parts(repo):
    calls = build(repo)
    big = LlmStage(surface="review", emits=Echo, gates=[], render=lambda c, f: "x" * (READ_PART_CHARS + 5))
    prepared = run(calls.prepare(spec(repo, big)))
    sizes = [len(Path(p).read_text()) for p in prepared.parts]
    assert len(sizes) == 2 and sizes[0] == READ_PART_CHARS and sum(sizes) == len(Path(prepared.reply).parent.joinpath("prompt.md").read_text())


def test_submit_is_pending_until_stop_then_accepts_the_reply(repo):
    calls = build(repo)
    prepared = run(calls.prepare(spec(repo)))
    Path(prepared.reply).write_text(echo("hello"))
    assert run(calls.submit(prepared.call, REVIEW)).state == "pending"
    reply(prepared, None)
    done = run(calls.submit(prepared.call, REVIEW))
    assert (done.state, done.outcome, done.final) == ("completed", "ok", True)
    assert done.result.artifact.echoed == "hello"
    assert (done.result.provider, done.result.model) == ("claude", "opus")
    completion = events(calls)[-1]
    assert completion.type == "effect_completion" and completion.key == "llm/t-1/0/review/1/1"
    assert completion.body["served"] == {"provider": "claude", "model": "opus"}
    assert completion.body["model_source"] == {"resolved": "opus", "host_reported": None}
    # A completed call replays: prepare spawns nothing, submit answers from the journal.
    again = run(calls.prepare(spec(repo)))
    assert (again.state, again.call) == ("completed", prepared.call)
    assert run(calls.submit(prepared.call, REVIEW)).result.artifact.echoed == "hello"


def test_gate_failure_reprompts_with_findings_until_the_cap(repo):
    calls = build(repo)
    first = run(calls.prepare(spec(repo)))
    reply(first, echo("nope"))
    failed = run(calls.submit(first.call, REVIEW))
    assert (failed.outcome, failed.final, [f.code for f in failed.findings]) == ("gate_failed", False, ["run_record"])
    second = run(calls.prepare(spec(repo)))
    assert Path(second.reply).parent.name == "call-02"
    assert "[run_record] not hello" in Path(second.parts[0]).read_text()
    reply(second, "not json")
    assert run(calls.submit(second.call, REVIEW)).outcome == "invalid_artifact"
    third = run(calls.prepare(spec(repo)))
    reply(third, echo("nope"))
    last = run(calls.submit(third.call, REVIEW))
    assert (last.outcome, last.final, last.result.outcome) == ("gate_failed", True, "gate_failed")


def test_submit_refuses_an_unissued_call_and_a_second_prepare_while_open(repo):
    calls = build(repo)
    with pytest.raises(Refusal, match="never issued"):
        run(calls.submit("f" * 32, REVIEW))
    run(calls.prepare(spec(repo)))
    with pytest.raises(Refusal, match="is open"):
        run(calls.prepare(spec(repo, ticket="t-2")))


@pytest.mark.parametrize("where", ["repo", "state"])
def test_a_stray_write_is_a_charged_confinement_failure(repo, where):
    calls = build(repo)
    prepared = run(calls.prepare(spec(repo)))
    target = repo / "app.py" if where == "repo" else calls.config.state_dir / "box" / "forged.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("forged\n")
    reply(prepared, echo("hello"))
    done = run(calls.submit(prepared.call, REVIEW))
    assert (done.state, done.outcome) == ("completed", "invalid_artifact")
    assert done.findings[0].code == "confinement" and target.name in done.findings[0].message


def test_no_stop_past_the_deadline_times_out_and_reopens_the_same_call_seq(repo):
    clock = Clock()
    calls = build(repo, clock=clock)
    prepared = run(calls.prepare(spec(repo)))
    clock.now += timedelta(seconds=601)
    done = run(calls.submit(prepared.call, REVIEW))
    assert (done.state, done.outcome, done.final) == ("closed", "timeout", True)
    assert events(calls)[-1].body["signal"] == CALL_CLOSED
    again = run(calls.prepare(spec(repo)))
    assert again.state == "pending" and again.call != prepared.call
    assert Path(again.reply).parent.name == "call-01"


def test_a_stop_recorded_after_the_deadline_is_a_timeout(repo):
    calls = build(repo)
    prepared = run(calls.prepare(spec(repo)))
    reply(prepared, echo("hello"), at=datetime(2026, 11, 2, 0, 11, tzinfo=UTC))
    assert run(calls.submit(prepared.call, REVIEW)).outcome == "timeout"


def test_a_limit_hit_is_uncharged_and_arms_the_host_cooldown(repo):
    calls = build(repo)
    prepared = run(calls.prepare(spec(repo)))
    reply(prepared, None, message="Claude usage limit reached")
    done = run(calls.submit(prepared.call, REVIEW))
    assert (done.state, done.outcome) == ("closed", "provider_drought")
    armed = [e for e in events(calls) if e.type == EventType.TIMER_ARMED]
    assert armed[0].body["timer_id"] == "provider-cooldown/claude/2026-11-02T00:30:00+00:00"
    assert not any(e.type == EventType.EFFECT_COMPLETION for e in events(calls))
    # The drought draws caps.infra, so a session cannot loop on it, and feeds the host's breaker.
    assert [e.body["cap"] for e in events(calls) if e.type == EventType.CAP_CONSUMED] == ["infra"]
    assert breaker(calls) == ["quota_exhausted"]
    assert Path(run(calls.prepare(spec(repo))).reply).parent.name == "call-01"


def test_an_auth_failure_notifies_and_is_uncharged(repo):
    notices = []
    calls = build(repo, notices=notices)
    prepared = run(calls.prepare(spec(repo)))
    reply(prepared, None, message="Not logged in. Please run /login")
    assert run(calls.submit(prepared.call, REVIEW)).outcome == "infra_error"
    assert notices == [("auth_error", "Not logged in. Please run /login")]


def test_a_missing_reply_with_an_unclassified_stop_is_charged(repo):
    calls = build(repo)
    prepared = run(calls.prepare(spec(repo)))
    reply(prepared, None, message="I could not finish")
    done = run(calls.submit(prepared.call, REVIEW))
    assert (done.state, done.outcome) == ("completed", "invalid_artifact")
    assert breaker(calls) == ["unclassified"]


def test_an_accepted_reply_resets_the_breaker_streak(repo):
    calls = build(repo)
    prepared = run(calls.prepare(spec(repo)))
    reply(prepared, echo("hello"))
    run(calls.submit(prepared.call, REVIEW))
    assert breaker(calls) == [None]


def test_prepare_refuses_an_agent_with_no_rendered_file(repo):
    calls = build(repo)
    (repo / ".claude/agents/tempo-review-opus.md").unlink()
    with pytest.raises(Refusal, match="run `tempo render`"):
        run(calls.prepare(spec(repo)))


def breaker(calls):
    return [e.body["failure_class"] for e in events(calls) if e.body.get("signal") == "provider_call_outcome"]


def test_a_dead_sessions_call_is_recorded_by_the_next_prepare_on_its_key(repo):
    clock = Clock()
    old = build(repo, clock=clock)
    stale = run(old.prepare(spec(repo)))
    new = build(repo, session="s2", clock=clock, ids=[f"{n:032x}" for n in range(50, 60)])
    assert [r["call_id"] for r in new.open_calls()] == [stale.call]
    fresh = run(new.prepare(spec(repo)))
    closed = [e.body for e in events(new) if e.type == EventType.SIGNAL]
    assert closed == [{"signal": CALL_CLOSED, "call_id": stale.call, "key": "llm/t-1/0/review/1/1",
                       "outcome": "infra_error", "reason": "abandoned by an ended session"}]
    assert [r["call_id"] for r in new.open_calls()] == [fresh.call]


def worktree(repo: Path) -> Path:
    tree = repo / ".tempo" / "state" / "worktrees" / "t-1"
    sh(repo, "git", "worktree", "add", "-q", "-b", "t-1", str(tree), "main")
    return tree


def test_implement_writes_its_worktree_and_submit_commits_outside_the_ticket_dir(repo):
    calls = build(repo)
    tree = worktree(repo)
    prepared = run(calls.prepare(spec(repo, IMPLEMENT, workspace=tree)))
    assert prepared.agent == "tempo-implement-sonnet"
    (tree / "app.py").write_text("x = 2\n")
    (tree / "new.py").write_text("y = 1\n")
    (tree / "tickets" / "t-1" / "run.md").write_text("record\n")
    reply(prepared, json.dumps({"outcome": "ok"}))
    done = run(calls.submit(prepared.call, IMPLEMENT))
    assert (done.outcome, done.final) == ("ok", True)
    assert sh(tree, "git", "show", "--name-only", "--format=%s", "HEAD").split() == [
        "tempo(t-1):", "implement", "call", "1", "app.py", "new.py"]
    assert sh(tree, "git", "status", "--porcelain").strip() == "?? tickets/t-1/run.md"
    assert events(calls)[-1].body["call"]["head"] == sh(tree, "git", "rev-parse", "HEAD").strip()


def test_implement_editing_its_ticket_is_a_confinement_failure(repo):
    calls = build(repo)
    tree = worktree(repo)
    prepared = run(calls.prepare(spec(repo, IMPLEMENT, workspace=tree)))
    (tree / "tickets" / "t-1" / "ticket.md").write_text("rewritten\n")
    reply(prepared, json.dumps({"outcome": "ok"}))
    done = run(calls.submit(prepared.call, IMPLEMENT))
    assert done.outcome == "invalid_artifact" and "ticket.md" in done.findings[0].message


def test_agent_names_clean_model_ids_and_carry_codex_effort():
    assert agent_name("claude", "review", "claude-opus-5-5", "high") == "tempo-review-claude-opus-5-5"
    assert agent_name("copilot", "implement", "gpt/6:luna", "low") == "tempo-implement-gpt-6-luna"
    assert agent_name("codex", "review", "gpt-6-astra", "max") == "tempo-review-gpt-6-astra-xhigh"
