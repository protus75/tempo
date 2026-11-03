# Live gate: Claude Code (P4c)

**Result: passes.** It is the one default host. Two items were not run live: a round trip with a prompt
at `RENDER_BOUND_CHARS`, and a real provider usage limit (a turn limit was forced instead).

Run 2026-11-02 in the Claude Code VS Code extension. The fixture repos were made with
`uv run python -m tests.fixturerepo ~/livegate/<name>` against an editable install of this checkout.
The payloads are in `tests/captured/claude/`, with repo paths and chat ids replaced by placeholders.
`tests/test_hooks_captured.py` replays them.

## Pass criteria

| Criterion | Evidence |
|---|---|
| Six hook cases | `tests/test_hooks_captured.py`, on captured payloads: nested spawn denied; subagent read and write confined to its call; spawn must name the prepared agent and prompt, in the foreground; orchestrator shell (non-`tempo tool`), write, and message denied; altered payloads that do not parse denied (exit 2). Submit of an unissued call: `tests/test_tools.py`. |
| Implement confinement | Live: implement wrote `calc.py` and `test_calc.py` and ran `python3 test_calc.py` inside its worktree, and all of it was allowed. The denials (a `/tmp` write, a `ticket.md` edit, `git status`, `claude --version`) were checked on altered captured implement payloads. The live implement subagent declined to try them, and the ticket text asking it to was treated as data. In the first run, the host honored tempo's deny inside a subagent (`SubagentHandback`). |
| Round trip | `/tempo-run` ran requisition_review, implement, check, review, and merge on `add-subtract` to a merge commit, three times (repos claude2, claude3, claude5). |
| Limit markers | A subagent stopped by a limit sends **no** `SubagentStop`. The spawn's `PostToolUse` result carries the notice ("stopped at its 1-turn limit"), and the hook now writes it to `stop.txt` (AGENT_PLAN.md section 3.1). The limit was forced with `maxTurns: 1`; a real usage-limit message was not observed. |
| Subagent stop tool | `TaskStop` with `{"task_id": <agentId>}` stopped a running subagent. It is deferred: the chat loads it with `ToolSearch`, which the hook allows. |
| No prompts on default approvals | claude5 ran in **Manual** mode (the extension's default; `permission_mode: "default"`) and asked nothing. claude4, before `begin_session` was allowed by the hook, asked once, for `tempo tool begin_session`. |

## Measurements

- **Whole-file Read:** a one-line file returned its first 21,249 characters (the 25,000-token cap). A
  file over 256 KB was refused. This sets `READ_PART_CHARS` = 20,000.
- **Shell timeout:** a command running longer than 120 s is moved to the background, not killed. This
  sets `TOOL_CALL_S` = 90.0.
- **Hook failure:** a hook that exits 1 or times out lets the call through, even with
  `"onFailure": "block"`. Exit 2 blocks. Tempo therefore no longer writes `onFailure`.

## Payload facts

- Every event carries `hook_event_name`, `session_id` (the chat), `cwd`, `permission_mode`, and
  `transcript_path`. Inside a subagent, `PreToolUse` also carries `agent_id` and `agent_type`.
- `SubagentStop` has `last_assistant_message` only when the subagent ended without `SubagentHandback`.
  No event names the model except `PostToolUse` on `Agent`: `tool_response.resolvedModel`, beside
  `tool_response.content[].text`.
- Tool names: `Read`, `Write`, `Bash`, `Agent` (`subagent_type` optional, `run_in_background`),
  `SubagentHandback` (a subagent's final report), `SendMessage`, `TaskStop`, `ToolSearch`, `ListAgents`.

## Defects the gate found, all fixed in P4c

- The engine log the hook writes was counted as a stray write, so every `submit` failed.
- `SubagentHandback` and `ToolSearch` were denied.
- A spawn that names no `subagent_type` did not parse, which blocked plain chats too.
- `begin_session` prompted on default approvals.
- Limit notices never reached `stop.txt`.
