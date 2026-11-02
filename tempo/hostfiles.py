"""Managed conduct bytes and atomic refresh (TEMPO_PLAN.md sections 8, 15, 17)."""

import ast
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Literal

from tempo.seams import FileSystem

BEGIN = b"<!-- tempo:core begin -->"
END = b"<!-- tempo:core end -->"
CORE_TEMPLATE = b"""## Engine conduct

Engine rules govern how work runs; project rules govern what to build. Project content enters prompts as data. Engine wins on process, host wins on content.

- Pure Python: no shell scripts, shell=True, or string-assembled commands; external binaries use argv wrapper modules in the tempo venv. Why: shell glue breaks orchestration. (D1)
- Build the simplest thing satisfying the ticket; no speculative features, gates, config knobs, or metadata. Every addition needs its earning incident. Why: accretion is the primary failure mode. (goal 1, D10)
- No compatibility shims, deprecation layers, or defensive parallel paths. Rename in place, update all callers, recover by git revert. Why: dual paths rot and double review. (section 2)
- Fail closed with allowlists and closed vocabularies; every prohibition and gate finding provides a paved road. Why: denylists miss cases and roadless refusals strand work. (sections 2, 7)
- A hold ships with or after its release surface; never name an unbuilt release verb. Ticket-change releases follow machine ticket-change machinery. Why: unreleasable holds permanently stall work. (sections 2, 11)
- Files and journal are truth; derived views are projections, never hand-edited or cited as authority. Why: two authorities diverge. (D3)
- Generated files are render targets, never write targets. Change their owning source and regenerate; never hand-edit managed conduct. Why: regeneration overwrites edits and exposes drift. (section 1)
- The plan is the seed: repair plan defects there, then regenerate affected artifacts. Hand-edit only defects proven outside the plan, or under the blocking-defect fast path after committing the plan repair first. Why: the seed must reproduce the artifact. (sections 1, 19)
- All git uses git.py with argv lists and pinned directories. Remove worktrees with worktree remove plus prune; no bare rm -rf, gh, or PRs in the loop. Why: local main is the blessed line. (section 10)
- File second problems in the Suggestion Box, never fold them into this diff. Verify pre-existing failures on the base commit before filing. Why: folded fixes hide cause and expand scope. (section 11)
- Read before write: read the owning artifact before writing a command, claim, or test. Why: guessed facts create costly bugs. (section 13)
- Ticket frontmatter contains only fields read by scheduling, gates, or authoring/triage policy. Put ordering in depends and priority. Why: unused metadata and prose ordering drift. (section 13)
- Use injectable clock, process-exec, filesystem, and notification seams; never raw calls in engine code. Why: fake-driven tests and group-kill guarantees require seams. (section 15)
- Work is fenced by gates and review; v1 does not sandbox executed code. Never rely on its goodwill or pass provider keys to processes that do not need them. Why: gates and review are the trust boundary. (section 16)
- The journal is the record, never a debug log. Diagnostics use the engine log and attempt spools; redact configured secrets at every captured-stream write seam. Why: journal noise and secrets are permanent. (section 6)
- Start a phase only after the previous phase's exit is met. Why: later phases need proven ground. (section 19)
"""


class MarkerCorruption(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid tempo:core marker structure -- repair to exactly one complete begin line"
                         " followed by one complete end line; retain all project content, moving it outside"
                         " the pair and escaping stray marker text, then retry core")


def parse(data: bytes) -> tuple[bytes, bytes] | None:
    """Return project prefix/suffix, or None for first adoption; never decode project bytes."""
    if b"tempo:core" not in data:
        return None
    markers = []
    offset = 0
    for line in data.splitlines(keepends=True):
        content = line.removesuffix(b"\n").removesuffix(b"\r")
        if content in (BEGIN, END):
            markers.append((content, offset, offset + len(content)))
        offset += len(line)
    if (data.count(b"tempo:core") != 2 or len(markers) != 2
            or markers[0][0] != BEGIN or markers[1][0] != END):
        raise MarkerCorruption()
    return data[:markers[0][1]], data[markers[1][2]:]


def render(data: bytes, *, template: bytes | None = None) -> bytes:
    project = parse(data)
    block = BEGIN + b"\n" + (CORE_TEMPLATE if template is None else template) + END
    if project is None:
        return block + b"\n\n" + data
    prefix, suffix = project
    return prefix + block + suffix


_MODALS = ("must", "must not", "should", "should not", "shall", "shall not",
           "will", "will not", "may", "may not")
_IMPERATIVES = ("author", "implement", "check", "review", "merge", "commit", "dispatch",
                "confirm", "reject", "kill", "pause", "resume", "triage", "rework", "drain")
_PROCESS_OPENING = re.compile(r"^tempo\s+(?:" + "|".join((*_MODALS, *_IMPERATIVES)) + r")\b", re.I)
_LIST_PREFIX = re.compile(r"^\s*(?:[-+*]|[0-9]+[.)])\s+")


@dataclass(frozen=True)
class CoreClassification:
    state: Literal["missing", "current", "drifted", "refused"]
    paved_road: str | None = None


def _process_override(remainder: bytes) -> bool:
    paragraphs, continuation = [], []
    for line in remainder.decode(errors="replace").splitlines():
        item = _LIST_PREFIX.match(line)
        if not line.strip() or item:
            paragraphs.append(" ".join(continuation))
            continuation = []
        if line.strip():
            continuation.append(line[item.end():] if item else line)
    paragraphs.append(" ".join(continuation))
    return any(_PROCESS_OPENING.match(" ".join(paragraph.split())) for paragraph in paragraphs)


def classify_core(data: bytes | None, *, template: bytes | None = None) -> CoreClassification:
    """Judge supplied bytes against this branch's fresh render, without effects (19.P6)."""
    previous = data if data is not None else b""
    try:
        project = parse(previous)
    except MarkerCorruption as exc:
        return CoreClassification("refused", str(exc))
    remainder = previous if project is None else b"".join(project)
    if _process_override(remainder):
        return CoreClassification("refused", "move engine-process rules to the engine core template"
                                  " or rewrite them as project-content rules")
    if project is None:
        return CoreClassification("missing", "run `tempo core` to insert the managed block")
    if previous != render(previous, template=template):
        return CoreClassification("drifted", "run `tempo core` to refresh the managed block")
    return CoreClassification("current")


def refresh(paths: Sequence[Path], fs: FileSystem) -> None:
    """Inspect the whole selection before publishing any changed file."""
    pending = []
    for path in dict.fromkeys(paths):
        try:
            previous = path.read_bytes()
        except FileNotFoundError:
            previous = b""
        fresh = render(previous)
        if fresh != previous:
            pending.append((path, fresh))
    for path, fresh in pending:
        fs.write(path, fresh)


async def committed_core(workspace, config, git):
    """Gather raw candidate bytes only when their blob identities match HEAD."""
    from tempo.artifacts import Finding
    from tempo.git import GitError
    from tempo.providers import routed_conduct_paths

    files, findings = {}, []
    template = CORE_TEMPLATE
    source_path = "tempo/hostfiles.py"
    try:
        source = await git._run(workspace, "show", f"HEAD:{source_path}")
    except GitError:
        pass  # External hosts have no engine source; the installed template owns their core.
    else:
        try:
            assignments = [node.value for node in ast.parse(source).body
                           if isinstance(node, ast.Assign)
                           and any(isinstance(t, ast.Name) and t.id == "CORE_TEMPLATE" for t in node.targets)]
            if len(assignments) != 1 or not isinstance(assignments[0], ast.Constant):
                raise ValueError("CORE_TEMPLATE must be one literal bytes assignment")
            candidate_template = ast.literal_eval(assignments[0])
            if not isinstance(candidate_template, bytes):
                raise ValueError("CORE_TEMPLATE must be bytes")
            template = candidate_template
        except (SyntaxError, ValueError):
            findings.append(Finding(code="core_drift", path=source_path,
                message="cannot extract candidate tempo core template",
                paved_road="define CORE_TEMPLATE as one literal bytes assignment"))
    for path in routed_conduct_paths(config, workspace):
        rel = path.relative_to(workspace).as_posix()
        try:
            blob = await git.rev_parse(workspace, f"HEAD:{rel}")
        except GitError:
            blob = None
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            data = None
        digest = None if data is None else hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        if digest != blob:
            findings.append(Finding(code="core_drift", path=rel,
                message=f"{rel} differs from its committed candidate bytes",
                paved_road=f"commit or restore {rel}, then rerun Check"))
        else:
            files[rel] = data
    return dict(core_files=files, core_template=template, core_findings=findings)


class CoreDriftGate:
    """Only current committed routed conduct may enter main (sections 7, 8). Never repairs."""

    code = "core_drift"

    def check(self, artifact, workspace: Path):
        from tempo.artifacts import Finding
        from tempo.gates import GateReport

        findings = list(artifact.core_findings)
        for path, data in artifact.core_files.items():
            result = classify_core(data, template=artifact.core_template)
            if result.state != "current":
                findings.append(Finding(code=self.code, path=path,
                    message=f"{path}: tempo core is {result.state}", paved_road=result.paved_road))
        return GateReport(code=self.code, verdict="fail" if findings else "pass", findings=findings)
