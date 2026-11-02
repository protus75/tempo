"""The one LLM-stage driver (TEMPO_PLAN.md section 5 invariant 2, section 6).

render -> spool prompt -> LLM effect (raced against the stuck budget) -> spool output -> unwrap one
fence -> validate -> gates -> on hard failure re-prompt the SAME workspace with the findings,
bounded by the retry cap. Stages differ only in spec (render), artifact type, and gate list.

Every model call crosses the LLM effect (tempo/llmeffect.py); the driver never calls the seam directly.
"""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from tempo.artifacts import Cost, Finding, Outcome, StageResult
from tempo.config import Config, ConfigSnapshot, Severity
from tempo.effects import Effects
from tempo.enginelog import EngineLog
from tempo.gates import Gate, GateReport, merge_severity, run_gates
from tempo.journal import Journal, run_seq as journal_run_seq
from tempo.llm import LLM, AgentEffort, AgentTier, LLMAborted, LLMRequest, LLMResult
from tempo.llmeffect import llm_call
from tempo.providers import ProviderBudgetExceeded, ProviderCallError, ProviderDrought, ProviderLLM, WRITING_SURFACES
from tempo.redact import Redactor
from tempo.seams import Clock, FileSystem, LocalFileSystem, Sleep

_FENCE = re.compile(r"\A\s*(`{3,})[\w+-]*[ \t]*\n(.*)\n\1[ \t]*\s*\Z", re.DOTALL)


def unwrap_fence(text: str) -> str:
    """Strip exactly ONE surrounding markdown code fence (optionally language-tagged)."""
    m = _FENCE.match(text)
    return m.group(2) if m else text


@dataclass(frozen=True)
class LlmStage:
    surface: str
    emits: type[BaseModel]
    gates: Sequence[Gate]
    render: Callable[[Any, list[Finding]], str]  # (consumed artifact, re-prompt findings) -> prompt
    review: Callable[[BaseModel, int], Awaitable[GateReport]] | None = None
    terminal_findings: frozenset[str] = frozenset()


class Spool:
    """Per-attempt capture at `<root>/<stem>/<attempt>/`; every byte passes the redactor."""

    def __init__(self, root: Path, redactor: Redactor, fs: FileSystem) -> None:
        self.root = root
        self._redactor = redactor
        self._fs = fs

    def write(self, stem: str, attempt: int, name: str, text: str) -> None:
        self._fs.write(self.root / stem / str(attempt) / name, self._redactor.scrub(text).encode())


class _StuckBudget(Exception):
    pass


@dataclass(frozen=True)
class ProviderDroughtResult(StageResult):
    provider_drought: dict | None = None


@dataclass
class _Tally:
    calls: int = 0
    tokens: int | None = None
    usd: float = 0.0
    last: LLMResult | None = None
    findings: list[Finding] = field(default_factory=list)

    def add(self, r: LLMResult) -> None:
        self.last = r
        self.usd += r.usd
        used = [t for t in (r.input_tokens, r.output_tokens) if t is not None]
        if used:
            self.tokens = (self.tokens or 0) + sum(used)


@dataclass
class _Race:
    call: asyncio.Future
    timer: asyncio.Future
    cleanup: asyncio.Task | None = None
    timer_stopping: bool = False

    def cancel_timer(self) -> None:
        if not self.timer_stopping:
            self.timer_stopping = True
            self.timer.cancel()


@dataclass
class _Invocation:
    task: asyncio.Task
    race: _Race | None = None
    abort: asyncio.Task | None = None
    detector: Any = None


async def _observe(task: asyncio.Future) -> Any:
    # A waiter's repeated cancellation cannot abandon the owned cleanup.
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except BaseException:
            break
    result = task.result()
    if cancelled is not None:
        raise cancelled
    return result


class Driver:
    def __init__(
        self,
        *,
        llm: LLM,
        journal: Journal,
        redactor: Redactor,
        spool: Spool,
        log: EngineLog,
        clock: Clock,
        sleep: Sleep,
        severity: Mapping[str, Severity],
        retry_cap: int,
    ) -> None:
        self.llm = llm
        self.journal = journal
        self.effects = Effects(journal)
        self.redactor = redactor
        self.spool = spool
        self.log = log
        self.clock = clock
        self.sleep = sleep
        self.severity = severity
        self.retry_cap = retry_cap
        self._active: _Invocation | None = None
        self.detector = None
        self._watch_identity = None
        self._notify_config = None

    @classmethod
    def from_config(
        cls,
        config: Config | ConfigSnapshot,
        *,
        llm: LLM,
        env: Mapping[str, str],
        clock: Clock,
        sleep: Sleep,
        fs: FileSystem | None = None,
    ) -> "Driver":
        """The production wiring: the redactor is built from config BEFORE any writer exists."""
        redactor = Redactor.from_config(config, env)
        driver = cls(
            llm=llm,
            journal=Journal(config.state_dir, clock),
            redactor=redactor,
            spool=Spool(config.state_dir / "spools", redactor, fs or LocalFileSystem()),
            log=EngineLog(config.state_dir / "engine.log", redactor, clock),
            clock=clock,
            sleep=sleep,
            severity=merge_severity(config),
            retry_cap=config.caps.retry,
        )
        driver._notify_config = (config, env)
        return driver

    async def run(
        self,
        stage: LlmStage,
        consumed: Any,
        *,
        ticket: str | None,
        attempt: int,
        workspace: Path,
        tier: AgentTier,
        effort: AgentEffort,
        stuck_budget: float,
        expected_budget: float | Literal['surface', 'eval'],
        scope_fence: Sequence[str],
        run_seq: int | None = None,
        budget_usd: float | None = None,
    ) -> StageResult:
        if self._active is not None:
            raise ValueError("await the active Driver.run or abort_current before another stage")
        # Own the stage itself, never the pipeline/worker task that called run.
        task = asyncio.create_task(self._run(
            stage, consumed, ticket=ticket, attempt=attempt, workspace=workspace,
            tier=tier, effort=effort, stuck_budget=stuck_budget, run_seq=run_seq,
            expected_budget=expected_budget, scope_fence=scope_fence,
            budget_usd=budget_usd,
        ))
        invocation = _Invocation(task)
        self._active = invocation
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if invocation.abort is not None:
                try:
                    await _observe(invocation.abort)
                except Exception:
                    if task.cancelled():
                        raise exc
                    raise
            elif self._active is invocation:
                await _observe(self._abort(invocation))
            raise
        finally:
            try:
                if invocation.detector is not None:
                    await _observe(asyncio.create_task(invocation.detector.close_notifications()))
            finally:
                if self._active is invocation and (
                    invocation.abort is None or invocation.abort.done()
                ):
                    self._active = None

    def materialize_exit_window(self, declarations: Mapping[str, Sequence], workspace: Path) -> Path | None:
        """Freeze resolved declarations in the consuming worktree, through the Driver's writer seam."""
        from dataclasses import asdict
        if not declarations:
            return None
        path = workspace / ".tempo" / "exit-read-window.json"
        payload = {declaration: [asdict(event) for event in events]
                   for declaration, events in declarations.items()}
        self.spool._fs.write(path, (json.dumps(payload, indent=2) + "\n").encode())
        return path

    async def _run(
        self, stage: LlmStage, consumed: Any, *, ticket: str | None, attempt: int,
        workspace: Path, tier: AgentTier, effort: AgentEffort, stuck_budget: float,
        run_seq: int | None,
        expected_budget: float | Literal['surface', 'eval'], scope_fence: Sequence[str],
        budget_usd: float | None,
    ) -> StageResult:
        """Run one stage attempt; `attempt` is the run sequence (section 6), stuck_budget in seconds."""
        stem = ticket or stage.surface  # ticketless surfaces spool and key under their surface name
        if run_seq is not None and ticket is not None:
            raise ValueError("run_seq requires ticket=None")
        seq = run_seq if run_seq is not None else journal_run_seq(self.journal.read(), stem)
        spool_stem = f"{stem}/{run_seq}" if run_seq is not None else stem
        self.begin_watch(owner=stem, ticket=ticket, run_sequence=seq, surface=stage.surface,
                         workspace=workspace, expected_budget=expected_budget,
                         stuck_budget=stuck_budget, scope_fence=scope_fence, attempt=attempt)
        started = self.clock()
        prior_wait = self.detector.cap_wait() if self.detector else 0
        deadline = started.timestamp() + stuck_budget
        ctx = {"stem": stem, "attempt": attempt, "surface": stage.surface}
        tally = _Tally()
        self.log.event("stage_start", **ctx, stuck_budget=stuck_budget)

        def done(outcome: Outcome, artifact: BaseModel | None = None) -> StageResult:
            seconds = max(0, (self.clock() - started).total_seconds()
                          - ((self.detector.cap_wait() if self.detector else 0) - prior_wait))
            self.log.event("stage_end", **ctx, outcome=outcome, calls=tally.calls)
            last = tally.last
            detector = self.detector
            meter = detector.call if detector is not None else None
            provider, model = (last.provider, last.model) if last else (None, None)
            if meter is not None and (last is None or outcome in {'infra_error', 'timeout'}):
                provider, model = meter.identity
            cost = Cost(
                tokens=tally.tokens,
                seconds=seconds,
                attempts=tally.calls,
                usd=tally.usd,
                provider=provider,
                model=model,
            )
            return StageResult(outcome=outcome, artifact=artifact, findings=tally.findings, cost=cost)

        async def failed(exc, call, name, *, pre_call=False):
            self.spool.write(spool_stem, attempt, f"{name}/error.txt", f"{type(exc).__name__}: {exc}")
            self.log.event('llm_error', **call, error=f'{type(exc).__name__}: {exc}')
            if isinstance(exc, ProviderDrought):
                tally.calls -= int(pre_call)
                tally.findings = []
                result = done('infra_error')
                return ProviderDroughtResult(**vars(result), provider_drought=exc.record)
            tally.findings = await self.provider_failure(exc, owner=stem, ticket=ticket, sequence=seq,
                                                        surface=stage.surface, workspace=workspace)
            return done('infra_error')

        outcome: Outcome = "ok"
        for call_seq in range(1, self.retry_cap + 2):  # the first call plus retry_cap re-prompts
            remaining = None if budget_usd is None else max(0.0, budget_usd - tally.usd)
            if remaining == 0:
                return done("budget_exceeded")
            call = {**ctx, "call_seq": call_seq}
            name = f"{stage.surface}/call-{call_seq:02d}"
            prompt = stage.render(consumed, tally.findings)
            req = LLMRequest(
                surface=stage.surface,
                rendered=prompt,
                tier=tier,
                effort=effort,
                ticket=ticket,
                worktree=workspace if stage.surface in WRITING_SURFACES else None,
                budget_usd=remaining,
            )
            # Before the call: a call that raises or hangs must leave exactly what was sent on disk.
            self.spool.write(spool_stem, attempt, f"{name}/prompt.md", prompt)
            self.log.event("llm_call", **call)
            tally.calls += 1
            try:
                recorded = await self.race(
                    lambda: llm_call(
                        self.effects, self.watched_llm(), req, self.redactor, ticket=ticket,
                        stem=stem, run_seq=seq, attempt=attempt, call_seq=call_seq,
                    ),
                    deadline - self.clock().timestamp() + (self.detector.cap_wait() if self.detector else 0),
                )
            except _StuckBudget:
                self.log.event("stuck_budget_kill", **call)
                tally.findings = []
                return done("timeout")
            except ProviderBudgetExceeded:
                tally.calls -= 1
                return done("budget_exceeded")
            except Exception as e:
                return await failed(e, call, name, pre_call=True)
            result = LLMResult(**recorded)
            tally.add(result)
            self.spool.write(spool_stem, attempt, f"{name}/output.txt", result.text)

            try:
                artifact = stage.emits.model_validate_json(unwrap_fence(result.text))
            except ValidationError as e:
                outcome = "invalid_artifact"
                tally.findings = [_invalid(stage, e)]
                self.log.event("artifact_invalid", **call, errors=e.error_count())
                continue

            gated = run_gates(stage.gates, artifact, workspace, severity=self.severity)
            hard_findings = [f for report in gated.hard_failures for f in report.findings]
            if stage.review is not None:
                try:
                    review = await self.race(lambda: stage.review(artifact, call_seq),
                        deadline - self.clock().timestamp() + (self.detector.cap_wait() if self.detector else 0))
                except _StuckBudget:
                    self.log.event("stuck_budget_kill", **call)
                    tally.findings = []
                    return done("timeout")
                except Exception as e:
                    return await failed(e, call, name)
                if review.verdict == "fail":
                    hard_findings.extend(review.findings)
            if hard_findings:
                outcome = "gate_failed"
                tally.findings = hard_findings
                self.log.event("gate_failed", **call, codes=[f.code for f in hard_findings])
                if any(f.code in stage.terminal_findings for f in hard_findings):
                    return done(outcome)
                continue
            if gated.passed:
                tally.findings = gated.findings  # soft only
                return done("ok", artifact)
        return done(outcome)

    async def provider_failure(self, exc, *, owner, ticket, sequence, surface, workspace):
        """The sole exception-to-Finding mapping, including standalone requisition review."""
        if not isinstance(exc, ProviderCallError):
            return []
        if self._notify_config is not None:
            config, env = self._notify_config
            if exc.failure_class == 'quota_exhausted':
                from tempo.daemon import storm_producer
                from tempo.box import BOX_DIR
                from tempo.storm import arrival_id
                storm_producer(root=config.state_dir / BOX_DIR, fs=self.spool._fs,
                    journal=self.journal, clock=self.clock).enqueue(message_class='failure_report',
                        origin=owner, stage=surface, outcome='infra_error', summary=f'{exc}; {exc.paved_road}',
                        occurrence_id=arrival_id('provider-quota', owner, sequence, surface, 'infra_error'))
            elif exc.failure_class == 'auth_error':
                from tempo.__main__ import watchdog_notifications
                from tempo.notify import send
                from tempo.seams import NotificationFailed
                notice = watchdog_notifications(config, env, self.effects, self.log,
                    redactor=self.redactor, cwd=workspace, owner=owner, ticket=ticket,
                    run_sequence=sequence, identity=exc.provider)
                if config.notify:
                    try:
                        await send(self.effects, notice.notifications, config.notify, owner=owner,
                            escalation='auth_error', identity=exc.provider, ticket=ticket,
                            message=f'{exc}; {exc.paved_road}')
                    except NotificationFailed as error:
                        self.log.event('notify_failed', error=str(error))
                else:
                    self.log.event('auth_alert', message=f'{exc}; {exc.paved_road}')
        return ([Finding(code=exc.failure_class, message=str(exc), paved_road=exc.paved_road)]
                if exc.failure_class and exc.paved_road else [])

    async def abort_current(self) -> None:
        """Stop and observe only the currently owned stage; dormant until explicitly invoked."""
        invocation = self._active
        if invocation is not None:
            await _observe(self._abort(invocation))

    def begin_watch(self, *, owner, ticket, run_sequence, surface, workspace,
                    expected_budget, stuck_budget, scope_fence, attempt):
        from tempo.__main__ import watchdog_observation, watchdog_notifications
        from tempo.watchdog import Detector

        if expected_budget == 'surface':
            if ticket is not None:
                raise ValueError('ticket-owned calls need the ticket expected budget')
            expected = stuck_budget / 2
        elif expected_budget == 'eval':
            expected = stuck_budget / 2
        elif isinstance(expected_budget, (int, float)) and expected_budget > 0:
            expected = expected_budget
        else:
            raise ValueError('missing ticket expected budget; supply its Time budget')
        identity = (owner, run_sequence, surface, attempt)
        if self.detector is not None and identity == self._watch_identity:
            if self._active is not None:
                self._active.detector = self.detector
            return
        self.detector = None
        self._watch_identity = identity
        if isinstance(self.llm, ProviderLLM):
            config, env = self._notify_config
            def cap_wait():
                return self.provider_wait(owner, run_sequence)
            prior_wait = cap_wait()
            self.detector = Detector(expected_minutes=expected / 60, stuck_minutes=stuck_budget / 60,
                clock=self.clock, sleep=self.sleep, observe=watchdog_observation(workspace, scope_fence),
                cap_wait=lambda: cap_wait() - prior_wait, log=self.log,
                notify=watchdog_notifications(config, env, self.effects, self.log,
                    redactor=self.redactor, cwd=self.llm._cwd,
                    owner=owner, ticket=ticket, run_sequence=run_sequence, identity=surface))
            if self._active is not None:
                self._active.detector = self.detector

    def provider_wait(self, owner, sequence):
        if not isinstance(self.llm, ProviderLLM):
            return 0
        prefix = f'llm/{owner}/{sequence}/'
        return sum(e.body['waited_seconds'] for e in self.journal.read()
            if e.body.get('signal') == 'provider_cap_wait'
            and e.body.get('call_key', '').startswith(prefix)) + self.llm.session.live_wait(prefix)

    def watched_llm(self):
        from tempo.watchdog import WatchedLLM
        return WatchedLLM(self.llm, self.detector) if self.detector is not None else self.llm

    def _abort(self, invocation: _Invocation) -> asyncio.Task:
        if invocation.abort is None:
            async def abort() -> None:
                try:
                    cleanup = None
                    if not invocation.task.done():
                        if invocation.race is not None:
                            cleanup = self._kill(invocation.race)
                        else:
                            self.llm.abort_current()
                            cleanup = None
                        invocation.task.cancel()
                    # Even a cleanup failure must not skip observation of the stage.
                    owned = [invocation.task] if cleanup is None else [cleanup, invocation.task]
                    results = await asyncio.gather(*owned, return_exceptions=True)
                    for result in results:
                        if isinstance(result, BaseException) and not isinstance(
                            result, (asyncio.CancelledError, LLMAborted)
                        ):
                            raise result
                finally:
                    if invocation.task.done() and self._active is invocation:
                        self._active = None
            invocation.abort = asyncio.create_task(abort())
        return invocation.abort

    async def race(self, start: Callable[[], Awaitable[Any]], remaining: float) -> Any:
        if remaining <= 0:
            raise _StuckBudget
        from tempo.watchdog import watch_deadline
        timer = self.sleep(remaining) if self.detector is None else watch_deadline(self.detector, remaining)
        race = _Race(asyncio.ensure_future(start()), asyncio.ensure_future(timer))
        detector = self.detector
        invocation = self._active
        if invocation is not None and invocation.task is asyncio.current_task():
            invocation.race = race
        try:
            try:
                await asyncio.wait({race.call, race.timer}, return_when=asyncio.FIRST_COMPLETED)
                if race.call.done():
                    race.cancel_timer()
                    await _observe(asyncio.gather(race.timer, return_exceptions=True))
            except BaseException as exc:
                try:
                    await _observe(self._kill(race))
                except Exception:
                    if (isinstance(exc, asyncio.CancelledError) and invocation is not None
                            and invocation.abort is not None):
                        # The abort owner reports cleanup failure; the killed stage stays cancelled.
                        raise exc
                    raise
                raise
            if race.call.done() and (detector is None or detector.decide() != 'stuck'):
                return race.call.result()
            await _observe(self._kill(race))
            raise _StuckBudget
        finally:
            if invocation is not None and invocation.race is race:
                invocation.race = None
            if invocation is None and detector is not None:
                await _observe(asyncio.create_task(detector.close_notifications()))

    def _kill(self, race: _Race) -> asyncio.Task:
        if race.cleanup is None:
            # Stop the external writer before any cancellation, including stage cancellation.
            self.llm.abort_current()
            race.cancel_timer()
            race.call.cancel()

            async def cleanup() -> None:
                results = await asyncio.gather(race.call, race.timer, return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException) and not isinstance(
                        result, (asyncio.CancelledError, LLMAborted)
                    ):
                        raise result

            race.cleanup = asyncio.create_task(cleanup())
        return race.cleanup


def _invalid(stage: LlmStage, e: ValidationError) -> Finding:
    detail = "; ".join(f"{'.'.join(map(str, err['loc'])) or '<root>'}: {err['msg']}" for err in e.errors())
    return Finding(
        code="invalid_artifact",
        message=f"output is not a valid {stage.emits.__name__}: {detail}",
        paved_road=f"reply with ONLY one JSON object matching the {stage.emits.__name__} schema, no prose",
    )
