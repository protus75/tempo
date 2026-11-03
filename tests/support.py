"""Shared test helpers: fake clocks, temp checkouts, and committed tickets, carried from the scripted layer's
tests (test_drain, test_terminal, test_scheduler, test_retro_box) when P5a deleted them."""

import ast
import secrets
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tempo.checkout import SPECS_DIR, Checkout
from tempo.driver import Driver
from tempo.effects import Effects
from tempo.git import Git
from tempo.journal import Journal
from tempo.seams import LocalFileSystem, SubprocessExec
from tempo.stages import StageContext
from tests.test_cli import CONFIG, ENV, TICKET as CLI_TICKET, git_out, ticket, write
from tests.test_stages import CONFIG as STAGES_CONFIG, STEM, TICKET, adopt_conduct, git

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


def checkout_at(root: Path) -> Checkout:
    from tempo.config import load_config
    process = SubprocessExec()
    return Checkout(root, load_config(None, cwd=root), ENV, process,
                    Git(process, env=ENV, timeout=30), journal(root), LocalFileSystem(), Clock())


# --- carried from test_terminal.py, test_scheduler.py, and test_retro_box.py with the scripted layer ---------


def clock() -> datetime:
    return datetime(2026, 10, 5, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tempo").mkdir(parents=True)
    (root / "tempo" / "thing.py").write_text("")
    (root / "config.yaml").write_text(STAGES_CONFIG)
    (root / ".gitignore").write_text(".tempo/\n")
    adopt_conduct(root)
    git(root, "init", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    return root


def author(repo: Path, stem: str = STEM, depends: str = "none") -> None:
    """Hand-author the ticket file only: intake commits it."""
    path = repo / "tickets" / stem / "ticket.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TICKET.format(bypass="").replace("## Depends on\nnone", f"## Depends on\n{depends}"))


class Time:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=UTC)
        self.waits = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        self.waits.append((self.now + timedelta(seconds=seconds), future))
        await future

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        for deadline, future in self.waits:
            if deadline <= self.now and not future.done():
                future.set_result(None)


async def turn():
    # Yield through callbacks without a wall-clock sleep or a timer.
    future = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(future.set_result, None)
    await future


def text(priority="P2", state="confirmed", depends="none"):
    return CLI_TICKET.format(depends=depends).replace(
        "priority: P2", f"state: {state}\nsource: human\npriority: {priority}", 1)


def import_closure(sources):
    pending, reached = ["tempo.__main__"], set()
    while pending:
        module = pending.pop()
        if module in reached or module not in sources:
            continue
        reached.add(module)
        if module != "tempo":
            pending.append("tempo")  # Package initialization is also reachable.
        for node in ast.walk(ast.parse(sources[module])):
            if isinstance(node, ast.Import):
                pending.extend(a.name for a in node.names if a.name.split(".")[0] == "tempo")
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    package = module.split(".")[:-node.level]
                    base = ".".join([*package, *([base] if base else [])])
                if base == "tempo" or base.startswith("tempo."):
                    pending.append(base)
                    pending.extend(f"{base}.{a.name}" for a in node.names)
    return reached




PROPOSAL = {"fixed_failure": "Observed café failure", "overcorrection_risk": "Rejecting useful inputs",
            "proposed_spec_paths": ["tempo/specs/review.md", "tempo/specs/author.md"]}


def stage_context(checkout: Checkout) -> StageContext:
    """The stage composition the tools build, on this checkout's journal."""
    driver = Driver.from_config(checkout.config, clock=checkout.clock, fs=checkout.fs)
    driver.journal = checkout.journal
    driver.effects = Effects(checkout.journal)
    return StageContext(repo=checkout.repo, config=checkout.config, env=checkout.env, exec_=checkout.exec_,
                        git=checkout.git, fs=checkout.fs, driver=driver, specs_dir=SPECS_DIR)


# --- the box flow, as the orchestrator runs it through the tools (AGENT_PLAN.md section 4.1, P5b) -----------


class Script:
    """Scripted subagent replies in call order; records each call's surface and whole prompt."""

    def __init__(self, replies) -> None:
        self.replies = list(replies)
        self.requests: list[SimpleNamespace] = []


def render_root(root: Path) -> None:
    """Write and commit the host files the tools and hooks need (`tempo render`)."""
    from tempo import render
    from tempo.config import load_config

    render.write(root, load_config(None, cwd=root), LocalFileSystem())
    git_out(root, "add", "-A")
    git_out(root, "commit", "-m", "render")


def call(host, args: dict, script: Script) -> dict:
    """prepare, spawn the scripted subagent, and submit until the stage is final or nothing is left to spawn."""
    while True:
        prepared = host.tool("prepare", args)
        if prepared["state"] != "pending":
            return prepared
        parts = [line.removeprefix("part: ") for line in prepared["spawn_prompt"].splitlines()
                 if line.startswith("part: ")]
        request = SimpleNamespace(surface=args["surface"], agent=prepared["agent"],
                                  rendered="".join(Path(p).read_text() for p in parts))
        script.requests.append(request)
        reply = script.replies.pop(0)
        host.subagent(prepared, reply(request) if callable(reply) else reply)
        result = host.until_done(host.tool("submit", {"call": prepared["call"]}), "submit",
                                 {"call": prepared["call"]})
        if result.get("final"):
            return result


def box_message(host, id: str, script: Script) -> str:
    """One pending message through triage and, when it says so, author and the candidate's review."""
    surface = "triage"
    while True:
        result = call(host, {"surface": surface, "message": id}, script)
        if result.get("next") is None:
            return result.get("line") or f"{id}: {result.get('outcome')}"
        surface = result["next"]


def triage(root: Path, script: Script) -> int:
    """One session's box pass: every pending message, then the session ends without a handoff commit."""
    from tempo.lockfile import Sessions
    from tests.hostfakes import FakeHost

    host = FakeHost("claude", root)
    host.ids = (secrets.token_hex(16) for _ in iter(int, 1))  # unique across this root's sessions
    host.tool("begin_session")
    try:
        lines = [box_message(host, m["id"], script) for m in host.tool("read_box")["pending"]]
    finally:
        Sessions(root / ".tempo" / "state", clock=host.clock, fs=LocalFileSystem()).end(host.token)
    print("\n".join(lines) if lines else "no pending suggestions")
    return 0
