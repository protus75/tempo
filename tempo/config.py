"""Host config loader (TEMPO_PLAN.md section 15): one config.yaml, validated fail-closed.

Defaulting is fail-closed: a shipped default applies only when a key is ABSENT; an
explicit null anywhere is refused before validation, so `X | None` fields below mean
"unset", never "null was written".
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator, model_validator

from tempo.seams import FileSystem

SCHEMA_VERSION = 1

Tier = Literal["low", "medium", "high", "max"]
Severity = Literal["hard", "soft"]
Trigger = Literal["always"] | list[str]
BoxRow = Literal[
    "failure_report",
    "retro_finding",
    "override_report",
    "suggestion",
    "bug_report_self_diagnosed",
    "bug_report_player_repro",
    "bug_report_player_no_repro",
]
StartState = Literal["draft", "confirmed"]
# Strict mode would refuse a YAML string for a Path; paths are the one sanctioned coercion.
PathField = Annotated[Path, Field(strict=False)]
Argv = Annotated[list[str], Field(min_length=1)]
Count = Annotated[int, Field(ge=1)]

# Section 12 policy table.
BOX_POLICY_DEFAULTS: dict[str, StartState] = {
    "failure_report": "confirmed",
    "retro_finding": "confirmed",
    "override_report": "draft",
    "suggestion": "draft",
    "bug_report_self_diagnosed": "confirmed",
    "bug_report_player_repro": "confirmed",
    "bug_report_player_no_repro": "draft",
}


class ConfigError(Exception):
    """config.yaml is unreadable or invalid; `key` is the dotted path of the (first) bad key."""

    def __init__(self, path: Path, key: str | None, message: str) -> None:
        super().__init__(f"{path}: {key}: {message}" if key else f"{path}: {message}")
        self.path = path
        self.key = key


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class ModelsByTier(_Strict):
    low: str
    medium: str
    high: str
    max: str


class Limits(_Strict):
    concurrency: Count
    est_cost_per_call_usd: Annotated[float, Field(ge=0)] | None = None
    quota_window_minutes: Count = 60


class Provider(_Strict):
    name: str
    kind: Literal["api", "cli"]
    auth: str | None = None
    package: str | None = None  # a `cli` provider's pnpm package: the preflight reads its latest release
    models_by_tier: ModelsByTier
    limits: Limits

    @model_validator(mode="after")
    def _cli_declares_its_package(self) -> "Provider":
        if self.kind == "cli" and not self.package:
            raise ValueError(f"cli provider {self.name!r} declares no `package`; name its pnpm package"
                             " (section 6 provider preflight)")
        return self


class Candidate(_Strict):
    provider: str
    model: str | None = None  # absent: the provider's models_by_tier[tier] serves (section 6)


class Route(_Strict):
    tier: Tier
    surface: str
    candidates: Annotated[list[Candidate], Field(min_length=1)]


class MechanicalCheck(_Strict):
    code: str
    argv: Argv
    trigger: Trigger
    severity: Severity


class ReviewSurface(_Strict):
    name: str
    trigger: Trigger
    rules_doc: PathField
    severity: Severity


class Review(_Strict):
    mechanical: list[MechanicalCheck] = []
    surfaces: list[ReviewSurface] = []
    trigger_map: dict[str, list[str]] = {}
    gate_severity: dict[str, Severity] = {}


class RegenerateStrategy(_Strict):
    paths: Annotated[list[str], Field(min_length=1)]
    strategy: Literal["regenerate"]
    argv: Argv


class UnionStrategy(_Strict):
    paths: Annotated[list[str], Field(min_length=1)]
    strategy: Literal["union"]


class Merge(_Strict):
    safety_checks: list[str] = []
    strategies: list[Annotated[RegenerateStrategy | UnionStrategy, Field(discriminator="strategy")]] = []


class Scheduler(_Strict):
    max_unmerged: Count = 2


class Caps(_Strict):
    diagnosis: Count = 6
    retry: Count = 6
    premise_bounce: Count = 2
    hardening: Count = 3  # held spec-gap terminals per lineage (section 11.4)
    infra: Count = 6
    quarantine: Count = 5
    poison: Count = 2


class Seeding(_Strict):
    max_seeds_per_admission: Count = 3


class CircuitBreaker(_Strict):
    k: Count = 3
    cooldown_minutes: Count = 10


class Drain(_Strict):
    max_runtime_hours: Count = 12
    max_ticket_minutes: Count = 90


class Config(_Strict):
    schema_version: int
    state_dir: PathField
    worktree_root: PathField | None = None
    providers: Annotated[list[Provider], Field(min_length=1)]
    routing: Annotated[list[Route], Field(min_length=1)]
    routing_default_tier: Tier = "medium"
    review: Review
    merge: Merge
    scheduler: Scheduler = Scheduler()
    caps: Caps = Caps()
    seeding: Seeding = Seeding()
    circuit_breaker: CircuitBreaker = CircuitBreaker()
    drain: Drain = Drain()
    engine_plane_safety_inventory: list[str]  # no shipped default: absence fails closed (section 12)
    box_policy: dict[BoxRow, StartState] = Field(default_factory=lambda: dict(BOX_POLICY_DEFAULTS))
    notify: Argv | None = None
    report_inbox: PathField | None = None
    context_files: list[PathField] = []

    @field_validator("notify")
    @classmethod
    def _notify_argv(cls, argv: list[str] | None) -> list[str] | None:
        if argv is not None and (not argv[0].strip() or any("\0" in part for part in argv)):
            raise ValueError("notify needs a nonblank executable and NUL-free argv strings; set an argv list")
        return argv

    @field_validator("box_policy")
    @classmethod
    def _box_policy_defaults(cls, rows: dict[str, StartState]) -> dict[str, StartState]:
        return {**BOX_POLICY_DEFAULTS, **rows}

    @model_validator(mode="after")
    def _resolve_paths(self, info: ValidationInfo) -> "Config":
        base: Path = info.context["base"]
        self.state_dir = base / self.state_dir
        self.worktree_root = base / self.worktree_root if self.worktree_root else self.state_dir / "worktrees"
        if self.report_inbox is not None:
            self.report_inbox = base / self.report_inbox
        return self


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    """Detached field-attribute view of a validated config record (19.P3.dispatch-config-snapshot)."""

    _fields: Mapping[str, Any]

    def __getattr__(self, name: str) -> Any:
        try:
            return self._fields[name]
        except KeyError:
            raise AttributeError(name) from None


def snapshot_config(config: Config) -> ConfigSnapshot:
    """Capture every validated field without running parsing, defaults, or validators again."""
    def freeze(value: Any) -> Any:
        if isinstance(value, BaseModel):
            return ConfigSnapshot(MappingProxyType({
                name: freeze(getattr(value, name)) for name in type(value).model_fields
            }))
        if isinstance(value, dict):
            return MappingProxyType({key: freeze(item) for key, item in value.items()})
        if isinstance(value, list):
            return tuple(freeze(item) for item in value)
        return value

    return freeze(config)


def load_config(config_path: Path | None, *, cwd: Path) -> Config:
    """Load the `--config` path, else `config.yaml` at the invocation cwd (the checkout root)."""
    path = config_path if config_path is not None else Path(cwd) / "config.yaml"
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise ConfigError(
            path, None, "config file not found; create config.yaml at the checkout root or pass --config <path>"
        ) from None
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(path, None, f"invalid YAML (safe_load only; no tags): {e}") from None
    if not isinstance(raw, dict):
        raise ConfigError(path, None, "top level must be a mapping of config keys")
    _check_schema_version(path, raw)
    if (null_key := _find_null(raw, "")) is not None:
        raise ConfigError(
            path, null_key, "explicit null is refused; omit the key to take its shipped default, or set a value"
        )
    try:
        cfg = Config.model_validate(raw, context={"base": path.parent})
    except ValidationError as e:
        raise _from_validation(path, e) from None
    _check_references(path, cfg)
    return cfg


class _CandidatePath(Path):
    """Let the real loader read an unpublished candidate at the original config base."""

    text: str

    def read_text(self, *args, **kwargs) -> str:
        return self.text


def migrate_config(config_path: Path | None, *, cwd: Path, fs: FileSystem) -> bool:
    """Validate before publishing an exact-byte backup and the sole supported 0->1 edit."""
    path = config_path if config_path is not None else Path(cwd) / "config.yaml"
    try:
        original = path.read_bytes()
    except OSError as exc:
        raise ConfigError(path, None, f"cannot read config file: {exc}; create config.yaml or pass --config <path>") from None
    try:
        # Keep the BOM in the text so YAML marks still address the original bytes.
        text = original.decode("utf-8")
        raw = yaml.safe_load(text)
        node = yaml.compose(text, Loader=yaml.SafeLoader)
        tokens = list(yaml.scan(text))
    except (ValueError, yaml.YAMLError) as exc:
        raise ConfigError(path, None, f"invalid YAML: {exc}; repair the selected config and retry migrate-config") from None
    if not isinstance(raw, dict):
        raise ConfigError(path, None, "top level must be a mapping of config keys; repair the selected config and retry migrate-config")
    if "schema_version" not in raw:
        raise ConfigError(path, "schema_version", "missing required key; supply explicit schema_version: 0 or 1 and retry migrate-config")
    version = raw["schema_version"]
    if type(version) is not int or version < 0:
        raise ConfigError(path, "schema_version", f"must be a non-negative integer, got {version!r}; supply the required integer version and retry migrate-config")
    if version > SCHEMA_VERSION:
        _check_schema_version(path, raw)
    candidate = text
    if version == 0:
        entries = [(key, value) for key, value in node.value
                  if isinstance(key, yaml.ScalarNode) and key.value == "schema_version"]
        scalars = [token for token in tokens if isinstance(token, yaml.tokens.ScalarToken)
                   and len(entries) == 1 and entries[0][0].end_mark.index <= token.start_mark.index
                   and entries[0][1].start_mark.index <= token.start_mark.index
                   and token.end_mark.index == entries[0][1].end_mark.index]
        if len(scalars) != 1:
            raise ConfigError(path, "schema_version", "version must be one explicit scalar, without duplicate keys or aliases; repair schema_version and retry migrate-config")
        scalar = scalars[0]
        start, end = scalar.start_mark.index, scalar.end_mark.index
        if scalar.style in ("'", '"'):
            start, end = start + 1, end - 1
        elif scalar.style in ("|", ">"):
            # Block indicators, header comments, indentation and line endings are formatting.
            start += len(text[start:end].splitlines(keepends=True)[0])
            while start < end and text[start].isspace():
                start += 1
            while end > start and text[end - 1].isspace():
                end -= 1
        candidate = text[:start] + str(SCHEMA_VERSION) + text[end:]
    source = _CandidatePath(path)
    source.text = candidate
    try:
        load_config(source, cwd=cwd)
    except ConfigError as exc:
        exc.args = (f"{exc}; repair the selected config and retry migrate-config",)
        raise
    if version == SCHEMA_VERSION:
        return False
    fs.write(path.with_name(path.name + ".bak"), original)
    fs.write(path, candidate.encode("utf-8"))
    return True


def _check_schema_version(path: Path, raw: dict[str, Any]) -> None:
    # Runs before full validation so a newer config's new keys report as "newer", not "unknown key".
    if "schema_version" not in raw:
        raise ConfigError(
            path, "schema_version", f"missing required key; this engine reads schema_version: {SCHEMA_VERSION}"
        )
    version = raw["schema_version"]
    if type(version) is not int or version < 0:
        raise ConfigError(path, "schema_version", f"must be a non-negative integer, got {version!r}")
    if version > SCHEMA_VERSION:
        raise ConfigError(
            path,
            "schema_version",
            f"{version} is newer than this engine's {SCHEMA_VERSION}; upgrade tempo to a release that reads it",
        )
    if version < SCHEMA_VERSION:
        raise ConfigError(
            path,
            "schema_version",
            f"{version} is older than this engine's {SCHEMA_VERSION}; run `tempo migrate-config` to migrate it"
            f" (the 0->1 step only rewrites schema_version to 1, every other key unchanged)",
        )


def _find_null(node: Any, prefix: str) -> str | None:
    items = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()
    for k, v in items:
        key = f"{prefix}.{k}" if prefix else str(k)
        if v is None:
            return key
        if (found := _find_null(v, key)) is not None:
            return found
    return None


def _from_validation(path: Path, e: ValidationError) -> ConfigError:
    lines = []
    for err in e.errors():
        key = ".".join(str(part) for part in err["loc"] if part != "[key]")
        msg = {
            "missing": "missing required key",
            "extra_forbidden": f"unknown key (not in the schema_version {SCHEMA_VERSION} schema)",
        }.get(err["type"], err["msg"])
        lines.append((key, msg))
    first_key, first_msg = lines[0]
    rest = "".join(f"\n  {key}: {msg}" for key, msg in lines[1:])
    return ConfigError(path, first_key, first_msg + rest)


def _check_references(path: Path, cfg: Config) -> None:
    kinds = {p.name: p.kind for p in cfg.providers}
    # Before the api refusal: routing Implement to api stays wrong after the api client ships.
    for i, route in enumerate(cfg.routing):
        for j, cand in enumerate(route.candidates):
            if route.surface == "implement" and kinds.get(cand.provider, "cli") != "cli":
                raise ConfigError(
                    path,
                    f"routing.{i}.candidates.{j}.provider",
                    f"implement must be served by a kind: cli provider ({cand.provider!r} is"
                    f" {kinds[cand.provider]}); an api provider returns text and cannot edit the worktree",
                )
    for i, provider in enumerate(cfg.providers):
        if provider.kind == "api":
            raise ConfigError(
                path,
                f"providers.{i}.kind",
                "kind: api is refused until the api client ships (section 6); use kind: cli",
            )
    declared = {p.name for p in cfg.providers}
    for i, route in enumerate(cfg.routing):
        for j, cand in enumerate(route.candidates):
            if cand.provider not in declared:
                raise ConfigError(
                    path,
                    f"routing.{i}.candidates.{j}.provider",
                    f"{cand.provider!r} is not a declared provider; declare it under providers or use one of"
                    f" {sorted(declared)}",
                )
    if cfg.caps.retry > cfg.caps.diagnosis:
        raise ConfigError(
            path,
            "caps.retry",
            f"{cfg.caps.retry} exceeds caps.diagnosis ({cfg.caps.diagnosis}); set retry at or below it",
        )
