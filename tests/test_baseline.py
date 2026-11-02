from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict
import json
import re
from types import SimpleNamespace

import pytest

from tempo.baseline import BaselineResolution, resolve_baseline
from tempo.config import snapshot_config
from tempo.journal import EventType, Journal
from tempo.policy import BASELINED_SURFACES, TIERS, baseline_identity
from tempo.providers import PLACEHOLDER
from tempo.specs import ENTRY_PARTS, entry_unit_gap, load_spec, required_units, resolve_plan_contract
from tests.test_drain import Clock
from tests.test_policy import ROOT, config, signal


def test_baseline_entry_unit():
    plan = (ROOT / "TEMPO_PLAN.md").read_text()
    unit = "19.P5.baseline-binding-reader"
    assert required_units(plan, "baseline-binding-reader", ()) == (unit,)
    for stem in ("baseline-binding-reader", "phase5-continue-03"):
        assert unit in required_units(plan, stem, ["19.I", unit, unit, "12"])
        assert required_units(plan, stem, [unit, unit]).count(unit) == 1
        assert resolve_plan_contract(plan, [unit, unit]).count(f"### {unit} ") == 1
        assert entry_unit_gap(plan, unit) is None
        for part in ENTRY_PARTS:
            missing = plan.replace(f"- **{part}:**", f"- **Removed {part}:**")
            assert entry_unit_gap(missing, unit) == f"entry unit {unit} lacks its {part} part(s)"


def test_baseline_states_and_latest_signal(tmp_path):
    cfg = config(tmp_path)
    identity = baseline_identity(cfg)
    go, no_go = signal("GO", identity), signal("NO_GO", identity)
    go.body["scorecard"] = {"unrelated": "evidence"}
    for events, state in [([], "ABSENT"), ([no_go], "NO_GO"), ([go], "GO"),
                          ([go, no_go], "NO_GO"), ([no_go, go], "GO"),
                          ([signal("GO", {})], "REVOKED")]:
        before = deepcopy(events)
        result = resolve_baseline(snapshot_config(cfg), events, specs_dir=ROOT / "specs")
        assert result.state == state and result.binds is (state == "GO")
        assert events == before
        with pytest.raises(FrozenInstanceError):
            result.state = "GO"
        with pytest.raises(FrozenInstanceError):
            result.binds = True
    with pytest.raises(ValueError):
        BaselineResolution("UNKNOWN")


@pytest.mark.parametrize("surface", BASELINED_SURFACES)
@pytest.mark.parametrize("tier", TIERS)
def test_baseline_routing_and_spec_drift(tmp_path, surface, tier):
    cfg = config(tmp_path)
    identity = baseline_identity(cfg)
    evidence = [signal("GO", identity)]
    specs = tmp_path / "specs"
    specs.mkdir()
    for name in BASELINED_SURFACES:
        (specs / f"{name}.md").write_text((ROOT / "specs" / f"{name}.md").read_text())
    def read():
        return resolve_baseline(cfg, evidence, specs_dir=specs)
    assert read().binds
    for key in ("provider", "model"):
        original = identity[surface]["rows"][tier][key]
        identity[surface]["rows"][tier][key] = "drifted"
        assert read().state == "REVOKED"
        identity[surface]["rows"][tier][key] = original
    route = next(r for r in cfg.routing if r.surface == surface and r.tier == tier)
    route.candidates[0].model = "changed-current-model"
    assert read().state == "REVOKED"
    route.candidates[0].model = None
    cfg.routing = [r for r in cfg.routing if r.surface != "author"]
    assert read().binds  # registry's author-to-review fallback
    path = specs / f"{surface}.md"
    text = path.read_text()
    version = load_spec(text).meta.version
    major, minor = map(int, version.split("."))
    path.write_text(re.sub(r"^version:.*$", f'version: "{major}.{minor + 1}"', text, flags=re.M))
    assert read().binds
    path.write_text(re.sub(r"^version:.*$", f'version: "{major + 1}.0"', text, flags=re.M))
    assert read().state == "REVOKED"


@pytest.mark.parametrize("bad", ["empty-routing", "empty-candidates", "unknown-route-tier",
    "unknown-provider", "placeholder-model", "placeholder-auth", "blank-model", "empty-identity",
    "missing-surface", "empty-rows", "unknown-tier", "bool-major", "string-major", "negative-major",
    "malformed-row", "blank-provider", "placeholder-provider", "placeholder-recorded-model",
    "missing-identity", "bad-body", "bad-event", "unknown-event-type", "bad-events", "iterator-error",
    "missing-spec", "invalid-spec", "malformed-spec", "unreadable-spec", "wrong-surface-spec"])
def test_baseline_invalid_inputs_fail_closed(tmp_path, bad):
    cfg = config(tmp_path)
    identity = baseline_identity(cfg)
    events = [signal("GO", identity)]
    specs = ROOT / "specs"
    row = identity["review"]["rows"]["low"]
    if bad == "empty-routing": cfg.routing = []
    elif bad == "empty-candidates": cfg.routing[0].candidates = []
    elif bad == "unknown-route-tier": cfg.routing[0].tier = "unknown"
    elif bad == "unknown-provider": cfg.routing[0].candidates[0].provider = "unknown"
    elif bad == "placeholder-model": cfg.providers[0].models_by_tier.low = PLACEHOLDER
    elif bad == "placeholder-auth": cfg.providers[0].auth = PLACEHOLDER
    elif bad == "blank-model": cfg.providers[0].models_by_tier.low = " "
    elif bad == "empty-identity": identity.clear()
    elif bad == "missing-surface": identity.pop("author")
    elif bad == "empty-rows": identity["review"]["rows"] = {}
    elif bad == "unknown-tier": identity["review"]["rows"]["unknown"] = row
    elif bad in ("bool-major", "string-major", "negative-major"):
        identity["review"]["spec_major"] = {"bool-major": True, "string-major": "1", "negative-major": -1}[bad]
    elif bad == "malformed-row": row.pop("model")
    elif bad == "blank-provider": row["provider"] = " "
    elif bad == "placeholder-provider": row["provider"] = PLACEHOLDER
    elif bad == "placeholder-recorded-model": row["model"] = PLACEHOLDER
    elif bad == "missing-identity": events[0].body.pop("identity")
    elif bad == "bad-body": events.append(SimpleNamespace(type=EventType.SIGNAL, body=[]))
    elif bad == "bad-event": events.append(None)
    elif bad == "unknown-event-type": events.append(SimpleNamespace(type="unknown", body={}))
    elif bad == "bad-events": events = None
    elif bad == "iterator-error":
        first = events[0]
        def broken():
            yield first
            raise ValueError("unreadable events")
        events = broken()
    else:
        specs = tmp_path / "specs"
        specs.mkdir()
        if bad == "invalid-spec": (specs / "review.md").write_bytes(b"\xff")
        if bad == "malformed-spec": (specs / "review.md").write_text("not a spec")
        if bad == "unreadable-spec": (specs / "review.md").mkdir()
        if bad == "wrong-surface-spec":
            (specs / "review.md").write_text((ROOT / "specs" / "author.md").read_text())
    assert not resolve_baseline(cfg, events, specs_dir=specs).binds


def test_baseline_invalid_inputs_fail_closed_torn_tail(tmp_path):
    cfg = config(tmp_path)
    journal = Journal(cfg.state_dir, Clock())
    event = journal.append(EventType.SIGNAL, signal("NO_GO", baseline_identity(cfg)).body)
    path, = journal.dir.glob("*.jsonl")
    torn_go = asdict(event)
    torn_go["body"]["verdict"] = "GO"
    with path.open("ab") as stream:
        stream.write(json.dumps(torn_go).encode())  # complete JSON, incomplete journal line
    before = path.read_bytes()
    assert resolve_baseline(cfg, journal.read(), specs_dir=ROOT / "specs").state == "NO_GO"
    assert path.read_bytes() == before
    path.write_bytes(json.dumps(torn_go).encode()[:80])
    assert resolve_baseline(cfg, journal.read(), specs_dir=ROOT / "specs").state == "ABSENT"
