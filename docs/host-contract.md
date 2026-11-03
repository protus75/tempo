# Host contract

The installed engine owns process; the host owns project content, tickets and
configuration. This document describes the merged configuration and interfaces.
Foreign process-state adoption is excluded.

## Copyable configuration

Copy this YAML to `config.yaml` at the host checkout root and edit the models,
paths and checks for your host. `--config <path>` selects another file;
otherwise the invocation cwd selects `config.yaml`. The loader uses PyYAML
`safe_load`, refuses unknown fields and validates strict types (paths accept
strings). These check commands and merge strategies are host choices, not engine
presets. Loading this example does not run its commands or contact a provider.

```yaml host-example
schema_version: 1
state_dir: .tempo
hosts: # subagent models per host; review is pinned to a model outside models_by_tier
  claude: # the one host that passed the P4c live gate (docs/livegate/)
    models_by_tier: {low: host-model, medium: host-model, high: host-model, max: host-model}
    pins: {review: host-review-model}
orchestrator:
  models: {claude: host-orchestrator-model} # every host here needs a hosts entry, and the reverse
review:
  mechanical: # argv lists, never shell commands; choose your host's checks
    - {code: host-test, argv: [uv, run, pytest, -q], trigger: always, severity: hard}
    - {code: host-style, argv: [uv, run, ruff, check], trigger: [src/], severity: soft}
  surfaces: # project rules are prompt data
    - {name: host-review, trigger: [src/], rules_doc: docs/review.md, severity: hard}
  trigger_map: {src/: [scope_fence]} # engine gate codes only; host entries use their own triggers
  gate_severity: {scope_fence: hard} # merge overrides; engine defaults are all hard
merge:
  safety_checks: [host-test] # each code must resolve to exactly one hard mechanical entry
  strategies: # choose paths suitable for your host
    - {paths: [uv.lock], strategy: regenerate, argv: [uv, lock]}
    - {paths: [CHANGELOG.md], strategy: union}
engine_plane_safety_inventory: [config.yaml, tempo/specs/, tempo/gates.py]
# worktree_root: .tempo/worktrees
# report_inbox: reports
# notify: [notify-send, tempo]
# context_files: [docs/architecture.md]
```

## Complete field inventory

Each row names a model and every accepted field. `required` means no shipped
default; `unset` means omit the key, never write null. Nested blocks retain their
own defaults when partially supplied. Config defaults shown as objects are the
entire default block. This inventory follows `tempo/config.py`.

```yaml config-fields
Config:
  required: [schema_version, state_dir, hosts, orchestrator, review, merge, engine_plane_safety_inventory]
  defaults:
    worktree_root: unset
    caps: {decisions: 6, retry: 6, premise_bounce: 2, infra: 6}
    circuit_breaker: {k: 3, cooldown_minutes: 10}
    tickets: {max_minutes: 90}
    box_policy: {failure_report: confirmed, retro_finding: confirmed, override_report: draft, suggestion: draft, bug_report_self_diagnosed: confirmed, bug_report_player_repro: confirmed, bug_report_player_no_repro: draft}
    notify: unset
    report_inbox: unset
    context_files: []
ModelsByTier:
  required: [low, medium, high, max]
  defaults: {}
Host:
  required: [models_by_tier]
  defaults: {pins: {}, quota_window_minutes: 60}
Orchestrator:
  required: [models]
  defaults: {session: {max_tickets: 5, max_minutes: 240}}
OrchestratorSession:
  required: []
  defaults: {max_tickets: 5, max_minutes: 240}
Review:
  required: []
  defaults: {mechanical: [], surfaces: [], trigger_map: {}, gate_severity: {}}
MechanicalCheck:
  required: [code, argv, trigger, severity]
  defaults: {}
ReviewSurface:
  required: [name, trigger, rules_doc, severity]
  defaults: {}
Merge:
  required: []
  defaults: {safety_checks: [], strategies: []}
RegenerateStrategy:
  required: [paths, strategy, argv]
  defaults: {}
UnionStrategy:
  required: [paths, strategy]
  defaults: {}
Caps:
  required: []
  defaults: {decisions: 6, retry: 6, premise_bounce: 2, infra: 6}
CircuitBreaker:
  required: []
  defaults: {k: 3, cooldown_minutes: 10}
Tickets:
  required: []
  defaults: {max_minutes: 90}
```

```yaml config-values
tier: [low, medium, high, max]
severity: [hard, soft]
trigger: [always, path-prefix-list]
strategy: [regenerate, union]
start_state: [draft, confirmed]
box_row: [failure_report, retro_finding, override_report, suggestion, bug_report_self_diagnosed, bug_report_player_repro, bug_report_player_no_repro]
```

`schema_version` must be the integer 1, not a boolean or string. Count fields
are integers at least 1, and retry must not exceed decisions. Argv and strategy
paths are nonempty lists where declared. Notify requires a nonblank executable
and NUL-free strings.

Tempo runs no agent CLI: every model call is a subagent of the host's VS Code
chat. `hosts` names each host (`claude`, `codex`, `copilot`) and its subagent
models. A surface's model is its `pins` entry, else `models_by_tier[tier]`.
`pins.review` is required and must not appear in `models_by_tier`, so a model
never reviews its own work. Every host in `hosts` needs an `orchestrator.models`
entry, and the reverse. Two model ids that render the same agent name are
refused. Host check codes are strings; engine gate overrides use engine codes. `trigger` is `always` or a path-prefix list.
Regenerate requires argv; union accepts no argv and only resolves append-only
conflicts. A conflict path must match exactly one strategy, and regenerated
paths must be outside the ticket fence; otherwise resolve through Rework with a
new approval.

Relative `state_dir`, explicit `worktree_root` and `report_inbox` resolve against
the selected config file's parent; absolute paths remain absolute. Absent
worktree_root becomes `<state_dir>/worktrees`. `context_files` and `rules_doc`
remain Path values as written at load time; use host-relative content paths.
An absent `box_policy` row takes its table default. Every explicit null,
including nested values and list items, is refused with its dotted key: omit
the key for a default or supply a value. Unknown keys require correcting the
selected config rather than silently accepting a future schema.

## Version migration

Newer versions refuse with a road to upgrade tempo; older versions name
`tempo migrate-config`. The only prior schema is explicit integer version 0;
missing version is a missing-required-key refusal, never inferred as 0.
`tempo migrate-config` selects the same config path, changes only the version
scalar from 0 to 1, and keeps all other bytes (comments, BOM and line endings).
It validates the full candidate with the real loader before any write, writes
the original bytes to adjacent `config.yaml.bak` (selected filename plus `.bak`),
then performs one atomic config replacement through the filesystem seam.
An existing backup is replaced. Current version is a validated byte-identical
no-op with no backup write. Missing file, missing/non-integer/negative version,
newer version, duplicate or aliased version-0 scalars, invalid YAML or invalid
candidate refuse without writes. Create/select the file, supply the explicit
integer, repair the named key and retry migrate-config, or upgrade for a newer
schema. Every future version bump must ship its migration step.

## Managed conduct and ticket ownership

Engine-owned `<!-- tempo:core begin -->` and `<!-- tempo:core end -->` lines
delimit the managed conduct block; the remainder is project-owned content.
Engine process wins over host content. Do not hand-edit the core: `tempo core`
inserts or refreshes it, then commit the selected files. Each configured host
selects its instruction file: `claude` CLAUDE.md, `codex` AGENTS.md, `copilot`
.github/copilot-instructions.md, deduplicated.
First adoption with zero marker-like text inserts the block before every
original byte. Refresh preserves prefix and suffix bytes without decoding them.
Partial, duplicate, reversed, inline or stray marker text refuses; repair to
exactly one complete begin line followed by one complete end line, retain
project content outside it and escape stray marker text, then retry core.
Refresh inspects the entire selection before writing any file. Core drift
requires `tempo core`; uncommitted routed bytes must be committed or restored.
Project remainder rules attempting to override engine process refuse: move
them to the engine template or rewrite as project-content rules.

Ticket frontmatter policy is additive-only, but today's parser is closed;
there is no frontmatter schema migration mechanism. This is the full accepted
list from `tempo/tickets.py::Frontmatter`:

```yaml ticket-fields
required: [state, source, priority, kind]
defaults: {agent_tier: medium, agent_effort: medium, gate_bypass: []}
```

State is `draft | confirmed | rejected | merged`; priority `P0 | P1 | P2 | P3`;
kind `bug | feature | chore`; tier and effort `low | medium | high | max`.
Source is `human` or `box:<class>` matching `box:[a-z][a-z0-9_]*`.
Each gate_bypass has only nonblank `code` (an engine gate code) and `reason`.
Put ordering in Depends on and priority, not extra metadata. New human intake
stamps source human and confirms; machine sources belong to engine authoring.

## Generated host files, hooks, and tools

`tempo render` writes, from config.yaml, each host's hook file (`.claude/settings.json`,
`.github/hooks/tempo.json`, `.codex/hooks.json`) and a `tempo:protocol` block in its instruction
file, for all three hosts. For each host in `orchestrator.models` it also writes the orchestrator
rules, the `/tempo-run` start command (on Codex the skill `.agents/skills/tempo-run/SKILL.md`), and one thin
`tempo-<surface>-<model>` agent file per reachable surface and model, plus effort on Codex. Stale
generated `tempo-*` files are deleted. These are render targets: change config.yaml or tempo's
`tempo/rules/` and rerender, never hand-edit them. `tempo render --check` lists drift; `doctor`
runs it, and `begin_session` refuses when the host's hook file is stale.

Every hook runs `tempo hook <host>` with the host's JSON payload on stdin, and writes the payload and
its reply to `<state_dir>/engine.log`. A payload that does not parse, and any hook fault, is denied
(exit 2). A hook that cannot start or that times out lets the call through on every host the live gate
ran (docs/livegate/); `submit`'s snapshot is the backstop. The hook allows `tempo tool begin_session`
from any top-level chat, so starting asks the user nothing. The chat that ran `begin_session` is the
orchestrator: its shell runs only `tempo tool <name> '<json>'`, it never writes, and it may spawn, in the
foreground, only the agent its open call names with exactly that call's spawn prompt. When that spawn
returns, its result text and reported model go to the call's `stop.txt` and `call.json`. A `tempo-*` subagent reads only its call's read
root, the engine specs, and its prompt parts; writes only its reply, and implement also its worktree
except `ticket.md`; and never spawns, messages, or resumes an agent, or runs git, `gh`, `tempo`, an
agent CLI, or a publish. Every other chat gets no tempo enforcement.

The orchestrator's tools print one JSON object: `{"ok": true, ...}` or `{"ok": false, "refused",
"do_instead"}` (exit 2). `begin_session` returns the session token every other tool except `status`
needs; `write_handoff` commits `tickets/orchestrator/handoff.md` and ends the session. `check`,
`merge`, and a call's validation in `submit` run in a detached `tempo job-run` worker; poll `job` (or
`submit`) until done. Session state is `<state_dir>/session.json`, hook state `<state_dir>/hooks/`, and
job state `<state_dir>/jobs/`; none of them, nor the engine log, is a confinement finding.

## Checks and injectable seams

`Gate.check(artifact, workspace) -> GateReport` is synchronous; the report has
code, `pass | fail` verdict, findings (default []), autofix_applied (default
false). Failure requires findings with paved roads; code must match the gate.
Severity belongs to the runner, with all engine codes hard at merge unless
`review.gate_severity` overrides them.

`MergeQueue.host_checks(ticket, *, attempt, safety)` runs review.mechanical argv
in the candidate worktree with explicit child env and the ticket stuck timeout.
rc 0 passes; nonzero, timeout and missing executable fail with repair roads.
It consumes exit status, not optional structured findings from stdout. Output
is spooled to `merge-safety/host-NN.txt` or `merge-integration/host-NN.txt`;
returned tails are redacted and captured output is scrubbed at the spool seam.
Every safety_checks code must resolve to exactly one hard mechanical entry;
invalid designations fail before commands. Integration selects only always/hard
entries. Host surfaces use their own triggers; trigger_map concerns engine gates.

The minimal seams in `tempo/seams.py` are:

- `Clock = Callable[[], datetime]` and `Sleep = Callable[[float], Awaitable[None]]`:
  timed work races injected sleep to a clock-derived deadline; tests advance
  deadlines without wall-clock waiting.
- `async run(argv, *, cwd, env, timeout, stdin_path=None) -> (rc, out, err)`:
  explicit required env and timeout (None means unbounded). Each child uses
  start_new_session; timeout and outer cancellation use the same group kill and
  wait helper. `kill_group(pgid)` is synchronous SIGKILL signal-not-reap; run
  owns reaping. `on_spawn` publishes the pgid.
  `on_stdout_line` includes the EOF tail; consumer failure stops delivery but
  drains capture, attaches process_capture and raises after reap. Unwind retains
  timeout/cancellation type. ExecutableNotFound names the missing binary: install
  it or fix child PATH. No stdin_path means devnull.
- `write(path, data)` fsyncs a temporary file and atomically replaces the target;
  `replace(src, dst)` atomically replaces; `publish(path, data)` durably publishes
  without overwriting an existing destination.
- `detached_spawn(argv, *, cwd, env, keep_fd)` starts a job worker in its own session that inherits
  the job lock's descriptor and is never waited for.
- `async notify(argv) -> dict[str, str | int]` validates nonempty argv, nonblank
  executable and NUL-free strings; CommandNotifications scrubs output and
  returns rc/stdout/stderr on success. Nonzero, timeout, missing executable or
  OS errors raise NotificationFailed: fix argv/PATH and retry pending evidence.

Children inherit the parent environment: no config field names a provider key.
Active-work and notification executors are separate; abort targets the executor that spawned active work.

## Instance custody

ONE instance writes a host repo line. Stable is a tagged release installed in
its own venv; dev uses its own disposable checkout, worktree root and state dir.
Instances share only remotes. Upgrade by tag,
install and restart; rollback reinstalls the previous tag.
`<state_dir>/tempo.lock` uses advisory flock before writes and records instance_id,
pid, host, state_dir and started_at for diagnostics only. Kernel release on
process death governs correctness, never stale-pid adoption. On LockHeld wait
for or stop the holder; never delete the lockfile. Child descriptors do not pin it.
State dir owns journal, box queue, attempt spools, and engine
log. Decisions and tombstones are committed under
`tickets/decisions/`, so dedup memory travels with the repo. Every `tempo tool`
call is a fresh process that reads config.yaml, so model, severity and ceiling
edits take effect on the next call.

## Report-inbox contract

The optional report_inbox is a directory for small host JSON reports, consumed
by sequential triage into the same durable Suggestion Box. Version-1 reports
have exactly these fields:

```yaml report-fields
required: [schema_version, origin, summary, app_commit, app_version]
defaults: {implicated_paths: [], evidence: {replay: null, log: null}}
self_diagnosed_required: [game_slug, anomaly_code, location]
origins: [self_diagnosed, player]
replay_max_bytes: 1048576
log_max_bytes: 65536
```

schema_version is integer 1; summary, app_commit/app_version and self-diagnosed
fields are nonblank. Player reports omit game_slug, anomaly_code and location.
Unknown report/evidence keys refuse; version 0 accepts the same shape with only
its version scalar changed. Evidence replay/log are nullable repo-relative
paths (unlike config's null refusal). implicated_paths are repo-relative paths.
Self-diagnosed dedup uses the exact tuple (game_slug, anomaly_code, location,
app_version), preserving digits and path bytes; player text uses sequential
semantic dedup. The producer supplies no signature field.

Replay JSON contains initial_state and an ordered actions list. Read metadata-first:
replay at most 1 MiB, log at most 64 KiB, accepting exact boundaries; recheck
copied bytes. Missing, escaping, non-regular or over-limit files refuse before
message recording. Repair schema/path/evidence and retry; malformed reports
stay unconsumed with diagnostics. Box copies report and bounded evidence into
durable box custody before message recording, never referring back to mutable
inbox evidence. Mark consumed only after durable enqueue or dedup, with a
stable arrival identity on retries. has_repro means a validated replay was
copied. Triage copies custody evidence into the authored ticket's evidence/
directory; report -> replay -> permanent regression fixture. Report text is
untrusted data at every hop: triage writes its own summary, raw text never enters
ticket prose and later prompts see only delimited data. Evidence is quoted,
never executed outside the deterministic replay harness. app_commit attribution
is a later consumer's judgment, not proof of an introducing commit.
