"""`tempo render` and `render --check` (AGENT_PLAN.md sections 4.4.2, 5)."""

import json

import pytest

from tempo import hostfiles, render
from tempo.__main__ import main
from tempo.config import load_config
from tempo.seams import LocalFileSystem
from tests.fixturerepo import FIXTURE

FS = LocalFileSystem()


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "config.yaml").write_text((FIXTURE / "config.yaml").read_text())
    (tmp_path / "CLAUDE.md").write_bytes(hostfiles.render(b"project rules\n"))
    return tmp_path


def config(repo):
    return load_config(None, cwd=repo)


def test_render_writes_agents_for_reachable_hosts_and_hooks_for_all(repo):
    render.write(repo, config(repo), FS)
    agents = sorted(p.name for p in (repo / ".claude/agents").iterdir())
    assert agents == ["tempo-author-claude-sonnet-5-5.md", "tempo-implement-claude-sonnet-5-5.md",
                      "tempo-orchestrator.md", "tempo-requisition_review-claude-sonnet-5-5.md",
                      "tempo-retro-claude-sonnet-5-5.md", "tempo-review-claude-opus-5-5.md",
                      "tempo-triage-claude-sonnet-5-5.md"]
    # Codex carries effort; max maps to its highest level, and review runs only on the pinned model.
    codex = {p.name for p in (repo / ".codex/agents").iterdir()}
    assert "tempo-review-gpt-6-astra-xhigh.toml" in codex
    assert not any(n.startswith("tempo-review-gpt-6.1-sol") for n in codex)
    toml = (repo / ".codex/agents/tempo-implement-gpt-6.1-sol-high.toml").read_text()
    assert 'model = "gpt-6.1-sol"' in toml and 'model_reasoning_effort = "high"' in toml
    # Copilot is not reachable: no model-bearing files, but its hooks and protocol block are written.
    assert not (repo / ".github/agents").exists()
    assert json.loads((repo / ".github/hooks/tempo.json").read_text())["hooks"]["PreToolUse"][0]["command"] == "tempo hook copilot"
    for rel in ("CLAUDE.md", "AGENTS.md", ".github/copilot-instructions.md"):
        assert hostfiles.parse((repo / rel).read_bytes(), name="protocol") is not None, rel
    assert b"project rules" in (repo / "CLAUDE.md").read_bytes()
    assert hostfiles.parse((repo / "CLAUDE.md").read_bytes()) is not None  # tempo:core kept


def test_agent_files_are_thin_and_name_no_spawn_tool(repo):
    render.write(repo, config(repo), FS)
    review = (repo / ".claude/agents/tempo-review-claude-opus-5-5.md").read_text()
    assert "model: claude-opus-5-5" in review and "tools: Read, Grep, Glob, Write\n" in review
    implement = (repo / ".claude/agents/tempo-implement-claude-sonnet-5-5.md").read_text()
    assert "Bash" in implement and "never its `tickets/<stem>/ticket.md`" in implement
    for path in (repo / ".claude/agents").glob("tempo-*-*.md"):
        assert "Task" not in path.read_text().split("---")[1] and "Agent" not in path.read_text().split("---")[1]
    command = (repo / ".claude/commands/tempo-run.md").read_text()
    assert "model: claude-opus-5-5" in command and ".claude/agents/tempo-orchestrator.md" in command


def test_render_is_idempotent_and_check_finds_drift_and_stale_files(repo):
    assert render.drift(repo, config(repo))
    render.write(repo, config(repo), FS)
    assert render.drift(repo, config(repo)) == []
    assert render.write(repo, config(repo), FS) == []
    (repo / ".claude/settings.json").write_text("{}\n")
    (repo / ".claude/agents/tempo-review-retired-model.md").write_text("old\n")
    assert render.drift(repo, config(repo)) == [".claude/agents/tempo-review-retired-model.md",
                                                ".claude/settings.json"]
    assert render.write(repo, config(repo), FS) == [".claude/agents/tempo-review-retired-model.md",
                                                    ".claude/settings.json"]
    assert not (repo / ".claude/agents/tempo-review-retired-model.md").exists()


def test_a_host_dropped_from_orchestrator_models_loses_its_model_bearing_files(repo):
    render.write(repo, config(repo), FS)
    lines = (repo / "config.yaml").read_text().splitlines(keepends=True)
    start = lines.index("  codex:\n")
    text = "".join(lines[:start] + lines[start + 4:]).replace("{claude: claude-opus-5-5, codex: gpt-6-astra}",
                                                             "{claude: claude-opus-5-5}")
    (repo / "config.yaml").write_text(text)
    render.write(repo, config(repo), FS)
    assert not list((repo / ".codex/agents").glob("tempo-*"))
    assert not (repo / ".codex/tempo-orchestrator.md").exists()
    assert (repo / ".codex/hooks.json").is_file()


def test_render_cli_writes_then_check_passes(repo, capsys):
    assert main(["render", "--check"], cwd=repo) == 2
    assert "stale: .claude/settings.json" in capsys.readouterr().out
    assert main(["render"], cwd=repo) == 0
    assert main(["render", "--check"], cwd=repo) == 0
    assert capsys.readouterr().out.endswith("render: current\n")
