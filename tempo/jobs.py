"""Start-then-poll jobs (AGENT_PLAN.md section 4.1).

`check`, `merge`, and a call's `submit` validation run in a detached worker process, with their state in
`<state>/jobs/<id>.json`. The starting tool takes the flock on `<id>.lock` and hands that descriptor to the
worker, so the lock is held from the moment the job exists until its worker exits. A job not yet done
whose lock is free has lost its worker: it reports `lost`, recorded as an uncharged `infra_error`, and
effect replay decides whether a new job re-runs the work.
"""

import fcntl
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tempo.checkout import Refusal
from tempo.journal import render_ts
from tempo.seams import Clock, FileSystem

JOBS_DIR = "jobs"
FINISHED = frozenset({"done", "lost"})
KINDS = ("check", "merge", "submit")


class Jobs:
    def __init__(self, state_dir: Path, *, fs: FileSystem, clock: Clock, new_id: Callable[[], str]) -> None:
        self.root = Path(state_dir) / JOBS_DIR
        self.fs, self.clock, self.new_id = fs, clock, new_id

    def _write(self, record: dict[str, Any]) -> None:
        self.fs.write(self.root / f"{record['id']}.json", (json.dumps(record, indent=2) + "\n").encode())

    def start(self, kind: str, args: dict[str, Any], launch: Callable[[str, int], None]) -> dict[str, Any]:
        """Record the job and launch its worker with the job lock's descriptor; one job at a time."""
        if kind not in KINDS:
            raise ValueError(f"unknown job kind {kind!r}")
        if running := self.active():
            raise Refusal(f"job {running[0]['id']} ({running[0]['kind']}) is still running",
                          f"poll `job` with id {running[0]['id']} until it is done")
        id = self.new_id()
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.root / f"{id}.lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._write({"id": id, "kind": kind, "args": args, "state": "running",
                         "started_at": render_ts(self.clock()), "result": None})
            launch(id, fd)
        finally:
            os.close(fd)
        return self.read(id)

    def finish(self, id: str, result: dict[str, Any]) -> None:
        record = json.loads((self.root / f"{id}.json").read_text())
        self._write({**record, "state": "done", "finished_at": render_ts(self.clock()), "result": result})

    def read(self, id: str) -> dict[str, Any]:
        path = self.root / f"{id}.json"
        if not path.is_file():
            raise Refusal(f"no job {id}", "pass a job id that check, merge, or submit returned")
        record = json.loads(path.read_text())
        if record["state"] not in FINISHED and self._free(id):
            record = {**record, "state": "lost", "finished_at": render_ts(self.clock()), "result": {
                "outcome": "infra_error", "findings": [{
                    "code": "job_lost", "message": f"the {record['kind']} worker exited without a result",
                    "paved_road": f"run {record['kind']} again; completed effects replay"}]}}
            self._write(record)
        return record

    def _free(self, id: str) -> bool:
        try:
            fd = os.open(self.root / f"{id}.lock", os.O_RDWR)
        except FileNotFoundError:
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        finally:
            os.close(fd)
        return True

    def records(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        ids = (p.stem for p in self.root.glob("*.json"))
        return sorted((self.read(id) for id in ids), key=lambda r: (r["started_at"], r["id"]))

    def active(self) -> list[dict[str, Any]]:
        return [r for r in self.records() if r["state"] not in FINISHED]

    def latest(self, kind: str, **args: Any) -> dict[str, Any] | None:
        matches = [r for r in self.records()
                   if r["kind"] == kind and all(r["args"].get(k) == v for k, v in args.items())]
        return matches[-1] if matches else None
