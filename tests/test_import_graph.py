"""The kept set, `__main__` included since P5a, never imports what is left of the scripted layer (AGENT_PLAN.md
P3, P5a), counting every import statement: module-level, function-level, and TYPE_CHECKING alike, followed
transitively. `rework` stays, unused, until P5b."""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "tempo"
EXCLUDED = frozenset({"rework"})


def _edges(package: Path = PACKAGE) -> dict[str, set[str]]:
    modules = {p.stem for p in package.glob("*.py")}
    edges: dict[str, set[str]] = {}
    for path in package.glob("*.py"):
        targets = edges.setdefault(path.stem, set())
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [f"tempo.{node.module or ''}"] + [f"tempo.{a.name}" for a in node.names]
            else:
                continue
            for name in names:
                parts = name.split(".")
                if parts[0] == "tempo" and len(parts) > 1 and parts[1] in modules:
                    targets.add(parts[1])
    return edges


def _path_to_excluded(start: str, edges: dict[str, set[str]]) -> list[str] | None:
    seen, stack = {start}, [[start]]
    while stack:
        path = stack.pop()
        for target in sorted(edges[path[-1]]):
            if target in EXCLUDED:
                return path + [target]
            if target not in seen:
                seen.add(target)
                stack.append(path + [target])
    return None


def test_kept_set_never_reaches_the_scripted_layer():
    edges = _edges()
    kept = sorted(set(edges) - EXCLUDED)
    leaks = {m: path for m in kept if (path := _path_to_excluded(m, edges))}
    assert not leaks, "\n".join(" -> ".join(p) for p in leaks.values())


def test_the_walk_counts_function_level_and_type_checking_imports(tmp_path):
    for name in ("kept", "daemon", "runner", "drain"):
        (tmp_path / f"{name}.py").write_text("")
    (tmp_path / "kept.py").write_text(
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from tempo.daemon import X\n"
        "def f():\n    from tempo import runner\n    import tempo.drain\n")
    assert _edges(tmp_path)["kept"] == {"daemon", "runner", "drain"}
