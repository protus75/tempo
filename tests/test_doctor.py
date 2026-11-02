"""Read-only operator setup inspection through production readers."""

import asyncio
import sys

import pytest

from tempo import doctor
from tempo.__main__ import main
from tempo.journal import EventType
from tempo.lockfile import Lockfile
from tempo.seams import ExecutableNotFound
from tests.test_cli import root, ENV, Clock, journal


class Exec:
    def __init__(self, failure=False):
        self.failure = failure
        self.calls = []

    async def run(self, argv, **kwargs):
        self.calls.append(argv)
        assert kwargs["env"] == {"PATH": ENV["PATH"]}
        if self.failure:
            raise ExecutableNotFound("git")
        return 0, "git version fixture\n", ""


def checks(root, *, venv=True, git_failure=False):
    executor = Exec(git_failure)
    results = asyncio.run(doctor.check(repo=root, config_path=None, env=ENV,
        exec_=executor, clock=Clock(), in_venv=venv))
    assert executor.calls == [["git", "-C", str(root), "--version"]]
    return results


def snapshot(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_doctor_check_order(root, monkeypatch):
    order = []
    run, load, lock, read = Exec.run, doctor.load_config, doctor.inspect_lock, doctor.Journal.read
    async def git_check(self, *args, **kwargs):
        order.append("git")
        return await run(self, *args, **kwargs)
    def config_check(*args, **kwargs):
        order.append("config")
        return load(*args, **kwargs)
    def lock_check(*args):
        order.append("lock")
        return lock(*args)
    def journal_check(self):
        order.append("journal")
        return read(self)
    monkeypatch.setattr(Exec, "run", git_check)
    monkeypatch.setattr(doctor, "load_config", config_check)
    monkeypatch.setattr(doctor, "inspect_lock", lock_check)
    monkeypatch.setattr(doctor.Journal, "read", journal_check)
    results = checks(root, venv=False)
    assert order == ["git", "config", "lock", "journal"]
    assert [r.name for r in results] == ["venv", "git", "config", "lock", "journal"]
    assert [r.passed for r in results] == [False, True, True, True, True]
    assert doctor.render(results) == (
        "doctor: failed\n"
        "FAIL venv: no active Python venv; run `uv run python -m tempo doctor`\n"
        "PASS git: git version fixture\n"
        "PASS config: config schema is valid\n"
        "PASS lock: no writer lock exists\n"
        "PASS journal: readable history (0 events)\n")
    assert not (root / ".tempo").exists()


@pytest.mark.parametrize("failure", ["venv", "git", "config", "lock", "journal"])
def test_doctor_failures(root, capsys, monkeypatch, failure):
    import tempo.__main__ as module
    holder = Lockfile(root / ".tempo/state", instance_id="other", clock=Clock())
    if failure == "config":
        (root / "config.yaml").write_text("schema_version: 1\n")
    elif failure == "lock":
        holder.acquire()
    elif failure == "journal":
        j = journal(root)
        j.dir.mkdir(parents=True)
        (j.dir / "000001-20260101.jsonl").write_bytes(b"bad\n")
    executor = Exec(failure == "git")
    monkeypatch.setattr(module, "SubprocessExec", lambda: executor)
    if failure == "venv":
        monkeypatch.setattr(sys, "prefix", sys.base_prefix)
    try:
        assert main(["doctor"], cwd=root, env=ENV, clock=Clock()) == 2
        output = capsys.readouterr()
        assert output.err == "" and "Traceback" not in output.out
        lines = output.out.splitlines()
        assert len(lines) == 6 and lines[0] == "doctor: failed"
        assert [line.split()[1].rstrip(":") for line in lines[1:]] == ["venv", "git", "config", "lock", "journal"]
        assert lines[["venv", "git", "config", "lock", "journal"].index(failure) + 1].startswith("FAIL")
        if failure == "config":
            assert all("FAIL" in line and "repair the selected config" in line for line in lines[3:])
        elif failure == "lock":
            assert "wait for" in lines[4] and "never delete" in lines[4]
        elif failure == "journal":
            assert "backup" in lines[5]
    finally:
        if holder.held:
            holder.release()


@pytest.mark.parametrize("history", ["absent", "empty", "torn", "corrupt", "held", "idle"])
def test_doctor_is_read_only(root, monkeypatch, capsys, history):
    import tempo.__main__ as module
    from tempo import runner
    def forbidden(*args, **kwargs):
        pytest.fail("doctor invoked a writer or model")
    monkeypatch.setattr(runner, "bind", forbidden)
    monkeypatch.setattr(module, "build_control", forbidden)
    holder = Lockfile(root / ".tempo/state", instance_id="original", clock=Clock())
    if history in {"held", "idle"}:
        holder.acquire()
        if history == "idle":
            holder.release()
    elif history != "absent":
        j = journal(root)
        if history == "torn":
            j.append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket="one")
            path = next(j.dir.glob("*.jsonl"))
            path.write_bytes(path.read_bytes() + b'{"unfinished":')
        else:
            j.dir.mkdir(parents=True)
            (j.dir / "000001-20260101.jsonl").write_bytes(
                {"empty": b"", "corrupt": b"bad\n"}[history])
    before = snapshot(root)
    monkeypatch.setattr(Lockfile, "acquire", forbidden)
    try:
        assert main(["doctor"], cwd=root, env=ENV, clock=Clock(), pipeline=forbidden) == (
            2 if history in {"held", "corrupt"} else 0)
        assert snapshot(root) == before
        if history == "absent":
            assert not (root / ".tempo").exists()
    finally:
        if holder.held:
            holder.release()
