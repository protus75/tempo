"""Engine log (TEMPO_PLAN.md section 6): line-oriented diagnostics, size-rotated, never authoritative.

Not the journal: nothing reads this for a gate or dispatch decision.
"""

import json
from pathlib import Path
from typing import Any

from tempo.journal import render_ts
from tempo.redact import Redactor
from tempo.seams import Clock

MAX_BYTES = 16 * 1024 * 1024
BACKUPS = 3  # 4 files with the active one


class EngineLog:
    def __init__(
        self, path: Path, redactor: Redactor, clock: Clock, *, max_bytes: int = MAX_BYTES, backups: int = BACKUPS
    ) -> None:
        self.path = path
        self._redactor = redactor
        self._clock = clock
        self._max_bytes = max_bytes
        self._backups = backups

    def event(self, event: str, **fields: Any) -> None:
        record = {"ts": render_ts(self._clock()), "event": event, **fields}
        line = (self._redactor.scrub(json.dumps(record, default=str)) + "\n").encode()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size + len(line) > self._max_bytes:
            self._rotate()
        with open(self.path, "ab") as f:
            f.write(line)

    def _rotate(self) -> None:
        for i in range(self._backups, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{i - 1}") if i > 1 else self.path
            if src.exists():
                src.replace(self.path.with_name(f"{self.path.name}.{i}"))
