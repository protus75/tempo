"""Closed host reports and bounded inbox ingestion."""

import hashlib
import json
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from tempo.specs import RENDER_BOUND_CHARS

CAPS = {"replay": 1024 * 1024, "log": 64 * 1024}
NonBlank = Annotated[str, Field(min_length=1, pattern=r"\S")]


class ReportError(ValueError):
    """Leave the arrival available for operator repair."""


def relative(value: str) -> str:
    path = Path(value)
    if not value.strip() or path.is_absolute() or ".." in path.parts or "\\" in value or "\0" in value:
        raise ValueError("use a repo-relative path without traversal")
    return value


class Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class EvidencePaths(Strict):
    replay: str | None = None
    log: str | None = None

    @field_validator("replay", "log")
    @classmethod
    def paths(cls, value):
        return relative(value) if value is not None else None


class PlayerReport(Strict):
    schema_version: Annotated[int, Field(ge=0, le=1)]
    origin: Literal["player"]
    summary: NonBlank
    app_commit: NonBlank
    app_version: NonBlank
    implicated_paths: list[str] = Field(default_factory=list)
    evidence: EvidencePaths = Field(default_factory=EvidencePaths)

    @field_validator("implicated_paths")
    @classmethod
    def paths(cls, values):
        return [relative(value) for value in values]


class SelfReport(PlayerReport):
    origin: Literal["self_diagnosed"]
    game_slug: NonBlank
    anomaly_code: NonBlank
    location: NonBlank


Report = Annotated[PlayerReport | SelfReport, Field(discriminator="origin")]
REPORT = TypeAdapter(Report)


class ReportDescriptor(Strict):
    report: Report
    custody: EvidencePaths


def validate_report(data: bytes) -> PlayerReport | SelfReport:
    try:
        return REPORT.validate_json(data)
    except ValueError as exc:
        raise ReportError(f"report_schema: {exc}; emit the closed version-1 report schema "
                          "(version 0 accepts the same fields)") from None


def bounded(root: Path, value: str, kind: str, *, metadata=Path.stat, read=Path.read_bytes) -> bytes:
    road = f"attach a host-contained regular {kind} file of at most {CAPS[kind]} bytes"
    try:
        path = (root / relative(value)).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError("path escapes root")
        info = metadata(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > CAPS[kind]:
            raise ValueError("non-regular or over-limit evidence")
        data = read(path)
        if len(data) > CAPS[kind]:
            raise ValueError("evidence grew beyond bound after metadata read")
        if kind == "replay":
            record = json.loads(data)
            if (not isinstance(record, dict) or "initial_state" not in record
                    or not isinstance(record.get("actions"), list)):
                raise ValueError("replay needs initial_state and an ordered actions list")
        return data
    except (OSError, ValueError) as exc:
        raise ReportError(f"report_evidence: {exc}; {road}") from None


def report_data(descriptor: ReportDescriptor, root: Path) -> dict:
    evidence = {}
    # Custody caps exceed the prompt cap. Keep full files for Author's copy and
    # leave headroom for the existing ticket/registry inputs and JSON escaping.
    budget = RENDER_BOUND_CHARS // (4 * len(CAPS))
    for kind in CAPS:
        if (value := getattr(descriptor.custody, kind)) is None:
            continue
        text = bounded(root, value, kind).decode(errors="replace")
        excerpt = text[:budget]
        while len(json.dumps(excerpt)) > budget:
            excerpt = excerpt[:len(excerpt) // 2]
        if excerpt != text:
            excerpt += f"\n[excerpt truncated; full evidence retained at {value}]"
        evidence[kind] = excerpt
    return {"report": descriptor.report.model_dump(), "custody": descriptor.custody.model_dump(),
            "evidence": evidence}


def ingest(box, *, root: Path, inbox: Path | None, diagnostic: Callable[[str], None],
           metadata=Path.stat, read=Path.read_bytes) -> list[str]:
    if inbox is None:
        return []
    settled = []
    for path in sorted(inbox.glob("*.json")):
        # Producers publish a fresh filename per arrival; retry uses that same name.
        arrival = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
        receipt = box.root / "custody" / arrival / "consumed"
        if receipt.exists():
            continue
        try:
            descriptor = box.report_custody(arrival)
            if descriptor is None:
                report = validate_report(read(path))
                evidence = {kind: bounded(root, value, kind, metadata=metadata, read=read) for kind in CAPS
                            if (value := getattr(report.evidence, kind)) is not None}
                descriptor = box.custody_report(arrival, report, evidence)
            id, _ = box.enqueue_report(descriptor, occurrence_id=arrival)
            box.fs.write(receipt, id.encode())
            settled.append(id)
        except (OSError, ValueError) as exc:
            diagnostic(f"report_intake {path.name}: {exc}; repair this arrival and retry triage")
    return settled
