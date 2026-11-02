import json
import os
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from tempo.checkout import Refusal
from tempo.lockfile import LOCK_STALE_S, LockHeld, Lockfile, Sessions
from tempo.seams import LocalFileSystem

T0 = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)


def make(state_dir, instance_id="v0.1.0"):
    return Lockfile(state_dir, instance_id=instance_id, clock=lambda: T0)


def test_lockfile_lives_at_state_dir_tempo_lock(tmp_path):
    lock = make(tmp_path)
    lock.acquire()
    assert lock.path == tmp_path / "tempo.lock"
    assert lock.path.exists()
    lock.release()


def test_acquire_records_holder_identity(tmp_path):
    lock = make(tmp_path, instance_id="v0.1.0-3-gabc1234-dirty")
    lock.acquire()
    assert json.loads(lock.path.read_text()) == {
        "instance_id": "v0.1.0-3-gabc1234-dirty",
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "state_dir": str(tmp_path),
        "started_at": "2026-08-04T12:00:00+00:00",
    }
    lock.release()


def test_lock_contention_refused_naming_holder(tmp_path):
    first = make(tmp_path, instance_id="first")
    first.acquire()
    second = make(tmp_path, instance_id="second")
    with pytest.raises(LockHeld) as exc:
        second.acquire()
    assert exc.value.holder["instance_id"] == "first"
    assert exc.value.holder["pid"] == os.getpid()
    assert "first" in str(exc.value)
    assert not second.held
    # A refused contender must not clobber the holder's identity.
    assert json.loads(first.path.read_text())["instance_id"] == "first"
    first.release()


def test_contention_refused_across_processes(tmp_path):
    lock = make(tmp_path)
    lock.acquire()
    child = (
        "import sys\n"
        "from datetime import UTC, datetime\n"
        "from pathlib import Path\n"
        "from tempo.lockfile import LockHeld, Lockfile\n"
        "l = Lockfile(Path(sys.argv[1]), instance_id='child', clock=lambda: datetime.now(UTC))\n"
        "try:\n"
        "    l.acquire()\n"
        "except LockHeld:\n"
        "    sys.exit(3)\n"
        "sys.exit(0)\n"
    )
    rc = subprocess.run([sys.executable, "-c", child, str(tmp_path)], cwd=os.getcwd()).returncode
    assert rc == 3
    lock.release()
    rc = subprocess.run([sys.executable, "-c", child, str(tmp_path)], cwd=os.getcwd()).returncode
    assert rc == 0


def test_release_then_reacquire(tmp_path):
    first = make(tmp_path, instance_id="parent")
    first.acquire()
    first.release()
    assert not first.held
    second = make(tmp_path, instance_id="child")
    second.acquire()
    assert second.held
    assert json.loads(second.path.read_text())["instance_id"] == "child"
    second.release()
    # The same object can take the lock again after releasing it.
    first.acquire()
    assert first.held
    first.release()


def test_lock_fd_not_inherited_by_children(tmp_path):
    # The self-upgrade handoff spawns a child after release; a leaked fd would keep the lock alive.
    lock = make(tmp_path)
    lock.acquire()
    assert os.get_inheritable(lock._fd) is False
    lock.release()


def test_crashed_holder_lock_is_free(tmp_path):
    child = (
        "import os, sys\n"
        "from datetime import UTC, datetime\n"
        "from pathlib import Path\n"
        "from tempo.lockfile import Lockfile\n"
        "Lockfile(Path(sys.argv[1]), instance_id='dead', clock=lambda: datetime.now(UTC)).acquire()\n"
        "os._exit(9)\n"
    )
    rc = subprocess.run([sys.executable, "-c", child, str(tmp_path)], cwd=os.getcwd()).returncode
    assert rc == 9
    lock = make(tmp_path, instance_id="restart")
    lock.acquire()
    assert json.loads(lock.path.read_text())["instance_id"] == "restart"
    lock.release()


def test_double_acquire_refused(tmp_path):
    lock = make(tmp_path)
    lock.acquire()
    with pytest.raises(RuntimeError):
        lock.acquire()
    lock.release()


def test_release_when_not_held_refused(tmp_path):
    with pytest.raises(RuntimeError):
        make(tmp_path).release()


def test_acquire_creates_missing_state_dir(tmp_path):
    lock = make(tmp_path / "state")
    lock.acquire()
    assert lock.held
    lock.release()


def test_holder_none_when_identity_unreadable(tmp_path):
    # Contender racing the holder's identity write sees an empty file; refusal still stands.
    import fcntl

    tmp_path.joinpath("tempo.lock").write_text("")
    fd = os.open(tmp_path / "tempo.lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(LockHeld) as exc:
            make(tmp_path).acquire()
        assert exc.value.holder is None
    finally:
        os.close(fd)


def test_session_record_begin_check_end_and_stale_takeover(tmp_path):
    now = [T0]
    sessions = Sessions(tmp_path, clock=lambda: now[0], fs=LocalFileSystem())
    record = sessions.begin(token="t1", host="claude", chat="c1", number=1)
    assert sessions.read() == record and record["tickets"] == 0
    now[0] += timedelta(seconds=30)
    assert sessions.check("t1")["heartbeat"] == "2026-08-04T12:00:30+00:00"
    with pytest.raises(Refusal, match="token does not hold"):
        sessions.check("t2")
    now[0] += timedelta(seconds=LOCK_STALE_S)
    with pytest.raises(Refusal, match="held by the claude chat c1"):
        sessions.begin(token="t2", host="codex", chat="c2", number=2)
    now[0] += timedelta(seconds=1)
    assert sessions.begin(token="t2", host="codex", chat="c2", number=2)["chat"] == "c2"
    sessions.end("t2")
    assert sessions.read() is None


def test_a_waiting_acquire_blocks_until_the_holder_releases(tmp_path):
    holder = make(tmp_path)
    holder.acquire()
    child = subprocess.Popen([sys.executable, "-c", (
        "import sys; sys.path.insert(0, %r)\n"
        "from datetime import UTC, datetime\n"
        "from tempo.lockfile import Lockfile\n"
        "Lockfile(%r, instance_id='w', clock=lambda: datetime.now(UTC)).acquire(wait=True)\n"
        "print('got it')\n") % (os.getcwd(), str(tmp_path))], stdout=subprocess.PIPE, text=True)
    with pytest.raises(subprocess.TimeoutExpired):
        child.wait(timeout=0.3)
    holder.release()
    assert child.communicate(timeout=10)[0] == "got it\n"
