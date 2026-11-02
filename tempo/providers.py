"""Provider layer (TEMPO_PLAN.md section 6): registry + routing, key-scoped child env, the cli adapters.

Session admission selects from the ordered route before its LLM effect. `CliAdapter` is the
shared base and the trust boundary -- write-grant derivation, redaction of both sinks (the captured
stream and the returned result) live there ONCE; each subclass is only its CLI's
argv contract and event-stream parse.
"""

import asyncio
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from tempo.config import Config, ConfigSnapshot, Provider
from tempo.llm import AgentEffort, AgentTier, LLMRequest, LLMResult
from tempo.redact import Redactor
from tempo.seams import FileSystem, GroupExec
from tempo.journal import EventType, render_ts

if TYPE_CHECKING:
    from tempo.watchdog import EventConsumer

# Closed allowlist of tree-writing surfaces: every other surface is called read-only.
WRITING_SURFACES = frozenset({"implement"})

# Section 0: the value prompt 1 authors when the operator stated none; it must never reach a model.
PLACEHOLDER = "OPERATOR-SETS-THIS"

FailureClass = Literal["rate_limited", "quota_exhausted", "outage", "auth_error", "model_error", "unclassified"]

FAILURE_MARKERS: tuple[tuple[FailureClass, tuple[str, ...]], ...] = (
    ("quota_exhausted", ("quota exhausted", "usage limit reached", "out of extra usage")),
    ("rate_limited", ("rate limit", "too many requests")),
    ("outage", ("service unavailable", "internal server error", "stream disconnected", "at capacity")),
    ("model_error", ("model unavailable", "model not found", "model is not supported")),
)


class ProviderSetupError(Exception):
    """A config/setup refusal raised before any call runs: nothing was metered."""


class ProviderCallError(Exception):
    """The CLI ran and failed. `stderr_tail` is scrubbed."""

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        rc: int,
        stderr_tail: str,
        failure_class: str | None = None,
        paved_road: str | None = None,
    ) -> None:
        super().__init__(f"{provider}: {message} (exit {rc}); stderr tail: {stderr_tail!r}")
        self.provider = provider
        self.rc = rc
        self.stderr_tail = stderr_tail
        self.failure_class = failure_class
        self.paved_road = paved_road


def secret_names(config: Config | ConfigSnapshot) -> frozenset[str]:
    return frozenset(p.auth for p in config.providers if p.auth)


def child_env(
    env: Mapping[str, str], config: Config | ConfigSnapshot,
    serving: Provider | ConfigSnapshot | None = None,
) -> dict[str, str]:
    """INHERIT-MINUS-SECRETS: the full parent env minus every provider key, plus only the serving call's own.

    Every non-LLM child (checks, verification, replay) passes `serving=None` and gets no key at all.
    """
    out = {k: v for k, v in env.items() if k not in secret_names(config)}
    if serving is not None and serving.auth:
        if serving.auth not in env:
            raise ProviderSetupError(
                f"provider {serving.name!r} needs env var {serving.auth} and it is unset; export it before the run"
            )
        out[serving.auth] = env[serving.auth]
    return out


@dataclass(frozen=True)
class Served:
    provider: Provider | ConfigSnapshot
    model: str


def resolve(config: Config | ConfigSnapshot, tier: AgentTier, surface: str) -> Served:
    """The FIRST candidate of the (tier, surface) row; its model, else the provider's models_by_tier[tier]."""
    route = next((r for r in config.routing if r.tier == tier and r.surface == surface), None)
    if route is None and surface != "review":
        route = next((r for r in config.routing if r.tier == tier and r.surface == "review"), None)
    if route is None:
        raise ProviderSetupError(
            f"no routing row for tier {tier!r}, surface {surface!r}; add one under `routing:` in config.yaml"
        )
    return _candidate(config, tier, surface, route.candidates[0])


def _candidate(config, tier, surface, cand):
    provider = next(p for p in config.providers if p.name == cand.provider)  # load_config checked the reference
    model = cand.model if cand.model is not None else getattr(provider.models_by_tier, tier)
    if PLACEHOLDER in (model, provider.auth):
        raise ProviderSetupError(
            f"routing for tier {tier!r}, surface {surface!r} resolves to provider {provider.name!r} still carrying"
            f" {PLACEHOLDER}; set the operator values in config.yaml"
        )
    return Served(provider, model)


def candidates(config, tier, surface) -> tuple[Served, ...]:
    # Resolve through the existing routing/placeholder owner, including the review fallback.
    route = next((r for r in config.routing if r.tier == tier and r.surface == surface), None)
    if route is None:
        resolve(config, tier, surface)
        route = next(r for r in config.routing if r.tier == tier and r.surface == "review")
    return tuple(_candidate(config, tier, surface, candidate) for candidate in route.candidates)


def routed_conduct_paths(config: Config | ConfigSnapshot, checkout: Path) -> tuple[Path, ...]:
    """Select each resolved CLI's conduct file once; unrouted declarations do not participate."""
    return tuple(dict.fromkeys(
        checkout / ("CLAUDE.md" if served.provider.name == "claude" else "AGENTS.md")
        for route in config.routing for served in candidates(config, route.tier, route.surface)
    ))


class ProviderDrought(Exception):
    """A pre-effect routing refusal, carried to the existing failure owner."""

    def __init__(self, tier, surface, refusal):
        self.record = {"tier": tier, "surface": surface, "providers": list(refusal.providers)}
        self.waited_seconds = refusal.waited_seconds
        self.deadline = refusal.open_until
        self.paved_road = refusal.paved_road
        super().__init__(f"provider_drought: {', '.join(refusal.providers)}; {self.paved_road}")


class ProviderSession:
    """One idle registry, admission owner and Timer lifetime per lock holder."""

    def __init__(self, config, *, journal, clock, sleep, timers):
        from tempo.thresh import Thresh
        self.journal, self.clock, self.sleep, self.timers = journal, clock, sleep, timers
        self.thresh = Thresh(config, journal=journal, clock=clock, sleep=sleep)
        self.auth_excluded: set[str] = set()
        self.auth_position: int | None = None
        self.active = False
        self.thresh.unavailable = self.unavailable
        self.thresh.on_outcome = self.outcome
        self.waits: set[asyncio.Task] = set()
        self._holds: dict[str, datetime] = {}
        self._held_seconds: dict[str, float] = {}

    def live_wait(self, prefix):
        return (self.thresh.live_wait(prefix)
                + sum(seconds for key, seconds in self._held_seconds.items() if key.startswith(prefix))
                + sum(max(0, (self.clock() - started).total_seconds())
                      for key, started in self._holds.items() if key.startswith(prefix)))

    async def wait_until(self, deadline):
        async def wait():
            while self.clock() < deadline:
                await self.sleep((deadline - self.clock()).total_seconds())
            self.timers.fire_due()
        task = asyncio.create_task(wait())
        self.waits.add(task)
        try:
            await asyncio.shield(task)
        finally:
            from tempo.driver import _observe
            task.cancel()
            await _observe(asyncio.gather(task, return_exceptions=True))
            self.waits.discard(task)

    async def close(self):
        from tempo.driver import _observe
        tasks = tuple(self.waits)
        for task in tasks:
            task.cancel()
        await _observe(asyncio.gather(*tasks, return_exceptions=True))
        self.waits.clear()

    def preflight_passed(self):
        if self.auth_position is None:
            self.auth_position = len(self.journal.read())

    def cooling(self):
        self.timers.fire_due()
        return {timer.body.timer_id.split('/')[1]: timer.deadline
                for timer in self.timers.pending.values()
                if timer.body.timer_id.startswith('provider-cooldown/')}

    def exclusions(self):
        if self.auth_position is not None:
            history = self.journal.read()
            self.auth_excluded.update(e.body['provider'] for e in history[self.auth_position:]
                if e.type == EventType.SIGNAL and e.body.get('signal') == 'provider_call_outcome'
                and e.body.get('failure_class') == 'auth_error')
            self.auth_position = len(history)
        return self.auth_excluded

    def unavailable(self, name):
        cooling = self.cooling()
        if name in self.exclusions():
            return True, cooling.get(name), ADAPTERS[name].LOGIN_ROAD
        if name in cooling:
            return True, cooling[name], f"wait until {render_ts(cooling[name])}"
        return False, None, None

    def quota(self, provider):
        if provider.name not in self.cooling():
            deadline = self.clock() + timedelta(minutes=provider.limits.quota_window_minutes)
            self.timers.arm(f"provider-cooldown/{provider.name}/{render_ts(deadline)}", deadline, ticket=None)

    def outcome(self, served, failure):
        # Thresh invokes this only after its sole outcome append succeeds.
        if failure == 'quota_exhausted':
            self.quota(served.provider)
        elif failure == 'auth_error':
            self.auth_excluded.add(served.provider.name)

    def drought(self, config, tier, surface):
        from tempo.thresh import UnavailableRoute
        route = candidates(config, tier, surface)
        refusal = self.thresh._select(route, 0, 0)
        return ProviderDrought(tier, surface, refusal) if isinstance(refusal, UnavailableRoute) else None

    def park(self, stem, drought):
        history = self.journal.read()
        previous = next((e.body for e in reversed(history)
                         if e.type == EventType.STATE_TRANSITION and e.ticket == stem), {})
        if previous.get('provider_drought') != drought.record:
            self.journal.append(EventType.STATE_TRANSITION,
                {'to': 'infra_error', 'reason': 'provider_drought', 'provider_drought': drought.record},
                ticket=stem, key=None)


class CliAdapter:
    """Shared base for one agent CLI. Subclasses supply `binary`, `argv`, `parse`."""

    binary: str
    AUTH_MARKERS: tuple[str, ...]
    LOGIN_ROAD: str

    def __init__(
        self,
        provider: Provider | ConfigSnapshot,
        *,
        config: Config | ConfigSnapshot,
        exec_: GroupExec,
        fs: FileSystem,
        redactor: Redactor,
        env: Mapping[str, str],
        cwd: Path,
        capture_dir: Path,
        timeout: float,
    ) -> None:
        self.provider = provider
        self._config = config
        self._exec = exec_
        self._fs = fs
        self._redactor = redactor
        self._env = env
        self._cwd = cwd
        self._capture_dir = capture_dir
        self._timeout = timeout
        self._seq = 0
        self._pgid: int | None = None

    def argv(self, model: str, effort: AgentEffort, writes: bool) -> list[str]:
        raise NotImplementedError

    def parse(self, out: str) -> str:
        """The result text; raise `ValueError` naming the failure when the stream carries no successful result."""
        raise NotImplementedError

    async def invoke(self, req: LLMRequest, model: str, *, consumer: "EventConsumer | None" = None) -> LLMResult:
        argv = self.argv(model, req.effort, req.surface in WRITING_SURFACES)
        writes = req.surface in WRITING_SURFACES
        if writes and req.worktree is None:
            raise ProviderSetupError(f"surface {req.surface!r} writes the tree but the request carries no worktree")
        self._seq += 1
        call_dir = self._capture_dir / (req.ticket or req.surface) / f"{self.provider.name}-{self._seq:04d}"
        prompt = call_dir / "prompt.md"
        # The prompt reaches the CLI only as a file on stdin: an argv element is capped by the OS limit.
        self._fs.write(prompt, self._redactor.scrub(req.rendered).encode())
        try:
            rc, out, err = await self._exec.run(
                argv,
                cwd=req.worktree if writes else self._cwd,
                env=child_env(self._env, self._config, self.provider),
                timeout=self._timeout,
                stdin_path=prompt,
                on_spawn=self._spawned,
                **({"on_stdout_line": lambda line: self._forward(line, consumer)} if consumer is not None else {}),
            )
        except BaseException as exc:
            capture = getattr(exc, "process_capture", None)
            if capture is not None:
                self._write_capture(call_dir, *capture)
            raise
        finally:
            self._pgid = None
        out, err = self._write_capture(call_dir, out, err)
        tail = err[-2000:]
        if rc != 0:
            # A CLI may report its failure in the event stream, not stderr: carry that message forward.
            try:
                self.parse(out)
                message = "nonzero exit"
            except ValueError as e:
                message = f"nonzero exit: {e}"
            self._raise_call_error(message, rc=rc, stderr_tail=tail)
        try:
            text = self.parse(out)
        except ValueError as e:
            self._raise_call_error(str(e), rc=rc, stderr_tail=tail)
        return LLMResult(
            # Scrubbed BEFORE it becomes the result: the one version that ever exists. Again after
            # decoding, since a JSON-escaped secret survives the raw-stream scrub.
            text=self._redactor.scrub(text),
            provider=self.provider.name,
            model=model,
        )

    def _write_capture(self, call_dir: Path, out: str, err: str) -> tuple[str, str]:
        out, err = self._redactor.scrub(out), self._redactor.scrub(err)
        self._fs.write(call_dir / "events.jsonl", out.encode())
        self._fs.write(call_dir / "stderr.txt", err.encode())
        return out, err

    def _spawned(self, pgid: int) -> None:
        self._pgid = pgid

    def _forward(self, line: str, consumer: "EventConsumer") -> None:
        for event in _events(line):
            # Scrub decoded strings too: JSON escaping can hide a configured secret in the raw line.
            consumer.consume(self._scrub_event(event))

    def _scrub_event(self, value):
        if isinstance(value, str):
            return self._redactor.scrub(value)
        if isinstance(value, dict):
            return {self._redactor.scrub(k): self._scrub_event(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._scrub_event(v) for v in value]
        return value

    def classify_failure(self, message: str, *, rc: int, stderr_tail: str) -> ProviderCallError:
        """Classify only failed diagnostics, never successful output."""
        message = self._redactor.scrub(message)
        tail = stderr_tail[-2000:]
        diagnostic = f"{tail}\n{message}".lower()
        markers = (("auth_error", self.AUTH_MARKERS), *FAILURE_MARKERS)
        failure = next((kind for kind, words in markers if any(w in diagnostic for w in words)), "unclassified")
        road = None
        if failure == "auth_error":
            road = self.LOGIN_ROAD
        elif failure == "model_error":
            road = f"fix provider {self.provider.name!r}'s model route in config.yaml, then retry"
        elif failure != "unclassified":
            road = f"wait for provider {self.provider.name!r} to recover from {failure}, then retry"
        return ProviderCallError(self.provider.name, message, rc=rc, stderr_tail=tail,
                                 failure_class=failure, paved_road=road)

    def parsed_failure(self, out: str, *, rc: int, stderr_tail: str) -> ProviderCallError | None:
        """Direct construction seam; invoke retains its existing production error handling."""
        try:
            self.parse(out)
        except ValueError as exc:
            return self.classify_failure(str(exc), rc=rc, stderr_tail=stderr_tail)
        if rc != 0:
            return self.classify_failure("nonzero exit", rc=rc, stderr_tail=stderr_tail)
        return None

    def _raise_call_error(self, message: str, *, rc: int, stderr_tail: str) -> None:
        raise self.classify_failure(message, rc=rc, stderr_tail=stderr_tail)

    def abort(self) -> None:
        if self._pgid is not None:
            self._exec.kill_group(self._pgid)


def _events(out: str) -> list[dict]:
    # Non-JSON lines (a CLI warning on stdout) stay in the capture; success still requires the terminal event.
    events = []
    for line in out.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


class ClaudeAdapter(CliAdapter):
    binary = "claude"
    AUTH_MARKERS = ("invalid api key", "please run /login", "not logged in", "oauth token has expired")
    LOGIN_ROAD = "run claude interactively and complete /login, then uv run python -m tempo drain"

    def argv(self, model: str, effort: AgentEffort, writes: bool) -> list[str]:
        # No effort flag: effort is recorded in provenance only. stream-json under -p requires --verbose.
        grant = ["--permission-mode", "bypassPermissions"] if writes else ["--allowedTools", "Read", "Grep", "Glob", "LS"]
        return [self.binary, "-p", "--output-format", "stream-json", "--verbose", "--model", model, *grant]

    def parse(self, out: str) -> str:
        result = next((e for e in reversed(_events(out)) if e.get("type") == "result"), None)
        if result is None:
            raise ValueError("no terminal `result` event in the stream")
        if result.get("is_error") or result.get("subtype") != "success":
            raise ValueError(f"result event reports failure: {result.get('subtype')}: {result.get('result')!r}")
        return str(result.get("result", ""))


class CodexAdapter(CliAdapter):
    binary = "codex"
    AUTH_MARKERS = ("not logged in", "401 unauthorized", "token has expired")
    LOGIN_ROAD = "run codex login, then uv run python -m tempo drain"

    def argv(self, model: str, effort: AgentEffort, writes: bool) -> list[str]:
        grant = (
            ["--dangerously-bypass-approvals-and-sandbox"]
            if writes
            else ["--sandbox", "read-only", "-c", "approval_policy=never"]
        )
        # Trailing `-`: the prompt is read from stdin.
        return [self.binary, "exec", "--json", "-m", model, "-c", f"model_reasoning_effort={effort}", *grant, "-"]

    def parse(self, out: str) -> str:
        text = None
        for e in _events(out):
            kind = e.get("type")
            # codex exits 0 on a failed turn: these events, not the exit code, are the failure signal.
            if kind == "turn.failed":
                raise ValueError(f"turn.failed: {(e.get('error') or {}).get('message')!r}")
            if kind == "error":
                raise ValueError(f"error event: {e.get('message')!r}")
            item = e.get("item") or {}
            if kind == "item.completed" and item.get("type") == "agent_message":
                text = str(item.get("text", ""))
        if text is None:
            raise ValueError("no `agent_message` item in the stream")
        return text


ADAPTERS: dict[str, type[CliAdapter]] = {"claude": ClaudeAdapter, "codex": CodexAdapter}


PROBE_PROMPT = "Reply with the single word ok."
PREFLIGHT_TIMEOUT_S = 120.0


class ProviderLLM:
    """Snapshot-local adapters sharing one session's ordered provider admission."""

    kind: Literal["api", "cli"] = "cli"

    def __init__(
        self,
        config: Config | ConfigSnapshot,
        *,
        exec_: GroupExec,
        fs: FileSystem,
        env: Mapping[str, str],
        cwd: Path,
        timeout: float,
        session: ProviderSession,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('provider timeout must be positive and finite')
        self.session, self._timeout = session, timeout
        session.active = True
        session.thresh.configure(config)
        self._config = config
        self._exec, self._env, self._cwd = exec_, env, cwd
        self.preflight_notices: list[str] = []
        redactor = Redactor.from_config(config, env)
        self._adapters: dict[str, CliAdapter] = {}
        for p in config.providers:
            if p.name not in ADAPTERS:
                raise ProviderSetupError(
                    f"provider {p.name!r} has no shipped cli adapter; name a provider one of {sorted(ADAPTERS)}"
                )
            self._adapters[p.name] = ADAPTERS[p.name](
                p,
                config=config,
                exec_=exec_,
                fs=fs,
                redactor=redactor,
                env=env,
                cwd=cwd,
                capture_dir=config.state_dir / "spools" / "providers",
                timeout=timeout,
            )
        self._active: CliAdapter | None = None

    async def preflight(self) -> list[str]:
        """Section 6.11: every routed (provider, model) answering a probe; each cli compared with its latest release.

        Returns each refusal (a failing probe) with its paved road; a stale cli whose probes pass is only a
        notice, left on `preflight_notices` (a newer release can lag its platform build).
        """
        problems: list[str] = []
        notices = self.preflight_notices = []
        for adapter in self._adapters.values():
            if adapter.provider.name in self.session.exclusions():
                continue
            package = adapter.provider.package
            _, out, err = await self._exec.run([adapter.binary, "--version"], cwd=self._cwd,
                                               env=child_env(self._env, self._config),
                                               timeout=PREFLIGHT_TIMEOUT_S)
            _, latest, _ = await self._exec.run(["pnpm", "view", str(package), "version"], cwd=self._cwd,
                                                env=child_env(self._env, self._config), timeout=PREFLIGHT_TIMEOUT_S)
            installed = re.search(r"\d+\.\d+\.\d+", out + err)
            if not latest.strip() or installed is None or installed.group(0) != latest.strip():
                notices.append(f"{adapter.binary} {installed.group(0) if installed else '(unknown)'} is not the"
                                f" latest release {latest.strip() or '(unreadable)'} -- run: pnpm add -g {package}@latest")
        probed: set[tuple[str, str]] = set()
        for route in self._config.routing:
            for cand in route.candidates:
                if cand.provider in self.session.exclusions():
                    continue
                adapter = self._adapters[cand.provider]
                model = cand.model or getattr(adapter.provider.models_by_tier, route.tier)
                if (cand.provider, model) in probed:
                    continue
                probed.add((cand.provider, model))
                req = LLMRequest(surface="preflight", rendered=PROBE_PROMPT, tier=route.tier, effort="low",
                                 ticket=None, worktree=None)
                try:
                    await adapter.invoke(req, model)
                except Exception as e:
                    problems.append(f"{cand.provider} model {model!r} failed its probe: {e} -- run: pnpm add -g"
                                    f" {adapter.provider.package}@latest, or route a model this login serves in config.yaml")
        if not problems:
            self.session.preflight_passed()
        return problems

    async def admitted_call(self, req, *, ticket, call_key, effect, meter=None):
        from tempo.thresh import UnavailableRoute
        route = candidates(self._config, req.tier, req.surface)
        used, queued, cap = self.session.thresh.occupancy(route[0].provider.name)
        estimate = ((used + queued) // cap) * self._timeout
        async def operation(served):
            consumer = meter(served) if meter else None
            result = await self._invoke(req, served, consumer=consumer)
            if consumer is not None and hasattr(consumer, 'complete'):
                consumer.complete()
            return result
        while True:
            result = await self.session.thresh.run(route, ticket=ticket, call_key=call_key,
                projected_wait_seconds=estimate, effect=effect, operation=operation)
            if not isinstance(result, UnavailableRoute):
                return result.result
            if ticket is not None or result.open_until is None:
                raise ProviderDrought(req.tier, req.surface, result)
            self.session._holds[call_key] = self.session.clock()
            try:
                await self.session.wait_until(result.open_until)
            finally:
                started = self.session._holds.pop(call_key)
                self.session._held_seconds[call_key] = self.session._held_seconds.get(call_key, 0) + max(
                    0, (self.session.clock() - started).total_seconds())

    async def call(self, req: LLMRequest, *, consumer: "EventConsumer | None" = None) -> LLMResult:
        """The raw LLM execution seam; llm_call owns its durable admission boundary."""
        return await self._invoke(req, resolve(self._config, req.tier, req.surface), consumer=consumer)

    async def _invoke(self, req, served, *, consumer=None):
        self._active = self._adapters[served.provider.name]
        try:
            return await self._active.invoke(req, served.model,
                **({'consumer': consumer} if consumer is not None else {}))
        finally:
            self._active = None

    def abort_current(self) -> None:
        if self._active is not None:
            self._active.abort()
