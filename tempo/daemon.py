"""Production daemon core and dispatch ownership."""

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Set
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from tempo.config import Config, ConfigSnapshot, snapshot_config
from tempo.box import Box, RereportJournal
from tempo.checkpoint import Checkpoint
from tempo.effects import Effects
from tempo.enginelog import EngineLog
from tempo.git import Git
from tempo.timers import Timers
from tempo.flake import Flake
from tempo.control import CONTROL_DECISION, ControlInbox, ControlProjection, write_active
from tempo.journal import EventType, Journal
from tempo import runner
from tempo.artifacts import Finding, StageResult
from tempo.caps import spent
from tempo.heartbeat import Heartbeat
from tempo.mergequeue import ConflictHandoff, MergeQueue
from tempo.merge import AdmissionBoundary
from tempo.rework import ReworkOrder, record_supersedes, rework
from tempo.runner import Dispatch
from tempo.restart import Restart
from tempo.scheduler import Scheduler
from tempo.seams import Clock, FileSystem, Sleep
from tempo.stages import StageContext, StageName, Stages, StagesRun
from tempo.storm import StormBreaker
from tempo.tickets import Ticket, parse_ticket, ticket_path
from tempo.watcher import Watcher

KILL_APPLIED = "kill_applied"


def checkpoint_push(repo: Path, *, journal: Journal, effects: Effects, timers: Timers,
                    git: Git, box: Box, clock: Clock, log: EngineLog) -> Checkpoint:
    """Idle boundary using the lock holder's existing writers; invoke poll explicitly."""
    return Checkpoint(repo, journal=journal, effects=effects, timers=timers,
                      git=git, box=box, clock=clock, log=log)


def storm_producer(*, root: Path, fs: FileSystem, journal: Journal, clock: Clock) -> Box:
    """Compose without effects; recovery and arrivals run under the caller's writer lock."""
    storm = StormBreaker(journal=journal, clock=clock,
                         publish=lambda body, count: box.publish_storm_report(body, count))
    box = Box(root, fs, arrival=storm.record, recover=storm.recover,
              rereport=RereportJournal(journal))
    return box


def flake_detection(*, journal: Journal, box: Box, config: Config,
                    escalate: Callable[[str], None]) -> Flake:
    """Flake detection/release boundary supplied with the lock holder's existing writers."""
    return Flake(journal=journal, box=box, cap=config.caps.quarantine, escalate=escalate)


class HeartbeatCycle:
    """Explicit main-loop boundary; task owners supply responsiveness."""

    def __init__(self, *, heartbeat: Heartbeat, workers: Callable[[], bool],
                 merge_queue: Callable[[], bool], box_consumer: Callable[[], bool],
                 watcher: Callable[[], bool]) -> None:
        self.heartbeat = heartbeat
        self._health = (workers, merge_queue, box_consumer, watcher)

    def cycle(self) -> bool:
        # Evaluate every component even when an earlier one reports unhealthy.
        health = tuple(probe() for probe in self._health)
        if not all(health):
            return False
        self.heartbeat.refresh()
        return True


def control_inbox(*, journal: Journal, lifecycle_id: str, holds: Callable[[], Set[str]],
                  apply: Callable[[ControlProjection], None],
                  files: Callable[[], Iterable[Path]], read: Callable[[Path], bytes]) -> ControlInbox:
    """Explicitly supplied by the lock holder, never a second writer."""
    return ControlInbox(journal=journal, lifecycle_id=lifecycle_id, holds=holds,
                        apply=apply, files=files, read=read)


async def executor_abort(abort_current: Callable[[], Awaitable[None]]) -> None:
    """Unwind the composed executor after durable kill acceptance."""
    await abort_current()


async def _protected_cleanup(cleanup: asyncio.Future) -> None:
    cancelled = None
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except BaseException:
            break
    cleanup.result()
    if cancelled is not None:
        raise cancelled


def _stop_workers(tasks: tuple[asyncio.Task, ...]) -> asyncio.Future:
    for task in tasks:
        if not task.done() and not task.cancelling():
            task.cancel()
    return asyncio.gather(*tasks, return_exceptions=True)


class WorkerStop:
    """Stop owner for one supplied task set.

    Construct a fresh boundary for a later task set. The lock holder supplies the
    inbox's recovered projection and the predecessor executor-abort operation.
    """

    def __init__(self, *, lifecycle_id: str, abort: Callable[[], Awaitable[None]],
                 workers: Iterable[asyncio.Task]) -> None:
        self.lifecycle_id, self.abort = lifecycle_id, abort
        self.workers = tuple(workers)
        self._stop: asyncio.Task | None = None

    async def stop(self, projection: ControlProjection) -> None:
        if projection.lifecycle_id != self.lifecycle_id or not projection.kill_requested:
            return
        if self._stop is None:
            async def stop() -> None:
                await self.abort()
                await _protected_cleanup(_stop_workers(self.workers))
            self._stop = asyncio.create_task(stop())
        await _protected_cleanup(self._stop)


class WorkerFailureObserver:
    """Notification boundary for WorkerStop's supplied workers.

    The lock holder supplies its current, durably folded projection. Observation
    never stops workers or replaces their owner's exception propagation.
    """

    def __init__(self, *, lifecycle_id: str, workers: Iterable[asyncio.Task],
                 projection: Callable[[], ControlProjection],
                 failure: Callable[[BaseException], None]) -> None:
        self.lifecycle_id, self.workers = lifecycle_id, tuple(workers)
        self.projection, self.failure = projection, failure
        self._observation: asyncio.Task | None = None

    async def observe(self) -> None:
        if self._observation is None:
            async def worker(task: asyncio.Task) -> None:
                try:
                    await task
                except BaseException as exc:
                    projection = self.projection()
                    if (projection.lifecycle_id != self.lifecycle_id
                            or not projection.kill_requested):
                        self.failure(exc)

            async def observe() -> None:
                # A notification failure must not abandon another worker's cleanup.
                results = await asyncio.gather(*(worker(task) for task in self.workers),
                                               return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        raise result

            self._observation = asyncio.create_task(observe())
        await _protected_cleanup(self._observation)


class PauseConsumer:
    """One serial inbox and desired pause state, owned by the engine's writer lock."""

    def __init__(self, *, journal: Journal, lifecycle_id: str, state_dir: Path,
                 fs: FileSystem, sleep: Sleep, files: Callable[[], Iterable[Path]],
                 read: Callable[[Path], bytes], recover: Callable[[], None] | None = None,
                 storm_holds: Callable[[], dict[str, str]] | None = None) -> None:
        self._recover = recover
        self._storm_holds = storm_holds
        self.state_dir, self.fs, self.sleep = state_dir, fs, sleep
        self.projection = ControlProjection(lifecycle_id)
        self._published = False
        self._advertised: tuple[str, str | None] | None = None
        self._stop: asyncio.Task | None = None
        self.admission: str | None = None
        self.author_driver = None
        self.inbox = control_inbox(journal=journal, lifecycle_id=lifecycle_id,
                                   holds=self._holds,
                                   apply=self._apply, files=files, read=read)

    def storm_holds(self) -> dict[str, str]:
        return {} if self._storm_holds is None else self._storm_holds()

    def _holds(self) -> set[str]:
        return set(self.storm_holds()) | ({self.admission} if self.admission is not None else set())

    def _apply(self, projection: ControlProjection) -> None:
        # The inbox has already fsynced the decision; discovery is only its projection.
        if self._published:
            self._refresh(projection)
        self.projection = projection

    def _selected_hold(self, projection: ControlProjection) -> str | None:
        if projection.pause_id is not None:
            return projection.pause_id
        if self.admission is not None and self.admission not in projection.released_hold_ids:
            return self.admission
        return next((identity for identity in self.storm_holds()
                     if identity not in projection.released_hold_ids), None)

    def _refresh(self, projection: ControlProjection) -> None:
        hold = self._selected_hold(projection)
        advertised = projection.lifecycle_id, hold
        if advertised != self._advertised:
            write_active(self.state_dir, projection, self.fs, hold_id=hold)
            self._advertised = advertised

    def hold(self, hold_id: str) -> None:
        self.admission = hold_id
        if self._published:
            self._refresh(self.projection)

    def publish(self) -> None:
        self._refresh(self.projection)
        self._published = True

    def retire(self) -> None:
        write_active(self.state_dir, None, self.fs, hold_id=None)
        self._published = False
        self._advertised = None

    async def checkpoint(self) -> None:
        while True:
            self.poll()
            if self.projection.kill_requested or self.projection.pause_id is None:
                return
            await self.sleep(0.1)

    def poll(self) -> None:
        """Consume control without occupying a paused offer's callback."""
        if self._recover is not None:
            self._recover()
        if self._published:
            self._refresh(self.projection)
        self.inbox.consume()

    async def apply_kill(self, abort: Callable[[], Awaitable[None]],
                         dispatch: asyncio.Task | None = None) -> None:
        if (self.projection.lifecycle_id != self.inbox.lifecycle_id
                or not self.projection.kill_requested):
            return
        if self._stop is None:
            async def stop() -> None:
                await executor_abort(abort)
                if dispatch is not None:
                    if not dispatch.done() and not dispatch.cancelling():
                        dispatch.cancel()
                    [result] = await asyncio.gather(dispatch, return_exceptions=True)
                    if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                        raise result
            self._stop = asyncio.create_task(stop())
        await _protected_cleanup(self._stop)
        # Requests arriving during unwind share that same stop. The journal fold,
        # rather than file deletion or memory, owns completion across reconstruction.
        self.inbox.consume()
        decided, accepted, applied = set(), [], set()
        for event in self.inbox.journal.read():
            body = event.body
            if event.type != EventType.SIGNAL:
                continue
            if (body.get("kind") == KILL_APPLIED
                    and body.get("lifecycle_id") == self.inbox.lifecycle_id):
                applied.add(body["request_id"])
            if body.get("kind") != CONTROL_DECISION or body["request_id"] in decided:
                continue
            decided.add(body["request_id"])
            if (body["decision"] == "accepted" and body["verb"] == "kill"
                    and body["lifecycle_id"] == self.inbox.lifecycle_id):
                accepted.append(body["request_id"])
        for request_id in accepted:
            if request_id not in applied:
                self.inbox.journal.append(EventType.SIGNAL, {
                    "kind": KILL_APPLIED, "request_id": request_id,
                    "lifecycle_id": self.inbox.lifecycle_id}, ticket=None, key=None)


@dataclass(frozen=True)
class DaemonCore:
    admission: "DaemonAdmission"
    scheduler: Scheduler
    watcher: Watcher
    control: PauseConsumer | None = None

    @property
    def restart(self) -> Restart | None:
        return self.admission.restart

    async def startup(self) -> None:
        """Explicit startup under the caller's writer lock, also used by first admission."""
        async with self.admission._slot:
            if self.restart is not None:
                await self.restart.startup()
            if self.control is not None and self.control._recover is not None:
                self.control._recover()

    async def sweep_orphans(self) -> list[str]:
        """Skip live dispatch, cleanup or admission; serialize the idle boundary with offers."""
        if self.admission._slot.locked():
            return []
        async with self.admission._slot:
            if self.restart is None:
                return []
            return await self.restart.sweep_orphans()


class StartupBoundary:
    """Admission awaits recovery before the existing pause checkpoint and snapshot."""

    def __init__(self, restart: Restart, pause: PauseConsumer) -> None:
        self.restart, self.pause = restart, pause

    async def checkpoint(self) -> None:
        await self.restart.startup()
        await self.pause.checkpoint()


class DaemonTasks:
    """Explicit lifetime owner for background consumers."""

    def __init__(self, *, watcher: Callable[[], Awaitable[object]],
                 merge_queue: Callable[[], Awaitable[object]],
                 box_consumer: Callable[[], Awaitable[object]]) -> None:
        self._consumers = (watcher, merge_queue, box_consumer)
        self.tasks: tuple[asyncio.Task, ...] = ()
        self.started: Callable[[tuple[asyncio.Task, ...]], None] = lambda _: None

    async def run(self) -> None:
        if self.tasks:
            raise ValueError("finish or cancel and await the existing DaemonTasks.run before another run")

        async def invoke(callback: Callable[[], Awaitable[object]]) -> None:
            await callback()

        self.tasks = tuple(asyncio.create_task(invoke(callback)) for callback in self._consumers)
        self.started(self.tasks)
        try:
            # Shield prevents owner cancellation from cancelling a consumer again during unwind.
            await asyncio.shield(asyncio.gather(*self.tasks))
        finally:
            try:
                await _protected_cleanup(_stop_workers(self.tasks))
            finally:
                self.tasks = ()


async def apply_rework(ctx: StageContext, ticket: Ticket, findings: list[Finding], *, attempt: int,
                       handoff: ConflictHandoff | None = None) -> StageResult:
    """Publish exact reviewed proposals on the ticket plane under the caller's writer lock."""
    road = "shrink or split the committed ticket text and rerun drain"
    workspace = ctx.worktree(ticket.stem)
    if not workspace.exists():
        return StageResult(outcome="gate_failed", artifact=None, findings=[Finding(
            code="ticket_schema", message="Rework has no usable workspace", paved_road=road)])
    try:
        text = await ctx.git._run(ctx.repo, "show", f"HEAD:{ticket_path(ticket.stem)}")
        original = parse_ticket(ticket.stem, text, ctx.repo)
        tier, effort = runner.capability(original, ctx.driver.journal.read())
        result = await rework(ctx, original, text, findings, attempt=attempt, workspace=workspace,
                              tier=tier, effort=effort, stuck_budget=original.stuck_minutes * 60,
                              handoff=handoff)
        if result.outcome != "ok":
            return result
        order = result.artifact
        assert isinstance(order, ReworkOrder)
        if order.action == "escalate":
            return result
        paths = [ticket_path(p.stem) for p in order.tickets]
        before = await ctx.git.rev_parse(ctx.repo, "HEAD")

        async def restore_publication() -> None:
            # Restore also removes added successors from the index and worktree. Stage any
            # files left by a write/add failure so they belong to that same restore lane.
            written = [path for path in paths if (ctx.repo / path).is_file()]
            if written:
                await ctx.git.add(ctx.repo, written)
            indexed = set(await ctx.git.ls_files(ctx.repo))
            restorable = [path for path in paths if path in indexed]
            if restorable:
                await ctx.git.restore(ctx.repo, restorable, source=before)
            # A commit can land before its caller fails or exact-byte verification refuses.
            # Compensate only its proposal paths, preserving unrelated staged work.
            committed = (await ctx.git._run(
                ctx.repo, "diff", "--name-only", before, "HEAD", "--", *paths)).splitlines()
            if committed:
                await ctx.git.commit(ctx.repo, f"tempo({ticket.stem}): undo refused rework", only=committed)

        async def publish() -> dict:
            for proposal, path in zip(order.tickets, paths, strict=True):
                ctx.fs.write(ctx.repo / path, proposal.ticket.encode())
            await ctx.git.add(ctx.repo, paths)
            await ctx.git.commit(ctx.repo, f"tempo({ticket.stem}): rework", only=paths)
            for proposal, path in zip(order.tickets, paths, strict=True):
                if await ctx.git._run(ctx.repo, "show", f"HEAD:{path}") != proposal.ticket:
                    raise ValueError(f"committed {path} differs from the reviewed proposal")
            return {"commit": await ctx.git.rev_parse(ctx.repo, "HEAD")}

        try:
            await ctx.driver.effects.run(publish, key=f"ticket-plane/{ticket.stem}/{attempt}/rework",
                                         ticket=ticket.stem)
            if errors := await record_supersedes(ctx, order):
                await restore_publication()
                return StageResult(outcome="gate_failed", artifact=None, findings=errors)
        except BaseException:
            cleanup = asyncio.create_task(restore_publication())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
            raise
        return result
    except Exception as exc:
        return StageResult(outcome="gate_failed", artifact=None, findings=[Finding(
            code="ticket_schema", message=ctx.driver.redactor.scrub(f"Rework publication refused: {exc}"),
            paved_road="commit every exact reviewed proposal before supersession; " + road)])


@dataclass(frozen=True)
class Continuation:
    ticket: Ticket
    ctx: StageContext
    run: StagesRun
    next_stage: StageName


class TicketWriter:
    """The composed ticket writer; queue handoffs are consumed only after admission unwinds."""

    def __init__(self, ctx: StageContext, queue: MergeQueue) -> None:
        self.ctx, self.queue = ctx, queue
        self.stages = Stages(ctx)
        self.admission = AdmissionBoundary(ctx)
        self.active = False
        self.continuations: dict[str, Continuation] = {}

    async def __call__(self, ticket: Ticket) -> str:
        boundary = self.ctx.boundary
        prior = self.continuations.get(ticket.stem)
        if prior is not None:
            ticket, boundary.run = prior.ticket, prior.run
        self.active = True
        try:
            if self.admission.mode == "inline":
                result = await runner.drive(self.ctx, ticket)
            else:
                result = await runner.drive(self.ctx, ticket, admission=self.admission,
                                            consume_handoff=self.consume_handoff)
            if boundary.next_stage is not None:
                assert boundary.run is not None and boundary.ticket is not None
                self.continuations[ticket.stem] = Continuation(
                    boundary.ticket, self.ctx, boundary.run, boundary.next_stage)
            else:
                self.continuations.pop(ticket.stem, None)
            return result
        finally:
            self.active = False
            boundary.run, boundary.ticket, boundary.next_stage = None, None, None

    async def consume_result(self, ticket: Ticket, result: StageResult | ConflictHandoff, *, attempt: int) -> None:
        """Deliver the producing run's result; predecessor offers retain their existing custody."""
        if self.admission.deliver(ticket, attempt, result):
            return
        if isinstance(result, ConflictHandoff):
            await self.consume_handoff(ticket, result, attempt=attempt)
        elif result.outcome != "ok":
            await runner.harvest_failure(self.ctx, ticket, attempt=attempt, stage="merge",
                outcome=result.outcome, findings=result.findings)
            await runner.failure_terminal(self.ctx, ticket, attempt=attempt, stage="merge",
                outcome=result.outcome, findings=result.findings)

    async def abort_current(self) -> None:
        await self.ctx.abort_current()

    async def consume_handoff(self, ticket: Ticket, handoff: ConflictHandoff, *, attempt: int) -> str:
        if self.queue.active is not None or self.queue._slot.locked():
            raise ValueError("finish and await MergeQueue.process before consuming its handoff")
        ctx = self.ctx
        outcome = "gate_failed"
        await runner.harvest_failure(ctx, ticket, attempt=attempt, stage="merge", outcome=outcome,
                                     findings=handoff.findings)
        reply = None
        if not spent(ctx.config.caps, ctx.driver.journal.read(), ticket.stem):
            reply = await apply_rework(ctx, ticket, handoff.findings, attempt=attempt, handoff=handoff)
        return await runner.failure_terminal(ctx, ticket, outcome=outcome, stage="merge",
                                             findings=handoff.findings, attempt=attempt, reworked=reply,
                                             rework_requested=True)


def daemon_core(
    repo: Path, *, journal: Journal, load: Callable[[], Config],
    bind: Callable[[ConfigSnapshot], Dispatch],
    read: Callable[[str], str | None], clock: Clock, sleep: Sleep, debounce: float,
    quarantined: Callable[[], Set[str]], drought_parked: Callable[[], Set[str]],
    completed_unmerged: Callable[[], int], max_unmerged: int,
    before_dispatch: Callable[[], Awaitable[None]] | None = None,
    control: PauseConsumer | None = None,
) -> DaemonCore:
    admission = DaemonAdmission(snapshot_dispatch(load, bind), before_dispatch=before_dispatch)
    scheduler = Scheduler(
        journal, admission.dispatch, quarantined=quarantined, drought_parked=drought_parked,
        completed_unmerged=completed_unmerged, max_unmerged=max_unmerged,
    )
    watcher = Watcher(
        repo, journal=journal, read=read, publish=scheduler.update, remove=scheduler.remove,
        clock=clock, sleep=sleep, debounce=debounce,
    )
    return DaemonCore(admission, scheduler, watcher, control)


def snapshot_dispatch(load: Callable[[], Config], bind: Callable[[ConfigSnapshot], Dispatch]) -> Dispatch:
    """Bind a fresh snapshot inside the admission-owned callback's lifetime, never while queued."""
    async def dispatch(ticket: Ticket) -> str:
        snapshot = snapshot_config(load())
        callback = bind(snapshot)
        return await callback(ticket)

    return dispatch


class DaemonAdmission:
    def __init__(self, dispatch: Dispatch, *,
                 before_dispatch: Callable[[], Awaitable[None]] | None = None) -> None:
        self._dispatch = dispatch
        self._before_dispatch = before_dispatch
        self.restart: Restart | None = None
        self._slot = asyncio.Lock()
        self.active: Ticket | None = None
        self.task: asyncio.Task[str] | None = None
        self.resume: Callable[[Ticket], Dispatch | None] = lambda _: None

    @asynccontextmanager
    async def waiting_for_merge(self):
        """Retain dispatch ownership while lending its ticket-plane boundary to the consumer."""
        self._slot.release()
        try:
            yield
        finally:
            await _protected_cleanup(asyncio.create_task(self._slot.acquire()))

    async def dispatch(self, ticket: Ticket) -> str:
        async with self._slot:
            if self._before_dispatch is not None:
                await self._before_dispatch()

            async def invoke() -> str:
                callback = self.resume(ticket) or self._dispatch
                return await callback(ticket)

            task = asyncio.create_task(invoke())
            self.active, self.task = ticket, task
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                task.cancel()
                # Repeated caller cancellation must not interrupt the callback's cleanup.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        break
                if not task.cancelled():
                    task.exception()
                raise
            finally:
                self.active, self.task = None, None
