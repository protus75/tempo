"""`tempo init`: install the governing layout shipped in the wheel's data files into a host repo.

A wheel's data files land under the install prefix (`<venv>/share/tempo/rules/`), never in a project
root, so this command is the step that places them at the host's top level.
"""

from __future__ import annotations

import json
import sys
import sysconfig
from pathlib import Path

from tempo.render import BLOCK_FILES, BEGIN, END, host_of, merge_block

# Host-owned JSON files: tempo's entries merge in; the host's own entries are kept.
MERGED_JSON = {".claude/settings.json", ".mcp.json", ".vscode/mcp.json", ".codex/hooks.json"}


def rules_dir() -> Path:
    path = Path(sysconfig.get_path("data")) / "share" / "tempo" / "rules"
    if not path.is_dir():
        raise SystemExit(f"tempo rules not found at {path}; install the wheel (uv tool install tempo-agents).")
    return path


def _merge(base, extra):
    if isinstance(base, dict) and isinstance(extra, dict):
        out = dict(base)
        for key, value in extra.items():
            out[key] = _merge(base[key], value) if key in base else value
        return out
    if isinstance(base, list) and isinstance(extra, list):
        return base + [item for item in extra if item not in base]
    return extra


def _extract_block(text: str) -> str:
    start, end = text.find(BEGIN), text.find(END)
    return text[start:end + len(END)] + "\n"


def install(root: Path, hosts: set[str], force: bool = False, source: Path | None = None) -> list[str]:
    source = source or rules_dir()
    report = []
    for src in sorted(p for p in source.rglob("*") if p.is_file()):
        rel = src.relative_to(source).as_posix()
        if host_of(rel) not in hosts:
            continue
        target = root / rel
        new = src.read_text(encoding="utf-8")
        old = target.read_text(encoding="utf-8") if target.exists() else None
        if rel in BLOCK_FILES:
            new = merge_block(old, _extract_block(new))
        elif rel in MERGED_JSON and old is not None:
            new = json.dumps(_merge(json.loads(old), json.loads(new)), indent=2) + "\n"
        elif old is not None and old != new and not force:
            report.append(f"skipped {rel}: differs from tempo's copy; merge {src} by hand or rerun with --force")
            continue
        if old == new:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new, encoding="utf-8")
        report.append(f"wrote {rel}")
    ignore = root / ".gitignore"
    lines = ignore.read_text(encoding="utf-8").splitlines() if ignore.exists() else []
    if ".tempo/" not in lines:
        ignore.write_text("\n".join([*lines, ".tempo/"]) + "\n", encoding="utf-8")
        report.append("wrote .gitignore (.tempo/)")
    return report


def main(root: Path, hosts: set[str], force: bool) -> int:
    for line in install(root, hosts, force):
        print(line)
    if "codex" in hosts:
        print("codex: trust this project and review its hooks with /hooks before they run.", file=sys.stderr)
    return 0
