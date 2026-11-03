"""Shared test helpers: a fake clock, a temp checkout, and committed tickets (carried from test_drain.py)."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from tempo.__main__ import build_control
from tempo.checkout import Checkout
from tempo.git import Git
from tempo.journal import Journal
from tempo.seams import LocalFileSystem, SubprocessExec
from tests.test_cli import CONFIG, ENV, git_out, ticket, write
from tests.test_stages import adopt_conduct

START = datetime(2026, 1, 1, tzinfo=UTC)


class Clock:
    """A fake clock a test (or a scripted stage) can advance."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


def confirmed(depends: str = "none", priority: str = "P2") -> str:
    """A ticket as the ticket-plane lane commits it: frontmatter already stamped, no intake needed."""
    return ticket(depends).replace("---\npriority: P2", f"---\nstate: confirmed\nsource: human\npriority: {priority}",
                                   1)


def commit_ticket(root: Path, stem: str, text: str) -> None:
    """A ticket-plane commit straight to main (a seeding ticket's output): no intake, no authoring event."""
    write(root, stem, text)
    git_out(root, "add", f"tickets/{stem}/ticket.md")
    git_out(root, "commit", "-m", f"tempo({stem}): ticket", "--only", "--", f"tickets/{stem}/ticket.md")


def make_root(tmp_path: Path, caps: str = "") -> Path:
    root = tmp_path / "repo"
    (root / "tempo").mkdir(parents=True)
    (root / "tempo" / "thing.py").write_text("")
    (root / "config.yaml").write_text(CONFIG + caps)
    (root / ".gitignore").write_text(".tempo/\n")
    adopt_conduct(root)
    git_out(root, "init", "-b", "main")
    git_out(root, "add", ".")
    git_out(root, "commit", "-m", "base")
    return root


def journal(root: Path) -> Journal:
    return Journal(root / ".tempo" / "state", Clock())


def pause_checkout(root):
    from tempo.config import load_config
    process = SubprocessExec()
    checkout = Checkout(root, load_config(None, cwd=root), ENV, process,
                    Git(process, env=ENV, timeout=30), journal(root), LocalFileSystem(), Clock())
    return replace(checkout, control=build_control(checkout))
