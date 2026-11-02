"""Dormant heartbeat record and external liveness evidence (19.P3.heartbeat)."""

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from tempo.seams import Clock, FileSystem


class Heartbeat:
    def __init__(self, *, state_dir: Path, fs: FileSystem) -> None:
        self.path, self.fs = state_dir / "heartbeat", fs

    def refresh(self) -> None:
        self.fs.write(self.path, b"")


def is_fresh(path: Path, *, metadata: Callable[[Path], datetime | None],
             clock: Clock, max_age: timedelta) -> bool:
    """Read-only evidence; the external caller owns scheduling and any response."""
    if max_age <= timedelta(0):
        raise ValueError("supply a strictly positive maximum age duration")
    mtime = metadata(path)
    if mtime is None:
        return False
    now = clock()
    if mtime.utcoffset() is None or now.utcoffset() is None:
        raise ValueError("supply timezone-aware modification and clock times")
    return timedelta(0) <= now - mtime <= max_age
