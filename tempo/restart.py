"""Lock-held startup and idle recovery (TEMPO_PLAN.md 19.P3.restart-timers)."""

import asyncio
from collections.abc import Callable

from tempo import reconcile, runner
from tempo.timers import Timers


class Restart:
    """The caller holds the checkout writer lock and serializes live ownership."""

    def __init__(self, checkout: runner.Checkout, *, timers: Timers,
                 owned: Callable[[], bool]) -> None:
        self.checkout, self.timers, self.owned = checkout, timers, owned
        self.ready = False
        self._slot = asyncio.Lock()
        self._failure: BaseException | None = None
        self.admissions = None

    async def _reconcile(self) -> list[str]:
        checkout = self.checkout
        return await reconcile.reconcile(
            checkout.journal, checkout.git, checkout.repo, checkout.config.worktree_root,
            lambda stem, attempt: runner.harvest_orphan(checkout, stem, attempt),
        )

    async def startup(self) -> None:
        async with self._slot:
            if self._failure is not None:
                raise self._failure
            if self.ready:
                return
            if self.owned():
                raise ValueError("startup requires no live run or admission; finish and await its cleanup first")
            try:
                if self.admissions is not None:
                    await self.admissions()
                await self._reconcile()
                self.timers.reconstruct()
                self.timers.fire_due()
            except BaseException as exc:
                # A reap may have terminalled before cleanup failed. Re-folding alone
                # must not erase that failure and authorize dispatch in this lifetime.
                self._failure = exc
                raise
            self.ready = True

    async def sweep_orphans(self) -> list[str]:
        async with self._slot:
            if self.owned():
                return []
            if self._failure is not None:
                raise self._failure
            return await self._reconcile()
