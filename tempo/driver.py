"""The LLM stage contract and the seams every stage shares (TEMPO_PLAN.md section 5 invariant 2, section 6).

A stage differs only in spec (render), artifact type, gate list, and review hook. Every model call runs as a host
subagent through `tempo.call`: `prepare` renders and spools the prompt, `submit` unwraps one fence, validates,
and runs the gates (`check_reply`) and the review hook.
"""

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from tempo.artifacts import Finding
from tempo.config import Config, ConfigSnapshot, Severity
from tempo.effects import Effects
from tempo.enginelog import EngineLog
from tempo.gates import Gate, GateReport, merge_severity, run_gates
from tempo.journal import Journal
from tempo.redact import Redactor
from tempo.seams import Clock, FileSystem, LocalFileSystem

_FENCE = re.compile(r"\A\s*(`{3,})[\w+-]*[ \t]*\n(.*)\n\1[ \t]*\s*\Z", re.DOTALL)


def unwrap_fence(text: str) -> str:
    """Strip exactly ONE surrounding markdown code fence (optionally language-tagged)."""
    m = _FENCE.match(text)
    return m.group(2) if m else text


def unrendered(consumed: Any, findings: list[Finding]) -> str:
    raise RuntimeError("a stage rebuilt for submit never renders")


@dataclass(frozen=True)
class LlmStage:
    surface: str
    emits: type[BaseModel]
    gates: Sequence[Gate]
    render: Callable[[Any, list[Finding]], str]  # (consumed artifact, re-prompt findings) -> prompt
    review: Callable[[BaseModel, int], Awaitable[GateReport]] | None = None
    terminal_findings: frozenset[str] = frozenset()


class Spool:
    """Per-attempt capture at `<root>/<stem>/<attempt>/`; every byte passes the redactor."""

    def __init__(self, root: Path, redactor: Redactor, fs: FileSystem) -> None:
        self.root = root
        self._redactor = redactor
        self._fs = fs

    def write(self, stem: str, attempt: int, name: str, text: str) -> None:
        self._fs.write(self.root / stem / str(attempt) / name, self._redactor.scrub(text).encode())


class Driver:
    """The seams one checkout's stages share: journal, effects, spool, redactor, engine log, clock, severity."""

    def __init__(self, *, journal: Journal, redactor: Redactor, spool: Spool, log: EngineLog, clock: Clock,
                 severity: Mapping[str, Severity]) -> None:
        self.journal = journal
        self.effects = Effects(journal)
        self.redactor = redactor
        self.spool = spool
        self.log = log
        self.clock = clock
        self.severity = severity

    @classmethod
    def from_config(cls, config: Config | ConfigSnapshot, *, clock: Clock, fs: FileSystem | None = None) -> "Driver":
        """The production wiring: the redactor exists BEFORE any writer."""
        redactor = Redactor({})
        return cls(
            journal=Journal(config.state_dir, clock),
            redactor=redactor,
            spool=Spool(config.state_dir / "spools", redactor, fs or LocalFileSystem()),
            log=EngineLog(config.state_dir / "engine.log", redactor, clock),
            clock=clock,
            severity=merge_severity(config),
        )


@dataclass(frozen=True)
class Checked:
    """Steps 5-6 of one reply: `artifact` is None when validation failed."""

    artifact: BaseModel | None
    hard: list[Finding]
    soft: list[Finding]
    errors: int = 0


def check_reply(stage: LlmStage, text: str, workspace: Path, severity: Mapping[str, Severity]) -> Checked:
    """Unwrap one fence, validate against `emits`, run the gates; used by `submit`."""
    try:
        artifact = stage.emits.model_validate_json(unwrap_fence(text))
    except ValidationError as e:
        return Checked(None, [_invalid(stage, e)], [], e.error_count())
    gated = run_gates(stage.gates, artifact, workspace, severity=severity)
    return Checked(artifact, [f for report in gated.hard_failures for f in report.findings], gated.findings)


def _invalid(stage: LlmStage, e: ValidationError) -> Finding:
    detail = "; ".join(f"{'.'.join(map(str, err['loc'])) or '<root>'}: {err['msg']}" for err in e.errors())
    return Finding(
        code="invalid_artifact",
        message=f"output is not a valid {stage.emits.__name__}: {detail}",
        paved_road=f"reply with ONLY one JSON object matching the {stage.emits.__name__} schema, no prose",
    )
