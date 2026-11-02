"""The sole fail-closed baseline binding reader (plan 19.P5.baseline-binding-reader)."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

from tempo.config import Config, ConfigSnapshot
from tempo.journal import EventType
from tempo.policy import BASELINE_SIGNAL, BASELINED_SURFACES, TIERS
from tempo.providers import PLACEHOLDER, resolve
from tempo.specs import load_spec

BaselineState = Literal["ABSENT", "NO_GO", "GO", "REVOKED"]


@dataclass(frozen=True, slots=True)
class BaselineResolution:
    state: BaselineState
    binds: bool = field(init=False)

    def __post_init__(self) -> None:
        if self.state not in get_args(BaselineState):
            raise ValueError("baseline state must be ABSENT, NO_GO, GO or REVOKED")
        object.__setattr__(self, "binds", self.state == "GO")


def _name(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value != PLACEHOLDER


def _matches(config, identity, specs_dir: Path) -> bool:
    if not isinstance(identity, Mapping) or set(identity) != set(BASELINED_SURFACES):
        return False
    if not config.routing or any(route.tier not in TIERS or not route.candidates for route in config.routing):
        return False
    for surface in BASELINED_SURFACES:
        recorded = identity[surface]
        if not isinstance(recorded, Mapping) or set(recorded) != {"spec_major", "rows"}:
            return False
        major, rows = recorded["spec_major"], recorded["rows"]
        if type(major) is not int or major < 0 or not isinstance(rows, Mapping) or not rows:
            return False
        spec = load_spec((specs_dir / f"{surface}.md").read_text())
        if spec.meta.llm_surface != surface or major != int(spec.meta.version.split(".")[0]):
            return False
        for tier, row in rows.items():
            if tier not in TIERS or not isinstance(row, Mapping) or set(row) != {"provider", "model"}:
                return False
            if not all(_name(row[key]) for key in ("provider", "model")):
                return False
            served = resolve(config, tier, surface)
            if not _name(served.provider.name) or not _name(served.model):
                return False
            if row != {"provider": served.provider.name, "model": served.model}:
                return False
    return True


def resolve_baseline(
    config: Config | ConfigSnapshot, events: Iterable[Any], *, specs_dir: Path
) -> BaselineResolution:
    """Fold complete journal records in order; invalid evidence never authorizes Author."""
    try:
        latest = None
        for event in events:
            if event.type not in EventType or not isinstance(event.body, Mapping):
                return BaselineResolution("REVOKED")
            if event.type == EventType.SIGNAL and event.body.get("signal") == BASELINE_SIGNAL:
                latest = event.body
        if latest is None:
            return BaselineResolution("ABSENT")
        if latest.get("verdict") != "GO":
            return BaselineResolution("NO_GO")
        return BaselineResolution("GO" if _matches(config, latest.get("identity"), specs_dir) else "REVOKED")
    except Exception:
        # This authority boundary also contains registry, spec and event-iteration failures.
        return BaselineResolution("REVOKED")


def review_coverage(config, events, *, specs_dir: Path, tier: str,
                    served: tuple[str | None, str | None]) -> tuple[bool, str]:
    events = tuple(events)
    resolution = resolve_baseline(config, events, specs_dir=specs_dir)
    if not resolution.binds:
        return False, "go_revoked" if resolution.state == "REVOKED" else "go_absent"
    latest = next(e.body for e in reversed(events)
                  if e.type == EventType.SIGNAL and e.body.get("signal") == BASELINE_SIGNAL)
    row = latest["identity"]["review"]["rows"].get(tier)
    covered = all(_name(name) for name in served) and row == dict(zip(("provider", "model"), served))
    return covered, "go_covered" if covered else "review_off_baseline"
