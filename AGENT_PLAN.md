# tempo — conversion plan

This plan **ports** chupa's working software, its completed code, to an agent-driven orchestration called
tempo. Tempo is a port. It never builds itself:
- None of chupa's self-build process is carried over: seed phases, registries, plan entry units, plan
  hardening, seeding tickets, phase exits, and the conductor.
- Where the completed code contains that machinery, the port removes it (§2.5, P2).

**Status:** revision 7. It applies the Kill, Cut, and two BuildIt rounds of review findings, and the
port-not-self-build correction, the review round on revision 5, and the no-human-in-the-loop rule,
2026-11-01. Revision 7 adds chat-session conduct (§5.1, R14, D16). Revision 8 removes every agent CLI
invocation: implement becomes a host subagent, and the supervisor that launched host CLIs is removed.
Tempo runs inside each host's VS Code extension: the orchestrator is the extension's chat agent, with one
subagent layer beneath it (R15, D17, D18). Nothing runs until
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
| R5 | Run in the VS Code extensions of Claude Code, GitHub Copilot, and Codex, each with its rule files in its native location | §5, P4 (live gate), P6, P7 |
| R6 | New repository `tempo` | P0 |
| R7 | Rename every chupa reference in carried-over code, specs, tests, and docs | P1 |
| R8 | Port the working system: carry its code, not its build evidence (§2.3), and remove its self-build machinery (§2.5) | §2.3, §2.5, P1, P2 |
| R9 | `uv build` produces a wheel for PyPI. The wheel ships the AI rules as package data, and `tempo init` renders them into the host repo's top-level folders (§6 explains why an installer cannot) | §6, P6 |
| R10 | `TEMPO_PLAN.md` holds the product features from SEED_PLAN.md (latest seed), filled with "tempo". The seed's build-process sections and passages are left out. It is amended as §10 lists. | P1, §10 |
| R11 | Every operator tunable lives in `config.yaml` and is validated at load. Contract constants stay in code, pinned by tests (§4.4.1). | §4.4, P4 |
| R12 | When a host reports it hit its limit, tempo journals a cooldown and the session ends with a handoff. `/tempo-run` resumes with no lost state. There is no host failover and no cost accounting. | §4.4.5, §4.6, P2, P4 |
| R13 | **No human in the loop.** A human writes tickets. Tempo carries each one to merge, or to a terminal decision, with no human step. Every human checkpoint in chupa goes to the orchestrator or is removed (§2.6). The only stop is a failure that only a person can fix, such as an expired login (`auth_error`). | §2.6, §4.2, §4.6 |
| R15 | **Agents only, no agent CLI.** Tempo never runs an agent CLI (`claude`, `codex`, `copilot`, or any other) as a child process, at any stage, in any phase from P4 on. **Everything runs in the host's VS Code extension:** the orchestrator is the extension's chat agent, started by the user in the chat panel, and every model call, implement included, is a subagent one layer beneath it (R2). Tempo never launches a session. Plain tools tempo runs (git, verification commands, test runners) are not agent CLIs and stay subprocesses. | §3.1, §3.2, §4.6, P4, P5 |
| R14 | A human-present chat session on any host authors tickets instead of editing code, and keeps track of the user's open requests across side questions. The rules ship in every instruction file. | §5.1, P0, P1, P2, P6 |

## 2. Facts this plan rests on

These come from read-only research on chupa at commit `63b89e4`.

### 2.1 The communication mechanism (R3, R4)

- **Invariant 1.** Stages communicate only through artifacts on disk plus the journal.
- **Prompt rendering.**
  - Prompts are rendered from `tempo/specs/<surface>.md` by `specs.render`.
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
  - Merge adds: ticket schema, post-rebase, approval, seed safety. Seed safety is removed in P2 (§2.5).
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

### 2.5 Self-build machinery in the completed code (removed in P2)

Chupa's completed code still carries the machinery it used to build itself. Tempo removes it:

| Where | What |
|---|---|
| `hardening.py` (whole module) | plan-hardening rounds |
| `specs.py` | plan-unit resolver (`resolve_plan_contract`, `unit_sha`, `hardenable_units`, `entry_unit_gap`), plan lint, registry rows |
| `tickets.py` | the `## Plan contract` and `## Exit-read window` sections, plan-unit and seed citation rules, `#19.P…` plan-anchored fence entries |
| `stages.py` | seed-ticket review (`_review_one`, `_restore_approved_seeds`, `_seed_verdicts`), the plan-unit scope-fence anchor (`_unit_confined`), the exit-read window, the eval-report artifacts in `KNOWN_ARTIFACTS` (shakeout, daemon soak, reliability battery, review baseline, host loop, exit receipt) |
| `merge.py` | `SeedSafetyGate`, `gather_seeds` |
| `runner.py` | `hold_on_hardening`, `file_hardening`, `hardening_text`, the `spec_gap` unit routing |
| `journal.py` | the `spec_gap_hold` dispatch value |
| `artifacts.py` | the eval-report and `ExitReceipt` schemas; the `unit` field on findings |
| `requisition.py` | spec-gap plan-unit naming |
| `caps` / `config` | the `hardening` cap |
| `specs/implement.md`, `specs/requisition_review.md` | the `plan_contract` and `prior_review` data blocks, the seeding exception, and plan-unit text. The `render` block in `requisition_review.md` **stays**: it is how the reviewer sees the implement prompt. |
| `specs.py` (rest) | `PlanContractError`, `plan_id`, `required_units`, `without_unit`, `lint_plan`, `_lint_registries`, and their constants |
| `tickets.py` (rest) | `PLAN_FILE`, the `seed` source, `SEED_REFUSED_SECTIONS`, `_seed_cites`, `siblings`, the `plan=` parameter of `parse_ticket` |
| `watcher.py`, `serve.py`, `__main__.py:261-263` | the plan text threaded into ticket parsing |
| `author.py:244`, `requisition.py` | plan reads, `hardenable_units`, `validate_finding_units`, the `prior` parameter. `author.py:244` reads the plan unconditionally and crashes on a host without one. |
| `stages.py` (rest) | `_review_seeds`, `_seeded_stems`, `_prior_seed_reviews`, `lift_seeds`, `_prior_review`, `APPROVED_SEEDS`, `SeedReview`, `Invoice.seeds`, the seeding branch of `check()`, `Evidence.plan_main`/`plan_head`, the plan read in `implement_stage` |
| `stages.py`, `merge.py` | the registered-report ("outbox-only admission") lane, which existed only for the eval reports: `_registered_files`, `_validate_reports`, and their uses in `gather_evidence`, `VerificationGate`, `check()`, `write_squash`, and `lifted_report_paths` |
| `merge.py`, `mergequeue.py` | `SeedOnMain`, `Candidate.seeds`/`seed_checks_text`, the seed parameters of `read_candidate`, the seed lines in `mergequeue` (410, 437-440, 454), and its `"bootstrap"` admission reason |
| `driver.py:231-240` | `materialize_exit_window` |
| `watchdog.py:11, 65-68` | plan-unit scope observation, which only fed spend and spiral detection. It is removed together with cost. |
| `config.py` | `seeding.max_seeds_per_admission` |
| `box.py` | bootstrap ingest: `BOOTSTRAP_ORIGIN`, `ingest_bootstrap`, `ingest_main_checkout`, the `__main__` block |
| `hostfiles.py` `CORE_TEMPLATE` | the self-build lines ("The plan is the seed…", "Start a phase only after…"). `CoreDriftGate` checks hosts against this text, so the template and the gate change together. |
| `journal.py` `SIGNAL_NAMES` | `hardening_round`, `review_baseline`, `diagnose_eval_start`, `drain_handoff` |
| `journal.py:122`, `lockfile.py:49,77`, `seams.py:113`, `__main__.py` | self-upgrade re-exec hooks |
| GO gating (D12) | `baseline.py`, the `review_baseline` signal and the `artifacts.py` baseline family, the `go` input of `policy.start_state`, and the supervised-merge `go_absent` hold in `mergequeue` |
| `runner.py:578-590`, `drain.py:75-94` | the plan-unit release in the keep verb, and the `spec_gap_hold` and plan-unit branches of `premise_parked` |
| docstrings, comments, `config.yaml`, `docs/host-contract.md` | §19 unit citations, "Phase N" and "dormant" staging notes, the bootstrap and seeding text, the "pending cutover" GO section |

**Kept, because they are product behavior:**
- `premise_failed`: the implement outcome for a ticket whose premise is false. Chupa parked the ticket
  until a human edited it. In tempo the orchestrator writes a successor ticket or rejects it (§2.6).
- `## On-demand`: repo paths the implementer may read.
- `spec_gap`/`authoring_error` finding kinds, without the plan `unit`.
  - The field and the spec text asking for it go in the same commit, or every reply fails validation.
  - A `spec_gap` finding takes the generic failure path.

**Removal follows the code, not this table.** P2 traces every caller before deleting. The table is the
starting list, verified against chupa at `63b89e4`.

### 2.6 Human checkpoints in chupa and what replaces them (R13)

| Chupa checkpoint | Tempo |
|---|---|
| GO gating: engine-authored tickets start as drafts; supervised merges are held | removed (D12): authored tickets are confirmed once requisition review approves; merges pass on gates plus review |
| Reject queue: failed tickets wait for an operator to keep or kill them | the orchestrator decides: retry, escalate, split into a successor ticket, or reject. No queue waits on a person. |
| `abandon-human` diagnosis verdict | removed with diagnose; the orchestrator decides |
| review or requisition `rma` ("a human must change the ticket") | the orchestrator writes a successor ticket through `split`, which gets requisition review |
| `premise_failed` park until a human edits `ticket.md` | the orchestrator writes a successor ticket or rejects |
| draft `confirm` verb | automatic |
| operator `pause` / `resume` / `kill` | removed in P5: they spoke to the daemon, which is gone. Stopping the orchestrator chat stops the run; `/tempo-run` resumes it from `status`. |

**Spec text.** Text in the specs telling the model a human will act is reworded to name the
orchestrator.

**The only remaining stop** is a failure that only a person can fix, such as an expired login
(`auth_error`). Tempo journals it, notifies, and stops (§4.6).

## 3. Communication design: chupa's mechanism, run by host subagents

### 3.1 The one real change: who runs the call

Chupa runs each surface as a child CLI and reads its stdout. In tempo, the orchestrator spawns a
host-native subagent instead, for every surface, implement included (R15). `Driver._run` is split at the point where the child process
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
- **replays completed calls**, but only when the latest completed `call_seq` succeeded. `prepare` then
  returns the call marked completed, and no spawn is needed. `submit` re-runs the artifact writer from
  the journaled result.
  - If the latest completion was a charged failure, `prepare` opens `call_seq` + 1 with that
    completion's findings.
  - Chupa's `Effects.run` writes intent and completion inside one call. Tempo splits it into `begin`
    (in `prepare`) and `complete` (in `submit`), with the same journal events.
  - A second intent on a key with no completion is allowed: chupa already re-executes such a key
    (`effects.py`).
- **retries with findings.** On a retry, it reads the findings that the last failed `submit` journaled in
  that effect's completion, and renders them into `retry_findings`.

**Scope.** `prepare` and `submit` serve the six surfaces that survive the conversion: implement (the one
writing surface, §3.2), review, requisition, triage, author, and retro. `diagnose` and `rework` are never
called from P4 on: the P4 orchestrator flow writes a handoff and stops on failure, and P5 deletes them.
The `prepare` and `submit` tools in P4 serve only requisition, implement, and review, the surfaces of
the P4 orchestrator flow. Triage, author, and retro run through `Calls` from P4a; their tool wiring
comes in P5 with the decide rules that trigger them.

**Surfaces with no ticket** (triage, retro, and author before its ticket exists):
- The stem is the one chupa's effect key already uses for that surface.
- The snapshot covers the repo only.
- The read root is the repo.

**The subagent** receives only the call id and those paths. It:
- reads every part
- does its work under the read root named in the prompt header: read-only, except implement, which
  writes inside its worktree (§3.2)
- writes its reply JSON to `output.txt`

Its final chat message is never acted on. The `SubagentStop` hook saves it to `call-NN/stop.txt`, for
failure classification only.

On Claude (P4c live gate), a subagent that hits a limit sends no `SubagentStop`, and one that ends with
`SubagentHandback` sends no message. So the orchestrator's `PostToolUse` hook on the spawn tool also
writes `stop.txt`, from the spawn's result text. The orchestrator spawns in the foreground only, so that
result is the subagent's end, not a launch notice.

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

**Timing rules.**
- `output.txt` is read only once `stop.txt` exists, or after the deadline. Before that, `submit`
  returns `pending`.
- The deadline is judged on the time recorded in `stop.txt`, not when `submit` is called.
- A call whose validation worker has started never times out.

**Limit hits inside a subagent.** A `provider_drought` outcome from `submit`:
- arms `provider-cooldown/<host>`
- counts against `caps.infra`, so a session cannot loop on an exhausted quota

**Provider value.** For a subagent call, the effect completion records `provider` = the host name and
`model` = the resolved model. `served_review` and `ApprovalGate` compare those against implement's
provider and model.

**Charged vs uncharged.**
- A charged outcome completes the effect, and the next `prepare` uses the next `call_seq`.
- An uncharged outcome (`infra_error`, an abandoned call) does not complete the effect. It is journaled
  as a note, and the next `prepare` reuses the same `call_seq` under a new call id.

On a validation or gate failure, `submit` returns the findings. The orchestrator calls `prepare`
again, which renders them into `retry_findings` as today. `caps.retry` counts those re-prompts; an
uncharged `infra_error` call does not count.

`timeout`, `invalid_artifact`, and `gate_failed` keep their meaning and their journal shape.

**New fields.** These fields are new in the journal call record. The effect intent gets four:
- `call_id`: a random 128-bit hex string
- `deadline`: measured from `prepare`
- `snapshot`: the hash of the state snapshot (§3.4)
- `dir`: the call's spool folder, which holds `call.json`

The completion gets two:
- `model_source`: `{resolved: <model>, host_reported: <model or null>}`, copied from `call.json`.
  `host_reported` is filled only when a host's subagent hook payload carries a model field.
- `call`: `{call_id, outcome, final, head, findings}`. It records what `submit` decided, so the next
  `prepare` knows whether to replay, re-prompt with the findings, or stop. `head` is implement's
  branch head after `submit` commits.

An uncharged call or a timeout gets no completion. It is closed by a `subagent_call_closed` signal
naming the call id and the outcome.

**The old path.** P4a deletes every way tempo could launch an agent CLI:
- the CLI adapters, `ProviderLLM`, and the CLI preflight
- `providers[]`, `routing`, and `routing_default_tier`
- provider admission and failover (`ProviderSession`, the `Thresh` slots); the breaker fold stays
- the CLI-stream watchdog
- the `run`, `drain`, `serve`, `triage`, and `retro` commands

`Driver._run` stays through P4 only as the parity-test reference, driven by the fake LLM. The scripted
modules (`drain`, `runner`, `serve`, `daemon`, `diagnose`, `rework`) stay until P5 for their tests,
which also use the fake LLM. They are not wired to the orchestrator and cannot run live. P5 deletes them
and the old path.

### 3.2 Implement is a subagent

**Implement** stays chupa's writing surface, with chupa's `specs/implement.md`, but it runs as a host
subagent through `prepare` → spawn → `submit`, like the read-only surfaces.

**Worktree.** `start(stem)` creates the ticket worktree and branch through `git.py`, as chupa does
(`.tempo/worktrees/<stem>`, branch `<stem>` from `main`). The prompt header names the worktree as the
read and write root.

**What the implement subagent may do** (enforced by hooks, §3.4):
- read the worktree and the repo
- write and edit files inside the worktree, except `tickets/<stem>/ticket.md`, which stays read-only (chupa's
  no-self-editing rule)
- run shell commands with their working directory inside the worktree, to build and test
- never run git, an agent CLI, `gh`, a package publish, or anything that spawns, messages, or resumes an
  agent

**Who commits.** The subagent never runs git. Its `submit` validates the reply, then commits the
worktree's changes to the ticket branch through `git.py` and records the head SHA. The outbox files under
`tickets/<stem>/` stay uncommitted and are lifted as chupa does.

**Limits replaced.** Chupa's stuck kill and process-group abort applied to a child process, which no longer
exists. Their replacement is the call deadline (§4.4.3), with the ticket's `stuck_minutes` as the deadline.
### 3.3 Verification that is kept unchanged (R4)

- **Schema validation.** Every reply is validated by the same pydantic `emits` models:
  `ReviewVerdict`, `RequisitionReply`, `TriageReply`, `AuthorReply`, `RetroArtifact`.
- **Gates.** `run_gates`, with gate-contract checks, severity, and paved roads.
- **Check and merge gates.** Unchanged, except the self-build pieces §2.5 removes: seed safety, the plan-unit fence anchor, and the registered-report lane. After P2 the scope fence checks path entries only.
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
- A read-only subagent writes only its call's `output.txt`. The implement subagent also writes inside its
  worktree, minus its `ticket.md` (§3.2).
- The implement subagent's shell commands must run in its worktree. The hook denies git, every agent CLI
  binary, `gh`, and publish commands. Allowed commands still run unsandboxed, as chupa's implement child
  did (chupa core: "fenced by gates, not jailed").
- A subagent reads only under its call's recorded read root, plus the engine `tempo/specs/` and its call's parts.
- A subagent never spawns, messages, or resumes an agent (R2).
- The spawn prompt is exactly the call id and its paths, and the agent spawned is the one `prepare`
  named.
- The orchestrator has no direct file writes, and its shell runs only `tempo tool` commands. All its
  actions are tempo tools.
- Every hook denies a payload it cannot parse, on all three hosts.

**After the call, `submit` is the check.**
- `prepare` snapshots the repo, the ticket worktree, and `.tempo/`:
  - HEAD
  - `git status --porcelain --untracked-files=all`
  - a content hash of every changed or untracked file
  - a content hash of everything under `.tempo/` except this call's own files (`output.txt`,
    `stop.txt`, `call.json`)
  - the journal's last sequence number

  Gitignored build and venv folders are excluded by name (`.venv/`, build outputs), so the snapshot
  stays fast.
- The snapshot's hash is journaled in the effect intent, not only kept in `call.json`.
- `submit` refuses on any difference outside this call's own files, apart from journal entries written
  by this call's `prepare` and `submit`. For implement, changes inside its worktree (except `ticket.md`
  and the worktree's HEAD) are its work, not a confinement finding.

A write that gets past a hook is caught before output is accepted, anywhere in the repo, the worktree, or
`.tempo/`. Writes outside those trees are not covered.

**State, reads, and calls:**
- `<state>` is `.tempo/`. It is gitignored, and the snapshot excludes it.
- **Subagent shell commands.** Codex subagents read through the shell.
  - The hook allows plain read commands (`cat`, `sed -n`, `head`, `tail`, `rg`, `grep`) on the allowed
    roots and denies every other shell command.
  - Commands are parsed as argv. The hook denies:
    - any shell metacharacter (`| ; & > < $ \``)
    - `sed` scripts containing `w`, `W`, or `e`
    - `rg --pre` and `--pre-glob`
    - any path argument outside the read roots
  - A Codex subagent writes `output.txt` with Codex's own patch tool, allowed only for that file.
  - A write that gets through is still caught by the `submit` snapshot.
- **One open call per session, on every host.**
  - At the spawn `PreToolUse`, the hook takes the call id from the spawn prompt.
  - At `SubagentStart`, it binds the subagent's id to that call.
- **Subagents cannot use tempo tools.** The hook denies any `tempo` command from a subagent.
- **What the orchestrator may use.**
  - Allowed: `tempo tool` commands in its shell, the host spawn tool (spawn-checked), and file read
    and search tools.
  - Denied: every other shell command, write, and edit.
- **Which session is the orchestrator.** `begin_session` (§4.6) records the host's chat `session_id`
  from the hook payload in the lockfile. Hooks apply the orchestrator rules to that chat session and the
  subagent rules to `tempo-*` subagents. Every other chat session in the repo, such as one authoring
  tickets, gets no tempo enforcement. Unparseable payloads are always denied.
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

Every tool is an existing effect-keyed function, or a thin wrapper over one. There is no MCP and no
server. The orchestrator calls a tool by running `tempo tool <name> '<json args>'` in its shell; the
command prints a JSON result and exits. All state is in files and the journal, so each call is a fresh
process.

**Session token.**
- `begin_session` (§4.6) takes the single-writer lockfile, creates a session token, and records it in the
  lockfile with the host name and the chat `session_id`. Tools read the host from there for model
  resolution and agent names.
- `begin_session` in a second chat, on any host, finds the lock held and refuses, naming the holder.
- Every tool except `begin_session` and `status` refuses unless its session token matches the lockfile.
- This gives:
  - a single writer across hosts
  - per-session counters for session limits

**Long-running work is start-then-poll.**
- `check(stem)` and `merge(stem)` return a job id at once. They run git and verification commands,
  never an agent.
- `job(id)` returns the job's state.
- **Jobs outlive sessions.** Jobs run in detached worker processes, with their state kept in
  `.tempo/jobs/`. A job whose worker died reports `lost` and is recorded as `infra_error`, uncharged.
  Effect replay decides whether it re-runs.
- **What P4 builds.** The Read and Run tools, plus `write_handoff`. P5 builds the Decide tools and the
  rest of Record.
- **Single writer.** Every journal and git write takes one repo lock: tools, job workers, and hooks. That lock is chupa's single-writer lock. The session token gates tools; the lock
  serializes writers.
- **`split` is two tools.**
  - `split_propose` runs rework's proposal checks and journals the proposal.
  - The orchestrator then runs requisition review on each proposed ticket through `prepare`, spawn,
    and `submit`.
  - `split_commit` publishes the approved tickets.

  A tool cannot spawn a subagent.
- No tool call blocks longer than `TOOL_CALL_S`, a code constant set in P4 below the shortest host
  shell-command timeout.

| Kind | Tools |
|---|---|
| Read | `status` (includes open calls, so a resumed session recovers them), `list_eligible` (the mechanical half of `drain._select`: confirmed, deps settled, not held, caps remaining, no drought), `read_ticket`, `read_attempts` (`prior_attempts`), `read_box` |
| Session | `begin_session` (takes the lock, §4.6); `write_handoff` releases it |
| Run | `start(stem)` (journals `{to: running}`, allocates `attempt`/`run_seq` as `drain._run_one` did, and creates the worktree), `prepare`, `submit`, `check`, `job`, `merge`. `prepare` serves `requisition_review`, `implement`, `review`, `triage`, `author`, and `retro`. |
| Decide | `retry`, `escalate` (offers only `caps.next_rung`'s rung), `reject`, `park`, `release_hold`, `reoffer`, `split_propose` / `split_commit` (see Split above), `confirm`. Each journals the same `state_transition` shape as `failure_terminal`: `{to, stage, reason, dispatch, rung}`. Each takes a required `lessons` argument, which the tool appends to `tickets/<stem>/decisions.md`. |
| Record | `file_box`, `notify`, `write_handoff` (rewrites `tickets/orchestrator/handoff.md`) |

**Decide tools on a failed run (P5a).** The tool that sees a stage end final and not ok (`prepare`,
`submit`, `check`, `merge`) journals a `stage_failed` signal `{stage, outcome, reason}`; `reason` is the
sorted finding codes, or the outcome when there are none. A Decide tool on a running stem reads that
signal, then does what `runner.drive` did after a failure: harvest the attempt, draw the caps (`infra`
for `infra_error`/`timeout`, `premise_bounce` for `premise_failed` except `render_over_bound`, and
`decisions` for every decision), append `lessons` to `decisions.md` and commit it, journal the terminal,
and remove the worktree.

| Tool | Does | Refuses |
|---|---|---|
| `retry` | terminal with dispatch `retry`; the next `start` re-offers it | caps spent; `IDENTICAL_K` |
| `escalate` | terminal with dispatch `escalate` and `caps.next_rung` for the session host; the next `start` runs at that rung | caps spent; no next rung |
| `reject` | on a failed run, terminal with dispatch `reject_queue`, then the ticket is stamped `rejected`; on an idle stem, only the stamp | settled stem |
| `park` | terminal with no dispatch; `list_eligible` skips the stem until its `ticket.md` changes, as for `premise_failed` | — |
| `reoffer` | releases a park or a `premise_failed` park at the same `ticket.md` | stem not parked; caps spent |
| `release_hold` | journals the accepted `resume` control decision that releases a storm hold; `list_eligible` skips storm-held stems | hold not live |

**Box, split, confirm, and retro (P5b).** `prepare` takes the surface plus what it works on:

| Arguments | Call |
|---|---|
| `{surface: requisition_review \| implement \| review, stem}` | the ticket flow, unchanged |
| `{surface: triage, message}` | triage of one pending box message |
| `{surface: author, message}` | the ticket for a message triage sent to author |
| `{surface: requisition_review, message}` | review of the author's candidate ticket |
| `{surface: requisition_review, stem, proposal}` | review of one proposed split ticket |
| `{surface: retro}` | one retro |

- **Triage.** The orchestrator triages each pending message whenever `read_box` shows one. The first
  triage `prepare` in a session journals `triage_pass`. Work that needs no model happens in `prepare`,
  which returns `completed` with nothing to spawn: a retro ticket recovery, an existing record, a
  superseded author verdict, an author run that never settled. A final ok `submit` does what
  `triage_pass` did after the call: it commits a decision or tombstone record and resolves the message,
  or records the `author` verdict and returns `next: author`.
- **Author.** `submit` runs `AuthorGate`; a final ok reply becomes the candidate and returns
  `next: requisition_review`. The review's `submit`: approve commits the ticket, journals
  `ticket_intake` and `requisition_verdict`, and resolves the message; snag returns findings, and the
  next author `prepare` re-prompts with them up to `caps.retry`; rma or the cap spent writes the failure
  decision record.
- **Split** replaces rework. The orchestrator writes the proposals itself; there is no rework subagent.
  - `split_propose {stem, tickets: [{stem, ticket}], lessons}` is a Decide tool on a failed run. It runs
    rework's proposal checks (at least two fresh stems, valid tickets, `source`, `state`, `agent_tier`,
    and `agent_effort` kept, no successor depending on the original), journals `rework_order`, then
    harvests, draws caps, writes lessons, and journals the terminal with dispatch `split`.
    `list_eligible` skips a stem whose split is pending.
  - Each proposal gets a requisition review through `prepare` and `submit`.
  - `split_commit {stem}` commits every proposal and records `supersedes`. It refuses unless every
    proposal has an approve verdict for its exact text.
  - Rework's `update` action goes (writing an existing `ticket.md` is refused); its `escalate` is the
    `escalate` tool.
- **Confirm.** `confirm {stem, lessons}` wraps `backlog.confirm_draft`: draft to confirmed.
- **Retro.** `status` returns `retro_due` from `select_trigger`; the orchestrator runs retro when it is
  true. `prepare retro` refuses when retro is not due, and handles a suppressing failure or an
  already-committed report itself, returning `completed`. A final ok `submit` commits the report and
  files its proposals in the box; a final failure journals `retro_failed` and files a failure report.

**Refusals, enforced in the tool:**
- caps spent
- bad or missing session token
- merge without an approval for the admitted SHA
- `submit` for an unissued call
- writing an existing `ticket.md`
- `retry` once the same terminal reason has repeated `IDENTICAL_K` times. The reason is derived mechanically from the outcome, never from orchestrator text.
- **at `max_tickets`:** `start` only
- **past `max_minutes`:** every tool except `status`, `job`, `submit`, and `write_handoff`.
- **one thing at a time:**
  - While a subagent call is open, every tool that writes or journals is refused except `submit`. That
    includes `start`, the Decide tools, `file_box`, `notify`, `write_handoff`, the jobs, and another
    `prepare`. `status` and `job` (read-only) stay allowed.
  - While any job is running, including one left from an earlier session, `prepare` is refused.

  The `submit` snapshot then only ever sees the call's own changes. Chupa also ran serially
  (`concurrency: 1`), so this is no change in behavior.
- **calls from a dead session:** a call opened under an earlier session token is abandoned.
  - The new session's `status` lists it.
  - The next `prepare` on that stem and surface records it: `timeout` if its deadline has passed,
    otherwise `infra_error`, uncharged.
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
| serve box consumer | triage whenever any box message is pending (hardcoded) | — |
| drain retro trigger | runs retro when `select_trigger` fires | trigger facts |
| watchdog spiral warning | nothing; it was spend-based and is removed | call deadline (time-based, §4.4.3) |

`IDENTICAL_K` (confirmed in P5): it compares the `reason` of the ticket's earlier terminal
`state_transition` events, never diagnose output. It moves from `runner` to `caps`, beside `spent` and
`next_rung`.

### 4.3 Single depth (R2)

1. **Definitions.** No subagent tool list contains a spawn tool.
2. **Hooks.** Any spawn, message, or resume from a subagent is denied (§3.4).
3. **Tests and live gate.** Each host's payloads are captured live in P4.

The implement subagent is held to the same rules as every subagent: no spawn tool, hooks deny spawns
and tempo tool calls, and its shell may not run an agent CLI (§3.4).

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

Chupa's `providers[]` and `routing` rows configured child CLIs. They are removed in P4. Implement models
come from `hosts.<host>.models_by_tier` (§4.4.2), like every other surface.

| Parameter | Location | Default | Read by |
|---|---|---|---|
| Circuit breaker | `circuit_breaker.{k, cooldown_minutes}` | `3`, `10` | `thresh` |
| Starting tier and effort | ticket frontmatter `agent_tier`, `agent_effort` | `medium`/`medium` | `caps.capability` |
| Ladder | `caps.next_rung` (code) | next tier with a distinct implement model, then effort +1; when exhausted, the orchestrator rejects or splits (§2.6) | `escalate` |
| Repeated-failure threshold | `IDENTICAL_K` (constant) | 3 | `retry` refusal |
| Re-prompts per call | `caps.retry` | 6 | `prepare`/`submit` |
| Other per-ticket caps | `caps.{decisions, infra, premise_bounce}` | chupa defaults (`diagnosis` renamed `decisions`) | decide tools |
| Ticket wall-clock limit | `tickets.max_minutes` (was `drain.max_ticket_minutes`) | chupa default | `prepare`, `list_eligible` |
| Implement stuck limit | ticket `stuck_minutes` | chupa default | implement call deadline |
| Prompt size bound | `RENDER_BOUND_CHARS` 400,000, `REQ_RENDER_HEADROOM` 0.75 (constants) | same | `prepare` |
| Diff budget | constants: 30 files / 1,500 inserted lines | same | `check` |

#### 4.4.2 Subagent models per host

```yaml
hosts:
  claude:
    models_by_tier: {low: claude-sonnet-5-5, medium: claude-sonnet-5-5, high: claude-sonnet-5-5, max: claude-sonnet-5-5}
    pins: {review: claude-opus-5-5}
    quota_window_minutes: 60
  codex:
    models_by_tier: {low: gpt-6-luna, medium: gpt-6.1-sol, high: gpt-6.1-sol, max: gpt-6.1-sol}
    pins: {review: gpt-6-astra}
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
  - Reachable hosts are those with an `orchestrator.models` entry.
  - Each needs a `hosts.<h>` entry and an `orchestrator.models.<h>` entry, or load refuses.
  - Review agents are not rendered for a host or tier that cannot run review.
  - `render` always writes hook files and `tempo:protocol` blocks.
  - `render` writes model-bearing agent files only for reachable hosts.
  - `render` deletes stale generated `tempo-*` agent files.
- **Drift.** There is one staleness check, `render --check`, called by the drift test and by `doctor`.
  `prepare` refuses when the agent it would name does not exist.
- **Review separation (D19).**
  - Implement and review run on the same host now, so separation is by model: the review pin must not
    appear in that host's `models_by_tier`. Config load refuses a host that breaks this.
  - The defaults keep the strongest model of each host for review and cap implement below it. The
    ladder (§4.4.1) then escalates effort once the tier models are used up.
  - `served_review` and `ApprovalGate` are unchanged: they compare the recorded review model against the
    recorded implement model.

#### 4.4.3 Subagent timeouts

- **Deadline.** `prepare` records a deadline from chupa's existing stuck limits:
  - `REQUISITION_STUCK_S`, `TRIAGE_STUCK_S`, and `AUTHOR_STUCK_S` (constants)
  - the ticket's `stuck_minutes`, for review
  - `RETRO_STUCK_S`, a new constant set to chupa's `call_timeout` default, for retro
- **Late reply.** `submit` after the deadline records `timeout`.
- **Hung subagent.** A call past its deadline is hung. The orchestrator stops it with the host's own
  stop tool for a running subagent, then calls `submit`, which records `timeout`. The P4 live gate
  confirms each host's stop tool. A host without one fails the gate.

#### 4.4.4 Orchestrator session

```yaml
orchestrator:
  models: {claude: claude-opus-5-5, codex: gpt-6-astra}   # copilot added after the P4 live gate
  session:
    max_tickets: 5                 # tickets started before the session hands off
    max_minutes: 240               # tools refuse new work after this; the session hands off
```

**How the orchestrator model reaches each host:**
- Claude and Copilot: a `model:` field in the rendered orchestrator file.
- Codex: `model` in the rendered orchestrator profile. The P4 live gate confirms the location.

#### 4.4.5 Provider limits, no cost

- **A limit inside a subagent** comes back from `submit` as `provider_drought` (§3.1). It arms
  `provider-cooldown/<host>` using `hosts.<h>.quota_window_minutes`. The orchestrator writes the handoff,
  and the session ends (§4.6).
- **Removed:**
  - `est_cost_per_call_usd`
  - `--max-budget-usd`
  - `budget_usd`, `budget_exceeded`
  - the effect `cost` fields and `StageResult.cost`
  - watchdog spend and spiral detection
  - retro, scorecard, and status spend and token measures
- **Kept:** `provider` and `model` in effect completions, because `served_review` needs them.

#### 4.4.6 Not configurable, on purpose

- subagent depth: 1
- Codex subagent threads: 1
- triage cadence (§4.2)
- paths
- these constants, set in the P4c commit from live-gate measurements, with placeholders in tests until
  then:
  - `READ_PART_CHARS`: the smallest whole-file Read size across hosts that pass the gate
  - `TOOL_CALL_S`: the shortest host shell-command timeout, minus a margin
  - `LOCK_STALE_S`: several times `TOOL_CALL_S`, so a live session is never taken over
  - `RETRO_STUCK_S`: chupa's `call_timeout` default

### 4.5 Session model

- A session runs until the queue is quiescent, or `max_tickets` or `max_minutes` is reached, and ends
  with `write_handoff`.
- Resuming from `status` alone must be correct.
- `handoff.md` is advisory.
  - `write_handoff` commits it through the single writer and journals the session number.
  - A handoff from an earlier session number is ignored.
- This replaces drain, the daemon loop, and the self-upgrade re-exec.
- The orchestrator loops inside one host session. It needs no fresh session for context: all state is
  in files and the journal, so the host's own context compaction loses nothing that `status` cannot
  give back.

### 4.6 Starting and ending sessions in VS Code (no supervisor)

There is no `tempo supervise`, and tempo launches no process that runs a model (R15). The orchestrator is
the chat agent of the host's VS Code extension.

**Start.** In the extension's chat panel, the user runs the tempo start command (`/tempo-run`; per-host
files in §5). It loads the orchestrator rules into that chat. Its first tool call is `begin_session`,
which takes the lock (§4.1), then `status`. Subagents spawned from that chat are the single layer
beneath it. Starting is not a checkpoint inside the loop: the user writes tickets and starts the run,
and no human step follows (R13).

**Pre-start check.** `begin_session` refuses when the host's hook files fail `render --check`, so
a repo where `tempo init` skipped a host's settings never runs without hooks.

**End.** The session ends with `write_handoff`, which also releases the lock, when:
- the queue is quiescent (no eligible ticket, no pending box message)
- `max_tickets` or `max_minutes` is reached
- `submit` reports `provider_drought` (a cooldown is armed) or `auth_error` (journaled, notified)
- the breaker opens on this host (`circuit_breaker`)

**A chat closed mid-run.** The lock records the chat `session_id` and a heartbeat that every tool call
renews. `begin_session` takes over a lock whose heartbeat is older than `LOCK_STALE_S`.

**Resume.** The user runs `/tempo-run` again, in any host's extension. It resumes from `status`. A call
left open is recorded by the next `prepare` (§4.1). A host still cooling down shows in `status`, and
the orchestrator ends at once with a handoff naming the cooldown's end.

**Cooldown keys** are per host. On flat-rate plans one subscription serves the whole host, so one key
covers every model on it.

## 5. Governing system for three hosts (R5)

**Agent files are thin.** A surface agent says only: read the prompt parts at the given paths, write the
reply JSON to the given path, nothing else. `tempo/specs/*.md` stays the only description of what each surface
does, and `prepare` renders it per call.

| | Claude Code | GitHub Copilot | Codex |
|---|---|---|---|
| Instructions | `CLAUDE.md` | `.github/copilot-instructions.md` | `AGENTS.md` |
| Orchestrator rules | `.claude/agents/tempo-orchestrator.md` | `.github/agents/tempo-orchestrator.agent.md` | `.codex/tempo-orchestrator.md` |
| Start command in the VS Code chat (§4.6) | `.claude/commands/tempo-run.md` | `.github/prompts/tempo-run.prompt.md` | `.agents/skills/tempo-run/SKILL.md` (a skill; Codex has no project prompts) |
| Surface agents (generated, §4.4.2) | `.claude/agents/tempo-<surface>-<model>.md` | `.github/agents/tempo-<surface>-<model>.agent.md` | `.codex/agents/tempo-<surface>-<model>-<effort>.toml` |
| Hooks | `.claude/settings.json` | `.github/hooks/tempo.json` | `.codex/hooks.json` |
| Caller identity | `agent_id`/`agent_type` | `parentAgentId`/`agentType` | markers, 1 thread |

**Instruction files.** `CLAUDE.md`, `AGENTS.md`, and `.github/copilot-instructions.md` each carry
chupa's managed `core` block, renamed `tempo:core`, a generated `tempo:protocol` block, and the
`tempo:session` block (§5.1).

### 5.1 Session conduct: chat sessions write tickets (R14)

**The gap.** Chupa has chat rules (its `CLAUDE.md` rule set B), but they fall short in three ways:
- They sit outside the managed `core` block, so `tempo init` would not ship them to host repos.
- `AGENTS.md` leaves them out, so Codex and Copilot chats get none.
- They have no ticket-first rule, and their open-threads rule is a one-liner. Boardgameui's
  `CLAUDE.md` holds the rules that work in practice: ticket first, author identified work, announce
  dives, instance plus cause, and track open threads. They are spread over about 300 lines there.

**The fix.** A third managed block, `tempo:session`, comes from one source file, `tempo/rules/session.md`.
`render` writes it into all three instruction files, so the hosts cannot drift apart. It is
boardgameui's rules cut down to the minimum. Boardgameui-only machinery is left out: orchestrator
lanes, phase waves, clone layout, and lint bundles.

**Scope.** The block applies to a VS Code chat that is not running tempo. A chat that has run
`/tempo-run` is the orchestrator and follows the orchestrator rules instead; hooks already fence it
(§3.4). Every `tempo-*` subagent skips the block, because the implement subagent must edit code.

**Proposed text** (ASCII in the shipped file):

```markdown
<!-- tempo:session begin -->
## Chat session conduct

Applies to a chat that is not running tempo. A chat that has run /tempo-run follows the
tempo-orchestrator rules instead, and every tempo-* subagent skips this block.

- **Ticket first.** Answer any request to change repo content (code, docs, config, rule files,
  hooks) by authoring a ticket, never by editing. Run `tempo new <stem>`, fill
  `tickets/<stem>/ticket.md` per `docs/tempo/tickets.md`, run `tempo lint <stem>` until it exits 0,
  then stop and report the stem. Never edit tracked files outside `tickets/` from chat.
  Only these exceptions apply: (1) the user says "edit directly", "skip the ticket", or the
  equivalent in the same turn. Anger is not an override. (2) The user directs an edit to an
  existing ticket file. (3) Read-only inspection.
  If you edited without an exception, stop, say so, and offer to revert and refile as a ticket.
  Why: a chat edit skips the gates, review, and record that make the work trustworthy.
- **The ask is the confirmation.** Author a ticket the user asked for without asking "should I file
  this?". Intake confirms it.
- **Split by feature and edit surface.** One ticket per feature and set of files it changes, never
  sized to fit a context. Ordering goes in `## Depends on` and `priority`, never in prose.
- **Identified work becomes a ticket or an open thread, never prose.** Never write "we could file
  a follow-up". For work the user did not request, name it in 1-2 sentences and ask: author now,
  author after the current task, or skip. "After" makes it an open thread.
- **Announce unsolicited dives.** Before an investigation the user did not ask for that will take
  more than a couple of minutes, give one line on what and why and get now / after / skip. The
  requested task always runs first.
- **Instance first, cause captured.** A reported problem gets two tickets in one turn. The minimal
  unblock comes first, and no cause diagnosis starts before it exists. Then a separate cause
  ticket. The turn is not done until both files exist.
- **Track open threads.** The user's main request is thread 1; answering a side question never
  closes it. End every reply that leaves a question, option, or next step unresolved with
  `Still open:` and one line per thread, every turn until the list is empty. A thread closes only
  when the user resolves or dismisses it, or you finish it and say so. If unsure, it is open.
- **Git safety.** Commit only files you authored, by explicit path. Never use a tree-wide
  destructive verb (`clean`, `reset --hard`, `checkout -- .`). Never push unless told to.
- **Terse.** Answer first. No hedging, filler, or restating the question.
- **No guessing on code or tools.** Never state a setting, menu, command, flag, field, file path, API, or
  model name you have not read or captured. If you do not know it, say so and ask the user what their
  screen shows, or look it up first. Why: a wrong name costs the user a round trip and their trust.
<!-- tempo:session end -->
```

**What the ticket-first rule needs from the code** (both in P6):
- `tempo lint <stem>` validates an existing ticket with the same `validate_ticket` that `tempo new`
  uses. Chupa can lint only a fresh template, so a chat could not check the ticket it filled in.
- `docs/tempo/tickets.md` holds the ticket contract: frontmatter fields, body sections, and grammar.
  It is extracted from TEMPO_PLAN §13 and shipped by `tempo init`, because host repos do not get
  TEMPO_PLAN.

**This repo.** Tempo never builds itself (D4), so ticket first cannot apply here. This repo's
project-owned part of `CLAUDE.md` and `AGENTS.md` replaces it with **plan first**: a change request
becomes an `AGENT_PLAN.md` edit, and the plan is approved before any code changes. Every other
session rule applies unchanged.

**Enforcement.** The rules are prose only, as in boardgameui, where prose works. A hook that denies
chat edits outside `tickets/` is left out until a chat breaks the rule anyway. A hook cannot see a
same-turn override, so it would need its own escape path.

## 6. Packaging (R9)

- **Build.** `uv build` uses hatchling, because uv's own backend cannot put `.github/` and other
  top-level files into a wheel.
- **Names.** The distribution is `tempo-agents`, because `tempo` is taken on PyPI. The import name and
  CLI are `tempo`.
- **Package data, no shared-data.** Every host file depends on the repo's `config.yaml`, so the wheel
  ships the sources, not rendered copies: `tempo/rules/` (the rule templates, `session.md`,
  `tickets.md`, and the default `config.yaml`) and `tempo/specs/` (the prompt specs, moved from the
  top-level `specs/` so an installed engine can load them). Both ship inside the `tempo` package.
- **Why `tempo init` is needed.** An installer places data files under the environment prefix, never in
  a project folder, and a wheel runs no install-time code. So `tempo init`:
  - writes a default `config.yaml` if there is none
  - merges `tempo:core` into each configured host's instruction file (as `tempo core` does)
  - runs `render`: the `tempo:protocol` and `tempo:session` blocks in all three instruction files, the
    hook files, and the model-bearing agent files from the repo's `config.yaml`
  - writes `docs/tempo/tickets.md`, or skips and reports it when an existing copy differs
  - appends `state_dir`, and `worktree_root` when set, to `.gitignore` as `/<path>/` lines when they
    sit inside the repo and no line already names them, so the journal and worktrees are never committed
- **Hook files merge.** `render` owns only the top-level `hooks` key (plus `version` on Copilot) of an
  existing hook JSON file such as `.claude/settings.json`, and keeps every other key. A file that is not
  a JSON object is refused with a paved road. Drift compares the merged result, so a repo with its own
  settings can still start a session.
- **Publishing.** A publish workflow is deferred until after P7. Until then a release is a manual
  `uv publish`.

## 7. Decisions (decided 2026-11-01)

These were decided first, then revised by the review dispositions you accepted.

- **D1 — orchestrator memory.** Decided. Lessons go into `tickets/<stem>/decisions.md` through the
  decide tools' `lessons` argument, and `prior_attempts` reads them.
- **D2 — implement routing.** Revised by D17. Implement models come from `hosts.<h>.models_by_tier`
  like every surface, and escalation picks the rung. Chupa's `providers[]` and `routing` rows go.
- **D3 — cost.** Decided. There is no cost accounting. The only provider concern is failing over on a
  limit (§4.4.5, §4.6).
- **D4 — self-build.** Decided: tempo never builds itself. Its self-build machinery is removed in P2
  (§2.5). No seed build rules are ported.
- **D5 — test host.** Revised by Cut C5 and Kill K1.
  - Host fakes and a minimal fixture repo are built in P4, where the exits first need them.
  - P7 uses the same fixture for live runs.
- **D6 — subagent model routing.** Decided: `hosts.<host>.models_by_tier` + `pins`, with generated
  agents (§4.4.2).
- **D7 — effort per host.** Revised by Cut C4. Codex carries effort. Claude and Copilot carry none.
- **D8 — subagent timeouts.** Revised by D17: a deadline in `submit`, plus the host's own subagent stop
  tool. The supervisor kill is gone with the supervisor.
  - The per-host prompt bound is removed (Cut C8).
  - It is added back only if a live run shows a host rejecting a prompt under `RENDER_BOUND_CHARS`.
- **D9 — orchestrator session.** Revised by Cut C3 and Kill K9.
  - Settings: `models`, `max_tickets`, and `max_minutes`.
  - The triage threshold is hardcoded.
  - Context budget is not a session limit.
- **D10 — Codex reply forging.** Decided: accept the risk. While a Codex subagent runs, the Codex
  orchestrator could write `output.txt` itself (§3.4, §9).
- **D12 — GO gating.** Decided: removed. Automation is the point (R13). Engine-authored tickets are
  confirmed after requisition review, and merges pass on gates plus review.
- **D13 — no human in the loop.** Decided (R13, §2.6). Every human checkpoint moves to the orchestrator
  or is removed. The only stop is `auth_error`.
- **D14 — the 22 scripted-layer test files.** Decided: carried, because several cover kept code (the
  stuck kill, process-group abort, cooldown timers, eligibility, pause). Each is deleted together with
  its module in P5.
- **D17 — no agent CLI.** Decided (R15). Implement is a host subagent (§3.2). The supervisor,
  `providers.CliAdapter`, and `routing` rows are removed. Git and verification
  commands remain subprocesses.
- **D18 — where sessions run.** Decided: in each host's VS Code extension. The user starts the
  orchestrator with `/tempo-run` in the chat panel, and its subagents are the single layer beneath it.
  No headless session, scheduler, or CLI launch.
- **D19 — review separation on one host.** Decided: a model never reviews its own work. Each host's
  strongest model is pinned to review, and implement is capped below it; config load refuses a host
  whose review pin appears in its `models_by_tier` (§4.4.2).
- **D11 — Copilot models.** Decided: they live in `config.yaml` under `hosts.copilot`, in the same
  format as the other hosts. The operator fills them in. The default config ships the section commented
  out.
- **D16 — chat session conduct.** Proposed (§5.1). A `tempo:session` managed block in all three
  instruction files carries boardgameui's chat rules in minimal form: ticket first, the ask is the
  confirmation, identified work becomes a ticket or an open thread, announced dives, instance plus
  cause, and tracked open threads. It is prose only, with no hook. This repo uses plan first instead
  of ticket first.

## 8. Phases

Before each phase, its exact file list is posted for approval. P1–P3, each of P4a, P4b, and P4c, and P6 are one commit each. P5a and P5b
are each a series of removal commits.

### P0 — Plan

**Work:**
- The empty repo exists (done). This plan.
- An interim `CLAUDE.md` and `AGENTS.md` for this repo. Each holds only the §5.1 session block, with
  plan first in place of ticket first, so chats in this repo follow the rules before P1.

**Exit:** You approve the plan.

### P1 — Carry and rename

**Work:**
- Carry over chupa's 54 `chupa/` modules as `tempo/`.
- Carry over `specs/`, `config.yaml`, `docs/host-contract.md`, `CLAUDE.md`, `AGENTS.md`,
  `.gitignore`, `pyproject.toml`, and the 80 test files not listed in §2.3.
  - Chupa's rule set B in `CLAUDE.md` is replaced by the P0 session block. `AGENTS.md` gains the same
    block.
- Two carried test files (`test_gates`, `test_stages`) import helpers from uncarried evidence tests.
  Those tests also import chupa's `eval/` and `hosts/`, so they are self-build evidence: no
  `tests/helpers.py`; P2 removes them.
- Write `TEMPO_PLAN.md` from SEED_PLAN.md filled with "tempo", keeping only the product features. The
  exact cut list is posted for approval first:
  - drop §0 cold start, §19 phases and registries, §21 bootstrap blocks, and §22's build provenance
  - keep the seed's section numbers, with gaps
  - keep §22 as "Contract constants", for the entries that P4 adds
  - drop the self-build passages inside the feature sections: seeding tickets, plan entry units, plan
    hardening, phase exits, GO-grade corpus building
- Generate `README.md` from SEED_PLAN §21's README block, as a one-off extraction.
- Rename chupa → tempo in identifiers, markers, `.chupa/`, data sentinels, and file names.
  - The rename preserves case: chupa→tempo, Chupa→Tempo, CHUPA→TEMPO.
  - It is a one-off script and is not committed.
- Regenerate `uv.lock` with `uv lock`.
- Name the project `tempo-agents` in `pyproject.toml`. It stays virtual until P6.
- Run the full suite. Classify every failure as one of:
  - (a) pinned to chupa's plan text or build evidence — becomes input to P2. After the rename the engine
    reads `TEMPO_PLAN.md` as a plan-contract source, so these failures are expected until P2 removes
    the plan reads.
  - (b) a real break — fixed now

**Exit:**
- The failure list, with causes and proposed handling, is approved.
- No "chupa" string remains in carried files. This is checked case-insensitively; `AGENT_PLAN.md` is
  excluded.
- `python -m tempo status` exits 0 at the repo root with an empty `.tempo/`.

### P2 — Strip self-build machinery and cost

**Work:**
- Remove the §2.5 self-build machinery and GO gating from code, specs, config, `CLAUDE.md`, `AGENTS.md`,
  and `docs/host-contract.md`, tracing every caller.
- Remove the "Supervising session" rule from `CLAUDE.md`. It names the conductor, drain, and Reject
  queue, which P2 and P5 remove.
- Apply the §2.6 human-checkpoint changes that live in carried, kept code. P5 completes those in removed
  modules.
- Edit or delete the tests that exercise them. The code check found these across:
  - `test_hardening`
  - `test_host_contract`, `test_watchdog_activation`, `test_vocabularies`
  - the entry-unit tests in `test_baseline`, `test_bug_gate`, `test_cli`, `test_config`, `test_hostfiles`,
    `test_inbox`, `test_retro`, `test_retro_box`, `test_scorecard`, `test_status`,
    `test_supervised_merge_hold`
  - the seed, plan-unit, exit-window, report-lane, and GO cases in `test_specs`, `test_tickets`,
    `test_requisition`, `test_stages`, `test_merge`, `test_mergequeue`, `test_gates`, `test_closed_sets`,
    `test_reject_queue`, `test_driver`, `test_watchdog`, `test_box`,
    `test_storm_notification_activation`, `test_author`, `test_policy`

  Shared fixtures that contain `## Plan contract` or write a plan file are edited, not deleted. Each
  deletion and edit is listed for approval first.
- Remove cost accounting (§4.4.5) from code, tests, and specs.
- Resolve the P1 (a) failures. Each is either self-build pinning, and removed with its code, or a
  feature test, pointed at TEMPO_PLAN.md.

**Exit:**
- Full suite green.
- No reference to plan units, seeding, hardening, phase exits, GO, or cost remains in `tempo/`,
  `specs/`, `config.yaml`, `CLAUDE.md`, `AGENTS.md`, or `docs/`. This is checked by a word-bounded grep,
  with the remaining hits reviewed by meaning.
- No code path reads `TEMPO_PLAN.md`.

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
- Drop `expected` from the ticket `## Time budget` section. After P2 removed the watchdog's soft
  region, nothing reads it at run time. It goes from `Ticket.expected_minutes`, the grammar check
  (`stuck` must exceed `expected`), retro calibration (`retro.py` regex), specs, templates,
  TEMPO_PLAN.md, and tests. `stuck` stays.

**Kept set.** The kept set is every `tempo/` module except `drain`, `daemon`, `serve` (the daemon's
foreground lifetime), `runner`, `rework`, and the CLI entry `__main__`.
- `__main__` still dispatches `drain` and `serve` until P5.
- Each P5 removal deletes its CLI command in the same commit, and `__main__` joins the kept set at the
  end of P5.
`stages` stays: P5 removes only its `diagnose` part. If a kept module imports `rework`, that link is cut
too. The move list above is a minimum, not the whole list.

**Exit:**
- An import-graph test proves the kept set imports none of `drain`, `daemon`, `serve`, `runner`, or
  `rework`.
  - It follows imports transitively.
  - It counts every import, including function-level and `TYPE_CHECKING` imports.
- Suite green.

### P4 — Agent layer, config, live gate

P4 runs as three sub-phases, one chat and one commit each, so the code work is not blocked on the live
gate:
- **P4a:** (a), the §4.4 config edits, and deleting the CLI path (§3.1, "The old path").
- **P4b:** (b), (c), the rest of (d), and (e).
- **P4c:** (f), and setting the §4.4.6 constants from the live-gate measurements.

**Work:**
- **(a)** Split `Driver._run` into `prepare`/`submit` (§3.1): prompt parts, the
  state snapshot, stop classification, and the session token. Implement moves onto the same path (§3.2),
  with `submit` committing its worktree changes. Remove `providers[]` and `routing` from config.
- **(b)** Start-then-poll tools and the `tempo tool` command (§4.1).
- **(c)** Hook policy and adapters for three hosts, failing closed on unparseable payloads (§3.4).
- **(d)** Role definitions and `render` (§4.4.2, §5), the config edits (§4.4), and session start and
  end (§4.6).
  - P4's `render` writes the host files and the `tempo:protocol` block into the fixture repo (e), never
    into this repo, so a hook fault cannot break chats here. The live gate runs with the fixture repo
    open in VS Code. P6 packages the files.
  - The orchestrator's instructions in P4 cover the call protocol in chupa's stage order:
    - `status` → `start`
    - requisition (`prepare`/spawn/`submit`)
    - implement (`prepare`/spawn/`submit`) → `check`
    - review (`prepare`/spawn/`submit`)
    - `merge`

    On any failure it writes the handoff and stops. The decision rules come in P5.
  - Add a plan §22 entry for each new contract constant.
  - Update `docs/host-contract.md`.
  - Give `pyproject.toml` the hatchling build backend and the `tempo` script, so the fixture repo can
    run `tempo` (`uv tool install --editable <tempo checkout>`). P6 adds the shared-data files.
- **(e)** Host fakes and a minimal fixture repo.
- **(f) Live gate**, in the VS Code extension of each of Claude Code, Copilot, and Codex:
  - before the gate, the hook file layouts are corrected from each host's docs, so the hooks load at all.
    `tempo hook` writes every raw payload and its reply, redacted, to the engine log; the captures come
    from there.
  - `/tempo-run` starts the orchestrator in the chat panel and `begin_session` records the chat's
    `session_id`; hooks fence that chat and leave a second, plain chat alone
  - capture hook payloads for a subagent spawn, a read, a write, a shell command, a nested spawn, an
    agent message, and an orchestrator write. The unparseable and mismatch cases are made by altering the
    captured payloads. "Submit of an unissued call" is tested at the tool level.
  - run one `prepare` → spawn → `submit`
  - run one implement subagent in a worktree: allowed writes and shell in the worktree; denied
    writes outside it, `ticket.md` edits, git, and an agent CLI from its shell
  - confirm the limit markers in `stop.txt`, the host's subagent stop tool,
    and that subagents spawned from the chat panel get the generated agent files and model
  - also capture: `SubagentStart` and `SubagentStop` payloads, a forced subagent limit hit, one round
    trip with a prompt at `RENDER_BOUND_CHARS`, that a run on the extension's default approval
    settings asks the user nothing (tempo's hook `allow` replies stand in for the prompts), and what each host does when a hook crashes or times out

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
  journal events, except the new fields in §3.1.
  - The comparison runs under a fixed clock and fixed ids.
  - Prompts are not compared, because the new prompt carries the worktree path.
- Config tests: unknown key, unresolvable combination, review separation.
- Constant pins.
- A live-gate report per host, in `docs/livegate/<host>.md`.
  - A host passes only if all of these are confirmed: the six hook cases, the implement confinement
    cases, the round trip, the limit markers, the subagent stop tool, and a run with no prompts on default
    approvals.
  - **A host that fails the gate is dropped from the defaults until it is fixed.**
  - R5 is met for the hosts that pass. A failed host is reported as
    an open gap; it does not block the others.
  - Copilot is enabled when it passes **and** `hosts.copilot` is filled in.

### P5a — Orchestrator replaces the scripts: the ticket flow

**Work:**
- Decide tools for the ticket flow (`retry`, `escalate`, `reject`, `park`, `reoffer`, `release_hold`)
  and the Record tools `file_box` and `notify` (§4.1).
- `IDENTICAL_K` moves from `runner` to `caps`.
- Remove the scripted layer in this commit order, listing each commit's test deletions first. The 22
  carried scripted-layer test files (D14) are deleted with their modules; tests of kept code inside them
  are first moved to the kept module's tests:
  - `drain`
  - `runner`, `daemon`, and `serve` together, because each imports the others: this takes `runner`
    dispatch, its ladder and rework routing, and diagnose's only caller
  - `stages.diagnose` + `specs/diagnose.md`
  - code the removed layer left uncalled in kept modules: `notify.NotificationReconciler`, `reconcile`,
    and the unread `caps.poison`. `mergequeue` stays, uncalled, with `merge`'s `compose_pipeline` and
    daemon admission mode and `control`'s pause inbox: its integration checks, conflict strategies,
    conflict hand-off, and red-streak hold may be wired into the `merge` tool later (decided 2026-11-03)
- Each removal also deletes its CLI command. At the end of P5a, `__main__` joins the kept set and the
  import-graph test covers it.
- Removed with the daemon, because only the daemon or `serve` called them: `restart`, `watcher`,
  `scheduler`, `heartbeat`, `checkpoint` (the push to `origin` every 5 merges; local main is the
  blessed line, and the user pushes), and `flake` (never wired into a run). The `pause`, `resume`, and
  `kill` CLI commands go too (§2.6).

**Exit:**
- A fake-host orchestrator session runs a ticket from intake to merge, and through each P5a decision.
- No code path in `tempo/` spawns an agent CLI. A test scans for the agent binary names in argv
  construction.
- A session killed mid-ticket resumes correctly from `status`.
- Suite green after every removal.

### P5b — Orchestrator replaces the scripts: split, confirm, triage, author, retro

Triage, author, retro, and rework each make their model calls inside one process pass, some nested (author
and rework run requisition review on each proposal). Each becomes separate prepare/submit calls.

**Work:**
- `split_propose` / `split_commit` and `confirm` (§4.1, "Box, split, confirm, and retro").
- `prepare` serves `triage`, `author`, and `retro`; each one-pass function splits into the work before
  the call and the work after it.
- `rework` keeps only its proposal checks and `record_supersedes`, renamed in place to `split`;
  `specs/rework.md` goes with the rest of `rework`.
- Remove the old `Driver._run` path (§3.1). The CLI adapters went in P4a. `FAILURE_MARKERS` stay, for
  `stop.txt`. `Driver` keeps its name as the holder of journal, effects, spool, redactor, log, clock,
  and severity; `llm`, `run`, `race`, the abort machinery, and `provider_failure` go, with the model
  types in `llm.py` (`AgentTier` and `AgentEffort` stay), `llm_call` (`llm_key` stays),
  `requisition.review_ticket`, `stages.implement` / `review` / `run_stages` / `StageBoundary` /
  `Stages`, `ProviderCallError`, and `PauseConsumer.author_driver`. Their tests go with them.
- `TEMPO_PLAN.md` §9 and §11-§14 changes (§10).

**Exit:**
- A fake-host session runs split, confirm, a triage pass, and a retro.
- `rework` and `Driver._run` are gone; suite green.

### P6 — Governing files and packaging

**Work:**
- Host files from `render` and `tempo init` (§6). Hook JSON files merge.
- Move `specs/` to `tempo/specs/`; every path naming it is renamed in place.
- The `tempo:session` block (§5.1): `tempo/rules/session.md`, rendered into all three instruction
  files with the same marker and idempotence contract as `tempo:core`.
- `tempo lint <stem>`, and `docs/tempo/tickets.md` extracted from TEMPO_PLAN §13 (§5.1). Its source is
  `tempo/rules/tickets.md`.
- `tests/fixturerepo.py` sets up its repo through `init`.

**Exit:**
- The wheel holds `tempo/rules/**` and `tempo/specs/**`.
- `render` into a repo with its own `.claude/settings.json` keeps its other keys, and `begin_session`
  starts there.
- `tempo init` writes `tempo:session` into all three instruction files, and a second run is
  byte-identical.
- `tempo lint` exits 0 on a valid filled ticket, and on an invalid one exits nonzero with each finding
  and its paved road.
- `tempo init` into an empty repo produces the §5 layout plus a default `config.yaml`.
- Installing from the wheel into a clean venv works.

### P7 — Live runs

**Work:** On the P4 fixture repo, run real tickets from the VS Code extension of each host that passed
the live gate.
Include one round trip with a prompt at `RENDER_BOUND_CHARS`; P4c's live gate did not run it.
Only Claude passed the live gate, so P7 runs Claude.

- **Repo.** `~/livegate/p7`, made by `tests/fixturerepo.py`. Its `config.yaml` sets Claude's `low`
  tier to Haiku (`claude-haiku-4-5-20251001`), so an escalate changes the model, and `max_tickets: 10`,
  because every retry is a new `start`.
- **One `/tempo-run`, three tickets**, on the extension's default approvals (Manual):
  - `add-subtract`: the fixture ticket, end to end.
  - `big-context`: a small change whose ticket body carries a bulk code list, sized with tempo's
    renderer. The ticket text appears twice in the requisition_review prompt (alone and inside the base
    Implement render), so that prompt is just under `RENDER_BOUND_CHARS` (399,493 characters, 20 parts)
    while the Implement render (200,491) stays under authoring headroom. A Context file cannot do this:
    it appears once, and headroom caps the Implement render at 300,000.
  - `retry-escalate` at `agent_tier: low`: its Verification also runs `gate.py`, outside the scope
    fence. It fails its first 3 runs in the ticket worktree (a counter outside the repo), passes in the
    merge-base worktree (so the failure is not attributed to main), and passes from run 4. The expected
    path is retry, retry, a retry refused by `IDENTICAL_K`, escalate to Sonnet, merge. The
    orchestrator's choices are its own; whatever it decides is reported.
- **Checked after the run:** the journal, merge commits, `decisions.md`, each spawn's
  `resolvedModel`, no permission prompt, and the 20-part prompt's round trip.
- **Results** go in a P7 section of `docs/livegate/claude.md`.
- **Fix from the run: one reply channel in the wording.** A Sonnet requisition_review put its verdict
  in its final message and left `output.txt` empty, so the call failed and was re-prompted. The spec
  said "Reply with ONLY one JSON object" while `tempo/rules/agent.md` says to write the reply file and
  that the final message is ignored; the model followed the spec. Every spec's Output format lead-in
  (author, implement, requisition_review, retro, review, triage) opens with "Your final message is not
  read." and says "Write ONLY one JSON object to the `reply:` file" where it said "Reply with", each spec's minor
  `version` goes up by one, and a test pins that no spec tells the model to "reply with" its JSON.
  `submit` keeps reading only `output.txt`: a fallback to the final message would be a second reply
  channel.
- **Fix from the run: a requisition_review failure is harvested.** `Harvest.stage` did not allow
  `requisition_review`, so `_decide` journaled `harvest_failed` and `read_attempts` showed no findings.
  The stage is allowed, and `prior_attempts` falls back to the harvest's findings when the failed
  stage left no review or checks artifact. The P7b rerun showed the rest: the orchestrator calls
  `read_attempts` before deciding, when `prior_attempts` holds only decided attempts, so it returned
  null. `read_attempts` now also returns `"failure"`, the `stage_failed` awaiting a decision (null
  once decided), and `tempo/rules/orchestrator.md` names it. A fixture-flow test covers both.
- **Fix from the run: requisition_review is pinned like review.** It resolved at the ticket's tier, so
  a `low` ticket had its feasibility judged by Haiku, which twice blocked a sound ticket. Config load
  now requires `pins.requisition_review` outside `models_by_tier`, as for `pins.review`. The default
  config, the fixture config, the test configs, `docs/host-contract.md`, and TEMPO_PLAN.md section 15
  say so.

**Exit:**
- Each host completes a ticket end to end with verified artifacts.
- One ticket per host goes through retry and escalate with a real model.
- Results reported.

## 9. Risks known now

- **Host behavior may differ from assumptions.** Mitigation: the P4 live gate runs before P5 deletes
  anything, and a failing host is dropped from the defaults rather than blocking the others.
- **Codex has no caller identity.** It runs one subagent thread and fails closed when more than one is
  active.
  - While a subagent runs, the Codex orchestrator could write `output.txt` itself. This is accepted
    (D10).
- **Model provenance is resolved, not attested (§3.3).** A host silently substituting a model is not
  detected.
- **No spiral detection.** Only the call deadline, `IDENTICAL_K`, and `max_minutes` guard against
  thrashing.
- **A host limit ends the whole session**, and it stays stopped until the user runs `/tempo-run` again. The call
  in flight loses its work and is re-prepared, uncharged.
- **Implement's shell is not sandboxed.** Hooks confine its working directory and deny git and agent
  CLIs, and the `submit` snapshot catches writes in the repo and `.tempo/`. Writes elsewhere on the
  machine are not caught, as with chupa's implement child.
- **A hook that times out or cannot start lets the tool call through.** The P4c live gate showed this on
  Claude, even with `onFailure: block` set. `tempo hook` itself denies on any fault it catches. The
  backstop for the rest is `submit`'s snapshot, which refuses a stray write in the repo or `.tempo/`.
- **P1's failure count is unknown until run.** 37 of the 80 carried test files read chupa's plan text, and 2 import helpers from uncarried evidence tests.
- **The orchestrator's decisions meet a real model only in P7**, through the retry and escalate runs.
  Nothing measures them before then.

## 10. Changes to TEMPO_PLAN.md

`TEMPO_PLAN.md` starts in P1 as the seed's product features only (no build process). These later
changes are made in the phase shown, so the plan and the code stay in step.

- **P2:**
  - Remove cost accounting from the feature sections. Keep the failure
    classes, breaker, cooldown, routing candidates, and `provider_drought`.
  - Remove the §9.7 spend-without-progress spiral signal.
- **P4 (§6, §8, §15):**
  - The read-only surface call becomes `prepare` + subagent + `submit`.
  - Configuration per §4.4.
  - Implement becomes a subagent surface; `providers[]` and `routing` are removed.
  - Session token, session start and end (no supervisor), and start-then-poll tools.
- **P5 (§9, §11, §12, §14):**
  - Dispatch, failure routing, the ladder walk, diagnose, rework, and the drain and daemon loops become
    orchestrator decisions through the §4.1 tools. Caps and refusals stay in tools.
- **P5 (§13):**
  - Reserve `tickets/orchestrator/`.
  - Add `decisions.md` to the ticket dir listing.
- **P6 (§17):** Root agent files become three, each with `tempo:core`, `tempo:protocol`, and
  `tempo:session`. Rule set B is replaced by the `tempo:session` block.
- **P6 (§13):** Add the `lint <stem>` verb to touchpoint 1, idea intake.
- **P6 (§18):** The "virtual project" rule ends at the release path, the PyPI wheel.
- **P6 (§8, §12, §13):** The prompt specs live at `tempo/specs/<surface>.md`.
