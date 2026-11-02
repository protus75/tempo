import asyncio
import json
import os
import sys
import textwrap
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tempo.config import ConfigError, load_config
from tempo.llm import LLMRequest
from tempo.providers import (
    ADAPTERS,
    CliAdapter,
    PLACEHOLDER,
    ProviderCallError,
    ProviderLLM,
    ProviderSession,
    ProviderSetupError,
    child_env,
    resolve,
)
from tempo.redact import Redactor
from tempo.journal import Journal
from tempo.timers import Timers
from tempo.seams import GroupExec, LocalFileSystem, SubprocessExec
from tempo.watchdog import EventConsumer

ROOT = Path(__file__).resolve().parent.parent

CONFIG = textwrap.dedent(
    """\
    schema_version: 1
    state_dir: state
    providers:
      - name: claude
        kind: cli
        package: test-cli
        auth: CLAUDE_KEY
        models_by_tier: {low: c-low, medium: c-med, high: c-high, max: c-max}
        limits: {concurrency: 1}
      - name: codex
        kind: cli
        package: test-cli
        auth: CODEX_KEY
        models_by_tier: {low: x-low, medium: x-med, high: x-high, max: x-max}
        limits: {concurrency: 1}
    routing:
      - {tier: medium, surface: implement, candidates: [{provider: codex}, {provider: claude}]}
      - {tier: high, surface: implement, candidates: [{provider: claude}]}
      - {tier: medium, surface: review, candidates: [{provider: claude, model: c-max}, {provider: codex}]}
      - {tier: low, surface: review, candidates: [{provider: codex}]}
    review: {}
    merge: {}
    engine_plane_safety_inventory: []
    """
)

ENV = {"PATH": "/usr/bin", "HOME": "/home/op", "CLAUDE_KEY": "sk-claude-secret", "CODEX_KEY": "sk-codex-secret"}


def config(tmp_path: Path, text: str = CONFIG):
    (tmp_path / "config.yaml").write_text(text)
    return load_config(None, cwd=tmp_path)


class FakeExec:
    """Scripted subprocess: records each invocation and what its stdin file held at spawn."""

    def __init__(self, *responses: tuple[int, str, str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.killed: list[int] = []
        self.hang: asyncio.Event | None = None

    async def run(self, argv, *, cwd, env, timeout, stdin_path=None, on_spawn=None, on_stdout_line=None):
        self.calls.append(
            {"argv": list(argv), "cwd": cwd, "env": dict(env), "stdin": Path(stdin_path).read_text(), "timeout": timeout}
        )
        if on_spawn is not None:
            on_spawn(4242)
        if self.hang is not None:
            await self.hang.wait()
            return -9, "", "killed"
        result = self.responses.pop(0)
        if on_stdout_line is not None:
            for line in result[1].splitlines(keepends=True):
                on_stdout_line(line)
        return result

    def kill_group(self, pgid: int) -> None:
        self.killed.append(pgid)
        if self.hang is not None:
            self.hang.set()


def provider_session(cfg, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC), sleep=asyncio.sleep):
    journal = Journal(cfg.state_dir, clock)
    return ProviderSession(cfg, journal=journal, clock=clock, sleep=sleep,
                           timers=Timers(journal=journal, clock=clock, sleep=sleep))


def llm(cfg, exec_: FakeExec, tmp_path: Path) -> ProviderLLM:
    return ProviderLLM(cfg, exec_=exec_, fs=LocalFileSystem(), env=ENV, cwd=tmp_path / "checkout", timeout=600,
                       session=provider_session(cfg))


def req(surface: str, tier="medium", worktree=None, rendered="do the thing", effort="high") -> LLMRequest:
    return LLMRequest(surface=surface, rendered=rendered, tier=tier, effort=effort, ticket="t-1", worktree=worktree)


def jsonl(*events: dict) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


def claude_ok(text="done") -> str:
    return jsonl(
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": text,
        },
    )


def codex_ok(text="patched") -> str:
    return jsonl(
        {"type": "thread.started", "thread_id": "th"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "first"}},
        {"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "ls"}},
        {"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": text}},
        {"type": "turn.completed"},
    )


def call(cfg, exec_, tmp_path, request):
    return asyncio.run(llm(cfg, exec_, tmp_path).call(request))


# --- registry + routing parse ---


def test_routing_takes_first_candidate_and_inherits_models_by_tier(tmp_path):
    cfg = config(tmp_path)
    served = resolve(cfg, "medium", "implement")
    assert (served.provider.name, served.model) == ("codex", "x-med")
    served = resolve(cfg, "high", "implement")
    assert (served.provider.name, served.model) == ("claude", "c-high")


def test_routing_candidate_model_pin_overrides_models_by_tier(tmp_path):
    served = resolve(config(tmp_path), "medium", "review")
    assert (served.provider.name, served.model) == ("claude", "c-max")


def test_routing_hole_is_a_setup_refusal_naming_the_row(tmp_path):
    with pytest.raises(ProviderSetupError, match="tier 'max', surface 'implement'"):
        resolve(config(tmp_path), "max", "implement")


def test_placeholder_row_is_refused_before_any_call(tmp_path):
    cfg = config(tmp_path, CONFIG.replace("x-med", PLACEHOLDER))
    exec_ = FakeExec()
    with pytest.raises(ProviderSetupError, match="set the operator values in config.yaml"):
        call(cfg, exec_, tmp_path, req("implement", worktree=tmp_path))
    assert exec_.calls == []


def test_implement_routed_to_api_provider_fails_config_load(tmp_path):
    text = CONFIG.replace("name: codex\n    kind: cli", "name: codex\n    kind: api")
    (tmp_path / "config.yaml").write_text(text)
    with pytest.raises(ConfigError) as exc:
        load_config(None, cwd=tmp_path)
    assert exc.value.key == "routing.0.candidates.0.provider"
    assert "kind: cli" in str(exc.value)


def test_unknown_cli_provider_has_no_adapter(tmp_path):
    cfg = config(tmp_path, CONFIG.replace("name: codex", "name: gemini").replace("provider: codex", "provider: gemini"))
    with pytest.raises(ProviderSetupError, match="no shipped cli adapter"):
        llm(cfg, FakeExec(), tmp_path)


def test_instance_config_routes_every_tier_for_review_author_implement():
    cfg = load_config(None, cwd=ROOT)
    assert cfg.state_dir == ROOT / ".tempo" / "state"
    for tier in ("low", "medium", "high", "max"):
        implement = resolve(cfg, tier, "implement")
        assert implement.provider.name == "codex" and implement.provider.kind == "cli"
        assert implement.model == getattr(implement.provider.models_by_tier, tier)
        author = resolve(cfg, tier, "author")
        assert author.provider.name == "claude"
        assert author.model == getattr(author.provider.models_by_tier, tier)
        review = resolve(cfg, tier, "review")
        assert (review.provider.name, review.model) == ("claude", cfg.providers[0].models_by_tier.max)
    llm(cfg, FakeExec(), ROOT)  # every provider has an adapter


# --- key scoping ---


def test_non_llm_child_env_carries_no_provider_key(tmp_path):
    env = child_env(ENV, config(tmp_path))
    assert env == {"PATH": "/usr/bin", "HOME": "/home/op"}


def test_llm_call_env_restores_only_the_serving_providers_key(tmp_path):
    cfg = config(tmp_path)
    exec_ = FakeExec((0, codex_ok(), ""), (0, claude_ok(), ""))
    client = llm(cfg, exec_, tmp_path)
    asyncio.run(client.call(req("implement", worktree=tmp_path)))
    asyncio.run(client.call(req("review")))
    codex_env, claude_env = exec_.calls[0]["env"], exec_.calls[1]["env"]
    assert codex_env == {"PATH": "/usr/bin", "HOME": "/home/op", "CODEX_KEY": "sk-codex-secret"}
    assert claude_env == {"PATH": "/usr/bin", "HOME": "/home/op", "CLAUDE_KEY": "sk-claude-secret"}


def test_serving_key_unset_is_a_setup_refusal(tmp_path):
    env = {k: v for k, v in ENV.items() if k != "CODEX_KEY"}
    with pytest.raises(ProviderSetupError, match="CODEX_KEY"):
        child_env(env, config(tmp_path), config(tmp_path).providers[1])


def test_echoed_secret_is_redacted_from_result_and_both_capture_sinks(tmp_path):
    cfg = config(tmp_path)
    leak = "env says sk-codex-secret and sk-claude-secret"
    exec_ = FakeExec((0, codex_ok(leak), "stderr sk-codex-secret"))
    result = call(cfg, exec_, tmp_path, req("implement", worktree=tmp_path))
    assert result.text == "env says [REDACTED:CODEX_KEY] and [REDACTED:CLAUDE_KEY]"
    captured = "".join(p.read_text() for p in (cfg.state_dir / "spools").rglob("*") if p.is_file())
    assert "sk-" not in captured
    assert "[REDACTED:CODEX_KEY]" in captured


# --- claude adapter live contract ---


def test_claude_implement_gets_the_write_grant_in_the_worktree(tmp_path):
    cfg = config(tmp_path)
    exec_ = FakeExec((0, claude_ok(), ""))
    worktree = tmp_path / "wt"
    call(cfg, exec_, tmp_path, req("implement", tier="high", worktree=worktree))
    inv = exec_.calls[0]
    assert inv["argv"] == [
        "claude", "-p", "--output-format", "stream-json", "--verbose", "--model", "c-high",
        "--permission-mode", "bypassPermissions",
    ]
    assert inv["cwd"] == worktree


def test_claude_read_only_surface_gets_the_read_tool_allowlist_and_no_grant(tmp_path):
    cfg = config(tmp_path)
    exec_ = FakeExec((0, claude_ok(), ""))
    call(cfg, exec_, tmp_path, req("review", worktree=tmp_path / "wt"))
    inv = exec_.calls[0]
    assert inv["argv"][-5:] == ["--allowedTools", "Read", "Grep", "Glob", "LS"]
    assert "bypassPermissions" not in inv["argv"]
    assert inv["cwd"] == tmp_path / "checkout"


def test_claude_prompt_goes_on_stdin_never_argv(tmp_path):
    exec_ = FakeExec((0, claude_ok(), ""))
    call(config(tmp_path), exec_, tmp_path, req("review", rendered="PROMPT-BODY " * 50))
    assert exec_.calls[0]["stdin"] == "PROMPT-BODY " * 50
    assert not any("PROMPT-BODY" in a for a in exec_.calls[0]["argv"])


def test_claude_text_comes_from_the_result_event(tmp_path):
    result = call(config(tmp_path), FakeExec((0, claude_ok("verdict"), "")), tmp_path, req("review"))
    assert (result.text, result.provider, result.model) == ("verdict", "claude", "c-max")


@pytest.mark.parametrize(
    "rc,out",
    [
        (1, ""),
        (0, jsonl({"type": "system", "subtype": "init"})),
        (0, jsonl({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": ""})),
    ],
)
def test_claude_failure_shapes_raise_call_error(tmp_path, rc, out):
    with pytest.raises(ProviderCallError):
        call(config(tmp_path), FakeExec((rc, out, "boom")), tmp_path, req("review"))


# --- codex adapter live contract ---


def test_codex_implement_gets_the_write_grant_in_the_worktree(tmp_path):
    cfg = config(tmp_path)
    exec_ = FakeExec((0, codex_ok(), ""))
    worktree = tmp_path / "wt"
    call(cfg, exec_, tmp_path, req("implement", worktree=worktree, effort="high"))
    inv = exec_.calls[0]
    assert inv["argv"] == [
        "codex", "exec", "--json", "-m", "x-med", "-c", "model_reasoning_effort=high",
        "--dangerously-bypass-approvals-and-sandbox", "-",
    ]
    assert inv["cwd"] == worktree
    assert inv["stdin"] == "do the thing"


def test_codex_read_only_surface_is_sandboxed_read_only_with_no_approvals(tmp_path):
    exec_ = FakeExec((0, codex_ok(), ""))
    call(config(tmp_path), exec_, tmp_path, req("review", tier="low", effort="low"))
    inv = exec_.calls[0]
    assert inv["argv"] == [
        "codex", "exec", "--json", "-m", "x-low", "-c", "model_reasoning_effort=low",
        "--sandbox", "read-only", "-c", "approval_policy=never", "-",
    ]
    assert "--dangerously-bypass-approvals-and-sandbox" not in inv["argv"]
    assert inv["cwd"] == tmp_path / "checkout"


def test_codex_text_is_last_agent_message(tmp_path):
    result = call(config(tmp_path), FakeExec((0, codex_ok("final"), "")), tmp_path, req("implement", worktree=tmp_path))
    assert (result.text, result.provider, result.model) == ("final", "codex", "x-med")


@pytest.mark.parametrize(
    "out",
    [
        jsonl({"type": "turn.started"}, {"type": "turn.failed", "error": {"message": "model unavailable"}}),
        jsonl({"type": "error", "message": "stream disconnected"}),
        jsonl({"type": "turn.started"}, {"type": "turn.completed", "usage": {}}),
    ],
)
def test_codex_failed_turn_raises_even_on_exit_zero(tmp_path, out):
    with pytest.raises(ProviderCallError):
        call(config(tmp_path), FakeExec((0, out, "")), tmp_path, req("implement", worktree=tmp_path))


def test_codex_nonzero_exit_carries_the_stream_failure_message(tmp_path):
    out = jsonl({"type": "turn.started"},
                {"type": "turn.failed", "error": {"message": "Selected model is at capacity."}})
    with pytest.raises(ProviderCallError, match="at capacity") as e:
        call(config(tmp_path), FakeExec((1, out, "")), tmp_path, req("implement", worktree=tmp_path))
    assert e.value.rc == 1


# --- shared base ---


def test_implement_without_worktree_is_refused_before_spawn(tmp_path):
    exec_ = FakeExec()
    with pytest.raises(ProviderSetupError, match="worktree"):
        call(config(tmp_path), exec_, tmp_path, req("implement"))
    assert exec_.calls == []


def test_abort_current_group_kills_the_active_child(tmp_path):
    exec_ = FakeExec()
    exec_.hang = asyncio.Event()
    assert isinstance(exec_, GroupExec)
    client = llm(config(tmp_path), exec_, tmp_path)

    async def scenario():
        task = asyncio.ensure_future(client.call(req("implement", worktree=tmp_path)))
        while not exec_.calls:
            await asyncio.sleep(0)
        client.abort_current()
        with pytest.raises(ProviderCallError):
            await task

    asyncio.run(scenario())
    assert exec_.killed == [4242]
    client.abort_current()  # idle: no active child, nothing to kill
    assert exec_.killed == [4242]


# --- provider preflight (section 6.11) ---


class PreflightExec:
    """Answers `--version`, `pnpm view`, and per-model probes; a model listed in `refused` fails its turn."""

    def __init__(self, versions: dict[str, str], latest: str, refused: frozenset[str] = frozenset()) -> None:
        self.versions, self.latest, self.refused = versions, latest, refused
        self.probed: list[str] = []
        self.version_envs: list[dict[str, str]] = []

    async def run(self, argv, *, cwd, env, timeout, stdin_path=None, on_spawn=None, on_stdout_line=None):
        if argv[1:] == ["--version"]:
            self.version_envs.append(dict(env))
            return 0, self.versions[argv[0]], ""
        if argv[0] == "pnpm":
            self.version_envs.append(dict(env))
            return 0, self.latest + "\n", ""
        model = argv[argv.index("--model" if argv[0] == "claude" else "-m") + 1]
        self.probed.append(model)
        if model in self.refused:
            return 1, jsonl({"type": "error", "message": f"The '{model}' model is not supported"}), ""
        return 0, (claude_ok("ok") if argv[0] == "claude" else codex_ok("ok")), ""


CURRENT = {"claude": "1.2.3 (Claude Code)", "codex": "codex-cli 1.2.3"}


def preflight(tmp_path, exec_) -> list[str]:
    return asyncio.run(llm(config(tmp_path), exec_, tmp_path).preflight())


def test_preflight_passes_with_current_clis_and_every_routed_model_answering(tmp_path):
    exec_ = PreflightExec(CURRENT, "1.2.3")
    assert preflight(tmp_path, exec_) == []
    assert sorted(exec_.probed) == ["c-high", "c-max", "c-med", "x-low", "x-med"]  # each routed pair once


def test_preflight_version_children_carry_no_provider_key(tmp_path):
    exec_ = PreflightExec(CURRENT, "1.2.3")
    assert preflight(tmp_path, exec_) == []
    assert len(exec_.version_envs) == 4
    for env in exec_.version_envs:
        assert "CLAUDE_KEY" not in env
        assert "CODEX_KEY" not in env
        assert env["PATH"] == ENV["PATH"]
        assert env["HOME"] == ENV["HOME"]


def test_preflight_reports_a_stale_cli_without_refusing_the_run(tmp_path):
    llm_ = llm(config(tmp_path), PreflightExec({**CURRENT, "codex": "codex-cli 1.2.0"}, "1.2.3"), tmp_path)
    assert asyncio.run(llm_.preflight()) == []
    [notice] = llm_.preflight_notices
    assert "codex 1.2.0 is not the latest release 1.2.3" in notice and "pnpm add -g test-cli@latest" in notice


def test_preflight_refuses_a_model_the_login_cannot_serve(tmp_path):
    [problem] = preflight(tmp_path, PreflightExec(CURRENT, "1.2.3", refused=frozenset({"x-low"})))
    assert "codex model 'x-low' failed its probe" in problem and "not supported" in problem


def test_cli_provider_must_declare_its_package(tmp_path):
    with pytest.raises(ConfigError, match="declares no `package`"):
        config(tmp_path, CONFIG.replace("    package: test-cli\n    auth: CODEX_KEY", "    auth: CODEX_KEY"))


def classifier_adapter(cfg, name, tmp_path):
    return ADAPTERS[name](next(p for p in cfg.providers if p.name == name), config=cfg,
                          exec_=FakeExec(), fs=LocalFileSystem(), redactor=Redactor.from_config(cfg, ENV),
                          env=ENV, cwd=tmp_path, capture_dir=cfg.state_dir / "spools", timeout=600)


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_cli_failure_classifier_closed_vocabulary_and_precedence(tmp_path, name):
    adapter = classifier_adapter(config(tmp_path), name, tmp_path)
    # Spell the contractual literals independently of the implementation's marker table.
    classes = [
        ("auth_error", adapter.AUTH_MARKERS),
        ("quota_exhausted", ("quota exhausted", "usage limit reached", "out of extra usage")),
        ("rate_limited", ("rate limit", "too many requests")),
        ("outage", ("service unavailable", "internal server error", "stream disconnected", "at capacity")),
        ("model_error", ("model unavailable", "model not found", "model is not supported")),
    ]
    observed = set()
    for index, (failure, markers) in enumerate(classes):
        for marker in markers:
            for message, tail in ((marker.upper(), ""), ("adapter failed", marker.upper())):
                error = adapter.classify_failure(message, rc=1, stderr_tail=tail)
                observed.add(error.failure_class)
                assert error.failure_class == failure
            lower_precedence = " ".join(words[0] for _, words in classes[index + 1:])
            assert adapter.classify_failure(marker, rc=0, stderr_tail=lower_precedence).failure_class == failure
    unknown = adapter.classify_failure("novel diagnostic", rc=37, stderr_tail="unknown")
    observed.add(unknown.failure_class)
    assert observed == {"auth_error", "quota_exhausted", "rate_limited", "outage", "model_error", "unclassified"}
    assert unknown.rc == 37 and unknown.stderr_tail == "unknown" and unknown.paved_road is None


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_cli_failure_classifier_handles_exit_zero_error_events(tmp_path, name, monkeypatch):
    adapter = classifier_adapter(config(tmp_path), name, tmp_path)
    out = (jsonl({"type": "result", "subtype": "error", "is_error": True, "result": "usage limit reached"})
           if name == "claude" else jsonl({"type": "turn.failed", "error": {"message": "usage limit reached"}}))
    error = adapter.parsed_failure(out, rc=0, stderr_tail="")
    assert error.failure_class == "quota_exhausted" and error.rc == 0
    out_error = adapter.parsed_failure(out, rc=1, stderr_tail="")
    assert out_error.failure_class == "quota_exhausted" and out_error.rc == 1
    seen = []
    original = adapter.classify_failure

    def probe(message, **kwargs):
        seen.append(message)
        return original(message, **kwargs)

    monkeypatch.setattr(adapter, "classify_failure", probe)
    success = (claude_ok if name == "claude" else codex_ok)("not logged in; rate limit; at capacity")
    assert adapter.parsed_failure(success, rc=0, stderr_tail="service unavailable") is None
    assert seen == []  # neither successful text nor incidental success stderr enters classification
    error = adapter.parsed_failure(success, rc=9, stderr_tail="mystery")
    assert error.failure_class == "unclassified" and seen == ["nonzero exit"]


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_classified_error_preserves_scrubbed_evidence_and_login_road(tmp_path, name):
    from tempo.driver import Driver, LlmStage
    from tempo.llm import FakeLLM
    from pydantic import BaseModel

    cfg = config(tmp_path)
    adapter = classifier_adapter(cfg, name, tmp_path)
    redactor = Redactor.from_config(cfg, ENV)
    tail = redactor.scrub("z" * 2100 + " sk-claude-secret sk-codex-secret")[-2000:]
    errors = []
    for marker, failure in ((adapter.AUTH_MARKERS[0], "auth_error"), ("rate limit", "rate_limited"),
                            ("quota exhausted", "quota_exhausted"), ("at capacity", "outage"),
                            ("model not found", "model_error"), ("new failure", "unclassified")):
        # JSON escapes bypass raw-stream scrubbing; the decoded failure message must still be scrubbed.
        message = marker + " sk-claude-secret sk-codex-secret"
        out = (jsonl({"type": "result", "is_error": True, "subtype": "error", "result": message})
               if name == "claude" else jsonl({"type": "turn.failed", "error": {"message": message}}))
        out = out.replace("sk-", "sk\\u002d")
        error = adapter.parsed_failure(redactor.scrub(out), rc=23, stderr_tail=tail)
        assert (error.provider, error.rc, error.stderr_tail, error.failure_class) == (name, 23, tail, failure)
        assert len(error.stderr_tail) == 2000 and "sk-" not in str(error)
        assert "[REDACTED:CLAUDE_KEY]" in str(error) and "[REDACTED:CODEX_KEY]" in str(error)
        if failure == "auth_error":
            assert error.paved_road == adapter.LOGIN_ROAD
        elif failure == "unclassified":
            assert error.paved_road is None
        else:
            assert name in error.paved_road and "retry" in error.paved_road
            assert ("route" if failure == "model_error" else "wait") in error.paved_road
        errors.append(error)

    class Reply(BaseModel):
        text: str

    async def sleep(_seconds):
        await asyncio.Event().wait()

    for index, error in enumerate(errors):
        driver = Driver.from_config(cfg, llm=FakeLLM([error]), env=ENV,
                                    clock=lambda: datetime(2026, 10, 6, tzinfo=UTC), sleep=sleep)
        result = asyncio.run(driver.run(LlmStage(surface="review", emits=Reply, gates=[], render=lambda *_: "probe"),
                                       None, ticket=f"classification-{index}", attempt=0, workspace=tmp_path,
                                       tier="medium", effort="low", stuck_budget=600))
        assert result.outcome == "infra_error"
        if error.failure_class == "unclassified":
            assert result.findings == []
        else:
            [finding] = result.findings
            assert (finding.code, finding.message, finding.paved_road) == (error.failure_class, str(error), error.paved_road)


def test_cli_failure_classifier_is_active(tmp_path, monkeypatch):
    from tempo import __main__, runner
    from tempo.driver import LlmStage
    from tempo.tickets import validate_ticket
    from pydantic import BaseModel

    config(tmp_path)
    hits, results = [], []
    original = CliAdapter.classify_failure

    def probe(self, message, **kwargs):
        hits.append(self.provider.name)
        return original(self, message, **kwargs)

    monkeypatch.setattr(CliAdapter, "classify_failure", probe)

    class Reply(BaseModel):
        text: str

    async def drive(ctx, _ticket):
        for surface in ("implement", "review"):
            result = await ctx.driver.run(LlmStage(surface=surface, emits=Reply, gates=[], render=lambda *_: "probe"),
                                          None, ticket="dormancy", attempt=0, workspace=tmp_path,
                                          tier="medium", effort="low", stuck_budget=600)
            assert result.outcome == "infra_error"
            results.append(result)
        return "infra_error"

    monkeypatch.setattr(runner, "drive", drive)  # stop before host work; retain the production composition

    class CompositionExec(PreflightExec):
        async def run(self, argv, **kwargs):
            if kwargs.get("stdin_path") is not None and Path(kwargs["stdin_path"]).read_text() != "Reply with the single word ok.":
                return 0, (jsonl({"type": "turn.failed", "error": {"message": "service unavailable"}})
                           if argv[0] == "codex" else jsonl({"type": "result", "subtype": "error", "is_error": True,
                                                            "result": "rate limit"})), ""
            return await super().run(argv, **kwargs)

    monkeypatch.setattr(__main__, "SubprocessExec", lambda: CompositionExec(CURRENT, "1.2.3"))

    def run_ticket(stem, checkout, dispatch):
        ticket = validate_ticket(stem, textwrap.dedent("""\
            ---
            priority: P2
            kind: chore
            source: human
            state: confirmed
            ---
            ## Depends on
            none
            ## Context
            - config.yaml
            ## Goal / Why
            Exercise provider classifier dormancy through production composition.
            ## Scope in / Scope out
            In: provider calls. Out: host work.
            ## Scope fence
            - config.yaml
            ## Acceptance criteria
            1. `uv run pytest -q tests/test_providers.py` exits 0.
            ## Verification
            ```
            uv run pytest -q tests/test_providers.py
            ```
            ## Definition of rejected
            Provider failure classification runs in the default composition.
            ## Time budget
            - stuck: 60m
            """), checkout.repo)

        async def run():
            await dispatch(ticket)
            return 0
        return run()

    monkeypatch.setattr(runner, "run_ticket", run_ticket)

    def assert_dormant():
        hits.clear()
        results.clear()
        # main's default pipeline is the real runner.pipeline; it builds ProviderLLM,
        # performs preflight, and binds Driver + StageContext before this call runs.
        assert __main__.main(["run", "dormancy"], cwd=tmp_path, env=ENV,
                             clock=lambda: datetime(2026, 10, 6, tzinfo=UTC)) == 0
        assert len(results) == 2
        assert hits == ['codex', 'claude']
        assert [r.findings[0].code for r in results] == ['outage', 'rate_limited']

    assert_dormant()

    def wired(self, message, **kwargs):
        raise ProviderCallError(self.provider.name, message, **kwargs)

    monkeypatch.setattr(CliAdapter, "_raise_call_error", wired)
    with pytest.raises(AssertionError):
        assert_dormant()
    assert hits == []


class PythonProviderExec(SubprocessExec):
    """Replace only the CLI binary with a scripted Python child; retain real group/pipe execution."""

    def __init__(self, script):
        self.script = script
        self.calls = []
        self.pgids = []

    async def run(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        spawn = kwargs.get("on_spawn")

        def spawned(pgid):
            self.pgids.append(pgid)
            if spawn is not None:
                spawn(pgid)

        return await super().run([sys.executable, "-c", self.script], **{**kwargs, "on_spawn": spawned})


def python_client(cfg, process, tmp_path, *, timeout=10, env=None):
    return ProviderLLM(cfg, exec_=process, fs=LocalFileSystem(),
                       env=env or {**os.environ, **ENV, "PATH": os.environ["PATH"]},
                       cwd=tmp_path, timeout=timeout, session=provider_session(cfg))


def provider_request(name, tmp_path, **kwargs):
    return req("implement", tier="high" if name == "claude" else "medium", worktree=tmp_path, **kwargs)


@pytest.mark.parametrize("fail_at_tail", [False, True])
def test_raising_line_callback_still_drains_and_reaps(tmp_path, fail_at_tail):
    marker = tmp_path / "finished"
    out = "first\n" + "x" * 300000 + "\nlast"
    err = "e" * 300000 + "stderr tail"
    script = (
        "import os, threading\n"
        "thread = threading.Thread(target=lambda: os.write(2, b'e' * 300000 + b'stderr tail'))\n"
        "thread.start()\n"
        "os.write(1, b'first\\n' + b'x' * 300000 + b'\\nlast')\n"
        "thread.join()\n"
        f"open({str(marker)!r}, 'w').close()\n"
    )
    process = PythonProviderExec(script)
    error = RuntimeError("line consumer failed")
    lines = []

    def consume(line):
        lines.append(line)
        if not fail_at_tail or line == "last":
            raise error

    async def scenario():
        before = set(asyncio.all_tasks())
        with pytest.raises(RuntimeError) as caught:
            await process.run([], cwd=tmp_path, env=os.environ, timeout=5, on_stdout_line=consume)
        assert caught.value is error
        assert error.process_capture == (out, err)
        assert set(asyncio.all_tasks()) == before
        with pytest.raises(ProcessLookupError):
            os.kill(process.pgids[-1], 0)

    asyncio.run(scenario())
    assert marker.exists()
    assert lines == (["first\n", "x" * 300000 + "\n", "last"] if fail_at_tail else ["first\n"])


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_consumer_error_keeps_call_capture(tmp_path, name):
    cfg = config(tmp_path)
    first = jsonl({"type": "tool", "text": "sk-codex-secret"})
    terminal = (claude_ok if name == "claude" else codex_ok)("sk-claude-secret").rstrip("\n")
    out = first + "x" * 300000 + "\n" + terminal
    err = "e" * 300000 + "sk-codex-secret stderr tail"
    script = (
        "import os, threading\n"
        "thread = threading.Thread(target=lambda: os.write(2, b'e' * 300000 + b'sk-codex-secret stderr tail'))\n"
        "thread.start()\n"
        f"os.write(1, {first.encode()!r})\n"
        "os.write(1, b'x' * 300000 + b'\\n')\n"
        f"os.write(1, {terminal.encode()!r})\n"
        "thread.join()\n"
    )
    process = PythonProviderExec(script)
    client = python_client(cfg, process, tmp_path)
    error = RuntimeError("consumer failed")
    events = []

    def consume(event):
        events.append(event)
        raise error

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(client.call(provider_request(name, tmp_path), consumer=EventConsumer(consume)))
    assert caught.value is error
    assert events == [{"type": "tool", "text": "[REDACTED:CODEX_KEY]"}]
    [capture] = (cfg.state_dir / "spools/providers/t-1").iterdir()
    redactor = Redactor.from_config(cfg, ENV)
    assert (capture / "events.jsonl").read_bytes() == redactor.scrub(out).encode()
    assert (capture / "stderr.txt").read_bytes() == redactor.scrub(err).encode()
    assert (capture / "prompt.md").read_text() == "do the thing"
    assert client._active is None and client._adapters[name]._pgid is None


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_inflight_events_are_scrubbed_and_capture_is_preserved(tmp_path, name):
    cfg = config(tmp_path)
    # Resolve secrets via the actual client construction, including escaped and nested strings.
    env = {**os.environ, **ENV, "PATH": os.environ["PATH"], "CODEX_KEY": "secret\n雪"}
    leak = env["CODEX_KEY"] + " sk-claude-secret"
    event = {"type": "tool", "payload": [{leak: leak}, 4, None, True]}
    terminal = (claude_ok if name == "claude" else codex_ok)(leak).rstrip("\n")
    out = "warning\n{broken\n[]\nnull\n42\n" + jsonl(event) + terminal
    err = "e" * 300000 + leak
    # Byte-sized writes fragment JSON escapes and UTF-8 without relying on read chunk boundaries.
    script = (
        "import os, sys, threading\n"
        "assert sys.stdin.read() == 'prompt [REDACTED:CLAUDE_KEY]'\n"
        f"data = {out.encode()!r}\n"
        f"thread = threading.Thread(target=lambda: os.write(2, b'e' * 300000 + {leak.encode()!r}))\n"
        "thread.start()\n"
        "for byte in data: os.write(1, bytes([byte]))\n"
        "thread.join()\n"
    )
    process = PythonProviderExec(script)
    client = python_client(cfg, process, tmp_path, env=env)
    events = []
    request = provider_request(name, tmp_path, rendered="prompt sk-claude-secret")
    watched = asyncio.run(client.call(request, consumer=EventConsumer(events.append)))
    ordinary = asyncio.run(client.call(request))
    assert watched == ordinary
    assert watched.text == "[REDACTED:CODEX_KEY] [REDACTED:CLAUDE_KEY]"
    assert events[0] == {"type": "tool", "payload": [
        {"[REDACTED:CODEX_KEY] [REDACTED:CLAUDE_KEY]": "[REDACTED:CODEX_KEY] [REDACTED:CLAUDE_KEY]"}, 4, None, True]}
    assert len(events) == 1 + len([json.loads(line) for line in terminal.splitlines()])
    assert events[-1]["type"] == ("result" if name == "claude" else "turn.completed")
    decoded_events = json.dumps(events, ensure_ascii=False)
    assert env["CODEX_KEY"] not in decoded_events and env["CLAUDE_KEY"] not in decoded_events
    redactor = Redactor.from_config(cfg, env)
    captures = sorted((cfg.state_dir / "spools/providers/t-1").iterdir())
    assert len(captures) == 2
    for capture in captures:
        assert {p.name for p in capture.iterdir()} == {"prompt.md", "events.jsonl", "stderr.txt"}
        assert (capture / "events.jsonl").read_bytes() == redactor.scrub(out).encode()
        assert (capture / "stderr.txt").read_bytes() == redactor.scrub(err).encode()
        assert (capture / "prompt.md").read_text() == "prompt [REDACTED:CLAUDE_KEY]"
    assert "on_stdout_line" in process.calls[0][1]
    assert "on_stdout_line" not in process.calls[1][1]
    assert client._active is None and client._adapters[name]._pgid is None


@pytest.mark.parametrize("name", ["claude", "codex"])
@pytest.mark.parametrize("unwind", ["timeout", "cancel", "callback_timeout", "callback_cancel", "abort"])
def test_event_callback_unwind_kills_group(tmp_path, name, unwind):
    marker = tmp_path / "grandchild-wrote"
    grandchild = "import time; time.sleep(0.5); open(" + repr(str(marker)) + ", 'w').close(); time.sleep(30)"
    script = (
        "import os, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
        "print('{\"type\": \"tool\"}', flush=True)\n"
        # Fill both pipes: exceptional cleanup must drain even if the callback reader failed.
        "os.write(1, b'x' * 300000)\n"
        "os.write(2, b'e' * 300000)\n"
        "time.sleep(30)\n"
    )
    process = PythonProviderExec(script)
    cfg = config(tmp_path)
    client = python_client(cfg, process, tmp_path, timeout=0.15 if unwind in {"timeout", "callback_timeout"} else 10)
    error = RuntimeError("consumer failed")

    async def scenario():
        before = set(asyncio.all_tasks())
        received = asyncio.Event()

        def consume(event):
            assert event == {"type": "tool"}
            assert client._active is client._adapters[name]
            assert client._active._pgid == process.pgids[-1]
            received.set()
            if unwind.startswith("callback_"):
                raise error

        task = asyncio.create_task(client.call(provider_request(name, tmp_path), consumer=EventConsumer(consume)))
        await asyncio.wait_for(received.wait(), 5)
        if unwind in {"cancel", "callback_cancel"}:
            task.cancel()
        elif unwind == "abort":
            client.abort_current()
        expected = {"timeout": TimeoutError, "cancel": asyncio.CancelledError,
                    "callback_timeout": TimeoutError, "callback_cancel": asyncio.CancelledError, "abort": ProviderCallError}[unwind]
        with pytest.raises(expected) as caught:
            await asyncio.wait_for(task, 5)
        if unwind.startswith("callback_"):
            assert caught.value.__cause__ is error
            [capture] = (cfg.state_dir / "spools/providers/t-1").iterdir()
            out, err = error.process_capture
            assert out.startswith('{"type": "tool"}\n')
            assert (capture / "events.jsonl").read_text() == out
            assert (capture / "stderr.txt").read_text() == err
        if unwind in {"cancel", "callback_cancel"}:
            assert task.cancelled()
        assert set(asyncio.all_tasks()) == before
        assert client._active is None and client._adapters[name]._pgid is None
        client.abort_current()
        with pytest.raises(ProcessLookupError):
            os.kill(process.pgids[-1], 0)  # The direct child was reaped.

    asyncio.run(scenario())
    time.sleep(0.6)
    assert not marker.exists()  # A direct-child-only kill would leave the grandchild writing.


@pytest.mark.parametrize('names,expected', [
    (['claude'], ['CLAUDE.md']), (['codex'], ['AGENTS.md']),
    (['claude', 'codex'], ['CLAUDE.md', 'AGENTS.md']),
    (['codex', 'codex', 'claude'], ['AGENTS.md', 'CLAUDE.md']),
])
def test_routed_conduct_paths(tmp_path, names, expected):
    from tests.test_cli import core_config
    from tempo.providers import routed_conduct_paths
    path = core_config(tmp_path, [('medium', 'implement', names), ('high', 'review', names)])
    config = load_config(path, cwd=tmp_path)
    assert routed_conduct_paths(config, tmp_path) == tuple(tmp_path / p for p in expected)
    # The declared but unrouted provider must never add a path.
    assert len(config.providers) == 2
