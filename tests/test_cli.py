import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.test_stages import adopt_conduct
from tempo.__main__ import main
from tempo.git import Git
from tempo.journal import EventType, Journal
from tempo.lockfile import Lockfile, LockHeld
from tempo.checkout import Refusal
from tempo.seams import SubprocessExec

ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/nonexistent",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}

CONFIG = """schema_version: 1
state_dir: .tempo/state
review: {}
merge: {}
hosts:
  claude:
    models_by_tier: {low: sonnet, medium: sonnet, high: sonnet, max: sonnet}
    pins: {review: opus}
orchestrator:
  models: {claude: opus}
engine_plane_safety_inventory: [config.yaml]
"""


TICKET = """---
priority: P2
kind: feature
---

## Depends on
{depends}

## Context
- tempo/thing.py

## Goal / Why
`thing()` returns ok.

## Scope in / Scope out
In: thing. Out: the rest.

## Scope fence
- tempo/thing.py

## Acceptance criteria
1. `uv run pytest` exits 0.

## Verification
```
uv run pytest
```

## Definition of rejected
Needs a new dependency.

## Time budget
- stuck: 20m
"""


def ticket(depends: str = "none") -> str:
    return TICKET.format(depends=depends)


class Clock:
    def __call__(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=UTC)


def with_retro_driver(checkout, dispatch):
    from tempo import runner
    from tempo.llm import FakeLLM
    dispatch.stages = runner.bind(checkout, FakeLLM(['{"proposals": []}'] * 10)).stages
    return dispatch


class Stages:
    """Scripted fake for the stage-dispatch seam: records each dispatched ticket and the lock state."""

    def __init__(self, terminal: str = "merged") -> None:
        self.terminal = terminal
        self.calls: list[str] = []
        self.lock_held: list[bool] = []
        self.checkout = None

    def __call__(self, checkout):
        self.checkout = checkout

        async def dispatch(t):
            self.calls.append(t.stem)
            contender = Lockfile(checkout.config.state_dir, instance_id="contender", clock=Clock())
            try:
                contender.acquire()
            except LockHeld:
                self.lock_held.append(True)
            else:
                contender.release()
                self.lock_held.append(False)
            checkout.journal.append(EventType.STATE_TRANSITION, {"to": self.terminal}, ticket=t.stem)
            return self.terminal

        return with_retro_driver(checkout, dispatch)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tempo").mkdir(parents=True)
    (root / "tempo" / "thing.py").write_text("")
    (root / "config.yaml").write_text(CONFIG)
    (root / ".gitignore").write_text(".tempo/\n")
    conduct = adopt_conduct(root)
    g = Git(SubprocessExec(), env=ENV, timeout=30.0)

    async def seed():
        await g.init(root, branch="main")
        await g.add(root, ["tempo", "config.yaml", ".gitignore", *conduct])
        await g.commit(root, "base")

    asyncio.run(seed())
    return root


def cli(root: Path, *argv: str, stages=None) -> int:
    return main(list(argv), cwd=root, env=ENV, clock=Clock(), pipeline=stages or Stages())


def journal(root: Path) -> Journal:
    return Journal(root / ".tempo" / "state", Clock())


def write(root: Path, stem: str, text: str) -> None:
    path = root / "tickets" / stem / "ticket.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def git_out(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], env=ENV, capture_output=True, text=True,
                          check=True).stdout


# --- new ----------------------------------------------------------------------------------------


def test_new_templates_a_ticket_and_lints_it(root, capsys):
    assert cli(root, "new", "add-thing") == 0
    path = root / "tickets" / "add-thing" / "ticket.md"
    text = path.read_text()
    assert "## Acceptance criteria" in text and "## Verification" in text
    out = capsys.readouterr().out
    assert "tickets/add-thing/ticket.md" in out
    assert "`## Acceptance criteria`" in out  # synchronous lint names what is left to fill
    assert git_out(root, "status", "--porcelain", "--", "tickets").startswith("??")  # authored, not committed
    assert not (root / ".tempo").exists()  # file authorship is outside the lock fence


def test_new_refuses_an_existing_stem(root, capsys):
    write(root, "add-thing", "keep me\n")
    assert cli(root, "new", "add-thing") == 2
    assert (root / "tickets" / "add-thing" / "ticket.md").read_text() == "keep me\n"
    assert "already exists" in capsys.readouterr().err


def test_new_refuses_a_bad_stem(root, capsys):
    assert cli(root, "new", "Bad_Stem") == 2
    assert not (root / "tickets").exists()
    assert "kebab-case" in capsys.readouterr().err


# --- status -------------------------------------------------------------------------------------


def test_status_on_an_empty_journal(root, capsys):
    assert cli(root, "status") == 0
    out = capsys.readouterr().out
    for heading in ("merged", "in flight", "blocked", "intake"):
        assert heading in out
    assert "current-window scorecard:\n- boundary: initial\n- merged tickets: 0\n" in out
    assert "window provenance:" in out and "## Surface scorecard" in out
    assert not (root / ".tempo").exists()  # write-free: no journal, no lockfile


def test_status_projects_the_journal(root, capsys):
    j = journal(root)
    j.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket="a")
    j.append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket="a")
    j.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket="b")
    j.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket="c")
    j.append(EventType.STATE_TRANSITION, {"to": "gate_failed"}, ticket="c")
    j.append(EventType.SIGNAL, {"signal": "ticket_intake", "source": "human", "state": "confirmed", "new": True,
                                "commit": "abc123"}, ticket="d")
    before = sorted(p.read_bytes() for p in (root / ".tempo").rglob("*") if p.is_file())

    assert cli(root, "status") == 0
    out = capsys.readouterr().out
    section = dict(block.split("\n", 1) for block in out.strip().split("\n\n"))
    assert "a" in section["merged:"] and "b" not in section["merged:"]
    assert "b" in section["in flight:"]
    assert "c: gate_failed" in section["blocked:"]
    assert "d: source: human" in section["intake:"]
    assert "merged tickets: 1" in section["current-window scorecard:"]
    assert sorted(p.read_bytes() for p in (root / ".tempo").rglob("*") if p.is_file()) == before


def test_status_is_deterministic(root, capsys):
    j = journal(root)
    for stem in ("z", "a", "m"):
        j.append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket=stem)
    cli(root, "status")
    first = capsys.readouterr().out
    cli(root, "status")
    assert capsys.readouterr().out == first
    assert first.index("- a") < first.index("- m") < first.index("- z")
    assert "- merged tickets: 3\n" in first


def test_status_escape_projection(tmp_path, monkeypatch, capsys):
    from tempo.__main__ import _status
    from tempo.retro import production_evidence, project_window, render_report, RetroArtifact
    from tempo.status import render
    from tests.test_triage import escape_rig, escape_report
    async def setup():
        c, _, _, _, admitted = await escape_rig(tmp_path)
        c, _, _ = await escape_report(c, commit=admitted.commit)
        status = await _status(c)
        window = project_window(c.journal.read())
        evidence = await production_evidence(window, c)
        return c, status, window, evidence
    c, status, window, evidence = asyncio.run(setup())
    before = c.journal.read()
    def forbidden(*args, **kwargs):
        pytest.fail("status escape rendering read or mutated state")
    with monkeypatch.context() as patch:
        for target, method in ((c.fs, "write"), (c.journal, "read"), (c.journal, "append"),
                               (c.git, "_run"), (Path, "read_text"), (Path, "read_bytes")):
            patch.setattr(target, method, forbidden)
        text = render(status)
        receipt = render_report(window, evidence, {}, [], RetroArtifact(proposals=[]))
        assert text.split("| Surface |")[1] == receipt.split("| Surface |")[1]
        assert "| review | 1 | 0 | 1 | 0 | 0.0000 | 1.0000 | false |" in text
    assert c.journal.read() == before
    assert main(["status"], cwd=c.repo, env=ENV, clock=c.clock) == 0
    output = capsys.readouterr().out
    assert output.split("| Surface |")[1] == text.split("| Surface |")[1]
    assert c.journal.read() == before


# --- run ----------------------------------------------------------------------------------------


def test_run_dispatches_the_locked_validated_stem_to_the_stage_seam(root):
    write(root, "add-thing", ticket())
    stages = Stages()
    assert cli(root, "run", "add-thing", stages=stages) == 0
    assert stages.calls == ["add-thing"]
    assert stages.lock_held == [True]  # the one process holding the lock is the one dispatching
    assert git_out(root, "status", "--porcelain") == ""  # intake committed the ticket before dispatch
    assert "tempo(add-thing): ticket" in git_out(root, "log", "--format=%s")
    events = [(e.type, e.ticket, e.body.get("to")) for e in journal(root).read()]
    assert (EventType.STATE_TRANSITION, "add-thing", "running") in events
    assert events.index((EventType.STATE_TRANSITION, "add-thing", "running")) < events.index(
        (EventType.STATE_TRANSITION, "add-thing", "merged"))
    Lockfile(root / ".tempo" / "state", instance_id="after", clock=Clock()).acquire()  # released on exit


def test_run_exits_1_on_a_non_ok_terminal(root):
    write(root, "add-thing", ticket())
    assert cli(root, "run", "add-thing", stages=Stages("gate_failed")) == 1
    assert (root / "tickets" / "add-thing" / "ticket.md").is_file()


def test_run_refused_when_the_lockfile_is_already_held(root, capsys):
    write(root, "add-thing", ticket())
    holder = Lockfile(root / ".tempo" / "state", instance_id="other", clock=Clock())
    holder.acquire()
    try:
        stages = Stages()
        assert cli(root, "run", "add-thing", stages=stages) == 2
    finally:
        holder.release()
    assert stages.calls == []
    assert "other" in capsys.readouterr().err  # the holder identity names who to wait for
    assert git_out(root, "status", "--porcelain").startswith("??")  # intake never ran: nothing committed
    assert journal(root).read() == []


def test_run_refuses_an_invalid_ticket(root, capsys):
    write(root, "add-thing", ticket().replace("## Verification", "## Verify"))
    stages = Stages()
    assert cli(root, "run", "add-thing", stages=stages) == 2
    assert stages.calls == []
    assert "ticket_schema" in capsys.readouterr().err
    assert not any(e.type == EventType.STATE_TRANSITION for e in journal(root).read())


def test_run_refuses_a_missing_stem(root, capsys):
    stages = Stages()
    assert cli(root, "run", "no-such", stages=stages) == 2
    assert stages.calls == []
    assert "new no-such" in capsys.readouterr().err


def test_run_refuses_an_unmerged_dependency(root, capsys):
    write(root, "first", ticket())
    write(root, "second", ticket(depends="- first"))
    stages = Stages()
    assert cli(root, "run", "second", stages=stages) == 2
    assert stages.calls == []
    assert "run first" in capsys.readouterr().err
    assert cli(root, "run", "first", stages=stages) == 0
    assert cli(root, "run", "second", stages=stages) == 0
    assert stages.calls == ["first", "second"]


def test_run_refuses_a_merged_ticket(root, capsys):
    write(root, "add-thing", ticket())
    stages = Stages()
    assert cli(root, "run", "add-thing", stages=stages) == 0
    assert cli(root, "run", "add-thing", stages=stages) == 2
    assert stages.calls == ["add-thing"]
    assert "merged" in capsys.readouterr().err


def test_run_refused_before_the_lock_when_the_pipeline_refuses(root, capsys):
    def unbuilt(checkout):
        raise Refusal("pipeline missing", "build it")

    write(root, "add-thing", ticket())
    assert main(["run", "add-thing"], cwd=root, env=ENV, clock=Clock(), pipeline=unbuilt) == 2
    assert "pipeline missing -- build it" in capsys.readouterr().err
    assert journal(root).read() == []


def test_config_error_exits_2(root, capsys):
    (root / "config.yaml").write_text("schema_version: 1\n")
    assert cli(root, "status") == 2
    assert "config.yaml" in capsys.readouterr().err


# --- module entry -------------------------------------------------------------------------------


def test_python_dash_m_tempo_is_the_entry(root):
    env = {**ENV, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    proc = subprocess.run([sys.executable, "-m", "tempo", "status"], cwd=root, env=env, capture_output=True,
                          text=True)
    assert proc.returncode == 0, proc.stderr
    assert "merged:" in proc.stdout
    proc = subprocess.run([sys.executable, "-m", "tempo", "bogus"], cwd=root, env=env, capture_output=True,
                          text=True)
    assert proc.returncode == 2


def test_project_stays_virtual():
    import tomllib

    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert "scripts" not in pyproject["project"]
    assert "build-system" not in pyproject
    assert os.path.isfile(Path(__file__).resolve().parents[1] / "tempo" / "__main__.py")


def released(root):
    lock = Lockfile(root / ".tempo/state", instance_id="after", clock=Clock())
    lock.acquire()
    lock.release()


@pytest.mark.parametrize("completed_report", [False, True])
def test_retro_no_merge(root, capsys, completed_report):
    if completed_report:
        journal(root).append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket="one")
        assert cli(root, "retro") == 0
        capsys.readouterr()
    def factory(c):
        class Dispatch:
            async def prepare(self):
                pytest.fail("unselected pipeline prepared")
            async def __call__(self, ticket):
                pytest.fail("ticket dispatched")
        return Dispatch()
    head = git_out(root, "rev-parse", "HEAD")
    assert cli(root, "retro", stages=factory) == 0
    assert capsys.readouterr().out == "retro: no merge since the latest completed report\n"
    assert git_out(root, "rev-parse", "HEAD") == head
    assert (root / "tickets/retro").exists() == completed_report
    released(root)


def test_retro_success(root, capsys, monkeypatch):
    from tempo import runner
    from tempo.llm import FakeLLM
    from tempo.retro import project_window
    import tempo.__main__ as module
    llm = FakeLLM(['{"proposals": []}'])

    class Held:
        kind = "cli"

        async def call(self, request):
            contender = Lockfile(root / ".tempo/state", instance_id="model", clock=Clock())
            with pytest.raises(LockHeld):
                contender.acquire()
            return await llm.call(request)

        def abort_current(self):
            pass

    journal(root).append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket="one")
    assert module.main(["retro"], cwd=root, env=ENV, clock=Clock(),
                       pipeline=lambda c: runner.bind(c, Held())) == 0
    assert capsys.readouterr().out == "retro: committed tickets/retro/000001.md\n"
    assert len(llm.requests) == 1
    assert project_window(journal(root).read()).boundary == "retro/1"
    completion = next(e for e in journal(root).read() if e.key == "retro/1" and e.type == EventType.EFFECT_COMPLETION)
    assert completion.body == {"result": {"commit": git_out(root, "rev-parse", "HEAD").strip()}}
    assert git_out(root, "show", "--format=", "--name-only", "HEAD").splitlines() == ["tickets/retro/000001.md"]
    released(root)


@pytest.mark.parametrize("suppressed, later_merge", [(False, False), (True, False), (True, True)])
def test_retro_no_commit(root, capsys, monkeypatch, suppressed, later_merge):
    from tempo import runner
    from tempo.llm import FakeLLM
    llm = FakeLLM([RuntimeError("failed")])
    j = journal(root)
    j.append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket="one")
    if suppressed:
        j.append(EventType.SIGNAL, {"kind": "retro_failed", "window_boundary": "initial"}, key="retro-failed/initial/forced")
    def factory(c):
        writer = runner.bind(c, llm)
        writer.stages.driver.retry_cap = 0
        return writer
    if later_merge:
        j.append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket="later")
    head = git_out(root, "rev-parse", "HEAD")
    assert cli(root, "retro", stages=factory) == 1
    assert git_out(root, "rev-parse", "HEAD") == head
    assert capsys.readouterr().out == "retro: no report committed; inspect status and the Suggestion Box\n"
    assert len(llm.requests) == (0 if suppressed and not later_merge else 1)
    assert not (root / "tickets/retro").exists()
    released(root)


@pytest.mark.parametrize("failure", ["lock", "config", "journal", "driver", "prepare"])
def test_retro_refusals(root, capsys, failure):
    j = journal(root)
    j.append(EventType.STATE_TRANSITION, {"to": "merged"}, ticket="one")
    holder = Lockfile(root / ".tempo/state", instance_id="other", clock=Clock())
    if failure == "lock":
        holder.acquire()
    elif failure == "config":
        (root / "config.yaml").write_text("schema_version: 1\n")
    elif failure == "journal":
        next(j.dir.glob("*.jsonl")).write_bytes(b"bad\n")
    def factory(c):
        class Dispatch:
            async def prepare(self):
                if failure == "prepare":
                    raise Refusal("preflight failed", "repair provider and retry")
            async def __call__(self, ticket):
                pytest.fail("dispatched")
        return Dispatch()
    try:
        assert cli(root, "retro", stages=factory) == 2
        err = capsys.readouterr().err
        assert "Traceback" not in err
        assert {"lock": "wait", "config": "repair", "journal": "never hand-edited",
                "driver": "construct the pipeline", "prepare": "repair provider"}[failure] in err
    finally:
        if holder.held:
            holder.release()
    released(root)


def test_doctor_cli(root, capsys):
    assert cli(root, "doctor", stages=lambda c: pytest.fail("pipeline constructed")) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "doctor: ok"
    assert [line.split(":", 1)[0] for line in lines[1:]] == [f"PASS {n}" for n in ("venv", "git", "config", "lock", "journal")]


def core_config(root, hosts):
    import yaml

    config = yaml.safe_load(CONFIG)
    entry = config["hosts"]["claude"]
    config["hosts"] = {host: entry for host in hosts}
    config["orchestrator"]["models"] = {host: "opus" for host in hosts}
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


@pytest.mark.parametrize("hosts, expected", [
    (["claude"], {"CLAUDE.md"}),
    (["codex"], {"AGENTS.md"}),
    (["claude", "codex"], {"CLAUDE.md", "AGENTS.md"}),
])
def test_core_host_conduct_files(tmp_path, monkeypatch, hosts, expected):
    import tempo.__main__ as module
    from tests.test_hostfiles import RecordingFS
    from tempo import hostfiles

    core_config(tmp_path, hosts)
    fs = RecordingFS()
    monkeypatch.setattr(module, "LocalFileSystem", lambda: fs)
    assert cli(tmp_path, "core") == 0
    assert {p.name for p, _ in fs.writes} == expected
    assert len(fs.writes) == len(expected)
    for name in ("CLAUDE.md", "AGENTS.md"):
        path = tmp_path / name
        assert path.exists() == (name in expected)
        if path.exists():
            assert path.read_bytes() == hostfiles.render(b"")
    assert cli(tmp_path, "core") == 0
    assert len(fs.writes) == len(expected)
    assert not (tmp_path / ".tempo").exists()


def test_core_refusal_and_no_git(tmp_path, monkeypatch, capsys):
    import tempo.__main__ as module
    from tempo import gates, hostfiles
    from tempo.seams import LocalFileSystem

    config = core_config(tmp_path, ["claude", "codex"])
    clean, corrupt = tmp_path / "CLAUDE.md", tmp_path / "AGENTS.md"
    project = b"# Project\r\nkeep every byte"
    clean.write_bytes(project)
    corrupt.write_bytes(b"project\n<!-- tempo:core beg")
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    def forbidden(*args, **kwargs):
        pytest.fail("core constructed git, journal, provider, pipeline, or gate work")
    for name in ("Git", "Journal", "SubprocessExec", "build_control"):
        monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(gates, "run_gates", forbidden)
    replacements = []
    original_replace = os.replace
    def observe_replace(src, dst):
        src, dst = Path(src), Path(dst)
        assert src.parent == dst.parent
        assert src.name == f".{dst.name}.tmp"
        assert src.read_bytes() == hostfiles.render(before[dst])
        assert dst.read_bytes() == before[dst]
        replacements.append(dst)
        original_replace(src, dst)
    monkeypatch.setattr(os, "replace", observe_replace)
    monkeypatch.setattr(module, "LocalFileSystem", LocalFileSystem)
    assert main(["--config", str(config), "core"], cwd=tmp_path, env=ENV, pipeline=forbidden) == 2
    assert replacements == []
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before
    error = capsys.readouterr().err
    assert "repair" in error and "retain all project content" in error and "retry core" in error
    # The paved road escapes marker-like project text without deleting its content.
    repaired = before[corrupt].replace(b"tempo:core", b"tempo&#58;core")
    corrupt.write_bytes(repaired)
    before[corrupt] = repaired
    assert main(["core"], cwd=tmp_path, env=ENV, pipeline=forbidden) == 0
    assert replacements == [clean, corrupt]
    assert clean.read_bytes() == hostfiles.render(project)
    assert corrupt.read_bytes() == hostfiles.render(repaired)
    assert main(["core"], cwd=tmp_path, env=ENV, pipeline=forbidden) == 0
    assert replacements == [clean, corrupt]
    assert {p.name for p in tmp_path.iterdir()} == {"config.yaml", "CLAUDE.md", "AGENTS.md"}


@pytest.mark.parametrize("explicit", [False, True])
def test_migrate_config_before_load(tmp_path, monkeypatch, capsys, explicit):
    import tempo.__main__ as module
    from tests.test_config import MigrationFS

    directory = tmp_path / "elsewhere" if explicit else tmp_path
    directory.mkdir(exist_ok=True)
    path = directory / ("host.yaml" if explicit else "config.yaml")
    original = CONFIG.replace("schema_version: 1", "schema_version: 0").encode()
    path.write_bytes(original)
    if explicit:
        (tmp_path / "config.yaml").write_bytes(b"unselected invalid config")
    fs = MigrationFS()
    monkeypatch.setattr(module, "LocalFileSystem", lambda: fs)
    def forbidden(*args, **kwargs):
        pytest.fail("migration invoked git, process, provider, journal, or pipeline work")
    for name in ("Git", "Journal", "SubprocessExec", "build_control"):
        monkeypatch.setattr(module, name, forbidden)
    args = (["--config", str(path)] if explicit else []) + ["migrate-config"]
    assert main(args, cwd=tmp_path, env=ENV, pipeline=forbidden) == 0
    backup = path.with_name(path.name + ".bak")
    expected = CONFIG.encode()
    assert fs.writes == [(backup, original), (path, expected)]
    assert path.read_bytes() == expected
    assert backup.read_bytes() == original
    assert main(args, cwd=tmp_path, env=ENV, pipeline=forbidden) == 0
    assert len(fs.writes) == 2
    assert path.read_bytes() == expected and backup.read_bytes() == original
    path.write_bytes(original + b"caps: {retry: 7}\n")
    before = path.read_bytes()
    assert main(args, cwd=tmp_path, env=ENV, pipeline=forbidden) == 2
    assert "caps.retry" in capsys.readouterr().err
    assert len(fs.writes) == 2
    assert path.read_bytes() == before and backup.read_bytes() == original
    assert not (tmp_path / ".tempo").exists()
    if explicit:
        assert (tmp_path / "config.yaml").read_bytes() == b"unselected invalid config"


def test_core_and_gate_share_host_paths(tmp_path, monkeypatch):
    from tempo import hostfiles, __main__ as module
    from tests.test_hostfiles import RecordingFS
    from tests.test_stages import evidence
    from tempo.config import load_config
    config_path = core_config(tmp_path, ['codex', 'claude'])
    config = load_config(config_path, cwd=tmp_path)
    resolver = hostfiles.conduct_paths
    calls = []
    def resolved(config, root):
        paths = resolver(config, root)
        calls.append(paths)
        return paths
    monkeypatch.setattr(hostfiles, 'conduct_paths', resolved)
    project = b'# project\r\nbytes\xff without newline'
    for path in resolver(config, tmp_path):
        path.write_bytes(project)
    fs = RecordingFS()
    monkeypatch.setattr(module, 'LocalFileSystem', lambda: fs)
    assert cli(tmp_path, 'core') == 0
    assert cli(tmp_path, 'core') == 0
    g = Git(SubprocessExec(), env=ENV, timeout=30)
    async def check():
        await g.init(tmp_path, branch='main')
        await g.add(tmp_path, ['config.yaml', 'CLAUDE.md', 'AGENTS.md'])
        await g.commit(tmp_path, 'mechanical adoption')
        gathered = await hostfiles.committed_core(tmp_path, config, g)
        assert hostfiles.CoreDriftGate().check(evidence(**gathered), tmp_path).verdict == 'pass'
    asyncio.run(check())
    assert calls == [resolver(config, tmp_path)] * 3
    assert len(fs.writes) == 2
    for path in resolver(config, tmp_path):
        assert hostfiles.parse(path.read_bytes()) == (b'', b'\n\n' + project)
    before = {p: p.read_bytes() for p in resolver(config, tmp_path)}
    damaged = tmp_path / 'CLAUDE.md'
    damaged.write_bytes(before[damaged] + b'stray tempo:core')
    corrupt = {p: p.read_bytes() for p in before}
    assert cli(tmp_path, 'core') == 2
    assert {p: p.read_bytes() for p in before} == corrupt and len(fs.writes) == 2
    async def refusal():
        await g.add(tmp_path, ['CLAUDE.md'])
        await g.commit(tmp_path, 'committed marker refusal')
        gathered = await hostfiles.committed_core(tmp_path, config, g)
        report = hostfiles.CoreDriftGate().check(evidence(**gathered), tmp_path)
        [finding] = report.findings
        assert finding.path == 'CLAUDE.md' and 'retain all project content' in finding.paved_road
    asyncio.run(refusal())
