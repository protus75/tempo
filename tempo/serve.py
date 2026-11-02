"""Continuous foreground lifetime."""

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from tempo import backlog, runner, triage
from tempo.checkout import Checkout, Refusal
from tempo.config import ConfigError, snapshot_config
from tempo.daemon import DaemonTasks, HeartbeatCycle, TicketWriter, WorkerFailureObserver
from tempo.control import protected_cleanup
from tempo.daemon import WorkerStop, checkpoint_push
from tempo.daemon import _stop_workers
from tempo.effects import Effects
from tempo.enginelog import EngineLog
from tempo.heartbeat import Heartbeat
from tempo.journal import EventType
from tempo.lockfile import Lockfile
from tempo.merge import AdmissionBoundary
from tempo.mergequeue import ConflictHandoff
from tempo.journal import JournalCorruption
from tempo.seams import ExecutableNotFound
from tempo.redact import Redactor
from tempo.storm import StormLedger
from tempo.tickets import Ticket, intake

SERVE_POLL_S = 0.1


class Responsiveness:
    """A round trip through an owned consumer, independent of task existence."""

    def __init__(self) -> None:
        self.request = asyncio.Event()
        self.sent = self.answered = 0

    def ask(self) -> None:
        self.sent += 1
        self.request.set()

    def reply(self) -> None:
        self.answered = self.sent
        self.request.clear()

    def responsive(self) -> bool:
        return self.sent > 0 and self.answered == self.sent

    async def wait(self, work: Awaitable):
        """The consumer stays responsive while owning and awaiting its operation."""
        task = asyncio.ensure_future(work)
        ping = None
        try:
            while not task.done():
                ping = asyncio.create_task(self.request.wait())
                await asyncio.wait((task, ping), return_when=asyncio.FIRST_COMPLETED)
                if ping.done():
                    self.reply()
                else:
                    ping.cancel()
                await asyncio.gather(ping, return_exceptions=True)
            return task.result()
        finally:
            if ping is not None:
                ping.cancel()
                await asyncio.gather(ping, return_exceptions=True)
            if not task.done():
                task.cancel()
            await protected_cleanup(asyncio.gather(task, return_exceptions=True))


class Serve:
    """One idle composition; only run acquires the lock and starts owned work."""

    def __init__(self, checkout: Checkout, *, config_path: Path | None = None,
                 read: Callable[[str], str | None],
                 stems: Callable[[], Iterable[str]],
                 prepare, llm, signals: Callable | None = None,
                 failure: Callable[[BaseException], None] | None = None) -> None:
        from tempo.__main__ import build_daemon_core, build_notifications

        self.read, self.stems, self.signals = read, stems, signals
        self.prepare, self.llm = prepare, llm
        self.config_path = config_path
        self.writers: dict[str, TicketWriter] = {}
        self.queue = None
        self.stopping = False
        self.wake = asyncio.Event()
        self.ready = asyncio.Event()
        self.workers: tuple[asyncio.Task, ...] = ()
        self.health_tasks: tuple[asyncio.Task, ...] = ()
        self.mutating: asyncio.Task | None = None
        self.background: asyncio.Task | None = None
        self.observation: asyncio.Task | None = None
        self.stopper = self.observer = self.checkpoint = None
        self.responses = tuple(Responsiveness() for _ in range(4))
        self.log = EngineLog(checkout.config.state_dir / "engine.log",
                             Redactor({}), checkout.clock)
        self.failure = failure or (lambda exc: self.log.event("worker_failure", error=str(exc)))
        self.notifications = build_notifications(checkout, config_path=config_path, log=self.log)

        async def prepared(local):
            callback = await prepare(local)
            if not isinstance(callback, TicketWriter):
                raise TypeError("serve preparation must return the composed TicketWriter")
            callback.ctx.boundary.select = self.select
            if self.queue is None:
                self.queue = callback.queue
            callback.queue = self.queue
            callback.admission = AdmissionBoundary(callback.ctx, mode="daemon", queue=self.queue,
                waiting=self.core.admission.waiting_for_merge)

            async def dispatch(ticket):
                if not self.select(ticket, "implement"):
                    self.core.scheduler.update(ticket)
                    return "running"
                self.writers[ticket.stem] = callback
                local.journal.append(EventType.STATE_TRANSITION, {"to": "running"},
                                     ticket=ticket.stem, key=None)
                try:
                    return await callback(ticket)
                finally:
                    if (not callback.continuations and not any(
                            context is callback.ctx and stem in callback.queue.pending
                            for stem, context in callback.queue.contexts.items())):
                        self.writers.pop(ticket.stem, None)
            return dispatch

        self.core = build_daemon_core(checkout, config_path=config_path, read=read,
            debounce=SERVE_POLL_S, quarantined=lambda: set(), drought_parked=lambda: set(),
            completed_unmerged=lambda: len(self.queue.pending) if self.queue is not None else 0,
            prepare=prepared)
        self.checkout = replace(checkout, control=self.core.control)
        self.control = self.core.control
        assert self.control is not None and self.core.restart is not None
        self.control.inbox.draft = lambda stem: runner.committed_draft(self.checkout, stem)
        self.box = self.control._recover.__self__
        self.storm = StormLedger(journal=checkout.journal, clock=checkout.clock)
        self.core.scheduler.continuations = lambda: (
            c.ticket for w in self.writers.values() for c in w.continuations.values())
        self.core.scheduler.next_stage = self.next_stage
        self.core.scheduler.select = self.select
        self.core.admission.resume = self.resume
        self.core.restart.owned = lambda: (
            self.core.admission.active is not None or self.core.admission.task is not None
            or any(w.continuations for w in self.writers.values()))
        self.tasks = DaemonTasks(watcher=self.watch, merge_queue=self.merge, box_consumer=self.triage)
        self.heartbeat = HeartbeatCycle(heartbeat=Heartbeat(state_dir=checkout.config.state_dir,
            fs=checkout.fs), workers=lambda: self.healthy(0),
            merge_queue=lambda: self.healthy(1), box_consumer=lambda: self.healthy(2),
            watcher=lambda: self.healthy(3))

    def healthy(self, component: int) -> bool:
        return (bool(self.health_tasks) and not self.health_tasks[component].done()
                and self.responses[component].responsive())

    def stop(self) -> None:
        self.stopping = True
        self.wake.set()

    def select(self, ticket: Ticket, stage: str) -> bool:
        self.control.poll()
        projection = self.control.projection
        if projection.kill_requested:
            self.stop()
        return (not self.stopping and projection.pause_id is None
                and (self.control.admission is None
                     or self.control.admission in projection.released_hold_ids)
                and not any(target["emitting_origin"] == ticket.stem
                            and target["emitting_stage"] == stage
                            for target in self.storm.held_stages().values()))

    def next_stage(self, ticket: Ticket) -> str:
        writer = self.writers.get(ticket.stem)
        prior = writer.continuations.get(ticket.stem) if writer is not None else None
        return prior.next_stage if prior is not None else "implement"

    def resume(self, ticket: Ticket):
        writer = self.writers.get(ticket.stem)
        return writer if writer is not None and ticket.stem in writer.continuations else None

    @asynccontextmanager
    async def mutations(self):
        async with self.core.admission._slot:
            self.mutating = asyncio.current_task()
            try:
                yield
            finally:
                self.mutating = None

    async def scan(self) -> dict[str, str | None]:
        async with self.mutations():
            if self.stopping:
                return {}
            c = self.checkout
            await intake(c.repo, c.git, c.journal, c.fs)
            return {stem: self.read(stem) for stem in self.stems()}

    async def watch(self) -> None:
        response = self.responses[3]
        async def changes():
            previous = {}
            while True:
                current = await response.wait(self.scan())
                # Hold admission through parsing/publication, including debounce unwind.
                async with self.core.admission._slot:
                    for stem in sorted(previous.keys() | current.keys()):
                        if stem not in previous or previous.get(stem) != current.get(stem):
                            yield stem
                    await response.wait(asyncio.gather(*self.core.watcher._waits.values()))
                previous = current
                await response.wait(self.checkout.sleep(SERVE_POLL_S))
        await self.core.watcher.run(changes())

    async def dispatch(self) -> None:
        response = self.responses[0]
        while True:
            self.control.poll()
            if self.control.projection.kill_requested:
                self.stop()
            if not self.stopping and self.control.projection.pause_id is None:
                await response.wait(self.core.scheduler.dispatch_next())
            await response.wait(self.checkout.sleep(SERVE_POLL_S))

    async def merge(self) -> None:
        response = self.responses[1]
        async def poll():
            async with self.mutations():
                self.control.poll()
                if self.stopping or self.control.projection.kill_requested:
                    return
                async def confirm_draft(stem, request_id):
                    ctx = self.queue.ctx if self.queue is not None else runner.bind(self.checkout, None).ctx
                    await backlog.confirm_draft(stem, request_id, ctx)
                await self.control.inbox.settle_admissions(confirm_draft)
                if self.queue is None:
                    return
                self.queue.deliveries = []
                await self.queue.process()
                for ticket, attempt, result in self.queue.deliveries:
                    owner = self.writers.get(ticket.stem) or next(
                        w for w in self.writers.values() if w.ctx is self.queue.contexts.get(ticket.stem))
                    if isinstance(result, ConflictHandoff) and owner.ctx.driver.llm is None:
                        owner = await self.prepare(self.checkout)
                        owner.queue = self.queue
                        owner.ctx.boundary.select = self.select
                        owner.admission = AdmissionBoundary(owner.ctx, mode="daemon", queue=self.queue)
                    self.writers[ticket.stem] = owner
                    await owner.consume_result(ticket, result, attempt=attempt)
                    if ticket.stem not in self.queue.pending:
                        self.queue.contexts.pop(ticket.stem, None)
                for stem, writer in tuple(self.writers.items()):
                    if not writer.active and not writer.continuations and stem not in self.queue.pending:
                        del self.writers[stem]
        while True:
            await response.wait(poll())
            await response.wait(self.checkout.sleep(SERVE_POLL_S))

    async def triage(self) -> None:
        response = self.responses[2]
        async def poll():
            async with self.mutations():
                if self.stopping:
                    return
                c = replace(self.checkout, config=snapshot_config(self.control.load_config()))
                triage.report_intake(c)
                if not self.box.pending():
                    return
                await triage.triage_pass(c, self.llm)
        while True:
            await response.wait(poll())
            await response.wait(self.checkout.sleep(SERVE_POLL_S))

    async def abort(self) -> None:
        operations = {task for task in (self.core.admission.task, self.mutating) if task is not None}
        aborts = [writer.abort_current for writer in tuple(self.writers.values())]
        if self.control.author_driver is not None:
            aborts.append(self.control.author_driver.abort_current)
        errors = []
        for abort in aborts:
            try:
                await abort()
            except BaseException as exc:
                errors.append(exc)
        # Observe preparation and non-model process operations before WorkerStop touches workers.
        for task in operations:
            if not task.done() and not task.cancelling():
                task.cancel()
        for task in operations:
            cleanup = asyncio.gather(task, return_exceptions=True)
            await protected_cleanup(cleanup)
            errors.extend(result for result in cleanup.result()
                          if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError))
        if errors:
            raise errors[0]

    async def cleanup(self) -> None:
        await self.ready.wait()
        abort_error = None
        try:
            await self.stopper.stop(replace(self.control.projection, kill_requested=True))
        except BaseException as exc:
            abort_error = exc
        finally:
            # Even a failed abort has unwound before worker cleanup. It cannot earn kill_applied.
            await protected_cleanup(_stop_workers(self.workers))
            await protected_cleanup(asyncio.create_task(self.notifications.close()))
        results = await asyncio.gather(self.background, self.observation, return_exceptions=True)
        errors = [task.exception() for task in self.workers if not task.cancelled()]
        errors += [r for r in results if isinstance(r, BaseException)]
        for writer in self.writers.values():
            writer.continuations.clear()
        if abort_error is not None:
            raise abort_error
        for error in errors:
            if isinstance(error, BaseException) and not isinstance(error, asyncio.CancelledError):
                raise error

    async def maintenance(self) -> None:
        self.control.poll()
        if self.control.projection.kill_requested:
            self.stop()
        if self.stopping:
            return
        self.core.restart.timers.fire_due()
        if not self.core.admission._slot.locked() and not self.core.scheduler._slot.locked():
            await self.core.sweep_orphans()
            async with self.core.admission._slot:
                self.box.recover()
                await self.checkpoint.poll()
        self.heartbeat.cycle()
        self.notifications.poll()

    async def run(self) -> int:
        c = self.checkout
        lock = Lockfile(c.config.state_dir, instance_id=await c.git.describe(c.repo), clock=c.clock)
        lock.acquire()
        restore = lambda: None
        code = 0
        try:
            try:
                await self.core.startup()
                self.notifications.poll(startup=True)
            except Exception as exc:
                raise Refusal(f"serve startup recovery refused: {exc}",
                                     "repair the recovery evidence and start serve again") from exc
            self.control.publish()
            if self.signals is not None:
                restore = self.signals(self.stop)
            self.checkpoint = checkpoint_push(c.repo, journal=c.journal, effects=Effects(c.journal),
                timers=self.core.restart.timers, git=c.git, box=self.box, clock=c.clock, log=self.log)
            # Guard only this lifetime: the component's ordinary run semantics stay intact.
            pending_failure = None
            async def guarded(callback):
                nonlocal pending_failure, code
                try:
                    await callback()
                    raise RuntimeError("owned serve consumer returned unexpectedly")
                except BaseException as exc:
                    # Latch before DaemonTasks cancels siblings; those cancellations
                    # belong to this lifetime's cleanup, not another worker failure.
                    if not self.stopping and not self.control.projection.kill_requested:
                        pending_failure = exc
                        code = (2 if isinstance(exc, (ConfigError, Refusal,
                                                     JournalCorruption, ExecutableNotFound)) else 1)
                    self.stop()
                    raise
            def observed_failure(_exc):
                nonlocal pending_failure
                if pending_failure is not None:
                    exc, pending_failure = pending_failure, None
                    self.failure(exc)
            self.tasks._consumers = tuple((lambda callback=callback: guarded(callback))
                                          for callback in self.tasks._consumers)
            def bind(tasks):
                self.workers = (worker, *tasks)
                self.health_tasks = (worker, tasks[1], tasks[2], tasks[0])
                self.stopper = WorkerStop(lifecycle_id=self.control.projection.lifecycle_id,
                                         abort=self.abort, workers=self.workers)
                self.observer = WorkerFailureObserver(lifecycle_id=self.control.projection.lifecycle_id,
                    workers=self.workers, projection=lambda: self.control.projection, failure=observed_failure)
                self.observation = asyncio.create_task(self.observer.observe())
                self.ready.set()
            self.tasks.started = bind
            self.background = asyncio.create_task(self.tasks.run())
            worker = asyncio.create_task(guarded(self.dispatch))
            try:
                await self.ready.wait()
                while not self.stopping:
                    await self.maintenance()
                    for response in self.responses:
                        response.ask()
                    done = [task for task in self.workers if task.done()]
                    if done:
                        for task in done:
                            if task.cancelled():
                                raise RuntimeError("owned serve consumer cancelled unexpectedly")
                            task.result()
                        raise RuntimeError("owned serve consumer stopped unexpectedly")
                    sleeping = asyncio.create_task(c.sleep(SERVE_POLL_S))
                    stopping = asyncio.create_task(self.wake.wait())
                    try:
                        await asyncio.wait((sleeping, stopping), return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        sleeping.cancel()
                        stopping.cancel()
                        await protected_cleanup(asyncio.gather(sleeping, stopping, return_exceptions=True))
            except asyncio.CancelledError:
                self.stop()
            except Exception as exc:
                self.log.event("serve_failed", error=str(exc))
                code = (2 if isinstance(exc, (ConfigError, Refusal,
                                             JournalCorruption, ExecutableNotFound)) else 1)
                self.stop()
            cleanup = asyncio.create_task(self.control.apply_kill(self.cleanup)
                if self.control.projection.kill_requested else self.cleanup())
            try:
                await protected_cleanup(cleanup)
            except asyncio.CancelledError:
                if cleanup.cancelled():
                    code = 1
            except Exception as exc:
                self.log.event("serve_cleanup_failed", error=str(exc))
                if code != 2:
                    code = 1
            return code
        finally:
            try:
                await protected_cleanup(asyncio.create_task(self.notifications.close()))
            finally:
                try:
                    self.control.retire()
                finally:
                    restore()
                    lock.release()


async def serve(checkout: Checkout, *, config_path: Path | None = None,
                read: Callable[[str], str | None] | None = None,
                stems: Callable[[], Iterable[str]] | None = None,
                prepare, llm, signals: Callable | None = None,
                failure: Callable[[BaseException], None] | None = None) -> int:
    from tempo.__main__ import build_serve

    owner = build_serve(checkout, config_path=config_path, read=read, stems=stems,
                        prepare=prepare, llm=llm, signals=signals, failure=failure)
    return await owner.run()
