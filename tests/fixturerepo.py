"""The minimal fixture repo (AGENT_PLAN.md D5, P4 step e).

P4 tests run tempo in a copy of `fixture/`, and the P4c live gate opens one in VS Code:
`uv run python -m tests.fixturerepo <dest>`. Init writes the host files there, never into this repo.
"""

import subprocess
import sys
from pathlib import Path

from tempo import render
from tempo.seams import LocalFileSystem

FIXTURE = Path(__file__).resolve().parent.parent / "fixture"
STEM = "add-subtract"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def materialize(dest: Path) -> Path:
    """Copy the fixture to `dest`, commit it on main, then run `tempo init` and commit what it wrote."""
    dest.mkdir(parents=True)
    for src in FIXTURE.rglob("*"):
        if src.is_file():
            (dest / src.relative_to(FIXTURE)).parent.mkdir(parents=True, exist_ok=True)
            (dest / src.relative_to(FIXTURE)).write_bytes(src.read_bytes())
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "config", "user.email", "fixture@tempo")
    _git(dest, "config", "user.name", "fixture")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "fixture")
    render.init(dest, None, LocalFileSystem())
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "tempo init")
    return dest


if __name__ == "__main__":
    print(materialize(Path(sys.argv[1]).resolve()))
