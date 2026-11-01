# tempo

tempo runs ticket-driven software work with **one orchestrator agent and one level of subagents**.
Agents talk only through files and tickets on disk, and every handoff is checked mechanically. The
same rules govern **Claude Code**, **GitHub Copilot**, and **Codex**, each through its own native
rule files.

- [`AGENT_PLAN.md`](AGENT_PLAN.md) is the plan for the agent-based orchestrator and its governing system.
- [`TEMPO_PLAN.md`](TEMPO_PLAN.md) is the engine plan it amends (kernel, tickets, gates, journal).

## How communication stays constrained

| Handoff | Rule | Checked by |
|---|---|---|
| orchestrator → subagent | The spawn prompt is a JSON manifest of input paths: `{"subject", "attempt", "inputs"}`. Content is never pasted in. | `PreToolUse` hook, then the `prepare_inputs` tool |
| subagent → disk | A subagent may write only `.tempo/outbox/<role>/<subject>/<attempt>/reply.json`. | `PreToolUse` hook |
| subagent → orchestrator | The final reply is exactly `{"written": [...], "status": "ok"}`. Every listed file must exist and pass its role's schema. | `SubagentStop` hook, then the `accept` tool |
| subagent → subagent | Not allowed directly. Output reaches another agent only after `accept` lifts it into `tickets/`. | Manifest inputs may not point into `.tempo/outbox/` |
| subagent → spawn | Not allowed. There is one level only. | `PreToolUse` hook, plus agent tool lists |

## Governing layout

`tempo render` generates every file below from `src/tempo/agents/*.md` and
`src/tempo/rules/protocol.md`. Never hand-edit a rendered file. The test suite fails on drift.

| Host | Instructions | Agents | Hooks | MCP |
|---|---|---|---|---|
| Claude Code | `CLAUDE.md` | `.claude/agents/tempo-*.md` | `.claude/settings.json` | `.mcp.json` |
| GitHub Copilot | `.github/copilot-instructions.md` | `.github/agents/tempo-*.agent.md` | `.github/hooks/tempo.json` | `.vscode/mcp.json` |
| Codex | `AGENTS.md` | `.codex/agents/tempo-*.toml`, `.codex/tempo-orchestrator.md` | `.codex/hooks.json` | `.codex/config.toml` |

In `CLAUDE.md`, `AGENTS.md`, and `copilot-instructions.md`, tempo owns only the
`<!-- tempo:protocol -->` block. Anything outside that block is yours.

## Install into a host repo

```sh
uv tool install tempo-agents          # puts `tempo` on PATH; the hooks call it
cd /path/to/host-repo
tempo init                            # all hosts; or --host claude|copilot|codex
```

The wheel ships the layout as data files under `<prefix>/share/tempo/rules/`. Python installers
cannot write into a project root, so `tempo init` copies the files there:

- In the three instruction files, it merges the protocol block.
- In host-owned JSON configs, it merges tempo's entries.
- Any other file that differs from tempo's copy is skipped and reported, unless you pass `--force`.

Codex runs project hooks only after you trust them with `/hooks`.

## Develop

```sh
uv sync
uv run pytest
uv run tempo render          # after editing src/tempo/agents or src/tempo/rules
uv build                     # dist/tempo_agents-<v>-py3-none-any.whl + sdist
uv tool install -e .         # put this checkout's `tempo` on PATH so this repo's own hooks run
```

## Status

Built and tested so far:

- the role definitions
- the renderer for all three hosts
- the hook policy and its adapters
- `tempo init`
- packaging

The MCP tool server (`tempo mcp`), the kernel port, and the supervisor are later phases (see
`AGENT_PLAN.md` section 12).
