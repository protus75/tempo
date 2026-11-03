"""Single-writer lockfile (TEMPO_PLAN.md D2, section 6): one advisory flock per checkout.

Correctness comes from `flock` alone: the kernel frees the lock when the holder
dies, so there is no liveness protocol and no stale-pid reclaim. The recorded
identity is diagnostics for the refusal message, never read for a decision.

Beside the lock sits the orchestrator session record (AGENT_PLAN.md sections 4.1, 4.6). The flock
serializes writers; the record says which chat's tool calls are served. Every tool call renews its
heartbeat, and `begin` takes over a record whose heartbeat is older than LOCK_STALE_S.
"""

import fcntl
import json
import os
import socket
from datetime import datetime
from pathlib import Path
from typing import Any

from tempo.checkout import Refusal
from tempo.journal import render_ts
from tempo.seams import Clock, FileSystem

# Contract constant (TEMPO_PLAN.md section 22): ten times TOOL_CALL_S.
LOCK_STALE_S = 900.0
SESSION_FILE = "session.json"


class LockHeld(Exception):
    """Another process holds the checkout's writer lock."""

    def __init__(self, path: Path, holder: dict[str, Any] | None) -> None:
        who = json.dumps(holder) if holder else "unknown holder (identity not yet written)"
        super().__init__(
            f"{path} is held by {who}; wait for that process to exit or stop it -- the kernel frees"
            " the lock when it dies, so never delete the lockfile"
        )
        self.path = path
        self.holder = holder


class Lockfile:
    def __init__(self, state_dir: Path, *, instance_id: str, clock: Clock) -> None:
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "tempo.lock"
        self._instance_id = instance_id
        self._clock = clock
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, *, wait: bool = False) -> None:
        """Take the lock before any write; refuse (LockHeld) if another open holds it, or block when `wait`."""
        if self._fd is not None:
            raise RuntimeError(f"{self.path} is already held by this Lockfile; release it first")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # os.open fds are non-inheritable, so a spawned child cannot pin the lock.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = _read_holder(fd)
            os.close(fd)
            raise LockHeld(self.path, holder) from None
        except BaseException:
            os.close(fd)
            raise
        identity = {
            "instance_id": self._instance_id,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "state_dir": str(self.state_dir),
            "started_at": render_ts(self._clock()),
        }
        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, json.dumps(identity).encode(), 0)
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        """Free the lock."""
        if self._fd is None:
            raise RuntimeError(f"{self.path} is not held by this Lockfile; nothing to release")
        fd, self._fd = self._fd, None
        os.close(fd)


def _read_holder(fd: int) -> dict[str, Any] | None:
    # The holder writes its identity after taking the lock; a contender can race that write.
    try:
        holder = json.loads(os.pread(fd, 65536, 0) or b"null")
    except ValueError:
        return None
    return holder if isinstance(holder, dict) else None


class Sessions:
    """The one orchestrator session record, `<state>/session.json`; `begin` runs under the writer lock."""

    def __init__(self, state_dir: Path, *, clock: Clock, fs: FileSystem) -> None:
        self.path = Path(state_dir) / SESSION_FILE
        self._clock = clock
        self._fs = fs

    def read(self) -> dict[str, Any] | None:
        return json.loads(self.path.read_text()) if self.path.is_file() else None

    def _write(self, record: dict[str, Any]) -> None:
        self._fs.write(self.path, (json.dumps(record, indent=2) + "\n").encode())

    def begin(self, *, token: str, host: str, chat: str, number: int) -> dict[str, Any]:
        held = self.read()
        now = self._clock()
        if held is not None and (now - datetime.fromisoformat(held["heartbeat"])).total_seconds() <= LOCK_STALE_S:
            raise Refusal(f"session {held['number']} is held by the {held['host']} chat {held['chat']},"
                          f" last active {held['heartbeat']}",
                          "end that session with write_handoff, or wait until it has been idle"
                          f" {LOCK_STALE_S:.0f}s and begin again")
        record = {"token": token, "host": host, "chat": chat, "number": number, "started_at": render_ts(now),
                  "heartbeat": render_ts(now), "tickets": 0}
        self._write(record)
        return record

    def check(self, token: object) -> dict[str, Any]:
        """The record this token holds, with its heartbeat renewed; refuse any other token."""
        held = self.read()
        if held is None or token != held["token"]:
            raise Refusal("this session token does not hold the tempo session",
                          "run `tempo tool begin_session` from the /tempo-run chat and pass its token")
        held["heartbeat"] = render_ts(self._clock())
        self._write(held)
        return held

    def update(self, record: dict[str, Any]) -> None:
        self._write(record)

    def end(self, token: object) -> None:
        self.check(token)
        self.path.unlink()
