import json
import re
import sysconfig
import tomllib
from pathlib import Path

import pytest

from tempo.init import install
from tempo.render import BEGIN, END, expected, layout, write
from tempo.roles import roles

REPO = Path(__file__).resolve().parents[1]


def test_rendered_files_have_not_drifted():
    assert write(REPO, check=True) == [], "run `uv run tempo render`"


def test_subagents_never_hold_a_spawn_tool():
    files = layout()
    for role in roles().values():
        if not role.subagent:
            continue
        claude = files[f".claude/agents/{role.agent_name}.md"]
        tools = re.search(r"^tools: (.*)$", claude, re.M).group(1).split(", ")
        assert not {"Agent", "Task", "Bash"} & set(tools) and not any(t.startswith("Agent(") for t in tools)
        copilot = files[f".github/agents/{role.agent_name}.agent.md"]
        assert set(json.loads(re.search(r"^tools: (.*)$", copilot, re.M).group(1))) == {"read", "search", "edit"}
        assert "agents:" not in copilot


def test_codex_config_caps_subagent_threads():
    config = tomllib.loads(layout()[".codex/config.toml"])
    assert config["agents"]["max_concurrent_threads_per_session"] == 1
    for path, text in layout().items():
        if path.endswith(".toml"):
            tomllib.loads(text)


def test_wheel_shared_data_covers_exactly_the_layout():
    table = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["hatch"]["build"]["targets"]["wheel"]["shared-data"]
    shipped = set()
    for src, dest in table.items():
        assert dest == f"share/tempo/rules/{src}"
        path = REPO / src
        shipped |= {p.relative_to(REPO).as_posix() for p in path.rglob("*") if p.is_file()} if path.is_dir() else {src}
    assert shipped == set(expected(REPO))


@pytest.fixture
def rules() -> Path:
    path = Path(sysconfig.get_path("data")) / "share" / "tempo" / "rules"
    if not path.is_dir():
        pytest.skip("tempo is not installed with its data files")
    return path


def test_init_merges_host_files(tmp_path, rules):
    (tmp_path / "CLAUDE.md").write_text("# host rules\n\nKeep tabs.\n")
    (tmp_path / ".claude").mkdir()
    host_hook = {"matcher": "Bash", "hooks": [{"type": "command", "command": "lint"}]}
    (tmp_path / ".claude/settings.json").write_text(json.dumps({"model": "x", "hooks": {"PreToolUse": [host_hook]}}))
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex/config.toml").write_text('model = "y"\n')

    report = install(tmp_path, {"claude", "copilot", "codex"}, source=rules)

    claude = (tmp_path / "CLAUDE.md").read_text()
    assert claude.startswith(BEGIN.split(" (")[0]) and END in claude and "Keep tabs." in claude
    assert "Engine conduct" not in claude  # only the protocol block ships into hosts
    settings = json.loads((tmp_path / ".claude/settings.json").read_text())
    assert settings["model"] == "x" and settings["hooks"]["PreToolUse"][0] == host_hook
    assert len(settings["hooks"]["PreToolUse"]) == 2
    assert any("skipped .codex/config.toml" in line for line in report)
    assert (tmp_path / ".github/agents/tempo-orchestrator.agent.md").exists()
    assert ".tempo/" in (tmp_path / ".gitignore").read_text()

    assert install(tmp_path, {"claude"}, source=rules) == []  # idempotent
