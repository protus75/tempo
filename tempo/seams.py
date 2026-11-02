"""Injectable seams (TEMPO_PLAN.md section 15): engine code never calls these raw."""

import asyncio
import os
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

Clock = Callable[[], datetime]
# Every timed wait races its work against this to a clock-derived deadline, so tests wait zero wall-clock.
Sleep = Callable[[float], Awaitable[None]]


@runtime_checkable
class ProcessExec(Protocol):
    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float | None,
        stdin_path: Path | None = None,
    ) -> tuple[int, str, str]: ...


@runtime_checkable
class GroupExec(ProcessExec, Protocol):
    """Publish spawn groups and optionally deliver stdout lines synchronously, including the EOF tail.

    A failed line consumer stops delivery, not capture: drain and reap, then raise its first
    exception with (stdout, stderr) in `process_capture`. An intervening unwind keeps its
    exception type, with the consumer failure as its cause and the capture attached to both.
    """

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float | None,
        stdin_path: Path | None = None,
        on_spawn: Callable[[int], None] | None = None,
        on_stdout_line: Callable[[str], None] | None = None,
    ) -> tuple[int, str, str]: ...

    def kill_group(self, pgid: int) -> None:
        """Signal-not-reap: returns once the group is SIGKILLed; the spawner's `run` still reaps."""
        ...


class ExecutableNotFound(Exception):
    """argv[0] does not resolve: a config/setup refusal, never a raw FileNotFoundError."""

    def __init__(self, binary: str) -> None:
        super().__init__(f"executable not found: {binary!r} (install it or fix PATH in the child env)")
        self.binary = binary


@runtime_checkable
class Notifications(Protocol):
    async def notify(self, argv: Sequence[str]) -> dict[str, str | int]: ...


class NotificationFailed(Exception):
    """Delivery did not complete; repair the command and retry the pending evidence."""


class CommandNotifications:
    def __init__(self, exec_: ProcessExec, *, cwd: Path, env: Mapping[str, str],
                 timeout: float, scrub: Callable[[str], str]) -> None:
        self.exec_, self.cwd, self.env = exec_, cwd, env
        self.timeout, self.scrub = timeout, scrub

    async def notify(self, argv: Sequence[str]) -> dict[str, str | int]:
        if (not argv or isinstance(argv, str) or any(not isinstance(p, str) or "\0" in p for p in argv)
                or not argv[0].strip()):
            raise ValueError("notify needs a nonempty argv list with a nonblank executable and no NULs")
        try:
            rc, out, err = await self.exec_.run(list(argv), cwd=self.cwd, env=self.env, timeout=self.timeout)
        except (ExecutableNotFound, TimeoutError, OSError) as exc:
            raise NotificationFailed(self.scrub(f"notify failed: {exc}; install/fix notify argv or PATH "
                                                "and retry pending evidence")) from None
        out, err = self.scrub(out), self.scrub(err)
        if rc != 0:
            raise NotificationFailed(f"notify exited {rc}: {out[-2000:]} {err[-2000:]}; "
                                     "make notify exit 0 and retry pending evidence")
        return {"rc": rc, "stdout": out, "stderr": err}


class SubprocessExec:
    """Real process-exec seam: every child in its own process group, every kill a group kill."""

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float | None,
        stdin_path: Path | None = None,
        on_spawn: Callable[[int], None] | None = None,
        on_stdout_line: Callable[[str], None] | None = None,
    ) -> tuple[int, str, str]:
        if shutil.which(argv[0], path=env.get("PATH", "")) is None:
            raise ExecutableNotFound(argv[0])
        # The one unbounded caller -- the drain's self-upgrade handoff child -- streams to the operator through
        # INHERITED stdio; its out/err come back empty (section 15).
        stream = None if timeout is None else subprocess.PIPE
        with open(stdin_path, "rb") if stdin_path else open(os.devnull, "rb") as stdin:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=dict(env),
                stdin=stdin,
                stdout=stream,
                stderr=stream,
                start_new_session=True,
            )
            readers: list[asyncio.Task] = []
            stdout, stderr = bytearray(), bytearray()
            consumer_error: BaseException | None = None

            def deliver(line: str) -> None:
                nonlocal consumer_error
                if consumer_error is None and on_stdout_line is not None:
                    try:
                        on_stdout_line(line)
                    except BaseException as exc:
                        consumer_error = exc

            try:
                if on_spawn is not None:
                    on_spawn(proc.pid)  # start_new_session: the child's pid IS its pgid
                async with asyncio.timeout(timeout):
                    if stream is None:
                        out, err = await proc.communicate()
                    else:
                        readers = [asyncio.create_task(_read_pipe(proc.stdout, stdout,
                                                                 deliver if on_stdout_line is not None else None)),
                                   asyncio.create_task(_read_pipe(proc.stderr, stderr)),
                                   asyncio.create_task(proc.wait())]
                        out, err, _ = await asyncio.gather(*readers)
            except BaseException as exc:
                # Timeout and outer cancellation alike: grandchildren must not outlive the call.
                for reader in readers:
                    reader.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
                remaining_out, remaining_err = await _kill_and_wait(proc)
                if consumer_error is not None:
                    stdout.extend(remaining_out or b"")
                    stderr.extend(remaining_err or b"")
                    capture = (stdout.decode(errors="replace"), stderr.decode(errors="replace"))
                    consumer_error.process_capture = capture
                    exc.process_capture = capture
                    # Cancellation/timeout still owns the unwind; keep the consumer failure visible.
                    raise exc from consumer_error
                raise
            if consumer_error is not None:
                consumer_error.process_capture = (stdout.decode(errors="replace"), stderr.decode(errors="replace"))
                raise consumer_error
        return proc.returncode or 0, (out or b"").decode(errors="replace"), (err or b"").decode(errors="replace")

    def kill_group(self, pgid: int) -> None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def _kill_and_wait(proc: asyncio.subprocess.Process) -> tuple[bytes | None, bytes | None]:
    SubprocessExec().kill_group(proc.pid)
    # Drain after failed/cancelled readers: wait alone can hang on a full pipe transport.
    return await proc.communicate()


async def _read_pipe(
    pipe: asyncio.StreamReader,
    captured: bytearray,
    on_line: Callable[[str], None] | None = None,
) -> bytes:
    pending = bytearray()
    while chunk := await pipe.read(65536):
        captured.extend(chunk)
        if on_line is not None:
            pending.extend(chunk)
            while (end := pending.find(b"\n")) >= 0:
                line = bytes(pending[:end + 1])
                del pending[:end + 1]
                on_line(line.decode(errors="replace"))
    if pending and on_line is not None:
        on_line(pending.decode(errors="replace"))
    return bytes(captured)


@runtime_checkable
class FileSystem(Protocol):
    def publish(self, path: Path, data: bytes) -> None:
        """Durable atomic publication; an existing destination is never overwritten."""
        ...

    def write(self, path: Path, data: bytes) -> None:
        """Atomic: temp file, fsync, rename."""
        ...

    def replace(self, src: Path, dst: Path) -> None: ...


class LocalFileSystem:
    def publish(self, path: Path, data: bytes) -> None:
        # Sync every ancestor edge, including directories another publisher just created.
        path.parent.mkdir(parents=True, exist_ok=True)
        for directory in (path.parent, *path.parent.parents):
            self._sync_directory(directory)
        fd, name = tempfile.mkstemp(prefix=".control-", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # link is atomic and refuses an existing name, unlike rename/replace.
            os.link(temporary, path)
            temporary.unlink()
            self._sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _sync_directory(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def write(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def replace(self, src: Path, dst: Path) -> None:
        os.replace(src, dst)
