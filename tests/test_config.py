import textwrap
from pathlib import Path

import pytest

from tempo.config import SCHEMA_VERSION, ConfigError, load_config

VALID = textwrap.dedent(
    """\
    schema_version: 1
    state_dir: .tempo
    review:
      mechanical:
        - {code: pytest, argv: [uv, run, pytest], trigger: always, severity: hard}
        - {code: ruff, argv: [uv, run, ruff, check], trigger: [tempo/], severity: soft}
      surfaces:
        - {name: correctness, trigger: always, rules_doc: docs/review.md, severity: hard}
      trigger_map: {tempo/: [scope_fence]}
      gate_severity: {scope_fence: hard}
    merge:
      safety_checks: [pytest]
      strategies:
        - {paths: [uv.lock], strategy: regenerate, argv: [uv, lock]}
        - {paths: [CHANGELOG.md], strategy: union}
    hosts:
      claude:
        models_by_tier: {low: haiku, medium: sonnet, high: sonnet, max: sonnet}
        pins: {review: opus}
    orchestrator:
      models: {claude: opus}
    engine_plane_safety_inventory: [specs/, tempo/gates.py, config.yaml]
    """
)


def write(tmp_path: Path, text: str, name: str = "config.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def edit(text: str, old: str, new: str) -> str:
    assert old in text
    return text.replace(old, new, 1)


def refusal(tmp_path: Path, text: str) -> ConfigError:
    write(tmp_path, text)
    with pytest.raises(ConfigError) as exc:
        load_config(None, cwd=tmp_path)
    return exc.value


def test_valid_config_loads_from_cwd_with_shipped_defaults(tmp_path):
    cfg = load_config(None, cwd=write(tmp_path, VALID).parent)
    assert cfg.schema_version == SCHEMA_VERSION == 1
    assert cfg.state_dir == tmp_path / ".tempo"
    assert cfg.worktree_root == tmp_path / ".tempo" / "worktrees"
    assert cfg.hosts["claude"].quota_window_minutes == 60 and cfg.hosts["claude"].pins == {"review": "opus"}
    assert cfg.review.mechanical[0].trigger == "always"
    assert cfg.review.mechanical[1].trigger == ["tempo/"]
    assert cfg.merge.strategies[0].argv == ["uv", "lock"]
    assert cfg.scheduler.max_unmerged == 2
    assert cfg.caps.model_dump() == {
        "decisions": 6, "retry": 6, "premise_bounce": 2, "infra": 6, "quarantine": 5, "poison": 2,
    }
    assert cfg.circuit_breaker.k == 3
    assert cfg.circuit_breaker.cooldown_minutes == 10
    assert cfg.tickets.max_minutes == 90
    assert cfg.box_policy == {
        "failure_report": "confirmed",
        "retro_finding": "confirmed",
        "override_report": "draft",
        "suggestion": "draft",
        "bug_report_self_diagnosed": "confirmed",
        "bug_report_player_repro": "confirmed",
        "bug_report_player_no_repro": "draft",
    }
    assert cfg.notify is None
    assert cfg.report_inbox is None
    assert cfg.context_files == []


def test_config_flag_path_overrides_cwd(tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    path = write(other, edit(VALID, "state_dir: .tempo", "state_dir: /var/tempo"), "host.yaml")
    cfg = load_config(path, cwd=tmp_path)
    assert cfg.state_dir == Path("/var/tempo")


def test_explicit_values_override_defaults_and_partial_blocks_keep_the_rest(tmp_path):
    text = VALID + textwrap.dedent(
        """\
        worktree_root: /tmp/wt
        caps: {retry: 4}
        box_policy: {failure_report: draft}
        notify: [notify-send, tempo]
        report_inbox: inbox
        context_files: [docs/arch.md]
        """
    )
    cfg = load_config(write(tmp_path, text), cwd=tmp_path)
    assert cfg.worktree_root == Path("/tmp/wt")
    assert cfg.caps.retry == 4 and cfg.caps.decisions == 6
    assert cfg.box_policy["failure_report"] == "draft"
    assert cfg.box_policy["suggestion"] == "draft"
    assert cfg.notify == ["notify-send", "tempo"]
    assert cfg.report_inbox == tmp_path / "inbox"
    assert cfg.context_files == [Path("docs/arch.md")]


@pytest.mark.parametrize(
    "key", ["schema_version", "state_dir", "hosts", "orchestrator", "review", "merge",
            "engine_plane_safety_inventory"],
)
def test_missing_required_top_level_key_is_refused_naming_it(tmp_path, key):
    lines = VALID.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{key}:"))
    end = next((i for i in range(start + 1, len(lines)) if not lines[i].startswith(" ")), len(lines))
    err = refusal(tmp_path, "".join(lines[:start] + lines[end:]))
    assert err.key == key
    assert key in str(err)
    assert "missing" in str(err)


def test_missing_nested_required_key_names_full_path(tmp_path):
    err = refusal(tmp_path, edit(VALID, "models_by_tier: {low: haiku, medium: sonnet, high: sonnet, max: sonnet}",
                                 "models_by_tier: {low: haiku, medium: sonnet, high: sonnet}"))
    assert err.key == "hosts.claude.models_by_tier.max"
    assert "hosts.claude.models_by_tier.max" in str(err)


def test_unknown_key_is_refused_naming_it(tmp_path):
    err = refusal(tmp_path, VALID + "workers: 4\n")
    assert err.key == "workers"
    assert "workers" in str(err)


def test_explicit_null_is_refused_naming_key_even_where_a_default_exists(tmp_path):
    err = refusal(tmp_path, VALID + "caps: {retry: null}\n")
    assert err.key == "caps.retry"
    assert "null" in str(err)
    err = refusal(tmp_path, VALID + "notify:\n")
    assert err.key == "notify"


def test_wrong_type_is_refused_not_coerced(tmp_path):
    err = refusal(tmp_path, VALID + 'caps: {retry: "4"}\n')
    assert err.key == "caps.retry"
    err = refusal(tmp_path, VALID + "caps: {retry: true}\n")
    assert err.key == "caps.retry"


def test_newer_schema_version_is_refused(tmp_path):
    err = refusal(tmp_path, edit(VALID, "schema_version: 1", "schema_version: 2") + "future_key: x\n")
    assert err.key == "schema_version"
    assert "newer" in str(err)
    assert "upgrade tempo" in str(err)


def test_older_schema_version_is_refused_naming_migrate_config(tmp_path):
    err = refusal(tmp_path, edit(VALID, "schema_version: 1", "schema_version: 0"))
    assert err.key == "schema_version"
    assert "older" in str(err)
    assert "tempo migrate-config" in str(err)


@pytest.mark.parametrize("value", ['"1"', "1.0", "true", "-1"])
def test_non_integer_or_negative_schema_version_is_refused(tmp_path, value):
    err = refusal(tmp_path, edit(VALID, "schema_version: 1", f"schema_version: {value}"))
    assert err.key == "schema_version"


def test_retry_cap_above_decisions_cap_is_refused(tmp_path):
    err = refusal(tmp_path, VALID + "caps: {retry: 7}\n")
    assert err.key == "caps.retry"


def test_hosts_resolve_each_surface_with_review_pinned_outside_the_tiers(tmp_path):
    from tempo.call import resolve_model
    cfg = load_config(None, cwd=write(tmp_path, VALID).parent)
    assert resolve_model(cfg, "claude", "implement", "low") == "haiku"
    assert resolve_model(cfg, "claude", "review", "low") == "opus"
    assert cfg.orchestrator.session.max_tickets == 5 and cfg.orchestrator.session.max_minutes == 240


@pytest.mark.parametrize("old,new,key", [
    ("pins: {review: opus}", "pins: {review: sonnet}", "hosts.claude.pins.review"),
    ("pins: {review: opus}", "pins: {}", "hosts.claude.pins.review"),
    ("models: {claude: opus}", "models: {claude: opus, codex: x}", "orchestrator.models.codex"),
    ("models: {claude: opus}", "models: {copilot: opus}", "orchestrator.models.copilot"),
    ("pins: {review: opus}", "pins: {review: opus, diagnose: opus}", "hosts.claude.pins.diagnose"),
    ("pins: {review: opus}", "pins: {review: opus}\n    effort: high", "hosts.claude.effort"),
    ("{low: haiku, medium: sonnet, high: sonnet", "{low: s/1, medium: s:1, high: sonnet", "hosts.claude"),
])
def test_host_config_refusals_name_the_key(tmp_path, old, new, key):
    assert refusal(tmp_path, edit(VALID, old, new)).key == key


def test_closed_vocabularies_refuse_unknown_values(tmp_path):
    assert refusal(tmp_path, edit(VALID, "severity: soft", "severity: warn")).key == (
        "review.mechanical.1.severity"
    )
    assert refusal(tmp_path, VALID + "box_policy: {bogus_row: draft}\n").key == "box_policy.bogus_row"


def test_union_strategy_takes_no_argv_and_regenerate_requires_one(tmp_path):
    err = refusal(tmp_path, edit(VALID, "strategy: union}", "strategy: union, argv: [x]}"))
    assert err.key.startswith("merge.strategies.1")
    err = refusal(tmp_path, edit(VALID, "strategy: regenerate, argv: [uv, lock]}", "strategy: regenerate}"))
    assert err.key.startswith("merge.strategies.0")


def test_missing_file_is_refused_with_a_paved_road(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(None, cwd=tmp_path)
    assert str(tmp_path / "config.yaml") in str(exc.value)
    assert "--config" in str(exc.value)


def test_non_mapping_and_unparseable_yaml_are_refused(tmp_path):
    assert "mapping" in str(refusal(tmp_path, "- a\n- b\n"))
    assert "YAML" in str(refusal(tmp_path, "a: [unclosed\n"))


def test_unsafe_yaml_tags_are_refused(tmp_path):
    err = refusal(tmp_path, VALID + "notify: !!python/object/apply:os.system [echo hi]\n")
    assert "YAML" in str(err)


def test_provider_backstop_never_undercuts_a_ticket_stuck_budget(tmp_path):
    from tempo.checkout import call_timeout
    write(tmp_path, VALID)
    cfg = load_config(None, cwd=tmp_path)
    assert call_timeout(cfg) == cfg.tickets.max_minutes * 60.0


@pytest.mark.parametrize("argv", ['[]', 'notify-send', '[3]', '[true]', '[{}]', '[""]',
                                 '["   "]', '[notify, "\\0"]'])
def test_notify_refuses_malformed_argv(tmp_path, argv):
    assert refusal(tmp_path, VALID + f"notify: {argv}\n").key.startswith("notify")


class MigrationFS:
    def __init__(self):
        from tempo.seams import LocalFileSystem
        self.local = LocalFileSystem()
        self.writes = []

    def write(self, path, data):
        self.writes.append((path, data))
        self.local.write(path, data)


@pytest.mark.parametrize("declaration, migrated", [
    ("schema_version: 0", "schema_version: 1"),
    ("'schema_version' :   +0 # version", "'schema_version' :   1 # version"),
    ("schema_version: !!int 0x0 # version", "schema_version: !!int 1 # version"),
    ("schema_version: !!int '0' # version", "schema_version: !!int '1' # version"),
    ("schema_version: !!int |- # version\n  0", "schema_version: !!int |- # version\n  1"),
    ("schema_version: !!int >- # version\n  0", "schema_version: !!int >- # version\n  1"),
])
@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_migrate_config_zero_to_one(tmp_path, monkeypatch, declaration, migrated, ending):
    import os
    from tempo.config import migrate_config

    text = "\ufeff# café: schema_version: 0\n" + VALID.replace("schema_version: 1", declaration)
    original = text.rstrip("\n").replace("\n", ending).encode()
    path = tmp_path / "config.yaml"
    path.write_bytes(original)
    backup = tmp_path / "config.yaml.bak"
    expected = original.replace((ending + declaration.replace("\n", ending)).encode(),
                                (ending + migrated.replace("\n", ending)).encode(), 1)
    fs = MigrationFS()
    publications = []
    replace = os.replace
    def observe(src, dst):
        src, dst = Path(src), Path(dst)
        if dst == path:
            assert src.parent == path.parent
            assert src.read_bytes() == expected
            assert path.read_bytes() == original
            assert backup.read_bytes() == original
            publications.append(dst)
        replace(src, dst)
    monkeypatch.setattr(os, "replace", observe)
    assert migrate_config(None, cwd=tmp_path, fs=fs) is True
    assert path.read_bytes() == expected
    assert backup.read_bytes() == original
    assert fs.writes == [(backup, original), (path, expected)]
    assert publications == [path]
    assert load_config(path, cwd=tmp_path).schema_version == 1


@pytest.mark.parametrize("invalid", ["caps: {retry: 7}\n", "unknown: true\n",
                                     "notify: null\n", 'caps: {retry: "4"}\n'])
def test_migrate_config_validates_before_write(tmp_path, monkeypatch, invalid):
    import tempo.config as module

    path = write(tmp_path, VALID.replace("schema_version: 1", "schema_version: 0") + invalid)
    backup = tmp_path / "config.yaml.bak"
    backup.write_bytes(b"previous backup")
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    fs = MigrationFS()
    load = module.load_config
    seen = []
    def observe(source, *, cwd):
        assert fs.writes == []
        assert source.read_text().startswith("schema_version: 1")
        seen.append(source)
        return load(source, cwd=cwd)
    monkeypatch.setattr(module, "load_config", observe)
    with pytest.raises(ConfigError, match="repair.*retry migrate-config"):
        module.migrate_config(path, cwd=tmp_path, fs=fs)
    assert len(seen) == 1
    assert fs.writes == []
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize("existing_backup", [False, True])
def test_migrate_config_current_is_noop(tmp_path, existing_backup):
    from tempo.config import migrate_config

    path = write(tmp_path, "# unchanged\n" + VALID)
    backup = tmp_path / "config.yaml.bak"
    if existing_backup:
        backup.write_bytes(b"previous backup")
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    fs = MigrationFS()
    for _ in range(2):
        assert migrate_config(path, cwd=tmp_path, fs=fs) is False
        assert fs.writes == []
        assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize("text, cause, road", [
    (None, "cannot read config file", "--config"),
    (VALID.replace("schema_version: 1\n", ""), "missing required key", "supply"),
    *[(VALID.replace("schema_version: 1", f"schema_version: {v}"), "non-negative integer", "supply")
      for v in ("null", "true", '\"0\"', "0.0", "-1")],
    (VALID.replace("schema_version: 1", "schema_version: 2"), "newer", "upgrade tempo"),
    ("a: [unclosed\n", "invalid YAML", "repair"),
    ("schema_version: !!int wrong\n", "invalid YAML", "repair"),
    ("!!python/object/apply:os.system [echo hi]", "invalid YAML", "repair"),
    ("- a\n- b\n", "mapping", "repair"),
    ("null\n", "mapping", "repair"),
    ("schema_version: 0\n", "missing required key", "repair"),
    ("schema_version: 1\n", "missing required key", "repair"),
    (VALID.replace("schema_version: 1", "schema_version: 0\nschema_version: 0"),
     "duplicate", "repair"),
    (VALID.replace("schema_version: 1", "version: &v 0\nschema_version: *v"),
     "explicit scalar", "repair"),
])
@pytest.mark.parametrize("existing_backup", [False, True])
def test_migrate_config_refuses_unsupported_inputs(tmp_path, text, cause, road, existing_backup):
    from tempo.config import migrate_config

    path = tmp_path / "config.yaml"
    if text is not None:
        path.write_text(text)
    if existing_backup:
        (tmp_path / "config.yaml.bak").write_bytes(b"previous backup")
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    fs = MigrationFS()
    with pytest.raises(ConfigError) as exc:
        migrate_config(None, cwd=tmp_path, fs=fs)
    assert cause in str(exc.value)
    assert road in str(exc.value)
    assert fs.writes == []
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before
