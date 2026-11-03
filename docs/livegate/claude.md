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

## P7 live runs

**Result: exit met.** Run 2026-11-03 in `~/livegate/p7` (setup in AGENT_PLAN.md section 8 P7), with one
`/tempo-run` in Manual mode (`permission_mode: "default"` on every hook payload). Nothing asked for
permission. All three tickets merged, and each of the 15 spawns ran on the model its agent file names
(`resolvedModel`).

| Ticket | Merge | Attempts | What it showed |
|---|---|---|---|
| `add-subtract` | `aa081a4` | 1 | End to end: requisition_review, implement, check, review, merge. |
| `big-context` | `18997bd` | 1 | Round trip at the bound: a 399,493-character requisition_review prompt in 20 parts, all 20 read, approved. Implement and review each had 11 parts. Implement read all 11. Review read 5 and grepped the other 6 (bulk lines only). `final_code()` returns the right code. |
| `retry-escalate` | `54947b8` | 4 | Retry and escalate decided by the orchestrator (Opus). Details below. |

`retry-escalate`, attempt by attempt:

- **Attempt 0.** Requisition_review on Haiku snagged the out-of-fence `gate.py` criterion. The
  orchestrator chose `retry` and wrote a lesson explaining the rig.
- **Attempt 1.** Haiku snagged again. The orchestrator chose `escalate`, so the rung became
  `medium` (Sonnet).
- **Attempt 2.** The Sonnet requisition_review approved on its second call; the first call wrote no
  reply (see below). Implement passed. Check failed on `gate.py` run 2, as designed, and the
  orchestrator chose `retry`.
- **Attempt 3.** `gate.py` passed on run 4. Review approved, and the ticket merged.

Implement never ran on Haiku: the escalate came at requisition_review. `IDENTICAL_K`'s retry refusal was
not reached, because the failure reasons differed between attempts.

### Findings

- **Defect: a failure at requisition_review loses its harvest.** `Harvest.stage` (`tempo/artifacts.py`)
  allows only `implement`, `check`, `review`, `merge`, so `_decide` journaled `harvest_failed` on attempts 0
  and 1. `read_attempts` then showed no findings for those attempts. The orchestrator filed this to the
  box, and triage closed it for want of a repro. Fixed in `f716130`: the stage is allowed, and
  `read_attempts` falls back to the harvest's findings.
- **A subagent skipped `output.txt`.** On attempt 2, the Sonnet requisition_review put its verdict in
  its final message (saved to `stop.txt`) and left `output.txt` empty. `submit` classed the call a
  failure, and prepare re-prompted. That cost one call, and the verdict was lost. Cause: the specs said
  "Reply with ONLY one JSON object". Fixed in `b9fb300`: every spec says to write it to the `reply:` file.
- **Haiku is a weak requisition_review.** Requisition_review runs at the ticket's tier. Haiku twice
  read an intentional rig as a spec gap, even after the retry lesson explained it. Fixed in `e46853e`:
  `pins.requisition_review` is required and outside `models_by_tier`, like `pins.review`.
