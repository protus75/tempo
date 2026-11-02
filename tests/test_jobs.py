"""Start-then-poll jobs (AGENT_PLAN.md section 4.1)."""

import os
import subprocess
import sys
from datetime import UTC, datetime

import pytest

from tempo.checkout import Refusal
from tempo.jobs import Jobs
from tempo.seams import LocalFileSystem, detached_spawn


def clock():
    return datetime(2026, 11, 2, tzinfo=UTC)


def jobs(tmp_path):
    ids = (f"job{n}" for n in range(1, 100))
    return Jobs(tmp_path, fs=LocalFileSystem(), clock=clock, new_id=lambda: next(ids))


def test_a_job_runs_while_its_worker_holds_the_lock_then_reports_done(tmp_path):
    j = jobs(tmp_path)
    held = []
    record = j.start("check", {"stem": "s"}, lambda id, fd: held.append(os.dup(fd)))
    assert record["state"] == "running" and record["args"] == {"stem": "s"}
    assert j.read(record["id"])["state"] == "running"  # the worker still holds the job lock
    j.finish(record["id"], {"outcome": "ok"})
    os.close(held.pop())
    assert j.read(record["id"])["state"] == "done" and j.read(record["id"])["result"] == {"outcome": "ok"}
    assert j.latest("check", stem="s")["id"] == record["id"] and j.latest("merge") is None


def test_a_job_whose_worker_died_reports_lost_as_infra_error(tmp_path):
    j = jobs(tmp_path)
    record = j.start("merge", {"stem": "s"}, lambda id, fd: None)  # the worker never started
    lost = j.read(record["id"])
    assert lost["state"] == "lost" and lost["result"]["outcome"] == "infra_error"
    assert j.active() == []


def test_one_job_at_a_time(tmp_path):
    j = jobs(tmp_path)
    held = []
    first = j.start("check", {"stem": "s"}, lambda id, fd: held.append(os.dup(fd)))
    with pytest.raises(Refusal, match=f"poll `job` with id {first['id']}"):
        j.start("merge", {"stem": "s"}, lambda id, fd: None)
    os.close(held.pop())


def test_unknown_job_and_kind(tmp_path):
    j = jobs(tmp_path)
    with pytest.raises(Refusal, match="no job nope"):
        j.read("nope")
    with pytest.raises(ValueError):
        j.start("build", {}, lambda id, fd: None)


def test_the_detached_worker_inherits_the_job_lock(tmp_path):
    """The real seam: the job stays running while the detached child lives, and is lost once it exits."""
    j = jobs(tmp_path)
    gate = tmp_path / "gate"
    script = f"import pathlib, time\nwhile not pathlib.Path({str(gate)!r}).exists(): time.sleep(0.01)\n"
    record = j.start("check", {}, lambda id, fd: detached_spawn([sys.executable, "-c", script], cwd=tmp_path,
                                                                env=dict(os.environ), keep_fd=fd))
    assert j.read(record["id"])["state"] == "running"
    gate.write_text("go")
    for _ in range(500):
        if j.read(record["id"])["state"] == "lost":
            break
        subprocess.run(["sleep", "0.01"])
    assert j.read(record["id"])["state"] == "lost"
