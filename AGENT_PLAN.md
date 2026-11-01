# tempo -- agent orchestration plan

This plan replaces tempo's scripted top level with one orchestrator agent. The scripted top level is
the `bootstrap/conductor.py` rungs, `tempo/drain.py`, the `tempo/daemon.py` dispatch loop,
`runner.drive`/`runner.run_ticket` dispatch, the capability ladder (`caps.next_rung` and the routing in
`runner.failure_terminal`), `stages.diagnose*`, and `rework.py` routing.

Everything that produces a verdict stays deterministic and becomes the orchestrator's tool surface.
Judgment goes in LLMs; verdicts go in scripts, applied one level up. The same governing system runs on
**Claude Code**, **GitHub Copilot**, and **Codex**.

This plan amends `TEMPO_PLAN.md` (the engine plan). Section numbers written "TEMPO_PLAN §n" refer to it.
On conflict this plan wins, and section 11 lists the edits it makes there.

## 1. Topology

- **Level 0: the orchestrator.** One host-CLI session per run: Claude Code (or the Claude Agent SDK),
  Copilot CLI, or Codex CLI. The supervisor (section 7) starts it with `TEMPO_ROLE=orchestrator`.
  - Its tools are the tempo MCP server (`tempo mcp`, stdio). This is a pure-Python wrapper over the
    kernel modules and is identical on all three hosts (D1 holds).
  - It is the only holder of the spawn tool.
- **Level 1: subagents, which are always leaves.** `author`, `requisition_review`,
  `correctness_review`, `triage`, and `retro`. They are host-native custom agents named
  `tempo-<role>`.
- **Implement is a tool, not a subagent.** `implement(stem)` runs the provider CLI through
  `tempo/providers.py` exactly as TEMPO_PLAN specifies.
  - The child runs with `TEMPO_ROLE=implement` and `TEMPO_FENCE=<ticket scope fence>`, and with
    multi-agent disabled (Codex: `agents.enabled = false`).
- **Diagnose is not a surface.** The orchestrator makes the post-failure call itself.

### 1.1 One level of subagents only

No subagent spawns, messages, or resumes another agent. Three things enforce this:

1. **Definitions.** Every subagent definition lists its tools explicitly, and no list contains a
   spawn tool.
   - Claude: no `Agent`/`Task`.
   - Copilot: no `agent`.
   - Codex: custom agents cannot drop the spawn tool, so items 2 and 3 carry the rule there.
   - `tempo.roles` refuses a subagent role that declares `spawns` or the `spawn` tool class.
2. **Hook.** The `PreToolUse` hook denies every spawn or agent-message call whose caller is a
   subagent, and logs the attempt to `.tempo/state/hooks/log.jsonl`. Section 8.4 explains how each
   host identifies the caller.
3. **Tests.** Every rendered subagent tool list excludes spawn tools. A nested spawn is refused for
   each host's payload shape. A spawned `tempo-orchestrator` is still a subagent and cannot spawn.

## 2. Communication: files and tickets only

Agents communicate and persist memory only through tickets, ordinary files on disk, and the journal
(TEMPO_PLAN §5 invariant 1, unchanged).

- **Input is paths.**
  - The orchestrator calls `prepare_inputs(role, subject)`. It writes every input under
    `.tempo/inbox/<role>/<subject>/<attempt>/`, with untrusted text wrapped in
    `<<tempo-data:begin name>>` blocks (TEMPO_PLAN §8), and returns a manifest.
  - The spawn prompt is exactly that manifest: `{"subject", "attempt", "inputs"}`, at most 4,096
    characters and 32 inputs.
  - Inputs must exist inside the repository and never point into `.tempo/outbox/` or `.tempo/state/`.
  - A spawn whose outbox reply for that attempt already exists is refused; bump the attempt.
- **Output is files.**
  - A subagent writes exactly `.tempo/outbox/<role>/<subject>/<attempt>/reply.json`, in its role's
    reply schema (section 2.1).
  - Its final message is only `{"written": [paths], "status": "ok"|"failed"}`. The orchestrator
    never acts on reply prose.
- **Accept lifts.** `accept(role, subject, attempt)` validates the reply file against its schema and
  the pending manifest. It then renders the ticket-plane artifact (for example `review.md` from the
  review reply, pinned to the reviewed SHA) and commits it through the single writer (TEMPO_PLAN §10).
  An invalid or missing file journals `invalid_artifact` for that attempt.
- **No direct subagent-to-subagent traffic.** One subagent's output reaches another only as an
  accepted file under `tickets/` that the orchestrator names in a later manifest.
- **Persistence.** Accepted files live in git and survive sessions and restarts. `.tempo/` is
  working state and is gitignored.

### 2.1 Who writes what

| Writer | Writes (allowlist) | Reply schema | Accepted into |
|---|---|---|---|
| author | `.tempo/outbox/author/*/*/reply.json` | `{stem, ticket}` | `tickets/<new-stem>/ticket.md` (staged; committed after `requisition_review` approves) |
| requisition_review | `.tempo/outbox/requisition_review/*/*/reply.json` | `{verdict, summary, findings[kind, unit, ...]}` | `tickets/<stem>/requisition_review.md` |
| correctness_review | `.tempo/outbox/correctness_review/*/*/reply.json` | `{verdict, summary, findings[code, ...]}` | `tickets/<stem>/review.md`, pinned to the reviewed SHA |
| triage | `.tempo/outbox/triage/*/*/reply.json` | `{verdict, link, summary, rationale, evidence, reopen_after_days}` | `<state_dir>/box/<msg>.json` status field |
| retro | `.tempo/outbox/retro/*/*/reply.json` | `{proposals[fixed_failure, overcorrection_risk, proposed_roles]}` | `tickets/retro/<seq>.md` |
| implement (tool) | the ticket's scope fence, never `tickets/` or `.tempo/` | run record | worktree + outbox, as TEMPO_PLAN specifies |
| orchestrator | `tickets/*/decisions.md`, `tickets/orchestrator/handoff.md`; creates (never edits) `tickets/*/ticket.md` | n/a | committed by `accept`/session end |

The reply schemas are the closed models in `tempo/contracts.py`. The role definitions in
`src/tempo/agents/*.md` declare the allowlists. The hooks and the renderer both read those same
definitions, so an agent file and its enforcement cannot disagree.

### 2.2 Enforcement

| Check | Early (hook) | Authoritative (tool) |
|---|---|---|
| spawn prompt is a manifest of real, permitted paths | `PreToolUse` on the spawn tool | `prepare_inputs` produced it; `accept` matches it |
| subagent writes only its reply file | `PreToolUse` on write tools | `accept` reads only that path |
| subagent runs no mutating shell | definitions grant no shell (Claude, Copilot); Codex hook allows read-only argv only | n/a |
| final reply is bare `{written, status}` with valid files | `SubagentStop` blocks the stop with the reason, at most 2 times | `accept` re-validates; failure is `invalid_artifact` |
| orchestrator writes only its memory files and new tickets | `PreToolUse` on write tools | single writer commits only those paths |
| `decisions.md` is append-only | n/a | `accept`/session end compares against HEAD and refuses a rewritten prefix |

The hooks give the agent fast feedback with a paved road. The tools issue the verdicts. A host where
a hook did not run (for example, `tempo` missing from PATH, or Codex hooks not yet trusted) loses only
the early feedback. `accept` still refuses invalid work, and `tempo doctor` (section 12, T3) reports
missing hooks before a run.

## 3. Orchestrator memory

- **`tickets/<stem>/decisions.md`** is append-only, with one entry per orchestrator decision on that
  ticket: action (retry | escalate | split | park | reject), reason, and files read. It is rendered
  into the next attempt's prompt, and replaces the diagnosis `lessons` field.
- **`tickets/orchestrator/handoff.md`** is rewritten and committed at every session end. It holds
  open threads, intended next actions, the watchlist, and pending experiments. The next session reads
  it after `status` and before acting.
- **Precedence.** The journal is authoritative for state. `decisions.md` and `handoff.md` hold
  judgment and memory. On conflict the journal wins, and the session records the discrepancy in
  `handoff.md`.
- **New work.**
  - Splits and successors are written directly as tickets with `source: orchestrator`, and the
    supersedes map is journaled.
  - `requisition_review` runs before confirm; no human confirm is needed.
  - All other orchestrator-originated work goes through `file_box`.

## 4. Tools (the `tempo mcp` server)

Every tool is an `@effect`, journaled with `actor: orchestrator`.

- **Read:** `status`, `list_eligible` (eligibility stays the mechanical fold), `read_ticket`,
  `read_harvest`, `read_journal_window`.
- **Handoff:** `prepare_inputs(role, subject)` returns the manifest. `accept(role, subject, attempt)`
  validates, lifts, renders, and commits.
- **Act:** `implement`, `run_checks`, `merge`, `park`, `reject`, `split`, `file_box`,
  `escalate_tier`, `notify`.
- **Review is a spawn.** Correctness review is `prepare_inputs("correctness_review", stem)`, then a
  spawn of `tempo-correctness-review`, then `accept`. This replaces a `review` tool, because a tool
  cannot start a host-native subagent on all three hosts.

`tempo.roles.TEMPO_TOOLS` is the closed list. The renderer emits it into each host's orchestrator
tool list (`mcp__tempo__<tool>` for Claude, `tempo/<tool>` for Copilot).

### 4.1 Refusals (in the tool, not the prompt)

- limit spent (limits are per agent run)
- scope fence
- `merge` re-runs the hard gate set and requires an accepted approval pinned to the admitted SHA
- single-writer lockfile
- no `ticket.md` self-edit
- the implementer and the reviewer are always separate sessions
- `accept` refuses a reply that does not match a manifest `prepare_inputs` issued

Untrusted text (ticket bodies, harvests, box messages) reaches any agent only inside TEMPO_PLAN §8
data blocks, in files.

## 5. What the orchestrator decides

These decisions replace scripted logic:

- which eligible ticket runs next (priority and age become advice in context, not the sort)
- after a failure: retry, escalate, split, park, or reject. This replaces the capability ladder,
  diagnose dispatch, `rework.py` routing, the identical-terminal rules, and auto-keep.
- dead-dependency handling, re-scope, and succession
- the response to a spiral warning: kill it or let it run
- phase sequencing and seeding (replaces the conductor rungs and the TEMPO_PLAN §19 registries'
  scripted walk; the registries stay as data)
- when to run `triage`: at every phase exit, and whenever `status` reports pending box messages
- when to run `retro`

## 6. Kept vs removed

**Kept as tool backends:** `journal`, `effects`, `git`, `gates`, `merge`, `mergequeue` (still the
serial single writer to main), `tickets` schema, `caps` (limits only; `next_rung` is removed),
`lockfile`, `redact`, `providers`, `status`, `watchdog` (detection only), `artifacts`, `box`,
`specs` (render bound only).

**Removed:** `drain.py`; the `daemon.py` dispatch loop; `runner.drive`/`run_ticket` dispatch;
`caps.next_rung` and ladder routing in `runner.failure_terminal`; `stages.diagnose*` and
`specs/diagnose.md`; `rework.py` routing and `specs/rework.md`; the `bootstrap/conductor.py` rungs.

**Replaced:** `specs/{author,requisition_review,review,triage,retro}.md` become
`src/tempo/agents/<role>.md`. The surface prompts become role definitions, with paths in and files
out.

## 7. Session model

- **Supervisor.** `tempo supervise --host claude|copilot|codex` holds the lockfile, touches the
  heartbeat, and starts orchestrator sessions with `TEMPO_ROLE=orchestrator`. It builds each launch
  as an argv list (D1):

  | Host | Launch |
  |---|---|
  | Claude Code | `claude --agent tempo-orchestrator -p <start prompt>` (or the Agent SDK with the same agent definition) |
  | Copilot CLI | `copilot --agent tempo-orchestrator -p <start prompt>` |
  | Codex | `codex exec -c model_instructions_file=.codex/tempo-orchestrator.md <start prompt>` |

- **Session end.** A session runs until the queue is quiescent, its context budget is reached, or K
  tickets have run. It then writes `handoff.md` and exits. The next session starts from `status` plus
  `handoff.md`.
- **No long-lived context.** The journal and committed files carry everything forward.

## 8. Governing system (Claude Code, GitHub Copilot, Codex)

### 8.1 One source, three renders

- **Canonical sources.**
  - `src/tempo/agents/<role>.md` holds frontmatter (`role`, `description`, `tools` as classes from
    `read|search|write|spawn|tempo`, `writes`, `creates`, `spawns`, `reply`) plus the instruction
    body.
  - `src/tempo/rules/protocol.md` holds the agent protocol every session loads.
- **Rendering.** `tempo render` writes every host file from those sources. `tempo render --check`
  and `tests/test_layout.py` fail on drift. Rendered files are never hand-edited.
- **Instruction files.** In `CLAUDE.md`, `AGENTS.md`, and `.github/copilot-instructions.md`, tempo
  owns only the `<!-- tempo:protocol begin ... -->`/`end` block. The rest of each file is authored
  (TEMPO_PLAN §17 as amended in section 11).

### 8.2 Layout per host

| Purpose | Claude Code | GitHub Copilot | Codex |
|---|---|---|---|
| Always-loaded instructions | `CLAUDE.md` | `.github/copilot-instructions.md` (Copilot also reads `AGENTS.md`) | `AGENTS.md` |
| Orchestrator | `.claude/agents/tempo-orchestrator.md` | `.github/agents/tempo-orchestrator.agent.md` (`agents:` lists the five subagents) | `.codex/tempo-orchestrator.md` (session instructions; Codex has no root custom agent) |
| Subagents | `.claude/agents/tempo-<role>.md`, `tools: Read, Grep, Glob, Write` | `.github/agents/tempo-<role>.agent.md`, `tools: [read, search, edit]` | `.codex/agents/tempo-<role>.toml` (`name`, `description`, `developer_instructions`) |
| Orchestrator spawn tool | `Agent(tempo-author, ...)` | `agent` | built-in `spawn_agent` |
| Hooks | `.claude/settings.json`: `PreToolUse`, `SubagentStop` | `.github/hooks/tempo.json` (`version: 1`): `preToolUse`, `subagentStop` | `.codex/hooks.json`: `PreToolUse`, `SubagentStart`, `SubagentStop` |
| MCP server | `.mcp.json` | `.vscode/mcp.json` | `.codex/config.toml` `[mcp_servers.tempo]` |
| Host config | n/a | n/a | `.codex/config.toml` `[agents] max_concurrent_threads_per_session = 1` |

### 8.3 Hook adapter

Every hook runs `tempo hook <host> <event>` with the host payload on stdin (`tempo/hooks.py`).

- **Normalizing.** The adapter normalizes the payload to a `Caller` (role, subagent?) and a `Call`
  (kind: `read | search | write | spawn | spawn_wait | spawn_message | shell | tempo | other`,
  target paths, command, spawn target, and prompt).
- **Deciding.** `tempo/policy.py` decides. The adapter emits the host's deny format:
  - Claude/Codex: `hookSpecificOutput.permissionDecision`.
  - Copilot: top-level `permissionDecision`.
  - Stops: `{"decision": "block"}`.
- **Fail closed.** An exception while deciding a `PreToolUse` is a deny.
- **Write targets per host.** Claude uses `file_path`. Copilot uses `path`, and the adapter accepts
  `toolArgs` as an object or a JSON string. Codex `apply_patch` has no path field, so the adapter
  reads `*** Add|Update|Delete File:` and `*** Move to:` lines from the patch text. A write whose
  target cannot be read is denied for constrained roles.

| Caller | read/search | write | spawn | shell | tempo tools | other |
|---|---|---|---|---|---|---|
| tempo subagent | allow | own reply file only | deny | read-only argv allowlist (Codex only; others have no shell) | deny | deny |
| orchestrator | allow | `writes` + `creates`-if-absent | the five subagents, valid manifest | deny | allow | deny (`spawn_message` denied; `spawn_wait` allowed) |
| implement | allow | inside `TEMPO_FENCE`, never `tickets/`/`.tempo/` | deny | allow (gates judge the result) | n/a | allow |
| developer (no tempo role) | allow | allow | tempo subagents need a valid manifest; others allowed | allow | allow | allow |
| foreign subagent (non-tempo) | allow | allow | deny | allow | allow | allow |

### 8.4 Identifying the caller

- **Claude Code.** A subagent's tool calls carry `agent_id` and `agent_type`. With `agent_id`
  present, the caller is that subagent. If `agent_type` is present but `agent_id` is not, the caller
  is the main thread run with `--agent`.
- **GitHub Copilot.** Subagent calls carry `agentId`, `agentType`, and `parentAgentId`. With
  `parentAgentId` present, the caller is a subagent.
- **Codex.** `PreToolUse` carries only the parent `session_id` for subagent calls, so the payload
  cannot name the caller.
  - `SubagentStart` writes `.tempo/state/hooks/active/<agent_id>`, and `SubagentStop` removes it.
  - With one marker, the caller is that subagent.
  - With more than one, the caller is `ambiguous`: reads are allowed and everything else is denied.
  - `.codex/config.toml` therefore caps open subagent threads at 1.
  - While a subagent runs, the parent's own calls are attributed to the subagent. This is stricter,
    not looser.
- **Every host.** An `agent_type` naming a tempo subagent role is always treated as a subagent,
  whatever else the payload says.

### 8.5 Host facts to re-verify at each host upgrade

These are platform behaviors this design depends on. `tests/test_hooks.py` pins the payload shapes
assumed here. When a host release changes one, update the adapter and that test together.

- Claude: `agent_id` is absent on main-thread calls under `claude --agent`. `Agent(<names>)` in
  `tools` restricts spawn targets.
- Copilot CLI: hook payloads include `parentAgentId` for subagent calls. `task` is the spawn tool
  name, and `create`/`edit` are the write tools. The `.vscode/mcp.json` location is for VS Code; the
  Copilot CLI and the Copilot coding agent configure MCP servers outside the repo.
- Codex: the `spawn_agent`/`send_input`/`wait`/`close_agent` tool names. `SubagentStop` honors
  `{"decision": "block"}`. `-c model_instructions_file=` is accepted. Project hooks run only after
  `/hooks` trust.

## 9. Packaging and distribution

- **Distribution.** The distribution is `tempo-agents` (the `tempo` name is taken on PyPI). The
  import package and CLI are `tempo`, and the version lives in `src/tempo/__init__.py`.
- **Build.** `uv build` produces the wheel and sdist. The backend is hatchling, because uv's own
  backend cannot map top-level repo files such as `.github/` into wheel data.
- **Data files.** The rendered layout ships as wheel data files:
  `[tool.hatch.build.targets.wheel.shared-data]` maps each rendered path to
  `share/tempo/rules/<same path>`. A wheel's data files install under the environment prefix
  (`<venv>/share/tempo/rules/`); no Python installer writes into a project root.
- **`tempo init [--host ...] [--force]`** places the shipped files at a host repo's top level:
  - It merges the protocol block into existing instruction files.
  - It merges tempo's entries into host-owned JSON (`.claude/settings.json`, `.mcp.json`,
    `.vscode/mcp.json`, `.codex/hooks.json`).
  - It writes absent files, skips and reports any other file that differs (unless `--force`), and
    adds `.tempo/` to `.gitignore`.
  - It is idempotent.
- **Sync test.** `tests/test_layout.py` asserts that the shared-data table covers exactly the
  rendered layout.
- **Install for use.** Run `uv tool install tempo-agents`. The hooks call `tempo` on PATH.
- **Release.** `.github/workflows/publish.yml` runs on a `v*` tag. It runs `uv sync`, `pytest`,
  `tempo render --check`, and `uv build`, then `uv publish` with PyPI trusted publishing (the
  `pypi` environment, no stored token).

## 10. Testing

- **Hook tests (`tests/test_hooks.py`), per host payload shape:**
  - nested-spawn refusal, including a spawned orchestrator
  - write-allowlist refusal, including Codex patch paths
  - manifest refusal: prose, extra keys, missing or escaping or outbox paths, oversize, replayed
    attempt
  - read-only shell for Codex subagents
  - orchestrator write scope, including no `ticket.md` self-edit
  - implement fence
  - reply contract: prose, prose before JSON, schema-invalid file, path outside the manifest, empty
    `ok`
  - bounded stop retries
  - Codex marker lifecycle and the ambiguous case
- **Layout tests (`tests/test_layout.py`):** render drift, subagent tool lists without spawn or
  shell, Codex thread cap, shared-data coverage, and `tempo init` merge and idempotence.
- **Orchestrator tests:** scripted tool-call sequences against the fake-LLM adapter replace the
  drain-ordering tests (T3).
- **Return-contract tests on `accept`:** missing path, out-of-allowlist path, schema-invalid file,
  and a manifest that `prepare_inputs` never issued (T3).

## 11. Changes to `TEMPO_PLAN.md`

- **Section 6:** the per-surface write grant becomes the per-role path allowlist of section 2.1, and
  `review`/`author`/`triage`/`retro` run as host-native subagents rather than read-only CLI calls.
- **Section 8:** prompt specs for the five subagent surfaces move to `src/tempo/agents/`.
  `diagnose` and `rework` specs are deleted. Data blocks use the `<<tempo-data:begin name>>`
  sentinel inside input files.
- **Sections 9 and 11:** dispatch and failure-recovery prose reduces to the tools and refusals of
  section 4 and the decisions of section 5.
- **Section 13:** reserve `tickets/orchestrator/` beside `decisions/` and `retro/`. Add
  `decisions.md` and `requisition_review.md` to the ticket-dir listing, and add `orchestrator` to
  the `source` vocabulary.
- **Section 15:** add `.tempo/inbox/`, `.tempo/outbox/`, and `.tempo/state/hooks/` to the instance
  layout.
- **Section 17:** root agent files are three render targets: `CLAUDE.md`, `AGENTS.md`, and
  `.github/copilot-instructions.md`. Each carries the generated protocol block. `CLAUDE.md` stays
  canonical for the authored conduct, and a rule change touches all three.
- **Section 19:** phases reduce to section 12 below.
- **Section 21:** the conductor extraction block is deleted, while README stays generated.
- **GO sign-off** (§13 touchpoint 7) is unchanged and covers this version.

## 12. Phases

Each phase starts only after the previous phase's exit is met.

| Phase | Deliverable | Exit |
|---|---|---|
| **T0** (done) | Governing layer: role definitions, renderer for three hosts, hook policy and adapters, reply contracts, `tempo init`, packaging, publish workflow | `uv run pytest` green; `tempo render --check` clean; `uv build` wheel carries `share/tempo/rules/**`; `tempo init` into an empty repo produces the section 8.2 layout |
| **T1** | Kernel: build TEMPO_PLAN Phases 0-1 kernel modules (section 6 "kept") in this repo, porting from the v3-seed build (`protus75/chupa` at `63b89e4`). Rename `chupa` to `tempo` in identifiers, markers, `.chupa/` to `.tempo/`, and data-block sentinels. | ported suites green; the predecessor name appears nowhere in `src/` or `tests/` |
| **T2** | `prepare_inputs` and `accept` with their return-contract tests; `decisions.md` append-only check | section 10 `accept` tests green |
| **T3** | `tempo mcp` (section 4 tools), `tempo doctor` (hooks installed, `tempo` on PATH, Codex trust), fake-LLM orchestrator sequences | an orchestrator session on each host runs one ticket end to end against `hosts/fixture` |
| **T4** | `tempo supervise` (section 7) and removal of the section 6 "removed" modules | soak: K tickets across two sessions resume from `handoff.md` with no journal discrepancy |
| **T5** | GO (TEMPO_PLAN §13 touchpoint 7) | unchanged |

## 13. Rationale (never cited)

- Hooks that bring work to a stop cost an agent turn per refusal, so each refusal states its paved
  road. The verdict still lives in the tool, so a missing hook cannot admit bad work.
- Reply-shape failures (prose before JSON) and an untriaged Suggestion Box were the most frequent
  avoidable stops in the v3-seed build. The `SubagentStop` contract and the orchestrator's triage
  cadence target both.
- Codex's single-thread cap trades parallel subagents for an exact caller identity. Lift it only
  when Codex hook payloads name the calling agent.
