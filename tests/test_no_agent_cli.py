"""No code path in tempo/ spawns an agent CLI (AGENT_PLAN.md R15, P5a exit): no argv tempo builds starts with
an agent binary, whether as a list or tuple literal or as the first argument of a call. A sequence made only of
binary names is a vocabulary of host names, not an argv."""

import ast
from pathlib import Path

from tempo.hooks import AGENT_CLIS

PACKAGE = Path(__file__).resolve().parent.parent / "tempo"


def _agent(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        name = Path(node.value).name
        return name if name in AGENT_CLIS else None
    return None


def argv_heads(package: Path = PACKAGE) -> list[str]:
    found = []
    for path in sorted(package.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
                if all(_agent(e) is not None for e in node.elts):
                    continue
                head = node.elts[0]
            elif isinstance(node, ast.Call) and node.args:
                head = node.args[0]
            else:
                continue
            if (name := _agent(head)) is not None:
                found.append(f"{path.name}:{node.lineno}: {name}")
    return found


def test_no_argv_in_tempo_starts_with_an_agent_binary():
    assert argv_heads() == []


def test_the_scan_finds_list_tuple_and_call_argv(tmp_path):
    (tmp_path / "spawn.py").write_text(
        'a = ["claude", "-p"]\nb = ("/usr/bin/codex", "exec")\nrun("copilot", "--prompt")\nc = ["git", "status"]\n'
        'HOSTS = ("claude", "codex")\n')
    assert argv_heads(tmp_path) == ["spawn.py:1: claude", "spawn.py:2: codex", "spawn.py:3: copilot"]
