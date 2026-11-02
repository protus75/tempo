"""Executable host documentation; pending consumers are checked against plan bytes."""

import asyncio
import copy
import inspect
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest
import yaml
from pydantic import BaseModel

from tempo import config, gates, hostfiles, providers, seams, tickets
from tempo.lockfile import Lockfile, LockHeld
from tempo.mergequeue import MergeQueue
from tempo.specs import (
    ENTRY_PARTS, PlanContractError, entry_unit_gap, required_units, resolve_plan_contract,
)

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs/host-contract.md").read_text()
PLAN = (ROOT / "TEMPO_PLAN.md").read_text()


def block(name):
    matches = re.findall(rf"^```yaml {name}\n(.*?)^```$", DOC, re.M | re.S)
    assert len(matches) == 1
    return matches[0]


def mentions(*facts):
    prose = " ".join(DOC.split())
    for fact in facts:
        assert fact in prose, fact


def load(tmp_path, raw=None):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw) if raw is not None else block("host-example"))
    return config.load_config(path, cwd=tmp_path)


def inventory(model, row):
    fields = model.model_fields
    assert set(row["required"]) == {k for k, f in fields.items() if f.is_required()}
    assert set(row["required"]) | set(row["defaults"]) == set(fields)
    for key, expected in row["defaults"].items():
        actual = fields[key].get_default(call_default_factory=True)
        if isinstance(actual, BaseModel):
            actual = actual.model_dump()
        assert actual == (None if expected == "unset" else expected), (model, key)


def test_host_contract_entry_unit():
    uid = "19.P6.host-contract-doc"
    ticket = tickets.parse_ticket("host-contract-doc",
        (ROOT / "tickets/host-contract-doc/ticket.md").read_text(), ROOT, plan=PLAN)
    assert required_units(PLAN, ticket.stem, ticket.plan_contract) == (uid,)
    assert required_units(PLAN, "continuation", (uid,)) == (uid,)
    resolved = resolve_plan_contract(PLAN, (*ticket.plan_contract, uid))
    assert resolved.count(f"### {uid} ") == 1
    assert ENTRY_PARTS == ("Owner", "Records", "Observable", "Tests")
    assert entry_unit_gap(PLAN, uid) is None
    for part in ENTRY_PARTS:
        for replacement in ("", f"- **{part}:**"):
            broken = re.sub(rf"^- \*\*{part}:\*\*.*$", replacement, resolved, flags=re.M)
            assert part in entry_unit_gap(broken, uid)
    for broken in ("", resolved + resolved):
        with pytest.raises(PlanContractError):
            resolve_plan_contract(broken, (uid,))


def test_host_contract_example_loads(tmp_path):
    cfg = load(tmp_path)
    assert cfg.schema_version == 1
    assert cfg.state_dir == tmp_path / ".tempo"
    assert cfg.worktree_root == cfg.state_dir / "worktrees"
    assert cfg.providers[0].package == "@openai/codex"
    assert cfg.review.mechanical[0].argv == ["uv", "run", "pytest", "-q"]
    assert cfg.review.mechanical[1].trigger == ["src/"]
    assert cfg.review.surfaces[0].rules_doc == Path("docs/review.md")
    assert cfg.review.trigger_map == {"src/": ["scope_fence"]}
    assert cfg.review.gate_severity == {"scope_fence": "hard"}
    assert cfg.merge.safety_checks == ["host-test"]
    assert [s.strategy for s in cfg.merge.strategies] == ["regenerate", "union"]
    assert cfg.merge.strategies[0].argv == ["uv", "lock"]
    assert not hasattr(cfg.merge.strategies[1], "argv")
    for name in ("mechanical", "surfaces", "trigger_map", "gate_severity", "safety_checks", "strategies"):
        assert re.search(rf"^  {name}:.*#", block("host-example"), re.M)


def test_host_contract_schema_coverage(tmp_path):
    rows = yaml.safe_load(block("config-fields"))
    owners = {name: cls for name, cls in vars(config).items()
              if isinstance(cls, type) and issubclass(cls, config._Strict) and cls is not config._Strict}
    assert set(rows) == set(owners)
    for name, cls in owners.items():
        inventory(cls, rows[name])
    values = yaml.safe_load(block("config-values"))
    for name, annotation in (("tier", config.Tier), ("severity", config.Severity),
                             ("start_state", config.StartState), ("box_row", config.BoxRow)):
        assert values[name] == list(get_args(annotation))
    assert values["kind"] == list(get_args(config.Provider.model_fields["kind"].annotation))
    assert values["strategy"] == ["regenerate", "union"]
    assert values["trigger"] == ["always", "path-prefix-list"]
    raw = yaml.safe_load(block("host-example"))
    for key in config.Config.model_fields:
        candidate = copy.deepcopy(raw)
        candidate[key] = None
        with pytest.raises(config.ConfigError, match="integer" if key == "schema_version" else "null") as exc:
            load(tmp_path, candidate)
        assert exc.value.key == key
    for key in rows["Config"]["required"]:
        candidate = copy.deepcopy(raw)
        del candidate[key]
        with pytest.raises(config.ConfigError, match="missing") as exc:
            load(tmp_path, candidate)
        assert exc.value.key == key
    for key, value in (("routing_default_tier", "unknown"), ("unknown", 1)):
        with pytest.raises(config.ConfigError):
            load(tmp_path, {**raw, key: value})
    for change in (lambda r: r["providers"][0].pop("package"),
                   lambda r: r["providers"][0].update(kind="api"),
                   lambda r: r["merge"]["strategies"][1].update(argv=["x"]),
                   lambda r: r["review"]["mechanical"][0].update(severity="warn")):
        candidate = copy.deepcopy(raw)
        change(candidate)
        with pytest.raises(config.ConfigError):
            load(tmp_path, candidate)
    cfg = load(tmp_path, {**raw, "worktree_root": "work", "report_inbox": "inbox",
                          "context_files": ["docs/a.md"], "box_policy": {"failure_report": "draft"}})
    assert cfg.worktree_root == tmp_path / "work" and cfg.report_inbox == tmp_path / "inbox"
    assert cfg.context_files == [Path("docs/a.md")]
    assert cfg.box_policy == {**config.BOX_POLICY_DEFAULTS, "failure_report": "draft"}
    assert cfg.providers[0].auth is None and cfg.providers[0].limits.quota_window_minutes == 60
    assert cfg.notify is None
    assert providers.resolve(cfg, "medium", "review").model == cfg.providers[0].models_by_tier.medium
    assert load(tmp_path, {**raw, "state_dir": str(tmp_path / "absolute")}).state_dir == tmp_path / "absolute"
    mentions("Every explicit null", "primary-until-pressure", "est_cost_per_call_usd",
             "Relative `state_dir`", "remain Path values as written", "use `cli`")


def test_host_contract_migration_and_managed_ownership(tmp_path):
    class FS:
        writes = []

        def write(self, path, data):
            self.writes.append((path, data))
            seams.LocalFileSystem().write(path, data)

    fs = FS()
    path = tmp_path / "config.yaml"
    original = ("# original café\r\n" + block("host-example").replace("schema_version: 1", "schema_version: 0")).encode()
    path.write_bytes(original)
    assert config.migrate_config(None, cwd=tmp_path, fs=fs)
    assert fs.writes == [(path.with_name("config.yaml.bak"), original),
                         (path, original.replace(b"schema_version: 0", b"schema_version: 1"))]
    fs.writes.clear()
    assert config.migrate_config(None, cwd=tmp_path, fs=fs) is False
    assert fs.writes == []
    for bad in (block("host-example").replace("schema_version: 1\n", ""),
                block("host-example").replace("schema_version: 1", "schema_version: 2"),
                block("host-example").replace("schema_version: 1", "schema_version: true"),
                block("host-example").replace("schema_version: 1", "schema_version: 0") + "unknown: 3\n"):
        path.write_text(bad)
        before = path.read_bytes()
        with pytest.raises(config.ConfigError):
            config.migrate_config(None, cwd=tmp_path, fs=fs)
        assert path.read_bytes() == before and fs.writes == []
    project = b"project\xff\r\n"
    adopted = hostfiles.render(project)
    assert adopted.endswith(project)
    assert hostfiles.render(adopted) == adopted
    data = b"prefix\xff\n" + hostfiles.BEGIN + b"\nold\n" + hostfiles.END + b"\r\nsuffix\xfe"
    assert hostfiles.parse(hostfiles.render(data)) == hostfiles.parse(data)
    for corrupt in (hostfiles.BEGIN, hostfiles.END + hostfiles.BEGIN, adopted + hostfiles.END):
        with pytest.raises(hostfiles.MarkerCorruption):
            hostfiles.render(corrupt)
    cfg = load(tmp_path)
    assert providers.routed_conduct_paths(cfg, tmp_path) == (tmp_path / "AGENTS.md",)
    cfg.providers[0].name = "claude"
    for route in cfg.routing:
        route.candidates[0].provider = "claude"
    assert providers.routed_conduct_paths(cfg, tmp_path) == (tmp_path / "CLAUDE.md",)
    assert "tempo core" in hostfiles.classify_core(None).paved_road
    inventory(tickets.Frontmatter, yaml.safe_load(block("ticket-fields")))
    mentions("additive-only", "no frontmatter schema migration mechanism", "validates the full candidate", "original bytes",
             "before any write", "no-op", "zero marker-like text", "without decoding", "unrouted")


def test_host_contract_seam_inventory(tmp_path):
    assert list(inspect.signature(seams.ProcessExec.run).parameters) == [
        "self", "argv", "cwd", "env", "timeout", "stdin_path"]
    assert inspect.signature(seams.ProcessExec.run).parameters["env"].default is inspect.Parameter.empty
    assert inspect.signature(seams.ProcessExec.run).parameters["stdin_path"].default is None
    assert list(inspect.signature(gates.Gate.check).parameters) == ["self", "artifact", "workspace"]
    assert list(inspect.signature(MergeQueue.host_checks).parameters) == ["self", "ticket", "attempt", "safety"]
    assert list(inspect.signature(seams.Notifications.notify).parameters) == ["self", "argv"]
    for method, parameters in (("write", ["self", "path", "data"]),
                               ("replace", ["self", "src", "dst"]), ("publish", ["self", "path", "data"])):
        assert list(inspect.signature(getattr(seams.FileSystem, method)).parameters) == parameters
    source = inspect.getsource(seams.SubprocessExec.run)
    assert "start_new_session=True" in source and "except BaseException" in source
    assert "await _kill_and_wait(proc)" in source
    assert "kill_group(proc.pid)" in inspect.getsource(seams._kill_and_wait)
    assert "SIGKILL" in inspect.getsource(seams.SubprocessExec.kill_group)
    assert "stdin_path=prompt" in inspect.getsource(providers.CliAdapter.invoke)
    assert "fsync" in inspect.getsource(seams.LocalFileSystem.write)
    assert "os.replace" in inspect.getsource(seams.LocalFileSystem.write)
    cfg = load(tmp_path)
    cfg.providers[0].auth = "SECRET"
    assert providers.child_env({"PATH": "/bin", "SECRET": "value"}, cfg) == {"PATH": "/bin"}

    class Queue(MergeQueue):
        async def _command(self, ticket, attempt, argv, code, name):
            return argv, gates.GateReport(code=code, verdict="pass")

    queue = Queue(SimpleNamespace(config=cfg), escalate=lambda event: None)
    results, reports = asyncio.run(queue.host_checks(None, attempt=1, safety=True))
    assert results == [cfg.review.mechanical[0].argv] and reports[0].verdict == "pass"
    assert asyncio.run(queue.host_checks(None, attempt=1, safety=False))[0] == results
    cfg.merge.safety_checks = ["host-style"]
    queue.ctx.driver = SimpleNamespace(redactor=SimpleNamespace(scrub=lambda text: text))
    assert asyncio.run(queue.host_checks(None, attempt=1, safety=True))[1][0].verdict == "fail"
    cfg.merge.safety_checks = ["host-test"]
    cfg.review.mechanical.append(cfg.review.mechanical[0])
    assert asyncio.run(queue.host_checks(None, attempt=1, safety=True))[0] == []

    class Exec:
        response = (0, "SECRET", "")

        async def run(self, argv, *, cwd, env, timeout):
            assert cwd == tmp_path and "SECRET" not in env and timeout == 120
            if isinstance(self.response, Exception):
                raise self.response
            return self.response

    captured = []
    exec_ = Exec()
    queue.ctx = SimpleNamespace(
        config=cfg, env={"SECRET": "credential"}, exec_=exec_, worktree=lambda stem: tmp_path,
        driver=SimpleNamespace(
            redactor=SimpleNamespace(scrub=lambda text: text.replace("SECRET", "[redacted]")),
            spool=SimpleNamespace(write=lambda *args: captured.append(args)),
        ),
    )
    for response, verdict in (((0, "SECRET", ""), "pass"), ((1, "", "SECRET"), "fail"),
                              (TimeoutError(), "fail"), (seams.ExecutableNotFound("missing"), "fail")):
        exec_.response = response
        result, report = asyncio.run(MergeQueue._command(queue,
            SimpleNamespace(stem="host", stuck_minutes=2), 1, ["check"], "host-test", "capture.txt"))
        assert report.verdict == verdict and "SECRET" not in result.tail
        assert captured[-1][2] == "capture.txt"
        if verdict == "fail":
            assert report.findings[0].paved_road and "SECRET" not in report.findings[0].message
    command = inspect.getsource(MergeQueue._command)
    assert "rc == 0" in command and "spool.write" in command and "redactor.scrub" in command
    mentions("Clock = Callable[[], datetime]", "Sleep = Callable[[float], Awaitable[None]]",
             "outer cancellation", "signal-not-reap", "stdin_path", "explicit required env",
             "atomic", "NotificationFailed", "rc 0 passes", "exactly one hard", "always/hard",
             "not optional structured findings", "Gate.check(artifact, workspace) -> GateReport")


def test_host_contract_report_custody_and_cutover(tmp_path):
    report = yaml.safe_load(block("report-fields"))
    assert report == {
        "required": ["schema_version", "origin", "summary", "app_commit", "app_version"],
        "defaults": {"implicated_paths": [], "evidence": {"replay": None, "log": None}},
        "self_diagnosed_required": ["game_slug", "anomaly_code", "location"],
        "origins": ["self_diagnosed", "player"], "replay_max_bytes": 1024 * 1024,
        "log_max_bytes": 64 * 1024,
    }
    # Pending consumers have no import here: the committed plan governs their obligations.
    for fact in ("metadata-first 1 MiB", "64 KiB", "durable box custody", "operator", "NO_GO"):
        assert fact in PLAN
    mentions("metadata-first", "1 MiB", "64 KiB", "exact boundaries", "recheck",
             "before message recording", "durable box custody", "untrusted data at every hop",
             "ticket's evidence/", "initial_state", "ordered actions", "ONE instance",
             "never delete the lockfile", "each dispatch cycle", "tickets/decisions/",
             "Unattended bootstrap", "Supervised fixture wiring", "Author-graph judgment",
             "Operator-recorded GO", "NO_GO or no declared host waits", "live-host acceptance",
             "Foreign process-state adoption is excluded")
    clock = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    one = Lockfile(tmp_path, instance_id="one", clock=clock)
    two = Lockfile(tmp_path, instance_id="two", clock=clock)
    one.acquire()
    try:
        with pytest.raises(LockHeld) as exc:
            two.acquire()
        assert exc.value.holder["instance_id"] == "one"
    finally:
        one.release()
    two.acquire()
    two.release()
