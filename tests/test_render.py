"""`tempo render` and `render --check` (AGENT_PLAN.md sections 4.4.2, 5)."""

import json

import pytest

from tempo import hostfiles, render
from tempo.__main__ import main
from tempo.checkout import Refusal
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
                      "tempo-orchestrator.md", "tempo-requisition_review-claude-opus-5-5.md",
                      "tempo-retro-claude-sonnet-5-5.md", "tempo-review-claude-opus-5-5.md",
                      "tempo-triage-claude-sonnet-5-5.md"]
    # Codex carries effort; max maps to its highest level, and review runs only on the pinned model.
    codex = {p.name for p in (repo / ".codex/agents").iterdir()}
    assert "tempo-review-gpt-6-astra-max.toml" in codex
    assert not any(n.startswith("tempo-review-gpt-6.1-sol") for n in codex)
    toml = (repo / ".codex/agents/tempo-implement-gpt-6.1-sol-high.toml").read_text()
    assert 'model = "gpt-6.1-sol"' in toml and 'model_reasoning_effort = "high"' in toml
    # Codex's start command is a skill, which names no model.
    skill = (repo / ".agents/skills/tempo-run/SKILL.md").read_text()
    assert skill.startswith("---\nname: tempo-run\n") and "model" not in skill.split("---")[1]
    assert ".codex/tempo-orchestrator.md" in skill
    assert (repo / ".github/agents/tempo-review-claude-opus-5-5.agent.md").is_file()
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
    text = "".join(lines[:start] + lines[start + 4:]).replace("codex: gpt-6-astra, ", "")
    (repo / "config.yaml").write_text(text)
    render.write(repo, config(repo), FS)
    assert not list((repo / ".codex/agents").glob("tempo-*"))
    assert not (repo / ".codex/tempo-orchestrator.md").exists()
    assert not (repo / ".agents/skills/tempo-run/SKILL.md").exists()
    assert (repo / ".codex/hooks.json").is_file()


def test_render_cli_writes_then_check_passes(repo, capsys):
    assert main(["render", "--check"], cwd=repo) == 2
    assert "stale: .claude/settings.json" in capsys.readouterr().out
    assert main(["render"], cwd=repo) == 0
    assert main(["render", "--check"], cwd=repo) == 0
    assert capsys.readouterr().out.endswith("render: current\n")


def test_session_block_lands_in_all_three_instruction_files_beside_core(repo):
    render.write(repo, config(repo), FS)
    for rel in ("CLAUDE.md", "AGENTS.md", ".github/copilot-instructions.md"):
        data = (repo / rel).read_bytes()
        assert hostfiles.parse(data, name="session") is not None, rel
        assert data.count(b"## Chat session conduct") == 1
    assert hostfiles.classify_core((repo / "CLAUDE.md").read_bytes()).state == "current"


def test_hook_file_keeps_the_repos_own_keys(repo):
    (repo / ".claude").mkdir()
    (repo / ".claude/settings.json").write_text('{"permissions": {"allow": ["Bash(ls)"]}, "hooks": {"x": []}}\n')
    render.write(repo, config(repo), FS)
    settings = json.loads((repo / ".claude/settings.json").read_text())
    assert settings["permissions"] == {"allow": ["Bash(ls)"]}
    assert settings["hooks"] == render.hook_config("claude")["hooks"]
    assert render.drift(repo, config(repo)) == []


def test_a_hook_file_that_is_not_a_json_object_is_refused(repo):
    (repo / ".claude").mkdir()
    (repo / ".claude/settings.json").write_text("[1]\n")
    assert ".claude/settings.json" in render.drift(repo, config(repo))
    with pytest.raises(Refusal, match="not a JSON object"):
        render.write(repo, config(repo), FS)
    assert (repo / ".claude/settings.json").read_text() == "[1]\n"


def test_init_into_an_empty_repo_writes_the_layout_and_a_second_run_changes_nothing(tmp_path, capsys):
    assert main(["init"], cwd=tmp_path) == 0
    assert (tmp_path / "config.yaml").read_bytes() == (render.RULES / "config.yaml").read_bytes()
    assert (tmp_path / "docs/tempo/tickets.md").read_bytes() == (render.RULES / "tickets.md").read_bytes()
    for rel in ("CLAUDE.md", "AGENTS.md", ".github/copilot-instructions.md"):
        assert hostfiles.parse((tmp_path / rel).read_bytes(), name="session") is not None, rel
    for rel in ("CLAUDE.md", "AGENTS.md"):  # the default config's hosts get tempo:core
        assert hostfiles.parse((tmp_path / rel).read_bytes()) is not None, rel
    for rel in (".claude/settings.json", ".claude/agents/tempo-orchestrator.md", ".claude/commands/tempo-run.md",
                ".codex/hooks.json", ".codex/tempo-orchestrator.md", ".agents/skills/tempo-run/SKILL.md",
                ".github/hooks/tempo.json"):
        assert (tmp_path / rel).is_file(), rel
    snapshot = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    capsys.readouterr()
    assert main(["init"], cwd=tmp_path) == 0
    assert capsys.readouterr().out == "init: current\n"
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == snapshot


def test_init_keeps_an_existing_config_and_reports_an_edited_ticket_doc(repo, capsys):
    kept = (repo / "config.yaml").read_bytes()
    (repo / "docs/tempo").mkdir(parents=True)
    (repo / "docs/tempo/tickets.md").write_text("ours\n")
    assert main(["init"], cwd=repo) == 0
    out = capsys.readouterr().out
    assert (repo / "config.yaml").read_bytes() == kept and "wrote: config.yaml" not in out
    assert (repo / "docs/tempo/tickets.md").read_text() == "ours\n"
    assert "skipped: docs/tempo/tickets.md" in out


def test_init_ignores_the_state_dir_once(tmp_path):
    assert main(["init"], cwd=tmp_path) == 0
    assert (tmp_path / ".gitignore").read_text() == "/.tempo/state/\n"
    assert main(["init"], cwd=tmp_path) == 0
    assert (tmp_path / ".gitignore").read_text() == "/.tempo/state/\n"


def test_init_appends_to_an_existing_gitignore_and_ignores_a_separate_worktree_root(repo):
    (repo / ".gitignore").write_text(".venv/")
    with (repo / "config.yaml").open("a") as f:
        f.write("worktree_root: .trees\n")
    assert main(["init"], cwd=repo) == 0
    assert (repo / ".gitignore").read_text() == ".venv/\n/.tempo/state/\n/.trees/\n"


def test_init_adds_no_line_for_a_state_dir_outside_the_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "config.yaml").write_text((FIXTURE / "config.yaml").read_text().replace(
        "state_dir: .tempo/state", f"state_dir: {tmp_path / 'state'}"))
    assert main(["init"], cwd=repo) == 0
    assert not (repo / ".gitignore").exists()
