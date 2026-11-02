"""The LLM seam (TEMPO_PLAN.md section 6) and its scripted fake."""

import asyncio
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

AgentTier = Literal["low", "medium", "high", "max"]
AgentEffort = Literal["low", "medium", "high", "max"]


@dataclass(frozen=True)
class LLMRequest:
    surface: str
    rendered: str
    tier: AgentTier
    effort: AgentEffort
    ticket: str | None
    worktree: Path | None  # set for a cli Implement run only: the one surface granted write
    budget_usd: float | None = None

    def __post_init__(self) -> None:
        if self.budget_usd is not None and (type(self.budget_usd) not in (float, int)
                or not math.isfinite(self.budget_usd) or self.budget_usd < 0):
            raise ValueError("budget_usd must be finite and nonnegative, or null")


@dataclass(frozen=True)
class LLMResult:
    text: str
    input_tokens: int | None  # None when a cli stream reports no usage
    output_tokens: int | None
    provider: str
    model: str
    usd: float


class LLM(Protocol):
    kind: Literal["api", "cli"]

    async def call(self, req: LLMRequest) -> LLMResult: ...

    def abort_current(self) -> None:
        """Synchronous: returns only once the external writer can no longer mutate the worktree."""
        ...


class LLMAborted(Exception):
    pass


HANG = object()  # script item: block, ignoring cancellation, until abort_current -- a hung CLI's shape

ScriptItem = str | LLMResult | BaseException | Callable[[LLMRequest], "ScriptItem"] | object


class FakeLLM:
    """Scripted responses in order; records every request. Exhausting the script is a test defect."""

    kind: Literal["api", "cli"] = "cli"

    def __init__(self, script: Iterable[ScriptItem]) -> None:
        self.script = list(script)
        self.requests: list[LLMRequest] = []
        self.aborted = 0
        self._abort = asyncio.Event()

    async def call(self, req: LLMRequest) -> LLMResult:
        self.requests.append(req)
        if not self.script:
            raise AssertionError(f"FakeLLM script exhausted at call {len(self.requests)}; script one more response")
        item = self.script.pop(0)
        while callable(item) and not isinstance(item, type):
            item = item(req)
        if item is HANG:
            self._abort.clear()
            while not self._abort.is_set():
                try:
                    await self._abort.wait()
                except asyncio.CancelledError:
                    pass  # cancellation alone does not stop a hung writer; only abort_current does
            raise LLMAborted("aborted")
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, LLMResult):
            return item
        if isinstance(item, str):
            return LLMResult(text=item, input_tokens=None, output_tokens=None, provider="fake", model="fake", usd=0.0)
        raise TypeError(f"unscriptable FakeLLM item {item!r}; use str, LLMResult, an exception, HANG, or a callable")

    def abort_current(self) -> None:
        self.aborted += 1
        self._abort.set()
