import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from tempo.baseline import resolve_baseline
from tempo.config import BOX_POLICY_DEFAULTS, load_config
from tempo.journal import EventType
from tempo.policy import BASELINE_SIGNAL, baseline_identity, policy_row, start_state, touches_inventory

ROOT = Path(__file__).resolve().parent.parent


def config(tmp_path: Path):
    routes = "".join(
        f"  - {{tier: {tier}, surface: {surface}, candidates: [{{provider: claude}}]}}\n"
        for tier in ("low", "medium", "high", "max") for surface in ("review", "author")
    )
    text = textwrap.dedent(
        """\
        schema_version: 1
        state_dir: state
        providers:
          - name: claude
            kind: cli
            package: test-cli
            models_by_tier: {low: low, medium: medium, high: high, max: max}
            limits: {concurrency: 1}
        routing:
        """
    ) + routes + "review: {}\nmerge: {}\nengine_plane_safety_inventory: [tempo/gates.py]\n"
    (tmp_path / "config.yaml").write_text(text)
    return load_config(None, cwd=tmp_path)


def signal(verdict: str, identity: dict):
    return SimpleNamespace(type=EventType.SIGNAL, body={"signal": BASELINE_SIGNAL, "verdict": verdict,
                                                         "identity": identity})


def state(config, row, *, go=True, fence=("tempo/author.py",), gate_bypass=(), reopen=False):
    return start_state(config, row=row, fence=fence, gate_bypass=gate_bypass, go=go, reopen=reopen)


def test_bootstrap_without_a_go_or_with_phase_one_no_go_drafts_every_row(tmp_path):
    cfg = config(tmp_path)
    identity = baseline_identity(cfg, ROOT / "specs")
    for row in BOX_POLICY_DEFAULTS:
        assert state(cfg, row, go=resolve_baseline(cfg, [], specs_dir=ROOT / "specs").binds) == "draft"
        assert state(cfg, row, go=resolve_baseline(cfg, [signal("NO_GO", identity)], specs_dir=ROOT / "specs").binds) == "draft"


def test_standing_go_uses_the_table_and_each_override_drafts(tmp_path):
    cfg = config(tmp_path)
    identity = baseline_identity(cfg, ROOT / "specs")
    go = resolve_baseline(cfg, [signal("GO", identity)], specs_dir=ROOT / "specs").binds
    assert go
    for row, expected in BOX_POLICY_DEFAULTS.items():
        assert state(cfg, row, go=go) == expected
        assert state(cfg, row, go=go, reopen=True) == "draft"
        assert state(cfg, row, go=go, gate_bypass=("scope_fence",)) == "draft"
        assert state(cfg, row, go=go, fence=("tempo/gates.py",)) == "draft"
        inventory = cfg.engine_plane_safety_inventory
        cfg.engine_plane_safety_inventory = []
        assert state(cfg, row, go=go) == "draft"
        cfg.engine_plane_safety_inventory = inventory

    row = "failure_report"
    assert not resolve_baseline(cfg, [signal("GO", identity), signal("NO_GO", identity)], specs_dir=ROOT / "specs").binds
    drifted = {**identity, "review": {**identity["review"], "spec_major": 999}}
    assert not resolve_baseline(cfg, [signal("GO", drifted)], specs_dir=ROOT / "specs").binds
    assert state(cfg, row, go=False) == "draft"
    cfg.engine_plane_safety_inventory = []
    assert state(cfg, row, go=True) == "draft"
    cfg.engine_plane_safety_inventory = ["specs/triage.md"]
    assert state(cfg, row, fence=("specs/",)) == "draft"
    cfg.engine_plane_safety_inventory = ["specs/"]
    assert state(cfg, row, fence=("specs/triage.md",)) == "draft"
    cfg.engine_plane_safety_inventory = ["tempo/gates.py"]
    assert state(cfg, row, gate_bypass=("scope_fence",)) == "draft"
    assert state(cfg, row, reopen=True) == "draft"
    assert not touches_inventory(("tempo/gatesx.py",), cfg.engine_plane_safety_inventory)


@pytest.mark.parametrize(("message_class", "bug_origin", "has_repro", "row"), [
    ("suggestion", None, None, "suggestion"),
    ("failure_report", None, None, "failure_report"),
    ("override_report", None, None, "override_report"),
    ("retro_finding", None, None, "retro_finding"),
    ("bug_report", "self_diagnosed", True, "bug_report_self_diagnosed"),
    ("bug_report", "player", True, "bug_report_player_repro"),
    ("bug_report", "player", False, "bug_report_player_no_repro"),
])
def test_policy_row_maps_every_message_class(message_class, bug_origin, has_repro, row):
    assert policy_row(message_class, bug_origin, has_repro) == row


@pytest.mark.parametrize("message_class, bug_origin, has_repro", [
    ("unknown", None, None), ("bug_report", None, None), ("bug_report", "player", None),
])
def test_policy_row_refuses_unknown_or_incomplete_messages(message_class, bug_origin, has_repro):
    with pytest.raises(ValueError):
        policy_row(message_class, bug_origin, has_repro)
