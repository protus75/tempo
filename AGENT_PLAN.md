# tempo — conversion plan

This plan converts chupa's script-driven orchestration into an agent-driven orchestration called tempo.

**Status:** revision 4. It applies the Kill, Cut, and two BuildIt rounds of review findings, 2026-11-01. Nothing runs until
you approve it. Each phase ends at a stop point, and nothing moves past a stop point without your OK.

## 1. Goal

**Metric (confirmed 2026-11-01).** The plan optimizes for two things, in order:
1. Make the least change to chupa's proven mechanism.
2. As the tie-breaker, reach one verified ticket on each host as fast as possible.

| # | Requirement | Where it is met |
|---|---|---|
| R1 | Convert chupa's scripted top level into agent-based orchestration | §4, P4–P5 |
| R2 | Subagents are single-depth only | §4.3, P4 |
| R3 | Keep chupa's file creation and communication mechanism as the way agents communicate | §3, P4 |
| R4 | Keep communication between agent sessions constrained and verified. Chupa's validation is kept unchanged. New checks are added only where a host subagent replaces a child process (§3.4). | §3.3, §3.4, P4 |
| R5 | Run under Claude Code, GitHub Copilot, and Codex, each with its rule files in its native location | §5, P4 (live gate), P6, P7 |
| R6 | New repository `tempo` | P0 |
| R7 | Rename every chupa reference in carried-over code, specs, tests, and docs | P1 |
| R8 | Carry over the working system, not its build evidence | §2.3, P1 |
| R9 | `uv build` produces a wheel for PyPI. The wheel ships the AI rules as data files, and `tempo init` places them in the host repo's top-level folders (§6 explains why an installer cannot) | §6, P6 |
| R10 | `TEMPO_PLAN.md` starts as SEED_PLAN.md (latest seed) filled with "tempo", and is amended as §10 lists | P1, §10 |
| R11 | Every operator tunable lives in `config.yaml` and is validated at load. Contract constants stay in code, pinned by tests (§4.4.1). | §4.4, P4 |
| R12 | When a provider or host reports it hit its limit, work continues on an alternate. There is no cost accounting. Implement failover needs a second `routing` candidate, which the operator configures. | §4.4.5, §4.6, P2, P4 |

## 2. Facts this plan rests on

These come from read-only research on chupa at commit `63b89e4`.

### 2.1 The communication mechanism (R3, R4)

- **Invariant 1.** Stages communicate only through artifacts on disk plus the journal.
- **Prompt rendering.**
  - Prompts are rendered from `specs/<surface>.md` by `specs.render`.
  - The frontmatter is strict.
  - All untrusted input goes inside `<<…-data:begin name>>` blocks, and payload delimiters are quoted.
  - Prompts are capped at 400,000 characters.
  - Every surface has a `retry_findings` block.
- **One surface call: `Driver._run`** (driver.py:242-371). Steps:
  1. Render the prompt.
  2. Spool `prompt.md`.
  3. Run the child CLI with `prompt.md` on stdin.
  4. Spool `output.txt`.
  5. Validate strictly: `emits.model_validate_json(unwrap_fence(text))`.
  6. Run the gates, each with a paved road.
  7. Run the optional review hook.
  8. On failure, re-prompt with the findings, up to `caps.retry`.
- **The effect record.** Each call is journaled as the effect
  `llm/<stem>/<run_seq>/<surface>/<attempt>/<call_seq>`. A completed key is replayed instead of re-run.
- **Who writes artifacts.**
  - Read-only surfaces reply with JSON only. The engine writes the artifact: `review.md`,
    `checks.json`, `diagnosis.json`, `tickets/decisions/*`, `tickets/retro/*`, or the author's
    ticket.md.
  - Implement is the only writing surface. It runs in its own worktree and branch.
- **Lifting.** `lift_outbox` validates registered reports, copies them to `tickets/<stem>/`, and
  commits them inside the effect `ticket-plane/<stem>/<attempt>/<kind>`.
- **Merge.**
  - Merge needs an `ApprovedInvoice` whose `reviewed_sha` equals the branch head.
  - Gates: scope fence, verification, run record, diff budget, core drift, bug evidence.
  - Merge adds: ticket schema, post-rebase, approval, seed safety.
- **Findings across attempts.** Findings reach the next attempt only through files and the journal:
  `prior_attempts` and `_prior_findings` read `review.md`, `checks.json`, and the diagnosis lessons.

### 2.2 The scripted top level and how tangled it is

**Decisions the scripted layer makes today, which move to the orchestrator:**
- ticket selection (`drain._select`, `Scheduler`)
- hold release
- re-offer (`drain._run_one`)
- run-terminal routing (`runner.drive`)
- failure dispatch and the ladder (`failure_terminal`, `caps.next_rung`, `IDENTICAL_K`)
- diagnose
- rework
- dead dependencies
- spiral response
- triage and retro cadence
- phase sequencing (`bootstrap/conductor.py`)

**Kept modules depend on scripted modules.** `merge`, `mergequeue`, and `box` import from `daemon`,
`runner`, `stages`, and `drain`:
- `PauseConsumer`
- `Refusal`
- `StageContext`, `Evidence`, `CommandResult`
- `sort_key`, `authored_at`
- `storm_producer`
- `runner.confirm_draft`, `runner._dead_dependents`

**Nothing can be lifted out first.** Following imports from the kept set reaches 52 of chupa's 54
modules. Only 3 of 146 test files touch only kept modules.

### 2.3 Build evidence (R8) that is never carried over

- `tickets/` (1,647 files of chupa's build history)
- `eval/`
- `bootstrap/`, the conductor and its state, which sits outside the `chupa/` package
- `hosts/`
- `CHUPA_PLAN.md`
- chupa's `README.md` and `uv.lock`
- 66 evidence test files:
  - all `test_seeded_*`
  - all `test_phase*_exit`
  - `test_daemon_soak`, `test_daemon_soak_runner`, `test_diagnose_eval`, `test_eval_harness`,
    `test_fault_injection`, `test_fixture_host`, `test_go_grade`, `test_host_loop`, `test_plan_lint`,
    `test_reliability_battery`, `test_shakeout`

### 2.4 Provider limits and cost (R12)

**Limit failover already exists and does not depend on cost.**
- `FAILURE_MARKERS` classify child-CLI output into a `FailureClass` (providers.py:34-41).
- The `thresh` circuit breaker opens after `k` (3) failures, for `cooldown_minutes` (10).
- Quota hits arm `provider-cooldown/<provider>/<deadline>` timers using `limits.quota_window_minutes`.
- Selection takes the first available `routing` candidate.
- When every candidate is out, the ticket parks as `infra_error`/`provider_drought`, uncharged.

**Cost accounting is separate, and is removed (§4.4.5):**
- `est_cost_per_call_usd`
- `--max-budget-usd`
- `budget_usd` and `budget_exceeded`
- the effect `cost` body
- watchdog spend tracking
- retro, scorecard, and status spend

SEED_PLAN.md §9.7 defines the only spiral signal as spend-without-progress, so spiral detection goes
with cost.

### 2.5 Where SEED_PLAN.md and chupa's code disagree (R10)

- **Identical sections.** Sections 2-6, 10, 14-16, 18, 20, and 21 are byte-identical apart from the
  name.
- **Entry units.** The seed makes the registry row the entry unit and has no `### 19.P<n>.<stem>`
  headings. Chupa's resolver, `unit_sha`, scope-fence plan anchor, and hardening text all read those
  headings. **P2 converts the code to rows.** This is coupled: the kept scope-fence gate needs it.
- **Other seed rules the code lacks.** These serve tempo building itself, which no requirement commits
  to, so they are not built. They are listed as optional phase P8 and in §10:
  - `fence_add`
  - plan size caps
  - requisition reply-shape and whole-ticket re-review
  - implement spec-gap discipline text
  - the fixture-corpus diff-budget exemption
  - closed-set pinning tests
- **Phase-exit triage duty.** Kept, as an orchestrator rule (§4.2).

## 3. Communication design: chupa's mechanism, run by host subagents

### 3.1 The one real change: who runs the call

Chupa runs each read-only surface as a child CLI and reads its stdout. In tempo, the orchestrator
spawns a host-native subagent instead. `Driver._run` is split at the point where the child process
ran. Nothing else in it changes.

**`prepare(stem, surface)`** is a tool. It:
- runs steps 1-2 unchanged: `specs.render`, then spools `prompt.md` under
  `<state>/spools/<stem>/<attempt>/<surface>/call-NN/`.
- puts one trusted header line ahead of the rendered prompt, giving the absolute read root (the ticket
  worktree, or the repo). The header counts toward `RENDER_BOUND_CHARS`. The specs and `specs.render`
  are not changed.
- splits the prompt into `prompt-01.md … prompt-NN.md`. Each part is at most `READ_PART_CHARS`, a
  code constant set in P4 to the smallest whole-file Read size among the hosts that pass the live gate.
- journals the effect intent under the existing `llm/...` key.
- records the call in two places:
  - The journal call record gets the three new fields (§3.1, end).
  - A side file `call-NN/call.json` gets the agent name (§4.4.2), the part paths, the reply path, and
    the state snapshot (§3.4).
- returns:
  - `call`
  - `agent`: the agent to spawn
  - `spawn_prompt`: the exact text to send, `call: <id>`, then one `part: <path>` line per part, then
    `reply: <path>`
  - the part paths
  - the reply path (`output.txt`)
- **replays completed calls.** If the effect key is already completed, `prepare` returns the call
  marked completed, and no spawn is needed. `submit` then re-runs the artifact writer from the journaled
  result.
- **retries with findings.** On a retry, it reads the findings that the last failed `submit` journaled in
  that effect's completion, and renders them into `retry_findings`.

**Scope.** `prepare` and `submit` serve the five read-only surfaces that survive the conversion: review,
requisition, triage, author, and retro. `diagnose` and `rework` stay on the old child-process path until
P5 deletes them.

**Surfaces with no ticket** (triage, retro, and author before its ticket exists):
- The stem is the one chupa's effect key already uses for that surface.
- The snapshot covers the repo only.
- The read root is the repo.

**The subagent** receives only the call id and those paths. It:
- reads every part
- does its work read-only, under the read root named in the prompt header
- writes its reply JSON to `output.txt`

Its final chat message is never acted on. The `SubagentStop` hook saves it to `call-NN/stop.txt`, for
failure classification only.

**`submit(call)`** is a tool. It refuses a call that `prepare` did not issue. Otherwise it decides the
outcome from the first matching row:

| State at `submit` | Outcome | Charged to `caps.retry` |
|---|---|---|
| Something changed outside `output.txt` (§3.4) | `invalid_artifact`, with a confinement finding | yes |
| Past the deadline | `timeout`; any late output is ignored | yes, as chupa |
| No `output.txt`, no `stop.txt`, before the deadline | `pending`; the call stays open | no |
| `output.txt` missing or invalid, and `stop.txt` classifies (`FAILURE_MARKERS`) as `quota_exhausted`, `rate_limited`, or `outage` | `infra_error`/`provider_drought` | no |
| Same, but classifies as `auth_error` or `model_error` | `infra_error`, notify | no |
| Same, with any other class or no `stop.txt` | `invalid_artifact` | yes |
| `output.txt` present | Run steps 5-8 unchanged, then journal the effect completion and call the existing artifact writer | per chupa |

Steps 5-8 are `unwrap_fence`, strict validation against `emits`, `run_gates`, and the review hook.
- "Invalid" in the table means step 5 failed.
- A gate failure is `gate_failed`.
- The artifact writers are `render_review`, the triage record, the retro report, the author ticket, and
  requisition's existing writer.

**Long checks.** `submit` follows the start-then-poll rule. It runs steps 5-8 in a worker and returns
`pending` until they finish. Calling `submit` again returns the result.

**Notify.** The "notify" in the table calls chupa's existing notification function directly. The
`notify` tool is P5.

**Charged vs uncharged.**
- A charged outcome completes the effect, and the next `prepare` uses the next `call_seq`.
- An uncharged outcome (`infra_error`, an abandoned call) does not complete the effect. It is journaled
  as a note, and the next `prepare` reuses the same `call_seq` under a new call id.

On a validation or gate failure, `submit` returns the findings. The orchestrator calls `prepare`
again, which renders them into `retry_findings` as today. `caps.retry` counts those re-prompts; an
uncharged `infra_error` call does not count.

`timeout`, `invalid_artifact`, and `gate_failed` keep their meaning and their journal shape.

**New fields.** Exactly three fields are new in the journal call record. `call_id` and `deadline` go in
the effect intent. `model_source` goes in the completion, copied from `call.json`, where the hook records
any host-reported model.
- the call id, a random 128-bit hex string
- the deadline, measured from `prepare`
- the model-source note: `{resolved: <model>, host_reported: <model or null>}`. `host_reported` is
  filled only when a host's subagent hook payload carries a model field.

**The old path.** The old child-process `Driver._run` path stays through P4 for two jobs: it is the
parity-test reference, and it is what the not-yet-removed scripted modules (`drain`, `runner`, `diagnose`,
`rework`) still call. P5 deletes it once they are gone.

### 3.2 What stays a child process

**Implement** stays chupa's writing surface. `providers.CliAdapter` runs it in the ticket worktree,
unchanged:
- the same argv, minus `--max-budget-usd`
- env scrubbing
- the stuck kill
- process-group abort

**Two additions:**
- Its argv disables project MCP servers and hooks. The exact flags are verified in the P4 live gate.
- Its env carries no `TEMPO_SESSION`, so even if the worktree's `.mcp.json` loads, every tempo tool
  refuses it (§4.1).

It is exposed as `implement(stem)`, which returns a job id that `job(id)` polls (§4.1).

### 3.3 Verification that is kept unchanged (R4)

- **Schema validation.** Every reply is validated by the same pydantic `emits` models:
  `ReviewVerdict`, `RequisitionReply`, `TriageReply`, `AuthorReply`, `RetroArtifact`.
- **Gates.** `run_gates`, with gate-contract checks, severity, and paved roads.
- **Check and merge gates.** Unchanged.
- **Review pinned to the SHA.** `ApprovalGate` and the `served_review` provenance checks are kept.
  - The model in the call record is the one `prepare` resolved and told the host to run. It is
    **resolved, not attested**: hosts do not report which model actually ran.
  - Where a host payload does report it, that is recorded too.
- **Journal.** The journal and effect keys, single-writer commits, and lift-and-validate of registered
  reports.

### 3.4 Confinement that is new, because subagents replace child processes (R2, R4)

Chupa got confinement from argv: `--allowedTools Read,Grep,Glob,LS`, one fresh process per call, and
stdout as the only channel. Tempo enforces the same in two places.

**During the call, the hook is the only write barrier.** It enforces:
- A subagent writes only its call's `output.txt`.
- A subagent reads only the ticket worktree, the engine `specs/`, and its call's parts.
- A subagent never spawns, messages, or resumes an agent (R2).
- The spawn prompt is exactly the call id and its paths, and the agent spawned is the one `prepare`
  named.
- The orchestrator has no shell and no direct file writes. All its actions are tempo tools.
- Every hook denies a payload it cannot parse, on all three hosts.

**After the call, `submit` is the check.**
- `prepare` snapshots the repo and the ticket worktree:
  - HEAD
  - `git status --porcelain --untracked-files=all --ignored`, excluding `.tempo/`
  - a content hash of every listed path
  - the journal's last sequence number
- `submit` compares against that snapshot. It refuses on any difference outside `output.txt`, apart
  from journal entries written by this call's own tools.

A write that gets past a hook is therefore caught before any output is accepted.

**State, reads, and calls:**
- `<state>` is `.tempo/`. It is gitignored, and the snapshot excludes it.
- **Subagent shell commands.** Codex subagents read through the shell.
  - The hook allows plain read commands (`cat`, `sed -n`, `head`, `tail`, `rg`, `grep`) on the allowed
    roots and denies every other shell command.
  - A Codex subagent writes `output.txt` with Codex's own patch tool, allowed only for that file.
  - A write that gets through is still caught by the `submit` snapshot.
- **One open call per session, on every host.**
  - At the spawn `PreToolUse`, the hook takes the call id from the spawn prompt.
  - At `SubagentStart`, it binds the subagent's id to that call.
- **Subagents cannot use tempo tools.** Agent files list none. The hook denies any tempo MCP call from a
  subagent, because the subagent inherits `TEMPO_SESSION`.
- **What the orchestrator may use.**
  - Allowed: tempo tools, the host spawn tool (spawn-checked), and file read and search tools.
  - Denied: shell, write, and edit.
- **Sessions with no tempo session token.** Hooks enforce nothing unless `TEMPO_SESSION` is set or the
  caller is a `tempo-*` agent. This covers you using a host by hand in the same repo. Unparseable
  payloads are always denied.
- **Codex, while a subagent marker is active.** Only these are allowed:
  - reads
  - that subagent's `output.txt` write
  - Codex's `wait` and `close_agent` tools

  Codex cannot tell the orchestrator from the subagent in that window. So the Codex orchestrator could
  write `output.txt` itself. This is accepted (D10, §9). Review never runs on Codex under the default
  routing (§4.4.2), and the gates still check content.

**Caller identity:**
- Claude: `agent_id`/`agent_type`.
- Copilot: `parentAgentId`/`agentType`.
- Codex: not provided. Tempo uses `SubagentStart`/`Stop` markers with at most one subagent thread, and
  denies everything except reads if more than one is active.

Each of these is confirmed by the P4 live gate before P5.

## 4. Orchestrator

### 4.1 Tools

Every tool is an existing effect-keyed function, or a thin wrapper over one.

**Session token.**
- The supervisor creates a token per session and records it in the lockfile, next to the current host.
  Tools read the host from there for model resolution, agent names, and review parking.
- It passes the token to the host process as `TEMPO_SESSION`. The generated MCP configs pass it
  through to `tempo mcp`.
- Every tool refuses when the token is missing or not the lockfile's current one.
- This gives:
  - a single writer across failover
  - an implement child locked out of tempo tools
  - per-session counters for session limits

**Long-running work is start-then-poll.**
- `implement(stem)`, `check(stem)`, and `merge(stem)` return a job id at once.
- `job(id)` returns the job's state.
- **Jobs outlive sessions.** Jobs run in detached worker processes, with their state kept in
  `.tempo/jobs/`. A job whose worker died reports `lost` and is recorded as `infra_error`, uncharged.
  Effect replay decides whether it re-runs.
- **What P4 builds.** The Read and Run tools, plus `write_handoff`. P5 builds the Decide tools and the
  rest of Record.
- No tool call blocks longer than `TOOL_CALL_S`, a code constant set in P4 below the shortest host MCP
  tool timeout.

| Kind | Tools |
|---|---|
| Read | `status` (includes open calls, so a resumed session recovers them), `list_eligible` (the mechanical half of `drain._select`: confirmed, deps settled, not held, caps remaining, no drought), `read_ticket`, `read_attempts` (`prior_attempts`), `read_box` |
| Run | `start(stem)` (journals `{to: running}` and allocates `attempt`/`run_seq`, as `drain._run_one` did), `prepare`, `submit`, `implement`, `check`, `job`, `merge` |
| Decide | `retry`, `escalate` (offers only `caps.next_rung`'s rung), `reject`, `park`, `release_hold`, `reoffer`, `split` (runs rework's existing proposal checks plus requisition review), `confirm`, `advance_phase`. Each journals the same `state_transition` shape as `failure_terminal`: `{to, stage, reason, dispatch, rung}`. Each takes a required `lessons` argument, which the tool appends to `tickets/<stem>/decisions.md`. |
| Record | `file_box`, `notify`, `write_handoff` (rewrites `tickets/orchestrator/handoff.md`) |

**Refusals, enforced in the tool:**
- caps spent
- bad or missing session token
- merge without an approval for the admitted SHA
- `submit` for an unissued call
- writing an existing `ticket.md`
- `retry` once the same terminal reason has repeated `IDENTICAL_K` times
- **at `max_tickets`:** `start` only
- **past `max_minutes`:** every tool except `status`, `job`, `submit`, and `write_handoff`. The
  supervisor's kill backs this up.
- **one thing at a time:**
  - While a subagent call is open, `implement`, `check`, `merge`, `write_handoff`, and another `prepare`
    are all refused.
  - While any job is running, including one left from an earlier session, `prepare` is refused.

  The `submit` snapshot then only ever sees the call's own changes. Chupa also ran serially
  (`concurrency: 1`), so this is no change in behavior.
- **calls from a dead session:** a call opened under an earlier session token is abandoned.
  - The new session's `status` lists it.
  - The next `prepare` on that stem and surface records it as `infra_error`, uncharged, or `timeout` if
    the supervisor killed the session.
  - It does not count as open.

### 4.2 Decisions moved from scripts to the orchestrator

| Scripted today | Orchestrator does | Stays mechanical, in a tool |
|---|---|---|
| `drain._select` sort | picks from `list_eligible` | eligibility filter, caps, holds |
| `drive` routing + `stages.diagnose` | reads attempts; decides retry, escalate, split, park, or reject; `lessons` → `decisions.md`, which `prior_attempts` reads | caps, `IDENTICAL_K` |
| `failure_terminal`, `next_rung` | whether to escalate | the rung menu, cap spend |
| hold release, re-offer | `release_hold`, `reoffer` | hold and premise-bounce caps |
| `rework` surface | `split` | proposal checks, requisition review |
| `_dead_dependents` | re-scope or succession | detection, box filing |
| serve box consumer | triage whenever any box message is pending, and at every phase exit (hardcoded) | — |
| drain retro trigger | runs retro when `select_trigger` fires | trigger facts |
| conductor rungs | `advance_phase`, from the §19 registry rows (TEMPO_PLAN) | registry data, phase-exit receipts |
| watchdog spiral warning | nothing; it was spend-based and is removed | stuck kill (time-based) |

P5 confirms that `IDENTICAL_K` compares `state_transition` reasons, not diagnose output, before
`diagnose` is deleted.

### 4.3 Single depth (R2)

1. **Definitions.** No subagent tool list contains a spawn tool.
2. **Hooks.** Any spawn, message, or resume from a subagent is denied (§3.4).
3. **Tests and live gate.** Each host's payloads are captured live in P4.

The implement child cannot spawn tempo agents: it holds no session token, and argv turns off its agent
features.

### 4.4 Configuration

**One file.** `config.yaml` at the root of the repo tempo runs in is the only operator file. If the
repo has none, `tempo init` writes tempo's default.

**Validated strictly at load:**
- Unknown keys are refused.
- Every reachable combination of stage, tier, and host must resolve to a model.
- The review-separation rule (§4.4.2) is checked.
- Every refusal names the key and its fix.

**`schema_version` stays 1.** No tempo config exists before P4, so P4 edits the carried `config.yaml`
directly. There is no migrator.

**Contract constants stay in code.** Changing one needs a plan §22 entry in the same commit plus a test
pinning its value.

#### 4.4.1 Kept as chupa has them

| Parameter | Location | Default | Read by |
|---|---|---|---|
| Implement models per tier | `providers[].models_by_tier` | codex: low `gpt-6-luna`, medium/high `gpt-6.1-sol`, max `gpt-6-astra` | `implement` |
| Implement candidates per tier, in failover order | `routing` rows, `surface: implement` | codex only (add a second for R12) | `implement` → `thresh` |
| Provider concurrency, quota window | `providers[].limits.{concurrency, quota_window_minutes}` | `1`, `60` | `providers`, `thresh` |
| Circuit breaker | `circuit_breaker.{k, cooldown_minutes}` | `3`, `10` | `thresh`, supervisor |
| Starting tier and effort | ticket frontmatter `agent_tier`, `agent_effort` | `medium`/`medium` | `caps.capability` |
| Ladder | `caps.next_rung` (code) | next tier with a distinct implement model, then effort +1, then the Reject queue | `escalate` |
| Repeated-failure threshold | `IDENTICAL_K` (constant) | 3 | `retry` refusal |
| Re-prompts per call | `caps.retry` | 6 | `prepare`/`submit` |
| Other per-ticket caps | `caps.{decisions, infra, premise_bounce, hardening}` | chupa defaults (`diagnosis` renamed `decisions`) | decide tools |
| Ticket wall-clock limit | `tickets.max_minutes` (was `drain.max_ticket_minutes`) | chupa default | `implement` job, `list_eligible` |
| Implement stuck limit | ticket `stuck_minutes` | chupa default | `implement` stuck kill |
| Prompt size bound | `RENDER_BOUND_CHARS` 400,000, `REQ_RENDER_HEADROOM` 0.75 (constants) | same | `prepare` |
| Diff budget | constants: 30 files / 1,500 inserted lines | same | `check` |

#### 4.4.2 Subagent models per host

```yaml
hosts:
  claude:
    models_by_tier: {low: claude-sonnet-5-5, medium: claude-sonnet-5-5, high: claude-opus-5-5, max: claude-opus-5-5}
    pins: {review: claude-opus-5-5}
    quota_window_minutes: 60
  codex:
    models_by_tier: {low: gpt-6-luna, medium: gpt-6.1-sol, high: gpt-6.1-sol, max: gpt-6-astra}
    pins: {}
    quota_window_minutes: 60
  # copilot:                       # operator fills in model ids to enable Copilot; checked like the others
  #   models_by_tier: {low: <id>, medium: <id>, high: <id>, max: <id>}
  #   pins: {review: <id>}
  #   quota_window_minutes: 60
```

- **Resolving the model.** `prepare` takes the tier and effort that chupa's code gives the call today,
  applies `pins`, and resolves the model.
- **Generated agent files.** `tempo render` writes one agent file per distinct reachable combination:
  - `(surface, model)` on hosts that carry no effort (Claude, Copilot): `tempo-review-claude-opus-5-5`
  - `(surface, model, effort)` on Codex: `tempo-review-gpt-6-astra-high`, with `model_reasoning_effort`
- **Spawn check.** The hook refuses a spawn of any agent other than the one `prepare` named.
- **Agent names.**
  - Model ids are used verbatim. Any character outside `[A-Za-z0-9.-]` becomes `-`.
  - Codex effort maps from chupa's effort names through a table set in the P4 live gate: `low`,
    `medium`, and `high` map to themselves, and `max` maps to Codex's highest level.
  - Codex agent names use the mapped level, so duplicates collapse.
  - If two model ids clean up to the same agent name, config load refuses and names both.
- **Reachable hosts.**
  - Reachable hosts are those in `orchestrator.hosts`.
  - Each needs a `hosts.<h>` entry and an `orchestrator.models.<h>` entry, or load refuses.
  - Review agents are not rendered for a host or tier that cannot run review.
  - `render` always writes hook files and `tempo:protocol` blocks.
  - `render` writes model-bearing agent files only for reachable hosts.
  - `render` deletes stale generated `tempo-*` agent files.
- **Drift.** There is one staleness check, `render --check`, called by the drift test and by `doctor`.
  `prepare` refuses when the agent it would name does not exist.
- **Review separation.**
  - The resolved review model must not appear in the `models_by_tier` of any provider named in an
    implement `routing` row.
  - This is checked per host and tier.
  - A host that cannot meet this does not run review. With the default implement routing, that is the
    Codex host.
  - While the session is on such a host, review calls park as `infra_error`/`provider_drought`,
    uncharged, until a session runs on a host that can.
  - Config load reports which hosts and tiers can run review.
  - Config load refuses only when no host in `orchestrator.hosts` can run review at all.

#### 4.4.3 Subagent timeouts

- **Deadline.** `prepare` records a deadline from chupa's existing stuck limits:
  - `REQUISITION_STUCK_S`, `TRIAGE_STUCK_S`, and `AUTHOR_STUCK_S` (constants)
  - the ticket's `stuck_minutes`, for review
  - `RETRO_STUCK_S`, a new constant set to chupa's `call_timeout` default, for retro
- **Late reply.** `submit` after the deadline records `timeout`.
- **Hung subagent.** A call past its deadline plus `SESSION_GRACE_S` is hung. The supervisor then kills
  the session (§4.6).

#### 4.4.4 Orchestrator session

```yaml
orchestrator:
  hosts: [claude, codex]           # failover order (§4.6); copilot added after the P4 live gate
  models: {claude: claude-opus-5-5, codex: gpt-6-astra}
  session:
    max_tickets: 5                 # tickets started before the session hands off
    max_minutes: 240               # the supervisor kills the session after this plus SESSION_GRACE_S
```

**How the orchestrator model reaches each host:**
- Claude and Copilot: a `model:` field in the rendered orchestrator file.
- Codex: `-c model=<id>` in the launch argv.

#### 4.4.5 Provider limits, no cost

- **Implement** keeps chupa's failover unchanged, minus cost.
- **Removed:**
  - `est_cost_per_call_usd`
  - `--max-budget-usd`
  - `budget_usd`, `budget_exceeded`
  - the effect `cost` fields and `StageResult.cost`
  - watchdog spend and spiral detection
  - retro, scorecard, and status spend and token measures
- **Kept:** `provider` and `model` in effect completions, because `served_review` needs them.
- **Host sessions** are handled by the supervisor (§4.6).

#### 4.4.6 Not configurable, on purpose

- subagent depth: 1
- Codex subagent threads: 1
- triage cadence (§4.2)
- paths
- these constants, set in the P4 commit from live-gate measurements, with placeholders in tests until
  then:
  - `READ_PART_CHARS`: the smallest whole-file Read size across hosts that pass the gate
  - `TOOL_CALL_S`: the shortest host MCP tool timeout, minus a margin
  - `SESSION_GRACE_S`: the measured time for a host to exit after SIGTERM, plus a margin
  - `RETRO_STUCK_S`: chupa's `call_timeout` default
  - `NO_PROGRESS_SESSIONS`: 3

### 4.5 Session model

- A session runs until the queue is quiescent, or `max_tickets` or `max_minutes` is reached, and ends
  with `write_handoff`.
- Resuming from `status` alone must be correct.
- `handoff.md` is advisory.
  - `write_handoff` commits it through the single writer and journals the session number.
  - A handoff from an earlier session number is ignored.
- This replaces drain, the daemon loop, and the self-upgrade re-exec.

### 4.6 Supervisor

`tempo supervise` holds the lockfile and heartbeat and owns the session token. Its loop:
1. Pick the first host in `orchestrator.hosts` that is not cooling down.
2. Launch that host's orchestrator session, with `TEMPO_SESSION` set and output captured to
   `<state>/spools/sessions/<n>/`.
3. Watch it.
4. Classify its exit.
5. Repeat.

| Host | Launch (verified in P4) |
|---|---|
| Claude Code | `claude -p --agent tempo-orchestrator <start prompt>` |
| Codex | `codex exec -c model=<id> -c model_instructions_file=.codex/tempo-orchestrator.md <start prompt>` |
| Copilot CLI | `copilot -p --agent tempo-orchestrator <start prompt>` (after the live gate) |

The supervisor classifies a session's exit output with `FAILURE_MARKERS`. The P4 live gate checks those
markers against real output from each host.

| How the session ended | Supervisor action |
|---|---|
| Normal (exit 0, and this session's number journaled by `write_handoff`) | next session; loop step 1 picks the host again |
| Exit 0 without this session's handoff | treated as `unclassified` (next row) |
| `quota_exhausted`, `rate_limited` | arm `provider-cooldown/<host>/<deadline>` using `hosts.<h>.quota_window_minutes`; next host |
| `outage`, `unclassified`, crash | count toward that host's breaker (`circuit_breaker`); restart on the same host until the breaker opens, then the next host |
| `auth_error` | journal, notify; the supervisor exits non-zero |
| `model_error` | arm a cooldown for `circuit_breaker.cooldown_minutes`, notify; next host |
| killed at `max_minutes`, or for a hung call | journal the open call as `timeout`; next session |
| `NO_PROGRESS_SESSIONS` in a row with no journaled `state_transition` | stop, journal, notify |
| every host cooling down | wait for the earliest cooldown; journal, notify |

**Killing a session.** SIGTERM to the session's process group, wait `SESSION_GRACE_S`, then SIGKILL.

**The start prompt** is fixed: "Resume from status. Follow the tempo-orchestrator rules. End with
write_handoff."

**Breaker counters are separate** for a host and the implement provider of the same name.

**Cooldown keys are shared** between a host and the implement provider of the same name. On flat-rate
plans one subscription serves both, so a Codex quota hit blocks both the Codex host and Codex implement.

**Resuming after a stop.** A new session resumes from `status`. A `prepare` call left open is
re-prepared under a new call id. The abandoned call is recorded as `infra_error`, uncharged, except after
a `max_minutes` or hung-call kill, which records `timeout`.

## 5. Governing system for three hosts (R5)

**Agent files are thin.** A surface agent says only: read the prompt parts at the given paths, write the
reply JSON to the given path, nothing else. `specs/*.md` stays the only description of what each surface
does, and `prepare` renders it per call.

| | Claude Code | GitHub Copilot | Codex |
|---|---|---|---|
| Instructions | `CLAUDE.md` | `.github/copilot-instructions.md` | `AGENTS.md` |
| Orchestrator | `.claude/agents/tempo-orchestrator.md` | `.github/agents/tempo-orchestrator.agent.md` | `.codex/tempo-orchestrator.md` |
| Surface agents (generated, §4.4.2) | `.claude/agents/tempo-<surface>-<model>.md` | `.github/agents/tempo-<surface>-<model>.agent.md` | `.codex/agents/tempo-<surface>-<model>-<effort>.toml` |
| Hooks | `.claude/settings.json` | `.github/hooks/tempo.json` | `.codex/hooks.json` |
| MCP (tempo tools; passes `TEMPO_SESSION`) | `.mcp.json` | location confirmed in the P4 live gate | `.codex/config.toml` |
| Caller identity | `agent_id`/`agent_type` | `parentAgentId`/`agentType` | markers, 1 thread |

**Instruction files.** `CLAUDE.md`, `AGENTS.md`, and `.github/copilot-instructions.md` each carry
chupa's managed `core` block, renamed `tempo:core`, plus a generated `tempo:protocol` block.

## 6. Packaging (R9)

- **Build.** `uv build` uses hatchling, because uv's own backend cannot put `.github/` and other
  top-level files into a wheel.
- **Names.** The distribution is `tempo-agents`, because `tempo` is taken on PyPI. The import name and
  CLI are `tempo`.
- **Data files.** `[tool.hatch.build.targets.wheel.shared-data]` maps each rendered host file to
  `share/tempo/rules/<path>`.
- **Why `tempo init` is needed.** An installer places data files under the environment prefix, never in
  a project folder, and a wheel runs no install-time code. So `tempo init`:
  - copies the files into the host repo's top level
  - merges the managed blocks in the three instruction files
  - writes a default `config.yaml` if there is none
  - re-renders the model-bearing agent files from the repo's `config.yaml`
  - skips and reports any other file that differs, including existing JSON configs, which the user
    merges by hand
- **Publishing.** A publish workflow is deferred until after P7. Until then a release is a manual
  `uv publish`.

## 7. Decisions (decided 2026-11-01)

These were decided first, then revised by the review dispositions you accepted.

- **D1 — orchestrator memory.** Decided. Lessons go into `tickets/<stem>/decisions.md` through the
  decide tools' `lessons` argument, and `prior_attempts` reads them.
- **D2 — implement routing.** Decided. Implement stays routed by tier and provider through
  `config.yaml`, and escalation picks the rung. Review separation is enforced by §4.4.2.
- **D3 — cost.** Decided. There is no cost accounting. The only provider concern is failing over on a
  limit (§4.4.5, §4.6).
- **D4 — seed plan units.** Revised by Cut C1.
  - P2 converts the code to row-based entry units only.
  - The other seed rules move to optional P8, which runs only if tempo is committed to building itself.
- **D5 — test host.** Revised by Cut C5 and Kill K1.
  - Host fakes and a minimal fixture repo are built in P4, where the exits first need them.
  - P7 uses the same fixture for live runs.
- **D6 — subagent model routing.** Decided: `hosts.<host>.models_by_tier` + `pins`, with generated
  agents (§4.4.2).
- **D7 — effort per host.** Revised by Cut C4. Codex carries effort. Claude and Copilot carry none.
- **D8 — subagent timeouts.** Decided: a deadline in `submit`, plus a supervisor kill.
  - The per-host prompt bound is removed (Cut C8).
  - It is added back only if a live run shows a host rejecting a prompt under `RENDER_BOUND_CHARS`.
- **D9 — orchestrator session.** Revised by Cut C3 and Kill K9.
  - Settings: `hosts` (failover order), `models`, `max_tickets`, and `max_minutes`.
  - The triage threshold is hardcoded.
  - Context budget is not a session limit.
- **D10 — Codex reply forging.** Decided: accept the risk. While a Codex subagent runs, the Codex
  orchestrator could write `output.txt` itself (§3.4, §9).
- **D11 — Copilot models.** Decided: they live in `config.yaml` under `hosts.copilot`, in the same
  format as the other hosts. The operator fills them in. The default config ships the section commented
  out.

## 8. Phases

Before each phase, its exact file list is posted for approval. P1–P4 and P6 are one commit each. P5 is a
series, one commit per removed module.

### P0 — Plan

**Work:** The empty repo exists (done). This plan.

**Exit:** You approve the plan.

### P1 — Carry and rename

**Work:**
- Carry over chupa's 54 `chupa/` modules as `tempo/`.
- Carry over `specs/`, `config.yaml`, `docs/host-contract.md`, `CLAUDE.md`, `AGENTS.md`,
  `.gitignore`, `pyproject.toml`, and the 80 test files not listed in §2.3.
- Write `TEMPO_PLAN.md` as SEED_PLAN.md filled with "tempo".
- Generate `README.md` from TEMPO_PLAN's README block.
- Rename chupa → tempo in identifiers, markers, `.chupa/`, and data sentinels.
- Run the full suite. Classify every failure as one of:
  - (a) pinned to chupa's plan text — becomes input to P2
  - (b) a real break — fixed now

**Exit:**
- The failure list, with causes and proposed handling, is approved.
- No "chupa" string remains in carried code, specs, tests, or docs.
- `python -m tempo status` runs.

### P2 — Reconcile and remove cost

**Work:**
- Convert entry units to registry rows in `specs.py`, `stages.py`, `tickets.py`, and `runner.py`
  (§2.5).
- Point the P1 (a) tests at TEMPO_PLAN.md.
- Remove cost accounting (§4.4.5) from code, tests, and specs.

**Exit:**
- Full suite green against TEMPO_PLAN.md.
- Plan lint clean.

### P3 — Cut the links

**Work:**
- Move these out of `runner`, `stages`, `drain`, and `daemon` into kept modules:
  - `Refusal`
  - `StageContext`, `Evidence`, `CommandResult`
  - `sort_key`, `authored_at`
  - `PauseConsumer`
  - `storm_producer`
  - `confirm_draft`, `_dead_dependents`
  - the mechanical half of `drain._select` (the eligibility filter) and the run allocation from
    `drain._run_one`, which `list_eligible` and `start` wrap in P4
  - the lockfile and heartbeat code, if they live in `daemon`
- Rename in place and update every caller.

**Kept set.** The kept set is every `tempo/` module except `drain`, `daemon`, `runner`, `rework`, and the
CLI entry `__main__`.
- `__main__` still dispatches `drain` and `serve` until P5.
- Each P5 removal deletes its CLI command in the same commit, and `__main__` joins the kept set at the
  end of P5.
`stages` stays: P5 removes only its `diagnose` part. If a kept module imports `rework`, that link is cut
too. The move list above is a minimum, not the whole list.

**Exit:**
- An import-graph test proves the kept set imports none of `drain`, `daemon`, `runner`, or `rework`.
  - It follows imports transitively.
  - It counts every import, including function-level and `TYPE_CHECKING` imports.
- Suite green.

### P4 — Agent layer, config, live gate

**Work:**
- **(a)** Split `Driver._run` into `prepare`/`submit` (§3.1): prompt parts, the
  state snapshot, stop classification, and the session token.
- **(b)** Start-then-poll tools and the `tempo mcp` server (§4.1).
- **(c)** Hook policy and adapters for three hosts, failing closed on unparseable payloads (§3.4).
- **(d)** Role definitions and `render` (§4.4.2, §5), the config edits (§4.4), and the supervisor
  (§4.6).
  - P4's `render` writes the host files and the `tempo:protocol` block into the repo. P6 only packages
    them.
  - The orchestrator's instructions in P4 cover the call protocol in chupa's stage order:
    - `status` → `start`
    - requisition (`prepare`/spawn/`submit`)
    - `implement` → `check`
    - review (`prepare`/spawn/`submit`)
    - `merge`

    On any failure it writes the handoff and stops. The decision rules come in P5.
  - Add a plan §22 entry for each new contract constant.
  - Update `docs/host-contract.md`.
- **(e)** Host fakes and a minimal fixture repo.
- **(f) Live gate**, on each of Claude Code, Copilot CLI, and Codex:
  - capture hook payloads for a subagent spawn, a read, a write, a shell command, a nested spawn, an
    agent message, and an orchestrator write. The unparseable and mismatch cases are made by altering the
    captured payloads. "Submit of an unissued call" is tested at the tool level.
  - run one `prepare` → spawn → `submit`
  - confirm the launch argv, the MCP config location, and the limit exit markers

**Exit:**
- Per-host hook tests on the captured payloads cover:
  - nested spawn
  - write and read confinement
  - the spawn prompt and the agent spawned
  - submit of an unissued call
  - orchestrator shell and write denial
  - denial of an unparseable payload
- `submit` refuses a stray write.
- The old driver path and `prepare` → fake subagent → `submit` produce byte-identical artifacts and
  journal events, except the three fields in §3.1.
  - The comparison runs under a fixed clock and fixed ids.
  - Prompts are not compared, because the new prompt carries the worktree path.
- Config tests: unknown key, unresolvable combination, review separation.
- Constant pins.
- A live-gate report per host, in `docs/livegate/<host>.md`.
  - A host passes only if all of these are confirmed: the six hook cases, the round trip, the launch
    argv, the MCP location, and the limit markers.
  - **A host that fails the gate is dropped from the defaults until it is fixed.**
  - Copilot is enabled when it passes **and** `hosts.copilot` is filled in.

### P5 — Orchestrator replaces the scripts

**Work:**
- Decide and record tools (§4.1).
- Confirm where `IDENTICAL_K` gets its data.
- Remove one scripted module per commit, listing each one's test deletions first:
  - `drain`
  - `stages.diagnose` + `specs/diagnose.md`
  - rework routing + `specs/rework.md`
  - ladder routing in `runner`
  - the `daemon` loop
  - `runner` dispatch
  - the old `Driver._run` child path (§3.1)
- Each removal also deletes its CLI command. At the end of P5, `__main__` joins the kept set and the
  import-graph test covers it.

**Exit:**
- A fake-host orchestrator session runs a ticket from intake to merge, and through each failure
  decision.
- A session killed mid-ticket resumes correctly from `status`.
- Suite green after every removal.

### P6 — Governing files and packaging

**Work:** Host files from `render`, `tempo init`, and pyproject shared-data.

**Exit:**
- The wheel holds `share/tempo/rules/**`.
- `tempo init` into an empty repo produces the §5 layout plus a default `config.yaml`.
- Installing from the wheel into a clean venv works.

### P7 — Live runs

**Work:** On the P4 fixture repo, run real tickets on each host that passed the live gate.

**Exit:**
- Each host completes a ticket end to end with verified artifacts.
- One ticket per host goes through retry and escalate with a real model.
- Results reported.

### P8 — Self-build rules (optional)

**Work:** Only if tempo is committed to building itself: the §2.5 seed rules the code lacks.

**Exit:** Per §10.

## 9. Risks known now

- **Host behavior may differ from assumptions.** Mitigation: the P4 live gate runs before P5 deletes
  anything, and a failing host is dropped from the defaults rather than blocking the others.
- **Codex has no caller identity.** It runs one subagent thread and fails closed when more than one is
  active.
  - While a subagent runs, the Codex orchestrator could write `output.txt` itself. This is accepted
    (D10).
- **Model provenance is resolved, not attested (§3.3).** A host silently substituting a model is not
  detected.
- **No spiral detection.** Only the stuck timeout, `max_minutes`, and the supervisor's no-progress stop
  guard against thrashing.
- **A host limit ends the whole session.** The call in flight loses its work and is re-prepared,
  uncharged.
- **P1's failure count is unknown until run.** It is bounded by the 65 test files that mention the plan.
- **The orchestrator's decisions meet a real model only in P7**, through the retry and escalate runs.
  Nothing measures them before then.

## 10. Changes to TEMPO_PLAN.md

These are made in the phase shown, so the plan and the code stay in step.

- **P2:**
  - Remove cost accounting from seed sections 0, 5, 6, 8, 9, 11-15, 17-20, and 22. Keep the failure
    classes, breaker, cooldown, routing candidates, and `provider_drought`.
  - Remove the §9.7 spend-without-progress spiral signal.
  - Mark these "not enforced until P8 (optional)": `fence_add`, plan size caps, requisition
    reply-shape and whole-ticket re-review, implement spec-gap discipline text, the fixture-corpus
    diff-budget exemption, and closed-set pinning tests.
- **P4 (§6, §8, §15):**
  - The read-only surface call becomes `prepare` + subagent + `submit`.
  - Configuration per §4.4.
  - Session token, supervisor, and start-then-poll tools.
- **P5 (§9, §11, §12, §14, §19, §21, §0):**
  - Dispatch, failure routing, the ladder walk, diagnose, rework, and the drain and daemon loops become
    orchestrator decisions through the §4.1 tools. Caps and refusals stay in tools.
  - Bootstrap and conductor blocks are deleted.
  - Phase advance uses `advance_phase`.
- **P5 (§13):**
  - Reserve `tickets/orchestrator/`.
  - Add `decisions.md` to the ticket dir listing.
  - Add `orchestrator` to the `source` vocabulary.
- **P6 (§17):** Root agent files become three, each with `tempo:core` and `tempo:protocol`.
- **P6 (§18):** The "virtual project" rule ends at the release path, the PyPI wheel.
