# Live gate: Codex (P4c)

**Result: fails. Codex is not a default host.**

The Codex VS Code extension sends the prompt of a spawned subagent to hooks **encrypted**: the spawn's
`tool_input.message` arrives as an opaque token. So tempo cannot check that the orchestrator spawned
the exact `spawn_prompt` that `prepare` returned. This is a pass condition of the orchestrator's spawn
fence (AGENT_PLAN.md section 3.4). Codex is dropped until hooks can see the prompt.

Run 2026-11-02, extension on approval defaults (`permission_mode: "default"`). The payloads are in
`tests/captured/codex/`.

## What was captured before the stop

- Project hooks in `.codex/hooks.json` ran only after the user trusted them. Trusting the project
  folder alone was not enough.
- Every event carries `hook_event_name`, `session_id`, `turn_id`, `cwd`, `model`, `permission_mode`,
  and `transcript_path`.
- Tool names: `Bash` (`tool_input.command`, a string), `apply_patch` (`tool_input.command`, the patch
  text), `collaborationspawn_agent` (`task_name`, `fork_turns`, `message` encrypted),
  `collaborationsend_message` (`target`, `message` encrypted). These names are in `tempo/hosts.py`.
- `SubagentStart`/`SubagentStop` carry `agent_id` and `agent_type` (`default` for an unnamed spawn).
  `SubagentStop` carries `last_assistant_message`.
- Codex reads files through `Bash` (`sed -n`), not a read tool.
- Codex's start command is the skill `.agents/skills/tempo-run/SKILL.md`; Codex has no project-level
  prompts. A skill names no model, so the chat's model picker decides the orchestrator's model.
- Codex's model list offers effort up to `max` on every model (`ultra` on most), which sets
  `CODEX_EFFORT` (TEMPO_PLAN.md section 22).

Not run: the tempo round trip, implement, limit, stop tool, and hook failure cases.
