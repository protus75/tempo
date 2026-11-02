"""Read-only mechanical setup checks."""

import fcntl
from dataclasses import dataclass

from tempo import render as host_render
from tempo.config import load_config
from tempo.git import Git
from tempo.journal import Journal
from tempo.lockfile import LockHeld, _read_holder


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


def inspect_lock(state_dir):
    """Probe an existing inode without creating it or publishing writer identity."""
    path = state_dir / "tempo.lock"
    try:
        stream = path.open("rb")
    except FileNotFoundError:
        return "no writer lock exists"
    with stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockHeld(path, _read_holder(stream.fileno())) from None
    return "writer lock is available"


async def check(*, repo, config_path, env, exec_, clock, in_venv):
    results = []
    def row(name, passed, detail):
        results.append(Check(name, passed, " ".join(str(detail).split())))
    row("venv", in_venv, "active Python venv" if in_venv else
        "no active Python venv; run `uv run python -m tempo doctor`")
    try:
        out = await Git(exec_, env={"PATH": env.get("PATH", "")}, timeout=30.0)._run(repo, "--version")
        row("git", True, out.strip())
    except Exception as exc:
        row("git", False, f"{exc}; install git or fix PATH")
    config = None
    try:
        config = load_config(config_path, cwd=repo)
        row("config", True, "config schema is valid")
    except Exception as exc:
        row("config", False, f"{exc}; repair the selected config and rerun doctor")
    if config is None:
        row("render", False, "config unavailable; repair the selected config and rerun doctor")
    else:
        drift = host_render.drift(repo, config)
        row("render", not drift, f"stale generated host files: {', '.join(drift)}; run `tempo render`"
            if drift else "generated host files are current")
    for name in ("lock", "journal"):
        if config is None:
            row(name, False, "state directory unavailable; repair the selected config and rerun doctor")
            continue
        try:
            detail = (inspect_lock(config.state_dir) if name == "lock" else
                      f"readable history ({len(Journal(config.state_dir, clock).read())} events)")
            row(name, True, detail)
        except Exception as exc:
            row(name, False, f"{exc}; " + ("restore journal readability from backup and rerun doctor"
                if name == "journal" else "wait for the writer to exit or stop it; never delete the lockfile"))
    return results


def render(results):
    return "\n".join(["doctor: ok" if all(r.passed for r in results) else "doctor: failed",
        *(f"{'PASS' if r.passed else 'FAIL'} {r.name}: {r.detail}" for r in results)]) + "\n"
