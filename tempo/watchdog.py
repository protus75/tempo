"""Watched LLM execution and run-local spend/progress detection."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence

from tempo.artifacts import Cost, StageResult
from tempo.driver import _observe
from tempo.llm import LLMAborted, LLMResult
from tempo.providers import ADAPTERS, ProviderSetupError, Served
from tempo.seams import Clock, Sleep
from tempo.specs import unit_sha

SPIRAL_SPEND_MULTIPLIER = 3


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


class ScopeObservation:
    """Project an injected path/byte snapshot onto the admitted fence, including plan slices."""

    def __init__(self, fence: Sequence[str], snapshot: Callable[[], Mapping[str, bytes]]) -> None:
        self.fence, self.snapshot = fence, snapshot

    def __call__(self) -> dict:
        files = self.snapshot()
        observed = {path: data for path, data in files.items() if any(
            path == prefix or path.startswith(prefix if prefix.endswith('/') else prefix + '/')
            for prefix in self.fence if '#' not in prefix)}
        for prefix in self.fence:
            if prefix.startswith('TEMPO_PLAN.md#'):
                data = files.get('TEMPO_PLAN.md')
                observed[prefix] = unit_sha(data.decode(), prefix.partition('#')[2]) if data is not None else None
        return observed


class CallMeter(EventConsumer):
    """A fixed basis and cumulative snapshots belong to one call, never to a retry."""

    def __init__(self, detector: 'Detector', served: Served) -> None:
        self.detector = detector
        self.identity = (served.provider.name, served.model)
        estimate = served.provider.limits.est_cost_per_call_usd
        self.reports_cost = ADAPTERS[served.provider.name].reports_cost
        if not self.reports_cost and estimate is None:
            raise ProviderSetupError('stream reports no cost; declare limits.est_cost_per_call_usd')
        self.basis = detector.bases.get(self.identity, estimate)
        self.estimate = estimate or 0.0
        self.epoch = detector.progress_epoch
        self.metered: float | None = None
        self.tokens = 0
        self.usage: dict[str, dict[str, int]] = {}
        self.tools: set[str] = set()
        detector.spend += self.estimate
        detector.total_usd += self.estimate
        super().__init__(self._consume)

    def _consume(self, event: dict) -> None:
        d = self.detector
        d.observe_progress()
        kind = event.get('type')
        message = event.get('message')
        if not isinstance(message, dict):
            message = {}
        if kind == 'assistant':
            content = message.get('content')
            if isinstance(content, list):
                self.tools.update(block['id'] for block in content
                                  if isinstance(block, dict) and block.get('type') == 'tool_use' and 'id' in block)
        item = event.get('item')
        if not isinstance(item, dict):
            item = {}
        if kind in {'item.started', 'item.updated', 'item.completed'} and item.get('type') in {
                'command_execution', 'file_change', 'mcp_tool_call', 'web_search'} and 'id' in item:
            self.tools.add(item['id'])
        usage = event.get('usage') if kind in {'result', 'turn.completed'} else message.get('usage')
        if isinstance(usage, dict):
            key = 'terminal' if kind in {'result', 'turn.completed'} else message.get('id')
            if key is not None:
                previous = self.usage.setdefault(key, {})
                for field in ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'):
                    value = usage.get(field)
                    if isinstance(value, int):
                        previous[field] = max(previous.get(field, 0), value)
                # Terminal usage describes the whole call, not an additional message.
                count = max(sum(self.usage.get('terminal', {}).values()),
                            sum(sum(v.values()) for k, v in self.usage.items() if k != 'terminal'))
                if any(self.usage.values()):
                    d.tokens = (d.tokens or 0) + count - self.tokens
                self.tokens = count
        if kind == 'result' and event.get('total_cost_usd') is not None:
            total = event['total_cost_usd']
            previous = self.metered if self.metered is not None else 0.0
            delta = max(0.0, total - previous)
            if self.metered is None:
                d.total_usd -= self.estimate
                if self.epoch == d.progress_epoch:
                    d.spend -= self.estimate
            d.spend += delta
            d.total_usd += delta
            self.metered = max(previous, total)
        d.decide()

    def complete(self) -> None:
        if self.metered is not None:
            self.detector.bases.setdefault(self.identity, self.metered)
        else:
            # A successful adapter result without metered USD confirmed the flat fallback.
            self.reports_cost = False
        self.detector.decide()


class Detector:
    """One instance per run, retaining spend and warning state across re-prompts.

    cap_wait supplies cumulative excluded seconds from admission/wait records (including an
    in-flight wait when observing admission). No journal or filesystem writer lives here.
    notify is bound by tempo.notify; requests never block the hard-deadline monitor.
    """

    def __init__(self, *, expected_minutes: float, stuck_minutes: float, clock: Clock, sleep: Sleep,
                 observe: ScopeObservation, cap_wait: Callable[[], float], log,
                 notify: Callable[[str], Awaitable[None]]) -> None:
        self.expected_seconds, self.stuck_seconds = expected_minutes * 60, stuck_minutes * 60
        self.clock, self.sleep, self.observe, self.cap_wait = clock, sleep, observe, cap_wait
        self.log, self.notify = log, notify
        self.started = clock()
        self.last_scope = observe()  # Before work starts, even if the first event is a mutation.
        self.progress_epoch = 0
        self.spend = self.total_usd = 0.0
        self.tokens: int | None = None
        self.bases: dict[tuple[str, str], float] = {}
        self.call: CallMeter | None = None
        self.spiral = self.warned = self.stuck_warned = False
        self.pending: list[str] = []
        self.deliveries: list[asyncio.Task] = []
        self.calls = 0

    @property
    def active_seconds(self) -> float:
        return max(0.0, (self.clock() - self.started).total_seconds() - self.cap_wait())

    @property
    def region(self) -> str:
        if self.active_seconds >= self.stuck_seconds:
            return 'stuck'
        return 'healthy' if self.active_seconds < self.expected_seconds * 1.5 else 'soft'

    def observe_progress(self) -> None:
        current = self.observe()
        if current != self.last_scope:
            self.last_scope = current
            self.spend, self.spiral = 0.0, False
            self.progress_epoch += 1

    def start_call(self, served: Served) -> CallMeter:
        self.observe_progress()
        self.call = CallMeter(self, served)
        self.calls += 1
        self.decide()
        return self.call

    def decide(self) -> str:
        self.observe_progress()
        basis = self.call.basis if self.call is not None else None
        spend = self.spend
        if (self.call is not None and self.call.reports_cost and self.call.metered is None
                and self.call.epoch == self.progress_epoch):
            # Reserve the estimate for a hung call, but never page from a provisional price
            # when the adapter will supply the actual cumulative USD.
            spend -= self.call.estimate
        if basis is not None and spend > SPIRAL_SPEND_MULTIPLIER * basis:
            if not self.spiral:
                self.log.event('spiral_detected', spend=self.spend, basis=basis, region=self.region)
            self.spiral = True
        region = self.region
        if region == 'soft' and self.spiral and not self.warned:
            self.warned = True
            self.pending.append('spiral-warning')
            self.log.event('spiral_warning', spend=self.spend)
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
                return StageResult('timeout', None, [], Cost(tokens=self.tokens, usd=self.total_usd,
                    seconds=self.active_seconds, attempts=self.calls,
                    provider=meter.identity[0], model=meter.identity[1]))
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
