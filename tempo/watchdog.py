"""Watched LLM execution and run-local stuck detection."""

import asyncio
from collections.abc import Awaitable, Callable

from tempo.artifacts import StageResult
from tempo.driver import _observe
from tempo.llm import LLMAborted, LLMResult
from tempo.providers import Served
from tempo.seams import Clock, Sleep


class WatchedLLM:
    """The effect still owns execution/replay; only executed calls consume adapter events."""

    def __init__(self, llm, detector):
        self.llm, self.detector = llm, detector
        self.kind = llm.kind

    async def call(self, req):
        return await self.llm.call(req, consumer=None)

    async def admitted_call(self, req, *, ticket, call_key, effect):
        return await self.llm.admitted_call(req, ticket=ticket, call_key=call_key, effect=effect,
                                           meter=self.detector.start_call)

    def abort_current(self):
        self.llm.abort_current()


async def watch_deadline(detector, remaining):
    deadline = detector.active_seconds + remaining
    while detector.decide() != 'stuck' and detector.active_seconds < deadline:
        if detector.pending:
            detector.deliveries.append(asyncio.create_task(detector.flush_notifications()))
        await detector.sleep(min(1.0, deadline - detector.active_seconds))
    if detector.pending:
        detector.deliveries.append(asyncio.create_task(detector.flush_notifications()))


class EventConsumer:
    """Receive scrubbed provider objects in order from the adapter's sole spool writer."""

    def __init__(self, on_event: Callable[[dict], None]) -> None:
        self._on_event = on_event

    def consume(self, event: dict) -> None:
        self._on_event(event)


class CallMeter(EventConsumer):
    """One call's served identity; every stream event re-runs the detector's decision."""

    def __init__(self, detector: 'Detector', served: Served) -> None:
        self.detector = detector
        self.identity = (served.provider.name, served.model)
        super().__init__(self._consume)

    def _consume(self, event: dict) -> None:
        self.detector.decide()

    def complete(self) -> None:
        self.detector.decide()


class Detector:
    """One instance per run, retaining warning state across re-prompts.

    cap_wait supplies cumulative excluded seconds from admission/wait records (including an
    in-flight wait when observing admission). No journal or filesystem writer lives here.
    notify is bound by tempo.notify; requests never block the hard-deadline monitor.
    """

    def __init__(self, *, stuck_minutes: float, clock: Clock, sleep: Sleep,
                 cap_wait: Callable[[], float], log, notify: Callable[[str], Awaitable[None]]) -> None:
        self.stuck_seconds = stuck_minutes * 60
        self.clock, self.sleep, self.cap_wait = clock, sleep, cap_wait
        self.log, self.notify = log, notify
        self.started = clock()
        self.call: CallMeter | None = None
        self.stuck_warned = False
        self.pending: list[str] = []
        self.deliveries: list[asyncio.Task] = []

    @property
    def active_seconds(self) -> float:
        return max(0.0, (self.clock() - self.started).total_seconds() - self.cap_wait())

    @property
    def region(self) -> str:
        return 'stuck' if self.active_seconds >= self.stuck_seconds else 'healthy'

    def start_call(self, served: Served) -> CallMeter:
        self.call = CallMeter(self, served)
        self.decide()
        return self.call

    def decide(self) -> str:
        region = self.region
        if region == 'stuck' and not self.stuck_warned:
            self.stuck_warned = True
            self.pending.append('stuck-past-threshold')
            self.log.event('stuck_budget_kill', active_seconds=self.active_seconds)
        return region

    async def flush_notifications(self) -> None:
        while self.pending:
            await self.notify(self.pending.pop(0))

    async def close_notifications(self) -> None:
        await asyncio.gather(*self.deliveries)
        await self.flush_notifications()

    async def watch(self, start: Callable[[EventConsumer], Awaitable[LLMResult]], *,
                    served: Served, abort: Callable[[], None]) -> LLMResult | StageResult:
        meter = self.start_call(served)
        work = asyncio.ensure_future(start(meter))
        deliveries: list[asyncio.Task] = []

        async def monitor() -> None:
            while True:
                region = self.decide()
                if self.pending:
                    deliveries.append(asyncio.create_task(self.flush_notifications()))
                if region == 'stuck':
                    return
                await self.sleep(min(1.0, self.stuck_seconds - self.active_seconds))

        timer = asyncio.create_task(monitor())

        async def cleanup() -> None:
            results = await asyncio.gather(work, timer, *deliveries, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, (asyncio.CancelledError, LLMAborted)):
                    raise result

        try:
            await asyncio.wait({work, timer}, return_when=asyncio.FIRST_COMPLETED)
            # The hard boundary wins even if a terminal event arrived in the same turn.
            if self.decide() == 'stuck':
                abort()  # Signal synchronously before cancelling any owned work.
                work.cancel()
                timer.cancel()
                await _observe(asyncio.create_task(cleanup()))
                return StageResult('timeout', None, [], provider=meter.identity[0], model=meter.identity[1])
            if timer.done():
                timer.result()
            result = work.result()
            meter.complete()
            return result
        finally:
            if not work.done():
                abort()
                work.cancel()
            timer.cancel()
            await _observe(asyncio.create_task(cleanup()))
            await self.flush_notifications()
