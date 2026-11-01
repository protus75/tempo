# tempo -- implementation plan (seed document)


Status: design plan. This file seeds a NEW, separate repository and is self-contained: every load-bearing rule, contract, and decision is stated here; implementer-owned internal schemas are delegated where marked (sections 6 and 13). Nothing depends on any other document existing. "tempo" is the project name (unique so agents never confuse this system's rules with another project's).

**Two eras -- a reading rule.** Every rule below binds ONE of two systems; conflating them produces wrong builds and wrong reviews.

- BOOTSTRAP era (Phases 0-2): the conductor then the continuous self-hosting drain, the NO-GO resting state; tuned for unattended forward progress under the good-enough bar. Its UNATTENDED conduct is not confined to Phases 0-2 -- it governs tempo building ITSELF and runs holds-exempt through the whole self-hosted build to Phase 6 (section 19), bounded by the section 18 safety envelope, never a per-phase `confirm`; the daemon is a component built and soak-proven in Phase 3, not a supervisor the still-building engine hands itself to.
- DAEMON era (Phase 3+): the continuous daemon machinery, and -- once the operator CUTS OVER to running on HOST work (GO first RECORDED, the daemon started on that work via `tempo serve`) -- the supervised product carrying the full gate set.

The split is contractual -- section 19's bootstrap contract is the authority. Bind every requirement, build decision, and review finding to its era before applying it:

- A product supervision surface never gates tempo's own self-build (through Phase 6). Supervised-merge holds, GO-gated auto-confirm, and the phase-boundary/box draft gate govern HOST work under a recorded GO; the self-build is bounded by the section 18 safety envelope, never a `confirm`. The GO-grade eval's Author-graph check runs from a harness-local author prompt, NOT the production `specs/author.md` (a Phase 2 seed).
- A bootstrap convenience never ships as engine code (section 0).

Why: holding one era to the other era's contract is wrong by construction.

## 0. Cold start (bootstrap this repo from this document)

Given only this file on a Linux box, the steps below stand up the repo and the environment Phase 0 (section 19) builds into. Everything here is a ONE-TIME human bootstrap the operator runs before any agent or daemon exists, distinct from the engine's own runtime, which never shells these bootstrap conveniences (one named exception: the drain's self-upgrade re-exec through `uv run`, D1).

**Prerequisites.** Two are required by the engine at runtime; the BOOTSTRAP CLI -- shipped default `claude`, retargetable per the paragraph below -- is required by the BOOTSTRAP (the conductor shells it per deliverable -- preflight-checked; a nonzero exit halts the run, never a silent skip); the rest are setup-only. Host #1 -- the operator-declared host project Phase 6 onboards (BoardGameUI in this plan's reference environment) -- is a declared setup input, not a binary: declare it here at setup or declare none. EITHER WAY Phase 6 scaffolds the committed fixture host and runs its wiring and exit reads against it (section 19); the declared host bears only on the operator's post-cutover onboarding and acceptance reads, and with none declared live onboarding simply waits at the cutover. `gh` is a convenience for repo creation ONLY -- the engine never shells it (no `gh`, no PRs in the loop; section 18, D8).

The bootstrap CLI is a declared constant at the top of `bootstrap/conductor.py` (shipped default `claude`; any agent CLI with a headless prompt mode satisfies it -- for `codex`, `codex exec` with the prompt on stdin). The conductor shells that one CLI per deliverable through a single adapter row (argv shape, prompt delivery, exit-code read), and deliverable 1 authors the conduct file THAT CLI loads -- CLAUDE.md for `claude`, AGENTS.md for `codex` (section 17's curated-subset rule then runs in the opposite direction) -- so retargeting the bootstrap is a one-constant edit plus that mapping, never a rewrite of this section.

| Tool | Version | Role | Required |
|---|---|---|---|
| Python | 3.14+ (floor; D1) | interpreter the tempo venv pins -- provided by uv, not the system | yes -- runtime |
| git | any recent | every ref/commit op goes through `git.py` (section 10) | yes -- runtime |
| claude | the latest release, installed with `pnpm add -g @anthropic-ai/claude-code@latest`, logged in | the conductor shells `claude -p` per deliverable (build + review contexts); engine runtime touches it only as a configured `cli` provider (section 6) | yes -- bootstrap |
| pnpm | any recent, its global bin dir on PATH | installs and updates every agent CLI globally; the preflight reads each CLI's latest release from it (section 6) | yes -- setup + preflight |
| uv | any recent | creates the venv and installs deps; runtime-shelled ONLY by the drain's self-upgrade re-exec (section 18, D1) | setup + one exception |
| gh | any recent | one-command repo creation below; engine NEVER shells it | no -- convenience |

Runtime deps are deliberately tiny (goal 1): stdlib (`asyncio`, `argparse`, `tomllib`, `json`) plus `PyYAML` (config parse, section 15) and `pydantic` (artifact + journal-body schema validation, section 5). Journal projections are stdlib read-time folds (D3); an analytics engine (DuckDB) is a D10 return at the first projection measured too slow as a fold. Provider SDKs are pulled per the provider layer (section 6), never hardcoded here.

Verify prerequisites, then let uv provide Python (uv fetches a standalone CPython from Astral's python-build-standalone, so the system Python version is irrelevant):

```bash
git --version      || echo "install git (required)"
command -v claude  || echo "install + log in claude (required: the conductor shells it)"
command -v uv      || echo "install uv:  curl -LsSf https://astral.sh/uv/install.sh | sh"
command -v gh      || echo "gh optional -- repo-create convenience only"
uv python install 3.14   # Python comes from uv, not the system
```

Create and seed the repo -- the `python3` block extracts both `bootstrap/conductor.py` and the pointer README from this document, so neither is hand-pasted and TEMPO_PLAN.md stays canonical:

````bash
mkdir -p ~/source/tempo && cd ~/source/tempo
git init -b main
mv ~/Downloads/TEMPO_PLAN.md .            # this document
mkdir -p bootstrap
python3 - <<'PY'
import re
plan = open("TEMPO_PLAN.md").read()
def extract(name):                       # one minimal pattern for every embedded source
    return re.search(r"# BEGIN_%s\n(.*?)\n# END_%s" % (name, name), plan, re.S).group(1)
open("bootstrap/conductor.py", "w").write(extract("CONDUCTOR") + "\n")
open("README.md", "w").write(extract("README") + "\n")
PY
git add README.md TEMPO_PLAN.md bootstrap/conductor.py
git commit -m "Seed: tempo plan + generated README + bootstrap conductor"
````

**Generated files (conductor + README).** The `python3` extractor materializes TWO files from this document's appendix (section 21) with one pattern -- the lines between a `# BEGIN_<NAME>` / `# END_<NAME>` sentinel pair, read straight from TEMPO_PLAN.md (already on disk after the `mv`): `bootstrap/conductor.py` from `# BEGIN_CONDUCTOR` (the phase-playbook driver) and `README.md` from `# BEGIN_README` (a deliberate POINTER -- identity, read-first link to this plan, and the lifecycle run commands (bootstrap, drain, serve) -- never a parallel run-book). README.md is GENERATED, never hand-edited: a README change edits the `# BEGIN_README` block in the appendix and re-runs the same extractor. Keep both sentinel pairs intact; the extractor keys on them.

Publish -- one command with gh, or the git-only path if gh is absent:

```bash
gh repo create tempo --private --source=. --remote=origin --push
```

```bash
# git-only fallback: create an empty PRIVATE repo in the GitHub web UI first, then
git remote add origin git@github.com:<owner>/tempo.git
git push -u origin main
```

Environment + first build. From the seeded repo, create the venv and start Phase 0:

```bash
uv venv --python 3.14
uv pip install pyyaml pydantic   # the non-stdlib runtime deps
```

Phase 0 formalizes these in a `uv`-managed `pyproject.toml` with `requires-python = ">=3.14"`; the manual install above only gets the first session running. Then drive the whole bootstrap -- Phase 0 then Phase 1 -- WITHOUT hand-pasting; `bootstrap/conductor.py` (seeded above) is the automation, and with no `--phase` it runs every conductor-owned phase end to end:

```bash
python3 bootstrap/conductor.py             # default: Phase 0 then Phase 1, end to end
python3 bootstrap/conductor.py --auto      # same, unattended (no verdict pauses)
```

The conductor parses this document's Phase N prompt playbook and runs each deliverable in a FRESH `claude -p` context (a separate scoped context -- the anti-wander property -- carrying a standing preamble with the verify-in-place and file-don't-ask rules), then GATES by the prompt's own stop-condition, layered fail-closed:

- The `claude` call MUST exit zero. Transient nonzero exits are retried with backoff a bounded number of times; a persisting failure (a dead or unauthenticated CLI) halts, never a phantom completion.
- The playbook item's `expects:` files MUST exist on disk (the agent-did-nothing check).
- The conductor re-runs `uv run pytest` itself, never trusting the agent's claim.
- A SECOND fresh context adversarially reviews the uncommitted diff, failing ONLY the good-enough bar's spine-breaking classes (section 19): state corruption, deadlock or permanent stall, secret exposure, false-green verification (tests that mirror the implementation instead of pinning behavior). A blocking finding must be REPRODUCIBLE, must attach to the CURRENT deliverable's diff (never pre-existing code or later-phase scope), and the finding set FREEZES after buying exactly one bounded reimplementation pass -- a reviewer that raises a new blocking objection each pass is the churn failure mode, and its later findings are advisory. Every other finding is appended to `bootstrap/suggestions.md`, never a halt. The same admissibility bounds apply to the pipeline's `correctness_review` REJECT from the day it exists (section 7): a bare unstructurable verdict, a pre-existing-defect complaint, or a scope-expanding demand does not block.
- Gate findings (red suite or spine-class review fail) are fed back up to twice before halting for the operator -- both rounds servicing the FROZEN first-pass blocking set per the freeze law above; a new blocking objection raised on a later pass files to `bootstrap/suggestions.md` instead of halting.

A real-model deliverable (its exit is a recorded verdict, not a green suite -- Phase 1 prompts 2 and 9) runs, commits on journal evidence, then pauses for the operator to read the verdict (`--auto` continues without pausing). A bootstrap run records at most NO-GO -- the GO-grade baseline is a Phase 6 deliverable run immediately before cutover, where GO is first read (section 19) -- leaving daemon-era machine authoring supervised until GO is recorded; the bootstrap drain itself never holds (section 19).

The single cold-start paste above is the ONLY hand-paste; from here the conductor drives every deliverable. It is section-0 bootstrap tooling, never engine code (D1 governs the engine, not these cold-start conveniences), and it retires once the Phase 1 walking skeleton lands and tempo runs its own tickets. Phases 0-1 are built by the conductor, not by tempo itself.

Automation is the default, in three rungs: no `--phase` runs every conductor-owned phase (0 then 1); `--phase N` runs one phase; `--phase N --deliverable K` runs exactly that one deliverable and stops. Coarser rungs run the finer one in order, gating each the same way, and compose through `bootstrap/state.json`. A deliverable that persistently exceeds its fix attempts halts naming that finest rung: the paved road is narrowing or splitting ITS playbook prompt in this document, then re-running exactly that deliverable -- never hand-pasting, never restarting the phase.

**First session (VSCode + Claude Code).** After cold start the repo holds this document, the README, and `bootstrap/conductor.py` -- no engine code. The operator does NOT hand-paste the build (the cold-start paste above is the only one). Why: pasting the whole plan and saying "go" is the wander trap the conductor exists to avoid -- each deliverable gets a fresh, scoped `claude -p` context instead.

- Open the repo (`code ~/source/tempo`); approve permissions once (`/permissions`) to watch in the panel, but the conductor invokes `claude -p` headless, so no panel interaction is required.
- Create the venv and deps (above), then run `python3 bootstrap/conductor.py` (no `--phase` runs Phase 0 then Phase 1 end to end; `--phase 0` runs just Phase 0; `--phase 0 --deliverable K` just that one step). It walks each phase's prompt playbook deliverable by deliverable: a fresh context per prompt, the layered gate above, a commit per green deliverable, a halt on red. Deliverable 1 authors the seed conduct file (CLAUDE.md, section 17; AGENTS.md waits for the first deliverable that routes a non-Claude agent CLI); once CLAUDE.md lands every later `claude -p` auto-loads it.
- The default run continues into Phase 1 (the walking skeleton) after Phase 0; `--phase 1` runs it alone. Two of its deliverables are real-model runs whose exit is a recorded verdict, not a green suite (the catastrophic-NO-GO spike and the first real ticket merge); the conductor runs them, then pauses for the operator to read the verdict before continuing (`--auto` skips the pause; GO itself is not earnable during the bootstrap -- the GO-grade baseline is a Phase 6 deliverable at cutover, section 19). Export the provider key first (see the Phase 1 prereqs below).
- Re-running over existing work is expected, not an error. The gates check outputs, not authorship, so the conductor cannot tell a deliverable it just built from one an earlier lineage already committed; a deliverable whose output is already present is VERIFIED in place (the conductor's standing preamble carries that instruction into every context) and `commit()` no-ops on the clean tree. Progress lives in `bootstrap/state.json`; a re-seed can leave it desynced from the tree (state says done N while the code is further along), which self-heals as the run advances. To restart a phase clean, delete `bootstrap/state.json`.
- The operator role may be a supervising chat session instead of a human at the terminal (section 17, rule set B): it runs the conductor with `--auto`, because the verdict pause waits on a human, and the drain in the background, then repairs each stop under the section 19 recovery order.
- A deliverable that notices an out-of-scope problem appends it to `bootstrap/suggestions.md` (the pre-Phase-2 stand-in for the Suggestion Box, section 11) and moves on -- it never stops to ASK, because a pytest-gated step does not pause and the question would be lost. The stand-in has a consumer: the Phase 2 Suggestion Box deliverable ingests this file as the box's first messages and retires it (section 19).
- The conductor's final Phase 1 deliverables -- the prompt 14-17 handoff, four seed batches -- author Phase 2's tickets (specs only) into a seeded queue, then it retires. After that the loop flips: the operator runs ONE `tempo drain` and tempo builds Phase 2 AND every later phase through its own pipeline -- each phase's last ticket seeds the next as `confirmed` INTO the same running drain, which carries the whole self-build to quiescence with no per-phase re-run (section 19).

**Phase 0 prompt playbook.** These are the conductor's input -- parsed in order from this section by `bootstrap/conductor.py --phase 0`, not hand-pasted. The conductor runs each in a fresh `claude -p` context, re-runs `uv run pytest` as the gate, and commits per deliverable (halting on red). Each assumes CLAUDE.md is loaded (deliverable 1 authors it). Prompt 1 is the seed-files step; 2-11 build Phase 0 to its exit gate (section 19). Keep every block's format intact -- an "N -- Title:" line, then an "expects:" line naming the space-separated files whose on-disk existence the conductor verifies after the run -- EVERY source file the deliverable creates, its production modules as well as its tests, so the check covers every output and no created module is left undeclared and unowned (`-` waives the check for journal-gated deliverables), then one fenced prompt -- because the conductor parses exactly that shape.

1 -- Seed conduct file (CLAUDE.md):
expects: CLAUDE.md

```
Read TEMPO_PLAN.md section 17 plus the design law in sections 2-3. Author the root
CLAUDE.md exactly per section 17: a 3-5 line
identity block (what tempo is, engine plane vs host plane), ONE read-first pointer
to TEMPO_PLAN.md, seed rule sets A and B as terse behavioral rules, hard cap
<= 120 lines. Do NOT author AGENTS.md: every bootstrap context is claude -p,
which loads CLAUDE.md -- the AGENTS.md curated subset is authored per section 17
by whichever deliverable first routes a non-Claude agent CLI. Write
no engine code. Stop when CLAUDE.md exists so I can review.
```

2 -- Project scaffold:
expects: pyproject.toml uv.lock .gitignore tempo/__init__.py tests/test_scaffold.py

```
Set up the project skeleton per TEMPO_PLAN.md (design law sections 2-3, D1 pure
Python, Phase 0 in section 19). Create a uv-managed pyproject.toml with
requires-python = ">=3.14", runtime deps pyyaml and pydantic (the full
section 0 runtime set), dev deps pytest and
pytest-asyncio; a tempo/ package plus tests/ dir; a .gitignore covering .venv/,
__pycache__/, and .tempo/ (the instance state dir: journal, spools, worktrees;
sections 10, 15).
Ship the project's first test (tests/test_scaffold.py) proving the harness:
the tempo package imports, and pyproject.toml parses (tomllib) with
requires-python ">=3.14" and the declared runtime deps -- enough that the suite
is green and every later pytest gate runs on a proven harness.
No other engine logic yet. Commit uv.lock
(this is an app -- pin it for reproducible sync). No kernel logic yet. Verify uv
run pytest is green, then stop.
```

3 -- journal.py (TDD):
expects: tempo/journal.py tests/test_journal.py

```
Implement tempo/journal.py per TEMPO_PLAN.md (D3 and the Phase 0 kernel list in
section 19): segmented append-only JSONL under the host state dir (the
segmented journal/ layout and ordered segment naming, section 6 -- but NO roll
trigger: size/age rotation and its roll test are a Phase 3 seed with the
daemon, rotation's first retention consumer), glob-ordered read across
segments, write-ahead fsync, torn-tail
tolerance (a truncated final line is skipped on read, not fatal), the pinned
`ts` rendering (section 6). TDD: write
tests/test_journal.py first, including one that reads every event back in
order across MULTIPLE pre-seeded segments (glob-ordered read is layout
behavior, not roll behavior), and one that appends a torn final line and
still reads the rest. Pure stdlib only. Stop when uv run pytest is green.
```

4 -- effects.py (once-semantics):
expects: tempo/effects.py tests/test_effects.py

```
Implement tempo/effects.py per TEMPO_PLAN.md (mechanical-sandwich in section 2, the
Phase 0 list in section 19): an effect wrapper that journals intent, executes
once, journals the result, and on replay returns the recorded result instead of
re-executing. Use the injectable seams (clock, process-exec, filesystem) as
Protocols so tests drive fakes. TDD tests/test_effects.py first: once-semantics is completion-keyed per
section 6 -- an effect whose key already has a completed event is not
re-executed on restart (a crash AFTER the completion is journaled does not
double-execute, and replay returns the recorded result). The intent-only
crash window (intent journaled, completion not yet) is closed by Phase-1
reconcile-on-entry (section 11), NOT the Phase-0 primitive, so do not build
intent-suppression into the primitive. Stop when uv run pytest is
green.
```

5 -- git.py:
expects: tempo/git.py tempo/seams.py tests/test_git.py

```
Implement tempo/git.py per TEMPO_PLAN.md section 10 and D1: every git call through
argv lists (no shell, no string assembly), dir-pinned with git -C <dir>. Cover
the ops Phase 1 needs (init, status, rev-parse, add, commit, branch, worktree
add/remove/prune, rebase, merge --squash, and describe --tags --always --dirty
for the section-6 lockfile identity) as thin typed wrappers over the
process-exec seam. Worktree cleanup is worktree remove + prune, never rm -rf. TDD
tests/test_git.py against a fake process-exec seam plus one test against a real
temp repo. Stop when uv run pytest is green.
```

6 -- Lockfile guard:
expects: tempo/lockfile.py tests/test_lockfile.py

```
Implement the single-writer lockfile guard (tempo/lockfile.py) per TEMPO_PLAN.md
(D2, sections 6 and 15): one advisory flock per project checkout carrying
instance identity, acquired before any write, refused if already held, and
RELEASABLE by the holder -- the drain's self-upgrade handoff releases it
before spawning its child (section 18). TDD tests/test_lockfile.py first,
including lock-contention-refused and release-then-reacquire. Stop when uv
run pytest is green.
```

7 -- config loader:
expects: tempo/config.py tests/test_config.py

```
Implement the config loader (tempo/config.py) per TEMPO_PLAN.md section 15: parse
config.yaml at the
checkout root (or --config path) with PyYAML safe_load, validate fail-closed
against the top-level schema in section 15, enforce the schema_version handshake
(refuse a NEWER schema_version, accept equal; refuse an OLDER one with a paved
road naming migrate-config -- migration is explicit per section 15, the verb
itself Phase 6's), and emit precise errors naming the
bad key. TDD tests/test_config.py first: valid config, missing required key, and
newer plus older schema_version
all covered. Stop when uv run pytest is green.
```

8 -- Artifact models + Gate runner:
expects: tempo/artifacts.py tempo/gates.py tests/test_gates.py

```
Implement the Artifact models and StageResult/Outcome types (tempo/artifacts.py),
and the Gate protocol +
runner + gate-lint (tempo/gates.py) per TEMPO_PLAN.md sections 5 and 7 and the
Phase 0 list in
section 19. Gates are a closed protocol; every gate finding ships a paved-road
message, and gate-lint FAILS a gate that lacks one. Prove the machinery with a
gate-lint test over a stub gate (the first real content gate arrives with a
Phase 1 deliverable that earns it). The runner applies hard/soft severity from
review.gate_severity (invariant 3); with no config loaded in Phase 0 it
defaults to the shipped all-hard map (every v1 hard-set code hard at merge,
section 15). TDD tests/test_gates.py first, including
gate-lint rejecting a
paved-road-less gate. Stop when uv run pytest is green.
```

9 -- LLM interface (fake) + stage driver:
expects: tempo/llm.py tempo/driver.py tempo/enginelog.py tempo/redact.py tests/test_driver.py

```
Implement the LLM interface with a scripted fake (tempo/llm.py), and the one
LLM-stage driver (tempo/driver.py)
per TEMPO_PLAN.md section 5 (invariant 2) and section 6. The driver renders the
prompt, writes it to the attempt spool BEFORE the call (a call that raises or
hangs must leave the sent prompt on disk), calls the LLM seam, captures each
attempt to the spool, writes
structured events to the engine log (never the journal), and runs every captured
stream through the redaction seam scrubbing configured secret VALUES before
persisting. Per invariant 2 the driver then validates the emitted artifact and
runs the stage's gates, feeding a hard-gate failure back as an in-stage
re-prompt on the SAME workspace bounded by the retry cap (section 11.1, shipped
default 6 from caps.retry); Phase 0 exercises this loop with the echo stage's
empty gate list. The fake returns scripted responses for deterministic tests. TDD
tests/test_driver.py first, including a secret value that must not appear in any
spooled/logged output. Stop when uv run pytest is green.
```

10 -- spec renderer + spec lint + plan lint:
expects: tempo/specs.py tests/test_specs.py tests/test_plan_lint.py

```
Implement the spec renderer + spec lint (tempo/specs.py) per TEMPO_PLAN.md
section 8. The renderer keeps untrusted host content in
the DATA channel, never the instruction channel; spec lint fails a spec that
folds data into instructions. The renderer also owns the `Plan contract`
resolver (section 13): a ticket's plan ids -- numeric section ids against
TEMPO_PLAN.md's `## N.` headings, subsection ids (`N.k`) against its
`### N.k` headings, and section-19 unit ids (`19.L`, `19.I`, `19.P0`-`19.P6`)
against its `### 19.<unit>` headings, and entry-unit ids `19.P<n>.<stem>`
against that phase registry's `seeds` row, rendered as that one row's YAML
mapping under its stem -- resolve verbatim,
deduplicated, injected as the sole channel for plan bytes; an unresolvable id,
or section 22, is a fail-closed refusal. It owns the RENDER_BOUND_CHARS and
REQ_RENDER_HEADROOM constants and the delimiter quoting of section 8. TDD the
renderer + lint (tests/test_specs.py), including a lint failure on a spec
that renders injected content outside the data-block form, a `Plan contract`
id that resolves to its section's bytes exactly once when cited twice, a unit
id resolving to exactly its unit, an entry-unit id resolving to exactly its
registry row, an unknown id refused, and a delimiter-
bearing payload rendered quoted. Then implement PLAN LINT per section 8 in
tempo/specs.py and tests/test_plan_lint.py, run against the real
TEMPO_PLAN.md (heading ids, registry parse/coverage/cite resolution,
row fields, size caps). Stop when uv run pytest is green.
```

11 -- Phase 0 exit gate:
expects: tests/test_echo_stage.py tests/test_fault_injection.py

```
Wire the Phase 0 exit gate per TEMPO_PLAN.md section 19. Build a toy echo stage
(tests/test_echo_stage.py):
consumes a stub artifact, emits one, running end-to-end under
the LLM-stage driver with the fake LLM. Then build the crash-point / fault-
injection harness (tests/test_fault_injection.py) over the state layer (journal +
effects) that induces crashes
at each write point and asserts torn-tail tolerance and effect once-semantics.
Phase 0 is done when the echo stage runs green end-to-end AND the fault harness
passes. Stop when uv run pytest is green.
```

**Phase 1 prompt playbook.** Same conductor as Phase 0 -- `bootstrap/conductor.py --phase 1` parses and runs these in order, gating and committing per deliverable -- with two differences. Phase 1 is FIRST real-model contact (a real provider key enters, key-scoped; see the operator prereqs below), and per the section 19 ordering exception the review-baseline eval runs FIRST, before the walking skeleton, because a NO-GO verdict is cheapest to find before the kernel is wrapped in a pipeline. Prompts 1-2 stand up real-model review and run the catastrophic-NO-GO spike; 3-8 build the walking skeleton; 9 is the exit gate; 10-17 are the handoff, one independently-gated deliverable per subsystem -- reconcile-on-entry (10); the `drain` verb's dispatch loop and park/retry accounting (11); the findings-fed re-entry rendering (12); the self-upgrade re-exec and premise park (13); and the seeded Phase 2 queue as four batches (14 caps+harvest, 15 diagnosis+its eval, 16 box+ladder+triage, 17 Author+requisition_review+battery+auditor+exit seed), the conductor's final acts -- no handoff prompt bundles two independently-testable subsystems into one coding context. Prompts 2 and 9 are the real-model deliverables (exit is a recorded verdict / a merged ticket, not a green suite) -- the conductor runs them, then pauses for the operator to read the verdict before continuing (`--auto` skips the pause). After 17 the queue is seeded and the loop flips: the operator runs ONE `tempo drain` that carries Phase 2 and every later phase to quiescence (section 19), each phase's final ticket seeding the next into the same running drain, and the conductor is retired.

**Phase 1 prerequisites (operator, before `--phase 1`).** Phase 1 adds one runtime dependency section 0 did not need -- a reachable model provider -- since Phase 0 ran the scripted fake. Before running `--phase 1`, and required by prompt 2's live run: pick the provider set and each provider's access `kind` (`api` or `cli` -- they degrade differently, section 6); make each reachable (an `api` SDK pulled into the venv, a `cli` agent binary installed with `pnpm add -g <package>@latest`, on PATH behind its argv wrapper, its pnpm package recorded as the provider's `package`, D1); export the key under the env-var NAME the config provider registry declares in `auth`, never a literal secret in any file (an `api`-keyed provider; a `cli` provider on ambient CLI login declares no `auth` and needs no export -- sections 6, 15); record the chosen per-tier model ids -- each provider's `models_by_tier` values and the strongest-model pin REVIEW takes at every tier -- where prompt 1's authoring agent can read them (a setup note, or a pre-authored `providers:` block in config.yaml that prompt 1 then treats as operator-owned truth), so the authored config carries operator-chosen ids, never agent guesswork; and set each no-usage `cli` provider's `limits.est_cost_per_call_usd` (recommended 1.00 USD for a flat-subscription agent CLI whose stream reports no cost, e.g. codex -- section 6). Pinning the (provider, model) rows for REVIEW and AUTHOR is part of the config.yaml prompt 1 AUTHORS (no earlier deliverable creates the instance config); switching later is a config edit, never a ticket edit. If prompt 1 finds NO operator-provided values (no setup note, no pre-authored `providers:` block), it does not guess and does not halt: it authors every operator-owned value as the literal placeholder `OPERATOR-SETS-THIS` (model ids, `auth` env-var names, `est_cost_per_call_usd`) -- schema-valid, so every offline deliverable builds and tests -- and the provider layer REFUSES a live call whose resolved row still carries a placeholder, a config/setup refusal naming this prerequisite (section 6; fail-closed: a placeholder can never reach a model, and inventing a real-looking value is the guesswork this paragraph bans).

1 -- Provider layer + real LLM adapter (first real-model contact):
expects: tempo/providers.py config.yaml tests/test_providers.py

```
Implement the provider layer (tempo/providers.py) per TEMPO_PLAN.md section 19
(Phase 1) and section 6:
a provider registry, routing parse (validation includes the
implement-requires-cli rule, section 6), and the FIRST routed candidate only, with
key-scoped injection (a surface's key reaches only the call that needs it, never
the wider process). Add a real client implementing the Phase 0 LLM
interface behind the routing. The engine ships one `cli` provider adapter per
agent CLI it can drive -- from the start, `claude` AND `codex` -- over a shared
base that owns the write-grant derivation, the redaction of both sinks, and the
cost floor (the trust boundary, so it lives ONCE, never a copy per adapter).
Each adapter is its own argv contract and event-stream parse: `claude` via
`-p --output-format stream-json` (model via `--model <model>`; effort has no
claude flag, so it is recorded in provenance only; write grant
`--permission-mode bypassPermissions`
for implement, the literal read-tool allowlist `--allowedTools Read Grep Glob LS`
elsewhere; text/usage/cost from the terminal
`result` event); `codex` via `exec --json` (model via `-m <model>`, effort via
`-c model_reasoning_effort=<effort>`; write grant
`--dangerously-bypass-approvals-and-sandbox` for implement, `--sandbox read-only`
+ `approval_policy=never` elsewhere; the prompt on stdin via a trailing `-`; text
from the last `agent_message` item, usage from `turn.completed`, NO cost reported
so the declared flat estimate charges, and a `turn.failed`/`error` event is the
failure signal BECAUSE codex exits 0 even on a failed turn). TDD each adapter's
LIVE contract against a scripted fake subprocess (section 6):
the per-surface write-grant allowlist reaches the invocation, the invocation
runs in the passed worktree, and usage/cost is parsed from the event stream
-- a client that omits the write grant must fail THIS suite, never surface
for the first time at the first real ticket. AUTHOR the instance's config.yaml at the checkout
root IF IT IS ABSENT (no earlier deliverable creates it); config.yaml is
HOST-PLANE (section 15), so an existing one is operator-owned truth this
deliverable READS for its routing and never overwrites: schema_version, state_dir
.tempo/state, the provider registry, and routing rows for the REVIEW, AUTHOR,
and IMPLEMENT surfaces at EVERY tier of the closed low|medium|high|max scale --
route IMPLEMENT to codex and AUTHOR to claude, each naming only its provider and
inheriting models_by_tier[tier] (section 6) so it scales with the ticket's
agent_tier with no per-row model, and REVIEW to claude pinned to its strongest
model at every tier. Routing review to a different provider or model than the
implementer is the PREFERRED diversity setting (section 6), never a hard gate:
the only hard requirement is that review runs in a SEPARATE session from
implement (the driver enforces it), so same-model-different-session is allowed
and the operator may route implement to any model (a config edit). The IMPLEMENT
rows pin a kind: cli
provider (the implement-requires-cli rule fails the config at load otherwise,
section 6). All
four tiers are declared so a seed authored at any agent_tier (section 13)
resolves to a route, never a routing hole that terminals infra_error and PARKS
the drain on an operator config edit -- a stop the bootstrap contract forbids
(section 19); shipped defaults may stand elsewhere. Parse ONLY limits.est_cost_per_call_usd from the limits block
(cost stamping needs it, section 6); the rest of limits is Phase 3's to
parse and enforce. If any routing row pins a non-Claude agent CLI, author
AGENTS.md (the section 17 curated subset) in this same deliverable AND reconcile
CLAUDE.md's note that AGENTS.md now exists (a both-files change, section 17) --
the first non-Claude context must start with conduct rules.
The operator's provider values (models_by_tier, routing pins, cli kinds,
est_cost_per_call_usd) are in bootstrap/operator-setup.md -- operator-owned
truth; author config.yaml from them, never placeholders for values it states.
TDD the registry + routing parse and key-scoping against
fakes (tests/test_providers.py); keep the suite offline -- no real call in uv run
pytest. Stop when uv run
pytest is green.
```

2 -- Review-baseline eval: fixtures + harness (the catastrophic-NO-GO spike):
expects: specs/review.md eval/harness.py eval/fixtures

```
Build the review-baseline eval per TEMPO_PLAN.md `19.P1` (exit reads) -- run
it now, before the walking skeleton. First author specs/review.md (the REVIEW
prompt-spec, section 8) if absent -- the SAME spec prompt 6 later wires
unchanged, so the baseline measures the spec the skeleton will run. Commit
eval/fixtures/<name>/ with 15-20
planted defects spanning logic, hidden-info leak, acceptance mismatch, and scope
escape, PLUS at least 3 clean (defect-free) fixtures whose expected verdict is
approve; each fixture dir holds diff.patch (the planted diff) and expected.json
carrying the expected verdict, the defect-class tag, and the author identity
(provider, model, tier) -- authored via a (provider, model) DIFFERENT from the one routed to
REVIEW (at minimum a different tier), the fixture-author identity recorded in
the baseline signal: same-family fixtures share the reviewer's blind spots and
inflate the catch rate (section 6). Implement eval/harness.py: drive
specs/review.md over each fixture
through the provider layer and score catch rate, known-bad false-approve, and
clean false-snag.
Record the run as a journal signal event carrying the baselined identity -- the
resolved (provider, model) rows serving REVIEW and AUTHOR at every tier the eval
exercised, plus those surfaces' spec-major versions -- and the verdict. The
scored run records NO-GO, the only verdict this spike can support: it is
catastrophic-NO-GO detection -- a reviewer that misses gross planted defects is
cheapest to find now, before the kernel is wrapped in a pipeline -- not GO
grading, which needs the >= 50-fixture set and the operator's Author-graph
judgment and is a Phase 6 deliverable run immediately before cutover, where GO
is first read (section 19; section 13 touchpoint 7). NO-GO is a designed state
(section 12 supervised mode; the bootstrap drain is exempt, section 19), not a
blocker. Do not discard the harness or fixtures -- they are the base the
Phase 6 GO-grade baseline extends. This is a real-model run: stop when the eval
records a verdict.
```

3 -- LLM call as effect:
expects: tempo/llmeffect.py tests/test_llm_effect.py

```
Move the LLM call behind the @effect wrapper per TEMPO_PLAN.md `19.P1`
and the effects contract in section 2: idempotency key ticket+run_seq+surface+
attempt+call_seq -- run_seq derived per section 6 as the count of the stem's
prior terminal events -- a cost-event journaled per call, and replay returning
the recorded result instead of re-calling (the Phase 0 driver called the seam
directly -- this replaces that path). TDD tests/test_llm_effect.py with the
scripted fake: a replay that does NOT re-call,
and exactly one cost-event per call. Stop when uv run pytest is green.
```

4 -- Ticket contract + runner intake:
expects: tempo/tickets.py tests/test_tickets.py

```
Implement the ticket contract and runner intake (tempo/tickets.py) per
TEMPO_PLAN.md section 13 and
`19.P1`. Ticket frontmatter carries only fields the scheduler, a
gate, or the authoring/triage policy reads; ordering lives in depends and
priority, never prose. Runner intake: at invocation, validate pending hand-
authored ticket FILES and ticket-plane-commit them, standing in for the Phase 3
watcher. Intake lint enforces the full section 13 grammar, including the
`Plan contract` section-id form and `Context`'s structural refusal of the
plan file itself. TDD tests/test_tickets.py: a valid ticket validates and
commits; a bad-schema ticket is refused with a paved-road message; a ticket
citing TEMPO_PLAN.md in `Context` is refused naming the `Plan contract` road;
a `Plan contract` bullet with an unresolvable section id is refused. Stop
when uv run pytest is green.
```

5 -- CLI verbs: status, new, run <stem>:
expects: tempo/__main__.py tempo/status.py tempo/runner.py tests/test_cli.py

```
Implement the CLI verbs per TEMPO_PLAN.md section 18 and `19.P1`:
status projects current state from the journal (a projection, never
authoritative); new authors a ticket file; run <stem> drives one ticket through
intake + the single-writer lockfile + the stage-dispatch seam, in the single
process holding the lock -- the stages and merge behind that seam are prompts
6-7's deliverables, so dispatch reaching the seam with the locked, validated
stem IS the tested behavior here (a scripted fake stands in). Expose the
verbs through a `python -m tempo` module entry (tempo/__main__.py); keep the project
virtual (no console script until the release path, section 13 touchpoint 6). TDD
tests/test_cli.py against a temp checkout, including run refused when the
lockfile is already held and run provably dispatching the locked stem to the
stage seam.
Stop when uv run pytest is green.
```

6 -- Stages: Implement / Check / Review:
expects: tempo/stages.py specs/implement.md tests/test_stages.py

```
Build the Implement, Check, and Review stages (tempo/stages.py;
specs/implement.md) per TEMPO_PLAN.md sections 4
and 5 and `19.P1`, as prompt-specs run through the LLM-stage driver,
single worker, full artifacts + provenance per stage. Review WIRES the existing
specs/review.md from prompt 2 UNCHANGED -- a spec-major rewrite here silently
drifts the just-recorded baseline identity (section 19). The live pipeline runs
IMPLEMENT-onward on one hand-authored ticket. Do NOT build the Author stage or
specs/author.md: nothing in Phase 1 executes them -- their machine input, the
triaged Suggestion Box, does not exist until Phase 2 -- so they are a seeded
Phase 2 ticket beside the triage consumer that feeds them (prompts 16-17). TDD
tests/test_stages.py: the stage
transitions with the scripted
fake. Stop when uv run pytest is green.
```

7 -- Merge: hard gate set + single-admission:
expects: tempo/merge.py tests/test_merge.py

```
Implement MERGE (tempo/merge.py) per TEMPO_PLAN.md section 9, section 10, and
`19.P1`:
the v1 hard gate set MINUS the bug gate (kind: bug, the ## Regression grammar,
and the merge-base overlay defer to the Phase 6 report inbox, their first
bug-intake consumer -- sections 7, 19), then a synchronous single-admission
merge -- ticket-plane restore (git restore of tickets/** to main's content,
section 9), rebase
onto main, mechanical hard-set re-run, pinned-approval check, squash + trailers,
and journal the admission's state_transition with body to: merged through the
Journal seam (the promoted envelope field, section 6 -- D3 reconcile and the
prompt 9 gate both key on it).
Both writer lanes (squash-merge to main + ticket-plane commits) run inline in the
one CLI process under the single-writer lockfile; the dedicated serial merge task,
red-streak pause, and tree-hash assert are Phase 3. All git through git.py. TDD
tests/test_merge.py against a temp repo: a passing ticket merges with trailers; a
failing hard gate
blocks admission. Stop when uv run pytest is green.
```

8 -- Non-ok terminal:
expects: tests/test_terminal.py

```
Implement non-ok terminal handling per TEMPO_PLAN.md section 11 and section 19
(Phase 1): a non-ok terminal -- a spent cap or any non-ok terminal state --
journals its state transition and exits 1 (the section 18 exit-code contract:
1 = a ticket outcome, 2 = an engine-plane refusal), leaving the ticket and branch in
place. Retry, diagnosis, and auto-harvest arrive with the Phase 2 spine, so build
none of them here (the reject-findings re-entry RENDERING ships with the
prompt 11-12 drain pair, section 11.2). TDD tests/test_terminal.py: a stage returning non-ok leaves
branch + ticket intact,
journals the transition, and the process exits 1 (an engine-plane refusal exits 2 --
section 18's exit-code contract). Stop when uv run pytest is
green.
```

9 -- Phase 1 exit gate:
expects: -

```
Drive the Phase 1 exit per TEMPO_PLAN.md section 19. Author ONE real ticket and run
it IMPLEMENT-onward to a local merge on main -- single worker, real model,
complete artifacts + provenance events, both writer lanes inline (`run <stem>`,
operator-invoked, is itself the supervised-merge go-ahead, section 12). The
prompt 2 baseline record STANDS as the bootstrap-era record -- do NOT re-run
the harness here: its outcome changes nothing the bootstrap does (the drain
is exempt, section 19), the section 12 policy reads identity at read time so
prompt 6's wired specs already resolve any drift to supervised mode, and the
GO-grade baseline is earned immediately before cutover, where GO is first read
(the Phase 6 deliverable; section 13 touchpoint 7). Phase 1 is done when the real ticket is merged
with complete artifacts and events. After this, tempo consumes its own
tickets -- stop hand-prompting and start authoring tickets.
```

10 -- Reconcile-on-entry (reap orphaned in-flight runs):
expects: tempo/reconcile.py tests/test_reconcile.py

```
Implement reconcile-on-entry per TEMPO_PLAN.md section 11 and `19.P1`:
when a `run`/`drain` scaffold acquires the single-writer lockfile, BEFORE it
dispatches, reap any orphaned in-flight run an interrupted predecessor left -- a
ticket the journal shows `running` with no terminal transition, or an
`effect_intent` with no matching `effect_completion`. The scaffold holds the sole
writer lock and no daemon exists, so such a run is provably dead. Reap is
fail-closed and minimal here: journal an `abandoned` terminal state_transition for
each orphaned ticket (never hand-edit the journal -- APPEND through the Journal
seam) and remove its orphan worktree via git worktree remove + prune through
git.py (never bare rm -rf). This is the foreground form of the Phase 3
restart-reconcile; auto-harvest of the orphan (section 11.2) folds in with the
Phase 2 spine, not here -- build no harvest. TDD tests/test_reconcile.py against a
temp checkout: a journal
with a `running` and no terminal is reconciled to `abandoned` and its worktree
removed on the next `run`/`drain`; a clean journal is a no-op. Stop when uv run
pytest is green.
```

11 -- Drain verb (run the ready queue: dispatch, park, retry accounting):
expects: tempo/drain.py tests/test_drain.py

```
Implement the `drain` verb per TEMPO_PLAN.md section 18 and `19.P1`:
drive every ELIGIBLE ticket -- validated, committed, depends satisfied, not
blocked -- through the same path as `run <stem>`, one at a time in the single
process holding the single-writer lockfile, in `depends`-constrained
(priority, age) order, to QUIESCENCE: a non-ok ticket terminal PARKS that stem and the drain continues;
a merge landed mid-invocation satisfies depends edges in the same invocation;
at quiescence -- nothing eligible and unparked -- each parked stem whose
`retry` cap (section 11.1) still holds budget is RE-OFFERED, one retry unit
drawn per re-offer, each draw journaled as a `cap_consumed` event and
remaining budget derived from the journal at entry (a LIFETIME budget per
stem, never an in-memory counter -- section 11.2), eligible work always
running ahead of re-offers, and
quiescence re-evaluated after every re-offer merge so unblocked dependents run
in the same invocation. After EVERY merge, RE-SCAN the committed tickets dir, so
a ticket authored or confirmed DURING the invocation -- a seeding ticket's
output committed to main via the ticket-plane lane -- becomes eligible in the
SAME invocation (section 18's true quiescence, tickets-dir half ONLY; the
Suggestion-Box re-consume is the Phase 2 box consumer's job, NOT this verb's).
The drain ends when nothing is eligible, unparked, re-offerable, or newly
authored, reporting every still-parked red with its findings. Two config bounds
(section 15, the `drain:` block) stop an unattended drain short of quiescence:
the overall wall-clock ceiling `config.drain.max_runtime_hours` -- on trip,
admit no NEW ticket, let the in-flight one reach its stage terminal, journal the
halt, report progress, and name the continuing `tempo drain` (a runaway
backstop, distinct from a quiescence stop, never a mid-stage kill); and the
per-ticket ceiling `config.drain.max_ticket_minutes` -- a ticket whose authored
`Time budget` stuck exceeds it PARKS at dispatch with a paved road (lower the
budget), never running. The reject-findings re-entry RENDERING is prompt 12's
deliverable, completed BEFORE this drain ever runs a seeded queue (section
11.2's re-entry-ships-with-the-drain law binds the 11-12 pair): leave the
render call as a seam here and build no folding logic. Engine-plane
refusals (lock contention, journal corruption, config/setup refusals) still
stop the drain. Fail closed:
an empty ready set is reported, not an error; a depends cycle is refused with a
paved road; no ticket runs before the tickets it depends on have merged. This is
the foreground precursor to the Phase 3 daemon's dispatch loop --
build no async, no worker pool, no queue persistence. The self-upgrade
re-exec and the premise_failed park are prompt 13's deliverable: leave the
re-exec trigger as a called seam here and build neither. TDD tests/test_drain.py
against a temp checkout:
two tickets with a depends edge run parent-first; two equal-priority stems with
distinct authoring-clock events dispatch older-first, and a stem with no
authoring event sorts after any stem with one (section 9's age term); a red
INDEPENDENT first ticket
parks and the second still runs, the red reported at the end; a ticket red on
its first attempt and green on its re-offer merges in the SAME invocation,
exactly one retry unit drawn, and a dependent blocked only on it runs in that
same invocation; a stem whose retry cap is spent stays parked and
is reported, never re-offered; a retry unit drawn in one invocation is still
spent in the NEXT invocation -- budget re-derived from the journal by counting
retry-NAMED `cap_consumed` events (never all caps, so a later cap in the
vocabulary never perturbs a retry-budget assertion -- section 11.2), never
carried in memory; a child whose
parent merges mid-invocation runs in the same invocation; a ticket committed to
the tickets dir DURING the invocation (a running ticket's ticket-plane commit of
a new `confirmed` stem) runs in the SAME invocation with no re-run; a fake clock
advanced past `max_runtime_hours` stops the NEXT dispatch, names the ceiling in
the report distinct from quiescence, and leaves the in-flight stage
un-interrupted; a ticket whose `Time budget` stuck exceeds `max_ticket_minutes`
parks at dispatch with its paved road and never runs; an empty queue
reports and exits zero, quiescence with parked reds still exits zero, and a
ceiling halt exits 1 (the section 18 exit-code contract). Stop when
uv run pytest is green.
```

12 -- Reject-findings re-entry (findings-fed re-offers):
expects: tempo/drain.py tests/test_drain_reentry.py

```
Complete the `drain` verb's re-entry per TEMPO_PLAN.md section 11.2 and
`19.P1`, behind the render seam prompt 11 left: a stem
re-entering after a non-ok terminal folds the prior terminal's findings
artifacts already durable in its ticket dir (review.md reject findings, a
failing checks.json) into the prior-attempts data section of the next
attempt's render -- rendered fresh each attempt IN CRITERIA-POSITION per
section 11.2, never written into ticket.md -- so no drain is ever
findings-blind (a blind re-run repeats its failure unchanged and parks; this
11-12 pair IS section 11.2's re-entry-ships-with-the-drain law, and no seeded
queue runs before both land). TDD tests/test_drain_reentry.py against a temp
checkout: a re-offer's rendered prompt provably contains the prior attempt's
reject findings, positioned in the same prompt section as the acceptance
criteria; a first attempt's render carries no prior-attempts section. Stop
when uv run pytest is green.
```

13 -- Drain self-upgrade re-exec + premise park:
expects: tests/test_drain_upgrade.py

```
Finish the `drain` verb per TEMPO_PLAN.md section 18 and `19.P1`,
behind the seam prompt 11 left. An admission whose diff touches tempo/** or
specs/** re-execs
the drain through the process-exec seam as `uv run python -m tempo drain` --
ONE form, dependency changes included, D1's named runtime exception -- the parked
set carried in argv, via the section 18 HANDOFF: journal the handoff, close
journal handles, RELEASE the single-writer lock, spawn with timeout=None and
inherited stdio, await, exit with the child's code. A stem whose last
terminal was premise_failed stays parked across invocations until its
ticket-plane ticket.md commit changes (section 18) and is never re-offered.
TDD tests/test_drain_upgrade.py against a temp checkout: a self-upgrading
admission re-execs before the next dispatch (fake the exec seam; assert the
HANDOFF ORDER -- lock released and journal closed BEFORE the spawn -- plus
the uv-run argv form, timeout=None, and that the parked set survives); retry
budget spent before the re-exec is still spent in the child -- journal-derived
per section 11.2, never re-armed by the exec; a premise_failed stem is
skipped by the next invocation
and runs again after a ticket edit lands. Stop when
uv run pytest is green.
```

14 -- Phase 1 -> Phase 2 handoff, batch 1 of 4 (caps + auto-harvest):
expects: tickets

```
As the first of the conductor's four final seeding acts (prompts 14-17 author
the Phase 2 queue in `depends` order; nothing dispatches until the operator
launches `tempo drain` after prompt 17), author the first Phase 2 seeds per
TEMPO_PLAN.md `19.P2` and section 11: one ticket.md per
deliverable, written under tickets/<stem>/ as real specs -- caps FIRST, scoped
to diagnosis and the infra budget (the `retry` cap with its journal-derived
`cap_consumed` accounting already shipped with the Phase 1 drain, as did the
reject-findings re-entry -- section 11.2 -- so every spine ticket runs
findings-fed and retry-bounded; this ticket rebuilds NEITHER; the
`premise_bounce` cap and its DRAW land with the escalation ladder + Reject
queue seeds of prompt 16's batch, beside the confirm/reject release verbs -- a
hold never lands before its release, section 2), then auto-harvest before wipe
(allowlist extraction, attempts/ dirs; harvest EXTENDS the section 11.2
rendering, never a second path). AUTHORING LAW, binding every seed in prompts
14-17 (sections 9, 13, 18, 19): every seed is a real spec written under
tickets/<stem>/; each '## Context' bullet a repo-relative path
that EXISTS (the read-first set), plan prose entering only through `Plan
contract` ids (section 13) -- `19.L`, `19.P2`, and the owning sections the
deliverable needs, never whole section 19 and never a whole-plan path; a
fenced existing file whose embedding breaches the section 8 authoring
headroom listed under '## On-demand' instead of Context; each '## Scope
fence' includes every file the acceptance criteria force AND fences the module
that OWNS each hooked seam (section 9 ownership law); seeds ordered by
`depends`/priority so `tempo drain` runs them in a sound order; `Time budget`
by rule, never feel -- expected sized to the deliverable, stuck = 2x expected
-- and every seed DISPATCHABLE under this instance's drain envelope: a stuck
budget over `config.yaml`'s `drain.max_ticket_minutes` parks at dispatch
(section 18), so size that ceiling to at least the largest seed's stuck --
the shipped default (90) sits below the spine's biggest tickets, the envelope
that admits the seeds ships WITH them (never lowering a ticket's authored
stuck threshold, its real hang bound, to fit a dispatch gate), and RAISING
that one key (upward only) is the ONE sanctioned edit of the operator-owned
config.yaml (prompt 1's never-overwrites rule governs every other key -- the
specific command here wins over the general rule); every seed stamped
`confirmed` at authoring (bootstrap seeds are the build plan, never
suggestions -- nothing waits at a draft gate or on a `confirm` verb the seeded
phase has not built yet); the pre-ladder tier rule of section 19 (HIGH tier
until the ladder merges; known-hard seeds high/high with cited evidence).
Author SPECS only; implement none of the code -- tempo does that when the
human runs `tempo drain`. Do NOT author Phase 3+ tickets: their specs depend
on what Phase 2 builds and learns (D10). Stop when the batch's stems exist
under tickets/ and uv run pytest is green.
```

15 -- Phase 1 -> Phase 2 handoff, batch 2 of 4 (diagnosis + its eval):
expects: tickets

```
Author the second Phase 2 seed batch per TEMPO_PLAN.md `19.P2`
and section 11.3, depends-ordered after prompt 14's stems: the diagnosis call
with `lessons` over the harvested material (the sandwich's one model judgment
-- closed verdict vocabulary, mechanical short-circuits), and the diagnosis
real-model eval as its TWO chained deliverables (section 19): first the
harness + committed fixture set, its expected verdicts proven reachable under
the fake LLM and its budget/timeout enforcement pinned by tests against the
committed report artifact; then the spend ticket that only EXECUTES the
already-merged harness. Prompt 14's AUTHORING LAW binds every seed here (it
restates sections 9, 13, 18, 19 -- read those sections). Author SPECS only;
implement none of the code. Stop when the batch's stems exist under tickets/
and uv run pytest is green.
```

16 -- Phase 1 -> Phase 2 handoff, batch 3 of 4 (Suggestion Box + ladder + triage):
expects: tickets

```
Author the third Phase 2 seed batch per TEMPO_PLAN.md `19.P2` and
sections 11-12, depends-ordered after prompt 15's stems: the Suggestion Box
BEFORE anything that files into it (durable queue, signature dedup, decision
registry, and the harvest wiring that enqueues each run record's second
problems -- the mailbox exists before its first letter; its spec includes
one-time ingestion of bootstrap/suggestions.md, the section 0 stand-in, as the
box's first messages -- ONE suggestion-class message per non-empty line --
that file then retired: deleted in the box deliverable's own reviewed code
diff, its `## Scope fence` naming `bootstrap/suggestions.md` (section 19),
never a ticket-plane commit; the storm-control circuit breaker is a Phase 3
seed beside the daemon's continuous producers, and tombstone auto-reopen +
retro itemization are Phase 5's, with the retro stage that reads them --
section 19), then the escalation ladder ending at the Reject queue WITH its
confirm/reject verdict verbs AND the `premise_bounce` cap and its draw landing
before any hold can fire (a hold never lands before its verdict verbs or its
release -- section 2, enforced by `depends` chaining across these seeds; every
seed that WRITES a marked terminal fences the terminal-write owner
(tempo/runner.py, section 9 ownership law) plus the stage layer whose
outcomes feed it (tempo/stages.py), and the Reject queue itself is a
journal-derived projection that writes no terminal -- section 19; the
pre-daemon default is auto-keep while retry budget remains, section 11.4),
then the box's sequential triage consumer (the machine route from a filed
problem to a ticket change) -- invoked ONLY by the operator verb `tempo
triage` (BOOTSTRAP era): ONE pass over every pending box item -- author,
tombstone, or decision-record each -- then STOP. The bootstrap drain NEVER
scans the box itself: no scan before dispatch, none after a merge (sections
12, 18). A box-authored ticket takes its section 12 table default -- almost
always `draft` -- and the operator who ran `tempo triage` confirms only the
few worth building; there is NO self-build auto-confirm exemption, because a
human is present at every scan and nothing from the box need run unattended.
`tempo/policy.py` resolves box starting-state from the table plus scope
override in BOTH eras with NO drain special-case; the seeds pin
`tests/test_policy.py` (a bootstrap box ticket resolves `draft`) and
`tests/test_drain.py` (a drain does NOT scan the box). PHASE SEEDS are NOT
box-authored -- a phase's final ticket authors the next phase's `ticket.md`
files DIRECTLY as confirmed ticket-plane output (sections 12, 19); the
DAEMON-era continuous box consumer and its GO-gated auto-confirm (sections 9,
12) are a SEPARATE era, built with the daemon, never folded in here. Prompt
14's AUTHORING LAW binds every seed (the box and ladder seeds cap out at stuck
150-180m). Author SPECS only; implement none of the code. Stop when the
batch's stems exist under tickets/ and uv run pytest is green.
```

17 -- Phase 1 -> Phase 2 handoff, batch 4 of 4 (Author + requisition_review + battery + auditor + exit seed):
expects: tickets tests/test_seeded_phase2.py

```
Author the final Phase 2 seed batch per TEMPO_PLAN.md `19.P2`,
depends-ordered after prompt 16's stems: the Author stage + specs/author.md
the triage consumer feeds (deferred from Phase 1, where nothing executes them
-- sections 4-5 and 8); `requisition_review` as its THREE chained deliverables
(section 19: the pure review call + spec + gate registration, then the
Author-path consumer, then the seed-path consumer at the seeding ticket's
Check stage with MERGE-SAFETY enforcement, each fencing the module that OWNS
its seam); then the phase's Emits as seeds of their own -- the invariant
auditor, and the shakeout battery as its per-owning-module chained groups plus
the report-lane deliverable (section 19: machine-produced shakeout-report.json
lifted by the one stage-terminal lift path, double-gate re-confirmation; the
battery's pass condition is that auditor) -- then LAST the Phase 2 exit
ticket, its depends transitively covering EVERY Phase 2 seed: it performs the
`19.P2` exit read and authors Phase 3's seeds in the `19.L` core-first chain
(the `19.P3` registry's core admission plus one phase3-continue seeding ticket), by
DIRECT ticket-plane authoring -- its Implement stage writes Phase 3's
ticket.md files `confirmed`, intake-lint validated AND feasibility-reviewed
(`requisition_review`) at its own Check stage, picked up by the drain's
after-every-merge tickets-dir re-scan (section 18) -- NEVER by filing a
Suggestion Box message (the box carries filed problems, never a known
build-plan ticket), and every later phase boundary seeds the same way. Prompt
14's AUTHORING LAW binds every seed. Author SPECS only; implement none of the
code. Do NOT author Phase 3+ feature tickets beyond that core batch and its `-continue` tail (D10). Add a
test (tests/test_seeded_phase2.py) that every seeded Phase 2 ticket passes
intake lint, that every seed's stuck budget is at or under the configured
`drain.max_ticket_minutes` (section 18), AND that the seeded depends edges
realize the deliverable order stated above (section 13 authoring law) -- the
test NAMES its phase's seed stems explicitly and asserts only over those named
stems, never a `source: seed` pattern-match over the tickets plane (every
later phase's seeds carry the same stamp, so a scan would be reddened by a
later seeding from outside this test's own fence); each seeding ticket ships its own per-BATCH named-stem test FILE the same way -- section 19's bounded-batch seeding puts SEVERAL seeding tickets in one phase (the phase-exit core batch, `<phase>-continue`, and any further continuation), and two writers of one per-phase file are a guaranteed conflict in the serial merge queue's rebase -- while the rung-1 resolution machinery is itself a downstream batch: each batch writes its OWN new test file (its Scope fence, never Context), reading the prior batch's merged file for the idiom. PIN GRAIN is law for every such test: it pins IDENTITY and STRUCTURE -- the named stem set, `depends` edges, `source: seed` and other frontmatter values, grammar validity -- and NEVER byte prose, phrase substrings, or criteria counts, because sanctioned machinery legitimately rewrites what byte pins freeze: the section 13 `reject <stem>` kill stamps `rejected`, Rework update/split rewrites criteria and supersedes stems (sections 4, 11.4), and the section 18 premise-park road releases a stem only through a new `ticket.md` content commit. So a state or existence assert accepts the full lifecycle (a later `rejected` stamp or supersedes-map entry is history, not drift); a live-queue assert (dispatchability, awaiting-terminal) is banned outright -- the test classifies only the CLOSED list of its own batch's stems as historical, and a confirmed later-phase stem mid-run is never required to show a terminal event; the section 19 known-hard tier pin asserts the frontmatter tier values and THAT the citing evidence exists, never its wording; and the section 19 invariant-to-test closure obligates tests of the built rule's BEHAVIOR, never a string-match over the prose that states it. Two further closure rules the batched form forces: (1) PINNED MATERIAL -- a seed test pins identity and structure only (stems, edges, frontmatter, fence, Context/On-demand partition) and asserts no size, plan-unit length, render arithmetic, or byte hash: a test that reads live state reddens later, historical, and out-of-fence when a fenced module legitimately changes, and a test that records sizes reddens whenever an unrun seed is legitimately edited in place; it never reads live files, live markers, or the live plan, and once its seeds merge, a seeding test is an immutable historical fixture no later ticket fences or migrates; (2) AUTHORED-STATE ONLY -- it asserts the seed's authored content and `confirmed` birth, never byte-identity against lifecycle state: the section 13 terminal `rejected` stamp into an authored seed's `ticket.md` is a sanctioned later transition that must not redden a merged seeding test. Stop when uv run
pytest is green. After this the conductor is retired: the human runs ONE
`tempo drain` that carries Phase 2 and every later phase to quiescence
(section 19), each phase's final ticket seeding the next as `confirmed` into
the same running drain.
```

## 1. What tempo is

A continuously running orchestration engine that authors and runs tickets against host project repos, including itself: it drafts the ticket set for a feature, wires dependencies, runs implement -> check -> review -> merge, harvests failures, and files follow-ups.

The ticket lifecycle is named after a business procurement cycle, with code development activities in place of the business activities:

| Procurement stage    | Dev activity                | Actor      |
|----------------------|-----------------------------|------------|
| Requisition          | Author ticket(s)            | LLM        |
| Fulfill & Ship       | Implement code              | LLM        |
| Invoicing            | Lint / acceptance checks    | script     |
| Inspect & Accept     | Code review                 | LLM        |
| Rework               | Update/split/escalate ticket| LLM        |
| Reject               | Human keep/edit/kill queue  | human      |
| Approve & Pay        | Approval record             | script     |
| Settle Payment       | Merge to local main         | script     |
| Reconcile            | Retrospective + cost ledger | LLM        |

The table is also the operator vocabulary: every operator-facing surface (CLI output, escalation text, README) speaks it or plain English; scheduler jargon stays in the spec and the code.

Design goals, in priority order:

1. Simple and robust over feature-rich; check/feature accretion is the primary failure mode designed against.
2. Automated: human touchpoints are enumerated and small.
3. Continuous queue, not batches, with live re-prioritization.
4. One consistent pattern for every stage, gate, and handoff.
5. Every failure path is designed.

Non-goals for v1: multi-machine execution, a composition/plugin engine, a web UI, GitHub PRs in the loop, deterministic re-execution recovery, fleet management (section 15).

## 2. Design law and anti-goals

Design law -- principles every section below must satisfy:

- **Rule surfaces fail closed.** Allowlists and closed vocabularies, never denylists; a paved road ships inside every prohibition; mechanical enforcement backs prose; exactly one narrow, auditable exception valve per gate.
- **A hold ships with its release.** Any state that suspends a ticket pending an outside verdict or change -- the Reject queue, a premise park, a poison quarantine -- lands WITH OR AFTER its release path -- in the same deliverable, or in a deliverable whose `depends` chain guarantees the release merged first -- and its paved road never names a verb that is not yet built. A hold whose only release is a ticket change may not precede the machinery that machine-produces ticket changes (sections 11, 12). A hold that can strand a ticket with no reachable release is fail-stuck, not fail-closed.
- **Failure recovery is a mechanical sandwich.** Deterministic machinery outside, exactly one model judgment call in the middle, deterministic dispatch out; caps are checked before the model is called. Two distinct retry senses: cross-attempt retry (section 11.4) is a fresh workspace from the top, never resume; the in-stage re-prompt loop (section 5, invariant 2) feeds gate findings back against the SAME workspace, bounded by the retry-like-action cap. When the deterministic layer can tell that asking the model is pointless, it does not ask.
- **The ticket is the contract; the run record is the receipt** (section 13), with predicted-vs-actual divergence as the calibration signal.
- **No dual-path code.** No compat shims, defensive parallel codepaths, or deprecation layers for internal surfaces; rename in place and update every call site in the same change; recovery is git revert.
- **Executed work is fenced by gates, not jailed.** v1 does not sandbox agent subprocesses or ticket-authored commands; on a single-operator personal box they run with the operator's own privileges (section 16). Gates and review are the trust boundary, never the goodwill of executed code.
- **Operator surfaces speak plain language.** Names a human reads or types -- CLI verbs and options, report lines, error text, ticket stems -- use ordinary words, parseable without this plan; plan-internal shorthand (spine, box, plane, quiesce) never reaches an operator surface. Internal vocabulary (journal event types, outcome enums) may stay terse. An existing violator renames via ticket when next touched, per the dual-path law.

Anti-goals -- failure modes this design prevents, each addressed by a numbered decision:

- No consistent pattern between steps for gates and handoffs (-> D4).
- Feature/check accretion that makes automation brittle (-> D10).
- Batch-oriented scheduling fighting a continuous queue (-> D6).
- One author-declared path list overloaded to do both conflict prediction and scope bounding (-> D7).
- A remote code host's PR machinery inside the dev loop (-> D8).
- Shell scripting as orchestration glue (-> D1).
- Metadata accreting to serve subsystems that do not exist yet (-> the anti-bloat law, section 13).

## 3. Core decisions

- **D1 -- Pure Python.** No bash anywhere: no shell scripts, no `shell=True`, no string-assembled commands. External binaries (git, agent CLIs) are exec'd with argument lists through dedicated wrapper modules. One tempo-owned venv, created and populated with `uv` (setup only, with ONE named runtime exception: the bootstrap drain's self-upgrade re-exec runs `uv run python -m tempo drain` through the process-exec seam, section 18), on Python 3.14+ (the pinned floor); startup refuses any other interpreter or a lower Python. Why: a stopped drain waiting for a human to retype the command is the failure the bootstrap contract bans (section 19); 3.14 raised from 3.11 for currency and EOL runway to Oct 2030, above the language features actually used (`tomllib` at 3.11, PEP 604 unions at 3.10).
- **D2 -- Single asyncio daemon.** One process, ONE in-flight ticket run (single-flight dispatch) -- through the entire self-build and the first daemon era. A concurrent worker pool is a post-cutover D10 return earned by measured queue starvation on host work; it multiplies the review surface and the conflict surface faster than any other knob, so it is never a default. No leases, no heartbeat protocols, no distributed locks. Crash recovery is restart + rescan + reconcile against the journal. A single-writer lockfile per project checkout (carrying instance identity) fences the orchestration write surfaces -- ref mutation, commits, the journal, dispatch state -- against a second daemon or a chat session; hand-editing FILES under the tickets dir is OUTSIDE the fence (the human intake path, section 13, safe because only the daemon commits). Reconcile scans from the journal tail; an additive `checkpoint` event shape is RESERVED (not built in v1) so bounding the scan later is a new event type, not a schema rewrite -- it ships per D10 only when a full rescan actually hurts.
- **D3 -- Files + journal are the source of truth.** Tickets and artifacts are markdown/JSON files in the host repo, committed via the serial writer's ticket-plane lane (section 10); every state transition and external action is an event in an append-only JSONL journal. Derived views (status, backlog, ledger, scorecard) are projections, never authoritative; no index builder -- every projection is a stdlib read-time fold over the JSONL (an analytics engine such as DuckDB is a D10 return at the first projection measured too slow as a fold). Crash precedence: for an externally observable effect the WORLD wins (a branch/file/main row that exists is truth) and reconcile re-derives the journal note -- a merged ticket specifically from the squash commit's `tempo-ticket` / `tempo-reviewed-sha` trailers (section 10), the identity a deleted branch no longer carries; the journal is authoritative only for intent and non-observable effects. Durability: artifact and ticket writes are atomic (temp file, fsync, rename); the journal is append-only, one JSON object per line, and every reader tolerates a torn trailing line -- a crash mid-append drops the partial record, no reader aborts.
- **D4 -- One stage contract** (section 5). Stages communicate only through schema-validated artifacts on disk plus the journal. One driver runs every LLM stage. One gate shape. One outcome vocabulary.
- **D5 -- Fail-closed gates only** (section 7).
- **D6 -- Continuous scheduler** (section 9). No waves, no groups, no batch verbs. Ordering comes only from `depends` and `priority`; a file watcher makes priority edits and new tickets take effect at the next dispatch.
- **D7 -- Scope is fenced by a gate; conflicts are resolved at merge** (section 9). No author-time conflict prediction anywhere; the serial merge queue is the one place conflicts are resolved. Scheduling may AVOID manufacturing a conflict only on observed facts, never on estimates -- the overlap law and its dispatch-skip / pre-review-hold checkpoints (section 9).
- **D8 -- GitHub out of the dev loop** (section 10). Local main is the blessed line; merges are local; GitHub is a non-blocking checkpoint sidecar.
- **D9 -- Thin primitives kernel** (section 6). Journal, Effect, Signal, Timer adopted in minimal form; re-execution recovery, hash chains, blob stores, and the composition engine refused.
- **D10 -- Every feature earns its place with an incident.** The only way a gate, check, or subsystem is added is the surprise -> rule -> gate promotion path, with the incident cited in its docstring. Retro reports catch-rates and proposes deleting gates with zero catches over N tickets. Gate count is a managed budget, not a ratchet. The v1 core set is the one seed grant, chosen by design rather than incident; D10 governs every addition beyond it, and retro's prune path applies to the seed set the same as to earned additions.

## 4. Lifecycle and artifacts

Every stage consumes one typed artifact and emits one typed artifact; the procurement documents are the artifacts:

- Requisition consumes a triaged Suggestion Box message (section 12 -- the one machine route into authoring; human intake authors ticket files directly, section 13) and emits the **ticket** (one or more, with a dependency graph).
- Implement emits the **packing slip**: a branch plus a run record.
- Invoicing (mechanical) emits the **invoice**: a structured check report.
- Inspect emits an **approved invoice**, a **snag list** (to Rework), or an **RMA** (to the human Reject queue).
- Rework emits a **rework order**: updated ticket, split tickets, and/or a model-tier escalation (the escalation element is consumed by the section 11.4 ladder's journal-derived rung, never written into ticket frontmatter).
- Approve (mechanical) emits an **approval record** -- an attestation (approver surface identity + reviewed SHA + the spec versions + the resolved gate set: the plain list of gate codes, review-surface names, and severities in effect at approval time), not a cryptographic signature -- pinned to the reviewed commit SHA. The approval record persists in the JOURNAL, as the body of the admission's `state_transition` (its pydantic model, section 13) -- never a ticket-dir file: the section 13 ticket-dir listing is closed, and `review.md`'s pinned verdict is the merge gate's on-disk input.
- Settle (mechanical) merges via the merge queue.
- Reconcile emits **retro findings** into the Suggestion Box.

LLM stages: Author, Implement, Review, Rework, Suggestion-Box triage, Retro. A second engine review surface, `requisition_review`, judges an AUTHORED TICKET rather than a diff -- the feasibility review that runs before a ticket commits confirmed (section 7). Everything else is a script. Judgment goes in LLMs; verdicts go in scripts.

## 5. Kernel contracts

```python
class Stage(Protocol):
    name: StageName          # closed vocab: author|implement|check|review|
                             #   rework|merge|triage|retro
    kind: Literal["mechanical", "llm"]
    consumes: type[Artifact] # pydantic v2 model, schema-validated on read
    emits: EmitSpec          # ONE type, OR an emits_by_verdict map for branch
                             #   stages (review: approved-invoice | snag-list
                             #   | RMA; triage's verdict-keyed set, section 12;
                             #   rework emits ONE composite rework-order type,
                             #   section 4); keys are stage-local verdicts,
                             #   NOT Outcome values; schema-validated on the
                             #   branch taken
    gates: list[GateCode]    # gates that validate the emitted artifact

@dataclass(frozen=True)
class StageResult:
    outcome: Outcome         # ok | already_satisfied | invalid_artifact |
                             #   gate_failed | premise_failed | timeout |
                             #   infra_error | budget_exceeded
    artifact: Artifact | None
    findings: list[Finding]  # {code, path, line, message, paved_road,
                             #   kind, unit}
    cost: Cost               # tokens, seconds, attempts, plus usd + provider +
                             #   model on LLM calls (usd=0, provider/model=None
                             #   for non-LLM effects); the effect_completion
                             #   event carries the same fields (section 6 ledger)

class Gate(Protocol):
    code: GateCode           # engine-shipped closed vocab (section 7 v1 set):
                             #   ticket_schema | scope_fence | verification |
                             #   run_record | diff_budget | post_rebase_regate |
                             #   bug_evidence | core_drift (both land Phase 6:
                             #   bug_evidence v1-deferred to its first
                             #   consumer, core_drift additive per D10,
                             #   sections 7, 8, 19) | correctness_review |
                             #   requisition_review. Host review surfaces
                             #   register more codes (own gate budget).
                             #   correctness_review at merge = the pinned-
                             #   approval check for the admitted SHA, never a
                             #   re-executed model call (section 9).
    def check(self, artifact, workspace) -> GateReport
    # GateReport: {code, verdict, findings, autofix_applied}; verdict is
    #   the closed pair pass | fail -- hard/soft severity is applied by
    #   the runner from config (invariant 3), never carried in the report
```

Invariants:

1. Stages communicate only through artifacts on disk plus the journal. No side channels, no shared mutable state. Every artifact EXCEPT the ticket (exempt per the versioning policy below) records the spec version and commit SHA that produced it (provenance): base-model fields `produced_by_spec_version` and `produced_at_sha`. `produced_at_sha` is the HOST-repo commit the stage's workspace was at (main for author/triage-time artifacts, the branch HEAD for implement/review-time ones) -- in the self-hosted case that commit legitimately IS an engine-repo SHA; what it never records is the running instance's own release identity.
2. One driver runs every LLM stage: render spec -> call model -> validate artifact schema -> run gates -> on hard failure feed structured findings back and RE-PROMPT the same workspace within caps -> escalate. A schema-INVALID artifact -- including a verdict value outside its closed vocabulary -- is a validation failure that takes this same bounded re-prompt loop (the validation error fed back as the finding) before any terminal, never a full-cycle burn. Before validation the driver strips exactly ONE surrounding markdown code fence (optionally language-tagged) from the final output, because agent CLIs routinely wrap JSON that way; any other malformation takes the re-prompt loop. Stages differ only in spec file, artifact type, and gate list. For Implement, call-model may be an agent-CLI subprocess executing in the ticket worktree behind the LLM seam (`kind: cli`, section 6), granted write access to that worktree because Implement is the one surface that mutates the tree (the grant is per-surface and fail-closed, section 6); a re-prompt is a re-invocation of that subprocess with the structured findings appended.
3. Gate severity (hard/soft) lives in config, not gate code: for engine-shipped codes the `review.gate_severity` map (MERGE context, section 15; shipped default: the v1 hard set of section 7 is hard at merge), and for host-declared mechanical checks the per-entry `severity` field (section 7). Author-context severity is an engine constant (soft) until a configuration actually needs the second dimension -- the per-context map is a D10 return. Soft failure emits a Suggestion Box message and continues; hard failure feeds the retry loop.
4. `premise_failed` is a first-class outcome: Implement may return "the bug does not reproduce / this conflicts with X", routing back to Requisition with feedback instead of grinding out a bad implementation. `already_satisfied` is the complementary first-class short-circuit: an Implement that finds every acceptance criterion ALREADY holds on the base -- proven by running the ticket's own `## Verification` commands green, never by judgment alone -- terminates `already_satisfied`, and the ticket settles as a no-op (journaled `to: merged` with no code diff; regenerated or reseeded work frequently arrives already built, and parking it as a false premise turns recovery into churn).
5. Autofix lints must be idempotent: run twice, assert fixpoint.
6. Branch stages (review, triage) emit one of several artifact types by stage-local verdict via `emits_by_verdict` (rework emits ONE composite rework-order type, section 4). Two named steps sit OUTSIDE the `StageName` vocab on purpose: `diagnose` is a failure-spine substep (section 11.3) and `approve` is a mechanical substep of `merge` (section 4). Model-facing config is keyed by `llm_surface`, a closed vocab (the LLM stage names plus `diagnose` and the engine review surface `requisition_review` of section 7, extended at config load with each host-declared review surface's `name`), used by prompt-spec frontmatter (section 8) and the provider routing table (section 6) -- so the diagnosis call gets a governed, versioned spec and a routing row without becoming a Stage, and a host surface can route to its own provider. A surface with no routing row of its own inherits the `review` row.

Every `Finding` carries a REQUIRED `paved_road` field -- a gate that cannot tell the agent what to do instead does not pass gate-lint; `path` and `line` are OPTIONAL (nullable), since a frontmatter- or schema-level finding has neither. `kind` and `unit` are OPTIONAL (nullable). `kind` is the closed `spec_gap | authoring_error | null` (section 7): REQUIRED on every `requisition_review` finding, optional on an Implement `premise` finding (absent = a plain premise), null from every other producer. `unit` is REQUIRED exactly when `kind` is `spec_gap` and the judged ticket has at least one HARDENABLE unit (section 11.4), and then names one of those units; otherwise it is null. A violation is a schema-invalid reply at the producing surface and takes that surface's schema-invalid path (invariant 2); `unit` is a validated field, never parsed from prose.

**Versioning policy** (one rule at every durable boundary): every persisted format that crosses a boundary carries a schema version, every reader refuses-newer and migrates-or-tolerates-older, and evolution is additive-only within a version (a field's meaning is never repurposed). Stamped surfaces: journal events (`v` per event); the base `Artifact` model (one inherited version field, `artifact_schema_version`, carried alongside and distinct from the invariant-1 provenance fields the base model also holds); the host config (`schema_version` handshake, section 15); the report inbox (engine refuses newer, tolerates older); the rendered `tempo:core` block (version + hash stamp); prompt specs (section 8). Ticket frontmatter deliberately carries NO version field -- and the ticket is likewise the ONE stage-emitted artifact exempt from the base-model `artifact_schema_version`/provenance fields (its provenance lives in the journal and the authoring stage's StageResult, so the closed frontmatter list in section 13 stays complete): tickets are ephemeral and flow between instances through git (successive engine versions operating one repo line -- concurrent divergence is excluded by the one-writing-instance rule, section 15), so a version-refusing parser would deadlock mixed-instance operation. Additive-only evolution plus strict closed-vocab authoring lint IS the ticket versioning provision; a breaking ticket-schema change ships by quiescing the queue and letting old tickets drain, never by migrating them. Quiesce = the section 9 kill-switch pause applied to DISPATCH only: stop admitting tickets, let in-flight ones finish and merge, so "drain" is a bounded, observable state. Across concurrent instances a breaking change is coordinated by pausing each instance's dispatch before the schema flip -- a manual v1 operator step.

## 6. Primitives kernel (adopt thin, refuse the rest)

### 6.1 Primitives

The kernel decomposes onto seven durable-execution primitives (the set Temporal, Restate, and DBOS converged on). Verdict per primitive:

| Primitive  | Adopt? | v1 form |
|------------|--------|---------|
| Journal    | yes    | JSONL event log, schema-versioned events. NO hash chain, NO content-addressed blob store (no v1 consumer). |
| Effect     | yes -- biggest win | one wrapper (`@effect(key=...)`) for every external action: git ops, LLM calls, ticket emission, notify, push. Owns journaling, idempotency key, cost capture, once semantics. |
| Signal     | yes -- as convention | human inputs (confirm, reject verdict, kill switch) are journal events. Completes the human-oversight audit trail. |
| Timer      | yes -- tiny module | journaled deadlines re-armed at daemon startup ("retro after M days", checkpoint pushes, stuck thresholds surviving restart). Built in Phase 3 with the daemon, its first consumer (D10). |
| Lease      | mostly no | ONE single-writer lockfile per checkout with instance identity; in-process asyncio locks otherwise. No general subsystem. |
| Step       | already have | it is the Stage protocol + driver. Skip the determinism formalism. |
| Projection | as a rule | derived views never authoritative. Satisfied by stdlib read-time folds over the JSONL. |

Explicitly refused: DBOS-style deterministic re-execution recovery. Why: the discipline tax (all nondeterminism through injected seams) buys nothing over the proven model (retry = fresh worktree from the top, daemon recovery = rescan + reconcile); without re-execution, hash-chaining and Step formalism have no consumer.

### 6.2 Journal

**Journal file layout and rotation.** The journal is a `journal/` directory of append-only JSONL SEGMENTS under the host's config-declared state dir (never a hardcoded path, section 15). One ACTIVE segment is appended and rolls at a size/age threshold (engine constants: 64 MiB or 24 hours, whichever first). The roll trigger ships with its first retention consumer, the Phase 3 daemon (section 19) -- the segmented layout and readers land in Phase 0, and pre-daemon the active segment simply grows. Rolled segments are IMMUTABLE, so the append-only law and torn-tail tolerance apply only to the active segment's tail. Segment names sort in write order (zero-padded sequence + roll date, e.g. `000001-20260804.jsonl`), so `read_json_auto` over a `journal/*.jsonl` glob reconstructs the ordered stream (D3's no-index-builder rule holds). v1 NEVER deletes or compacts a segment; retention/GC is the deferred ARCH block, returning via D10. `Journal.read_segments()` is the ONE parser -- it yields each ordered segment as its own ordered event tuple under the same corruption and active-tail rules -- and `Journal.read()` flattens it, so the auditor's per-segment laws need no filesystem reach-through. Reconcile and status read newest-segment-first; the reserved `checkpoint` event (D2) later bounds that scan to a suffix of segments.

Durability is write-ahead: an effect's intent event is fsync'd BEFORE the effect executes, completion events are fsync'd before anything dispatches on them, and background appends fsync at least every 3 seconds (an engine constant, not config) -- a crash can cost buffered non-load-bearing events but never an intent record, a Timer, a Signal, or a cap-consumption event the system acted on.

Both readers obey ONE law: tolerate exactly a torn FINAL line of the ACTIVE segment; any other malformed line -- mid-segment or in a rolled segment -- is a hard error surfaced as corruption, never skipped. The WRITER's startup dual: truncate a torn final line of the active segment before appending (append-only applies to records, not bytes), so a resumed append never creates a mid-segment malformed line and a roll never seals a torn tail into an immutable segment. (Bootstrap-surfaced hazard, filed to the sink per section 11: the startup dual runs only at startup, so a LIVE writer that tears its own active tail without dying -- a short write under `ENOSPC` -- then rolls that segment before any restart WOULD seal the torn tail into an immutable one. v1 does not guard the live path; repair-before-roll is the earned fix when it first fires.) Every projection reader validates rolled segments strictly and applies tail-tolerance only to the active segment (never a blanket ignore-errors read).

**Journal event envelope.** Every line is one JSON object with a fixed top-level shape -- `v` (event-schema version, per `type`, per the versioning policy above), `type` (a value from the closed `EventType` set below), `ts` (UTC from the clock seam in ONE pinned rendering: RFC 3339 with a `+00:00` offset, exactly an aware-UTC Python `datetime.isoformat()` -- pinned so lexicographic order IS chronological order, and readers, projections, and the bootstrap conductor may compare `ts` as a string), `ticket` (owning ticket stem or null), `key` (the effect idempotency key or null), and `body` (a `type`-specific object) -- so every type-varying field lives in `body`, never at top level. The closed `EventType` set (extended additively per D10, each type carrying its own `v`):

| `type` | emitted |
|--------|---------|
| `effect_intent`     | before an effect executes (write-ahead) |
| `effect_completion` | after it completes, carrying the result |
| `signal`            | a human input lands (confirm, reject, kill) |
| `timer_armed`       | a deadline is scheduled |
| `timer_fired`       | a deadline elapses |
| `cap_consumed`      | a failure-spine or budget cap is spent (section 11) |
| `state_transition`  | a ticket or run changes state |
| `checkpoint`        | RESERVED, not emitted in v1 (D2) -- bounds the reconcile scan later |

An unknown `type` on read is a hard corruption error, never skipped (the fail-closed law). Per-type `body` schemas are implementer-owned pydantic models, validated at the write seam and versioned by each type's `v` -- deliberately not enumerated here (same status as the non-`run.md` artifact bodies, section 13); the ENVELOPE, not the bodies, is the cross-version contract -- with FOUR promoted body rules. First: every `state_transition` body carries `to`, the destination state's name from the closed RUN-STATE vocabulary -- `running`, plus the terminal states `merged`, `abandoned`, `rejected`, and each non-`ok` `Outcome` value (section 5) used as a terminal state name -- the vocabulary the section 15 invariant auditor validates against, because D3's world-wins reconcile and the bootstrap conductor's verdict gate both key on it. Second: a terminal `state_transition` that routes its stem to the Reject queue also carries the marker `routed: reject_queue` -- the field the section 9 eligibility fold reads. Third: a terminal's `dispatch` is closed -- `retry` (re-offer at the same rung, drawing `retry`), `escalate` (re-offer one rung up, the rung recorded on that `retry` draw), `reject_queue` (awaits a Reject verdict; always paired with `routed: reject_queue`), `spec_gap_hold` (held on a hardening round, section 11.4), or ABSENT (no spine dispatch: premise park, over-bound park, `budget_exceeded`, drought, `abandoned`); every consumer matches it exhaustively, so a new value fails the build until each consumer classifies it. A spec-gap terminal also carries `plan_units` (`{<entry unit id>: <unit sha>}`) and, when held, `round` (section 11.4). Consumers key on `dispatch`, `routed`, `round`, and `plan_units`, never on `reason`, which stays the code-only operator-facing value and the identical-wall comparison key (section 11.4). Fourth: a `signal` body's NAME (its `signal` key, or `kind` for the signals that key on it) is closed: the write seam refuses a name outside the one enumerated set kept beside `EventType`, and a new name enters that set with its consumer. Everything else in a body stays implementer-owned.

### 6.3 Effects

**Effect contract (the once semantics reconcile depends on).** `@effect(key=...)` -- decorator sugar over the `Effects.run(action, *, key, ticket)` primitive, deferred to its first call site (the Phase 1 LLM call) while Phase 0 ships only the primitive -- is once-per-KEY against the surviving journal: on restart, an effect whose key already appears as a completed event is not re-run. That completed-key check reads the journal directly (D3, no index builder) -- the daemon holds an in-memory set of completed keys, scanned from the journal at startup and appended to as effects complete.

The key DOMAIN is chosen per effect class:
- attempt-EXCLUDING for user-visible side effects that must fire once across retries (notify / page / GitHub push key on ticket+event, so a retried run never double-alerts);
- reviewed-SHA for merge (merging a given approved SHA is idempotent);
- attempt-scoped for re-runnable work (a fresh worktree's git ops key on ticket+attempt, and an LLM call on ticket+surface+attempt+call_seq -- a monotone per-attempt sequence -- so each call records and replays deterministically and a re-prompt takes a fresh key, never a replayed result). A ticketless box-driven LLM surface uses the SAME universal key shape with the surface name in the ticket slot -- triage `llm/triage/<box message seq>/triage/<pass>/<call_seq>`, Author `llm/author/<pass>/author/<box message seq>/<call_seq>`, where `<pass>` counts prior `triage_pass` signals -- and spools under `spools/<surface>/...`, never a surface-specific bypass of the Driver or the LLM effect.

Every per-run key -- the attempt-scoped class and the ticket-plane RUN-artifact commits (run record, checks, review; the intake ticket commit is once per file and stays run-blind) -- additionally carries the RUN SEQUENCE: the count of this stem's prior terminal events (completed terminals and `abandoned` reaps, never a run-less drought park, section 15) in the journal, derived at run entry by a read-time fold (D3, no counter file). Same-sequence replay is a WITHIN-PROCESS guard: only a re-entry with no intervening terminal replays, and every cross-process entry path reconciles FIRST (scaffold on-entry, daemon restart-reconcile -- section 11), reaping an interrupted run to `abandoned` -- so crash recovery is always a fresh sequence doing real work, and the cross-run once-guarantees live in the run-BLIND domains below (notify, merge). A run that reached ANY terminal is history: the next entry takes the next sequence, every key is fresh, and the re-run is real work. Keys are never retired and never deleted -- a superseded run's completions are inert journal history, unreachable by construction.

The attempt-excluding class and the reviewed-SHA merge key keep their domains run-BLIND on purpose: an alert fires once per ticket+event across all runs, and merging a given approved SHA is idempotent -- with ONE named exception: the spiral-warning notify key carries the run sequence, because a fresh run's spiral is fresh spend and must re-page (section 9).

Effects also split by OBSERVABILITY for reconcile: an externally observable effect (branch exists, file on disk, row in main) is reconciled by observing the world; a non-observable effect (notify) that crashed between acting and journaling is reconciled conservatively -- re-send beats a missed alert, and the attempt-excluding key prevents a storm. A fresh run's worktree creation is defined as TEARDOWN-AND-CREATE: any worktree and branch a prior run left are removed first (worktree remove + prune + branch delete, through git.py), then created fresh from current main -- so a fresh keyspace can never trip over a dead run's leavings.

Do NOT add a separate idempotency-class enum: the key already encodes the class. The `key=` argument is a callable over the effect's own arguments (`(args) -> str`; a bare string is allowed for a singleton effect), and the arguments it closes over ARE what select the domain above -- an attempt-excluding key omits the attempt number, an attempt-scoped key includes it. Key components join with `/` (the `key=retro/<seq>` convention, section 14): keys persist across restarts and self-upgrades, so the join is pinned, never per-call-site taste.

### 6.4 Single-writer lockfile

**Single-writer lockfile (the Lease row).** One advisory `flock` on the lockfile `<state_dir>/tempo.lock` fences the orchestration write surfaces (ref mutation, commits, journal, dispatch state) against a second daemon or a chat session. `flock` is chosen over an `O_EXCL` pidfile precisely because it needs no liveness protocol (D2 refuses leases and heartbeats): the kernel releases the lock when the holder dies, so a crashed daemon's lock is free at restart with no stale-pid reclaim. The file records the holder identity -- `{instance_id, pid, host, state_dir, started_at}` (instance_id is the installed release tag, or for an untagged dev instance the engine checkout's `git describe --tags --always --dirty`) -- for diagnostics and the "someone else holds it" error, but correctness comes from `flock`, not from reading those fields. In-process asyncio locks serialize tasks within the one daemon; the lockfile is the only cross-process lock.

### 6.5 Logging, spools, and redaction

**Operational logging (the journal is not a debug log).** The journal records intents, effects, signals, timers, caps, and state transitions -- never diagnostic chatter: it is versioned, corruption-strict, and never deleted. Diagnostic capture is two instance-local surfaces under the state dir, neither ever authoritative (no gate or dispatch decision reads them; harvest quotes them as informational material only), plus one shared filter:

- **Engine log.** Daemon diagnostic output -- unexpected exceptions, watcher/debounce noise, reconcile detail -- goes to one line-oriented engine log, size-rotated (shipped default 16 MiB x 4 files; an engine constant, not config). Load-bearing failures are ALSO journaled as events (a watcher parse failure, a harvest error); the engine log is the human-readable detail behind the event, never the record.
- **Attempt spool.** The driver tees each stage attempt's full output -- subprocess stdout/stderr, the agent adapter's JSONL event stream (section 9), check and verification command output -- to a per-attempt spool at `<state_dir>/spools/<stem>/<attempt>/` (`<attempt>` is the RUN SEQUENCE above -- one attempt per run entry, so a run's spool dir and its `attempts/<n>/` harvest dir share the number, section 11.2). Inside it each model call's capture (prompt, output, error) lives at `<surface>/call-<nn>/`, the stage's surface plus its call sequence, so the Implement, Review, and diagnose calls sharing an attempt never overwrite one another's files. The rendered prompt is written to the spool BEFORE the model call executes: a call that raises or hangs must leave on disk exactly what was sent. That spooled prompt file is ALSO the sole prompt-delivery channel: the agent-CLI adapter hands the prompt to the child by connecting THAT FILE as its standard input (section 15's `stdin_path`), and the rendered prompt is NEVER a command-line argument -- a prompt is unbounded while an argv element is capped by the OS argument limit, so a large ticket's prompt on argv aborts the call before the model is reached (the failure a file avoids by construction); argv carries only the fixed flags. The spool is what the section 11 harvest cuts its capped tails from and what spiral detector fixtures are cut from (section 9). Spools are deleted at the run's post-harvest cleanup; a spiral warning or poison quarantine PINS the run's spool (never auto-deleted) until an operator releases it, so fixture material survives the wipe.
- **Redaction seam.** Configured secret VALUES (the env vars named by the provider registry's `auth` fields) are redacted from every captured stream -- journal bodies, engine log, spools, harvested artifacts -- by one shared filter at the write seam that replaces each resolved secret value, matched as a literal substring, with the token `[REDACTED:<NAME>]`, so a subprocess that echoes its environment cannot persist a secret. The filter is wired at CONSTRUCTION time, before the first spool write, into EVERY production writer: the four streams above resolve to a maintained WRITER-SITE checklist (the journal writer, the engine-log writer, the attempt-spool writer, harvest serialization -- one stream may have several writers, and every writer of a listed stream appears here), and a deliverable adding a captured stream or a new writer of an existing stream extends this checklist in the same change; the regression test exercises the CONFIG-to-writer path end to end -- a test that passes the secret in manually proves nothing and is a banned test shape (it satisfies the letter of the Phase-0 prompt-9 secret test, section 0, while the wiring is absent). An effect RESULT is scrubbed exactly once, BEFORE it becomes the effect's return value and its completion record: the executing path and a later replay of the same key return the SAME post-scrub bytes by construction -- the recorded result is the only version that ever exists.

### 6.6 Block catalog and fake LLM

A 16-code block catalog names the orchestration building blocks. It is a NAMING SCHEME and coverage checklist, not a plugin system:

| Code   | Block                                                      | v1?      |
|--------|------------------------------------------------------------|----------|
| AUTH   | ticket authoring (schema + closed vocabularies)            | core     |
| HGATE  | human gate (draft->confirmed, reject verdicts, quarantine release) | core |
| PLAN   | scheduler (gutted to deps + priority dispatch, section 9)  | core     |
| WORK   | workspace-per-ticket + engine-agnostic agent adapter       | core     |
| GATE   | gate-check harness (hard/soft config, check-runner)        | core     |
| SHIP   | merge queue (rebase, re-gate, squash-merge, section 9)     | core     |
| EMIT   | dedup-then-emit shared ticket writer (the Suggestion Box)  | core     |
| THRESH | threshold-then-escalate (caps, budgets, circuit breaker)   | core     |
| WDOG   | watchdog (stuck/spiral detection, external heartbeat)      | core     |
| DIAG   | the single failure-diagnosis step (section 11)             | core     |
| HARV   | harvest-from-artifact on failure (section 11)              | core     |
| JUDGE  | standalone LLM judgment step (verdict + confidence)        | deferred |
| CLUS   | failure-cause clustering (signature normalize + group)     | deferred |
| HOOK   | lifecycle hook registry (isolated terminal side effects)   | deferred |
| STEW   | steward composition (idempotent maintenance chains)        | deferred |
| ARCH   | archive / reconcile GC (terminal-artifact relocation)      | deferred |

Deferred codes return only via D10. The table is a coverage checklist; module naming is the implementer's.

The fake-LLM adapter is a first-class kernel component: the `LLM` interface has a scripted-fake implementation from day one, and journaled effects returning recorded results IS the record/replay mechanism -- replay fixtures fall out of the kernel, keyed by the effect key itself, never a separate fixture id.

### 6.7 LLM interface and routing

**LLM interface (the one seam every model call crosses).** One async method behind the `@effect` wrapper, with a `cli` implementation plus the scripted fake; the `api` implementation is a D10 return built at the first configured `api` provider (section 18), and the interface's `kind` field reserves its place. This is a kernel contract the driver and cost capture both call, so its shape is fixed here, not implementer-owned:

```python
class LLM(Protocol):
    kind: Literal["api", "cli"]
    async def call(self, req: LLMRequest) -> LLMResult: ...
    # abort_current is the synchronous kill seam, deliberately NOT behind
    # @effect (the one-async-method-behind-@effect law above stays true): it
    # returns only after the external writer (the CLI subprocess and its
    # process group) can no longer mutate the worktree. The driver calls it
    # BEFORE harvest and BEFORE recording any timeout terminal -- proven by a
    # test with a cancellation-resistant fake.
    def abort_current(self) -> None: ...

@dataclass(frozen=True)
class LLMRequest:
    surface: LlmSurface      # closed vocab (section 5): selects spec + routing
    rendered: str            # rendered prompt (spec renderer); the agent-CLI
                             #   invocation input for kind: cli
    tier: AgentTier          # low|medium|high|max -> models_by_tier
    effort: AgentEffort      # low|medium|high|max, passed through to the model
    ticket: str | None       # owning ticket stem (keying + provenance)
    worktree: Path | None    # the writable tree; set for a cli Implement run
                             #   only, the one surface granted write (invariant 2)

@dataclass(frozen=True)
class LLMResult:
    text: str                # model / agent output
    input_tokens: int | None # None when a cli stream reports no usage
    output_tokens: int | None
    provider: str            # which provider served (journal + run-record stamp)
    model: str               # which model served
    usd: float               # metered cost, or the cli est_cost_per_call_usd
                             #   fallback (below); feeds effect_completion cost
```

The routing table (below) resolves `(tier, surface)` to the concrete `(provider, model)` BEFORE the call; a resolution landing on a row still carrying the section 0 placeholder value is refused pre-call as a config/setup refusal (the placeholder convention -- never a live call); a `kind: cli` provider fills the token fields from the adapter event stream when present and falls back to `limits.est_cost_per_call_usd` otherwise.

### 6.8 Provider layer

**Provider layer.** The `LLM` interface is multi-provider. Instance config declares a provider registry -- `{name, kind: api | cli, auth (an env-var NAME, never a literal secret), package (a `cli` provider's pnpm package), models-by-tier, limits}` -- plus a routing table mapping each (`agent_tier`, `llm_surface`) to an ORDERED candidate list of (provider, model) pairs. A candidate names a provider and MAY pin an explicit `model`; when it omits one, the provider's `models_by_tier[tier]` supplies it, so `models_by_tier` is the per-provider default and a candidate's `model` is an optional per-route override, never a second independent source (Phase 1's authored config pins a model only where the row demands it -- REVIEW at every tier -- while its implement/author candidates name only a provider and inherit `models_by_tier[tier]` from the start, per the section 0 prompt 1 authoring order). Tickets ask for semantic capacity only (section 13); the routing table is the single place semantics resolve to a concrete provider, so switching providers is a config edit, never a ticket edit. Tier resolution is uniform, and the PREDICATE decides -- the parentheticals are examples, never enumerations: every surface invoked FOR a ticket (implement, review, rework, its diagnose, `requisition_review` of that authored ticket) resolves at that ticket's `agent_tier`; calls no ticket owns (triage, retro, Author) resolve at the config's `routing_default_tier` (section 15; shipped default medium). The journal and run record stamp which provider and model served each attempt. A provider key is injected ONLY into the agent-CLI or API call that needs that specific key; every other child process -- verification commands, checks, evidence replay -- is spawned without that key in its environment and never inherits it. The child base environment is INHERIT-MINUS-SECRETS: every child gets the parent's full environment (an ambient-login CLI needs HOME and PATH) minus every env var any provider's `auth` names, with exactly the serving call's key restored for that one call -- never an allowlist rebuild. **The provider set is a bootstrap INPUT, collected up front and carried through unchanged.** The operator's setup (the Phase 1 prerequisite, section 0), BEFORE the conductor and seeds run, collects EVERY input the build needs -- which providers, each provider's STYLE (`kind: cli` or `kind: api`), its models by tier, and its key. The CONTRACT covers both styles and any provider count the config declares, but v1 SHIPS only the `cli` style: a `cli` provider is a subprocess behind its argv wrapper (D1); the `api` client (an operator-installed venv SDK import, fail-closed if absent -- the api analog of the section 15 seam's unresolvable-`argv[0]`) is a D10 return built, together with its spend ceiling and token buckets (below), at the first configured `api` provider -- until it ships, a config declaring `kind: api` is REFUSED at load with an error naming the unshipped client (fail-closed, never a silent fallback). Neither a CLI binary nor an SDK is ever a tempo runtime dependency in `pyproject.toml`. The specific set is the operator's CHOICE, never hardcoded in a seed -- this repo happens to run two `cli` providers (codex, claude), but a seed builds GENERICALLY over whatever the config declares. Once the conductor and seeds start there are NO human gates of any kind: everything the build needs was gathered at setup, so no seed stops to ask for input, and none adds, removes, re-declares, or switches away from a configured provider. A provider or model change is a `config.yaml` edit BETWEEN runs, never a ticket edit and never mid-run. Phase 4's provider work (routing, failure classification, cooldowns, failover) builds the engine handling over the configured set -- `cli` classified from exit code + stderr, `api` from HTTP status (below) -- adding no provider and no dependency.

### 6.9 Rate limits and spend

- **Rate limiting is deterministic and pre-call.** Per-provider CONCURRENCY caps are enforced by the dispatcher BEFORE the effect executes; a call that would exceed the cap waits or spills to the next candidate instead of burning a provider-side rejection. The wait-vs-spill rule is deterministic: spill when the next candidate can serve the call NOW and the primary's projected wait exceeds 60 seconds (an engine constant, not config); otherwise wait, FIFO. Token buckets (requests/min, tokens/min) are `api`-metering machinery, deferred with the `api` client -- a `cli` provider exposes no enforceable TPM/RPM surface (below). Time spent waiting at a cap is journaled and EXCLUDED from the ticket's time budget and the watchdog's wall-clock regions, so a starved run never reads as stuck.
- **Spend is stamped in v1; spend ENFORCEMENT ships with the `api` client.** Every completion event carries its cost (metered usage when the stream reports it, else the declared `est_cost_per_call_usd` flat estimate) -- the LEDGER is the read-time fold over `effect_completion` cost fields (D3), and reconcile reports it. On a FLAT-SUBSCRIPTION `cli` provider a USD ceiling is meaningless backstop -- marginal per-call cost is ~0 (the CLI reports a NOTIONAL equivalent-API cost, as Claude Code's `total_cost_usd` does, or none, as Codex does) -- so the operative backstops are the provider's own rate/quota limits (`rate_limited` | `quota_exhausted`, below), the concurrency caps, and the circuit breaker, and a flat-subscription instance carries NO USD spend cap by design. The cumulative spend ceiling -- global and per-provider over a rolling window, the pre-call check whose crossing returns `budget_exceeded` (section 5), pauses dispatch, and fires the budget-exceeded escalation (section 13) -- protects real per-token dollars and is deferred WITH the `api` client to the first configured `api` provider; the config-load refusal above keeps that fail-closed (no `api` provider can run unmetered before the ceiling exists). The `budget_exceeded` outcome and escalation stay defined in their vocabularies; their producer ships with the ceiling.

### 6.10 Provider failures, failover, and droughts

- **Provider failures classify into a closed vocabulary:** `rate_limited | quota_exhausted | outage | auth_error | model_error | unclassified`. `rate_limited`: honor retry-after, retry same provider, spill under queue pressure. `quota_exhausted`: mark the provider cooling-down until its window resets (a journaled Timer whose duration is `limits.quota_window_minutes`, section 15 -- a `cli` provider exposes no retry-after, so the window length needs this declared source), fail over for subsequent calls, emit a soft Suggestion Box report. `outage`: per-provider circuit breaker -- K consecutive failures opens the circuit for T minutes (shipped defaults K=3, T=10) -- with failover. `auth_error`: fail over AND alert (a section 13 escalation) -- a dead credential never self-heals, so failover must not silently mask it. `unclassified` -- a `cli` exit matching no known class -- is a first-class member, never a raw `infra_error` loop: it draws from the stem's `infra` budget (section 11.1), counts toward the same circuit breaker, and at budget spent the stem PARKS with a paved road naming the provider and the captured exit/stderr tail, so an unrecognized CLI failure mode is bounded and visible instead of retried blind. Classification is the provider's; the driver owns ONE mapping from a classified exception to a `Finding` (`code` = the failure class, message = the provider error, paved road = the provider's declared login road for `auth_error`) carried in the attempt harvest -- never copied into the terminal reason, which stays code-only -- and an unclassified exception carries no finding.
- **One provider payload per session.** Each lock-held engine session (`drain` or `serve`) composes ONE provider registry/cooldown payload and ONE journal-backed `Timers` lifetime and threads it to every provider-client construction site (implement, review, rework, triage, Author, retro); nothing constructs a second registry. The watchdog and its inner client resolve each call's served identity once from that payload. A classified quota hit arms cooldown and ends that attempt WITHOUT drawing the stem's `infra` cap; later calls skip the cooling candidate in configured order; an all-candidates-cooling route holds until its journaled Timer fires.
- **Failover granularity is the attempt, never mid-run.** An in-flight run is not migrated; if it dies, the normal fresh-workspace retry re-resolves routing and lands on the next healthy candidate.
- **All candidates exhausted for a tier:** a DROUGHT is a ROUTING outcome, never a spine failure -- no call was possible, so nothing is metered. The dispatcher consults breaker/cooldown/quota state BEFORE dispatch; a stem whose resolved route has no available candidate parks COST-FREE, the dispatcher journaling the drought-park event (the journal-derived source of section 9's `not parked (provider drought)` eligibility term): no model call, no cap draw, no diagnosis, no Reject-queue arrival -- the `budget_exceeded` park's exact shape (section 11.3) -- and the stem re-enters eligibility automatically once routing resolves, an open circuit's cooldown expiry included (section 9). A drought DISCOVERED MID-RUN (a stage call refused pre-call because the resolved tier's candidates are all unavailable -- open breaker, cooling quota, exhausted tier) terminals the attempt `infra_error` with the drought reason so the section 11.2 handler still runs (harvest -> journal -> wipe), but that reason draws NO cap, gets NO diagnosis, and is EXEMPT from section 11.4's identical-terminal ladder and Reject routing -- it drought-parks exactly like the pre-dispatch case (capability cannot fix weather; the headless auto-keep must never convert a drought into a spend loop or a terminal rejection of healthy work). What survives for the resumed attempt is section 11.2's harvest -- run record, findings, committed-candidate sha in the prior-attempts render; the unreviewed diff never enters the ticket spec, and failover granularity stays the attempt (this section). Every provider failure from a call that actually RAN (outage, model_error, unclassified, auth_error) still terminals `infra_error` and draws the infra cap per section 11.1 -- a failed call is always metered; only the absence of a callable provider is free, its bound the routing-resolves re-entry gate rather than a cap. A fleet-wide provider drought raises the escalation alert (section 13).

### 6.11 CLI provider contract

- Implementer and reviewer always run in SEPARATE SESSIONS -- distinct stage calls each with their own context, never one session grading its own output. That separation is the HARD requirement, enforced structurally by the one-call-per-stage driver (section 5). Routing the review surface to a DIFFERENT provider or model than implement is a PREFERENCE layered on top -- it reduces shared model-family blind spots -- never a hard gate: the same model in two separate sessions is allowed, and which model serves each surface is the operator's config choice (a config edit, section 15).
- **`kind: api` and `kind: cli` degrade differently.** An `api` provider (once its client ships) exposes per-call token usage and HTTP status: full contract above. A `cli` provider is a subprocess black box: per-provider CONCURRENCY caps and classification from exit code + stderr only, no enforceable TPM/RPM buckets, no retry-after (spill-on-pressure and the circuit breaker still apply). Cost is never unmetered -- Implement typically runs as a `cli` subprocess and is the largest spender: a `cli` provider's completion events carry usage parsed from the adapter event stream when the CLI reports it, and a provider whose stream carries no usage MUST declare `limits.est_cost_per_call_usd`, a conservative flat estimate charged to the ledger per call; declaring neither is a config error (fail-closed). Phase 4's quota-exhaustion exit is exercised over whatever providers the config declares -- a `cli` provider classifies rate-limit/quota from exit code + stderr, an `api` provider from HTTP status. Failover PROOF is fixture-scoped: the failover deliverable's verified runs exercise a multi-candidate FIXTURE registry in which the failing candidate fails over to the next, and the `19.P4` exit read -- "lands the next attempt on the failover candidate" -- is read from THAT verified run's served-identity stamps, never from a provider added to the live config for the occasion. The live config is never the fixture: a SINGLE-candidate live route (a legal operator choice -- the set is the operator's CHOICE, above) never invents, adds, or switches to a provider to satisfy an exit read; its live quota evidence is instead the `quota_exhausted` classification, the armed journaled cooldown Timer (above), and that Timer's journaled `timer_fired` event after the window resets, the served identity remaining the sole configured candidate throughout -- a quota with no remaining candidate parks per the candidates-exhausted rule (above); either style's access (a `cli` binary, an `api` SDK) is operator-installed at setup, never a tempo runtime dependency (section 0).
- **A `kind: cli` call's write grant is per-SURFACE, never per-directory, and fail-closed.** Implement is the one surface that mutates the tree, so its `worktree` is set (above) and the agent-CLI subprocess is invoked as a WRITING surface -- the adapter's write jail OFF, running with the operator's own privileges per section 16. A directory-scoped grant ("write access to exactly the worktree") cannot serve Implement: a worktree's git metadata lives in the parent checkout's `.git`, and a `## Verification` toolchain writes caches outside the workspace root, so a jailed subprocess can neither commit nor verify and terminates `ok` on an EMPTY diff. Every other surface -- Author, Review, each host review surface, and the ticket-less triage/retro calls -- runs the agent CLI READ-ONLY and writes no file (they emit an artifact the driver writes, invariant 1). The grant is a closed allowlist of the tree-writing surfaces, derived by the client build from the request's `surface`, never a global default a subprocess carries: a surface off the allowlist gets read-only, so a new writing surface adds itself deliberately. This is a kernel contract, not an adapter default. Routing validation enforces the same rule fail-closed at config load: a candidate serving the `implement` surface must be `kind: cli` -- an `api` provider returns text, and no apply-patch machinery exists to turn text into worktree edits (a D10 addition if ever earned) -- so a config that routes Implement to `api` is a config error.
- **The reliability knobs are a D10 seam.** Circuit breaker, cooldown Timers, and diversity routing ship in their simplest working form; each tightening waits on a cited incident.
- **Provider preflight.** Before every `tempo drain` dispatches, each routed (provider, model) pair answers one minimal probe call, and each configured `cli` provider's installed version (`<binary> --version`) is compared with its package's latest release (`pnpm view <package> version`). A failing probe refuses the run -- exit 2, nothing dispatched, no cap drawn -- naming the provider, the model, and the provider's own message, with the paved road `pnpm add -g <package>@latest` or a routing fix in `config.yaml`. A stale CLI whose probes pass is reported in the run's output with that same upgrade road, never a refusal: a newer release can lag its platform build, and the probe is what proves the installed CLI serves. The Phases 0-1 conductor runs no model probe: its own guard (section 21) refuses to start unless git, uv, pnpm, and the bootstrap CLI are on PATH and that CLI is at its package's latest release.

Event vocabulary note (deferred, not v1): if external anchoring ever matters, align journal event fields with OpenTelemetry GenAI attributes and the EU AI Act Article 12 logging bar. The AI audit trail (a v1 refusal, section 18) returns as projections over the journal -- trajectory/reward/preference exports, per-merge attestations -- never as ticket frontmatter.

## 7. Gates: fail closed, one valve, earn your way in

### 7.1 Gate shape

Every gate is built to the five-rung shape:

1. Prose explains WHY (the only rung that teaches intent).
2. Never a denylist. Allowlist semantics and closed vocabularies only; unlisted means stop. The Gate base class offers `allowed:`-style helpers, not pattern-ban helpers.
3. Every finding carries its paved road (required field).
4. Mechanical enforcement is the only road to merge: the merge stage is the single writer to main and re-runs the hard gate set itself (mechanical codes; the review surface participates as the pinned approval, section 9). Prose compliance is never load-bearing.
5. Exactly one valve, uniform everywhere: `gate_bypass: [{code, reason}]` in ticket frontmatter, surfaced at the draft->confirmed human gate, visible forever, auto-reported to the Suggestion Box. K bypasses of the same gate code (shipped default K=3 within one retro window) auto-files a rule-defect report (an `override_report` box message, section 12). There are NO env-var bypasses; the config namespace does not contain them.

### 7.2 Hard gate set

v1 hard gate set (each cites its motivating incident; parenthesized stage = whose emitted artifact the code validates): ticket schema + closed vocab (Requisition); ticket feasibility (`requisition_review`; Author and seed authoring, section 13); scope fence (section 9; Check); the ticket's own `## Verification` commands pass (Check; executed like every ticket-authored command, section 16), attributed by BASE DIFF (the attribution machinery ships with the Phase 2 spine beside its filing consumer, the Suggestion Box -- the Phase 1 gate fails on any red, section 19) -- a `## Verification` command that fails on the branch is re-run at the branch's MERGE BASE, and one that ALSO fails there is a PRE-EXISTING base failure: filed as a second problem (section 11.7) and charged to neither the Check nor the retry budget, so a full-suite command never fails a stem for a red it did not introduce. A command green at the base and red on the branch is the branch's own regression and fails the Check; the merge-time INTEGRATION CHECK on a green main (section 9) is the backstop for any command the base excused. Attribution is per COMMAND, never per test -- the engine ships no per-runner output parsing (section 18); run record present + schema-valid (Check); diff budget (Check) -- a mechanical cap on the reviewable diff: changed-file count and inserted-line count against engine constants (shipped: 30 files / 1,500 inserted lines; a FIXTURE-CORPUS row's diff -- `19.P6` `go-grade-corpus-*`, fence `eval/fixtures/` alone -- counts fixture DIRECTORIES, not files, against the file term), sized so every rendered review prompt fits the serving provider's context bound (the section 8 render contract is the same law at the prompt seam) -- an over-budget diff terminals `gate_failed` with the paved road "split the ticket (diagnosis verdict `split`, section 11)", so an unreviewably large diff is split at the gate, never dispatched to a review call that cannot hold it (a fixture corpus counts per file, so an eval case is ONE closed envelope file, never a directory of per-field files); post-rebase gate re-run (merge); bug gate (Check) -- a `kind: bug` ticket must carry evidence in its ticket dir and a regression check that failed pre-fix, verified mechanically: the gate runs the ticket's `## Regression` command (section 13) at the branch head, which must PASS, and at the merge base WITH the branch's `carries:` paths overlaid (section 13), which must FAIL -- the overlay exists because the regression test usually arrives WITH the fix, and a bare base run would fail for the vacuous reason that the test file does not exist yet, satisfiable by any new test regardless of the defect (heavier bug machinery -- commit-order enforcement, spec-first ordering, device matrices -- is excluded until earned per D10). The bug gate ships with its first bug-intake consumer, the Phase 6 report inbox (section 19); bootstrap-era merges run the hard set without it. Two engine LLM review surfaces in v1: `correctness_review` (correctness / acceptance-criteria review of an implemented diff, participating at merge as the pinned approval, section 9) and `requisition_review` (the feasibility review of an AUTHORED TICKET before it commits confirmed -- run at AUTHORING for every machine-authored ticket whatever its starting state: a draft-starting box ticket carries the verdict on its draft, and the later human `confirm` flip re-runs nothing -- the semantic complement to the grammar-only `ticket_schema` gate: it judges the ticket BUILDABLE against the shipped engine and the plan it renders, catching a criterion that contradicts merged behavior, a `## Scope fence` missing a file the acceptance criteria force, mutually unsatisfiable criteria, an authored base Implement render over the section 8 bound (RENDER FEASIBILITY, measured mechanically at authoring -- section 19), or -- for a phase-exit/seeding ticket, which is IN SCOPE like any machine-authored ticket -- an exit criterion reading a signal or artifact that no deliverable of its phase emits (the exit-read closure, section 19), verdicts approve | snag | rma mirroring code review -- a snag re-authors within caps, an rma parks for a human, section 13). Always-run vs diff-triggered is a config map from path prefixes to gate codes (`review.trigger_map`, section 15 -- it governs ENGINE-shipped gate codes; host-declared checks and surfaces carry their own per-entry `trigger`). Everything else starts soft and must earn hard status (D10).

### 7.3 Review surfaces

**Host-declared review surfaces.** The engine ships the generic gates above; everything project-specific comes from the HOST's config (data plane), in a `review` section validated fail-closed (unknown codes, shapes, or vocab are config errors, not skips):

- **Mechanical checks:** each entry is `{code, argv command, trigger: always | path-prefixes, severity: hard | soft}`. The engine runs the argv through the check-runner contract (any stack): exit status plus OPTIONAL structured findings JSON on stdout -- a JSON array of section 5 `Finding` objects (`{code, path, line, message, paved_road}`; `paved_road` optional in this one producer, defaulted to the generic road) -- wrapped into the uniform GateReport. A bare nonzero exit with no JSON becomes a single finding whose message is the captured stderr/stdout tail and whose paved_road is generic, so an off-the-shelf linter (ruff, eslint, clippy) drops in without a per-tool adapter. A check that CRASHES fails closed -- routed like a hard failure, never a silent skip.
- **LLM review surfaces:** for CODE the engine ships exactly one (`correctness`; the authored-ticket feasibility surface `requisition_review` of section 7 is the separate authoring-time review, not a diff surface); the host declares additional surfaces as `{name, trigger, rules_doc, severity: hard | soft}`, where `rules_doc` is a host-owned review-rules file the engine's generic review spec renders in as content. Composition is conjunction: each triggered surface runs as its own review call under the one driver (routed by its `llm_surface` name, section 5); a hard surface's snag-list routes to Rework exactly like `correctness`'s; a soft surface's findings go to the Suggestion Box without blocking; and the merge-admissible approval (section 9) exists only when EVERY triggered hard surface has approved the same SHA -- the approval record's resolved surface list (section 4) pins "which surfaces approved", never implicit. New surfaces earn their way in per D10 within the host's own gate budget, and every surface is measured by the section 14 catch/escape scorecard. Every `requisition_review` finding carries a closed `kind`: `spec_gap` (a SPEC GAP, section 11.4: a hardenable unit of the judged ticket whose row lacks a SPEC DEPTH field, or omits or contradicts a fact the work needs) or `authoring_error` (the ticket contradicts stated spec or merged code, or is malformed). A `spec_gap` finding names in `unit` the hardenable unit it gaps -- required whenever the judged ticket has one, null otherwise (section 5). An Implement `premise` finding may carry the same `kind` and `unit`. Section 11.4 routes on `kind` and `unit` only. REPLY SHAPE: a review reply is ONE bare JSON object with nothing before or after it; a schema-invalid reply is the REVIEWER's error, clears no finding, and the next round reviews the whole ticket again -- including the transitive import and composition closure of its fence and every preservation suite its `## Verification` names -- so a finding lost to reply shape is re-raised, never approved past. CITE ROLES are grammar: a `source: seed` ticket whose `## Plan contract` violates section 13's role list (an implementing seed citing `19.L` or a phase unit; a seeding ticket omitting `19.L` or `19.I`) is snagged `authoring_error` at the Requisition gate before any review call.

## 8. Prompt specs

### 8.1 Prompt specs and the render contract

Every LLM stage is defined by a prompt spec file -- one engine-plane file per `llm_surface` at `specs/<surface>.md`: YAML frontmatter (closed vocab: `llm_surface` (section 5), consumes/emits artifact types, model tier, effort, gate list, version) plus fixed prose sections (Role, Task, Inputs, Output format, On-failure -- every spec tells the model what to do when it cannot comply).

- Spec lint validates frontmatter schema, vocab, section presence, size budget (shipped default: 200 lines). Structured feedback with paved roads, bounded AI re-run.
- Rendered-prompt size is a render CONTRACT: the renderer measures every rendered prompt against the serving provider's context bound -- for CLI providers exposing no tokenizer, ONE engine-owned conservative character-count bound, the same at every tier and effort so climbing the ladder never shrinks it -- engine constant `RENDER_BOUND_CHARS` = 400,000, about half the smallest configured model's context window at three characters per token, leaving the other half for the agent's own reads and work; a provider with a smaller window lowers it (stage-local bounds are forbidden) -- and REFUSES an over-bound render as a named stage terminal -- a MECHANICAL PRE-CALL SHORT-CIRCUIT like `budget_exceeded` (section 11.3): ticket-text arithmetic, never implementer failure, so no diagnosis call and no cap draw; the stem parks like `premise_failed` (paved road: shrink the inputs or split the ticket), released only by a ticket-plane content change, and dispatches mechanically to Rework `split` once Rework is live -- never dispatching a prompt for the provider to crash on. The diff budget gate (section 7) keeps review prompts inside this bound by construction; prompt delivery is always the spooled stdin file (section 6), never argv. AUTHORING HEADROOM: an authored ticket's base Implement render must fit `REQ_RENDER_HEADROOM` = 0.75 of that bound (300,000 characters), leaving room for attempt history (`19.L`).
- PLAN LINT keeps this plan renderable as per-ticket seed contracts (Phase 0, `tempo/specs.py`, `tests/test_plan_lint.py`): every `###` heading is a subsection `N.k` inside its own `## N.` section, `19.L`, `19.I`, or a phase unit `19.P<n>` -- NO other heading exists under section 19 (an entry unit is a registry ROW, never a heading) -- and each id has exactly one heading; every `BEGIN_REGISTRY_P<n>` block parses as YAML, its `admissions` cover its `seeds` exactly with at most two payloads each, every non-exit row carries non-empty `records`, `observable`, and `tests` fields (and `owner` when its `does` or `records` names a module absent from main), and every seed `cite` resolves to a quoted `"N.k"` subsection, `"19.L"`, `"19.I"`, or an integer section whose size is at most 10,000 characters -- a whole-section cite of a larger section fails lint. SIZE CAPS are law, lint-enforced: `19.L` at most 22,000 characters, `19.I` at most 4,000, each phase unit at most 60,000 INCLUDING its registry, each non-exit registry row at most 2,600 characters of YAML text of which `owner` + `records` + `observable` + `tests` at most 1,100 (1,600 for a `deep: true` row), every other citable section at most 40,000, and the whole plan at most 520,000; the synthetic seed render -- `19.I` + the largest registry row + the largest subsection any row cites + an empty ticket -- is at most 80,000 (one fifth of the bound). A plan commit that changes `RENDER_BOUND_CHARS` or `REQ_RENDER_HEADROOM` carries a section 22 entry in the same commit, else lint fails. A plan edit failing lint is reworked before it commits; a row that cannot state its contract under its cap is two rows (GRAIN LAW, `19.L`), never a longer row.
- Untrusted-origin content never enters a prompt as instructions. Host docs render inside delimited data blocks; player-report text and evidence files render only as quoted data marked untrusted (and only summarized-by-triage text reaches ticket prose, section 12). Spec lint checks that templates wrap injected content in the data-block form -- the data/instruction boundary is a rendering contract, not stage-local vigilance. A data payload that legitimately contains the engine's data-block delimiter -- a Review diff of renderer code, retry findings or a harvested tail quoting it, a test asserting it -- is QUOTED at the ONE rendering boundary before data-block validation: every occurrence becomes the deterministic visible text `[tempo-data:` in the model-visible payload, the durable source staying byte-exact; this is engine-owned rendering, never a stage refusal or a reason to omit the material. A rendered prompt or prompt spec is never injected as a later prompt's input, quoted or otherwise. The change that first lands quoting is bootstrap-safe under the OLD renderer: its diff never spells the raw delimiter (it uses the engine constant and dynamically built fixture values) and leaves existing delimiter-bearing lines byte-identical. Golden tests prove a payload whose runtime value contains the delimiter reaches the model visibly quoted.
- Specs are code: versioned as MAJOR.MINOR (a MAJOR bump is the "spec-major change" that fires the section 13 touchpoint 7 re-baseline trigger), golden-tested (render from fixture inputs, snapshot), and every artifact records the producing spec version.
- Spec changes ship as ordinary tickets through the pipeline (self-hosting). Formal canary/shadow rollout is deferred until a spec change causes harm.

### 8.2 Rule planes

**The two rule planes.** tempo defines ticket structure and control; the target repo defines the what/content of implementation:

- **Engine plane (tempo-owned): HOW work runs.** Ticket schema and closed vocabularies, stage prompt specs, gate discipline, workspace/git rules, the run-record contract. Versioned with the engine, propagated to hosts by bootstrap refresh, never host-edited.
- **Host plane (target-repo-owned): WHAT to build.** Architecture and domain rules, tech stack, design docs, review-rules docs, verification conventions. Never engine-edited. It enters the pipeline AS DATA: rendered into Implement/Review prompts via host-declared context files and review surfaces (section 7), with provenance stamping the engine spec version and the host-doc SHAs in each prompt.

The planes meet in exactly two places: the rendered root AI files (managed `tempo:core` block + project-owned remainder, section 15) and prompt rendering. Conflict rule: the engine plane wins on process, the host plane wins on content -- and "redefines process" is a CLOSED MECHANICAL predicate, never a semantic judgment: the drift lint normalizes the project-owned remainder into paragraphs (Markdown list prefixes stripped, nonblank continuation lines joined -- a rule split across physical lines is ONE paragraph), and a normalized paragraph FAILS the lint exactly when it opens with the subject `tempo` followed by a directive from the closed modal list (must, must not, should, should not, shall, shall not, will, will not, may, may not) or by an imperative from the closed engine-process predicate vocabulary the drift-lint deliverable declares; every other subject and ordinary project rule passes, proven by refusal AND acceptance fixtures (a lint that reddens `The tempo dashboard must use dark mode.` is as broken as one that greens an override). The managed block is fail-closed on its own markers: a duplicated, reordered, or UNTERMINATED `tempo:core` marker -- or stray marker-like text anywhere in the file -- refuses both render and lint (corruption is never green); a routed CLI's file with NO marker and NO marker-like text is FIRST ADOPTION (`19.P6`): the managed block is inserted with every existing byte preserved as project-owned remainder; a missing marker WITH marker-like text present still refuses; repair never erases a project-owned byte, and a second render over a clean file is byte-identical (idempotence, a named test). The renderer manages the conduct file of every CLI the RESOLVED ROUTING names, via section 17's mapping (the `claude` CLI loads CLAUDE.md; any other agent CLI loads AGENTS.md): a routed CLI's file is always rendered, an unrouted CLI's file is never created (an unread copy to drift, section 17) -- never an assumed provider's file. This machinery lands as chained deliverables -- the renderer (carrying the marker and idempotence contract), drift DETECTION (the pure classifier), then gate ACTIVATION -- never one ticket (`19.P6`; it proved unbuildable bundled). Reflexive case (tempo developing tempo): the `tempo:core` render and drift lint run against the TICKET BRANCH's engine version, and the branch's golden snapshot is the authority, so an engine-plane template change ships with its own updated block instead of drift-failing against the running instance's older version. The golden snapshot is REGENERATED by a mechanical render target, never hand-edited: the drift lint compares the committed block against a fresh render of the branch's template, so a snapshot a fresh render does not reproduce fails.

Rule ROUTING is three-way: orchestration-generic -> the engine's core template (propagated by bootstrap refresh); project-specific -> that host's project section; personal machine-wide preference -> the operator's global agent config. Engine rules NEVER go in the personal global config. Why: concurrent instances may run different engine versions, and a global copy of one version's rules would leak into the other's sessions.

## 9. Scheduler, scope fence, merge queue

### 9.1 Continuous dispatch

**Continuous dispatch.** Eligible = `state: confirmed` + all `depends` merged + not quarantined + not parked (provider drought) + not held on an open hardening round (section 11.4) + not a retired hardener (section 11.4) + not awaiting a Reject-queue verdict -- the last four journal-derived, like quarantine (the Reject fold: a stem awaits a verdict when its latest terminal `state_transition` body carries the Reject-routing marker `routed: reject_queue` -- stamped by the spine when it terminates a stem to the queue, section 11 -- with no later `confirm`/`reject` `signal`; the bootstrap drain's auto-keep journals the same signal with a MACHINE actor stamped on its body, section 11.4 -- either actor's signal resolves the awaiting-verdict hold; only the operator's bounds the cap fold (section 11.2); no new run state exists for this). A PLAN-BOUND arrival -- a Reject terminal carrying `plan_units` (section 11.4) -- is never auto-kept; its machine release is that same machine-actor keep, journaled only when a unit in its `plan_units` has changed bytes, and it draws one `retry` unit like the auto-keep. Drought-parked tickets re-enter eligibility automatically at the next dispatch cycle once routing resolves; the OPERATOR'S Reject-queue `keep` re-enqueues the ticket and re-arms its spent failure-spine caps; the bootstrap drain's auto-keep (section 11.4) re-enqueues only -- it DRAWS DOWN remaining budget and NEVER re-arms a spent cap. Sort by (priority, age) -- the age term is the starvation guard; age = time since the journal event of the stem's first ticket-plane `ticket.md` commit -- and EVERY authoring path emits it: human intake (section 13 touchpoint 1), the Author stage, box triage, and phase seeding all journal the same per-stem authoring-commit event, else the starvation guard is silently dead for machine-authored stems -- journal-derived, D3, never a file mtime, with lexicographic stem order as the final deterministic tiebreak, a stem with no event sorting as `None` (never a synthetic now: no event means no seniority); the deliverable that lands the age term also lands its consumer, demoting the stem-name comparison to the final tiebreak in the eligibility sort, proven by a test with DISTINCT per-stem clocks (an equal-clock test cannot discriminate a working sort from a broken one). A file watcher on the tickets dir re-sorts the pending queue on any edit (live re-prioritization). It DEBOUNCES so a half-written ticket is never scheduled, and parses FAIL-CLOSED: a ticket that does not parse holds its last-known-good sort position (a NEW ticket with no parsed state stays out of the queue), and the parse failure is journaled. No preemption of running work (a P0 takes the next free slot).

### 9.2 Dependency liveness

**Dependency liveness.** A dependency edge can die: its predecessor is killed at the Reject queue or abandoned. Terminal-without-merge is an event the scheduler consumes: the daemon journals a dead-dependency event for every confirmed ticket whose `depends` names the dead stem and emits one `failure_report` per dependent into the Suggestion Box (pre-daemon, the Phase 2 `reject` verb emits exactly these -- the dead-dependency events and per-dependent reports -- INLINE at the kill, the handling shipping with its trigger, section 19; the daemon's continuous scan takes over in Phase 3), so stranded work resurfaces as schedulable revision (re-wire, re-scope, or kill) instead of silting. When Rework splits a ticket, it journals a supersedes map (old stem -> successor stems); the scheduler satisfies a dependency on a superseded stem once ALL successors merge, and the terminal-without-merge scan resolves stems THROUGH that map in both directions -- a dead successor also marks the superseded stem dead, so dependents of the ORIGINAL stem get their dead-dependency events too.

### 9.3 Ordering and scope fence

**Ordering and scope metadata.** One author-declared path list must never do two jobs (conflict prediction and scope bounding). Why: path predictions are guesses that over-serialize the queue and miss real collisions. Two separate mechanisms:

- `depends`: explicit predecessor stems, minimal, the only ordering mechanism.
- `## Scope fence`: the implement stage's WRITE ALLOWLIST. After implement, a mechanical gate checks `git diff --name-only` is a subset of the declared prefixes, and a `TEMPO_PLAN.md#<unit id>` entry admits a plan diff only when every changed line lies inside that unit's registry row (the row's YAML mapping lines, section 13); outside edits fail closed, with the `gate_bypass` valve for genuine surprises. AUTHORING law: the fence includes every file the acceptance criteria force -- criteria that compel an out-of-fence edit are an authoring defect, and the correct implement outcome is premise_failed, never a compat shim. The forced set is JUDGED at authoring, never left to run-time discovery: `requisition_review` checks the fence CLOSURE before a ticket commits confirmed -- its prompt directs the reviewer to trace the reference closure of every symbol the scope-in changes (callers and importers, aliases included) and the recorded-value closure of every constant or version it bumps (the artifacts and assertions carrying the old value) -- and rejects a fence omitting any, naming them, so a forced-file gap is caught at authoring time rather than as a runtime premise_failed. The closure is LLM judgment under that review's prompt, never a shipped static analyzer (the engine carries no per-stack reference tooling, section 18); a mechanical closure check is a D10 return at the first fence-gap escape this review misses. Behavioral fan-out that only a run reveals is absorbed at the gate, not by pre-widening: the scope-fence gate auto-admits an out-of-fence edit ONLY when it is confined to a TEST file and a mechanical anti-weakening check passes (no test deleted, skipped, or xfailed; assertion count non-decreasing) with the full suite green -- a TEST file matches the engine-constant patterns (a `tests/` path component, or basename `test_*` / `*_test.*`), and the anti-weakening check is language-aware for PYTHON only, stdlib `ast` over both sides of the diff (the self-host's language; no per-stack tooling accretes, section 18): a file those patterns claim but the parser cannot own FORFEITS auto-admission, fail-closed to the `gate_bypass` valve, never a heuristic pass -- a test edit cannot balloon the shipped surface, and loosening one is separately detectable. Every other out-of-fence edit -- any production file outside the derived closure -- stays fail-closed: a genuine surprise takes the `gate_bypass` valve, and reach with no reference, value, or dependency link to the scope-in is the authoring defect the fence exists to stop. One authored exception is structural, not a defect: a phase-SEEDING ticket (section 19) writes not-yet-authored `ticket.md` files -- the next phase's core batch, or its own phase's `-continue` feature batches -- whose stems are unknowable when its own fence is authored, so its fence is necessarily the whole `tickets` plane -- pre-naming the stems would be speculative seeding (D10). The broad prefix bounds writes to the ticket plane; that a seeding run only CREATES new stems and never rewrites an existing ticket is Review's to confirm, not the fence's. And the whole-plane fence is a WRITE bound ONLY, never review material: `requisition_review`'s material for a seeding ticket is PER AUTHORED SEED -- that seed's own text and Context closure, one render per seed, each under the section 8 bound -- never one union blob over the whole authored set or the plane's history (sections 7, 19).
- Conflicts are RESOLVED at the merge queue, the one place they are real; single-flight dispatch (D2) makes them rare by construction, and the deferred overlap checkpoints (section 18) are the D10 return if a future worker pool starts manufacturing them.

### 9.4 Merge queue

**Merge queue (one serial async task).** The queue admits ready branches in (priority, age) order -- the same sort as dispatch -- and an in-flight admission is never interrupted (a P0 takes the next free slot). Per branch, in order, all BEFORE main moves: RESTORE the branch worktree's ticket plane to main's content (`git restore` of `tickets/**` through git.py -- stage terminals lift-and-unlink ticket-plane artifacts, section 10, so a worktree born from an earlier main carries tracked deletions of the already-lifted copies, and an un-restored tree refuses the rebase) -> rebase onto main -> fast MERGE-SAFETY tier -> full INTEGRATION CHECK -> squash-merge -> delete branch. The MERGE-SAFETY tier is the cheap subset a rebase can invalidate -- rebase-clean, scope fence, run-record present, plus host-designated fast checks (`merge.safety_checks`, section 15) -- run first so an inadmissible candidate is rejected before paying the full run. The INTEGRATION CHECK is the FULL always-run hard set -- every engine-shipped hard gate plus every host mechanical entry with `trigger: always` and `severity: hard` (the host's own test suite belongs in that class; the green-main guarantee is exactly as strong as this set) -- AND the ticket's own `## Verification`, executed against the rebased candidate tree. That tree is content-identical to what the squash-merge will put on main, so checking it pre-admission catches semantic conflicts (green alone, red together) while main stays green by construction: a red candidate is not admitted -- main is untouched, the branch routes back through the failure spine with the integration findings as context (rework with conflict/semantic context, re-review per the risk routing below), and the next branch is admitted immediately. No freeze machinery exists because the failure mode it would manage cannot occur on this path.

### 9.5 Merge-time gate scoping and conflict resolution

Merge-time gate scoping: the re-run set is the MECHANICAL codes only, validating the admission's code diff, not every declared-binary evidence file already on main; integration-shaped checks (the ticket's verification) run against the tree; `correctness_review` participates as the pinned-approval requirement for the admitted SHA, never as a re-executed model call. Auto-admission is additionally GO-COVERED: the review effect's completion stamps the (provider, model) that actually SERVED (section 6), and the queue compares that served identity -- never just the routing config -- to the currently binding GO identity; an approval served off-baseline (a spill or failover landed the review call on an unbaselined candidate, section 6) queues for the operator's `confirm` exactly like supervised merge (section 12), dispatch and other candidates' checks continuing. A green candidate is squash-merged; the pinned review approval carries across a clean rebase (the deliberate, stated exception to approval-SHA strictness -- MERGE-SAFETY plus the INTEGRATION CHECK bound the carry's risk to what mechanical checks can see; the residual, a byte-identical diff whose meaning changed under a moved main, is an accepted risk the section 14 escape attribution measures, and an escape of that shape is the cited incident that would earn re-review-after-rebase per D10). Daemon era only: when no GO baseline currently binds, a green candidate queues for the operator's `confirm` before admission (supervised merge, section 12) while dispatch and review continue -- the bootstrap drain never holds (section 19). The hold itself is a Phase 6 deliverable, built at the first daemon-on-host-work cutover (section 19); through the entire self-build no admission holds, so the Phase 3 soak and every phase-exit seed exercise the merge queue without it, and no self-build seed (Phase 4 provider failover included) may require it. After the merge the queue asserts main's tree hash equals the checked candidate's tree hash; a mismatch is an engine defect and escalates immediately (section 13). One full verification run per admitted ticket is the deliberate serialization cost of a permanently green main. That cost has a stated capacity: admissions per day are bounded by rebase + MERGE-SAFETY + INTEGRATION CHECK wall-clock (a 30-minute suite caps at ~48/day), and the host's always-run hard set must be sized so capacity comfortably exceeds worker throughput. Backpressure is explicit, not emergent: at `scheduler.max_unmerged` completed-but-unmerged branches (section 15; shipped literal default `2` -- v1 has no `workers` key, and the `2 x workers` formula returns WITH the worker pool, section 18) dispatch stops admitting NEW tickets until the merge queue drains. If K consecutive DISTINCT tickets go integration-red (shipped default K=3), that is a systemic signal: merge admissions pause and the red-streak escalation fires (section 13); `resume` re-opens admissions. A conflicted rebase walks a cheapest-first resolution ladder; whatever rung resolves it -- and whenever every rung refuses -- the worktree is NEVER left mid-rebase: a refusal runs `rebase --abort` before it returns (section 10), so the branch the refusal names stays re-runnable:

1. **Mechanical resolution.** git's own merge machinery absorbs non-overlapping edits; on top of it, host-declared per-path strategies (closed vocab: `regenerate: <command>` -- take either side and re-run the generator, for lockfiles and generated fixtures; `union` -- for append-only files). Deterministic and fail-closed: only declared paths get a strategy. Strategy-resolved content is derived output and needs no re-review beyond the post-rebase gates. A generated path has exactly ONE owner: either the ticket's `## Scope fence` (agent regenerates in-branch, the default) or a host-declared merge-time `regenerate:` path -- never both; the scope-fence gate exempts host-declared strategy paths. The same ownership law governs FENCE AUTHORING: a deliverable that hooks, writes, or reads an engine seam fences the module that OWNS that seam -- the run's terminal `state_transition` write and the section 11.2 terminal handler (harvest -> dispatch -> journal -> wipe) are the driver's (`tempo/runner.py`); the stage-terminal OUTBOX lift and stage-evidence gathering are the stage layer's (`tempo/stages.py`), and section 10's "the driver lifts the outbox" means the driver INVOKES that one stage-layer lift path, never a second copy; admission is the merge queue's (`tempo/merge.py`); the eligibility sort is the scheduler's (`tempo/drain.py`); a cap draw is owned by the module that journals its `cap_consumed` event -- never only the module that calls into a seam or the helper that computes for it; `requisition_review` treats an outside-owner hook as a forced-file closure gap and rejects the ticket at authoring. Wherever a deliverable's `expects:` list names a file, the deliverable prose states what that file OWNS and the tests must import and exercise the production module -- a near-empty stub satisfying a file-existence check is the false-green shape this rule exists to refuse. (A rerere replay cache with its custody protocol, and a conflict-only micro-rework mode, are D10 returns at the first chronically re-resolved conflict the conflict facts show -- section 18.)
2. **Full Rework** with conflict context and invalidated approval (re-review) for every conflict the mechanical rung leaves.

Strategy caveat: an auto-resolved path can stay green while silently changing meaning, so retro watches strategy-resolved merges -- a resolution later implicated in an integration-red or a bug ticket revokes that path's strategy declaration.

### 9.6 Cross-ticket overlap

**Cross-ticket overlap: resolved at merge, measured for the future.** Author-time path prediction (fence-vs-fence) stays rejected, and under single-flight dispatch (D2) no second in-flight run exists to overlap. The dispatch-time overlap skip and the pre-review hold -- spend optimizations that only pay off under a concurrent pool -- are DEFERRED with that pool (section 18); their law is recorded here for the return: never act on estimate-vs-estimate; act only when at least one side is an OBSERVED fact. What ships now is the MEASUREMENT: the scope-fence gate already computes `git diff --name-only` per branch, the driver journals each branch's observed changed-file set at implement-terminal, and every merge admission journals its CONFLICT FACTS -- the conflicted file list, the resolving rung (counting strategy hits, because a path that repeatedly needs mechanical resolution is chronically contended even though every merge "succeeded"), and integration-red implicated paths. Those facts are the evidence stream that earns the deferred checkpoints, a dispatch-serialization knob, or a retro-proposed refactor-split back via D10 -- measured contention, never a guess.

### 9.7 Watchdogs

**Watchdogs: liveness and progress are different questions.** Per-stage `asyncio.wait_for` bounded by exactly the ticket's stuck budget; the guard against an expected==stuck authoring mistake is the section 13.3 grammar rule that `stuck` exceeds `expected`, never a runtime floor; process-group kill for agent subprocesses. A heartbeat answers only "is the run alive?" -- the wasted hours live in busy-but-spinning runs -- so the watchdog also runs SPIRAL detection over the agent adapter's event stream. The adapter CONTRACT requires one JSONL event per tool call from any integrated agent CLI, recorded to the run's attempt spool (section 6). Three time regions per ticket size class: healthy (below expected x 1.5) -- log, do nothing; soft band (expected x 1.5 up to stuck) -- notify on the signal; stuck -- the hard timeout path. v1 ships ONE cheap deterministic signal, no model calls ("declared output file" = any path matching the ticket's `## Scope fence` prefixes):

- **Spend-without-progress:** token/cost burn since the last mutation to a declared output file crosses a threshold -- an ENGINE CONSTANT, never a new config knob: spend since that mutation exceeding 3 x the serving row's per-call cost basis (metered usage, else `limits.est_cost_per_call_usd`, section 6) -- the shape-blind signal that catches a thrasher whatever its thrash looks like (debris accumulation, command repetition, poll loops, or file-diverse spinning alike burn spend without moving a declared output).

Additional signal SHAPES -- throwaway-file accumulation, tool-call repetition, mutation-entropy stall, poll-loop match -- and a degraded-watch mode for a stream-less CLI are D10 returns, each earned by a pinned spiral fixture the shipped signal misses (section 18). The cross-ATTEMPT analog -- the same terminal reason repeating across attempts -- is the failure spine's identical-terminal short-circuit (section 11.4), not a watchdog signal.

The signal tripping in the soft band pushes ONE notification per run (the notify key carries the run sequence, section 6: re-tripping within a run never re-pages, a NEW run's spiral pages again). NO AUTO-KILL: the human decides -- kill (`tempo kill <run>`, SIGKILL of the run's process group, section 16), inject guidance (edit the ticket THEN kill: attempt renders are immutable per invariant 2, so an edit reaches the next attempt's render, never a running one), or let it cook. Every observed spiral becomes a permanent detector fixture (the recorded event stream from the run's pinned attempt spool, section 6, plus the expected verdict), so the signal set is regression-tested against a corpus and each deferred signal shape returns citing the fixture that earned it; an LLM-as-judge signal stays unbuilt until a spiral appears that no cheap pattern can express. The bottom layer is a dumb external heartbeat (systemd timer / cron) checking the daemon's heartbeat file -- touched by the main loop each cycle ONLY while its watched core tasks (workers, merge queue, box consumer, watcher) are live, so a dead or wedged core task stops the touch and the mtime check catches it; unrelated to the reserved journal `checkpoint` event (D2). No LLM in the bottom layer. One kill-switch flag pauses all workers at the next safe checkpoint.

## 10. Git and GitHub

### 10.1 Main line and worktrees

- Local `main` is the blessed line. Branch per ticket in a worktree off one shared `.git`, created at `<worktree_root>/<stem>` under the config-declared `worktree_root` (section 15; shipped default `<state_dir>/worktrees` -- instance-local and gitignored with the state dir; the stem names the worktree dir exactly as it names the branch). The merge queue SQUASH-merges locally and deletes the branch: main history is one commit per ticket (per-ticket bisect granularity, no agent WIP-commit noise); intra-ticket history is disposable, the durable detail lives in the journal and run record. The branch name is the ticket STEM verbatim -- no `ticket/` or other prefix (section 13: the one identity reused as dir name, branch name, and squash trailer). Each squash commit's subject is `tempo(<stem>): <ticket Goal line>` and it records `tempo-ticket: <stem>` and `tempo-reviewed-sha: <sha>` trailers, so reconcile and dependency-eligibility can map an integrated commit back to its ticket and approved SHA after the branch is deleted -- the identity D3's world-wins reconcile needs. No PRs, no `gh`, no PR-state failure modes in the loop.

### 10.2 Admission lanes

- **Two admission lanes, one writer.** The serial merge task is the single writer to main; everything reaching main goes through one of two lanes: (a) squash-merges of reviewed ticket branches -- code; (b) TICKET-PLANE COMMITS -- mechanical, path-fenced to `tickets/**` -- by which stages persist process artifacts directly: authored tickets (Author, box triage, human intake), run records, check reports (`checks.json`), review verdicts, harvest `attempts/` dirs, evidence copies, decision records, retro reports (section 14). Ticket-plane commits need no branch and no review (process record, not code); a ticket-plane commit touching anything outside `tickets/**` is refused, with one named exception: the rendered root AI files of the `tempo:core` bootstrap refresh (section 15), a deterministic drift-linted render the daemon commits mechanically after an engine upgrade. (Seed `ticket.md` files reach main through this ticket-plane lane; their REFUSABLE review point is the seeding ticket's own Check stage, section 19 -- a refused seed never commits, so no code-lane exception exists or is needed for `tickets/**`.) The lane's writer validates what it commits: schema checks for artifacts it knows; evidence files are declared binary data and exempt. Failed-attempt artifacts and the tickets of runs that never merge reach durable history through this lane -- custody never depends on a ticket succeeding. Ticket-plane commits interleave with code admissions in the same serial task, so "single writer to main" stays true. Custody at the worktree seam: the agent's own ticket dir (`tickets/<stem>/`) inside its worktree is its OUTBOX -- run record, `checks.json`, box-message files, evidence -- and the scope-fence gate structurally exempts `tickets/<stem>/**` EXCEPT `ticket.md`, which stays read-only to the agent (the no-self-editing rule, section 11). At every stage terminal the driver lifts the outbox into the canonical tickets dir and commits it via this lane -- ONE ticket-plane commit per stage terminal, subject `tempo(<stem>): <artifact-kind>` (e.g. `run-record`, `review`, `checks`), no trailers (trailers identify the CODE line, not the process record) -- ingesting box-message files into the Suggestion Box. Outbox files are scooped from the worktree FILESYSTEM and never committed to the ticket branch: the branch carries code only, so no rebase ever replays a `tickets/**` commit against the already-lifted copies on main, and a branch that does carry one fails MERGE-SAFETY with the paved road "leave outbox files uncommitted; the driver lifts them". The scope-fence gate reads the branch's committed diff; uncommitted worktree debris is watchdog territory (signal 1), wiped with the worktree. The squash-merge applies the CODE diff only; `tickets/**` never rides the code lane.

### 10.3 Review and GitHub sidecar

- Review happens on the local diff (`git diff main...<stem>`); the verdict is a review artifact pinned to the reviewed SHA, persisted via the ticket-plane lane to `tickets/<stem>/review.md`; the merge gate requires an approval for the SHA it admits (carried across a clean rebase per section 9).
- GitHub is a checkpoint sidecar: a mechanical Effect pushes main (and optionally a mirror) after every K merges (shipped default K=5) or daily. It can never block the loop; push failure is a soft finding to the Suggestion Box, and a FAILED push never marks its trigger window done -- the effect re-fires at the next trigger until a push lands, because an attempt that records itself as done on failure silently ends the offsite copy. The sidecar ships with the Phase 3 daemon and its Timer (section 19): the sole offsite copy of an unattended multi-phase build does not wait for the sidecar phase.

### 10.4 The git wrapper

- All git access goes through one `git.py` wrapper -- argv lists, dir-pinned (`["git", "-C", dir, ...]`), constructed with a REQUIRED keyword-only child `env` (never a `None` default: the process seam consumes a mapping, and section 6's key-injection rule needs every caller to state what a child inherits), executed through the process-exec seam as ASYNC subprocesses so nothing blocks the event loop (D2). The Phase-1 op set -- the ONE canonical enumeration, which prompt 5 (section 0) defers to so the list cannot drift from its consumers -- is `init`, `status --porcelain`, `rev-parse`, `diff --name-only <base>...<stem>` and `diff <base>...<stem>` (the scope-fence gate and Review both read the branch diff -- this section and section 9), `add`, `commit`, `branch`, `worktree add`/`remove`/`prune`, `rebase` with its `rebase --abort` dual (a refused conflicted rebase is ABORTED before the refusal returns, so the worktree sits on its own branch head -- a refusal that leaves a rebase in progress strands the very branch it tells the operator to re-run), `restore` (the merge admission's ticket-plane restore, section 9), `merge --squash`, and `describe --tags --always --dirty` (diagnostics-only: it derives the section-6 lockfile `instance_id` for an untagged dev instance, enumerated here so the D1 all-git-through-`git.py` law holds). The richer merge-queue resolution ops a conflict needs (per-path merge strategies) are Phase 3 (section 9); rerere is a D10 return (section 18). Worktree cleanup uses `git worktree remove` + `prune`, never bare `rm -rf` (orphaned-metadata lesson). An orphan sweeper reconciles worktrees at daemon startup.

## 11. Failure spine

Uniform routing on the outcome envelope; the recovery path is the mechanical sandwich:

### 11.1 Caps

**Caps fire first, before any model call.** Max diagnosis invocations per ticket (shipped default 6); max retry-like actions (shipped default 6, never set above the diagnosis cap); max premise bounces (default 2 -- Author<->Implement round-trips on `premise_failed`, invariant 4); max hardening rounds per lineage (`hardening`, shipped default 3 -- one unit drawn per HELD spec-gap terminal, checked only at a spec-gap terminal's dispatch and never at eligibility, section 11.4); max infra terminals (shipped default 6 -- `infra_error` and `timeout` draw this budget, so a crashing review call or a hung subprocess is bounded exactly like a model failure, never an unbounded park-and-retry loop the retry cap cannot see; a provider failure from a call that RAN lands here by construction; a drought -- no callable candidate, pre-dispatch or discovered mid-run -- parks cost-free instead (section 6)). Every non-ok terminal draws from one of these NAMED budgets -- `gate_failed` and in-stage re-prompts from retry, `premise_failed` from premise_bounce, `infra_error`/`timeout` from infra, a held spec-gap terminal from hardening -- so no failure class can loop outside the cap system. The terminals that draw nothing re-run only through a bounded release: the drought-reason `infra_error`, bounded by its routing-resolves re-entry gate and the fleet-wide drought escalation (sections 6, 13); `budget_exceeded`, bounded by the ceiling's pre-call check (section 11.3); the over-bound render park and the premise park, released only by a ticket content change (section 18); and the plan-bound spec-gap Reject terminal, released only by a draw-bearing machine keep on a changed unit or by the operator's bounded keep (section 11.4). Three refusal events map onto the vocabulary explicitly, each a `gate_failed` drawing retry: a review REJECT verdict (the review surface is a gate, invariant 3); a conflicted rebase every live resolution rung refuses (pre-Rework, section 19 -- the abort leaves the branch re-runnable, section 10); and an EMPTY COMMITTED DIFF at the verification gate, unless Implement proved `already_satisfied` (invariant 4) -- the one sanctioned empty-diff settlement. The diagnosis and retry defaults are 6, not 3, because the escalation ladder (section 11.4) draws a cap unit per rung: a hard ticket climbs tier-then-effort AND must still have budget to LAND at the top rung, so a cap that only covered the climb would RMA every hard stem to a human. These spine caps are config-declared (the `caps:` key, section 15) with the shipped defaults above, checked deterministically BEFORE any model call -- never model-decided -- present from the Phase 0-2 failure spine and distinct from the Phase 3 THRESH spend and rate budgets (section 6). The retry cap counts BOTH retry senses (section 2): in-stage re-prompts and cross-attempt retries draw down the one budget, and that bound exists from Phase 0 with the driver. ONE case has no lineage to fold: an AUTHORING-time in-stage re-prompt -- the Author driver's re-prompt on a `requisition_review` snag, run BEFORE any `ticket.md` is committed and so before any stem or lineage exists (section 11.2 assigns the lineage budget at the first ticket-plane commit) -- is bounded by the DRIVER'S LOCAL per-invocation allowance sized from `caps.retry`, never by the journal cap fold (the section 11.2 in-memory ban governs LIFETIME lineage budgets; pre-lineage there is nothing durable to fold), and an exhausted authoring allowance terminates the authoring pass with the cap NAMED; the lineage fold governs the committed stem's in-stage re-prompts and cross-attempt retries from its first ticket-plane commit on. Because each cross-attempt retry rides a FRESH diagnosis (the sandwich's rung order), the retry cap is never set above the diagnosis cap: a retry the diagnosis budget cannot cover is a diagnosis-STARVED re-run -- it repeats a failure with only the raw prior findings and no new steering, at full model spend -- so a stem whose diagnosis budget is spent escalates to the Reject queue rather than drawing another retry. If any cap is spent -- `hardening` excepted, which routes only the next spec-gap terminal (section 11.4) -- terminate to the human Reject queue without invoking the model -- and the recorded reason NAMES the spent cap verbatim ("diagnosis cap spent", "infra cap spent"), never a generic verdict-shape message that sends an operator chasing a phantom model failure. (The Reject queue itself arrives with the Phase 2 spine; in Phase 0-1, before it exists, a spent cap or any non-ok terminal just journals its state transition and exits nonzero, leaving ticket and branch in place -- `19.P1`. The exit contract holds for FAULTS too: nothing reaches the operator as a raw traceback. A fault outside the stage vocabulary still exits as a named, stopped verb -- an unresolvable binary surfaces as the section 15 seam's config/setup refusal, and a key left mid-effect by a dead run (an intent with no completion) is reported with the key and the run that stranded it; the orphan's DISPOSITION is fixed -- restart-reconcile (Phase 3) reaps it to `abandoned` exactly like reconcile-on-entry (section 11.2) -- never a silent retry and never a traceback.)

### 11.2 Auto-harvest and the terminal handler

**Auto-harvest before wipe -- on EVERY non-ok terminal.** The driver's terminal handler order is fixed: harvest -> dispatch -> journal -> wipe worktree, where dispatch is the section 11.3-11.4 recovery (the one diagnosis call and its cap draw, then the deterministic routing decision) and journal writes the run's SINGLE terminal `state_transition` carrying any `routed: reject_queue` marker the routing set (section 6). Dispatch PRECEDES the terminal write for a load-bearing reason: the routing predicate reads the FINAL cap state -- including this recovery's own diagnosis draw -- so the marker rides the terminal it belongs on; a terminal journaled before dispatch cannot carry its own routing. Harvest still precedes both (the diagnosis call reads the harvested material) and the wipe still comes last: no worktree is wiped until after its terminal is journaled, and a crash mid-dispatch leaves a `running` with no terminal -- reconcile's orphan reap, below. The section 9 ownership-law parenthetical states this same order -- the two statements never diverge. Killed, stalled, and timed-out runs route through the same handler; orphan worktrees found at restart are harvested before the sweeper removes them. Before the Phase 3 daemon exists, the same reconcile runs ON-ENTRY: a `run`/`drain` scaffold, holding the sole writer lock, reaps any orphaned in-flight run -- a `running` with no terminal, an `effect_intent` with no completion -- the moment it becomes the writer, journaling an `abandoned` terminal and removing the orphan worktree (Phase 1 reaps bare; Phase 2 folds in this harvest; Phase 3 moves it to daemon startup). The `abandoned` terminal a reap journals is what FREES the stem: keys are run-scoped (section 6), so the next entry takes the next run sequence and a fresh keyspace, and teardown-and-create clears the dead run's worktree and branch. An `abandoned` or non-ok stem therefore stays ELIGIBLE, and its re-run is real work -- never a replay of the recorded failure, never a removed worktree's path handed to a stage. Re-authoring under a fresh stem is DEAD as a recovery path for a LIVE lineage: a rename is never machinery, and no rename or re-author resets caps (the lineage law below). The sanctioned retirement for ANY confirmed lineage that will never merge (obsoleted or superseded seed included) is the section 13 `reject <stem>` kill -- journal-identity resolution, `rejected` stamped, section 9's dead-dependency handling fired (it resolves THROUGH the supersedes map, both directions), caps and history preserved -- never a drain special case keyed to stem names. Distinct and SANCTIONED after that retirement: SUCCESSION -- a FRESH REVIEWED same-goal stem, narrowed or split, on a fresh branch never the rejected candidate's, authored via touchpoint 1 intake (the operator its reviewer, section 13) or a reviewed machine authoring, free to cite the rejected lineage's committed Check and review artifacts (`tickets/<old-stem>/checks.json`, `review.md` -- durable ticket-plane history, section 10) as Context evidence. A retirement WITH a named successor journals the SAME supersedes-map entry a Rework split does (old stem -> successor stems, section 9), so every dependent -- the phase exit's transitive reads included (section 19) -- resolves through the map instead of stranding on a dead edge; only a successor-less kill leaves dependents to the bare dead-dependency events. UNATTENDED AUTHORITY: when an immutable stem parks or terminally rejects SOLELY because a base-reproducible plan, engine, or test defect made its unchanged contract impossible, the recovery chain itself performs the state-only `reject` plus succession once the fix is committed and green -- this pair rides section 19's answer-a-Reject-item touchpoint, never a new authority -- and the SAME root-cause defect sanctions it ONCE: recurrence is an operator boundary, as is every park with any other cause. The cost model is stated, not hidden: under drain's park-on-red run-to-quiescence loop (section 18) and deterministic sort, a persistently red stem is re-attempted findings-fed at real model spend, down to its remaining `retry` cap, while the rest of the queue proceeds. The cap is a LIFETIME budget per stem, never per-invocation: every draw journals a `cap_consumed` event NAMING ITS CAP and recording the `ticket_sha` its run was dispatched against (section 6) -- `ticket_sha` is the git BLOB SHA of the committed `tickets/<stem>/ticket.md` content (hash-object identity, never a commit id: run-record and review ticket-plane commits move every commit id but not the ticket's blob, so only a content edit moves it) -- and each cap's remaining budget is derived at entry by the ONE journal cap fold, which counts the lineage's `cap_consumed` events that name THAT cap -- a fold that ignores the cap name draws every cap down against one budget, a single-cap assumption the next cap in the vocabulary (section 11.1) silently breaks; an in-memory counter is wrong by construction, because it forgets across invocations and across the drain's self-upgrade re-exec. Cap budgets are scoped to the STEM'S LINEAGE, assigned once at the stem's first ticket-plane `ticket.md` commit (intake or authoring, the same event section 9's age term reads) and invariant across every edit and rename: the cap fold counts every `cap_consumed` naming that cap across ALL ticket shas of the lineage, and `ticket_sha` is recorded on each draw for audit and attribution only -- it NEVER selects, clears, or refills a budget. There is no content-edit cap reset: an edit reaches the next attempt's render (an edited ticket is new work for the IMPLEMENTER, not a new budget), and the ONE re-arm is the operator's Reject-queue `keep` (`confirm <stem>`, sections 9 and 13): its journal `signal` also bounds the fold, so draws before the stem's latest OPERATOR-actor keep fall out of the count -- the auto-keep's machine-actor signal (section 11.4) resolves the verdict hold and NEVER bounds the fold, and the double-confirm refusal (section 13 touchpoint 3) keeps that re-arm finite -- and a draw recorded with NO `ticket_sha` counts against the current budget so a journal written before this mechanism never reads as refilled. That lineage-scoped count is the single cap fold, defined in the journal beside the run-state vocabulary it reads (section 6); a cap-spent stem stays parked and reported, never silently re-armed; the drain's provisional-quiescence re-offer fold, on discovering a spent cap, RECORDS the releasable Reject-queue arrival before returning exactly as the run-terminal path does (section 9's marker -- answered by the section 11.4 auto-keep only at remaining budget, by the operator's verdict otherwise), so no lineage is left releaseless, and a cap-spent stop never advertises `drain` as its own release (the section 0 prompt-11 drain text's "stays parked ... never re-offered" governs re-DISPATCH only, never the arrival record). Drain is an operator action until the Phase 3 daemon, and the Phase 2 escalation ladder is what routes a repeat offender onward -- until it lands, the journal-derived cap itself is the bound, never the operator's finger. Harvest is a fail-closed ALLOWLIST extraction (never "everything except the diff"): the run record if written; structured gate findings and the StageResult; the stage's terminal REASON when the outcome carries no findings (for `invalid_artifact`, the schema-validation error string the driver logged); a capped stage-log tail and a capped adapter event-stream tail (both cut from the attempt spool, section 6); `git diff --stat` (names and counts, never content); wall time, cost, attempt number. Every non-ok terminal NAMES where its detail lives -- the `attempts/<n>/` dir once harvest exists, the engine log path before then (Phase 1). The unreviewed code diff never enters the ticket spec. Attempt artifacts land in `tickets/<stem>/attempts/<n>/` (ticket-plane lane, section 10; `<n>` IS the section 6 run sequence -- the same number that keys the run's effects and names its spool dir -- so cross-run re-entries never collide); the renderer for attempt N+1 automatically includes a "Prior attempts (informational, unverified, not reviewed)" section built from them -- BOUNDED, so history never grows a render into the section 8 bound on exactly the lineages with the most history: once an attempt's harvest has a journaled diagnosis `lessons` (section 11.3), every later render carries that attempt's typed lessons and terminal reason (the section 11.4 comparison key), NEVER the raw harvest payload, regardless of that payload's individual size -- cumulative accretion, not any one payload, is what crosses the bound; raw harvests stay durable at `attempts/<n>/` for the operator and the diagnosis call, and only a not-yet-diagnosed attempt's harvest renders raw. -- the ticket file itself is never edited, so acceptance criteria and predictions cannot be clobbered, and nothing depends on a human running a harvest verb. POSITION is part of this contract: the prior terminal's UNRESOLVED findings and their paved roads render as an itemized clear-these-findings block in the SAME prompt position as the acceptance criteria -- criteria-position is what a re-prompted implementer acts on; an appendix is what it skims -- marked attempt-scoped and rendered fresh each attempt, never written into `ticket.md` (findings accreted into criteria harden stale attempt guidance into permanent acceptance and eventually contradict the work). The section has one SENIOR source that predates harvest: the prior run's terminal findings artifacts -- review.md's reject findings, a failing checks.json -- already durable in the canonical ticket dir when the stem re-enters (ticket-plane lane, section 10). The re-entry renderer folds them into this same prior-attempts section, untrusted data like all of it, with no harvest, no diagnosis call, and no new bookkeeping: the run-entry fold that derives the run sequence (section 6) already reads the prior terminal event, so the renderer knows a prior run ended and how. This REJECT-FINDINGS RE-ENTRY ships WITH the Phase 1 drain verb (section 19; realized as the section 0 prompt 11-12 pair, both landing before any seeded queue runs) and depends on run-scoped keys alone, so no drain over a seeded queue is ever findings-blind. Harvest EXTENDS the same rendered section with the material only the dying worktree holds -- one rendering seam, never a second path. Harvest is soft: a harvest error journals a finding and dispatch proceeds. Setup-death short-circuit: a workspace that died before doing anything skips harvest.

### 11.3 Diagnosis

**One diagnosis LLM call.** Context: state, ticket, run record if any, capped log tail, capped diff. One question: what should happen next? Verdict vocabulary is closed (retry | escalate | split | reject | abandon-human); a verdict outside the set is treated FAIL-CLOSED as a mechanical RMA to the Reject queue, never a silent retry. `retry` re-runs at the SAME capability (a fixable oversight the findings now steer); `escalate` says more CAPABILITY would help and hands the knob to the deterministic ladder below -- the model RECOMMENDS escalation, it never picks its own tier or effort. The verdict includes a `lessons` field, journaled and rendered as the harvest's form in every later attempt's prior-attempts section (section 11.2) -- diagnosed raw payloads never re-render; no extra model call. Lessons are bounded to the record: each restates a finding the terminal carries and cites the plan text or merged code that answers it, never naming a record, field, constant, or mechanism neither states; a fact neither states is a `spec_gap` (section 11.4). Synthetic short-circuit: if the workspace is gone, write `abandon-human` mechanically and never ask. `budget_exceeded` is the second mechanical short-circuit: the exhausted ceiling blocks the diagnosis call itself (section 2's pointless-call rule), so the stem parks like a provider drought -- no cap consumed, no Reject routing -- and re-enters eligibility at the first dispatch cycle whose pre-call check clears (window rolled or ceiling raised, section 6). An over-bound render refusal (section 8) is the third mechanical short-circuit: the ticket's text against the bound is arithmetic -- a ticket-text defect, never implementer failure -- so NO diagnosis call, NO cap consumed, and the stem PARKS like `premise_failed` (section 18 -- released only by a ticket-plane content change that shrinks or splits it) until Rework is live, then dispatches mechanically to Rework `split`; section 11.4's pre-Rework fail-closed split-to-Reject routing (and section 19's restatement) governs diagnosis verdicts on IMPLEMENTED attempts only -- a confirmed ticket never terminally rejects at zero attempts over prompt size. A retry starts from the MERGE BASE in a fresh worktree (section 11.2), so a lesson never tells the next attempt to keep or build on a prior diff; lessons restate the work that must exist, and when a finding offered more than one paved road the lesson re-checks each before naming one as missing.

### 11.4 Deterministic dispatch and the capability ladder

**Deterministic dispatch.** A match on the verdict string: `retry` re-runs at the authored capability; `escalate` walks the CAPABILITY LADDER one rung, deterministically and MODEL-FIRST -- raise `agent_tier` to the next tier that resolves a DIFFERENT model (a bump resolving the SAME model, e.g. `high`->`max` where the config shares them, is SKIPPED so no attempt is wasted on a no-op), and once no higher model exists raise `agent_effort` one step toward `max` -- the RUNG is journal-derived, never a ticket edit: each escalate dispatch records its rung on the body of the `cap_consumed` event its retry draw already journals, scoped to the stem's LINEAGE like the caps (folded at entry, section 11.2), dispatch resolves the effective (tier, effort) as authored frontmatter plus the folded rungs, and `ticket.md` is NEVER edited to escalate (an edit changes neither budgets nor rungs; the operator's Reject-queue `keep` is the one event that resets both, with the spent spine caps of section 13 touchpoint 3; frontmatter is the STARTING capability, section 13); Rework may choose split (Rework is a Phase 3 deliverable, landing with the merge queue whose resolution rung 2 it is -- section 19; until it lands, a `split` verdict dispatches fail-closed to the Reject queue like a spent ladder, and in Phase 1, before that queue exists, the stem just terminals non-ok -- never a silent retry); RMA to the Reject queue only when the ladder is EXHAUSTED (top model at `max` effort). Model first because the model is the dominant capability lever and nothing sits above the top one, so max effort on that model is the final rung. The failure-spine caps (section 11.1) are sized to cover the full ladder, so a stem can climb every rung before RMA; a longer model ladder raises the caps to match. Oscillation detection: consecutive snag lists alternating the same findings short-circuit to Reject, and so does a retry that comes back STILL failing the same gate code it was dispatched to clear -- returning to the identical wall is not findings-fed progress at the CURRENT capability, so it climbs the capability ladder above instead of re-running at the same rung; only when the ladder is EXHAUSTED (top model, `max` effort) does the identical wall short-circuit to Reject. The rule generalizes across the WHOLE terminal vocabulary, not just gate codes: K consecutive attempts ending with an identical terminal reason (shipped K=3) -- the same gate code, the same `infra_error` reason string, the same timeout (EXCEPT a drought reason -- open breaker or exhausted tier -- which drought-parks cost-free per section 6, never laddering or Rejecting) -- short-circuit past further same-rung retries (to the ladder while rungs remain, to park/Reject when none do) regardless of remaining budget, because repeating an identical wall is never progress whatever the wall is made of. Only `authoring_error` walls climb the ladder: more capability cannot supply a fact the spec omits. Non-converging authoring review is an `authoring_error` wall, bounded by `retry` and the identical-wall rule above.

**Spec-gap hardening.** TERMS: an ENTRY UNIT id is `19.P<n>.<row>` for a non-exit row of phase `n`'s registry; it names the ROW (section 13 resolves it to the row's YAML mapping), so a unit is never a heading and never absent while its row exists. A UNIT SHA is the sha256 of the row's resolved bytes on main (the `Plan contract` resolver, section 13), or the literal `absent` when no such row exists -- one helper, one definition; rows are never added by hardening (GRAIN LAW, `19.L`), so a gap naming an absent row is plan-bound at once. A ticket's HARDENABLE UNITS are its own row's entry unit when its stem is a non-exit registry row, plus every entry unit its `## Plan contract` cites, plus -- for a ticket citing a phase unit `19.P<n>` (only seeding and exit tickets do, section 13) -- the entry unit of every row of that phase's registry, so a seeding ticket can name a thin payload or lookahead row. A SPEC GAP is a hardenable unit of the judged ticket whose row lacks a SPEC DEPTH field (section 19) or omits or contradicts (with merged code or other plan text) a fact the work needs; a complete row can still gap. A FENCE GAP is the spec gap whose fix is a path the row's `fence` omits (rule 5's caller closure is the usual source): its finding carries `fence_add: [paths]`, which the hardener applies to the row's `fence` as data.

DETECTION reads codes and records, never prose. (a) Seeding Check: the seed path's mechanical SPEC DEPTH check runs over every REQUIRED unit of each seed (its own row's entry unit and every entry unit it cites, never the wider phase rows a phase citation makes hardenable) before grammar and before any review call (`19.L`), so a missing field is a spec gap, never an unresolvable-id refusal; the review call's findings carry `kind` and `unit` (section 7). A seeding run whose Check ends `gate_failed` is a SPEC-GAP TERMINAL iff that run's own Check StageResult holds a snag finding with `kind: spec_gap` and a non-null `unit`. (b) Implement: a `premise_failed` StageResult is a spec-gap terminal iff a `premise` finding carries `kind: spec_gap` and a valid `unit`. Implement's discipline before such a finding: it reads the cited row on current main -- a row that STATES the fact while the ticket's fence, Context, or criteria are what is wrong is `kind: authoring_error`, never `spec_gap`; it confirms no in-fence route, including every alternative paved road a prior finding offered, satisfies the criteria before calling anything a fence wall; and one `premise_failed` names EVERY gapped row it can see among the ticket's hardenable units, never the first alone, so one bounce files one round. Excluded, routed as any other terminal: a `spec_gap` finding with `unit: null`, and every finding of a stem a `hardening_round` record names as its hardener. Mixed findings take the spec-gap path; the others render on the re-run.

DISPATCH at a spec-gap terminal is the terminal handler's dispatch step (section 11.2): no diagnosis call, no `retry` or `premise_bounce` draw. Let G be the terminal's gapped units; P the stem's latest prior spec-gap terminal in its lineage; and D the units of P's `plan_units` that P's round covered and whose current unit sha still equals the recorded one. (1) G within D: a PLAN-BOUND Reject with reason `spec_gap_unresolved` -- a hardener already had these units and left them unchanged, and another round cannot settle that disagreement. (2) Else, `hardening` spent: a plan-bound Reject with reason `hardening cap spent`. (3) Else draw one `hardening` unit, then JOIN the open round if one exists, or FILE round N covering G minus D. Neither (1) nor (2) draws. At most ONE round is open engine-wide: a later round is filed only after re-detection against the post-merge plan.

RECORDS, each written by the terminal handler. FILING commits the hardening ticket `tickets/plan-gap-<N>/ticket.md` through the ticket-plane Effect keyed `ticket-plane/hardening/<N>` (run-blind: once per round across runs, so a reaped filer's re-run replays the completed commit), then journals the ordinary `ticket_intake` signal (section 9's age term) and the `signal` `hardening_round` with envelope `ticket` the hardener and body `{round: N, units: {<unit>: <unit sha>}, filed_by: <stem>, gaps: [<finding>]}`, built from the Effect's completion result. `N` is one plus the count of prior `hardening_round` records. The hardener's identity is this record; its stem name is an operator label only. The HELD terminal body is `{to, stage, reason: spec_gap, dispatch: spec_gap_hold, round: N, plan_units}`; a PLAN-BOUND terminal body is `{to, stage, reason: spec_gap_unresolved | hardening cap spent, dispatch: reject_queue, routed: reject_queue, plan_units}`. `plan_units` maps the stem's own gapped units to their unit shas at this terminal; `to` stays the producing outcome (`gate_failed` from a seeding Check, `premise_failed` from Implement). The `hardening` draw is the ordinary `cap_consumed` record naming its cap.

ROUND STATE is closed -- `open | closed` -- and is ONE fold, used by both the terminal handler (join or file) and the eligibility fold (hold or release), over the hardener's terminals journaled after its `hardening_round` record; the first closing terminal closes the round forever. CLOSING: `merged`, `already_satisfied`, `rejected`, `premise_failed` (the hardener's engine-composed ticket is never edited, so its premise park would never release), and any terminal carrying `routed: reject_queue`. OPEN: no terminal yet, `running`, `gate_failed`, `invalid_artifact`, `timeout`, or `infra_error` without `routed` (the hardener re-runs within its own caps, which end in `routed`), `abandoned` (reaped; it re-runs on fresh keys), and `budget_exceeded` (a cost-free park). Every run state (section 6) maps; a new run state fails the build until mapped.

THE HARDENING TICKET is engine-composed from its record -- no Author call, and exempt from `requisition_review` because no model authors its text: `state: confirmed`, `source: seed`, `kind: chore`, the filer's `priority`, starting `high/high` when any gapped row is KNOWN-DEEP and `medium/medium` otherwise, the ladder climbing from there; its `## Plan contract` cites `19.L`, each gapped unit's phase unit, each gapped unit that exists, and each gapped row's `cite`; its `## Scope fence` is one `TEMPO_PLAN.md#<unit>` per unit in the round, each admitting edits to that row's YAML mapping only (section 9.3); its criteria render each gap finding as data -- row `<u>` states the needed fact in its `owner`, `records`, `observable`, or `tests` field, consistent with merged code, and carries every `fence_add` path in its `fence` -- plus plan lint green (size caps included), and its `## Verification` runs plan lint. A fact that will not fit under the row cap is a SPLIT -- two rows, a plan edit under the recovery order -- so the hardener terminates `premise_failed` naming the cap, which closes the round and makes the stem's arrival plan-bound. It runs the ordinary Implement, Review, and merge path, and its `run.md` records every choice it made. `already_satisfied` is a legal outcome (the fact is judged already stated) and closes the round.

HOLD AND RELEASE (section 9). HELD: the latest terminal carries `dispatch: spec_gap_hold` and its round is open -- ineligible, reported as waiting on that round's hardener. RELEASED: its round is closed -- eligible; the re-run draws nothing further (the hold drew `hardening`), and its re-entry render's prior-attempts section (section 11.2) names the round, the hardener's terminal `to`, and the hardener's `run.md`; a `spec_gap_hold` terminal without `round` reads as released (versioning policy, section 5). PLAN-BOUND: in the Reject queue, released by the operator's `confirm` or `reject`, or mechanically when a unit in `plan_units` has a current unit sha different from the recorded one -- the dispatching single writer (the drain pre-cutover, the daemon after) then journals the machine-actor keep and draws one `retry` unit; with `retry` spent the mechanical release does not fire and the item is the Reject item whose retry budget is spent. A RETIRED HARDENER -- the hardener of a closed round -- is never dispatched again, auto-keep included, and its later terminals never reopen the round; the operator's `reject` clears its Reject entry.

TERMINATION: held re-runs of a lineage are bounded by `caps.hardening`; plan-bound re-runs by `caps.retry` plus the operator's keeps, which the double-confirm refusal bounds (section 13); each hardener by its own spine caps; rounds never reopen, and a hardener never files a round. EXHAUSTION: a plan-bound arrival is a terminal-without-merge and the drain proceeds; its quiescence report names the stem, its `reason`, each unit in `plan_units`, the rounds that covered them with their hardeners' terminals, and the continuing step -- fix `<unit>` in TEMPO_PLAN.md, commit it, then `tempo drain` -- because the unit's changed bytes release the item with no `confirm` (section 19 recovery order).

**Pre-daemon default verdict:** the bootstrap drain runs headless -- zero human input (section 19), and supervised-merge holds bind the daemon era, never the bootstrap drain (section 12) -- so no hold waits on a human. A Reject-queue arrival -- a plan-bound spec-gap arrival excepted, whose inputs are unchanged until its release condition holds (above), so re-asking is pointless (section 2) -- whose `retry` cap still holds budget auto-resolves to `keep` (one findings-fed re-entry, one unit drawn -- DRAW-DOWN only: its machine-actor signal never bounds the section 11.2 cap fold and never re-arms a spent cap; re-arm is the operator's manual keep alone, bounded by the double-confirm refusal, section 13 touchpoint 3) inside the same drain; at spent budget the stem reaches a terminal-without-merge and the drain proceeds to the rest of the queue -- never a park for a verdict no headless run can deliver. The caps bound the spend; `confirm` and `reject` are the daemon-era operator surface, present only when an operator is watching, never a stop in the bootstrap drain. A findings-fed retry earns its next attempt only by CLEARING the finding it was handed, never by trading it for a new evasion of the same code -- that distinction is what stops a hard gate from merely relocating the spiral from prose-argument into evasion-invention. Cross-attempt retry is always a fresh worktree (never the in-stage re-prompt loop of section 5); only the branch name and journal history survive.

### 11.5 Flaky handling

**Flaky handling.** A check that fails then passes on a bare re-run (same workspace, no code change -- a third, narrow sense of "retry") is a bug signal, not a pass: auto-file a flake report (signature-deduped) and quarantine-ledger the test so it stops blocking merges while its fix ticket queues. A config cap bounds simultaneously quarantined tests (shipped default 5); crossing it halts further auto-quarantine and escalates (section 13), so environmental flakiness cannot silently gut the gate set. Release is mechanical AND identity-bound: the quarantine ledger entry stores its flake report's (signature-deduped) box message id, and the triage resolution that routes that report to a fix ticket records the minted stem in the message's status field -- the resolution record section 12 already keeps, never a decision-registry record -- so ledger entry -> message -> fix stem resolves mechanically, and it is THAT stem's MERGE whose green re-run lifts the entry: never a stem-name inference, never new ticket frontmatter. `resume` stays the manual override -- an HGATE journal Signal like `confirm` (section 13), materialized by whichever single writer holds the lock (the drain pre-cutover, the daemon after; the request reaches it through section 20's control inbox), the release decision journaled BEFORE the ledger changes. Ledger identity: detection -- called only after one named test fails then passes on the bare re-run -- appends one `signal` keyed `flake/<box_id>` (`{kind: flake_detected, test_id, signature, box_id}`) and folds the test into quarantine; release appends one `signal` keyed `flake-release/<box_id>/<fix_stem>` (`{kind: flake_released, test_id, signature, box_id, fix_stem}`) BEFORE folding it out, only when the box resolution names `fix_stem`, that stem merged, and the test's green re-run is observed; a repeated release appends nothing. Flake handling ships with the Phase 3 merge queue (`19.P3`).

### 11.6 Poison quarantine

**Poison quarantine.** A ticket whose run brings down the daemon or its worker is attributed deterministically: restart-reconcile journals each crash's IN-FLIGHT SET (runs with an intent event and no completion) and every member accrues one strike -- under single-flight dispatch (D2) the set is normally a singleton and attribution is exact; if a concurrent pool ever returns (section 18) the culprit is not singly identifiable, so the whole set is striked, and the escalation prints each crash's set so the human can read the intersection. K strikes (shipped default 2) auto-quarantines: journaled, excluded from eligibility (section 9), escalated (section 13). An innocent striked alongside a culprit exits the same way as the culprit: release is a human verb (`tempo resume --ticket <stem>`, an HGATE decision).

### 11.7 Second problems

**Second problems are filed, never folded in.** When an agent notices an out-of-scope problem mid-run -- an adjacent bug, a pre-existing red test, a refactor itch -- the contract is: leave it alone, record it in the run record, emit a Suggestion Box message so it becomes schedulable work (the scope fence makes the inline fix fail closed anyway). A filed problem costs a fresh, correctly scoped run later -- strictly cheaper than a blurred diff that cannot be bisected, reviewed, or reverted cleanly. A pre-existing failure the agent did not cause routes the same way: verify it reproduces on the base commit, then file it, not fix it (unless the fix is trivially in scope).

## 12. Suggestion Box and decision registry

### 12.1 Queue and producers

One durable queue, one sequential LLM consumer (sequential keeps dedup trivially consistent). On disk the queue is one atomic JSON file per message under `<state_dir>/box/`, named `<seq>-<sig8>.json` (zero-padded enqueue sequence + the first 8 hex chars of the failure signature), message id `box-<seq>-<sig8>` -- the id `run.md`'s `## Second problems filed` cites -- with the triage resolution recorded as a status field inside the file (atomic replace, D3). Message classes: `suggestion | failure_report | override_report | retro_finding | bug_report`; class-specific triage prompting lives INSIDE the single `specs/triage.md` (section 8's one-file-per-surface law): the one spec renders a per-class Task variant keyed by the message class, never a spec file per class.

- **Producers.** A second problem never folds into the diff that found it; harvest enqueues each run record's `second_problems` into the box, so a parked stem's filed fix becomes triageable work -- the machine route to the ticket change that releases a premise park (sections 11, 13).

### 12.2 Consumer scheduling

- **Consumer scheduling is era-split.** BOOTSTRAP era: `drain` NEVER scans the box; the Suggestion Box is a filed-complaint queue consumed ONLY when the operator runs `tempo triage`, which triages every pending item in one pass and stops -- a supervising session (section 17) runs it at every phase exit. Producers still enqueue automatically -- only the CONSUMER is human-triggered -- and the next phase's seeds do NOT route through the box (they are direct `confirmed` ticket-plane files, picked up by the drain's tickets-dir re-scan, section 18). DAEMON era (Phase 3+): the watched continuous consumer (section 9) replaces `tempo triage`, always on, no ordering knob (D10). Separate eras, separately governed -- never read one era's cadence onto the other.

### 12.3 Duplicates and the decision registry

- **Never hard-delete duplicates.** Tombstone with a link to the existing ticket or decision. A registry record is ONE markdown file per decision or tombstone under `tickets/decisions/` (committed via the ticket-plane lane, section 15): YAML frontmatter carrying `id`, `kind: decision | tombstone`, `link` (the ticket or decision it resolves to), and the reopen window as `reopen_after_days: <int>` -- REQUIRED, no shipped default, set by triage per record, measured from the record's ticket-plane commit date -- with rationale and evidence as body. The Decision Registry makes every "decided not to implement" durable and searchable; dedup checks new items against open tickets, merged tickets, AND decisions (semantic match, not string match). Decisions may be re-opened after a time window with new evidence. Tombstone visibility: tombstoning a confirmed-class report -- a box record whose `message_class` resolves to a `confirmed` row in the policy table below (`failure_report`, `retro_finding`, and the confirmed `bug_report` rows), read from the record's EXISTING `message_class`; box records carry NO priority field, so nothing here reads P0/P1 (the table's `(P0/P1)` notes are the AUTHORED ticket's priority, never a field on the record) -- is surfaced in status and itemized at the next retro. K re-reports deduping onto the same tombstoned record (shipped default K=3) auto-reopen it, and the reopen's mechanics are PINNED, because every unstated reading violates a standing law: the INVOKER is the box's own dedup path -- whichever layer collapses a re-report onto the tombstoned record (the enqueue signature match, or triage's semantic match) counts it on that record, and the K-th fires the reopen in the same pass; box code owns its queue records (retro READS tombstones for itemization, it never mutates queue state), idempotent on (tombstone record id, reopen occurrence) -- a threshold crossing reopens once, never once per scan or per retro. The reopen CLEARS the tombstoned message's resolution and returns it to `pending` carrying a reopen marker (the registry record stays -- never hard-deleted, above), so ticket creation stays with the ONE machine-authoring seam -- the box consumer -> triage -> Author path (section 4) -- NEVER a second ticket-writing path in box or retro code; a reopen-marked message is never re-deduped onto the record it just reopened -- triage authors from it, and the ticket it authors starts `draft` UNCONDITIONALLY -- the valve exists to buy a human look, so a reopen never takes an auto-confirm row from the policy table below (GO-absent eras resolve `draft` anyway by that table's own rule; under a standing GO this clause overrides the row). Each reopen journals a `signal` keyed `tombstone-reopen/<box_id>/<reports>` (body `{kind: tombstone_auto_reopened, box_id, signature, reports}`, `reports` the post-increment count) BEFORE the resolution clears, through a journal-backed callback every journal-holding Box constructor receives (one `record_rereport` path serves both dedup layers; a Box built without it counts the re-report but at the K-th refuses `RereportCallbackRequired` without clearing), which status and the next retro's itemization read -- the false-positive escape hatch that makes a wrong dedup kill observable from inside the system (and the valve the deferred section 14 dedup-health proxies would read, section 18).

### 12.4 Admission and pre-lineage spend

- **Admission and pre-lineage spend are fail-closed.** Triage returns `author` only when the message or its class-owned evidence demonstrates a current, materially harmful behavior gap, the change is bounded to one buildable ticket, and the goal names a measurable post-change observation. A plan-wording mismatch, speculative hardening, cleanup, refactor, style preference, or test-only improvement is a decision record, not work, unless tied to reachable incorrect runtime behavior. An `author` verdict receives ONE Author invocation with its bounded local re-prompt allowance for the item's lifetime: if it commits no ticket -- schema or requisition exhaustion, timeout, provider failure, post-author validation or commit failure -- the consumer writes one decision record carrying the failure evidence and resolves the item, and no later scan invokes Author for it again; new evidence returns through the normal decision-reopen path. A recorded `author` verdict whose `produced_by_spec_version` differs from the current `specs/triage.md` version is resolved as a decision record (the stale version and the current one as evidence) with NO model call, so a policy change retires the backlog it supersedes instead of spending Author on it. Requisition-review feedback that Author stores beside a recorded triage verdict is an operator annotation outside the closed `TriageAuthor` artifact: a pass resuming that verdict validates only the artifact's own fields, never failing on the annotation. Because that one invocation is the item's whole allowance, `specs/author.md` requires a mechanical PREFLIGHT before emitting: every list item starts `- `; `Depends on` is `- none` or stem bullets; each acceptance criterion names, in backticks, an exact substring of a `## Verification` command or an observable repository path; a bug's `## Regression` is exactly one fenced command plus one or more `- carries: <path-prefix>` bullets; on a re-prompt, Author repairs every supplied finding while keeping already-valid sections byte-identical.

### 12.5 Storm control

- **Storm control.** Dedup by failure signature at enqueue -- the mechanical layer only (the semantic dedup above runs in the sequential triage consumer, never at enqueue). An ENGINE-produced message's signature is sha256 over (message class, producing stem + stage, terminal outcome or gate code, and the reason string normalized: every token containing a path separator stripped, every digit run stripped, whitespace collapsed to single spaces -- deterministic and order-stable); a bootstrap-ingested suggestion line (section 19), which has no stem, stage, or outcome, signs over (`suggestion`, the fixed origin `bootstrap-ingest`, its normalized line text); a host `bug_report` uses the intake recipe below. Plus a circuit breaker: same signature > K in window T (shipped defaults K=5, T=1 hour) TRIPS. The journaled trip PRECEDES the durable held state (a crash can never expose an unjournaled hold) and carries a trip identity. The hold suppresses the emitting stage at dispatch SELECTION -- the scheduler's seam (section 9), which keeps selecting unrelated work -- over a CLOSED pausable set of pipeline stages, a pause surface of its own, independent of the merge queue's integration-red-streak admission pause and the kill-switch pause (both section 9); a non-pipeline producer (a bootstrap-ingested suggestion, a host `bug_report`) has no emitting stage and files + alerts WITHOUT a pause. The trip is ONE journaled signal -- the section 13 `storm-breaker trip` escalation -- whose body carries the held state and the trip identity, plus ONE enqueued P0 `failure_report`: pause, P0, and alert are that one signal plus that one box message, never three separate durable records. Release is `resume` (section 18) BOUND to that trip identity, consumed exactly once within the current engine lifecycle (the running `drain` or `serve`) -- an earlier request can never release a later trip. Enqueue dedup FEEDS this counter rather than starving it: a dedup hit (an arriving item collapsing onto an existing signature) counts toward the breaker window, so upstream dedup cannot make the breaker unreachable. Ledger identity: each arrival records one `signal` keyed `storm-occurrence/<signature>/<occurrence_id>` (`{kind: storm_occurrence, signature, occurrence_id, emitting_stage, emitting_origin}`, the last two nullable strings; a producer-supplied `occurrence_id` unique per arrival and stable on replay), idempotent on an existing key; the window is the half-open `(now - T, now]` over envelope timestamps across active and rolled segments, tripping when count > K; the trip identity is deterministic over (signature, first live occurrence id, crossing occurrence id). In the BOOTSTRAP drain an offer is a whole ticket, not a selectable stage, so its hold selects the ticket whose stem equals `emitting_origin` -- before retry draws or any other dispatch accounting -- while `emitting_stage` stays diagnostic, and a null or non-ticket origin creates no hold.

### 12.6 Outcomes

- **Outcomes:** a ticket (routed to Requisition), a tombstone, or a decision record. The starting state of a box-authored ticket is set by the policy table below -- config-declared as `box_policy` (section 15), one key per table ROW (the three `bug_report` rows are distinct keys; an ABSENT key or row takes this table's value as its shipped default -- unlike the safety inventory's deliberate no-default -- while an explicit `null` is refused per section 15), so the safety inventory fences it by path -- the single knob controlling how much unsupervised authoring authority the daemon has. The confirmed defaults BIND only while a currently recorded GO baseline stands (section 19; the GO record is a journal `signal` event carrying the baselined identity -- the resolved review/author routing rows and spec-major versions, section 19 -- compared to CURRENT routing at policy-read time); absent, revoked, or identity-drifted GO, every auto-confirm class resolves `draft` in EVERY era -- supervised mode; the bootstrap era, where no GO is ever recorded, resolves `draft` by this same rule (prompt 16's `tests/test_policy.py` pin is that rule's protected instance, not an exemption). The BOOTSTRAP era needs no exemption here: its box is consumed only by the operator's `tempo triage` (a human is present at every scan), so a box-authored ticket takes its table default -- almost always `draft` -- and the operator confirms only the few worth building; nothing from the box auto-confirms or auto-runs while tempo builds itself, and the recovery for a stalled bootstrap build is the operator editing the plan and rerunning, never an in-engine loop off the box. Phase seeds are NOT box-authored -- they are direct `confirmed` ticket-plane files (section 19). The policy table below binds the DAEMON era on host work only:

  | Box item class            | Starting state      |
  |---------------------------|---------------------|
  | `failure_report`          | `confirmed` (P0/P1) |
  | `retro_finding`           | `confirmed`         |
  | `override_report` (aggregated rule defect) | `draft` |
  | `suggestion`              | `draft`             |
  | `bug_report` -- self-diagnosed by the host app | `confirmed` (P0/P1) |
  | `bug_report` -- player-submitted, repro attached | `confirmed` |
  | `bug_report` -- player-submitted, no repro | `draft` |

  **Scope override (applies to every row).** Regardless of class, a box-authored ticket whose `## Scope fence` touches the ENGINE-PLANE SAFETY INVENTORY starts at `draft`, so a human sees any change that could widen the daemon's autonomy or shrink its oversight. The inventory is a closed, config-declared path list covering: gate definitions; prompt specs; closed-vocabulary lists; gate severity/trigger config; this policy table and the auto-confirm classes; host low-risk path declarations (the re-review skip list, section 9); per-path merge strategies (a `regenerate:` argv runs at merge time, section 9); MERGE-SAFETY tier membership; failure-spine caps and the quarantine caps; spend ceilings, windows, and provider routing/limits; and the inventory itself. The inventory has NO shipped default and absence FAILS CLOSED: with no declared inventory the override cannot classify a fence, so every machine-originated ticket starts `draft` until the key is declared. The override binds every MACHINE edit of a ticket, not only box authoring: a Rework-updated or Rework-split ticket whose fence NEWLY touches the inventory reverts to `draft` (split successors inherit the check), and a machine-originated ticket carrying any non-empty `gate_bypass` starts at -- or reverts to -- `draft`: the valve's surfacing point is the draft gate, and a bypass that would never pass that gate does not get to skip it.

  **Supervised merge (the daemon-era half of supervised mode).** Starting state governs who CONFIRMS a ticket; it cannot govern whether an unproven reviewer merges unsupervised -- hand-authored tickets enter `source: human`, conductor-seeded bootstrap tickets are authored `confirmed` outright and stamped `source: seed` (sections 13, 19), and neither waits at the draft gate. So absent, revoked, or identity-drifted GO, the DAEMON's merge queue also holds: a green candidate queues for the operator's `confirm <stem>` instead of auto-admitting, while dispatch, review, and other candidates' checks continue -- a supervision queue, never a stop-the-world. The hold's mechanics are pinned here because each crosses a seam: (1) HELD is an ADMISSION state -- a typed result of the merge lane's admission, recorded after MERGE-SAFETY and the INTEGRATION CHECK pass and before ANY main-tree mutation -- never a new run Outcome or run state (the closed vocabularies of sections 5 and 6 do not grow); a held run records NO terminal until its admission settles, and the drain reads HELD as still-settling, never as ok-without-merged; (2) a held stem is EXCLUDED from dispatch eligibility and its worktree is KEPT -- no terminal fires at hold time, so the section 11.2 terminal handler (harvest -> dispatch -> journal -> wipe) never runs on it and the runner returns control to the drain without a wipe -- so release admits the reviewed artifact, not a rebuild; (3) release is the operator's `confirm <stem>`, sharing touchpoint 3's journal-identity resolution (a dirless stem is still confirmable) but NOT its Reject-queue keep semantics: a held release journals the admission's release event, never the `keep` signal that re-arms spine caps (section 11.2's fold), and the double-confirm refusal does not bind it; when main has advanced past the held candidate's checked sha, the release RE-RUNS rebase and the mechanical merge-time re-run set (section 9), the pinned review approval carrying across a clean rebase exactly as section 9 states -- a frozen continuation is never committed over moved main, and every remaining held candidate re-checks after a release advances main; (4) holds are DURABLE, and this clause AMENDS the orphan definitions of sections 11.2 and 15 in place: the hold is JOURNALED when it is recorded, and a `running` with no terminal whose journal shows a live hold is a HELD admission, not reconcile's orphan -- restart-reconcile preserves the held worktree and reconstructs the admission's authority from the journal and committed ticket artifacts, never from process memory; (5) the deliverable landing the hold fences EVERY seam owner it touches -- the merge lane, the hold-policy read, the `confirm`/`reject` release surfaces, the CLI entry, the stage layer, the drain's eligibility fold, and the runner's worktree lifecycle (section 9's ownership law names one owner PER SEAM; a hold crosses many seams, so its deliverable fences every owner, never one module). `run <stem>` -- one ticket, foreground, operator-invoked -- is itself the go-ahead (section 16: supervision here is an attention guard, not an authentication boundary), and the BOOTSTRAP drain never holds an admission at all (section 19). A currently-binding GO lifts the daemon hold; that is what the number gates. The hold is built in Phase 6 (section 19), its first daemon-on-host-work consumer; the self-build runs under the bootstrap drain and never holds, so no earlier phase builds it and no self-build seed depends on it. One machine actor exists on this surface: the Phase 6 exit harness's scripted confirms against its OWN fixture subprocess (section 19) -- delivered through the section 20 control inbox and journaled as machine-actor supervision evidence, never a cap re-arm; a live host operator surface never has a machine confirmer.

  Why: machine-detected breakage and retro findings derived from measured history auto-confirm (self-repair should not wait on the owner; the tickets still pass review); judgment-shaped items -- rule-defect proposals, new ideas -- park at the draft gate.

### 12.7 Advisory checks

- **Advisory checks cannot become theater.** Soft-gate failures are journaled per check code; a configured unhealthy streak for the same code (shipped default: 5 consecutive failures) escalates into one confirmed repair ticket whose goal is to fix the check or remove it through the normal config-change path. Soft means non-blocking -- never invisible.
- The box consumer is watched by the watchdog layer; its failure alerts via the external heartbeat, not via itself.

### 12.8 Host bug intake

**Host bug intake.** Host applications feed the same box, not a separate queue -- dedup, the decision registry, and the storm breaker are exactly what a bug stream needs. The seam is generic: the host contract declares a REPORT INBOX directory; anything host-side -- app self-diagnosis (unhandled engine exceptions, illegal-move storms, view-leak detections) or a player-facing report composer -- writes a small JSON report file there and tempo ingests it as a `bug_report`. The report schema carries origin (`self_diagnosed | player`), summary, a dedup signature (game slug + anomaly code + location + app version for self-diagnosed; semantic dedup for player text), app commit/version, optional implicated code paths, and evidence file paths.

Evidence is the point of the intake: a record-actions host attaches `initial_state` plus the action log -- a deterministic repro -- plus a capped structured-log excerpt. Triage copies evidence into the authored ticket's `evidence/` dir, and the bug gate (section 7) turns the attached replay into a permanent regression fixture: report -> replay -> regression test. Player-submitted text is UNTRUSTED at every hop: triage treats it as data and writes its own SUMMARY into the authored ticket -- raw report text never enters ticket prose, and reaches later prompts only as delimited untrusted-data blocks (section 8). Evidence files are quoted, never executed outside the deterministic replay harness (section 16).

## 13. Ticket contract and human touchpoints

### 13.1 Ticket file and frontmatter

A ticket is one markdown file: frontmatter a script schedules -- YAML between `---` fences, parsed with PyYAML `safe_load` like every YAML surface (section 15) -- plus a body an agent executes. Frontmatter is the minimum the code consumes: `state` (closed vocab `draft | confirmed | rejected | merged` -- `rejected` stamped at a Reject-queue kill via the ticket-plane lane, and the draft->confirmed flip from a human `confirm` is a journal Signal the daemon materializes the same way; `merged` is recorded at settle by the admission's `to: merged` journal transition, NOT a frontmatter restamp -- the scheduler folds merged stems out of eligibility from that journal record (D3), so a settled ticket's frontmatter rests at the value it was authored with; running state is journal-derived, never frontmatter), `source` (closed vocab `human | seed | box:<class>` -- everything machine-originated routes through the box EXCEPT a phase seed, and `source: human` is what "human-requested work auto-confirms" keys on; `seed` is a phase-boundary deliverable authored directly to the ticket plane -- by the conductor or, once self-hosting, a phase-exit ticket's Implement stage (section 19) -- born `confirmed`, never through the box, asserting the seed entry path so the record never claims a human hand-wrote machine-seeded work), `priority` (`P0..P3`), `kind` (`bug | feature | chore`, set explicitly by the author or box triage, NEVER inferred by any lexical heuristic over prose -- an inference is a rule an agent can argue with; an explicit declaration is not), `agent_tier`, `agent_effort` (both on the closed `low | medium | high | max` scale -- tier keys the `models_by_tier` map, section 15; effort passes through to the routed model; an authored ticket DEFAULTS to `medium`/`medium` -- a starting capability the failure spine's ladder (section 11) can climb from on evidence, never a blanket `high`/`high` that leaves escalation nowhere to go, and an author raises the START only for a ticket it judges known-hard), `gate_bypass`. Nothing else. Frontmatter bloat is structurally prevented by the anti-bloat law: **ticket frontmatter may only contain fields the scheduler, a gate, or the authoring/triage policy reads; audit, calibration, and training data are journal-derived projections, never ticket fields.** Field classes consciously excluded -- context-size hints, engine/model pins, agent profiles, doc-ordering modes, tags, groups, lexical bug heuristics -- enter only via D10.

### 13.2 Body sections

Body sections, each existing to remove one specific way an unsupervised agent goes wrong:

- **Depends on** -- the execution graph. The scheduler reads ONLY this; prose ordering does nothing. The graph is closed over consumption: a ticket whose scope consumes another unmerged ticket's deliverable carries that edge, and when a source spec states a deliverable order, an authored SET's edges must realize it -- an authoring pass that translates prose order into tickets asserts the edges, not just each ticket's lint.
- **Context** -- the read-first files: read-only references, distinct from the scope fence, injected into the implement prompt. The habit encoded is read-before-write.
- **On-demand** (optional) -- fenced EXISTING paths deliberately not embedded because embedding them breaches the authoring headroom (section 8); the implementer reads each from the worktree before editing it.
- **Plan contract** (optional) -- the sole channel for plan prose: a bullet list of plan ids -- a numeric section id (e.g. `section 11`), a subsection id (e.g. `section 11.4`), or a section-19 unit id (`19.L`, `19.I`, `19.P0`-`19.P6`, or an entry unit such as `19.P3.rework-stage`); a subsection or section-19 unit resolves to its `###` heading through the next `###` or `##` heading, an entry unit resolves to its registry ROW -- that row's YAML mapping rendered alone under its stem -- and a section is never cited with one of its own subsections -- the renderer resolves verbatim, deduplicates, and injects at bounded size; section 22 never resolves. `Context` structurally refuses the plan file itself (grammar below), so a seed citing the plan cites sections or units, never the file.
- **Goal / Why** -- one observable post-merge outcome, plus the judgment fuel for the fork the spec did not anticipate.
- **Scope in / Scope out** -- scope-out fences the tempting adjacent cleanups; what must NOT change is as load-bearing as what must.
- **Scope fence** -- the write allowlist a gate enforces (section 9).
- **Acceptance criteria** -- every item measurable ("exits 0 when...", "is unchanged"); never "improved" or "better".
- **`## Verification`** -- the exact commands the agent runs to check its own work instead of asking a human. This is where autonomy lives; a ticket without it has to phone home.
- **`## Regression`** (present exactly when `kind: bug`) -- the ONE command that reproduces the reported defect, plus `carries:` path prefixes naming the branch-added test/fixture files the command needs; the section 7 bug gate runs it at the branch head (must pass) and at the merge base with the `carries:` paths overlaid from the branch (must fail -- for the defect, not for a missing test).
- **Definition of rejected** -- when to STOP and throw the branch away rather than churn down a dead end.
- **Time budget** -- agent wall-clock anchored (expected + stuck threshold), never human-engineer hours.

### 13.3 Body grammar

Every machine-read body section has a fixed grammar the Requisition gate validates: `Depends on` is a bullet list of stems (or `none`), each stem required to RESOLVE against existing ticket dirs, merged squash trailers, the supersedes map, or, for a machine-authored seed, a sibling seed of its own batch (`19.L`) -- an unresolvable stem fails closed -- and the same gate rejects dependency CYCLES across the authored set plus the existing graph, re-validated on every ticket write (author, rework update or split, human intake): acyclic at every insertion keeps the global graph acyclic; `Context` is one repo-relative path per bullet, each required to exist in the tree the ticket lands in (for a machine-authored seed, main, never its author's worktree) -- EXCEPT the plan file itself AND the engine's prompt-spec files (`specs/*.md`), both of which `Context` structurally refuses as governed engine prose: plan prose enters a prompt only through the `Plan contract` body section, a bullet list of plan ids (a numeric section id such as `section 11`, a subsection id such as `section 11.4`, or a section-19 unit id such as `19.L`, `19.P3`, or `19.P3.rework-stage`) that the renderer resolves verbatim, deduplicates, and injects as the sole channel for plan bytes, refused fail-closed at the grammar gate when an id does not resolve; a prompt-spec's behavior is cited the same way -- the plan section that governs the surface, never the raw `specs/*.md` file, which carries the engine's OWN data-block delimiter and so breaks the section 8 data/instruction rendering contract the moment it is injected as data; a file the ticket itself will CREATE is named in `## Scope fence` and `## Verification` ONLY -- `Context` is read-first EXISTING material, so a future path there is exactly what the existence check refuses, and a review finding demanding one is demanding the refused shape (this is the one placement rule for future paths); and `Plan contract`, optional elsewhere, is REQUIRED on a `source: seed` ticket and MUST cite, by role: a seed implementing a registry row cites `19.I`, its own entry unit, and the ids its row's `cite` names, never its phase unit, nor `19.L` unless its row cites it; a seeding or exit ticket cites `19.L`, `19.I`, its phase unit, and the entry units of the seeds it authors, and a seeding ticket whose admission carries its phase's exit seed also cites the next phase unit; a hardening ticket -- the stem a `hardening_round` record names (section 11.4), never recognized by its name -- cites `19.L`, the phase unit of each unit in its round, each of those units that exists, and each gapped row's `cite`, a set its engine composer fixes, the grammar gate holding it to the seeding role's floor; a seed of a phase with no registry cites `19.L` and its phase unit -- never whole section 19, nor sections 0, 21, or 22 -- so the plan bytes that OWN the machinery its row states inject (the section 9 ownership law names the owner): cited sections are the only plan bytes that inject, so a seed citing none renders an implement prompt with zero governing plan text -- a closure gap `requisition_review` rejects at authoring (section 7); `Scope fence` is one path prefix per bullet, or `TEMPO_PLAN.md#<unit id>` confining plan edits to that unit's registry row -- several such anchors admit a plan diff confined to their rows together; `On-demand` is one existing repo-relative path per bullet, each also covered by `Scope fence` and absent from `Context`, refused fail-closed otherwise; `Verification` is a fenced command block, one argv-parseable command per line (each tokenized with `shlex.split(posix=True)`, never a shell, D1); `Regression` (present exactly when `kind: bug`, absent otherwise) is ONE argv-parseable command plus one or more `carries: <path-prefix>` lines, each required to match at least one branch-changed file -- the reproducing check the section 7 bug gate executes and the overlay set it applies at the merge base; `Time budget` is an expected/stuck minutes pair written as two bullets, `expected: <int>m` and `stuck: <int>m` (integer minutes, `stuck` greater than `expected`); `Exit-read window` (present only on a phase-exit ticket, section 19) is a bullet list of closed-form journal-window declarations (event type + bounding criterion) the renderer resolves and the driver materializes as the exit ticket's consumed artifact (section 19's materialized journal window), refused fail-closed when a declaration does not parse. The same gate enforces the checkable-criteria floor at the grammar level: every acceptance criterion must name at least one `Verification` command or observable artifact that checks it, and banned adjective forms ("improved", "better", "cleaner") fail lint. Whether the commands ACTUALLY prove the criteria is judgment -- Review's duty, measured by the section 14 scorecard. Grammar is likewise necessary, not sufficient: feasibility is `requisition_review`'s duty (section 7). Every MACHINE-authored ticket -- `box:<class>` and `seed` alike, the engine-composed hardening ticket excepted because no model authors its text (section 11.4) -- is judged BUILDABLE against the shipped engine before it commits `confirmed` and becomes eligible; a `snag` re-authors within caps, an `rma` (a plan defect the author cannot fix) parks for a human. A `source: human` intake (touchpoint 1) runs it ADVISORY -- the operator is its reviewer -- so the front door never blocks on a model call.

### 13.4 Layout and run record

Layout: **one directory per ticket** (`tickets/<stem>/` holding `ticket.md`, `run.md`, `review.md`, `checks.json`, `attempts/<n>/`, evidence). Multiple artifacts per ticket are a directory listing, not a naming convention. Stems match `^[a-z0-9][a-z0-9-]{1,63}$` (lowercase kebab-case): the directory name IS the stem, reused verbatim as the branch name and the squash-trailer identity, validated fail-closed by the `ticket_schema` gate. Two reserved sibling dirs are excluded from the stem namespace by that same gate: `tickets/decisions/` (section 15) and `tickets/retro/` (section 14) -- both match the stem regex, so without the exclusion a ticket could shadow them.

The run record is the co-located receipt (surprises, judgment calls, dead ends, outcome, resolved engine/model, predicted-vs-actual divergence). Run records deliberately preserve NEGATIVE information -- what failed first, what was abandoned, what was left alone on purpose. Together with the journal they are the project's queryable memory: Author and triage query them and the decision registry before creating new work.

The `run.md` schema the `run_record` gate validates is a fixed section set: `## Outcome` (closed-vocab terminal state -- the section 5 `Outcome` vocabulary, no separate set, so a merged run's value is `ok`, or `already_satisfied` for a no-op settlement), `## Surprises / judgment calls`, `## Dead ends` (what was abandoned and why), `## Second problems filed` (box message ids, section 11.7), `## Resolved engine/model` (provider + model + spec versions that served), and `## Predicted vs actual` (the calibration divergence). Sections may be empty but must be present; the gate checks presence and the closed `## Outcome` vocab, not prose quality -- that is Review's duty. The other artifact bodies (packing slip, invoice, snag list, RMA, rework order, approval record, retro findings) are implementer-owned pydantic models, schema-validated at the stage seam but deliberately NOT a cross-version persisted contract the way `run.md`, the journal envelope, and config are.

### 13.5 Human touchpoints

Human touchpoints -- the complete list:

1. Idea intake. The front door is a file: hand-author `ticket.md` under `tickets/<stem>/` in the working tree, or template one with `tempo new <stem>`. File authorship is outside the lock fence (D2) by design; the watcher validates it and the daemon commits it via the ticket-plane lane with a fail-closed `source` stamp: a NEW stem commits `source: human` -- intake STAMPS it (an absent `source` is filled in; a new stem CLAIMING a machine source, `seed` or `box:<class>`, is refused with a paved road naming this rule -- only the machine writes those), and the auto-confirm is the intake commit's journal Signal materializing `state: confirmed` exactly like the human `confirm` flip (frontmatter paragraph above); a stem whose committed predecessor carries `source: seed` KEEPS it across an in-place correction (the section 19 recovery-edit path) -- intake never demotes an established seed, because a demotion falsifies the record (claiming a human hand-wrote machine-seeded work) and reddens any seed-integrity check on the next merge. The human never runs git against a checkout the daemon holds. Validation is never silent: `tempo new` lints synchronously, and tickets failing the watcher's parse are a named top-of-output category in `status`. Every intake commit is journaled and itemized in a `status` intake category: `source: human` asserts the ENTRY PATH, not an authenticated identity (section 16), so the operator can always see what claimed to be them.
2. Draft->confirmed on machine-originated tickets (policy table decides which classes need it; `source: human` auto-confirms).
3. The Reject queue (RMA'd tickets: keep / edit / kill; a kill triggers the dead-dependency handling of section 9 for everything that depended on it). Verb mapping is fixed: `confirm <stem>` = keep (re-enqueue, spent spine caps reset, section 9); `reject <stem>` = kill (`rejected` stamped, dead-dependency events fire); edit is not a verb -- the operator edits `ticket.md` (the intake path, touchpoint 1) and then `confirm`s. Both verbs resolve the stem against the JOURNAL identity, never by loading `ticket.md` first: a stem whose ticket dir is gone (regenerated away, section 19) is still confirmable/rejectable, and rejecting a dirless ghost clears its queue entries with a journal-only `rejected` -- a hold must never outlive its release because its file vanished (section 2). And a `confirm` on a stem with NO input change since its previous `confirm` -- an input change is a `ticket.md` content change or, for a plan-bound arrival (section 11.4), a unit in its `plan_units` whose current unit sha differs from the recorded one, since a plan fix never edits the seed's `ticket.md` -- is refused on the second consecutive occurrence, paved road "edit the ticket (or fix the plan and regenerate it) before re-enqueueing" -- an unchanged ticket re-fed to freshly reset caps is an infinite cap refill, not recovery.
4. Escalations -- the ONLY push channel, a closed vocabulary: stuck-past-threshold, spiral-warning (soft band), budget-exceeded, poison-quarantined, quarantine-cap crossed, provider drought, dead credential (`auth_error`), storm-breaker trip, integration-red streak (merge admissions paused), post-merge tree-hash mismatch, box-consumer death (via the external heartbeat), and Reject-queue arrival (a terminated ticket must never die invisibly while its dependents starve). Transport is a config-declared argv notify command run as an Effect through the section 15 notifications seam; unset, escalations land in `status` and the daemon warns at startup that push is off. The transport has ONE owning deliverable: the notify-transport stem named in the `19.P4` list (the transport Effect, its section 6 notify key domains, and the section 15 `notify` config parse), ordered BEFORE its first push consumer, the watchdog soft band (section 9). Until it merges, EVERY escalation here -- Phase 3's storm-breaker trip and red streak included -- lands in `status` only as a journaled signal, and no earlier seed names or invokes a notify consumer. Each escalation names its pull-side exit (`resume`, `reject`, `kill` -- section 18). Three pinned explicitly: a tree-hash mismatch pauses merge admissions like a red streak (`resume` after inspection or revert); drought-parked tickets un-park automatically at the next dispatch cycle once routing resolves (section 9); box-consumer death exits by restarting the daemon -- an operator action, not a verb.
5. Read-only status, pulled on demand: a `status` projection over the journal (merged, in-flight, blocked, spend, box activity, tombstone digest, and from Phase 5 the current-window scorecard), deterministic and write-free -- a later field is ADDED, never replacing or dropping an existing section or its wording -- consumable as a CLI verb, a written file, or a slash command; a dashboard is a possible later skin over the same projection. Never pushed on a schedule.
6. Engine release. Merged engine work reaches a RUNNING stable instance only through the release path: cut a tag, install into the stable venv, drain (the section 5 quiesce), restart. Same loop for the pre-schema-flip pause of section 5. Recurring, deliberate, small.
7. GO-baseline upkeep. Run the committed baseline eval (the GO-grade fixture harness -- the Phase 1 spike's set grown to GO grade in Phase 6, immediately before cutover, where GO gets its first reader -- a mechanical run), read its committed report -- the run-lane execution inside the drain has already committed it and mechanically recorded its verdict signal, at most NO-GO before cutover (section 0) -- and record GO, when the report earns it, via the harness's operator mode (`--record-go` carries your Author-graph judgment, section 19). The real 24-48h `serve` soak on a synthetic or host workload is part of this same operator cutover step -- run supervised, before GO is recorded; the Phase 3 exit's bounded synthetic soak is its drain-executable stand-in (section 19). Recurs after every model, provider, or spec-major routing change (section 19); until re-recorded the system rests in supervised mode -- a safe resting state, not a stall. A retro-shipped spec-MAJOR change (Phase 5) therefore revokes GO on merge by design, so spec-major self-improvements batch naturally ahead of one re-baseline; MINOR spec versions do not revoke. The post-cutover acceptance reads -- K >= 10 machine-authored tickets merged on host #1 and one real host bug loop, counted only from this engine's own journal and provenance stamps (section 6) -- are this same operator step's follow-through after cutover, never a phase-exit criterion (section 19).

Everything else is autonomous. No pipeline's resting state is a manual step.

## 14. Retro (Reconcile)

### 14.1 Triggers and reads

- Triggers: N tickets OR M days (shipped defaults N=25, M=7) OR a signal SPIKE forcing an early retro -- PER KIND, never a summed total: any one of the four signal kinds reaching the shipped default S=5 within the CURRENT retro window (counted from the latest retro-report `effect_completion` boundary below, like every other window count) fires the trigger on its own. Each kind counts exactly ONE journal-derived identity, pinned here because an unpinned operand reads as unspecifiable and silently drops the trigger (the observed loss): override = each USED bypass (a ticket admitted carrying a non-empty `gate_bypass` entry, section 7 -- the RAW uses the valve's K=3 same-code aggregation and the scorecard's bypass count already fold, never the aggregated `override_report` box message); rework = each Rework invocation (one per emitted rework order, section 4); gate-failure = each terminal `state_transition` carrying `to: gate_failed` (the section 6 run-state vocabulary); integration-red = each admission whose journaled conflict facts carry integration-red implicated paths (section 9 -- the individual reds, never the K=3 red-streak escalation, which is a section 13 pause, not this count). All THREE trigger classes ship in v1; none is deferrable, and a build that cannot author the spike trigger has a plan defect to fix, not a trigger to drop.
- Reads the journal/ledger: predicted vs actual cost and time per ticket, gate catch-rates, bypass aggregation, failure clusters. The driver materializes this window projection as the retro stage's consumed artifact -- the one RECURRING stage whose input is the journal itself (invariant 1's "plus the journal"; a phase-exit ticket's materialized exit-read window, section 19, rides the same driver seam); the committed retro report below is its persisted output.
- Dedup-health proxies (near-identical-merged and reversal-rate signals over the box consumer) are a post-host-#1 D10 return (section 18): until an incident earns them, dedup regressions surface through the operator reading retro reports and through the section 12 tombstone auto-reopen valve, which stays -- the observability is the valve, the proxy metrics are the deferrable layer.
- Conflict-hotspot trending across retro windows -- and the retro-proposed serialization stopgap and refactor-split machinery it would drive -- is a post-host-#1 D10 return (section 18). The section 9 conflict facts are journaled from Phase 3 onward, so the return arrives to an evidence stream already waiting; under single-flight dispatch (D2) conflicts are rare enough that a human reading the facts in the retro window is the v1 detector.

### 14.2 Catch and escape instrumentation

- **Per-surface catch and escape instrumentation** -- the data D10 needs to promote or prune LLM review surfaces, not only mechanical gates:
  - A CATCH is a journal-DERIVED increment per (surface, ticket), never a new emitted event: a gate or review surface raised findings -- a `state_transition` carrying `to: gate_failed` (the gate's codes in its recorded reason/findings), or the review surface's snag/rma verdict recorded in its own LLM-call `effect_completion` (keyed ticket+surface+attempt+call_seq, section 6) -- and the same stem subsequently reached `to: merged` with those findings resolved. A finding upheld at a terminal-without-merge outcome (an RMA the human confirms by killing, an abandon) ALSO counts: a surface that reliably stops the worst work must not read as prunable zero-activity. Deterministic gates and LLM review surfaces are counted the SAME way, from these fail-then-pass and upheld-at-terminal records -- NO per-catch event is emitted and no driver or stage grows a catch emitter (the section 6 EventType set carries no catch type; the scorecard below is a read-only projection, D3 and section 13's anti-bloat law), so a catch fold reading any source other than these named durable records is the fixture-only-event false green this rule exists to refuse.
  - An ESCAPE is a defect that passed every surface: a `bug_report` (or later integration-red) attributed back through the squash trailers (section 10) to a merged ticket OR a bounded range of them. Exact introducing-commit identity is a NON-GOAL. Triage narrows the range from the report's app commit/version and implicated paths (section 12), journals it with a confidence note, and the scorecard counts the escape against the surfaces that passed the range's merges. The escape PRODUCER -- triage's bug-to-merged-range attribution and the squash-trailer read op in `git.py` it needs (a trailer-format log read -- the section 10 op enumeration grows per phase, as the Phase 3 resolution ops did; never a section 18 CLI verb) -- ships with the Phase 6 bug intake (section 19), so the scorecard's escape column is Phase-6-live while its other columns are self-build-live from Phase 5.
  - The SURFACE SCORECARD is the journal-derived projection retro reads: per-surface catch rate, escape rate, bypass count. A projection, never ticket frontmatter (anti-bloat law, section 13). Zero catches AND zero escapes over N tickets makes a surface a prune candidate exactly like a gate; escapes clustering on one surface are the cited-incident evidence for tightening it.
  - Run-filed follow-ups surface through backlog hygiene (below): a stale filed item resurfaces for an explicit promote-or-kill decision; an aggregate scheduled-vs-silted rate metric is a D10 return (section 18).

### 14.3 Outputs and the retro report

- Outputs go through the Suggestion Box (dedup + decision registry for free); one single entry point for all work creation.
- **The retro report is the committed receipt.** Each retro also persists ONE report file -- `tickets/retro/<seq>.md`, zero-padded seq so listings sort in order, committed via the ticket-plane lane (`retro/` is a reserved non-stem dir, section 13) -- snapshotting what the human reads and the next retro diffs against: the surface scorecard, spend against the ceilings, calibration divergence, the tombstone digest (section 12), and every proposal with its named fixed-failure/overcorrection pair. The journal stays authoritative -- the report is a re-derivable snapshot (D3), committed so the receipt travels with the repo the way run records do. The report's commit effect (`key=retro/<seq>`) doubles as the WINDOW BOUNDARY: the N-tickets / M-days triggers and every "within one retro window" count (section 7) measure from the latest retro-report `effect_completion` event -- no new event type needed.

### 14.4 Pruning and tuning

- Retro also PRUNES: proposes deleting gates with zero catches over N tickets, compressing rules, retiring unused vocab.
- **Rule tuning guards against overcorrection.** Every retro-proposed rule or gate change must name BOTH the failure it fixes and the overcorrection it risks. Urgency and importance stay on separate axes: the urgent instance is unblocked first, the important cause is captured as its own scheduled ticket, and ordering lives in `depends` and `priority`, never in prose.
- **Backlog hygiene.** Retro resurfaces stale draft tickets for an explicit promote-or-kill decision. STALE is pinned like every other trigger operand here: a draft ticket with no ticket-plane edit across one full retro window (the window boundary above).

## 15. Engine, hosts, and instances

### 15.1 Engine and instances

- **Engine repo + per-project data plane.** The tempo repo contains engine code only; every host project (including tempo itself) keeps its own `tickets/`, config, and state. Rejected alternative: a hub control repo with one central cross-repo queue -- it divorces tickets from the code they change, and one hub bug breaks every project at once.
- **Self-hosting.** tempo runs its own development tickets.
- **Instance isolation.** An instance = one installed release + its own project checkouts, worktree roots, and state dirs. Stable = a tagged release installed in its own venv, never a symlink into a working tree; rollback = reinstall the previous tag. Stable and dev instances share only git remotes. ONE instance writes a given host repo line: the stable instance owns the host's blessed main; dev instances run against their OWN disposable host checkouts with the checkpoint push disabled; validated engine changes reach stable through the release path (tag + install + restart, section 13 touchpoint 6), never by promoting a dev instance's host main. The "tickets flow between instances through git" provision (section 5) is about SUCCESSIVE engine versions operating one repo line, not concurrent mains. Worked layout:

  ```
  ~/bin/tempo               # stable entry point, installed from tag vX.Y.Z
  ~/source/tempo            # stable engine clone (main)
  ~/source/tempo-next       # dev engine clone (feature branches)
  ~/source/<host>          # host clone: daemon writes main, human authors tickets (sec 13)
  ~/orch/<host>-work/      # stable instance: ticket worktrees + state
  ~/orch-dev/<host>/       # dev instance: its OWN separate host checkout
  ~/orch-dev/<host>-work/  # dev instance: ticket worktrees + state
  ```

### 15.2 Host contract

- **Host contract (doc authored in Phase 6, with its machinery and first external reader; until then this section IS the schema's source).** Config schema with `schema_version` handshake (refuse newer; refuse older with a paved road naming `migrate-config` -- and that verb PERFORMS a real migration, never a vacuous no-op: version 0 IS the defined prior schema, a `config.yaml` declaring the explicit integer `schema_version: 0` (an ABSENT `schema_version` stays a missing-required-key refusal at load and under `migrate-config` alike -- fail-closed defaulting, never a migration case), and `migrate-config` carries it to the current schema (the 0->1 step rewrites `schema_version` and keeps every other key byte-for-byte) -- validating the FULL migrated candidate fail-closed before ONE atomic replace via the filesystem seam, a byte-identical no-op on a current-version config, and a no-write refusal naming the cause on everything else (missing file, missing or non-integer `schema_version`, newer, older-than-supported) -- so the loader's refusal and the verb it names agree, and every future `schema_version` bump ships its migration step in the same change); additive-only ticket frontmatter; the check-runner interface; root AI files (CLAUDE.md/AGENTS.md) rendered from engine-owned templates as ONE committed file with a managed `<!-- tempo:core begin/end -->` block plus a project-owned remainder, refreshed by bootstrap, drift-linted, never hand-edited inside the block (the committed form of the two rule planes, section 8); the single-writer lockfile with instance identity; the injectable-seams inventory -- one minimal seam each (a PRNG seam is NOT in it: nothing in the engine draws randomness; it is added with its first consumer, D10): clock (a zero-arg `Clock = Callable[[], datetime]` -- one clock convention kernel-wide, not a `now()` Protocol; plus its blocking companion `Sleep = Callable[[float], Awaitable[None]]` -- every timed wait, the driver's stuck-budget race included, races the active work against an injected sleep to a clock-derived deadline, never a raw `asyncio.wait(timeout=...)`, so a test advances a deadline with zero wall-clock wait), process-exec (`async run(argv, *, cwd, env, timeout, stdin_path=None) -> (rc, out, err)`; every child is spawned in its OWN PROCESS GROUP (`start_new_session`) and every kill the seam performs is a GROUP kill -- an agent CLI spawns tool subprocesses of its own, and killing only the direct child leaves grandchildren writing the worktree; the seam also exposes a SYNCHRONOUS group kill that section 6's `abort_current` returns behind (signal-not-reap: the reap stays with the async spawner -- a SIGKILLed group can no longer mutate the worktree, which is all `abort_current` promises), and BOTH the seam's own timeout branch AND an outer asyncio cancellation unwinding through `run` route through that ONE kill-and-wait helper (section 19's Phase-0 per-stage `wait_for` stuck-budget kill reaches the seam as exactly that cancellation) -- a cancellation that skips the group kill exits with the CLI's grandchildren alive in a worktree the driver believes frozen; the group-kill promises of sections 6, 9, and 16 (`abort_current`'s contract, the watchdog, `tempo kill <run>`) are implementable only at this seam, which exposes `kill_group` plus a spawn-time pgid hook; the run-to-group binding is PUBLISHED at spawn, cleared at call return, consumed at most once by the `serve` kill path, threaded providers -> the LLM effect -> stages -> serve; `kill <run>` is PER-RUN and SIGKILL-on-receipt -- it never stops the daemon (the serve loop keeps running with dispatch and admission state unchanged; `serve` itself stops via the kill switch or an OS signal, section 18) and it is never a fold at dispatch checkpoints, which cannot kill a hung run that yields no checkpoint, while the kill-switch pause stays the distinct graceful path (section 16); a `kill` naming a run whose merge admission is in flight DEFERS: the admission-owned child is never aborted (the admission path holds the single ticket-plane writer lock, sections 6/9), the kill is consumed when the admission unwinds, and the run launches no further child; a kill signal from a prior daemon lifecycle never replays (the control-inbox identity binding, section 20); ONE active-work executor INSTANCE is threaded through the production composition -- `abort_current` (section 6) is meaningful only on the instance that spawned the active child, so no task identity is aliased and no process seam is shared between active work, notification, and the self-upgrade handoff; and under cancellation the abort/spawn composition STILL translates an unresolvable `argv[0]` to the seam's declared error, never an escaping `FileNotFoundError`; `env` is required and explicit at the seam and every wrapper above it; `timeout` is required but accepts None for the one unbounded caller -- the drain's self-upgrade handoff child, which also runs with INHERITED stdio, streamed to the operator, out/err empty in the result (section 18); `stdin_path` is an optional FILE path (default None) the seam OPENS and connects as the child's standard input -- the sole channel for an agent-CLI prompt, because a rendered prompt is UNBOUNDED and a command-line argument is capped by the OS argument limit, so a prompt is NEVER an argv element and reaches the CLI only as this file (the one the driver spools before the call, section 6); the unbounded handoff child passes none; and the seam FAILS CLOSED on an unresolvable `argv[0]` -- it raises its own declared error naming the missing binary, a config/setup refusal, never an escaping `FileNotFoundError` from spawn), filesystem (atomic `write` / `replace`), notifications (`notify(argv)`) -- kept for fake-driven testing even though re-execution recovery is refused; the notify command (section 13); the state-dir contents (journal, box queue, quarantine ledgers, attempt spools, engine log, heartbeat file -- instance-local, while the decision registry and tombstones live in the host repo under `tickets/decisions/`, committed via the ticket-plane lane, so dedup memory travels with the repo); config re-read at each dispatch cycle, so a ceiling, routing, or severity edit takes effect without a restart (the daemon reads it through the same watcher-and-debounce discipline as tickets; the pre-daemon scaffold verbs satisfy the same rule by RE-PARSING `config.yaml` at each dispatch cycle -- same cadence, no watcher); and an optional report-inbox path for host-app bug intake (section 12).

  The host contract is ONE `config.yaml` at the host checkout root -- located via the `--config` flag, else `config.yaml` at the invocation cwd (the checkout root) -- parsed with PyYAML `safe_load` and validated fail-closed against the `schema_version` handshake (additive-only; the full field list, plus a commented EXAMPLE `review`/`merge` block a new host copies and edits, lives in the host-contract doc, authored in Phase 6). Defaulting is fail-closed: a key with a shipped default takes that default only when ABSENT; an explicit `null` is refused with a precise error naming the key, never parsed into a `None` a typed field forbids. Top-level keys:

  ```yaml
  schema_version: 1
  state_dir: <path>                 # journal, box queue, quarantine ledgers, heartbeat
  worktree_root: <path>             # ticket worktrees (section 10); default <state_dir>/worktrees
                                    # (journal segment roll thresholds are engine constants, section 6)
  providers:                        # section 6
    - name: <str>
      kind: api | cli               # api is REFUSED at load until its client ships (section 6)
      auth: <ENV_VAR_NAME>          # env-var NAME, never a literal secret; OMIT for a cli
                                    #   provider using ambient CLI login (a flat subscription)
      models_by_tier: {low: <model>, medium: <model>, high: <model>, max: <model>}
      limits: {concurrency: <int>,  # rpm/tpm buckets defer with the api client (section 18);
               est_cost_per_call_usd: <usd or unset>,  # required iff cli with no usage in stream
                                    #   (section 6; spill wait is an engine constant)
               quota_window_minutes: <int>}  # cooldown Timer length on quota_exhausted
                                    #   (section 6; shipped default 60 -- a cli provider has no retry-after)
  routing:                          # (agent_tier, llm_surface) -> ordered candidates
    - {tier: <str>, surface: <str>, candidates: [{provider: <str>, model: <str>}]}
  routing_default_tier: medium      # tier for calls no ticket owns (triage, retro; section 6)
  review:                           # section 7
    mechanical:
      - {code: <str>, argv: [<str>], trigger: always | [<path-prefix>], severity: hard | soft}
    surfaces:
      - {name: <str>, trigger: always | [<path-prefix>], rules_doc: <path>, severity: hard | soft}
    trigger_map: {<path-prefix>: [<gate-code>]}
      # always-run vs diff-triggered for ENGINE-shipped gate codes only
      # (section 7); mechanical/surfaces entries use their own trigger
    gate_severity:                  # section 5 invariant 3; engine-shipped codes, MERGE context
      <gate-code>: hard | soft
      # shipped default: every v1 hard-set code is hard at merge; author-context
      # severity is an engine constant (soft) until a consumer exists (D10)
  merge:                            # section 9
    safety_checks: [<code>]         # host-designated fast checks in the MERGE-SAFETY tier
    strategies:                     # rung-1 per-path conflict resolution
      - {paths: [<path-prefix>], strategy: regenerate, argv: [<str>]}
      - {paths: [<path-prefix>], strategy: union}
      # (a low_risk_paths re-review skip list defers with micro-rework, section 18)
  scheduler:                        # sections 2 (D2) and 9 -- single-flight dispatch;
    max_unmerged: 2                 # dispatch backpressure: pause new admissions at this many
                                    #   completed-but-unmerged branches
                                    # (a workers key returns with the worker pool, section 18;
                                    #   serialize_paths defers with the overlap checkpoints)
  # (spend ceilings defer with the api client, section 18; cost stamping needs no key)
  caps: {diagnosis: 6, retry: 6, premise_bounce: 2, hardening: 3, infra: 6, quarantine: 5, poison: 2}
  seeding: {max_seeds_per_admission: 3}  # section 19 bounded-batch seeding contract
                                    #   (the successor seeder is included in the count)
  circuit_breaker: {k: 3, cooldown_minutes: 10}
  drain:                            # section 18 -- the self-build drain's unattended safety envelope (the daemon has its own THRESH bounds, section 6); replaces the per-phase `confirm` (section 19)
    max_runtime_hours: 12           # overall drain wall-clock ceiling: a runaway backstop, NOT a phase gate -- on trip, halt at a safe checkpoint naming the continuing `tempo drain`
    max_ticket_minutes: 90          # hard ceiling the drain refuses to DISPATCH past: a ticket whose authored `Time budget` stuck exceeds it parks with a paved road, never runs
  engine_plane_safety_inventory: [<path-prefix>]  # section 12
  box_policy:                       # section 12 policy table, one key per ROW:
    # failure_report | retro_finding | override_report | suggestion |
    # bug_report_self_diagnosed | bug_report_player_repro |
    # bug_report_player_no_repro
    # absent key/row: the section 12 table value is the shipped default
    #   (explicit null refused, per the defaulting rule above)
    <policy-row>: draft | confirmed
  notify: <argv or unset>           # section 13 escalations
  report_inbox: <path or unset>     # section 12 host bug intake
  context_files: [<path>]           # host-plane content rendered as data (section 8)
  ```
- **No host-layout hardcoding.** Every path the engine touches comes from the host config. A check that assumes a host's directory layout is a portability bug.

### 15.3 Test-harness ladder

- **Test-harness ladder replaces wall-clock soaks:** (1) invariant auditor over the journal, run continuously against live runs -- a closed set of named invariants folded over the record, each carrying the law it enforces: exactly one terminal transition per run, a pre-dispatch drought park (`to: infra_error` carrying `provider_drought` with no open run, section 6) being a run-less park, never a run terminal; every `cap_consumed` naming a declared cap; every `effect_intent` in a run that reached THE ok terminal `merged` carrying a matching `effect_completion` -- the pairing invariant fires at `merged` and NOWHERE else: EVERY non-ok terminal (timeout, abandoned, rejected, gate_failed, premise_failed, all of them) is exempt as closed-run history (section 11.2), and a run that reached no terminal is reconcile's orphan, not the auditor's finding; every `to: merged` transition carrying the commit it produced; every state name inside the closed run-state vocabulary (section 6); timestamps non-decreasing within a segment; (2) crash-point + fault injection on the state layer; (3) replay corpus -- every real incident becomes a permanent deterministic fixture, including journals written by older engine versions as the backward-compat check (journaled effects returning recorded results IS replay); (4) fake-agent simulation driving whole pipelines through the injected seams, pass condition = the invariant auditor; (5) shadow mode, deferred. Any phase exit that names a soak uses rung 4's fake-driven production composition with the injected clock advanced across the specified duration and recurring cycles; elapsed wall time is never the evidence. The shakeout battery (synthetic tickets with known outcomes: bad schema, scope escape, premise-false, unfixable lint, review-reject, merge conflict, orchestrator-crash) is the orchestrator's own permanent test suite.

### 15.4 Portability and fleet

- **Portability proof.** After BoardGameUI runs as host #1, a deliberately different-stack second host validates the host contract with contract fixes only, no project-local hacks. A NON-BLOCKING annex to the Phase 6 exit (section 19): v1 DONE does not gate on it, and it may run any time after host #1.
- **Fleet layer last.** A registry + merged status views only; holds no state and makes no decision a per-project instance could make. Deferred.

Rejected alternatives, recorded so they are not re-litigated: provider load balancing (round-robin or least-loaded candidate selection) -- routing stays an ORDERED candidate list, primary-until-pressure with deterministic spill (section 6), because balanced routing makes the serving model nondeterministic per attempt, muddying the GO-baseline identity (section 19), provenance, and failure attribution, for no throughput the spill rule does not already recover; porting or extracting an existing orchestrator codebase (this is a fresh implementation); and sprint mode (one long-lived feature branch per feature set with a heavyweight promotion gate), which trades per-ticket merge risk for concentrated integration risk at promotion -- the serial merge queue + cheap hard gates + post-rebase re-check delivers the same blast containment without a second-class branch discipline.

## 16. Threat model

tempo is a single-operator personal tool: it runs on a LAN box the operator owns, against the operator's own repositories, and does NOT sandbox executed work. Agent subprocesses, `## Verification` commands, mechanical checks, and evidence replay run with the operator's own privileges.

The failure modes are named so they are decisions: wrong or malicious model output (code, tickets, verdicts, verification commands); prompt injection riding data (countered by the section 8 rendering contract, which keeps untrusted content in the data channel, out of the instruction channel); and a bad dependency or repro pulled during a run. What bounds them is not an OS jail but the pipeline: the hard gate set, the correctness review, and the serial merge queue -- nothing reaches main without passing them (sections 7, 9), and mechanical enforcement is load-bearing, never prose. Two consequences of no-sandbox, named at their sharpest points: first, ticket-authored `## Verification` commands execute with operator privileges BEFORE any review exists -- the gate set bounds what MERGES, not what RUNS, and the section 8 rendering contract narrows the injection path into that execution without walling it; second, the intake front door (section 13) trusts a PATH -- any process with operator privileges, including an executing agent, can write a `source: human` ticket that auto-confirms, which is why every intake commit is journaled and itemized in status. `tempo kill <run>` SIGKILLs a run's process group; the kill-switch pause is the graceful path. Provider keys are injected only into the one call that needs them (section 6), and the redaction seam scrubs configured secret VALUES from every captured stream before it persists -- configured provider secrets only: any other credential on disk is exposed to executed work exactly as to any process the operator runs. OS containment is a D10 return if tempo is ever pointed at work the operator does not already trust.

## 17. Seed root agent files (CLAUDE.md / AGENTS.md)

The repo is seeded with a root CLAUDE.md written from this section by Phase 0 deliverable 1 -- the agents that build Phase 0 need standing conduct rules from the first session. AGENTS.md, the curated subset, is authored from this same section by whichever deliverable FIRST routes a non-Claude agent CLI to a writing surface: that CLI's child loads AGENTS.md and never CLAUDE.md, so the moment the config routes one, AGENTS.md is authored and both files are load-bearing -- a rule add/change/remove touches both in the same change. While every routed context is `claude -p`, CLAUDE.md alone is load-bearing and a second conduct file would be an unread copy to drift. The seed files are the v0 of the engine core template (section 8): when bootstrap rendering ships (section 15), this content becomes the first `tempo:core` block and hand-editing inside the managed block stops.

Format contract -- the context-efficiency rules the seed file obeys:

- Opens with a 3-5 line identity block (what tempo is, engine plane vs host plane) and ONE read-first pointer to this plan. The plan is never duplicated into CLAUDE.md: the root file carries only conduct an agent needs in-context every session, and links here for design detail.
- Two rule kinds, maintained differently. Behavioral rules -- ones no gate can check -- keep full but terse text; the prose is the only enforcement. Gate-backed rules compress to one line: rule + author-time actionable + gate code + link. At seed time every rule is behavioral (no gates exist yet); when a rule gains its gate (Phases 0-2), compress it in the same change that lands the gate.
- Hard size budget: target <= 120 lines. A rule earns a line only if violating it is cheap to do and expensive to unwind.
- Every rule carries a one-line why plus its source: a section of this plan at seed time, a cited incident once D10 takes over.
- Regeneration stop check is ROW COVERAGE, not shape: every rule-set-A row and set-B session bullet is a required semantic assertion compared against the existing file; a row the plan changed that the file still contradicts fails the check, even when identity block, pointer, and line cap already pass.

Seed rule set A -- engine conduct, restated from this plan. The root file states only the conduct form; the linked section owns the design detail:

| Conduct rule | From |
|---|---|
| Pure Python: no shell scripts, no `shell=True`, no string-assembled commands; external binaries only via argv wrapper modules; the tempo venv only | D1 |
| Build the simplest thing that satisfies the ticket; no speculative features, gates, config knobs, or metadata -- every addition cites the incident that earned it | goal 1, D10 |
| No dual-path code: no compat shims, deprecation layers, or defensive parallel paths; rename in place, update every call site, recover by revert | section 2 |
| Fail closed: allowlists and closed vocabularies, never denylists; every prohibition and every gate finding ships a paved road | sections 2, 7 |
| A hold ships with its release: the Reject queue, a premise park, or a poison quarantine lands with or after its release path (same deliverable, or `depends`-after it), its paved road never names an unbuilt verb, and a hold whose only release is a ticket change never precedes the machinery that machine-produces ticket changes | sections 2, 11 |
| Files + journal are the source of truth; derived views are projections and never authoritative -- never hand-edit one or cite one as authority | D3 |
| Generated files are render targets, never write targets: README.md and bootstrap/conductor.py extract from this plan's appendix sentinel blocks, CLAUDE.md is authored from section 17 by Phase 0 deliverable 1 (AGENTS.md by the first deliverable that routes a non-Claude agent CLI) -- a change edits this plan and reruns the generator, never the rendered file | section 1 |
| The plan is the seed: a plan defect (gap, bug, wrong spec) is fixed in the plan, then regenerated from it -- rerun the owning deliverable, deleting and regenerating the affected tickets or code so the seed drives the artifact; a ticket or code file is hand-edited ONLY for a defect provably not the plan's OR under the blocking-defect fast path (a defect stopping the drain's forward progress: plan edited and committed FIRST, then the minimal congruent code fix by hand in the same session, regeneration ticket optional -- section 19), and never before the plan's status is determined | sections 1, 19 |
| All git through `git.py` (argv lists, dir-pinned); worktree cleanup via `worktree remove` + `prune`, never bare `rm -rf`; no `gh`, no PRs in the loop | section 10 |
| Second problems are filed (Suggestion Box), never folded into the current diff; a pre-existing failure is verified on the base commit, then filed | section 11 |
| Read before write: no command, claim, or test is written until the artifact that owns that fact has been read | section 13 |
| Ticket frontmatter carries only fields the scheduler, a gate, or the authoring/triage policy reads; execution ordering lives in `depends` and `priority`, never in prose | section 13 |
| Clock, process exec, filesystem, and notifications go through the injectable seams -- never called raw in engine code | section 15 |
| Executed work is fenced by gates and review, not jailed: v1 does not sandbox executed code; never rely on its goodwill, and never pass a provider key to a process that does not need it | section 16 |
| The journal is the record, never a debug log: diagnostics go to the engine log and attempt spools, and configured secret values are redacted from every captured stream at the write seam | section 6 |
| Do not start a phase until the previous phase's exit is met | section 19 |

Seed rule set B -- session conduct for interactive chat in this repo. Proven in host #1's rule file; stated in full so this document stays self-contained (a human-present chat session is the one context the pipeline machinery does not govern):

- **Terse communication.** Lead with the answer; cut hedging, filler, and preamble; short sentences and tight lists.
- **Comments explain why, not what.** Comment only invariants, hazards, and deliberate-looking-wrong choices; match surrounding density.
- **Plan prose is pure spec.** Rules stated tersely; no incident citations, session references, or change history in plan prose -- provenance lives in the Suggestion Box, the journal, and git history; the one exception is section 22, the plan's uncited rationale appendix.
- **Track open threads.** A reply that raises several questions or options owns that list until it is empty; restate unresolved threads every turn; the user engaging on one thread never closes the others.
- **Announce unsolicited dives.** Name any investigation or authoring the user did not request in 1-2 sentences and get a now / after / skip decision before spending the time; the requested task always runs first. (Autonomous pipeline stages are exempt -- they file same-turn via the Suggestion Box.)
- **Supervising session.** A chat session the user directs to run the conductor or drain and repair its stops is an operator under the section 19 recovery order and the same closed touchpoint list. It runs the conductor with `--auto` and the drain as a background process, acting only when the process exits. Recovery-order work at a stop is requested work, never an unsolicited dive. It stops for the user on: a plan edit changing a section 3 decision, a section 18 refusal, a section 20 open decision, or the 19.L bootstrap contract; an expired agent login; a Reject-queue keep or kill (it diagnoses and proposes; a ticket edit following a committed plan fix it applies itself); and a stem stopping again on a cause it already repaired (one repair per cause). Each recovery reports the plan commit, any hand-fix commit, the filed cause ticket, and the continuing command. At every phase exit it runs `tempo triage` once (section 12.2) and reports each verdict, so the bootstrap-era box is consumed, never left pending.
- **Instance first, cause captured.** A reported problem yields the minimal unblock first and a separately filed cause ticket in the same session -- the chat form of the section 14 urgency/importance rule.
- **Git session safety.** Respect the single-writer lockfile: a chat session never mutates git state in a checkout whose lock a daemon holds (authoring ticket FILES in the working tree is the sanctioned intake path, section 13, and needs no git). Stage and commit only files authored this session, by explicit path; never a tree-wide destructive verb (`clean`, `reset --hard`, `checkout -- .`) in a shared checkout. Never push or remote-mutate from a chat session unless the user explicitly says push -- pushes are the checkpoint Effect's job (section 10).

AGENTS.md is a curated subset, never a mirror: only load-bearing, expensive-to-violate rules cross over; CLAUDE.md is canonical on conflict; once AGENTS.md exists, a rule add/change/remove touches both files in the same change. Once bootstrap rendering ships, both render from the same engine template and the manual sync duty dissolves into the render step.

Excluded on purpose, for self-consistency with the anti-bloat law and the section 18 refusals: host-plane and product rules from any host repo; metadata taxonomies and their lints; batch/wave scheduling rules; provider or model pins; and any restatement of design this plan already owns.

## 18. Explicitly not building (v1 refusals)

### 18.1 Refusals

Named so they are decisions, not omissions. Each may return only via D10 (a real incident):

- Deterministic re-execution recovery; journal hash-chaining; blob store.
- Composition/plugin engine over the block catalog.
- GitHub PRs, `gh`, PR-state machinery in the loop.
- Author-time conflict-prediction machinery (path-bucket tables) and wave/group batch scheduling; multiple ticket authoring modes.
- Metadata taxonomies and their lints (code annotations, test marks, doc tags, glossary currency), producer-health manifests, semantic history search.
- A shipped generic-check catalog. The engine ships ZERO code checks -- no ruff/eslint/complexity presets, no enable-flags: generic linters are one-line host `review.mechanical` entries (section 7), and the host-contract doc's commented example config (Phase 6, section 15) is where the typical rows live -- documentation to copy, never an engine surface, so the check-runner contract stays the only seam and the engine never accretes per-tool knowledge.
- A sprawling CLI. The CLI stays small (stdlib `argparse`, no click/typer): status, new, confirm, reject, kill, pause, resume, retro, triage, migrate-config, doctor, core (the Phase 6 `tempo:core` bootstrap render + drift lint, sections 8, 15 -- the operator and new-host invocation of the ONE renderer whose post-upgrade refresh the daemon commits mechanically per section 10, one renderer with two invokers, never a second render path; listed here so the section 19 deliverable and this closed vocabulary agree), `serve` -- plus `run <stem>` and `drain`, the self-build's scaffold verbs (drive one ticket, or the whole ready queue, through the pipeline synchronously). `drain` carries the entire self-hosted build to quiescence (Phases 2-6, section 19); it retires when the operator CUTS OVER to the continuous daemon on host work, not at Phase 3 -- the daemon is built and soak-proven WITHIN the self-build, then takes execution over at the operator's explicit promotion: `tempo serve` STARTS that continuous daemon (section 19, Phase 3) on host work -- the long-running foreground process that holds the single-writer lock, emits the heartbeat, and is governed by `kill`/`pause`/`resume`; unlike `drain` it does not stop at quiescence but runs until stopped. `serve` is a DAEMON-ERA verb: the self-build never invokes it ON HOST WORK (the scaffold `drain` carries Phases 2-6; the two exceptions are the Phase 3 soak's in-process synthetic-checkout `serve` composition and the Phase 6 exit harness's bounded synthetic-checkout `serve` subprocess, section 19); the one operator-launched pre-cutover `serve` is touchpoint 7's supervised 24-48h soak, run before GO is recorded, so the first UNSUPERVISED `tempo serve` on host work is the cutover itself, launched only after GO is recorded (section 13 touchpoint 7).
  - `drain` runs every eligible ticket in `depends`-constrained (priority, age) order, one at a time in the single writer process, to QUIESCENCE: a non-ok TICKET terminal PARKS that stem and the drain continues with the next eligible ticket; a merge landed mid-invocation satisfies `depends` edges in the same invocation. Quiescence is TRUE quiescence: after every merge the drain RE-SCANS the committed tickets dir -- shipped WITH the Phase 1 drain verb, needing nothing Phase 1 lacks -- so a ticket authored or confirmed mid-invocation (a seeding ticket's ticket-plane output) becomes eligible in the SAME invocation -- the seed-to-seed continuity the self-build needs (section 19). The bootstrap drain NEVER scans the box: the Suggestion Box is consumed only by the operator's `tempo triage` verb (section 12), and the daemon's continuous box consumer is a Phase 3+ era, not this drain. At quiescence -- nothing eligible and unparked -- each parked stem whose `retry` cap (section 11.1) still holds budget is RE-OFFERED -- in the same `depends`-constrained (priority, age) order as dispatch -- one retry unit drawn per re-offer, so eligible work always runs ahead of re-offers and a red stem never starves the queue; a re-offer renders FINDINGS-FED (section 11.2 -- the re-entry ships with this verb, as the section 0 prompt 11-12 pair), and quiescence is re-evaluated after every re-offer merge, so an unblocked dependent runs in the same invocation; the drain ends when nothing is eligible, unparked, re-offerable, or newly authored, reporting every still-parked red with its findings -- the stop names its continuing command (section 19), never a silent tail. Two config-declared bounds hold an unattended drain in place of an operator's finger (section 15). The overall wall-clock ceiling (`drain.max_runtime_hours`) stops it short of quiescence: on trip the drain admits no new ticket, lets the in-flight one reach its stage terminal, journals the halt, reports progress, and names the continuing `tempo drain` (which reconciles on-entry and resumes) -- a runaway backstop the operator sizes above a normal build, never a routine phase gate, and a re-run after it resumes a bounded safety stop, never the banned release for machine-retryable work (section 19). The per-ticket ceiling (`drain.max_ticket_minutes`) is checked at DISPATCH: a ticket whose authored `Time budget` stuck exceeds it PARKS with a paved road (lower the budget) rather than dispatching, so no single ticket claims an unbounded run before the Phase 4 watchdog enforces the authored threshold. The report also lists any draft tickets awaiting `confirm` (section 12) -- box-triage-authored drafts can exist mid-build; the SEEDS themselves are never draft (authored `confirmed`, section 19). Engine-plane refusals (lock contention, journal corruption, config/setup refusals) still STOP the drain; park is for ticket outcomes only. Exit codes are ONE three-value contract for both scaffold verbs: 0 = quiescence reached (parked reds included -- the report names them and their continuing command); 1 = a non-quiescent stop (`run <stem>`'s ticket at a non-ok terminal, or a drain halted by `max_runtime_hours`); 2 = an engine-plane refusal -- wrappers and the conductor key on the class, never the message text.
  - An admission whose diff touches `tempo/**` or `specs/**` is a SELF-UPGRADE: before the next dispatch the drain re-execs itself through the process-exec seam (section 15) as `uv run python -m tempo drain` -- ONE form, dependency changes included (uv's sync is a no-op when nothing changed); this is D1's one named runtime `uv` exception. It carries the invocation's parked set in argv -- one repeated `--parked <stem>` flag per stem -- so no stage runs stale in-process code against the upgraded checkout. The re-exec is a HANDOFF with a fixed order: the parent journals the handoff, closes its journal handles, RELEASES the single-writer lockfile, then spawns the child through the seam with `timeout=None` and inherited stdio (section 15), awaits it, and exits with the child's exit code -- doing nothing else after the spawn, so the child (which reconciles on-entry and takes the now-free lock like any drain) is the only writer. Each self-upgrading admission nests exactly one such awaiting parent; the chain is bounded by the invocation's self-upgrading admissions and every process in it is idle except the leaf.
  - Both scaffold verbs reconcile ON-ENTRY: on taking the sole writer lock they first reap any orphaned in-flight run an interrupted predecessor left (section 11), so an interrupted `run`/`drain` self-heals on the next invocation. A stem whose last run ended non-ok -- reaped (`abandoned`) or a completed non-ok terminal -- stays ELIGIBLE: keys are run-scoped (section 6), so the re-run takes a fresh keyspace and is real work, never a replay, and its re-entry prompt carries the prior findings (section 11.2). A red stem costs a drain at most its remaining `retry` cap, drawn one unit per re-offer from the one retry budget every retry sense shares (section 11.1); the operator escape begins only where that cap ends. Exception: a stem whose last terminal was `premise_failed` -- and carries neither `round` nor `plan_units`, since a spec-gap terminal is held and released by section 11.4 alone -- stays parked across invocations until its ticket-plane `ticket.md` commit changes -- the verdict answered the ticket as written, so re-asking it unchanged replays a judgment, not work (section 2's pointless-call rule). That park's paved road is `source`-keyed (section 13): a `source: seed` stem renders the plan, so its false premise is a PLAN defect -- the road names fixing the false assumption in TEMPO_PLAN.md first (section 1), the releasing `ticket.md` change then being the plan-congruent regeneration -- the section 19 recovery-edit path (an in-place correction of the unrun seed after the plan commit), never a code-first edit that precedes the plan's status determination; a `source: human | box:<class>` stem's road names the direct `ticket.md` edit (a box stem's plan-level cause files separately, section 12). The mechanical release is the one rule both share -- the stem's committed `ticket.md` content changes -- so the road differs only in where the fix originates, never in what unparks the stem.
  - `new` templates a ticket file (the intake front door, section 13); `serve` starts the continuous daemon loop on host work (the daemon era, `19.P3`) -- the sole writer for its lifetime, reconciling on-entry like the scaffold verbs, running until `kill` or a signal stops it; `kill` hard-stops a run (section 16); `migrate-config` performs the explicit older-config migration (section 15); `doctor` is a mechanical self-check (venv, git on PATH, config schema, lock, journal readability). `resume` is the pull-side remediation for every push escalation (section 13): un-pause dispatch after a `budget_exceeded` pause (with the ceiling raised or the window rolled past, else the next pre-call check re-pauses), release a quarantined test or ticket, lift a storm-breaker hold (section 12, bound to its trip identity), and re-open merge admissions after an integration-red streak -- no escalated state is a dead end.
  - Invocation: the CLI is a MODULE entry (`tempo/__main__.py`), run during dev and the whole bootstrap as `uv run python -m tempo <verb>` -- the same venv convention as `uv run pytest`. The project stays a VIRTUAL uv project (`package = false`, no build backend) per goal 1 and D10: a packaged `tempo` console script is not built until the release path needs one (section 13 touchpoint 6), at which point a stable release puts bare `tempo` on `$PATH` (`~/bin/tempo`, section 15). Bare `tempo` is never assumed on `$PATH` during the build.
- The AI audit trail layer (trajectories, reward vectors, preference pairs, training exports, attestations) and its frontmatter/table surfaces. Planned to return as projections over the journal (section 6) -- the kernel already captures the raw material (model, spec version, SHA, cost per effect), so nothing is lost by deferring.
- Formal canary/shadow rollout for prompt-spec changes (provenance measurement first).
- Multi-daemon / multi-machine execution and general leases.
- A feature-flag subsystem. Engine behavior knobs are explicit config keys; rollout risk is handled by the stable/dev instance split and tagged releases (a flag framework recreates the dual-path shape section 2 bans, and every flag doubles the shakeout battery's state space). Host-product feature flags are host-plane content: implemented in host code/config by ordinary ticket diffs, never set from ticket frontmatter, with flag-hygiene gates a host may add per D10.
- A web UI.

### 18.2 Deferred behind a named trigger

Deferred behind a NAMED trigger -- D10 returns, distinct from the refusals above and listed so deferred scope cannot silently re-accrete; each returns only when its trigger fires, citing it:

- The `api` provider client, its token buckets (rpm/tpm), and the cumulative USD spend ceiling -> the first configured `kind: api` provider; until they ship together, `kind: api` is refused at config load (section 6).
- The concurrent worker pool -- and with it the dispatch-time overlap skip, the pre-review hold, a `workers` config key, and dispatch serialization by path -> measured queue starvation or measured contention on host work, post-cutover (D2, section 9).
- A rerere replay cache with its custody protocol, conflict-only micro-rework, and a `low_risk_paths` re-review skip list -> the first chronically re-resolved conflict shown by the journaled conflict facts (section 9).
- Spiral-signal shapes beyond spend-without-progress (debris accumulation, command repetition, mutation-entropy stall, poll-loop match) and the degraded-watch mode -> a pinned spiral fixture the shipped signal misses / a configured stream-less CLI (section 9).
- Dedup-health proxy metrics, conflict-hotspot trending, and the scheduled-vs-silted rate -> a post-host-#1 incident each (section 14).
- The triage-dedup real-model eval -> daemon-era entry, beside the GO-grade baseline (section 19).
- A mechanical scope-fence closure analyzer -> the first fence-gap escape `requisition_review` misses (section 9).
- An analytics engine over the journal (DuckDB) -> the first projection measured too slow as a stdlib fold (D3).
- The per-context (author/merge) gate-severity map -> the first gate needing different severities in the two contexts (section 5).
- A PRNG seam -> its first consumer (section 15).

## 19. Implementation phases

Section 19 is split into separately citable units (section 13 `Plan contract` grammar): `19.L` holds the seeding, exit, and operator laws; `19.I` holds the laws every implementing ticket applies; `19.P0`-`19.P6` hold one phase each -- deliverables, settled contracts, exit reads, and, for the self-hosted Phases 3-6, the phase's SEED REGISTRY; and each registry ROW, citable as `19.P<n>.<stem>`, holds that row's full contract in its bounded fields (`19.L` SPEC DEPTH) -- there are no headings below the phase units. A `source: seed` ticket cites exactly the units section 13 names for its role; it never cites whole section 19, nor sections 0, 21, or 22. Why each law exists lives in section 22, which nothing cites.

### 19.L Build laws

**Phase gating.** Do not start a phase until the previous phase's exit is met, and met is READ, never claimed: a criterion that lives in the journal or git is verified by reading that artifact (D3). Ordering is mechanical: every phase's seeds `depend` on the prior phase's exit ticket, so they cannot dispatch until it merges -- an edge read from git, never operator judgment. One exception: the Phase 1 review-baseline eval runs FIRST within Phase 1, before the walking skeleton; its harness and fixtures are the base the Phase 6 GO-grade baseline grows.

**Bootstrap contract.** The self-hosted build runs to done -- the Phase 6 exit receipt exists green -- or to a plain-English stop naming the one command that continues it, never a per-phase re-run; a drain that goes quiescent with a phase's exit reads unperformed has stalled, not finished, and its quiescence report names the parked exit stem and the unmet read. Phases 0-1 run on `bootstrap/conductor.py`; from the Phase 1 handoff ONE continuous `tempo drain` (section 18) carries Phases 2-6, each phase's final ticket seeding the next into the same running drain, whose after-every-merge tickets-dir re-scan makes those seeds eligible in the same invocation. Each launch carries a durable `bootstrap_attempt_id` stamped into `bootstrap/state.json` (the section 21 conductor owns it) and is bounded by the wall-clock ceiling, `MAX_ATTEMPT_CALLS` (section 21), and -- once real-model calls flow through cost events (section 6) -- the spend ceilings those events feed, read as `max_attempt_cost_usd`. The attempt's record is that state file plus the journal entries carrying its id; the legitimate second attempt after repair is the next no-flag invocation, validated against that record.
- Pre-daemon operator touchpoints are a CLOSED list: start a run; scan the Suggestion Box at will (`tempo triage`); answer a Reject item whose retry budget is spent; edit a premise-failed ticket or fix the plan and rerun (a plan-bound spec-gap arrival releases on its unit's changed bytes, section 11.4); re-authenticate an expired agent login. A run that stops for anything else is a P0 defect the run files to the box. At a stop, an outer context's ONLY writes are those touchpoints, in the recovery order below; any other ticket edit, `confirm`, journal write, or motion of main from outside the engine's own lanes is a P0 defect -- the conductor's builder contexts included (a builder never commits to main past its reviewer, and no deliverable context re-enters the conductor).
- GOOD-ENOUGH BAR: bootstrap review -- the conductor's second-context review of a deliverable and, once tempo self-hosts, the pipeline's review of a ticket -- fails work ONLY for state corruption, deadlock or permanent stall, secret exposure, or false-green verification; every other finding is filed to the box (pre-Phase-2, `bootstrap/suggestions.md`) and the work settles.
- ERA SPLIT: bootstrap behavior is tuned for unattended forward progress through Phase 6; no supervision surface (the GO gate, draft gates, supervised merge, section 12) gates the self-build, and every seed is authored `confirmed`. Supervision engages at the operator's explicit cutover to host work, when GO is first RECORDED -- not because the daemon's code was built. The unattended drain's bound is the SAFETY ENVELOPE -- `drain.max_ticket_minutes` and `drain.max_runtime_hours` with section 18's trip behavior -- a runaway backstop, never a phase gate. The bootstrap drain never holds an admission; recovery is git revert.

**Operator recovery order** (a stopped drain). (1) Read the stopped stem's artifacts -- `diagnosis.json`, `review.md`, `attempts/<n>/`, the engine log, the journal -- for the REAL cause, never the status label (a "no schema-valid verdict" park is usually a spent diagnosis cap). (2) DETERMINE the plan's status before editing anything. A plan defect (a gap, contradiction, orphaned behavior, or a value that should be an input) is fixed in the plan and COMMITTED first, then regenerated from: a merged stem cannot re-run (the drain folds it out), so ship a NEW corrective seed; an unrun seed is edited in place and left UNCOMMITTED for the next drain's intake -- its new SHA releases the park and only the park, because caps are LINEAGE-scoped and no edit resets them (section 11.2; the one re-arm is the operator's Reject-queue `keep`, section 13). A defect provably NOT the plan's -- a fence missing a criteria-forced file (grep every reference before a rename-fence), a sound spec the model could not implement -- is a ticket or code fix, never before the plan's status is determined. BLOCKING-DEFECT FAST PATH: a defect stopping the drain's forward progress (an engine bug no ticket routes around, a seam fault below the failure spine) is repaired by committing the plan FIRST, then hand-applying the minimal congruent code fix with its test in the same session, filing a regeneration ticket only if the hand fix is narrower than the plan change. A code-first patch is the violation; a plan-only edit that strands the working fix unapplied is the opposite violation. Never lead with a ticket edit, `confirm`, or `drain`. A PLAN-BOUND spec-gap stop (`hardening cap spent` or `spec_gap_unresolved`, section 11.4) follows the same order: read the stem's terminal `plan_units`, every `hardening_round` record covering those units with its `gaps`, each such hardener's `run.md`, `review.md`, and terminal, and the stem's seed or premise findings; the default status is a plan defect (two models disagreeing on whether a unit states a fact means the unit does not state it unambiguously); fix the row's `owner`, `records`, `observable`, `tests`, or `fence` (or split the row), run plan lint, and commit the plan by explicit path; file why hardening failed as the cause; continue with `tempo drain`, whose unit-change release re-offers the stem. Never edit the seeding ticket, the seed, or code first.

**Drain conduct.** No drain is findings-blind: the reject-findings re-entry ships WITH the Phase 1 drain verb (section 11.2). A parked stem with remaining `retry` budget is re-offered findings-fed in the SAME invocation, quiescence is re-evaluated after every re-offer merge, and requeue-on-unblock is tested drain behavior (section 18), never an operator duty. The self-upgrade re-exec -- `uv run python -m tempo drain` through the process-exec seam, D1's one runtime exception -- is automatic, never a stop. A seeded queue never relies on its own recovery machinery: a `depends` edge INTO a recovery-machinery ticket must never block the operator escape (a non-ok stem stays eligible and re-runs on fresh run-scoped keys, sections 6, 11, 18), and a recovery-machinery ticket is sized to pass the active gates with the standing render alone, split before seeding when it cannot.

**Seed buildability -- proven where authored, never discovered where run.** The Requisition gate is GRAMMAR only; every machine-authored ticket, seed and box alike, is judged by `requisition_review` (section 7) before it commits `confirmed` -- the engine-composed hardening ticket excepted (section 11.4). The bootstrap floor: Phase 2's conductor-authored seeds and `requisition_review`'s own deliverables pass grammar only and fall to the run-time premise park (section 18); from the Phase 2 exit ticket on, every seed is reviewed before it commits. The review reads the engine and tests the ticket names and judges: the fence covers every file the criteria force (section 9's authoring law); no invariant or criterion contradicts merged behavior; the criteria are mutually satisfiable; every invariant the deliverable states or cites has a NAMED test obligation (a stated rule with no named test is a gap -- a green suite that never exercises it is false-green); and RENDER FEASIBILITY -- the authored base Implement render (standing inputs, zero attempt history) measured against the bound times the authoring headroom (section 8). An over-bound render is a `snag` (shrink or split at authoring). A `snag` re-authors within caps; an `rma` -- a genuine plan defect the author cannot fix -- parks for a human. A machine-authored seed's closure is ALSO pinned by mechanical assertions in the seeding ticket's own test file, beside (never replacing) the review -- the seed's own tests, not an engine-shipped analyzer, and the already-earned D10 return of section 9's mechanical closure check:
1. CONTEXT CLOSURE: every fenced EXISTING path is embedded `## Context` or listed in `## On-demand` (section 13); paths the seed creates are in neither. A path is on-demand only when embedding it breaches headroom; the authoring render-feasibility check (section 8) is the proof, and no test records a size.
2. CONTRADICTED TESTS: every existing test whose assertions the criteria's behavior flip contradicts is fenced -- at authoring, grep the flipped symbol and the old recorded value across `tests/`. A fence wall on a criteria-forced path is this authoring defect; the correct implement outcome is `premise_failed` (section 9), drawing the premise bounce back through Author (section 11.1).
3. ACTIVATION: an activation seed fences each predecessor construction ticket's dormancy-pinning test files, and every fixture or stub object a predecessor's tests hand to a constructor or path the activation now reads (a dormancy fixture passing a bare placeholder where the activated code reads a field is this rule's failure, found by grepping the constructor's test call sites).
4. OWNERS: a deliverable introducing a not-yet-built module states its owner in the seed's prose, sourced from its row's `owner` field (section 11.4); a hardenable row silent on an owner the seed needs is a `spec_gap` of that row (SPEC DEPTH), never an owner invented inline.
5. CALLER CLOSURE: a seed that changes a public signature, constructor arity, or composition-root wiring fences every direct caller an authoring-time grep finds across `tempo/`, `eval/`, and `tests/` (`eval/` harnesses and benches are composition roots); a seed adding a public operation to a module whose public surface an allowlist test pins fences that test; a seed superseding an earlier phase's behavior fences the test that pins the old behavior.
An existing test the seed must keep green but never edits is a PRESERVATION suite: run unchanged in `## Verification`, neither fenced nor Context.

**Seeding chain.** Each phase's FINAL deliverable (its exit ticket) seeds the NEXT phase CORE-FIRST from that phase's registry -- the YAML block in its `19.P<n>` unit, the ONE source every seeding ticket reads and never edits, parsed straight from this plan; it is never copied into a repo file. It authors the registry's `core` admission plus one `<phase>-continue` seeding ticket. Each continuation authors the registry's next admission -- at most `seeding.max_seeds_per_admission` `source: seed` files INCLUDING its successor seeder (config-declared beside `caps:`, section 15; shipped 3) -- plus the next numbered continuation (`<phase>-continue-02`, ...), until the terminal admission carries the phase-exit seed as its SOLE payload with no successor. Each continuation owns only `tickets` plus its own new `tests/test_seeded_phase<n>_<nn>.py`, depends on every payload of the admission before it, and embeds one merged earlier seeding test as its idiom, never the test created in its own admission. Rules:
- GRAIN LAW, both directions: a registry row is ONE ticket owning ONE independently provable contract -- never a multi-boundary bundle, never one deliverable split into a chain of sub-component tickets a single context could own.
- A KNOWN-DEEP row (its contract crosses module seams) rides its admission ALONE and starts `high/high`. The phase-exit seed is plan-named KNOWN-HARD and starts `agent_tier: high, agent_effort: high` (this sentence is its citing evidence). Every other row starts `medium/medium`, except under the pre-ladder tier rule below.
- A batch's seeds depend only on merged work, already-seeded batches, and within-batch stems; a forward dependency on a later batch is refused. Within an admission, list order implies nothing: a seed consuming a sibling's output declares `depends` on it (the registry's `depends`). The chain is finite: each link names its position and carries the shrinking unseeded suffix.
- `requisition_review` at a seeding Check renders ONE target per pass -- each seed against its own text and Context plus committed plan and code, never the accumulated batch -- plus that seed's PRIOR REVIEW: its latest `requisition_verdict` signal in the seeding ticket's lineage (every seed review journals one, carrying `verdict`, `ticket_sha`, a snag's `findings`, and `text` -- the judged seed bytes, the diff base below and the bytes an approved seed is restored from) and the diff from the bytes it judged. The review rules each prior finding cleared or standing and raises a new finding only against changed text. An `approve` holds while the seed's bytes match its `ticket_sha`: Check does not re-review it, and a retry keeps it verbatim and re-authors only the snagged seeds.
- A row's fence is a FLOOR: the seeding ticket adds only paths mechanical rules 2-5 find and records each addition in its test. Any other widening, and any renaming, reordering, adding, splitting, or omitting of rows, is a plan edit under the recovery order, never seeding-time invention. A seed realizes exactly its row's machinery (D10).
- CONTEXT PARTITION: embedded Context is the default; an existing path whose embedding breaches headroom moves to `## On-demand`. A path a sibling seed creates in the SAME admission is never Context; a seed authored after that sibling merges may embed it. Prompt-spec sources and any file carrying the engine data-block delimiter are never Context (section 13).
- SPEC DEPTH: each registry row IS the ticket's full contract -- its `does` plus four bounded fields: `owner` (each module the row introduces, with one clause of responsibility; omitted when it introduces none), `records` (every record, signal, body key, Effect key shape, file layout, frontmatter value, config key, constant with its shipped value, and public signature the row reads, writes, or changes -- exact names and shapes, one clause each), `observable` (one sentence: the dormancy or behavioral proof), and `tests` (the test FILES the row owns plus at most three named tests for its load-bearing invariants; the seed names the rest at authoring under the invariant-to-test closure above). Together the four fields are at most 1,100 characters, 1,600 for a KNOWN-DEEP row (section 8 lint); a contract that will not fit is two rows -- a plan edit under the recovery order -- never a longer row and never a heading. No entry-unit prose exists: `19.P<n>.<stem>` resolves to the row (section 13), so an implementer reads `19.I`, its row, and the row's cited subsections. The seed path's mechanical SPEC DEPTH check runs over every REQUIRED row of each seed (its own row and every row id it cites, section 11.4) before grammar and before any review call: a missing field is a `spec_gap` finding naming that `unit`; a fact a seed needs that a hardenable row omits or contradicts is a `spec_gap` its review finds, naming the row; either hardens the row through section 11.4, never seeding-time invention. A seeding ticket cites each row its seeds need and never copies row text into a seed or into its own ticket. COPY REFUSAL: beside the SPEC DEPTH check, a seed authored by a seeding ticket whose body outside `## Plan contract` contains, whitespace-normalized, any run of 60 or more characters of one of its cited rows' field text is snagged `kind: authoring_error` with the paved road "cite the unit in `## Plan contract`; never copy its text" (an engine-composed hardening ticket is not a seeding ticket's seed). Never seed past the NEXT phase (D10).
- Seeds are written DIRECTLY by the seeding ticket's Implement stage as `confirmed` ticket-plane output, intake-lint validated AND `requisition_review`-checked at its own Check stage -- never through the Suggestion Box, which carries filed problems, never a known build-plan ticket. Seed emission is an ordinary re-runnable Implement pass, never a hook registered to fire after its own merge. DELIVERY: seeds reach main only through the ticket-plane lane (section 10), never the seeding ticket's code branch (that fails MERGE-SAFETY). RE-RUN: a re-run treats the stem's OWN previously lifted seeds (journal-identified) as already emitted, never a collision; foreign stems stay protected. REJECTION: a materialized seed later terminally `reject`ed (section 13) leaves its obligation to a fresh same-goal seed authored by the same pass; section 9's dead-dependency handling surfaces every dependent for re-wiring, the exit's edge included, and the exit read accepts `state: rejected` plus the successor's `to: merged`.
- SEEDING TESTS pin IDENTITY and STRUCTURE only (section 0 prompt 17's pin-grain law): stems, `depends` edges, frontmatter values, fence, and the Context/On-demand partition. No sizes, plan-unit lengths, render arithmetic, or byte hashes: render feasibility is the renderer's over-bound refusal (section 8) and byte identity the `requisition_verdict` `ticket_sha` (above), so no later edit to a fenced file, the plan, or an unrun seed can redden them. They never read the live tree or plan; once their seeds merge they are immutable historical fixtures no later ticket fences or migrates.

**Exit tickets.** The exit ticket carries three obligations -- the exit READ, committing any phase report, and authoring the next phase's core -- and splits into a `depends`-chained pair (READ + report, then seeding) whenever one alone strains its context. Every prior-phase stem is a TRANSITIVE `depends`, so its dispatch IS the merged-presence proof (git-read, the squash trailers of section 10); any authored entry-read is at most that bare check, never a `checks.json`/approval provenance cross-check and never a git verb `git.py` does not ship. Every OTHER criterion reads a COMMITTED artifact or a WIRED-AND-SHAPED proof against merged code -- never the live journal (the exit runs on a clean checkout without the gitignored state dir) and never a live occurrence of a misbehavior-conditional emitter (a healthy drain never flakes, storms, or fails a push). Where evidence IS runtime history, the read is over a journal WINDOW the driver materializes into the worktree, declared in the closed-grammar `Exit-read window` section (section 13), keyed to the declaration, never a stem name. Each read names the emitter's REAL durable record by its own constant -- never an invented signal, never a symbol-presence grep, never a journal or report the exit test itself writes. "Read from the journal" in an exit-read list names the emitter's record surface, never the exit ticket's read surface. An unmet criterion returns `premise_failed` naming it. An exit read realized as a merged TEST runs at Check and on merged main, where only committed artifacts and merged code exist; the conductor-performed Phase 0-1 gates run with the live state dir and may read it directly. The exit ticket's acceptance criteria NAME the transitive-dependency coverage and each read, never a `pytest` run blind to them. When seeding is staged, the exit seed lands LAST with its predecessor chain already committed, so `requisition_review` verifies coverage against committed tickets.
- EXIT-READ CLOSURE: every criterion NAMES its emitter deliverable; `requisition_review` rejects an exit read with no emitter among the phase's deliverables. A COMMITTED-REPORT read names two roles: MACHINERY -- the closed schema in `tempo/artifacts.py`, its registration among the lane writer's KNOWN artifacts in `tempo/stages.py`, its canonical writer, AND a runner that constructs the report from member-local production evidence -- and PRODUCER, a run strictly after the machinery merges (a dedicated no-code run-lane seed, later deliverable runs, or the exit's own harness run). One deliverable is never both registrar and committer, and a deliverable never lifts its own report. Each report registers under its OWN name. A report committed by the lock-holding drain through the ticket-plane Git/Effects seam (the retro report, `19.P5`) takes no KNOWN registration: its machinery is the deliverable that wires that commit and its producer the run that commits it.
- RUN-LANE ADMISSION: a no-code producer's deliverable is its schema-validated report in its worktree OUTBOX. Its admission accepts an empty committed diff exactly when the lift names a registered KNOWN artifact under that stem's OUTBOX besides `run.md` -- never unknown output, uncommitted code, or committed `tickets/**` (section 10); merge journals `to: merged` with `commit: null` and retires once.
- CUSTODY: every exit-read artifact or receipt is MACHINE-PRODUCED by its emitting run and reaches main only through the engine's lanes -- no hand-committed copy or out-of-lane refresh, ever; touching one from outside is a P0 defect. A fault-injection report records a member green only from that member's own evidence AND an invariant-auditor-green member journal.
- ENVELOPE: an exit gate is satisfiable inside the safety envelope -- soaks spend injected-clock time, never wall time; live host reads and the real 24-48h soak are operator post-cutover acceptance (section 13 touchpoint 7), never drain-executable criteria.
- TERMINAL: `phase6-exit` authors no seeds; it performs the exit reads and commits `tickets/phase6-exit/exit-receipt.json` (closed schema, `19.P6`) through the OUTBOX lift, regenerable byte-identical from the artifacts it cites.

**Pre-ladder tier rule.** Until the escalation ladder (section 11) merges, the conductor and every code-bearing seed run at the HIGH tier; a plan-named known-hard seed carries `high/high` at START with its citing evidence recorded in the seed and pinned by its seeding test.

### 19.I Implementation laws

**Staged construction -> activation (phase-generic).** Work that flips production behavior lands as a dormant-until-activated chain with a stated evidence contract per stage:
- CONSTRUCTION builds components unreachable from production, proven by a discriminating dormancy observable -- the transitive `tempo.*` import closure rooted at `tempo/__main__.py`, recognizing both `import tempo.x` and `from tempo import x`, which must FAIL once the component becomes reachable (a byte-snapshot or a one-idiom scan is false-green). A construction landing inside an already-reachable module instead states a BEHAVIORAL observable in its row -- no production caller of the new seam, proven by a test over the production composition graph. Its evidence grade is explicitly sub-production: section 9's production-exercise rule is met by exercising the dormant component directly.
- ACTIVATION flips callers under a TRANSITION FENCE: fence, Context, and Verification name every predecessor dormant fixture and every predecessor NEGATIVE assertion the flip invalidates (verb absence, task count, `not hasattr` composition checks -- an absence assertion has no reference edge to the new symbol, so it is named explicitly and migrated in the same commit), plus every config and composition root the flip forces. Acceptance is proven through the REAL production composition: the first activation of a phase lands an in-process production-composition harness constructing the CLI/`serve` object graph without launching host work, and later activations reuse it. A test-only graph is never production evidence.
- A hold lands with or after its release surface (section 2), and a dedicated proof ticket demonstrates a merged ticket registers its successor end to end before anything depends on it.

**Closed sets are invariants.** Every closed vocabulary or enumerated set the engine defines -- verdict values, `signal` names, `dispatch` values, run states, failure classes, escalation kinds, gate codes, CLI verbs, frontmatter enums -- is an invariant of the row that introduces or extends it, and that row names ONE test that asserts the exact member set (no member missing, none extra) and that the write or parse seam refuses a value outside it; a seam that refuses unknowns while no test pins the members is false-green, because a renamed member passes every consumer. Likewise every constant a row's `records` lists with a shipped value names the test that pins that value. `requisition_review` treats a row adding a member, a set, or a constant without the named test as a `spec_gap` of that row (`19.L` SPEC DEPTH).

### 19.P0 Phase 0 -- Kernel + contracts

- *Deliverables:* `journal.py` (segmented append-only JSONL under the host state dir, glob-ordered read, `read_segments()` as the one parser, write-ahead fsync, torn-tail tolerance; the roll TRIGGER ships in Phase 3); `effects.py`; the lockfile guard (release-then-reacquire proven -- the drain's self-upgrade handoff depends on it, section 18); `git.py`; the config loader (`config.yaml` parse, fail-closed validation, the `schema_version` refuse-newer check, the `kind: api` load refusal -- sections 6, 15); Artifact models, StageResult/Outcome (`already_satisfied` included, section 5), Gate protocol + runner + gate-lint (paved road required); the one LLM-stage driver (section 5 invariant 2: the schema-invalid re-prompt loop, the single-fence JSON unwrap, and the stuck-budget kill raced against the injected `Sleep` seam, section 15) with attempt-spool capture, the engine log, and the redaction seam (section 6); spec renderer + spec lint + the rendered-prompt size refusal (section 8) + the `Plan contract` resolver (section 13: section and unit ids resolved verbatim, deduplicated, the sole channel for plan bytes) + PLAN LINT (section 8); the LLM interface with scripted fake.
- *Emits:* the kernel test suite; the crash-point / fault-injection harness on the state layer (section 15).
- *Exit reads (each from the named emitter's artifact):* a toy echo stage -- consumes a stub artifact, emits one -- runs end to end under the driver with the fake LLM (emitter: the driver); kernel tests green, including a journal read back in order across multiple pre-seeded segments (emitter: `journal.py` + suite); plan lint green over this plan (emitter: `tempo/specs.py` + `tests/test_plan_lint.py`); the fault-injection harness proves torn-tail tolerance and effect once-semantics under induced crashes (emitter: the harness).

### 19.P1 Phase 1 -- Walking skeleton

- *Deliverables:* the provider layer -- registry + routing parse and the FIRST candidate only, with key-scoped injection, and BOTH `cli` adapters (`claude`, `codex`) over the shared base owning write-grant derivation, redaction, and the cost floor (section 6; section 0 prompt 1 is the contract). SINGLE-PROVIDER FALLBACK: with only one provider CLI available, route REVIEW to that provider's strongest tier, record the lost review-identity diversity as a standing open decision, and expect to need section 7's review-authority bounds sooner. The review-baseline NO-GO spike FIRST: a committed fixture set of 15-20 planted defects PLUS at least 3 clean (defect-free) fixtures under `eval/fixtures/`, scored by `eval/harness.py` for catch rate, known-bad false-approve, and clean false-snag; fixtures authored by a different (provider, model) -- at minimum a different tier -- from the one serving REVIEW, the fixture-author identity recorded in the baseline signal. The LLM call behind the `@effect` wrapper (section 6 key shapes, cost event, replay; of `limits` only `est_cost_per_call_usd` parses here). Implement -> Check -> Review -> Merge on ONE real hand-authored ticket, single-flight, CLI-invoked (`status`, `new`, and the scaffold `run <stem>`, section 18); both writer lanes inline in the one CLI process holding the single-writer lock. Merge = the v1 hard gate set minus the bug gate (deferred to Phase 6's report inbox) and including the diff budget gate (section 7), scoped per section 9's merge-time law: the re-run set is the MECHANICAL codes only, `correctness_review` participates as the pinned approval, and `requisition_review` never runs at merge; ticket-plane restore before rebase, squash + trailers (section 9). The verification gate runs WITHOUT base-diff attribution (any red `## Verification` command fails the Check; attribution lands with the Phase 2 spine, section 7). The runner validates and ticket-plane-commits pending hand-authored tickets at invocation (the fail-closed `source` stamp of section 13; intake lint enforces the full section 13 grammar including `Plan contract` ids, `On-demand`, and the `Context` plan-file refusal). Reconcile-on-entry (reaping orphaned in-flight runs). The `drain` verb (section 18) with findings-fed re-entry rendered IN CRITERIA-POSITION (section 11.2) and journal-derived `retry` accounting; a non-ok terminal journals its transition and exits 1, leaving ticket and branch in place. The Author stage + `specs/author.md` are a Phase 2 seed. Engine and host stay COLOCATED; the host-contract indirection is first exercised in Phase 6.
- *Emits:* the `review_baseline` NO-GO signal carrying the baselined identity (the resolved (provider, model) rows serving REVIEW and AUTHOR at every tier plus those surfaces' spec-major versions); one merged real ticket with complete artifacts and provenance events; the seeded Phase 2 queue -- the conductor's FINAL act (section 0 prompts 14-17): each seed intake-lint-clean, `confirmed`, dispatchable under the drain envelope (authored stuck budget at or under `drain.max_ticket_minutes`), ordered by `depends`/`priority`.
- *Exit reads:* the real ticket's `to: merged` transition and squash trailers (emitters: the merge lane, the journal); the recorded NO-GO verdict signal (emitter: the spike harness); the Phase 2 seeds present and lint-green on the ticket plane (emitter: the handoff deliverable). The conductor then retires.

### 19.P2 Phase 2 -- Failure spine

The reject-findings re-entry and journal-derived `retry` accounting shipped with the Phase 1 drain, so every deliverable here runs findings-fed and retry-bounded from its first invocation. Phase 2's seeds are authored by the conductor (section 0 prompts 14-17), each citing `19.L` and `19.P2`.
- *Deliverables, in `depends` order:* caps -- scoped to `diagnosis` and the `infra` budget (`premise_bounce` lands with its release below); auto-harvest before wipe (allowlist extraction, `attempts/` dirs; harvest EXTENDS the section 11.2 rendering, never a second path; worktree keys run- and attempt-scoped, section 6); the diagnosis call with `lessons` over harvested material (section 11's rung order); the Suggestion Box BEFORE anything that files into it (durable queue, signature dedup, decision registry; harvest enqueues each run record's second problems once the queue exists; its first messages are the ingested `bootstrap/suggestions.md` entries -- ONE `suggestion`-class message per non-empty line, the line as summary -- that file then DELETED in the box deliverable's own reviewed code diff, its fence naming `bootstrap/suggestions.md`); the escalation ladder ending at the Reject queue WITH `confirm` and `reject` AND the `premise_bounce` cap and its draw, landing before any hold can fire -- every deliverable that WRITES a marked terminal fences the terminal-write owner `tempo/runner.py` plus `tempo/stages.py`; the Reject queue is a journal-derived projection keyed on the `routed: reject_queue` marker riding the run's single terminal `state_transition` -- queue code never writes that terminal, and the verbs' resolutions journal through the same owner; the box's sequential triage consumer (operator verb `tempo triage`, section 12) -- an `author` verdict invokes Author IN the same pass, and until Author merges an `author`-verdict item rests `pending` with a paved road naming that deliverable; the Author stage + `specs/author.md`; `requisition_review` as THREE chained deliverables -- the pure review call + `specs/requisition_review.md` + gate registration with no consumers; its box-path wiring at Author; its seed-path wiring, sited at the seeding ticket's own Check stage with the PRE-ADMISSION seam in `merge.py`/`stages.py` (MERGE-SAFETY) enforcing the same verdict as the last point that can refuse a seeding branch -- never "drain intake"; each consumer fences the module that OWNS its seam.
- *Emits (each its own seeded ticket; the exit transitively depends on all):* the cumulative shakeout battery (fake LLM) as ONE chained deliverable per owning production module (a fixture ticket fences the ONE module whose behavior it pins plus its test file), each member specifying (1) the planted fault, (2) the single discriminating observable a correct engine produces and a faked run cannot, and (3) which artifact carries the human-readable detail -- the harvested finding, never the terminal REASON, which stays a stable code-only value (the section 11.4 comparison key). Each group run plants its faults and MACHINE-PRODUCES its report entries to its worktree OUTBOX, each later group re-confirming every prior group's entries before appending its own (the double gate). Membership: bad schema, scope escape, premise-false, unfixable lint planted BRANCH-ONLY, review-reject -- all reaching correct terminals with zero human input; a review-rejected ticket whose re-entry renders the prior findings IN CRITERIA-POSITION; a timed-out ticket whose second attempt sees the first attempt's dead ends; engine death mid-call reaped/harvested/re-entered findings-fed; a conflicted rebase leaving no half-rebased worktree; an empty committed diff reaching a non-ok terminal; a planted secret reaching no ticket-plane artifact; a premise-failed stem skipped until its ticket changes, then run; agent-CLI auth expiry yielding a classified non-ok naming the re-auth road; unparseable or schema-invalid output exhausting the bounded re-prompt then terminating; a stage past its stuck budget killed by the Phase 0 driver, harvested, and terminated while the drain proceeds; a ticket red on attempt one and green on attempt two merging in ONE drain invocation drawing one `retry` unit; K identical terminal reasons short-circuiting (section 11.4); per-COMMAND base-diff attribution (section 7). PLUS the diagnosis real-model eval as TWO chained deliverables -- harness + committed fixture set (expected verdicts proven reachable under the fake LLM; hard-kill before the deadline records and pre-call cost cap pinned by tests against the committed report), then the spend ticket that only EXECUTES the merged harness under a flat per-run USD constant (shipped 5.00), agreement rate recorded (the triage-dedup eval is deferred to daemon-era entry, section 18; Rework and Retro stay unevaluated -- their production signal is the section 14 scorecard). PLUS the invariant auditor over the journal, green across the whole battery.
- *Settled contracts:*
  - NAMES: the battery lives in `eval/shakeout/` with its public bench at `eval/shakeout/bench.py` and suite `tests/test_shakeout.py`; the auditor is `tempo/audit.py`/`tests/test_audit.py`; caps `tempo/caps.py`/`tests/test_caps.py`; box `tempo/box.py`/`tests/test_box.py`; triage `tempo/triage.py`/`tests/test_triage.py`; Author `tempo/author.py`/`tests/test_author.py`; starting-state policy `tempo/policy.py`/`tests/test_policy.py`; requisition review `tempo/requisition.py`; the diagnosis eval `eval/diagnose.py` with fixtures under `eval/diagnose_fixtures/`; the review harness test `tests/test_eval_harness.py`. Later registries name these paths.
  - BENCH SEAM: `Bench.configure(parsed_config)` rebuilds the production runner, drain, redactor, log, and pipeline factory around a replacement config whose `state_dir` resolves to the existing bench state dir, preserving the disposable repo, journal, clock, process, filesystem, git adapter, report sink, and scripted-model seams; members select fixtures only through it and never assign private bench fields.
  - REPORT REQUIRED: a group run's report command is never excusable. When a `## Verification` command names `tickets/<stem>/<registered KNOWN artifact>`, the Check fails `verification` unless that artifact is in the OUTBOX after Verification, whatever per-command attribution says (paved road: make the named runner exit 0 so it writes the report); so a passing group Check implies its runner's double gate and every member's auditor came back green.
  - FIXTURE CORPORA: a diagnosis-eval case is ONE closed JSON envelope (ticket text, harvest, optional run record, expected answer), so the 12-case corpus plus harness and tests stays inside the section 7 diff gate.
  - SUPERSEDED BEHAVIOR (rule 5): the verdict-verb/`premise_bounce` deliverable owns `tests/test_drain_upgrade.py`; the base-diff attribution deliverable owns `tests/test_drain_reentry.py`; Author owns `tests/test_eval_harness.py`'s pre-Author `spec_major.author: null` expectation.
  - ATTRIBUTION: recorded per verification command; every base-red command files through `Box.enqueue` with `origin` equal to the reporting stem (the section 12 signature dedups within that stem, never across stems); the same attribution runs at post-rebase merge regating; the Phase 1 branch-only-red re-entry regression stays beside the new base-red case.
  - LADDER: the escalation deliverable fences `tempo/caps.py`/`tests/test_caps.py`; the rung rides the retry `cap_consumed` body through the existing `consume` writer, never a copy in `drain.py`.
  - BOX: one-time ingest resolves a linked worktree through a `git rev-parse --git-common-dir` operation owned by `tempo/git.py`, fenced by the box deliverable. Messages carry nullable `bug_origin` and `has_repro` (required together when `message_class: bug_report`); Author uses a named tracked-file listing operation in `git.py` and validates policy inputs before its paid call.
  - REQUISITION WIRING: the Author-path consumer fences `tests/test_triage.py`, and every triage integration script reaching Author supplies the newly mandatory review response. The Driver gate loop gains one generic `terminal_findings` hook so a configured terminal verdict (requisition `rma`) returns at once without discarding other gates' findings; the review target resolver reuses the complete ticket-schema admission predicate, reserved stems included, so grammar-invalid work never spends a review call.
  - PROVIDER FAILURES in shakeout follow section 6's classification-to-finding rule: auth expiry proves `to: infra_error` plus an `infra` `cap_consumed`; the harvested finding carries `auth_error` and the re-auth road.
- *Exit reads:* every spine stem's `to: merged` (emitter: the merge lane, via transitive `depends`); battery green including every named member (emitter: the committed `shakeout-report.json`, a registered KNOWN artifact with a closed schema declared by the report-lane deliverable -- member id, planted fault, observed terminal/event, producing run id -- written only by the group runs' OUTBOXes and lifted by the ONE stage-terminal lift path, whose owner `tempo/stages.py` and `tests/test_stages.py` the report-lane deliverable fences; the cumulative copy rests in the LAST group's ticket dir); auditor green across the battery (emitter: the auditor's Check-lane `checks.json`, never authored by the auditor's own fence). The exit ticket authors Phase 3's `core` admission from `19.P3`.

### 19.P3 Phase 3 -- Continuous daemon (core)

- *Deliverables (index; the registry below is the seedable form):* the asyncio scheduler (single-flight dispatch, D2) with watcher-driven re-prioritization; the serial merge queue with post-rebase re-gate, pre-admission INTEGRATION CHECK, red-streak pause, tree-hash assert, conflict-facts journaling, and both resolution rungs (section 9); the Rework stage + `specs/rework.md` (update/split/escalate, the supersedes map; never invoked inline from the admission path, which holds the single ticket-plane writer lock -- the mechanical rung returns a typed unresolved-conflict handoff that Rework consumes after the admission unwinds); THRESH in its flat-subscription form (per-provider concurrency caps, `cli` classification including `unclassified`, the circuit breaker, section 6); the daemon control surface (section 20) one boundary per ticket; heartbeat; restart-reconcile + orphan sweeper + `timers.py`; flake handling (section 11.5) as detection then release; the journal roll trigger (section 6 engine constants, 64 MiB / 24h); the storm breaker (section 12) as ledger -> producer wiring -> notification activation -> dispatch hold; the checkpoint push with failed-push re-fire (section 10); production `serve` composition; daemon-mode merge admission; recovery disposition; run-lane admission; the bounded soak machinery, runner, and run. Until Rework activates, a `split` dispatches to the Reject queue (section 11.4).
- *Exit reads:* a BOUNDED DETERMINISTIC soak: `daemon-soak-runner` drives the PRODUCTION `serve` composition IN-PROCESS through injected seams (section 15 harness ladder rung 4 -- the FIRST of the two sanctioned pre-cutover `serve` harness runs, never the cutover) with the injected clock advanced at least 24 hours per member, so every recurring daemon cycle fires; elapsed wall time is never evidence. The member list is CLOSED: `worker_killed_mid_run` (reconciled, its `recovery_alert` naming disposition and producing run, the stem re-run clean); `conflict_resolution_rungs` (both rungs, the correct one selected, main green after each); `semantic_conflict_integration_red` (integration-red WITHOUT main ever going red). Every fault lands in the box or an alert, never silence; a member is green only when its terminal passed AND the auditor is green over that member's own journal. Emitter: `tickets/soak-run/daemon-soak-report.json` (machinery `daemon-soak` + `daemon-soak-runner`, producer `soak-run`), re-derivable by the merged suite. The real 24-48h soak stays operator acceptance (section 13 touchpoint 7).
- *Settled contracts:* the control inbox, `pause`, and `kill` semantics are section 20's; flake identity is section 11.5's; storm identity and the drain's hold selector are section 12's. Construction rows landing inside already-reachable modules -- `dispatch-admission-boundary`, `dispatch-config-snapshot`, `background-consumers`, `control-inbox`, `dispatch-pause-boundary`, the four dormant kill boundaries, `flake-detection`, `storm-producer-wiring` -- prove dormancy BEHAVIORALLY (`19.I`): no production caller of the new seam, by a test over the composition harness once it exists, before it by a test over the CLI composition root. Large roots expected on-demand in most renders: `tempo/drain.py`, `tempo/__main__.py`, `tests/test_drain.py`, `tests/test_mergequeue.py`, `tests/test_merge.py`.

```yaml
# BEGIN_REGISTRY_P3
phase: 3
admissions:
  - [daemon-scheduler, seed-successor-proof]
  - [merge-queue]
  - [rework-stage]
  - [thresh-runtime]
  - [dispatch-admission-boundary, dispatch-config-snapshot]
  - [scheduler-activation]
  - [merge-queue-activation]
  - [rework-activation]
  - [background-consumers, control-inbox]
  - [dispatch-pause-boundary, pause-resume-activation]
  - [admission-holds-activation]
  - [kill-signal-journal, kill-executor-abort]
  - [kill-worker-stop, kill-failure-suppression]
  - [kill-cli-activation]
  - [heartbeat]
  - [restart-timers]
  - [flake-detection, flake-release]
  - [journal-roll, storm-ledger]
  - [storm-producer-wiring, storm-notification-activation]
  - [storm-dispatch-hold]
  - [checkpoint-push]
  - [serve-activation]
  - [serve-merge-admission]
  - [worker-recovery-disposition]
  - [outbox-only-admission]
  - [daemon-soak]
  - [daemon-soak-runner]
  - [soak-run]
  - [phase3-exit]
seeds:
  daemon-scheduler: {cite: ["9.1", "9.3"], fence: ["tempo/scheduler.py", "tempo/watcher.py", tests/test_scheduler.py],
    does: "Dormant single-flight scheduler plus watcher re-prioritization; dormancy = the 19.I import-closure scan, failing once either module is reachable.",
    owner: "tempo/scheduler.py: pending queue, single-flight dispatch; tempo/watcher.py: debounced ticket changes, last-good cache",
    records: "Reuses drain.authored_at, drain.sort_key (priority rank, missing age, timestamp, stem), drain.SETTLED; holds: routed: reject_queue until signal: reject_verdict, quarantine/drought sets, scheduler.max_unmerged (default 2); age = ts of FIRST SIGNAL ticket_intake; watcher.py WATCHER_PARSE_FAILURE = \"watcher_parse_failure\": SIGNAL ticket: <stem>, key: null, {signal: watcher_parse_failure, path: tickets/<stem>/ticket.md, reason}",
    observable: "Driven directly with injected seams, edits re-sort pending work without preempting the active dispatch; both modules stay outside the 19.I CLI closure.",
    tests: "tests/test_scheduler.py (test_single_flight_dispatch, test_watcher_parse_failure_preserves_last_good, test_scheduler_and_watcher_are_dormant)"}
  seed-successor-proof: {cite: ["9.1", "9.2", 10], fence: [tests/test_seed_successor.py],
    does: "A merged ticket registers its successor through the real lift/intake/rescan path, end to end.",
    records: "No new records. Fixture Implement writes tickets/<successor>/ticket.md (state: confirmed, source: seed, priority: P2, kind: chore, agent_tier: medium, agent_effort: medium, ## Depends on parent); Check writes checks.json SeedReview {stem, ticket_sha, verdict: approve, findings: [], mechanical} and SIGNAL under successor {signal: requisition_verdict, seeding: <parent>, verdict, ticket_sha, findings, text}; lift_seeds commits tempo(<parent>): seeds and SIGNAL {signal: ticket_intake, source: seed, state: confirmed, new: true, commit, seeded_by: <parent>}; Effects keyed ticket-plane/<parent>/<attempt>/seeds; merge writes {to: merged, commit, reviewed_sha}; drain.SETTLED unchanged",
    observable: "One production CLI drain run with a scripted FakeLLM merges the parent, picks up its committed seed successor on the next rescan only after that terminal, and merges it too, with one intake and one merged event per stem.",
    tests: "tests/test_seed_successor.py (test_merged_parent_registers_and_runs_successor_in_same_drain)"}
  merge-queue: {deep: true, cite: ["9.3", "9.4", "9.5", 10, "15.2"],
    fence: ["tempo/mergequeue.py", "tempo/merge.py", "tempo/git.py", tests/test_mergequeue.py, tests/test_git.py],
    does: "Dormant serial admission: post-rebase mechanical regate, integration check running the ticket's Verification through the existing gate on the rebased worktree, red-streak pause, tree-hash assert, conflict facts, two typed resolution rungs. git.py adds only public stop-at-conflict, conflicted-path listing, and rebase-continue, preserving rebase/rebase_abort.",
    owner: "tempo/mergequeue.py: MergeQueue serial admission, mechanical rung, ConflictHandoff, red-streak/tree pauses, host check runner",
    records: "mergequeue.py: CONFLICT_FACTS = \"merge_conflict_facts\", RED_STREAK = \"merge_red_streak\", TREE_MISMATCH = \"merge_tree_mismatch\", RED_STREAK_LIMIT = 3; SIGNAL ticket: <stem>, key: null bodies {kind: merge_conflict_facts, conflicted_paths, resolving_rung: none|mechanical|rework, strategy_paths, integration_red_paths}, {kind: merge_red_streak, stems, limit: 3}, {kind: merge_tree_mismatch, checked_tree, main_tree}; ConflictHandoff {stem, reviewed_sha, conflicted_paths, findings, approval_invalidated: true}; new stages.gather_safety_evidence(ctx, ticket, claimed); new Git ops rebase_stop_at_conflict, conflicted_paths, rebase_continue; reads merge.safety_checks, review.mechanical {code, argv, trigger, severity}, merge.strategies {paths, strategy: regenerate|union, argv}; spools merge-safety/host-<nn>.txt, merge-integration/host-<nn>.txt; host timeout ticket.stuck_minutes*60, rc: null, base_red: false",
    observable: "Driven directly, one serial admission restores, rebases, runs safety then integration on the rebased worktree, squashes only when main^{tree} will match the checked tree, pauses on 3 distinct red stems or tree mismatch; outside the CLI closure.",
    tests: "tests/test_mergequeue.py (test_squash_tree_trailers_and_retirement, test_unresolved_conflict_returns_typed_handoff_after_abort, test_merge_queue_is_dormant); tests/test_git.py; tests/test_stages.py"}
  rework-stage: {deep: true, cite: [4, "9.2", "9.5", "11.3", "11.4"],
    fence: ["tempo/rework.py", specs/rework.md, "tempo/mergequeue.py", tests/test_rework.py],
    does: "Dormant update/split/escalate plus the supersedes map, consuming the typed conflict handoff only after admission unwinds.",
    owner: "tempo/rework.py: Rework stage, order schema, proposal validation, supersedes writer/folds; specs/rework.md: rework prompt",
    records: "Reply {action: update|split|escalate, tickets: [{stem, ticket}]} -> ReworkOrder (Artifact + stem, attempt, action, tickets); proposals keep source, state: confirmed, starting agent_tier/agent_effort; REWORK_ORDER = \"rework_order\": SIGNAL on original stem {signal, attempt, action}; SUPERSEDES = \"supersedes\": {signal, successors: sorted unique} after split publication, idempotent",
    observable: "Via Driver.run (surface rework) orders are reviewed and returned without ticket edits; queue never calls Rework inline; rework.py outside the CLI closure.",
    tests: "tests/test_rework.py (test_split_order_requires_fresh_buildable_successors, test_supersedes_record_after_publication_is_idempotent, test_rework_is_dormant)"}
  thresh-runtime: {deep: true, cite: ["6.8", "6.9", "6.10", "6.11"],
    fence: ["tempo/thresh.py", "tempo/providers.py", "tempo/config.py", tests/test_thresh.py, tests/test_providers.py],
    does: "Flat subscriptions, per-provider concurrency caps (limits.concurrency parses here), cli classified/unclassified failures, the breaker.",
    owner: "tempo/thresh.py: per-provider slot admission, FIFO cap waits, breaker writer/fold, pre-call refusal",
    records: "Reads limits.concurrency, circuit_breaker {k: 3, cooldown_minutes: 10} (existing); SPILL_WAIT_SECONDS = 60; PROVIDER_CAP_WAIT = \"provider_cap_wait\": SIGNAL key: null {signal, provider, call_key, started_at, waited_seconds, disposition: admitted|cancelled|unavailable} per ended wait; PROVIDER_CALL_OUTCOME = \"provider_call_outcome\": {signal, provider, call_key, failure_class: null|rate_limited|quota_exhausted|outage|auth_error|model_error|unclassified, open_until} per executed call; K consecutive outage/unclassified opens until clock + cooldown; providers.py marker order: AUTH_MARKERS; quota exhausted/usage limit reached/out of extra usage; rate limit/too many requests; service unavailable/internal server error/stream disconnected/at capacity; model unavailable/model not found/model is not supported",
    observable: "Driven directly, a slot is reserved before the effect callback, open breakers are skipped and rechecked at the FIFO head, all-open routes refuse pre-call without effect or cap draw; thresh.py and the classifier stay unreachable from the CLI.",
    tests: "tests/test_thresh.py (test_cap_wait_rechecks_open_breaker_before_admission, test_breaker_rebuilds_from_journal_and_releases_at_deadline, test_thresh_is_dormant); tests/test_providers.py"}
  dispatch-admission-boundary: {cite: ["9.1"], fence: ["tempo/daemon.py", tests/test_daemon_admission.py],
    does: "Dormant daemon admission/task boundary.",
    owner: "tempo/daemon.py: DaemonAdmission, single-flight task owner around an injected tempo.runner.Dispatch",
    records: "async DaemonAdmission.dispatch(ticket) -> terminal str, Ticket passed unchanged to Dispatch = Callable[[Ticket], Awaitable[str]]; memory-only active (Ticket) and task (asyncio.Task[str]), both None when idle; slot acquired before task creation, held until callback unwind; exceptions/cancellation propagate; no new record; running transitions and retry draws stay in runner.py/drain.py",
    observable: "Direct tests show no overlapping callbacks and ownership cleared on success, exception or cancel, while CLI run/drain neither import tempo.daemon nor hit a raising probe on dispatch.",
    tests: "tests/test_daemon_admission.py (test_concurrent_dispatch_is_single_flight, test_active_cancellation_awaits_cleanup_before_release, test_daemon_admission_is_dormant)"}
  dispatch-config-snapshot: {cite: ["15.2"], depends: [dispatch-admission-boundary],
    fence: ["tempo/daemon.py", "tempo/config.py", tests/test_daemon_config.py], does: "Per-dispatch immutable config snapshot.",
    records: "config.snapshot_config(config): detached recursively immutable Config view equal to config.model_dump(mode=\"python\") (models refuse assignment, dicts read-only, lists -> tuples); daemon.snapshot_dispatch(load, bind) -> Dispatch as the DaemonAdmission callback: load() -> Config via load_config, bind(snapshot) -> ticket callback; capture once per admitted call inside the slot; load_config and DaemonAdmission.dispatch(ticket) signatures unchanged; no new record",
    observable: "Each dispatch loads/captures/binds once and keeps its snapshot despite later edits, the next admission sees valid edits, invalid reloads fail before binding; CLI run/drain call neither operation.",
    tests: "tests/test_daemon_config.py (test_snapshot_is_recursively_immutable_and_detached, test_dispatch_captures_once_and_keeps_snapshot_until_completion, test_config_snapshot_is_dormant)"}
  scheduler-activation: {deep: true, cite: ["9.1", "6.8", "15.2"],
    fence: ["tempo/daemon.py", "tempo/scheduler.py", "tempo/watcher.py", "tempo/__main__.py", tests/test_scheduler.py, tests/test_daemon_admission.py, tests/test_daemon_composition.py],
    does: "Daemon core reachable from production; lands the in-process production-composition harness (tests/test_daemon_composition.py); migrates the scheduler dormancy test and the daemon-absence assertion.",
    records: "New async runner.prepare_pipeline(checkout) -> Dispatch (ProviderLLM, preflight(), runner.Refusal, then bind), shared by sync runner.pipeline which uses asyncio.run only at the bootstrap root; daemon core wires Watcher.publish -> Scheduler.update, Watcher.remove -> Scheduler.remove, Scheduler.dispatch -> DaemonAdmission.dispatch with snapshot_dispatch(load, bind); root bind(snapshot) = dataclasses.replace(checkout, config=snapshot); preparation seam Callable[[Checkout], Awaitable[Dispatch]]; Checkout.config, StageContext.config, Driver.from_config and provider/redact/gates/caps consumers accept Config | ConfigSnapshot; no new record",
    observable: "Daemon, scheduler and watcher become reachable from the CLI and the real root builder constructs the graph without dispatching or snapshotting, awaiting preflight inside each admission; run/drain keep inline admission.",
    tests: "tests/test_daemon_composition.py (test_production_core_awaits_preflight_inside_admission, test_production_core_dispatches_through_admission_and_snapshot, test_daemon_core_is_reachable_from_cli); tests/test_scheduler.py; tests/test_daemon_admission.py; tests/test_daemon_config.py"}
  merge-queue-activation: {cite: ["9.4", "13.5"],
    fence: ["tempo/merge.py", tests/test_merge.py, tests/test_mergequeue.py, tests/test_daemon_composition.py],
    does: "Composes the queue into the production pipeline factory and migrates its composition-absence assertion; the bootstrap drain keeps inline Phase 1 admission (daemon-mode routing is serve-merge-admission).",
    records: "merge.compose_pipeline(ctx, *, escalate) -> MergeQueue, no default consumer; runner.bind(checkout, llm) builds a sync no-op Callable[[Event], None] and calls compose_pipeline once after building ctx; pipeline/bind/Dispatch signatures and Checkout unchanged; fresh queue pending: {}, active: None, paused: false, red_stems: []; offer(ticket, *, attempt), process() -> list[StageResult | ConflictHandoff]; existing queue signals and merge.write_squash unchanged; no new record",
    observable: "The queue is reachable from the CLI and built by the real pipeline factory with the pipeline's context and consumer, with no offer, git work, event or task at construction, while run/drain still call inline merge(ctx, ticket, *, attempt).",
    tests: "tests/test_daemon_composition.py (test_production_pipeline_composes_merge_queue, test_composed_merge_queue_admits_reviewed_candidate); tests/test_mergequeue.py (test_merge_queue_is_reachable_from_production); tests/test_merge.py"}
  rework-activation: {deep: true, cite: ["9.2", "9.5", "11.3", "11.4", 10], depends: [merge-queue-activation],
    fence: ["tempo/daemon.py", "tempo/rework.py", "tempo/mergequeue.py", tests/test_rework.py, tests/test_daemon_composition.py],
    does: "Rework consumes handoffs on the lock-owning writer; split stops routing to the Reject queue.",
    records: "daemon.py consumes ConflictHandoff after MergeQueue.process unwinds and calls the shared runner.py terminal writer: STATE_TRANSITION {to: gate_failed, stage: merge, reason: sorted unique handoff codes joined with commas, or gate_failed}; producing terminal keeps {to, stage, reason}; update -> dispatch: retry; split -> no dispatch/routed/rung, supersedes map before terminal, separate {to: rejected} retirement after, dispatch returns rejected; escalate -> dispatch: escalate + rung: {tier, effort} via next_rung, or dispatch: reject_queue + routed: reject_queue when exhausted; refusal/failed publication/spent cap -> both Reject markers; over-bound keeps to: premise_failed, render_over_bound, no markers; re-offer draws {cap: retry, ticket_sha, rung} before running; reuses supersedes_maps, successor_leaves, settled_dependencies, dead_dependencies",
    observable: "Diagnosis split and the 11.3 over-bound route reach reviewed Rework instead of the Reject queue, handoffs are consumed by the lock-owning writer after the queue unwinds, and drain's return equals the latest state_transition.to.",
    tests: "tests/test_daemon_composition.py (test_production_consumes_conflict_handoff_after_unwind); tests/test_drain.py (test_rework_split_preserves_dispatch_terminal_invariant); tests/test_ladder.py (test_split_dispatches_reviewed_rework); tests/test_rework.py; tests/test_scheduler.py; tests/test_reject_queue.py; tests/test_diagnose.py"}
  background-consumers: {cite: ["9.1", "9.4", "12.2"], fence: ["tempo/daemon.py", tests/test_daemon_tasks.py],
    does: "Owned watcher, merge-queue, and box-consumer tasks (DaemonTasks).",
    records: "daemon.DaemonTasks(watcher, merge_queue, box_consumer): three named zero-arg async callbacks plus async run(); memory-only tasks tuple, empty before start and after cleanup; one task per callback, invoked exactly once per run, created inside the owned task; overlapping run refused before any callback; a failure cancels siblings and awaits cleanup before propagating; wraps Watcher.run(changes), MergeQueue.process(), triage_pass(checkout, llm) unchanged; no new record or restart policy",
    observable: "An explicit run drives all three consumers concurrently and owns them to the end with no detached task or unobserved exception, while production graph construction and CLI run/drain never construct or run DaemonTasks.",
    tests: "tests/test_daemon_tasks.py (test_run_owns_three_concurrent_consumers, test_consumer_failure_cancels_and_awaits_siblings, test_background_consumers_are_dormant)"}
  control-inbox: {cite: [20, "6.2", "6.4"], depends: [background-consumers],
    fence: ["tempo/control.py", "tempo/daemon.py", "tempo/seams.py", tests/test_control.py],
    does: "Dormant crash-safe inbox: identity-bound, exactly once, decision journaled before mutation; publication through a tested durable no-overwrite filesystem-seam operation.",
    owner: "tempo/control.py: control requests, publication, validation, consumption",
    records: "<state_dir>/control/inbox/<request_id>.json = {request_id: str, lifecycle_id: str, verb: pause | resume | kill, hold_id: str | null} (filename-safe id; hold_id only for resume); new FileSystem.publish(path, data: bytes): atomic no-overwrite (FileExistsError); CONTROL_DECISION = \"control_decision\": SIGNAL null ticket/key {kind: control_decision, request_id, lifecycle_id, verb, hold_id, decision: accepted | stale | rejected, reason} appended before application; request-id fold = exactly-once authority; pause hold id = request id",
    observable: "Each request gets at most one durable decision before its idempotent application, stale requests mutate nothing, crashes lose nothing; CLI run/drain never touch the inbox.",
    tests: "tests/test_control.py (test_publication_is_durable_and_never_overwrites, test_decision_precedes_application, test_request_is_decided_once)"}
  dispatch-pause-boundary: {cite: [20, "9.1", "11.1"], fence: ["tempo/daemon.py", "tempo/drain.py", tests/test_daemon_pause.py, tests/test_drain.py],
    does: "Pause precedes ALL durable dispatch accounting, the drain's retry-cap draw included.",
    records: "Optional zero-arg async before_dispatch callback on DaemonAdmission.dispatch and the drain, awaited inside the slot before task/snapshot and before _select's machine keep and _run_one's accounting; preserved: caps.consume {cap: retry, ticket_sha, rung: {tier, effort}}, _run_one {to: running, ticket_sha}, _select {signal: reject_verdict, verdict: keep, actor: machine}",
    observable: "With an explicit checkpoint no keep, retry draw, running, snapshot or callback occurs while paused; eligibility/budget re-read after release; CLI run/drain supply none.",
    tests: "tests/test_daemon_pause.py (test_pause_checkpoint_precedes_dispatch_and_snapshot, test_dispatch_pause_boundary_is_dormant); tests/test_drain.py (test_pause_precedes_retry_cap_draw)"}
  pause-resume-activation: {cite: [20, "6.4", "9.1"], depends: [dispatch-pause-boundary],
    fence: ["tempo/daemon.py", "tempo/control.py", "tempo/drain.py", "tempo/__main__.py", tests/test_daemon_pause.py, tests/test_control_cli.py, tests/test_daemon_composition.py],
    does: "CLI pause/resume route through the inbox while an engine holds the lock; direct locked behavior unchanged.",
    records: "CLI pause/resume publish inbox requests to a live engine; discovery <state_dir>/control/active.json = {lifecycle_id: nonempty str, hold_id: nonempty str | null} or null when retired, via FileSystem.write after lock, after each decision, b\"null\\n\" before lock release; idle: take the lock, exit 0 'nothing running to pause' / 'nothing running to resume', write nothing",
    observable: "With a live drain, CLI pause/resume publish requests without journal writes and the drain holds all next-offer accounting until a matching resume; idle verbs no-op.",
    tests: "tests/test_control_cli.py (test_pause_resume_apply_directly_under_lock); tests/test_daemon_composition.py (test_live_drain_pause_blocks_all_offer_accounting, test_control_lifecycle_cleanup_on_exit_and_handoff); tests/test_daemon_pause.py; tests/test_control.py"}
  admission-holds-activation: {deep: true, cite: [20, "9.4", "9.5", "13.5"], depends: [pause-resume-activation],
    fence: ["tempo/mergequeue.py", "tempo/merge.py", "tempo/__main__.py", eval/shakeout/bench.py, tests/test_mergequeue.py, tests/test_merge.py, tests/test_daemon_composition.py],
    does: "Red-streak and tree-hash holds activate with identity-bound resume over ONE shared inbox: the CLI root shares it between the drain control factory and the pipeline factory, every direct compose_pipeline caller supplies it, no fallback constructs a second; minimal MergeQueue construction stays valid.",
    records: "RED_STREAK/TREE_MISMATCH bodies gain hold_id = uuid4().hex: {kind: merge_red_streak, stems, limit: 3, hold_id}, {kind: merge_tree_mismatch, checked_tree, main_tree, hold_id}, journaled before paused/hold_id, consumer.hold(hold_id), escalate(event); MergeQueue.resume() removed, release at process() when hold_id in consumer.projection.released_hold_ids; compose_pipeline(ctx, *, escalate, control); new final field Checkout.control: PauseConsumer | None = None, set by main via replace(checkout, control=build_control(checkout)) before pipeline(checkout) for run/drain only; build_daemon_core calls build_control once; drain/compose_pipeline refuse an absent consumer; PauseConsumer.admission: str | None; write_active(state_dir, projection, fs, *, hold_id): pause_id, else unreleased admission slot, else null",
    observable: "Holds from the real factory-returned queue release only on an identity-bound accepted resume over the ONE shared PauseConsumer per run/drain root; pause and admission holds stay independent; inline admission never holds.",
    tests: "tests/test_mergequeue.py (test_admission_hold_signal_precedes_projection); tests/test_daemon_composition.py (test_production_shares_one_admission_control_consumer, test_composed_admission_holds_accept_identity_bound_resume); tests/test_control_cli.py; tests/test_drain.py; tests/test_merge.py"}
  kill-signal-journal: {cite: [20, "6.2"], fence: ["tempo/control.py", "tempo/daemon.py", tests/test_kill_signal_journal.py],
    does: "Dormant kill-signal journaling.",
    records: "Reuses <state_dir>/control/inbox/<request_id>.json {request_id, lifecycle_id, verb: pause | resume | kill, hold_id}; kill = verb: kill, null hold_id, nonempty target lifecycle; CONTROL_DECISION = \"control_decision\" SIGNAL (null ticket/key) {kind: control_decision, request_id, lifecycle_id, verb, hold_id, decision: accepted | stale | rejected, reason} appended/fsynced before application; accepted current-lifecycle kill latches ControlProjection.kill_requested: bool (initially false, never cleared by pause/resume); prior-lifecycle kill is stale; request-id fold is exactly-once; no applied signal added",
    observable: "Through the dormant daemon.py boundary a matching kill's decision is durable before its idempotent callback, stale kills change nothing, duplicates/crashes yield one decision; CLI run/drain never bind kill application.",
    tests: "tests/test_kill_signal_journal.py (test_kill_decision_precedes_projection, test_kill_decision_is_once, test_kill_signal_journal_is_dormant)"}
  kill-executor-abort: {cite: ["6.3", "6.7", 20], depends: [kill-signal-journal],
    fence: ["tempo/daemon.py", "tempo/driver.py", tests/test_kill_executor_abort.py], does: "Dormant executor abort atomicity.",
    records: "New async Driver.abort_current() (construction/run signatures unchanged): calls sync LLM.abort_current() BEFORE cancelling the active invocation and call/timer futures, awaits unwind, clears in-memory ownership, no-op when idle; daemon.py dormant executor-abort boundary awaits that unwind and owns no worker cancellation; timeout path reuses the same cleanup; LLM effect envelopes, keys, spools, cost, redaction unchanged; an interrupted intent is never closed by a fabricated completion or terminal; writes no journal record, cap or applied signal",
    observable: "An explicit abort stops the writer before cancellation, waits for unwind under repeated aborts and waiter cancellation, propagates cancellation rather than timeout or success; never invoked by CLI run/drain.",
    tests: "tests/test_kill_executor_abort.py (test_executor_abort_stops_writer_before_cancellation, test_executor_abort_waits_for_unwind, test_executor_abort_is_dormant)"}
  kill-worker-stop: {cite: [20], fence: ["tempo/daemon.py", tests/test_kill_worker_stop.py],
    does: "Dormant worker stop ordering: executor unwinds before worker cancel.",
    records: "Reads ControlProjection.lifecycle_id and kill_requested folded from accepted current-lifecycle CONTROL_DECISION = \"control_decision\" signals ({kind: control_decision, request_id, lifecycle_id, verb, hold_id, decision: accepted|stale|rejected, reason}); only a matching accepted kill authorizes stop; writes nothing durable; stop ownership is in-memory",
    observable: "Explicit invocation awaits executor abort before cancelling any worker, then retrieves every worker outcome; dormant: real CLI run/drain and the production-composition harness never bind or invoke it.",
    tests: "tests/test_kill_worker_stop.py (test_executor_unwinds_before_worker_cancel, test_worker_stop_requires_matching_kill, test_kill_worker_stop_is_dormant)"}
  kill-failure-suppression: {cite: [20, "11.2"], depends: [kill-worker-stop], fence: ["tempo/daemon.py", tests/test_kill_failure_suppression.py],
    does: "Dormant post-kill failure-path suppression.",
    records: "daemon.py dormant worker-failure observation boundary over kill-worker-stop's worker tasks; reads ControlProjection.lifecycle_id: str and kill_requested: bool folded from accepted CONTROL_DECISION = \"control_decision\" signals {kind, request_id, lifecycle_id, verb, hold_id, decision: accepted | stale | rejected, reason}; only an accepted kill for the observer's own lifecycle suppresses; in-memory latch, no durable flag or counter; writes no decision, applied signal, terminal, cap or box message",
    observable: "After a matching accepted kill, worker outcomes are retrieved without the failure callback (no retry, diagnosis, escalation or dispatch); unmatched failures keep the ordinary path, abort failures propagate; CLI run/drain never bind it.",
    tests: "tests/test_kill_failure_suppression.py (test_post_kill_worker_failures_are_suppressed, test_worker_failures_without_matching_kill_are_preserved, test_kill_failure_suppression_is_dormant)"}
  kill-cli-activation: {deep: true, cite: [20, "18.1"],
    fence: ["tempo/daemon.py", "tempo/stages.py", "tempo/drain.py", "tempo/__main__.py", tests/test_kill_signal_journal.py, tests/test_kill_executor_abort.py, tests/test_kill_cli_activation.py],
    does: "Adds the kill verb against the live bootstrap drain per section 20; Stages exposes only its Driver abort; worker-stop and failure-suppression stay dormant until serve-activation. Other kill tests are preservation suites.",
    records: "daemon.py owns KILL_APPLIED = \"kill_applied\": EventType.SIGNAL, null ticket/key, body {kind: kill_applied, request_id, lifecycle_id}, once per accepted kill after executor abort and dispatch cleanup; drain Report.killed: bool = False, kill exits EXIT_TICKET = 1, idle kill refuses EXIT_REFUSED = 2; StageContext.abort_current() and TicketWriter.abort_current() forward to Driver.abort_current()",
    observable: "Against a live bootstrap drain, CLI kill publishes one bound inbox request; the drain journals the decision, latches stopping before all offer accounting, unwinds the production Driver, then records kill_applied before unlock.",
    tests: "tests/test_kill_cli_activation.py (test_live_drain_kill_reaches_production_driver, test_kill_stops_before_all_offer_accounting, test_kill_application_is_once_and_identity_bound); tests/test_kill_signal_journal.py; tests/test_kill_executor_abort.py"}
  heartbeat: {cite: ["9.7", "15.2", "13.5"], fence: ["tempo/heartbeat.py", "tempo/daemon.py", tests/test_heartbeat.py],
    does: "Heartbeat file plus external heartbeat.", owner: "heartbeat.py: heartbeat file writer and external freshness check",
    records: "Empty-bytes file <state_dir>/heartbeat written via FileSystem.write once per explicitly requested healthy cycle; mtime is the liveness evidence; no journal record or config key; daemon.py cycle boundary takes four named zero-arg bool callbacks workers, merge_queue, box_consumer, watcher and returns True only after a write; external check(path, mtime reader -> aware datetime | None, Clock, positive timedelta max_age) is True iff 0 <= clock() - mtime <= max_age",
    observable: "A healthy cycle refreshes the file and an unresponsive component stops refreshes so the external check turns unhealthy; dormant: real CLI run/drain and the production-composition harness never invoke the cycle or create the file.",
    tests: "tests/test_heartbeat.py (test_healthy_cycle_refreshes_heartbeat, test_external_heartbeat_freshness, test_heartbeat_is_dormant)"}
  restart-timers: {deep: true, cite: ["6.2", "6.3", "11.2", "15.1", "18.1"],
    fence: ["tempo/restart.py", "tempo/timers.py", "tempo/daemon.py", "tempo/__main__.py", tests/test_restart_timers.py],
    does: "Restart-reconcile reaps orphans to abandoned, orphan sweeper, journaled timers armed, persisted, and re-armed at startup -- reap and re-arm before dispatch in the CLI/serve composition roots.",
    owner: "restart.py: startup coordinator and idle orphan sweep; timers.py: Timers projection and writes",
    records: "Timers is sole writer of EventType.TIMER_ARMED / TIMER_FIRED, body {timer_id: nonempty str, deadline: str}, ticket str | null, null key; deadline = aware UTC isoformat +00:00; fold Journal.read(): armed without fired = pending; append/fsync before visibility; due when clock() >= deadline, in (deadline, timer_id) order",
    observable: "Explicit lock-held startup via build_daemon_core awaits reconciliation, re-arms timers and fires overdue deadlines before the first dispatch; the idle sweeper never reaps owned work.",
    tests: "tests/test_restart_timers.py (test_production_restart_precedes_dispatch, test_timers_rearm_from_journal, test_orphan_sweep_preserves_live_ownership)"}
  flake-detection: {cite: ["11.5", "12.1", "12.5", "13.5"], fence: ["tempo/flake.py", "tempo/daemon.py", tests/test_flake.py],
    does: "Section 11.5 detection and quarantine through a dormant daemon composition hook.",
    owner: "flake.py: flake detection and quarantine projection",
    records: "Files Box.enqueue(failure_report, origin=test_id, stage=check, outcome=gate_failed), then appends null-ticket signal key flake/<box_id>, body {kind: flake_detected, test_id, signature, box_id}; projection folds release key flake-release/<box_id>/<fix_stem> {kind: flake_released, test_id, signature, box_id, fix_stem}; cap Config.caps.quarantine (shipped 5) counts active tests; crossing appends no detection and calls the escalation callback",
    observable: "Explicit detection files the report and returns a durable quarantine projection; dormant: real CLI run/drain and the production-composition harness never call the hook.",
    tests: "tests/test_flake.py (test_detection_signal_and_quarantine, test_quarantine_cap_boundary, test_flake_detection_is_dormant)"}
  flake-release: {cite: ["11.5", "12.6", "13.5"], depends: [flake-detection], fence: ["tempo/flake.py", "tempo/daemon.py", tests/test_flake.py],
    does: "Section 11.5 identity-bound mechanical release.",
    records: "Reads detection key flake/<box_id>; Box.get(box_id) must match id/signature/origin; fix identity is the message's status: resolved with resolution: {kind: ticket, link: <fix_stem>}; requires STATE_TRANSITION ticket: <fix_stem> to: merged plus caller-supplied named green re-run evidence; appends EventType.SIGNAL, ticket null, key flake-release/<box_id>/<fix_stem>, body {kind: flake_released, test_id, signature, box_id, fix_stem} before folding out; replay is a no-op",
    observable: "Explicit release lifts only the report whose Box resolution names the merged fix with named green re-run evidence and never evicts other quarantines; dormant: real CLI run/drain and the production-composition harness never call the hook.",
    tests: "tests/test_flake.py (test_release_requires_matching_merge_and_named_green_rerun, test_release_signal_is_write_ahead, test_flake_release_is_dormant)"}
  journal-roll: {cite: ["6.2"], fence: ["tempo/journal.py", tests/test_journal_roll.py], does: "Roll at 64 MiB or 24h, engine constants.",
    records: "journal.py constants ROLL_BYTES = 64 * 1024 * 1024, ROLL_AGE = timedelta(hours=24); before an append, roll when active segment length >= ROLL_BYTES or age (injected UTC now minus first complete event ts, never mtime) >= ROLL_AGE; a crossing write stays whole, the next append rolls; new segment <six-digit zero-padded seq>-<UTC YYYYMMDD>.jsonl, seq = highest + 1, no overwrite, directory fsync; rotation writes no event; Journal(state_dir, clock), append/read/close, read_segments() unchanged",
    observable: "Rotation is synchronous inside Journal.append with no caller change, all prior records survive in order through read_segments()/read(), only the newest segment tolerates a torn tail, and startup tail repair precedes any roll.",
    tests: "tests/test_journal_roll.py (test_roll_constants_and_size_boundary, test_roll_preserves_records_and_read_laws, test_startup_tail_repair_precedes_roll)"}
  storm-ledger: {cite: ["12.5", "6.2"], depends: [journal-roll], fence: ["tempo/storm.py", tests/test_storm.py],
    does: "Dormant section 12 occurrence ledger; no trip, message, notification, or hold.",
    owner: "storm.py: occurrence writer and journal-derived window projection",
    records: "Per arrival appends EventType.SIGNAL, ticket null, key storm-occurrence/<signature>/<occurrence_id>, body {kind: storm_occurrence, signature, occurrence_id, emitting_stage, emitting_origin}; signature = producer's 64-char sha256; occurrence_id = nonblank producer-supplied, unique per arrival; same key+body replay is a no-op; STORM_WINDOW = timedelta(hours=1); projection counts distinct keys per signature with ts in (now - STORM_WINDOW, now]",
    observable: "Direct invocation records and counts arrivals once each with no trip, message or hold; dormant: tempo.storm is absent from the tempo.* import closure rooted at tempo/__main__.py.",
    tests: "tests/test_storm.py (test_occurrence_signal_shape_and_identity, test_window_boundaries_and_signature_isolation, test_storm_ledger_is_dormant)"}
  storm-producer-wiring: {cite: ["12.1", "12.5", "6.2"],
    fence: ["tempo/storm.py", "tempo/box.py", "tempo/daemon.py", tests/test_storm.py, tests/test_storm_producer.py],
    does: "Every box arrival, dedup hit included, records one stable occurrence; production stays dormant.",
    records: "Box construction gains an optional keyword-only arrival callback; Box.enqueue gains optional keyword-only occurrence_id (required nonblank when bound), return (message_id, created) unchanged; callback called exactly once per enqueue, before dedup return or queue write, kwargs exactly {signature, occurrence_id, emitting_stage, emitting_origin}; dedup hits record an occurrence without rewriting the message; daemon.py hook binds it to the lock holder's Journal and clock",
    observable: "Direct hook invocation with a real Box and Journal records each valid arrival once, dedup hits included, with no trip or hold; dormant: the production-composition harness never calls the hook.",
    tests: "tests/test_storm_producer.py (test_new_and_dedup_arrivals_record_occurrences, test_enqueue_crash_replay_finishes_once, test_storm_producer_hook_is_dormant); tests/test_storm.py"}
  storm-notification-activation: {cite: ["12.1", "12.3", "12.5", "13.5"], depends: [storm-producer-wiring],
    fence: ["tempo/storm.py", "tempo/box.py", "tempo/daemon.py", "tempo/__main__.py", tests/test_storm.py, tests/test_drain.py, tests/test_storm_notification_activation.py],
    does: "Activates the trip signal and its one P0 failure_report; migrates only the drain test's blanket no-box-event assertion to admit storm_occurrence events while still proving the drain never mutates or triages box records. No dispatch suppression.",
    records: "STORM_THRESHOLD = 5 (trip at >5); STORM_TRIP = \"storm_breaker_trip\": null-ticket signal storm-trip/<trip_id>, body {kind, signature, trip_id, first_occurrence_id, crossing_occurrence_id, emitting_stage, emitting_origin, held}; trip_id = sha256([signature, first id, crossing id]); held = stage/origin pair for implement|check|review else null; deduped Box failure_report origin storm-breaker/<trip_id>, reason \"storm breaker trip\", own id storm-report/<trip_id>; producer ids = storm-arrival/ + sha256 of compact JSON ['rework-failure',stem,attempt,stage,outcome], ['dead-dependency',stem,dependency], ['second-problem',stem,attempt,index], ['base-red',stem,attempt,stage,n], ['flake-detection',stem,attempt,test_id,index], ['bootstrap-ingest',source_digest,line_number]",
    observable: "Six same-signature production arrivals yield one trip and one P0 failure report; no dispatch hold or notification.",
    tests: "tests/test_storm_notification_activation.py (test_production_arrivals_and_dedup_hits_trip_once, test_production_occurrence_id_recipes); tests/test_storm.py; tests/test_drain.py"}
  storm-dispatch-hold: {deep: true, cite: ["12.5", 20, "9.1", "13.5", "18.1"],
    fence: ["tempo/storm.py", "tempo/control.py", "tempo/daemon.py", "tempo/drain.py", "tempo/__main__.py", tests/test_storm_hold.py],
    does: "Section 12 dispatch hold and identity-bound resume in the live drain; fences each predecessor storm test carrying a dispatch-absence assertion. Sole production owner of the hold.",
    records: "An unreleased trip with held stage in implement | check | review holds the bootstrap ticket whose stem equals emitting_origin; release = accepted inbox resume {verb: resume, hold_id: <trip_id>}, id added to ControlProjection.released_hold_ids; re-arm counts only occurrences after the release; active.json advertises pause, admission hold, then storm hold",
    observable: "The bootstrap drain excludes storm-held stems before any dispatch accounting, polls the inbox while only held work remains, and a live resume makes the ticket eligible in the same invocation.",
    tests: "tests/test_storm_hold.py (test_storm_hold_precedes_all_dispatch_accounting, test_live_resume_releases_in_same_drain, test_storm_release_rearms_on_new_arrivals_only)"}
  checkpoint-push: {cite: [10, "6.2", "6.3", "12.1"],
    fence: ["tempo/checkpoint.py", "tempo/daemon.py", "tempo/git.py", tests/test_checkpoint.py, tests/test_git.py, tests/test_mergequeue.py],
    does: "Public argv-only push in git.py (migrating only the git public-operation allowlist test); re-fires an incomplete push after restart, never duplicating a completed one.",
    owner: "checkpoint.py: trigger fold and push Effect",
    records: "CHECKPOINT_MERGES = 5, CHECKPOINT_INTERVAL = timedelta(hours=24); Effect key checkpoint-push/<window>, completion {result: {merged: <int>}}; failure signal key checkpoint-push-failed/<window>/<attempt>, body {kind: checkpoint_push_failed, window, attempt, error_code}; Box report occurrence_id=storm.arrival_id(\"checkpoint-push\", window, attempt); git.py adds async Git.push(dir, remote, branch)",
    observable: "Direct lock-held invocation pushes origin/main after five code merges or one injected day and re-fires failed pushes; dormant: CLI/drain never invokes it.",
    tests: "tests/test_checkpoint.py (test_checkpoint_success_advances_once, test_checkpoint_failed_push_refires, test_checkpoint_boundary_is_dormant); tests/test_git.py; tests/test_mergequeue.py"}
  serve-activation: {deep: true, cite: ["9.1", "9.7", "12.5", "18.1", 20],
    fence: ["tempo/serve.py", "tempo/daemon.py", "tempo/__main__.py", "tempo/triage.py", "tempo/driver.py", "tempo/runner.py", "tempo/stages.py", "tempo/scheduler.py", "tempo/storm.py", tests/test_stages.py, tests/test_daemon_admission.py, tests/test_daemon_config.py, tests/test_serve.py, tests/test_daemon_composition.py, tests/test_daemon_tasks.py, tests/test_kill_worker_stop.py, tests/test_kill_failure_suppression.py, tests/test_heartbeat.py, tests/test_storm.py],
    does: "The serve verb and continuous loop composing dispatch, watcher, merge, box, control, heartbeat, restart/timers, storm, checkpoint, and worker tasks; reconciles before dispatch, holds the lock for its lifetime, exits only via kill/signal or a terminal worker failure; activates worker-stop and failure suppression (decision before mutation, executor unwind before worker cancel); tests build the real graph in-process and replace serve-absence assertions.",
    owner: "serve.py: continuous foreground lifetime and recurring production consumers (async in-process entrypoint)",
    records: "argparse serve verb; SERVE_POLL_S = 0.1 in serve.py; one lifecycle, control inbox, journal and Timers lifetime shared by dispatch and merge; storm projection exposes each unreleased trip's held: {emitting_stage, emitting_origin} to stage selection; suspended continuation is memory-only {ticket, ctx, run, next_stage: implement | check | review} held by TicketWriter, no terminal; run_stages gains a next-stage/suspension seam; triage_pass Driver/Effects must use checkout.journal (fence adds triage.py, driver.py); CLI exit 0 kill/signal stop, 1 worker failure, 2 setup refusal",
    observable: "tempo serve holds the writer lock for its lifetime, recovers before any work, owns all production consumers, waits at an empty queue, suspends storm-held next stages, and exits only via kill/signal or terminal worker failure.",
    tests: "tests/test_serve.py (test_serve_cli_uses_production_composition, test_serve_kill_unwinds_executor_before_workers, test_serve_storm_suspends_stage_and_frees_selection); tests/test_daemon_composition.py; tests/test_daemon_tasks.py; tests/test_kill_worker_stop.py; tests/test_kill_failure_suppression.py; tests/test_heartbeat.py; tests/test_storm.py; tests/test_stages.py; tests/test_daemon_admission.py; tests/test_daemon_config.py"}
  serve-merge-admission: {deep: true, cite: ["9.4", "9.5", 10],
    fence: ["tempo/merge.py", "tempo/serve.py", tests/test_merge.py, tests/test_mergequeue.py, tests/test_serve.py],
    does: "Explicit daemon-admission mode selected only by serve composition (drain keeps inline admit, never daemon holds): a settled run passes code-lane and seed-safety prechecks, then offers exactly one candidate to the MergeQueue, which owns rebase, both rungs, regate, integration, and holds; success retires worktree/branch and journals to: merged once with the real commit and reviewed SHA; gate_failed/rework leave main and branch intact. Tests drive the real serve-selected path, never queue admit from a harness.",
    records: "merge.py admission mode closed inline | daemon (default inline; daemon requires the serve-composed MergeQueue); daemon path builds Candidate {stem, reviewed_sha, changed_files, ticket_text, review_text, seeds: list[SeedOnMain], seed_checks_text}; hard PostRebaseGate/SeedSafetyGate prechecks precede one (Ticket, attempt) offer; results stay StageResult or ConflictHandoff",
    observable: "Only the serve-selected path routes a Review-ok run through prechecks and one offer to the production MergeQueue, reporting merged only from that candidate's result; run/drain stay inline.",
    tests: "tests/test_merge.py (test_daemon_prechecks_precede_offer, test_admission_mode_is_explicit); tests/test_serve.py (test_serve_routes_settled_run_to_composed_queue_once); tests/test_mergequeue.py"}
  worker-recovery-disposition: {deep: true, cite: ["6.2", "11.2", "15.3"], fence: ["tempo/reconcile.py", tests/test_reconcile.py],
    does: "Per reaped orphan, after abandoned and before worktree removal, one run-scoped signal {kind: recovery_alert, disposition: alert, outcome: abandoned, reason}; the terminal prevents re-emission on later restarts; no box mail or routing change.",
    records: "reconcile.py owns RECOVERY_ALERT = \"recovery_alert\"; journal.py adds it to SIGNAL_NAMES; per reaped orphan: capture run_seq(journal.read(), stem) before abandonment, append {to: abandoned}, then EventType.SIGNAL ticket: <orphan stem>, key null, body exactly {kind: recovery_alert, disposition: alert, outcome: abandoned, reason: \"orphaned run\", run_seq: <int>}, then Git.worktree_remove/prune; alert append failure prevents worktree removal",
    observable: "Every reconciliation path (scaffold on-entry, Restart startup, idle sweep) emits one recovery_alert per reaped orphan between abandonment and worktree removal, never re-alerting closed history.",
    tests: "tests/test_reconcile.py (test_recovery_alert_records_producing_run, test_recovery_alert_precedes_worktree_removal, test_recovery_alert_is_once_per_reaped_run); tests/test_restart_timers.py; tests/test_vocabularies.py"}
  outbox-only-admission: {deep: true, cite: [19.L, 10, "13.4"], fence: ["tempo/stages.py", "tempo/merge.py", tests/test_stages.py, tests/test_merge.py],
    does: "The 19.L run-lane admission rule, at initial Verification and merge regate.",
    records: "Qualifying report = path under tickets/<stem>/ whose basename is in KNOWN_ARTIFACTS (not run.md), produced by Verification and lifted by checks (Effect ticket-plane/<stem>/<attempt>/checks); Admission.commit becomes NonBlank | null; run-lane admission returns commit: null, original reviewed_sha, nonblank produced_at_sha; merge Effect merge/<stem>/<attempt> returns {commit: null}; terminal STATE_TRANSITION ticket: <stem>, null key, body {to: merged, commit: null, reviewed_sha}",
    observable: "An ok run with an empty committed diff passes the empty-diff rule at initial Verification and merge regate exactly when its current checks lift carries schema-valid registered report output, via inline and daemon admission.",
    tests: "tests/test_stages.py (test_outbox_only_check_accepts_registered_report, test_outbox_only_check_requires_current_report); tests/test_merge.py (test_outbox_only_admission_records_null_commit_and_retires)"}
  daemon-soak: {deep: true, cite: [19.L, "13.4", "15.3", 4],
    fence: [eval/daemon_soak.py, "tempo/artifacts.py", "tempo/stages.py", tests/test_daemon_soak.py],
    does: "Closed daemon-soak-report.json schema (member, planted fault, observed terminal/event, disposition, producing run, auditor verdict, green), KNOWN-artifact registration, canonical ordinary-lane writer.",
    owner: "eval/daemon_soak.py: canonical write_report writer",
    records: "DAEMON_SOAK_REPORT = \"daemon-soak-report.json\", DAEMON_SOAK_MEMBERS = (\"worker_killed_mid_run\", \"conflict_resolution_rungs\", \"semantic_conflict_integration_red\"); DaemonSoakReport.entries: list[DaemonSoakEntry], three members once in order; entry {member, planted_fault, expected, observed, disposition: alert | resolved | refused, producing_run, auditor: list[str], green: bool}; expected/disposition per member: abandoned_alerted_then_merged/alert, mechanical_and_rework_main_green/resolved, integration_red_main_green/refused; green == (observed == expected and not auditor); registered in KNOWN_ARTIFACTS",
    observable: "Validation rejects malformed or false-green reports, only an all-green report reaches the writer's filesystem seam, and registration uses the ordinary checks lift.",
    tests: "tests/test_daemon_soak.py (test_daemon_soak_schema_is_closed, test_daemon_soak_green_matches_observation_and_auditor, test_daemon_soak_writer_validates_before_write)"}
  daemon-soak-runner: {deep: true, cite: ["15.3", "18.1", "9.5", "11.2", "12.1"],
    fence: [eval/daemon_soak.py, tests/test_daemon_soak.py, tests/test_daemon_soak_runner.py],
    does: "Public deterministic runner driving merged production serve in-process for the three closed members (exit reads above), deriving every field from member-local evidence and returning the report only to the writer.",
    records: "eval/daemon_soak.py adds async produce(root: Path) -> DaemonSoakReport driving tempo.serve.serve; auditor via tempo.audit.audit_journal as <invariant>: <ticket or ->: <detail>; runner files Box failure_report stage: merge with occurrence_id <producing_run>/mechanical-conflict, <fault_run>/unresolved-conflict, <producing_run>/semantic-integration-red; sole command uv run python -m eval.daemon_soak --out <path>",
    observable: "The runner drives the merged production serve graph in-process for all three members, advancing each injected clock 24 hours, and re-derives a byte-identical all-green report.",
    tests: "tests/test_daemon_soak_runner.py (test_daemon_soak_runs_production_serve, test_daemon_soak_requires_member_local_evidence, test_daemon_soak_is_rederivable); tests/test_daemon_soak.py"}
  soak-run: {cite: [19.L, "13.4", "15.3"], fence: [tickets/soak-run/daemon-soak-report.json],
    does: "No code: invokes the merged runner and writer, leaving the report in its OUTBOX.",
    records: "Produces exactly tickets/soak-run/daemon-soak-report.json (DaemonSoakReport, three ordered green entries, empty auditor lists, real HEAD provenance) via Verification command uv run python -m eval.daemon_soak --out tickets/soak-run/daemon-soak-report.json; left uncommitted for the checks lift ticket-plane/soak-run/<attempt>/checks; settles via the run-lane merge Effect with one {to: merged, commit: null, reviewed_sha} terminal; no code, schema, test or signal",
    observable: "A green run has an empty committed code diff and a schema-valid current-run report lifted by the ordinary lane, retiring once; the Phase 3 exit reads it through its schema.",
    tests: "tests/test_daemon_soak.py (test_daemon_soak_command_writes_report_only_on_green); tests/test_daemon_soak_runner.py (test_daemon_soak_is_rederivable); tests/test_stages.py; tests/test_merge.py"}
  phase3-exit: {exit: true, fence: [tickets, tests/test_phase3_exit.py, tests/test_seeded_phase4_core.py],
    does: "Reads the committed soak report's green members through its artifacts schema; authors the 19.P4 core."}
# END_REGISTRY_P3
```

### 19.P4 Phase 4 -- Reliability and providers

- *Deliverables:* the stuck/spiral watchdog as a staged chain -- the in-flight adapter EVENT STREAM (per-tool-call JSONL events surfaced at the driver/provider boundary to a consumer seam; spool capture, cleanup, and the post-terminal harvest reader unchanged); the NOTIFY transport (section 15 `Notifications.notify(argv)` seam running the config-declared argv command as an Effect with section 6's key domains and conservative re-send reconcile -- the ONE transport section 13 names; its merge IS the production flip for Phase 3's escalation emitters, storm breaker and red streak); the spend-without-progress DETECTOR + hard-timeout wiring (token/cost accumulated from the event stream since the last OBSERVED mutation to a `## Scope fence` path across section 9's three regions -- a tool-call request is never a mutation; a stream reporting no usage charges `limits.est_cost_per_call_usd` at call start; trips at spend exceeding 3x the serving row's per-call basis, an engine constant; ONE run-seq-keyed notification, the stuck alert run-BLIND; process-group kill on stuck); the ACTIVATION flipping EVERY production driver call, review included, to the watched path. Provider cooldown Timers and failover over the CONFIGURED candidates (`providers.py` owns ordered selection and served-identity stamps; `timers.py` + the journal own cooldown state; section 6's one-payload-per-session rule). The reliability battery and its run.
- *Exit reads (committed artifacts only):* every member green in `tickets/reliability-run/reliability-battery-report.json` (schema `ReliabilityBatteryReport` in `tempo/artifacts.py`), member order CLOSED: `classified_quota_exhaustion` (quota classified, cooldown deadline persisted), `all_candidates_cooling_recovery` (ordered candidate exhaustion, the all-cooling hold, the matching `timer_fired` recovery, the recovered served identity), `unclassified_failure_preservation` (an unknown failure stays `unclassified`), each run over the battery's multi-candidate FIXTURE registry and green only when observed equals expected and the member-local journal passes the auditor (emitters: machinery `reliability-battery`, producer `reliability-run`). The spiral soft-band notification (fired once, no auto-kill, its notify intent/completion pair in the run journal) and the hard-timeout group kill are obligations of `watchdog-detector`/`watchdog-activation` and their merged production-composition tests -- the hard-timeout test asserting the killed run is harvested -- read from `watchdog-activation`'s committed passing `checks.json`.

```yaml
# BEGIN_REGISTRY_P4
phase: 4
admissions:
  - [watchdog-event-stream, notify-transport]
  - [watchdog-detector, watchdog-activation]
  - [provider-cooldown-failover]
  - [reliability-battery]
  - [reliability-run]
  - [phase4-exit]
seeds:
  watchdog-event-stream: {cite: ["6.11", "6.5", "9.7", "15.2"],
    fence: ["tempo/watchdog.py", "tempo/providers.py", "tempo/seams.py", tests/test_watchdog.py, tests/test_providers.py],
    does: "Dormant in-flight event callback through the process-exec seam to a watchdog consumer seam.",
    owner: "tempo/watchdog.py: per-call watchdog event consumer seam",
    records: "group-exec call gains an optional synchronous stdout-line callback (default absent): complete lines in order while the child runs, final unterminated line at EOF, captured stdout/stderr unchanged; tempo/providers.py forwards redactor-scrubbed decoded JSON objects to the per-call consumer, non-JSON and non-object lines excluded; forwarded shapes: Claude type: tool_use blocks and result usage/total_cost_usd, Codex tool-call items and turn.completed usage; tool-call identity and event order preserved; adapter stays sole writer of prompt.md, events.jsonl, stderr.txt; no new journal event, counter or spool",
    observable: "Directly constructed consumers get ordered events before call completion; production calls stay unwatched and timeout/cancel/callback failure unwinds via the existing group-kill path.",
    tests: "tests/test_watchdog.py::test_event_consumer_receives_before_terminal, tests/test_watchdog.py::test_event_stream_is_dormant, tests/test_providers.py::test_event_callback_unwind_kills_group"}
  notify-transport: {cite: ["6.3", "13.5", "15.2", "12.5", "9.5"], depends: [watchdog-event-stream],
    fence: ["tempo/notify.py", "tempo/config.py", "tempo/seams.py", "tempo/serve.py", "tempo/__main__.py", tests/test_notify.py, tests/test_config.py, tests/test_serve.py],
    does: "Notifications seam, notify config parse, key domains and reconcile; serve reconciles Effect-backed notifications at startup and each poll, pushing storm and red-streak escalations; unset config warns once and stays status-only.",
    owner: "tempo/notify.py: notification transport, keys, escalation reconcile",
    records: "config notify: optional nonempty argv list (absent = push off; explicit null or malformed argv refused); Notifications.notify(argv) runs as an Effects.run action with host-root cwd, bounded timeout, child_env(..., serving=None); nonzero exit or missing executable = failed delivery; key notify/<ticket-or-event-owner>/<escalation>/<event-identity> (attempt- and run-blind) except notify/<stem>/spiral-warning/<run-sequence>; completion suppresses re-send, intent without completion re-sends; reads kind: storm_breaker_trip (trip_id), merge_red_streak (hold_id, stems, limit), merge_tree_mismatch (hold_id, checked_tree, main_tree)",
    observable: "serve reconciles pending escalations at startup and each poll, pushing once per key; unset notify warns once and invokes nothing.",
    tests: "tests/test_notify.py::test_notify_effect_once_and_conservative_resend, tests/test_notify.py::test_notify_key_domains, tests/test_serve.py::test_serve_reconciles_notifications_at_startup_and_poll, tests/test_config.py"}
  watchdog-detector: {cite: ["9.7", "13.3", "6.3", "6.9"], fence: ["tempo/watchdog.py", "tempo/notify.py", tests/test_watchdog.py],
    does: "Dormant spend-without-progress detector and hard-timeout wiring.",
    records: "reads ticket expected_minutes, stuck_minutes, scope_fence; SPIRAL_SPEND_MULTIPLIER = 3 engine constant in tempo/watchdog.py; run-local spend/latch state survives re-prompts, resets per run; progress = observed create/change/delete of a fenced path; basis fixed at call start = first completed metered total for that identity this run, else limits.est_cost_per_call_usd charged once, else calibration (no spend verdict); trips when spend strictly exceeds 3 x basis; healthy below expected x 1.5, soft to stuck, hard deadline exactly stuck_minutes; cap-wait excluded; notify keys spiral-warning (run seq), stuck-past-threshold (run-blind); no new journal record",
    observable: "A directly constructed detector notifies once per run in the soft band without killing and at the hard deadline calls LLM.abort_current into the existing timeout outcome; production stays unwatched.",
    tests: "tests/test_watchdog.py::test_spend_metering_and_threshold, tests/test_watchdog.py::test_watchdog_regions_and_notify_once, tests/test_watchdog.py::test_detector_is_dormant, tests/test_notify.py"}
  watchdog-activation: {cite: ["9.7", "6.7", "11.2"], depends: [watchdog-detector],
    fence: ["tempo/watchdog.py", "tempo/driver.py", "tempo/stages.py", "tempo/notify.py", "tempo/drain.py", "tempo/serve.py", "tempo/__main__.py", tests/test_watchdog.py, tests/test_watchdog_activation.py, tests/test_daemon_composition.py, eval/shakeout/providers.py, eval/daemon_soak.py],
    does: "Every production driver call watched; proves soft-band notify-once and hard-timeout group kill through the production composition.",
    records: "detector run context threaded to every production Driver call; ticket-owned expected time from the ticket, never derived; ticketless/eval-owned: expected_seconds = stuck_budget / 2, expected_minutes = expected_seconds / 60, stuck_minutes = stuck_budget / 60; AUTHOR_STUCK_S = 900.0 (450/675 s), TRIAGE_STUCK_S = 600.0 (300/450 s), baseline STUCK_BUDGET_S = 1200.0, diagnose stuck_budget = min(CALL_STUCK_S, left), CALL_STUCK_S = 600.0; eval synthetic ticket ids kept; StageResult(outcome=\"timeout\") harvested to tickets/<stem>/attempts/<attempt>/harvest.json",
    observable: "Every production model call is watched through the real composition: one soft-band notification per run, no auto-kill, group kill at the hard deadline into the unchanged harvest path.",
    tests: "tests/test_watchdog_activation.py::test_every_production_driver_surface_is_watched, tests/test_watchdog_activation.py::test_eval_owned_budgets_preserve_synthetic_identities, tests/test_daemon_composition.py::test_production_watchdog_shares_active_executor, tests/test_watchdog.py, tests/test_notify.py"}
  provider-cooldown-failover: {deep: true, cite: ["6.10", "6.11", "6.9", "6.2", "15.2"],
    fence: ["tempo/providers.py", "tempo/watchdog.py", "tempo/timers.py", "tempo/runner.py", "tempo/restart.py", "tempo/daemon.py", "tempo/stages.py", "tempo/merge.py", "tempo/drain.py", "tempo/serve.py", "tempo/__main__.py", eval/shakeout/bench.py, eval/daemon_soak.py, tests/test_provider_cooldown_failover.py, tests/test_providers.py, tests/test_restart_timers.py, tests/test_stages.py, tests/test_serve.py, tests/test_merge.py, tests/test_mergequeue.py, tests/test_daemon_composition.py, "tempo/audit.py", tests/test_audit.py, "tempo/journal.py", tests/test_journal.py],
    does: "Section 6 cooldown/failover with one registry/cooldown payload and one Timers lifetime per lock-held session, threaded to every provider-client construction site.",
    records: "reads limits.concurrency, limits.quota_window_minutes (default 60); projected_wait_seconds = ((u + q) // c) * t from the one shared Thresh (u reserved slots, q earlier FIFO waiters, c cap), t = runner.call_timeout(snapshot) = snapshot.drain.max_ticket_minutes * 60; after a durable auth_error provider_call_outcome the provider is excluded in session memory until a fresh session's successful preflight; quota_exhausted arms Timer provider-cooldown/<provider name>/<deadline> with existing timer_armed/timer_fired body {timer_id, deadline} (aware-UTC ISO), ticket: null, key: null, via Timers only; drought park state_transition to: infra_error, reason: provider_drought, provider_drought: {tier: low | medium | high | max, surface, providers}",
    observable: "Production calls skip cooling, open or auth-excluded candidates in configured order with FIFO waits and the strict >60 s spill rule, fail over only on a later call, hold cost-free until the earliest journaled Timer fires; one payload and Timers per session.",
    tests: "tests/test_provider_cooldown_failover.py::test_ordered_failover_preserves_served_identity, tests/test_provider_cooldown_failover.py::test_all_candidates_cooling_recovers_on_timer_fired, tests/test_provider_cooldown_failover.py::test_auth_failure_excludes_provider_until_fresh_successful_preflight, tests/test_providers.py, tests/test_thresh.py, tests/test_restart_timers.py, tests/test_stages.py, tests/test_serve.py, tests/test_merge.py, tests/test_mergequeue.py, tests/test_daemon_composition.py, tests/test_watchdog.py"}
  reliability-battery: {cite: ["6.10", 10, "13.4"],
    fence: [eval/reliability_battery.py, "tempo/artifacts.py", "tempo/stages.py", tests/test_reliability_battery.py],
    does: "Fault injection, closed report schema and registration, writer, and runner for the three closed members.",
    owner: "eval/reliability_battery.py: produce(root) runner and write_report(path, report, fs)",
    records: "tempo/artifacts.py: RELIABILITY_BATTERY_REPORT = \"reliability-battery-report.json\", RELIABILITY_BATTERY_SPEC_VERSION = 1, RELIABILITY_BATTERY_MEMBERS = (classified_quota_exhaustion, all_candidates_cooling_recovery, unclassified_failure_preservation); entry {member, planted_fault, expected, observed, producing_run, auditor, green}, green == (observed == expected and not auditor); expected quota_classified_cooldown_persisted, all_cooling_held_timer_fired_recovered, unknown_failure_unclassified; KNOWN_ARTIFACTS[RELIABILITY_BATTERY_REPORT] registered; uv run python -m eval.reliability_battery --out <path>",
    observable: "produce(root) runs all three members through the merged production composition; the command writes only an all-green report, else exits nonzero.",
    tests: "tests/test_reliability_battery.py::test_reliability_battery_schema_is_closed, tests/test_reliability_battery.py::test_reliability_battery_command_writes_only_on_green, tests/test_stages.py, tests/test_audit.py"}
  reliability-run: {cite: [10, "13.4", "6.10"], fence: [tickets/reliability-run/reliability-battery-report.json],
    does: "No code: runs the merged battery once into its OUTBOX.",
    records: "produces exactly tickets/reliability-run/reliability-battery-report.json (ReliabilityBatteryReport under RELIABILITY_BATTERY_REPORT) via Verification uv run python -m eval.reliability_battery --out tickets/reliability-run/reliability-battery-report.json; all three members green with empty auditor lists; left uncommitted, never hand-copied; lifted only through ticket-plane/reliability-run/<attempt>/checks; empty code-diff admission records {to: merged, commit: null, reviewed_sha: <reviewed SHA>}; no new record or lift path",
    observable: "The producer settles with an empty committed code diff, a fresh all-green report lifted by the ordinary ticket-plane lane and one retirement, which the Phase 4 exit reads at that destination.",
    tests: "tests/test_reliability_battery.py::test_reliability_battery_command_writes_only_on_green, tests/test_merge.py::test_outbox_only_admission_records_null_commit_and_retires, tests/test_merge.py::test_outbox_only_check_requires_current_report, tests/test_stages.py"}
  phase4-exit: {exit: true, fence: [tickets, tests/test_phase4_exit.py, tests/test_seeded_phase5_core.py],
    does: "Reads the committed report; authors the 19.P5 core."}
# END_REGISTRY_P4
```

### 19.P5 Phase 5 -- Sidecar + self-improvement (slim)

- *Deliverables:* the retro stage (section 14 count/time/signal triggers, committed retro report + window boundary) WITH its bootstrap-era INVOKER -- the drain itself, because nothing else can fire a retro before cutover (`serve` is daemon-era, the `retro` verb is an operator surface, and no run lane can produce `tickets/retro/<seq>.md`); WITH it the box's tombstone auto-reopen and retro itemization (section 12); the per-surface catch scorecard (the escape column ships in Phase 6), spend/calibration reporting, gate earn/prune reporting, bypass aggregation, and backlog hygiene (section 14's kept core); the status projection (section 13 touchpoint 5); the GO-baseline BINDING READER (section 12); the `retro` verb; `doctor`.
- *Settled contracts:*
  - INVOKER: an UNFORCED due check at the top of every drain dispatch iteration selects a trigger first (outside the failure-capturing block; none due returns with no signal): at least N=25 merged tickets in the window, a window at least M=7 days old, or one signal kind reaching S=5 in the window (per-kind identities exactly: a used non-empty `gate_bypass`; a Rework invocation; a terminal `state_transition` to `gate_failed`; an admission whose conflict facts carry integration-red paths). It runs ONE FORCED retro at quiescence and immediately before any phase-exit ticket dispatches, whenever a merge landed after the latest completed report. It runs in the MAIN checkout under the lock the drain holds, through a hook wired only from the drain entrypoint (default-off for direct `Drain` construction; the self-upgrade child re-wires it). The retro is an `LLMStage` named `retro` emitting a closed local `RetroArtifact` from `specs/retro.md`, run by the existing Driver with the session's shared provider payload; the Driver reaches the hook through a public read-only `Stages.driver` via `Pipeline.stages`, never private attributes or globals, and a missing Driver raises `RetroConstructionError` with a paved road. The hook writes `tickets/retro/<seq>.md` (zero-padded; `retro` is a reserved non-stem directory) and commits it through the ticket-plane Git/Effects seam, effect key `retro/<seq>`, whose completion is the window boundary -- no OUTBOX lift, no KNOWN-artifact registration. A failure after a trigger is selected appends one `signal` keyed `retro-failed/<window-boundary>/<trigger>` (body `{kind: retro_failed, window_boundary, trigger, error_code}`), enqueues ONE deduped `failure_report`, and suppresses every further attempt in that window until a later merge or a later `retro/<seq>` completion.
  - RETRO -> BOX: one `retro_finding` per proposal carrying `retro_report_key`, `fixed_failure`, `overcorrection_risk`, `proposed_spec_paths`, with box origin `retro-proposal/<retro_report_key>/<proposal_id>` (`proposal_id` = first 16 hex of sha256 over the canonical fields); tombstone re-reports per section 12; authoring a ticket from a `retro_finding` appends one bridge `signal` keyed `retro-ticket/<stem>` (`{kind: retro_ticket_authored, box_id, retro_report_key, ticket_stem}`); at successful admission of a `source: box:retro_finding` ticket changing any `specs/*.md`, merge appends one `signal` keyed `retro-prompt-spec-change/<stem>/<squash-sha>` (`{kind: retro_prompt_spec_change_merged, box_id, retro_report_key, ticket_stem, squash_sha, changed_spec_paths}`); a missing or ambiguous bridge is a hard merge finding.
  - SCORECARD (pure, read-only): the retro window gains ordered `check_observations` `(ticket, code, verdict, bypassed)` from each completed `check/<ticket>/<run>` invoice; `project_scorecard(window)` returns immutable `Scorecard(boundary, merged_ticket_count, spend_usd, tokens, signal_counts, gate_failure_count, surfaces)` with rows `SurfaceScorecardRow(surface, evaluated_tickets, catches, escapes, bypass_count, catch_rate, escape_rate, prune_candidate)` sorted by surface; catches are non-bypassed fails; zero denominators give zero; `prune_candidate` iff at least 25 evaluated tickets with zero catches and zero escapes; escapes stay zero until Phase 6; the report renders a deterministic `## Surface scorecard` table.
  - STATUS: section 13 touchpoint 5; the production caller passes its injected clock, the real HEAD SHA via `Git.rev_parse`, and the real `specs/retro.md` version into the window projection -- no sentinel provenance.
  - BINDING READER: `resolve_baseline(config, events, *, specs_dir) -> BaselineResolution(state, binds)`, never raising, state CLOSED `ABSENT | NO_GO | GO | REVOKED`, selecting the LATEST complete `review_baseline` signal: none is ABSENT; a non-GO verdict is NO_GO; a GO binds only while its recorded identity equals the current registry resolution for every recorded tier on `review` and `author` and its spec-major equals the majors loaded from `specs/review.md` and `specs/author.md`, else REVOKED; empty routing, unresolved placeholders, unknown tiers, malformed fields, unreadable specs, or a torn tail resolve to the supervised side. It is the ONE reader: `tempo/author.py` is its sole starting-state caller, passing `binds` into `policy.starting_state` (the sole starting-state policy), and the Phase 6 supervised-merge hold is its only other consumer.
  - VERBS: `retro` takes the lock and runs the SAME composition as the hook forced; with no merge since the latest report it prints `retro: no merge since the latest completed report` and exits 0; success prints `retro: committed tickets/retro/<seq>.md`, exit 0; a selected run committing nothing prints `retro: no report committed; inspect status and the Suggestion Box`, exit 1; refusals exit 2. `doctor` (`tempo/doctor.py`) checks exactly `venv`, `git`, `config`, `lock`, `journal` in that order, read-only, rendering `doctor: ok|failed` then one `PASS|FAIL <name>: <detail>` per check; any failure exits 2, never a traceback.
- *Exit reads (committed artifacts and merged tests only):* the lexicographically latest committed `tickets/retro/<seq>.md`, committed by the forced pre-exit hook after the latest feature merge, carries a `## Surface scorecard`, numeric spend/tokens, only existing check surfaces, and a zero escape column; every scorecard entry resolves to real journal events and EXISTING stems -- a fabricated or fixture-derived entry is false-green (machinery: `retro-drain-invoker` + `scorecard-reporting`; producer: the forced pre-exit retro). The wired retro-finding -> box -> triage/Author -> prompt-spec-merge chain is re-exercised by merged `tests/test_retro_box.py` with `retro-box-activation`'s committed `checks.json` passing -- the live loop is a daemon-era observation (section 13 touchpoint 7). The binding reader's merged tests prove an injected Phase 1 NO-GO resolves unbound and an injected GO plus a spec-major bump resolves REVOKED, with `baseline-binding-reader`'s `checks.json` passing; no production GO exists before cutover.

```yaml
# BEGIN_REGISTRY_P5
phase: 5
admissions:
  - [retro-drain-invoker]
  - [retro-box-activation]
  - [scorecard-reporting]
  - [status-projection, baseline-binding-reader]
  - [retro-doctor-cli]
  - [phase5-exit]
seeds:
  retro-drain-invoker: {deep: true, cite: [14, "13.3", "6.3", "6.2", 8],
    fence: ["tempo/retro.py", specs/retro.md, tests/test_retro.py, "tempo/drain.py", "tempo/driver.py", "tempo/stages.py", "tempo/__main__.py", "tempo/box.py", tests/test_drain.py, tests/test_driver.py, tests/test_stages.py, tests/test_box.py],
    does: "INVOKER contract plus the sections 13/19 Exit-read window renderer resolution and driver materialization on the same window-projection seam. Transition-risk preservation: every suite reaching `main([\"drain\"])` through to a merge (re-entry, upgrade, kill, storm, restart, provider, watchdog, pause, control) migrates only an assertion its scenario actually forces, else stays unchanged with the non-firing reason recorded in the seeding test.",
    owner: "tempo/retro.py: retro window projection, RetroArtifact, lock-held report commit hook; specs/retro.md: retro LLM surface",
    records: "window = journal events after the latest EFFECT_COMPLETION with key retro/<seq> (intent never advances); merges from state_transition to: merged, spend from completion cost usd/input_tokens/output_tokens; spike operands: used gate_bypass via Invoice.bypassed, rework.REWORK_ORDER, state_transition to gate_failed, mergequeue.CONFLICT_FACTS with integration_red_paths; thresholds N=25, M=7 days, S=5 of ONE kind; report tickets/retro/<seq>.md committed via Git inside Effects.run(key=\"retro/<seq>\", ticket=None), completion {result: {commit: <sha>}}; failure SIGNAL key retro-failed/<window-boundary>/<trigger>, body {kind: retro_failed, window_boundary, trigger, error_code}, + deduped Box.enqueue(message_class=\"failure_report\"); exit bullet <event type>: current retro window (type in tempo/journal.py::EVENT_VERSIONS) materializes matching events with exact {v, type, ts, ticket, key, body}; Stages.driver via Pipeline.stages, absent Driver raises RetroConstructionError",
    observable: "Drain checks due status each dispatch iteration and runs one forced retro at quiescence and before any phase-exit dispatch when a merge landed since the last completed report, committing under the drain lock.",
    tests: "tests/test_retro.py::test_window_uses_completed_report_boundary, tests/test_drain.py::test_drain_retro_due_quiescence_and_pre_exit, tests/test_driver.py::test_exit_read_window_materialization, tests/test_stages.py, tests/test_plan_lint.py"}
  retro-box-activation: {deep: true, cite: ["12.3", "12.6", "12.1", "12.5", "6.2"], depends: [retro-drain-invoker],
    fence: ["tempo/retro.py", "tempo/box.py", "tempo/merge.py", "tempo/author.py", "tempo/triage.py", "tempo/policy.py", "tempo/stages.py", "tempo/runner.py", "tempo/daemon.py", "tempo/drain.py", "tempo/serve.py", "tempo/status.py", "tempo/__main__.py", tests/test_box.py, tests/test_merge.py, tests/test_author.py, tests/test_triage.py, tests/test_policy.py, tests/test_stages.py, tests/test_daemon_composition.py, tests/test_drain.py, tests/test_serve.py, tests/test_retro_box.py],
    does: "RETRO -> BOX contract; every production Box constructor with journal access receives the journal-backed rereport callback.",
    records: "box message gains retro_report_key, fixed_failure, overcorrection_risk and proposed_spec_paths; proposal_id = first 16 hex of sha256 over sorted-key compact JSON of {fixed_failure, overcorrection_risk, proposed_spec_paths}; origin retro-proposal/<retro_report_key>/<proposal_id>; queue records gain reports (int, 0) and reopen (bool, false); Box.record_rereport reopens once at K=3; callback writes SIGNAL key tombstone-reopen/<box_id>/<reports> body {kind: tombstone_auto_reopened, box_id, signature, reports} before clearing resolution; no callback raises RereportCallbackRequired; Author SIGNAL key retro-ticket/<stem> body {kind: retro_ticket_authored, box_id, retro_report_key, ticket_stem} before Resolution(kind=\"ticket\", link=<stem>), ticket source box:retro_finding; merge SIGNAL key retro-prompt-spec-change/<stem>/<squash-sha> body {kind: retro_prompt_spec_change_merged, box_id, retro_report_key, ticket_stem, squash_sha, changed_spec_paths}; policy.start_state(reopen=...)",
    observable: "The completed-report -> box -> triage -> Author -> prompt-spec-merge chain runs in production composition; every journal-holding Box gets the rereport callback; reopened tickets start draft under GO.",
    tests: "tests/test_retro_box.py::test_completed_retro_proposals_reach_prompt_spec_merge, tests/test_box.py::test_tombstone_rereports_reopen_at_threshold, tests/test_daemon_composition.py::test_production_boxes_receive_rereport_callback, tests/test_merge.py, tests/test_author.py, tests/test_triage.py, tests/test_policy.py, tests/test_retro.py, tests/test_status.py"}
  scorecard-reporting: {cite: [14, "13.4", "6.3", 7], fence: ["tempo/scorecard.py", tests/test_scorecard.py, "tempo/retro.py", tests/test_retro.py],
    does: "SCORECARD contract; tests/test_retro.py assertions are preserved unchanged.",
    owner: "tempo/scorecard.py: pure read-only project_scorecard(window) projection",
    records: "evidence gains ordered check_observations (ticket, code, verdict pass | fail, bypassed) from completed Check invoices; returns Scorecard(boundary, merged_ticket_count, spend_usd, tokens, signal_counts, gate_failure_count, surfaces); surfaces = SurfaceScorecardRow(surface, evaluated_tickets, catches, escapes, bypass_count, catch_rate, escape_rate, prune_candidate) sorted by surface; catch per (surface, ticket) = non-bypassed failure or snag/rma finding then merge, or upheld at terminal-without-merge; prune_candidate true at >= 25 evaluated tickets with zero catches and escapes",
    observable: "Retro renders a deterministic, engine-derived ## Surface scorecard table from the write-free projection; the receipt's existing evidence stays unchanged.",
    tests: "tests/test_scorecard.py::test_scorecard_uses_completed_check_evidence, tests/test_scorecard.py::test_scorecard_catches_resolved_and_upheld_findings, tests/test_scorecard.py::test_scorecard_rates_and_prune_threshold, tests/test_retro.py"}
  status-projection: {cite: ["13.5", 14, "12.3"], depends: [scorecard-reporting, retro-box-activation],
    fence: ["tempo/status.py", tests/test_status.py, "tempo/scorecard.py", tests/test_scorecard.py, "tempo/box.py", tests/test_box.py, "tempo/retro.py", tests/test_retro.py, "tempo/__main__.py", tests/test_cli.py],
    does: "STATUS contract.",
    records: "Status keeps merged, in_flight, blocked (latest state_transition per stem), intake tuples (stem, source, state, commit) from INTAKE_SIGNAL, lifetime spend_usd from completion cost usd, reject from reject_queue, tombstones (box_id, message_class, reports) from Box.confirmed_tombstones(policy), reopens bodies {kind: tombstone_auto_reopened, box_id, signature, reports}; adds current-window Scorecard via project_scorecard, boundary initial or latest completed retro/<seq>, lifetime spend kept separate from window spend; provenance = injected clock, dir-pinned Git.rev_parse HEAD, loaded specs/retro.md version; writes no new record",
    observable: "main([\"status\"]) stays pull-only, deterministic and write-free, preserving every existing section's wording and appending the current-window scorecard with real provenance.",
    tests: "tests/test_status.py::test_status_current_window_scorecard, tests/test_status.py::test_status_production_provenance, tests/test_status.py::test_status_preserves_sections_and_is_write_free, tests/test_cli.py"}
  baseline-binding-reader: {cite: ["12.6", "13.5", "6.7", "6.2"], depends: [retro-box-activation],
    fence: ["tempo/baseline.py", tests/test_baseline.py, "tempo/journal.py", "tempo/config.py", "tempo/policy.py", tests/test_policy.py, "tempo/author.py", tests/test_author.py, tests/test_retro_box.py],
    does: "BINDING READER contract, migrating any interim go-binding helper into the one reader.",
    owner: "tempo/baseline.py: sole GO-baseline reader resolve_baseline(config, events, *, specs_dir) -> BaselineResolution(state, binds)",
    records: "reads SIGNAL records with body signal: review_baseline (BASELINE_SIGNAL) in journal order: verdict and identity {review: {spec_major, rows: {<tier>: {provider, model}}}, author: {...}}, tiers low | medium | high | max; compares to the current registry resolution and the majors of specs/review.md and specs/author.md; state ABSENT | NO_GO | GO | REVOKED, binds true only for GO; drift or invalid identity = REVOKED; replaces interim tempo/policy.py::go_binds; Author passes binds to policy.start_state go",
    observable: "Resolution never raises and invalid or torn input yields binds false so Author stays supervised; a later non-GO or MAJOR spec bump revokes a GO, a MINOR bump does not.",
    tests: "tests/test_baseline.py::test_baseline_states_and_latest_signal, tests/test_baseline.py::test_baseline_invalid_inputs_fail_closed, tests/test_author.py::test_author_uses_baseline_resolution, tests/test_policy.py, tests/test_retro_box.py"}
  retro-doctor-cli: {cite: ["18.1", 14, "6.4", "6.3"],
    fence: ["tempo/doctor.py", tests/test_doctor.py, "tempo/retro.py", "tempo/__main__.py", tests/test_retro.py, tests/test_cli.py], does: "VERBS contract.",
    owner: "tempo/doctor.py: mechanical read-only self-check and its rendering",
    records: "retro verb composes Retro(checkout, pipeline) and runs forced=True under the single-writer lock with the Driver from Pipeline.stages.driver; report tickets/retro/<seq:06d>.md under key retro/<seq> returning {commit: <sha>}; outputs retro: no merge since the latest completed report (exit 0), retro: committed tickets/retro/<seq:06d>.md (0), retro: no report committed; inspect status and the Suggestion Box (1), engine refusals exit 2; doctor checks in order venv, git, config, lock, journal, renders doctor: ok or doctor: failed then one PASS <name>: <detail> or FAIL <name>: <detail> per check, exit 0 all-pass else 2",
    observable: "retro takes and releases the lock on every exit and commits only when a merge landed since the last completed report; doctor runs all five checks read-only with no traceback, state creation or provider/pipeline call.",
    tests: "tests/test_cli.py::test_retro_success, tests/test_cli.py::test_retro_no_merge, tests/test_doctor.py::test_doctor_is_read_only, tests/test_retro.py"}
  phase5-exit: {exit: true, fence: [tickets, tests/test_phase5_exit.py, tests/test_seeded_phase6_core.py],
    does: "Exit reads above; authors the 19.P6 core."}
# END_REGISTRY_P5
```

### 19.P6 Phase 6 -- External hosts + cutover

- *Deliverables:* the host-contract machinery -- the `tempo:core` managed-block renderer and drift classifier in `tempo/hostfiles.py` (section 8; FIRST ADOPTION: a routed CLI's conduct file with ZERO marker-like text gets the block INSERTED with every existing byte kept as project-owned remainder; malformed, partial, or duplicate marker text refuses; tempo's own CLAUDE.md/AGENTS.md are the first case), the `core_drift` gate ACTIVATION (an engine-shipped HARD code joining the merge-time mechanical re-run set), `migrate-config`, and `docs/host-contract.md` (the section 15 schema with a copyable commented `review`/`merge` example, the seam inventory, the report-inbox contract, managed-file ownership, cutover steps; foreign process-state adoption excluded until earned, D10). Host #1 is an OPERATOR-DECLARED input (section 0) -- a build declaring none simply waits at cutover; EVERY build scaffolds the committed fixture host `hosts/fixture/` and runs Phase 6 wiring and exit reads against it -- a host-root config profile, a miniature deterministic app with a replay-runner command, a CLOSED scenario list, bounded version-1 report fixtures, one merge-base regression defect, one machine-introduced escape scenario, and a scripted agent CLI (one more `kind: cli` provider behind the section 6 adapter contract) serving Author/Implement/Review/triage at zero model spend. The report inbox with EVERY loop seam owned (section 12 version-1 NORMATIVE report schema; metadata-first 1 MiB replay-file and 64 KiB log caps; bounded contents COPIED into durable box custody before the message records; the sequential triage consumer owns the runtime ingest caller, the evidence copy into the authored ticket's `evidence/`, and the `kind: bug` authoring path). WITH it the bug gate (`kind: bug`, `## Regression`, the merge-base overlay, section 7) and the scorecard ESCAPE column (triage's bug->range attribution plus the squash-trailer read op in `git.py`). The supervised-merge hold (sections 9, 12). The GO-grade baseline and the exit-receipt machinery. A host-work ticket carries no `Plan contract`.
- *Settled contracts:*
  - DRIFT: one `tempo/providers.py` resolver maps routing to conduct-file paths, shared by the `core` verb and the gate; the gate compares each committed managed block with a fresh branch-version render; the classifier is pure `missing | current | drifted | refused`.
  - MIGRATE-CONFIG: the sole older schema is version 0, identical to version 1 except `schema_version: 0`; the mapping changes only that scalar; the candidate validates through the real loader before one atomic replace with an adjacent backup; current version 1 is a byte-identical no-op; anything else refuses with no write.
  - ESCAPE: the source is a resolved `bug_report`'s `app_commit` (lowercase SHA or `BASE..HEAD`); git accepts only first-parent `HEAD` ancestry with one valid trailer pair; anything else is unattributed; each passed-Check report/ticket/surface counts once.
  - HOLD: durable HELD after merge safety and integration, before main mutation; held stems stop dispatch but keep their worktrees; identity-bound `confirm` releases without a cap keep signal; a moved main rebases and regates; restart reconstructs holds; the bootstrap self-build never holds.
  - GO-GRADE: the Phase 1 spike's set grown to at least 50 planted defects PLUS at least 15 clean (defect-free) fixtures -- grown ONLY by the `go-grade-corpus-1..3` rows, each at most 25 fixture directories under the diff gate (section 7) and merged before `go-grade-machinery`, which adds no fixtures, so the partial-report rule below is a safety net and never the paved road -- scored by `eval/harness.py` under a flat per-run cap (engine constant, shipped USD 5.00) for catch rate on planted defects and false-snag rate on clean fixtures; the run applies a per-call ceiling (an optional `LLMRequest` budget passed to a cost-reporting CLI, e.g. Claude `--max-budget-usd`; a provider without cost reporting refuses before spawn when its flat estimate exceeds the remainder), each call receiving the remaining cap. A run that exhausts the cap before scoring every fixture commits its measured PARTIAL report, which resolves `NO_GO` mechanically; an incomplete report can never record GO; a bounded re-run is the paved road. The AUTHOR-GRAPH CHECK runs the harness-local author prompt (NEVER production `specs/author.md`) over a committed fixture problem set into one committed graph artifact the operator judges. `--record-go` ships with this deliverable (the Phase 1 spike records NO-GO mechanically and has no GO mode), is operator-only, and refuses unless the report is complete with catch rate at least 0.85 AND false-snag rate at most 0.15 (engine constants); the operator's Author-graph judgment remains required above those floors. GO and NO-GO are both valid exits; cutover waits at NO-GO. The committed report embeds its verdict signal's journal identity.
  - RECEIPT: `exit-receipt.json` closed schema: per-read verdict, `host_loop_digest` and `baseline_report_digest` (lowercase SHA-256 of the validated `host-loop-report.json` and `review-baseline-report.json` bytes), the GO-grade verdict with its embedded verdict-signal identity, and `schema_version` -- no wall-clock or checkout-dependent field, so it regenerates byte-identical. The verdict is read ONLY from the committed GO-grade report: an incomplete report yields `NO_GO`; a complete one yields the verdict its embedded identity records; `phase6-exit` performs no journal read. `tempo/artifacts.py` owns schemas; `tempo/stages.py` KNOWN_ARTIFACTS validates OUTBOX bytes.
- *Exit reads:* `phase6-exit`'s harness launches `tempo serve` as a supervised SUBPROCESS against the fixture host's profile on a DISPOSABLE checkout -- the SECOND sanctioned pre-cutover `serve`, sized inside its `Time budget` -- in supervised mode (no GO is recordable pre-cutover), the harness PLAYING THE OPERATOR by writing `confirm` requests into the subprocess's control inbox for each draft fixture ticket and held candidate, each journaled with a MACHINE actor (never re-arming caps). Committed `host-loop-report.json` members, in order: at least 3 distinct-run `machine_ticket_merge` with zero engine-plane edits; `report_to_regression_bug_loop` (report -> replay -> regression fixture end to end); `escape_attribution` via the squash trailers (machinery `exit-receipt-machinery`, producer the exit's own harness run). The committed GO-grade report and its embedded verdict identity (machinery `go-grade-machinery`, producer `go-grade-run`). LIVE host reads -- K >= 10 machine-authored merges on host #1 and one real bug loop, counted only from this engine's own journal and provenance stamps -- are the operator's post-cutover acceptance. *Annex, any time after host #1:* a different-stack second host merges M >= 5 tickets with host-config and host-contract fixes only. Fleet is out of scope.

```yaml
# BEGIN_REGISTRY_P6
phase: 6
admissions:
  - [core-renderer, core-drift-classifier]
  - [core-drift-activation, migrate-config]
  - [host-contract-doc, fixture-host-scaffold]
  - [bug-gate-grammar, report-inbox-triage]
  - [escape-column]
  - [supervised-merge-hold]
  - [go-grade-corpus-1, go-grade-corpus-2]
  - [go-grade-corpus-3]
  - [go-grade-machinery, go-grade-run]
  - [exit-receipt-machinery]
  - [phase6-exit]
seeds:
  core-renderer: {cite: [8, "15.2"], fence: ["tempo/hostfiles.py", tests/test_hostfiles.py, "tempo/__main__.py", tests/test_cli.py],
    does: "Managed-block renderer, first adoption, byte-idempotent, no git or commit; core CLI registration.",
    owner: "tempo/hostfiles.py: core template, marker parsing, byte render, fs-seam refresh",
    records: "Managed block: one `<!-- tempo:core begin -->` line, branch-version template, one `<!-- tempo:core end -->` line; outside bytes project-owned, byte-identical; no marker-like text (`tempo:core` elsewhere) -> prepend block + newline; duplicate/reordered/partial/unterminated/stray markers refuse before any write; routing: claude CLI -> CLAUDE.md, other agent CLI -> AGENTS.md, deduped; refresh is sole writer via injected atomic fs write",
    observable: "`tempo core` renders every routed conduct file preserving project bytes; refusal exits 2 with repair road and no writes, success exits 0 and reruns byte-identically; no git or gate registration.",
    tests: "tests/test_hostfiles.py, tests/test_cli.py; tests/test_hostfiles.py::test_core_first_adoption_preserves_project_bytes, tests/test_hostfiles.py::test_core_marker_corruption_refuses_without_writes, tests/test_hostfiles.py::test_core_render_is_byte_idempotent"}
  core-drift-classifier: {cite: [8], depends: [core-renderer], fence: ["tempo/hostfiles.py", tests/test_hostfiles.py],
    does: "Pure DRIFT classification, unreachable from production gates.",
    records: "Returns exactly `missing | current | drifted | refused` (absent/marker-free -> missing; block == fresh branch render -> current; differing block -> drifted; marker corruption or project process paragraph -> refused, precedence); remainder lint normalizes list prefixes/continuations and matches paragraph openings case-insensitively: `tempo` + modal (must, must not, should, should not, shall, shall not, will, will not, may, may not) or imperative (author, implement, check, review, merge, commit, dispatch, confirm, reject, kill, pause, resume, triage, rework, drain)",
    observable: "Direct classification is deterministic and write-free against the ticket branch's fresh template; `core_drift` stays unregistered and out of the merge rerun set.",
    tests: "tests/test_hostfiles.py; tests/test_hostfiles.py::test_core_drift_classification, tests/test_hostfiles.py::test_core_remainder_process_refusals, tests/test_hostfiles.py::test_core_classifier_unreachable_from_production_gates"}
  core-drift-activation: {cite: [7, 8], depends: [core-drift-classifier],
    fence: ["tempo/hostfiles.py", "tempo/gates.py", "tempo/config.py", "tempo/merge.py", "tempo/providers.py", "tempo/__main__.py", tests/test_hostfiles.py, tests/test_gates.py, tests/test_merge.py, tests/test_providers.py, tests/test_cli.py],
    does: "core_drift hard gate in the merge-time rerun set over the shared resolver; replaces the classifier-unreachable assertion.",
    records: "Reuses `core_drift` in `ENGINE_GATE_CODES` (severity `hard`); providers.py resolver (claude -> CLAUDE.md, else AGENTS.md); only `current` passes, else `Finding` coded `core_drift` naming the path; `GateReport` {code, verdict: pass | fail, findings, autofix_applied: false}; same candidate commits self-host CLAUDE.md and AGENTS.md",
    observable: "`core_drift` runs hard in Check and both merge reruns before main moves, failing closed on missing/stale/corrupt/overriding routed files.",
    tests: "tests/test_hostfiles.py, tests/test_gates.py, tests/test_merge.py, tests/test_providers.py, tests/test_cli.py, tests/test_serve.py; tests/test_hostfiles.py::test_core_drift_reachable_from_production_gates, tests/test_merge.py::test_core_drift_merge_rerun, tests/test_merge.py::test_core_activation_first_adoption_merge"}
  migrate-config: {cite: ["15.2"], depends: [core-renderer], fence: ["tempo/config.py", "tempo/__main__.py", tests/test_config.py, tests/test_cli.py],
    does: "MIGRATE-CONFIG contract.",
    records: "Sole prior schema: version 0 = current `SCHEMA_VERSION = 1` shape with `schema_version: 0`; reads `--config` else `config.yaml` at cwd; changes only that scalar to `1`, all other bytes kept; validates full candidate via real `load_config` before any write, then adjacent backup of original bytes + one atomic fs-seam replacement; version 1 is byte-identical no-op without backup; missing file/key, non-integer/negative, unsupported, invalid YAML, or failed validation refuse with cause and road",
    observable: "`migrate-config` runs before normal loading: valid 0-to-1 exits 0 and the real loader accepts the result, version 1 reruns exit 0 unchanged, every refusal exits 2 without writing.",
    tests: "tests/test_config.py, tests/test_cli.py; tests/test_config.py::test_migrate_config_zero_to_one, tests/test_config.py::test_migrate_config_validates_before_write, tests/test_config.py::test_migrate_config_refuses_unsupported_inputs"}
  host-contract-doc: {cite: ["15.2", "12.8"], depends: [migrate-config], fence: [docs/host-contract.md, tests/test_host_contract.py],
    does: "The host-contract doc; no production code.", owner: "docs/host-contract.md: copyable host configuration and cutover contract",
    records: "Documents full `schema_version: 1` config (fields, defaults, closed vocabularies, absent-vs-null); copyable YAML example with commented `review.mechanical`, `review.surfaces`, `review.trigger_map`, `review.gate_severity`, `merge.safety_checks`, `merge.strategies`; version handshake and 0-to-1 migration; additive-only frontmatter, check-runner interface, state-dir custody, seam signatures; report inbox v1 shape with 1 MiB replay / 64 KiB log bounds; `tempo:core` ownership and `tempo core` road; cutover order",
    observable: "A new host copies the example into a config the real loader accepts, and the doc explains managed-block ownership, migration, seams, report custody, and GO/NO_GO cutover without runtime activation.",
    tests: "tests/test_host_contract.py; tests/test_host_contract.py::test_host_contract_example_loads, tests/test_host_contract.py::test_host_contract_schema_coverage, tests/test_host_contract.py::test_host_contract_report_custody_and_cutover"}
  fixture-host-scaffold: {cite: ["15.2", "6.11", "12.8"], depends: [host-contract-doc, core-drift-activation],
    fence: [hosts/fixture/, tests/test_fixture_host.py], does: "The fixture host as listed in Deliverables; no engine module.",
    owner: "hosts/fixture/: host profile, deterministic app, replay runner, scenarios, scripted CLI",
    records: "Loader-valid version-1 host-root `config.yaml`; replay = `initial_state` + ordered `actions`; closed scenarios: three machine-ticket merges, one merge-base regression defect, one machine-introduced escape; report fixtures in the inbox v1 shape (both origins, replay <= 1 MiB, log <= 64 KiB); fixture-local `claude` reads stdin prompt and emits {type: result, subtype: success, is_error: false, result, total_cost_usd: 0}",
    observable: "Every build drives the fixture through the existing Claude adapter protocol with no external host, network, or model spend, while no serve, GO, journal, or exit-report producer is activated.",
    tests: "tests/test_fixture_host.py; tests/test_fixture_host.py::test_fixture_reports_are_bounded, tests/test_fixture_host.py::test_fixture_cli_adapter_roundtrip, tests/test_fixture_host.py::test_fixture_scaffold_is_dormant"}
  bug-gate-grammar: {cite: [7, "13.2", "13.3"], depends: [fixture-host-scaffold],
    fence: ["tempo/tickets.py", "tempo/gates.py", "tempo/stages.py", tests/test_tickets.py, tests/test_gates.py, tests/test_bug_gate.py],
    does: "kind: bug, mandatory ## Regression, head-pass/base-with-carries-overlay-fail hard gate; a test missing at base is never defect evidence.",
    records: "`kind: bug` needs `## Regression` (argv command + `- carries: <path-prefix>`); stdout: exactly one JSON {regression_result: pass | defect | error} (longer than `OUTPUT_TAIL_CHARS` refuses), pass <=> exit 0, defect <=> positive exit; `BugEvidenceGate` in `CHECK_GATES`, code `bug_evidence` hard; `gather_evidence` runs head, then merge base in a detached worktree with carries overlaid; Check writes the report to `checks.json`, both merge paths recompute in memory; evidence under ticket `evidence/`",
    observable: "Only head-pass/base-defect protocol output passes Check and both merge paths; non-bugs run no regression.",
    tests: "tests/test_tickets.py, tests/test_gates.py, tests/test_bug_gate.py, tests/test_mergequeue.py; tests/test_bug_gate.py::test_regression_overlay_proves_defect, tests/test_bug_gate.py::test_passing_new_test_is_not_bug_evidence, tests/test_bug_gate.py::test_bug_gate_merge_wiring"}
  report-inbox-triage: {cite: ["12.8", "12.3", "12.2", "12.5", "12.6"], depends: [bug-gate-grammar],
    fence: ["tempo/inbox.py", "tempo/box.py", "tempo/triage.py", "tempo/author.py", "tempo/daemon.py", "tempo/serve.py", tests/test_box.py, tests/test_triage.py, tests/test_author.py, tests/test_serve.py, tests/test_inbox.py],
    does: "The report inbox with every seam owned; daemon and live serve consumers wired.",
    owner: "tempo/inbox.py: report validation and Box ingestion",
    records: "Report v1: `schema_version: 1`, `origin: self_diagnosed | player`, `summary`, `app_commit`, `app_version`, optional `implicated_paths`, `evidence` {replay, log}; self-diagnosed adds `game_slug`, `anomaly_code`, `location`; replay <= 1 MiB, log <= 64 KiB; dedup signature (game_slug, anomaly_code, location, app_version); Box record gains nullable `report` descriptor with custody paths, `bug_origin`, `has_repro`; consume only after durable enqueue/dedup; config path `report_inbox`",
    observable: "`tempo triage` and live serve ingest `report_inbox` before the pending scan, yielding a `kind: bug` ticket with evidence under `tickets/<stem>/evidence/`.",
    tests: "tests/test_inbox.py, tests/test_box.py, tests/test_triage.py, tests/test_author.py, tests/test_serve.py; tests/test_inbox.py::test_report_custody_precedes_enqueue, tests/test_serve.py::test_empty_box_report_intake, tests/test_inbox.py::test_report_replay_to_regression"}
  escape-column: {cite: [10, "14.2", "14.3", "14.4", "12.3"], depends: [bug-gate-grammar, report-inbox-triage],
    fence: ["tempo/scorecard.py", "tempo/git.py", "tempo/retro.py", "tempo/__main__.py", tests/test_scorecard.py, tests/test_git.py, tests/test_retro.py, tests/test_cli.py, tests/test_mergequeue.py],
    does: "ESCAPE contract; the scorecard stays pure.",
    records: "Eligible: resolved `bug_report` linked to a bug ticket; `app_commit` = one lowercase 40-hex SHA or `A..B` on first-parent HEAD ancestry; git.py read yields ordered commits, changed paths, one `tempo-ticket: <stem>` + one `tempo-reviewed-sha: <sha>` trailer each; `ESCAPE_ATTRIBUTION = \"escape-attribution\"`, Effect key `escape-attribution/<box_id>`, result {box_id, bug_ticket, app_commit, matches: [{ticket, commit, reviewed_sha, surfaces}], confidence_note}; scorecard counts (box_id, ticket, surface) once; pruning at 25 needs zero escapes",
    observable: "After bug resolution the sequential triage pass completes a replay-safe attribution Effect; the pure scorecard folds completions into per-surface escape counts, once per box_id/ticket/surface.",
    tests: "tests/test_scorecard.py, tests/test_git.py, tests/test_triage.py, tests/test_retro.py, tests/test_cli.py, tests/test_mergequeue.py; tests/test_git.py::test_escape_first_parent_trailers, tests/test_triage.py::test_resolved_bug_escape_attribution, tests/test_scorecard.py::test_escape_counts_once_per_report_ticket_surface"}
  supervised-merge-hold: {deep: true, cite: ["9.4", "9.5", "12.6", "13.5"], depends: [escape-column],
    fence: ["tempo/merge.py", "tempo/mergequeue.py", "tempo/serve.py", "tempo/restart.py", "tempo/baseline.py", "tempo/control.py", "tempo/__main__.py", "tempo/stages.py", "tempo/drain.py", "tempo/runner.py", eval/daemon_soak.py, tests/test_merge.py, tests/test_mergequeue.py, tests/test_serve.py, tests/test_restart_timers.py, tests/test_baseline.py, tests/test_control_cli.py, tests/test_cli.py, tests/test_drain.py, tests/test_daemon_soak_runner.py, tests/test_supervised_merge_hold.py],
    does: "HOLD contract inside daemon-mode MergeQueue admission (after integration, before main moves), reconstructed by restart; the soak runner reads the candidate-base regate identity.",
    records: "HELD typed result in merge.py {stem, attempt, hold_id}; `MERGE_REGATE = \"merge-regate\"` Effect key `merge-regate/<stem>/<attempt>/<candidate_base_sha>/<candidate_sha>`, result exactly {stem, attempt, reviewed_sha, candidate_base_sha, candidate_sha, checked_tree, review_provider, review_model, admission: READY | HELD, reason: bootstrap | foreground | confirmed | go_covered | go_absent | go_revoked | review_off_baseline}, immutable, hold_id = key; admission needs binding GO plus the approving Review's actual served (provider, model) matching `review.rows`; `MERGE_RELEASE = \"merge-release\"` key `merge-release/<hold_id>/<request_id>`, result {stem, attempt, hold_id, request_id, lifecycle_id, verdict: confirm | reject, actor: operator | machine}; inbox request {request_id, lifecycle_id, verb, stem, hold_id, actor}; `control_decision` gains stem, actor",
    observable: "Only daemon-mode admission holds a green candidate after both tiers but before squash, freeing the slot until `confirm <stem>` or covered GO releases it; foreground run and bootstrap drain never hold.",
    tests: "tests/test_supervised_merge_hold.py, tests/test_serve.py, tests/test_mergequeue.py, tests/test_control.py, tests/test_control_cli.py, tests/test_restart_timers.py, tests/test_reconcile.py, tests/test_merge.py, tests/test_drain.py, tests/test_daemon_soak_runner.py; tests/test_supervised_merge_hold.py::test_hold_after_green_before_main_mutation, tests/test_mergequeue.py::test_supervised_confirm_regates_moved_main, tests/test_restart_timers.py::test_restart_reconstructs_supervised_admission"}
  go-grade-corpus-1: {cite: [7, "15.3"], depends: [supervised-merge-hold], fence: [eval/fixtures/],
    does: "FIXTURE-CORPUS row, no engine code: adds at most 25 planted-defect fixture directories (logic and hidden_info_leak classes) authored through the harness author command.",
    records: "eval/fixtures/<case>/ holds ticket.md, diff.patch, expected.json; expected.json {expected_verdict: approve | snag, defect_class: logic | hidden_info_leak | acceptance_mismatch | scope_escape | none, defect: str, author: {provider, model, tier}}; a planted case has a non-none class and a nonblank defect; every case is authored by `uv run python -m eval.harness author --provider <p> --tier <t>` with author identity from that call's `author_invoked` record, never hand-written; each diff within 30 changed files and 1,500 inserted lines",
    observable: "The committed corpus loads through eval/harness.py with planted count raised by exactly this row's directories and clean count unchanged.",
    tests: "tests/test_eval_harness.py"}
  go-grade-corpus-2: {cite: [7, "15.3"], depends: [go-grade-corpus-1], fence: [eval/fixtures/],
    does: "FIXTURE-CORPUS row, no engine code: adds at most 25 planted-defect fixture directories (acceptance_mismatch and scope_escape classes) in the corpus-1 envelope shape.",
    records: "Same envelope and authoring command as go-grade-corpus-1; cumulative planted count after this row at least 50",
    observable: "The committed corpus loads with at least 50 planted cases across all four defect classes and clean count unchanged.",
    tests: "tests/test_eval_harness.py"}
  go-grade-corpus-3: {cite: [7, "15.3"], depends: [go-grade-corpus-2], fence: [eval/fixtures/],
    does: "FIXTURE-CORPUS row, no engine code: adds at most 25 clean (defect-free) fixture directories in the corpus-1 envelope shape.",
    records: "Clean case = expected_verdict approve, defect_class none, defect empty string, same authoring command; cumulative clean count after this row at least 15",
    observable: "The committed corpus loads with at least 50 planted and at least 15 clean cases, so go-grade-machinery's completeness rule can be met by a single bounded run.",
    tests: "tests/test_eval_harness.py"}
  go-grade-machinery: {cite: ["13.5", 7, "6.9", "6.11"], depends: [fixture-host-scaffold, go-grade-corpus-3, supervised-merge-hold],
    fence: [eval/harness.py, eval/fixtures/, "tempo/artifacts.py", "tempo/stages.py", "tempo/llm.py", "tempo/providers.py", tests/test_eval_harness.py, tests/test_stages.py, tests/test_providers.py, tests/test_go_grade.py],
    does: "GO-GRADE machinery: fixtures, closed review-baseline-report.json schema/registration/writer, per-call ceiling, partial-report rule, Author-graph artifact, gated --record-go.",
    records: "Corpus unchanged (18 planted, 4 clean); `MIN_DEFECTS = 15`, `MIN_CLEAN = 3`; GO floors 50 planted / 15 clean; `RUN_CAP_USD = 5.00`, `MIN_CATCH_RATE = 0.85`, `MAX_FALSE_SNAG_RATE = 0.15`; `LLMRequest.budget_usd: float | null`; `REVIEW_BASELINE_REPORT = \"review-baseline-report.json\"` in `KNOWN_ARTIFACTS`; `ReviewBaselineReport` {schema_version: 1, complete, cap_usd: 5.00, spent_usd, results: [{name, expected, outcome, verdict: approve | snag | rma | null}], scores, author_graph: {problems, tickets} | null, verdict_signal: Event {v, type, ts, ticket, key, body: {signal: review_baseline, verdict: GO | NO_GO, identity, exercised, fixture_authors, scores}}}",
    observable: "`eval.harness run --out <path>` writes a validated report to the OUTBOX; `--record-go` refuses unless complete, floors met, identity matches, graph approved.",
    tests: "tests/test_go_grade.py, tests/test_eval_harness.py, tests/test_providers.py, tests/test_stages.py; tests/test_eval_harness.py::test_go_grade_existing_corpus_is_partial_no_go, tests/test_go_grade.py::test_review_baseline_report_closed_schema"}
  go-grade-run: {cite: [10, "13.5"], depends: [go-grade-machinery], fence: [tickets/go-grade-run/review-baseline-report.json],
    does: "No code: one scored run into its OUTBOX, recording its verdict signal.",
    records: "Produces exactly `tickets/go-grade-run/review-baseline-report.json` validated as `ReviewBaselineReport` under `REVIEW_BASELINE_REPORT` with embedded `review_baseline` verdict-signal envelope; Verification runs `uv run python -m eval.harness run --out tickets/go-grade-run/review-baseline-report.json`, never `--record-go` (records NO_GO); OUTBOX bytes uncommitted until the checks lift at `ticket-plane/go-grade-run/<attempt>/checks`; empty code diff settles with one `{to: merged, commit: null, reviewed_sha: <reviewed SHA>}` terminal and one retirement",
    observable: "An empty committed code diff plus a freshly measured schema-valid NO_GO report (complete or partial) is lifted through the ordinary ticket-plane lane, readable by the Phase 6 exit without live journal access.",
    tests: "tests/test_eval_harness.py, tests/test_go_grade.py, tests/test_stages.py, tests/test_merge.py"}
  exit-receipt-machinery: {cite: [10, "12.8", "14.2", "9.4"], depends: [go-grade-run],
    fence: ["tempo/artifacts.py", "tempo/stages.py", eval/host_loop.py, tests/test_host_loop.py, tests/test_gates.py, tests/test_stages.py, hosts/fixture/],
    does: "Closed host-loop-report.json and exit-receipt.json schemas, registration, writers, and the host-loop runner driving supervised fixture serve.",
    owner: "eval/host_loop.py: supervised fixture-host loop runner",
    records: "`HOST_LOOP_REPORT = \"host-loop-report.json\"`, `EXIT_RECEIPT = \"exit-receipt.json\"` in `KNOWN_ARTIFACTS`; `HostLoopReport` {schema_version: 1, entries: [{member, producing_run, expected, observed, engine_plane_edits, auditor, green}]} >=3 `machine_ticket_merge`, then `report_to_regression_bug_loop`, then `escape_attribution`; `ExitReceipt` {schema_version: 1, reads: {machine_ticket_merge, report_to_regression_bug_loop, escape_attribution, go_grade_report: pass | fail}, host_loop_digest, baseline_report_digest (SHA-256), verdict_signal}; canonical byte-identical regeneration",
    observable: "`eval.host_loop` drives supervised fixture `tempo serve` at zero spend and exits nonzero on any unmet read.",
    tests: "tests/test_host_loop.py, tests/test_stages.py, tests/test_gates.py; tests/test_host_loop.py::test_host_loop_drives_supervised_fixture_serve, tests/test_host_loop.py::test_exit_receipt_regenerates_from_committed_bytes, tests/test_gates.py::test_host_loop_regression_uses_real_bug_gate"}
  phase6-exit: {exit: true, depends: [exit-receipt-machinery],
    fence: [tickets/phase6-exit/host-loop-report.json, tickets/phase6-exit/exit-receipt.json, tests/test_phase6_exit.py],
    does: "Runs eval/host_loop.py, reads the committed GO-grade report, commits the RECEIPT; no seeds, no engine edit."}
# END_REGISTRY_P6
```

## 20. Open decisions

Open experiments and undecided seams, each with the evidence that would close it. None blocks the build; each is a deliberate non-default awaiting data:

- **Capability tiering.** This plan ships the validated configuration: ticket default `medium`/`medium`, implement->codex, review+diagnose->claude, review pinned to a fixed strong tier (never on the escalation ladder -- a climbing reviewer blurs the baseline identity). The alternative bet -- a cheap implementer started at `low`/`max` under a strong author, climbing only on evidence -- is OPEN, currently disfavored by observed per-ticket review-snag churn; it closes when a run under each configuration compares merges-per-intervention at equal scope.
- **Single-adapter Phase 1.** Shipping one adapter in Phase 1 and deferring the second to Phase 4 would cut first-contact surface; it is OPEN and disfavored -- cross-provider review diversity from Phase 1 is part of the only lightly-attended completed recipe -- and closes only via a deliberate comparison run, never by default.
- **Daemon control channel.** DECIDED -- Phase 3 builds it (the deferral to "first daemon-era friction" re-bought a prior generation's paid lesson, and `19.P3` requires `kill`/`pause`/`resume` control before `serve`, so the friction is guaranteed, not conditional). A second CLI can never append journal signals while a daemon holds the single-writer lock (sections 6/9) -- the old interim contradicted that fence. While a live engine process -- the drain or the daemon -- holds the lock, control verbs act through a durable file-backed CONTROL INBOX of typed requests under the state dir: the CLI writes the request; the lock-holding engine process -- sole journal writer for its lifetime (section 18) -- consumes each request EXACTLY ONCE, journals its decision BEFORE mutating any state the decision governs, then applies it. Every request binds to identity: the restart-unique lifecycle id of the running engine process it targets (the running `drain` or later `serve`, section 12), and for a hold release the specific hold instance it releases (a storm-breaker trip, section 12; a red-streak or tree-hash admission pause, section 9) -- a `resume` filed before a hold exists, or a request from a prior lifecycle, can never release or stop a later one. `pause` takes effect BEFORE any durable dispatch accounting of the next offer -- section 9's "next safe checkpoint" MEANS pause precedes ALL dispatch accounting, a re-offer's retry draw included, or a consumed pause still spends cap draws before blocking execution; only `resume` releases it, latest-wins. With no engine process running, `pause`/`resume` take the lock themselves and apply directly -- the pre-daemon path is unchanged.

  `kill` is the one additive Phase 3 CLI verb. Against a live bootstrap `drain`, its control consumer runs concurrently with an in-flight dispatch as well as at offer boundaries. A current-lifecycle kill is accepted and journaled before mutation, latches that drain into stopping state, reaches the production `Driver` owned by `Stages` through the `Pipeline`/`Runner` composition, aborts and observes its active invocation, and admits no later ticket or retry draw; the killed run is left journal-valid terminal or restart-reconcilable before the lock releases, and the applied decision is journaled only after the abort unwinds. Stale-lifecycle requests are journaled stale and mutate nothing. With no engine running, `kill` takes the lock only to refuse (`nothing running to kill`), creating no lifecycle, decision, or latch. The worker-stop and kill-failure-suppression boundaries stay dormant until `serve-activation` composes the real worker tasks and activates them in the same order (decision before mutation, executor unwind before worker cancel); a test-only task graph is never production evidence.
- **Settled-ticket archive.** Merged stems currently rest in `tickets/` forever, folded out of eligibility by the journal; a physical archive (relocating settled dirs under `tickets/archive/`) is OPEN, earned by the first measured tooling pain from an unbounded tickets dir (it is the ARCH block's first plausible consumer).
- **Journal retention.** The roll trigger ships in Phase 3; retention/GC of rolled segments stays refused (section 18's ARCH deferral) until disk pressure is actually measured.

## 21. Appendix: embedded bootstrap sources (extraction blocks)

Both bootstrap sources live here. The section-0 cold-start extractor materializes each by pulling the lines between its `# BEGIN_<NAME>` / `# END_<NAME>` sentinel pair -- one minimal pattern, reused for both -- and writing them to disk, so the operator never hand-pastes either file. Keep the sentinel lines intact and exactly as written; the extractor keys on them.

**Bootstrap conductor** -- extracted to `bootstrap/conductor.py` (section 0).

````python
# BEGIN_CONDUCTOR
#!/usr/bin/env python3
"""Bootstrap conductor -- ONE-TIME operator tooling that drives the phase prompt
playbooks in TEMPO_PLAN.md to their exit gates so the human never hand-pastes.

Section-0 cold-start convenience, NOT engine code: it exists only until the
Phase 1 walking skeleton lands and tempo runs its own tickets, and D1 governs the
engine, not these conveniences. It still obeys the argv-list rule (subprocess.run
with lists, never shell=True, never string-assembled commands).

Automation is the DEFAULT, in three rungs the operator picks between -- coarser
rungs just run the finer one in order and gate every deliverable the same way:
  (no --phase)               -- run every conductor-owned phase (0 then 1) end
                                to end; this is the one command for the whole
                                bootstrap.
  --phase N                  -- run all of phase N's deliverables.
  --phase N --deliverable K  -- run exactly that one deliverable, commit it,
                                then stop.
`--auto` removes the designed real-model verdict pauses for a fully unattended
run. The rungs compose through bootstrap/state.json, so hand-stepping a few
deliverables then letting the default finish the rest resumes correctly.
Every rung SKIPS deliverables already recorded done; to redo the whole
bootstrap after editing the plan, delete bootstrap/state.json first. Rungs
REFUSE to run ahead of recorded progress -- a skipped phase or deliverable
would otherwise be ratcheted done without ever running.

Per deliverable: parse the next unrun prompt from this phase's playbook in
TEMPO_PLAN.md (the single source -- no copied checklist), run it in a FRESH
`claude -p` process (a separate scoped context, the anti-wander property; a
standing PREAMBLE carries the verify-in-place and file-don't-ask rules into
every context), then gate by the prompt's own stop-condition. Two checks bind
EVERY gate kind: the `claude` call must exit zero (transient failures are
retried with backoff; a persisting nonzero -- a dead or unauthenticated CLI --
halts, never a silent phantom completion), and the playbook item's
`expects:` files must exist on disk afterward (the agent-did-nothing check;
`-` waives it for journal-gated deliverables). Then per kind:
  pytest  -- re-run `uv run pytest` (never trust the agent's claim), then have
             a SECOND fresh context adversarially review the uncommitted diff
             against the good-enough bar's spine-breaking classes ONLY (state
             corruption, deadlock/stall, secret exposure, false-green tests
             that mirror the implementation -- the builder's own tests are
             not the last word on the builder); every other finding files to
             bootstrap/suggestions.md and passes. Verdict via
             bootstrap/review.json, missing/unparseable = fail closed. Gate
             findings (red suite or review fail) are fed back up to
             MAX_FIX_ATTEMPTS times, then halt. On pass commit + advance.
  verdict -- a real-model deliverable whose exit is a recorded verdict, not a
             green suite (Phase 1 prompts 2 and 9): run, then VERIFY in the
             journal that the artifact the stop-condition names appeared since
             the deliverable started -- a `signal` event when the prompt says
             verdict, a transition to `merged` when it says merged (the
             `state_transition` body's `to` field is a promoted envelope
             contract, section 6); on evidence
             commit, then pause for the operator to read the verdict (--auto
             continues without pausing; GO is not earnable during the
             bootstrap -- the --record-go mode is Phase 6's, section 19); on
             none, halt WITHOUT committing.
  none    -- no test and no verdict (the seed-files step): run, check
             expects, commit, advance.
Guards, all halt-for-operator: a preflight refuses to start without git, uv,
pnpm, and claude on PATH, or with claude older than its latest pnpm release; each phase's parsed deliverable count must match the
plan's stated count (a playbook format drift halts -- never a short parse
silently declared complete); a deliverable REFUSES to
start on a dirty tree (the commit sweeps `git add -A`, so anything already
dirty would splice into this deliverable's commit -- the halt names the
commit-then-rerun paved road; recovery is git revert, never stash or
reset); and a verdict deliverable
REFUSES to record done
until the journal carries its named artifact, never the context's claim.
Progress is bootstrap/state.json ({"phase": N, "done": K} -- phases below N are
complete, phase N has K deliverables done) so a halt resumes where it stopped;
delete it to start the whole bootstrap over from the first deliverable.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAN = ROOT / "TEMPO_PLAN.md"
STATE = ROOT / "bootstrap" / "state.json"
CONDUCTOR_PHASES = (0, 1)   # the phases the conductor owns; 2+ run via `tempo drain`
MAX_FIX_ATTEMPTS = 2
MAX_ATTEMPT_CALLS = 500   # per-launch model-call ceiling (section 19 bootstrap contract)
ATTEMPT_ID = datetime.now(timezone.utc).strftime("bs-%Y%m%dT%H%M%SZ")  # durable per-launch id (section 19)
CLAUDE_TRANSIENT_RETRIES = 2   # bounded backoff before a nonzero exit halts
CLAUDE_PACKAGE = "@anthropic-ai/claude-code"   # the bootstrap CLI's pnpm package (section 0 preflight)
EXPECTED_DELIVERABLES = {0: 11, 1: 17}   # playbook counts; a parse drift halts
REVIEW_FILE = ROOT / "bootstrap" / "review.json"
PREAMBLE = """\
Bootstrap deliverable for the tempo repo; TEMPO_PLAN.md is canonical.
If this deliverable's output already exists (a re-run over prior work),
VERIFY it against the plan and its stop-condition and change only what
fails them -- never rebuild green work. An out-of-scope problem is
appended to bootstrap/suggestions.md and left alone; never stop to ask.
Never run git commit, git add, or any other git write: the conductor
reviews your UNCOMMITTED diff and commits it only after review passes.
You run headless: your process ends the moment you stop replying, killing
anything still running. Run every command -- model calls, evals, authoring
-- in the FOREGROUND to completion; never background work or end your turn
waiting for it. Stop only when the stop-condition is actually met.

"""

CALLS = 0


def count_call():
    # section 19 bootstrap contract: every model call draws the attempt's
    # MAX_ATTEMPT_CALLS ceiling; the next no-flag launch is a fresh attempt.
    global CALLS
    CALLS += 1
    if CALLS > MAX_ATTEMPT_CALLS:
        sys.exit("attempt %s exceeded MAX_ATTEMPT_CALLS=%d -- halting; "
                 "re-run python3 bootstrap/conductor.py to start a fresh "
                 "attempt with a fresh ceiling" % (ATTEMPT_ID, MAX_ATTEMPT_CALLS))



def parse_deliverables(phase):
    text = PLAN.read_text()
    marker = "**Phase %d prompt playbook.**" % phase
    start = text.find(marker)
    if start == -1:
        sys.exit("no playbook for phase %d in TEMPO_PLAN.md" % phase)
    tail = text[start + len(marker):]
    stops = [m.start() for m in re.finditer(r"\*\*Phase \d+ prompt playbook\.\*\*", tail)]
    sec = re.search(r"\n## \d+\. ", tail)
    if sec:
        stops.append(sec.start())
    block = tail[:min(stops)] if stops else tail
    items = re.findall(
        r"\n(\d+) -- ([^\n]+):\nexpects: ([^\n]+)\n+```\n(.*?)\n```",
        block, re.S)
    parsed = [(title.strip(), expects.split(), prompt.strip())
              for _num, title, expects, prompt in items]
    want = EXPECTED_DELIVERABLES.get(phase)
    if want is not None and len(parsed) != want:
        sys.exit("phase %d playbook parsed %d deliverables, expected %d -- "
                 "the playbook format drifted ('N -- Title:' line, 'expects:' "
                 "line, one fenced prompt); fix TEMPO_PLAN.md, never skip"
                 % (phase, len(parsed), want))
    return parsed


def gate_of(prompt):
    low = re.sub(r"\s+", " ", prompt.lower())
    if "pytest" in low:
        return "pytest"
    if "verdict" in low or "merged" in low:
        return "verdict"
    return "none"


def run(cmd, **kw):
    return subprocess.run(cmd, cwd=str(ROOT), **kw)


def journal_evidence(prompt, started_at):
    """The mechanical half of a verdict gate: the artifact the stop-condition
    names must be IN the journal since `started_at`, never the context's
    claim. Returns the set of still-missing evidence kinds."""
    state_dir = ROOT / ".tempo" / "state"
    cfg = ROOT / "config.yaml"
    if cfg.exists():
        m = re.search(r"^state_dir:\s*(\S+)", cfg.read_text(), re.M)
        if m:
            state_dir = ROOT / m.group(1)
    low = re.sub(r"\s+", " ", prompt.lower())
    need = set()
    if "verdict" in low:
        need.add("signal")
    if "merged" in low:
        need.add("merged")
    seen = set()
    for seg in sorted((state_dir / "journal").glob("*.jsonl")):
        for line in seg.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue        # torn tail is the reader's normal case
            if e.get("ts", "") < started_at:
                continue
            if e.get("type") == "signal":
                seen.add("signal")
            if (e.get("type") == "state_transition"
                    and (e.get("body") or {}).get("to") == "merged"):
                seen.add("merged")
    return need - seen


def preflight():
    missing = [b for b in ("git", "uv", "pnpm", "claude") if not shutil.which(b)]
    if missing:
        sys.exit("missing required binaries: %s -- see the prerequisites table "
                 "(TEMPO_PLAN.md section 0)" % ", ".join(missing))
    installed = run(["claude", "--version"], capture_output=True, text=True).stdout.split()[:1]
    latest = run(["pnpm", "view", CLAUDE_PACKAGE, "version"], capture_output=True, text=True).stdout.strip()
    if not latest or installed != [latest]:
        sys.exit("claude %s is not the latest release %s -- run: pnpm add -g %s@latest"
                 % ((installed or ["(unknown)"])[0], latest or "(unreadable)", CLAUDE_PACKAGE))


def claude(prompt):
    # fresh one-shot context per deliverable; operator-owned bootstrap repo.
    # Transient nonzero exits (overload, network) get bounded retries with
    # backoff -- machine-retryable work never waits on an operator rerun.
    for attempt in range(CLAUDE_TRANSIENT_RETRIES + 1):
        count_call()
        r = run(["claude", "-p", PREAMBLE + prompt, "--dangerously-skip-permissions"])
        if r.returncode == 0:
            return
        if attempt < CLAUDE_TRANSIENT_RETRIES:
            wait = 30 * (attempt + 1)
            print("claude exited %d -- retrying in %ds (%d/%d)"
                  % (r.returncode, wait, attempt + 1, CLAUDE_TRANSIENT_RETRIES))
            time.sleep(wait)
    sys.exit("claude exited %d after %d retries -- persistent failure (auth? "
             "quota?); fix it and re-run the same command to continue; nothing "
             "gated, nothing committed, state not advanced"
             % (r.returncode, CLAUDE_TRANSIENT_RETRIES))


def adversarial_review(title, frozen=None):
    """Second fresh context reviews the builder's uncommitted work against
    the section 19 good-enough bar: FAIL only spine-breaking classes; every
    other finding files to bootstrap/suggestions.md and passes. New and
    untracked files are part of the
    review surface (git diff alone misses them). Transient failures --
    nonzero exit or an unparseable verdict file -- get bounded retries;
    a persisting one halts fail-closed. Returns [] on pass, findings on fail.
    A re-review pass receives the FROZEN first-pass blocking set; a NEW
    objection on a later pass is advisory (the section 0 freeze law)."""
    verdict = None
    for attempt in range(CLAUDE_TRANSIENT_RETRIES + 1):
        if REVIEW_FILE.exists():
            REVIEW_FILE.unlink()
        count_call()
        r = run(["claude", "-p",
                 "You are the adversarial reviewer for one tempo bootstrap "
                 "deliverable; TEMPO_PLAN.md is canonical. Review the working "
                 "tree's UNCOMMITTED work -- git status for the file set, git "
                 "diff for tracked changes, and READ each new/untracked file "
                 "in full (the diff does not show them) -- against the "
                 "plan sections the deliverable cites. FAIL only for the "
                 "bootstrap contract's spine-breaking classes (TEMPO_PLAN.md "
                 "section 19): state corruption, deadlock or permanent stall, "
                 "secret exposure, or false-green verification -- tests that "
                 "mirror the implementation instead of pinning real behavior "
                 "(orderings, refusals, crash points). A blocking finding "
                 "must be REPRODUCIBLE and must attach to THIS deliverable's "
                 "uncommitted diff -- never pre-existing code or later-phase "
                 "scope. Append every OTHER "
                 "finding (conformance drift, style, scope) as one-line items "
                 "to bootstrap/suggestions.md and still pass. Write EXACTLY "
                 "bootstrap/review.json: "
                 '{"verdict": "pass"} or {"verdict": "fail", "findings": '
                 '["..."]}. Change no file other than those two. '
                 + ("" if frozen is None else
                    "RE-REVIEW: the blocking set is FROZEN to the findings "
                    "listed after the deliverable name -- fail ONLY if one "
                    "of them is still unresolved; any NEW problem, whatever "
                    "its class, files to bootstrap/suggestions.md and never "
                    "fails. Frozen findings: " + "; ".join(frozen) + ". ")
                 + "Deliverable: " + title,
                 "--dangerously-skip-permissions"])
        if r.returncode != 0:
            if attempt < CLAUDE_TRANSIENT_RETRIES:
                wait = 30 * (attempt + 1)
                print("review context exited %d -- retrying in %ds"
                      % (r.returncode, wait))
                time.sleep(wait)
                continue
            sys.exit("review context exited %d after retries -- fix and "
                     "re-run the same command to continue" % r.returncode)
        try:
            verdict = json.loads(REVIEW_FILE.read_text())
            break
        except (OSError, ValueError):
            if attempt < CLAUDE_TRANSIENT_RETRIES:
                print("no parseable bootstrap/review.json -- re-asking the reviewer")
                continue
            sys.exit("no parseable bootstrap/review.json after %d asks -- "
                     "fail closed, halting for operator"
                     % (CLAUDE_TRANSIENT_RETRIES + 1))
    REVIEW_FILE.unlink()
    if verdict.get("verdict") == "pass":
        return []
    return verdict.get("findings") or ["review verdict: fail (no findings listed)"]


def pytest_green():
    return run(["uv", "run", "pytest", "-q"]).returncode == 0


def ensure_ignored():
    gi = ROOT / ".gitignore"
    txt = gi.read_text() if gi.exists() else ""
    add = [e for e in ("bootstrap/state.json", "bootstrap/review.json")
           if e not in txt]
    if add:
        sep = "" if (not txt or txt.endswith("\n")) else "\n"
        gi.write_text(txt + sep + "\n".join(add) + "\n")
        return True
    return False


def require_clean(phase, n):
    # the commit below sweeps `git add -A`, so a dirty tree at start would
    # splice unrelated work into this deliverable's commit -- halt instead.
    dirty = run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout.strip()
    if dirty:
        sys.exit("phase %d deliverable %d: tree is dirty (a prior halt leaves "
                 "its partial work uncommitted). To continue: commit it "
                 "(`git add -A` + a wip commit), then re-run the same command "
                 "-- verify-in-place converges over committed partial work, "
                 "and recovery is git revert, never stash or reset:\n%s"
                 % (phase, n, dirty))


def commit(msg):
    ensure_ignored()
    dirty = run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout.strip()
    if not dirty:
        return
    run(["git", "add", "-A"], check=True)
    run(["git", "commit", "-m", msg], check=True)


def load_state():
    # (phase, done): phases below `phase` are complete, `phase` has `done` done.
    if STATE.exists():
        s = json.loads(STATE.read_text())
        return int(s.get("phase", 0)), int(s.get("done", 0))
    return 0, 0


def save_state(phase, done):
    # Ratchet: an explicit rerun of an earlier deliverable must never rewind
    # the resume point past completed work.
    phase, done = max(load_state(), (phase, done))
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(
        {"phase": phase, "done": done, "attempt_id": ATTEMPT_ID}))


def run_deliverable(phase, items, i, auto):
    """Run one deliverable (0-based index i). Return True to keep going, False to
    pause on a verdict. Halts the process on a failed gate. Commits + records
    state on success."""
    title, expects, prompt = items[i]
    n = i + 1
    gate = gate_of(prompt)
    require_clean(phase, n)
    print("=== phase %d deliverable %d/%d (%s): %s ===" % (
        phase, n, len(items), gate, title))
    started_at = datetime.now(timezone.utc).isoformat()
    # the expects: files are the gate; a prompt that never names one would
    # otherwise fail the existence check on a correct but differently-placed build
    claude(prompt if expects == ["-"] else prompt + "\n\nRequired outputs -- "
           "the gate checks each exists on disk: " + " ".join(expects))
    missing = [] if expects == ["-"] else [p for p in expects
                                           if not (ROOT / p).exists()]
    if missing:
        save_state(phase, i)
        sys.exit("phase %d deliverable %d: expected outputs missing: %s -- the "
                 "agent run did not produce its deliverable; nothing committed"
                 % (phase, n, ", ".join(missing)))
    if gate == "verdict":
        missing_ev = journal_evidence(prompt, started_at)
        if missing_ev:
            save_state(phase, i)
            sys.exit("phase %d deliverable %d: journal shows no %s since start "
                     "-- halting for operator, nothing committed"
                     % (phase, n, "/".join(sorted(missing_ev))))
    if gate == "pytest":
        attempt = 0
        frozen = None
        while True:
            if not pytest_green():
                findings = ["uv run pytest is red"]
            else:
                findings = adversarial_review(title, frozen)
                if findings and frozen is None:
                    frozen = list(findings)   # the section 0 freeze law
            if not findings:
                break
            attempt += 1
            if attempt > MAX_FIX_ATTEMPTS:
                save_state(phase, i)
                sys.exit("phase %d deliverable %d still failing its gate after "
                         "%d fix attempts -- halting. Paved road: narrow or "
                         "split this deliverable's playbook prompt in "
                         "TEMPO_PLAN.md, then re-run exactly it:  python3 "
                         "bootstrap/conductor.py --phase %d --deliverable %d"
                         % (phase, n, MAX_FIX_ATTEMPTS, phase, n))
            claude("The last change failed its gate. Findings:\n- "
                   + "\n- ".join(findings) + "\n"
                   "Read them, fix the code (not the test, unless the test is "
                   "wrong per TEMPO_PLAN.md), keep the change minimal. Stop "
                   "when uv run pytest is green.")
    commit("bootstrap: phase %d deliverable %d -- %s" % (phase, n, title))
    save_state(phase, n)
    if gate == "verdict" and not auto and n < len(items):
        print("deliverable %d is a real-model step -- read its recorded verdict, "
              "then re-run to continue (--auto skips these pauses)." % n)
        return False
    return True


def run_phase(phase, start, auto):
    """Run phase `phase` from deliverable index `start`. Return True if the phase
    fully completed, False if it paused on a verdict (halts exit on red)."""
    items = parse_deliverables(phase)
    for i in range(start, len(items)):
        if not run_deliverable(phase, items, i, auto):
            return False
    print("phase %d complete." % phase)
    return True


def main():
    preflight()
    ap = argparse.ArgumentParser(description="tempo bootstrap conductor")
    ap.add_argument("--phase", type=int,
                    help="run one phase (default: every conductor phase, %s)"
                         % "->".join(map(str, CONDUCTOR_PHASES)))
    ap.add_argument("--deliverable", type=int,
                    help="with --phase: run exactly this deliverable "
                         "(1-based), then stop")
    ap.add_argument("--auto", action="store_true",
                    help="do not pause on real-model (verdict) deliverables")
    args = ap.parse_args()

    # ignore + commit the conductor's state files up front, so a halt before
    # the first deliverable commit can never dirty the tree with them.
    if ensure_ignored():
        changed = run(["git", "status", "--porcelain", "--", ".gitignore"],
                      capture_output=True, text=True).stdout.strip()
        if changed:
            run(["git", "add", ".gitignore"], check=True)
            run(["git", "commit", "-m",
                 "bootstrap: ignore conductor state files"], check=True)

    # finest rung: one named deliverable, then stop
    if args.deliverable is not None:
        if args.phase is None:
            ap.error("--deliverable requires --phase")
        items = parse_deliverables(args.phase)
        n = args.deliverable
        if not 1 <= n <= len(items):
            ap.error("phase %d has deliverables 1..%d, not %d"
                     % (args.phase, len(items), n))
        cur, done = load_state()
        if args.phase > cur:
            ap.error("phase %d is ahead of recorded progress (phase %d, %d "
                     "done) -- finish earlier phases first, or delete "
                     "bootstrap/state.json to start over"
                     % (args.phase, cur, done))
        if args.phase == cur and n > done + 1:
            ap.error("deliverable %d is ahead of recorded progress (%d "
                     "done) -- deliverables run in order; %d is next, or "
                     "delete bootstrap/state.json to start over"
                     % (n, done, done + 1))
        run_deliverable(args.phase, items, n - 1, args.auto)
        return

    # middle rung: one whole phase
    if args.phase is not None:
        cur, done = load_state()
        if args.phase > cur:
            ap.error("phase %d is ahead of recorded progress (phase %d, %d "
                     "done) -- finish earlier phases first, or delete "
                     "bootstrap/state.json to start over"
                     % (args.phase, cur, done))
        start = done if cur == args.phase else 0
        run_phase(args.phase, start, args.auto)
        return

    # default rung: every conductor-owned phase, end to end
    cur, done = load_state()
    for phase in CONDUCTOR_PHASES:
        if phase < cur:
            continue
        start = done if phase == cur else 0
        if not run_phase(phase, start, args.auto):
            return
        save_state(phase + 1, 0)
    print("conductor phases complete -- from here run `uv run python -m tempo "
          "drain` ONCE; it carries every remaining phase to quiescence "
          "(section 19).")


if __name__ == "__main__":
    main()
# END_CONDUCTOR
````

**README** -- extracted to `README.md`; GENERATED, so edit this block, never the file, then re-run the extractor. A deliberate POINTER, not a run-book: section 0 already is the operator path, and a parallel run-book is a second copy to drift (goal 1); a fuller README returns via D10 only if a stale-doc incident earns it.

````markdown
# BEGIN_README
# tempo

tempo is a continuously running orchestration engine that authors and runs
tickets against host repos, including itself. This README is GENERATED from
TEMPO_PLAN.md (the `# BEGIN_README` block in its appendix) -- edit that block,
never this file. TEMPO_PLAN.md is canonical for everything: read section 0
(cold start, prerequisites, the conductor and its rungs, the self-hosting
handoff) first, and run its one-time cold-start paste to seed the repo, venv,
and deps before any command below.

The lifecycle is four commands, run in order:

    python3 bootstrap/conductor.py            # bootstrap: Phase 0 then Phase 1, gated per deliverable
    python3 bootstrap/conductor.py --auto     # same, unattended (no verdict pauses)
    uv run python -m tempo drain             # after Phase 1: ONE drain self-hosts Phases 2-6 to quiescence
    uv run python -m tempo serve             # cutover: start the continuous daemon on host work (after GO is recorded)

Once `serve` is running, control it with `kill` / `pause` / `resume`; `status`
and `doctor` inspect at any time. Section 18 lists every verb; section 13
touchpoint 7 is the GO/cutover gate `serve` waits behind.
# END_README
````

## 22. Appendix: rationale and provenance (never cited)

Why the section 19 laws exist. Nothing here is a rule: the `Plan contract` resolver refuses section 22, and plan lint excludes it from every render budget. Edit a law in its unit; record why here.

- **One continuous drain, runs-to-done (`19.L` bootstrap contract).** The first implementation attempt died in Phase 2: a week of drains stopped on parked tickets whose fixes were simple, each waiting on an operator to "just rerun". Every drain-conduct rule removes one of those operator waits.
- **Recovery order (`19.L`).** Skipping it is the recurring failure: patching a ticket or code before the seed leaves the defect to reproduce at the next regeneration (section 1).
- **Seed buildability at authoring (`19.L`).** A grammar-valid but unbuildable seed reaches the serial chain, `premise_failed`s at Implement, and parks every downstream stem -- the single-step stall. The fence-gap escape behind mechanical rules 1-4 was paid in every prior build. Render feasibility is measured at the ladder TOP because escalation re-keys the bound smaller mid-lineage, so a render fitting only its authored rung dies on the ladder.
- **Caller closure, rule 5 (`19.L`).** In the v2-seed build, about 25 of 113 plan repairs added a direct caller (`compose_pipeline` users in tests and benches, `stages.compose`, provider payload users in `eval/`) or a public-surface allowlist test (the git operation allowlist, twice) to a fence after the seed parked.
- **Bounded batches and KNOWN-DEEP (`19.L`).** A batch clears review jointly at roughly per-seed-approval^N: nine seeds in one admission never cleared in thirteen attempts (best 1/9), and interlocked seeds sharing one closed member set churn as ONE batch. Depth, not count, drives review churn. The phase-exit seed bundles a multi-read exit test with the next phase's core, a shape that oscillates across attempts without ever presenting the K-identical terminal section 11.4 keys on -- hence KNOWN-HARD at start.
- **Grain law (`19.L`).** Every coarser "runtime" bundle was terminally rejected on a fresh genuine race per review pass; a micro-ticket chain builds one link per continuation and stalls a phase for days. `requisition_review` and the watchdog each proved unbuildable as one ticket in two prior builds; Phase 5 seeded flat cost one build a 27-attempt authoring grind and another a 7-way plan re-decomposition.
- **Registries instead of phase prose (`19.P3`-`19.P6`).** Prose phase bullets left ticket-level fences and orderings to seeding tickets, which invented them one `requisition_review` cycle at a time at the phase boundary, when the exit context is most loaded. The v2-seed build spent 35 plan repairs on Phase 3 alone and had to add six corrective deliverables: `serve-activation` (nothing composed production `serve`), `daemon-soak-runner` (schema and writer landed with no runner), `serve-merge-admission` (the merge queue was never production-reachable), `worker-recovery-disposition` (no run-scoped record of a reaped worker), `outbox-only-admission` (no-code run lanes were rejected for an empty diff), and the split of `admission-holds-activation` from pause activation. The registries are the settled outcome of that build, normalized.
- **Units and On-demand (`19.L`, sections 8, 13).** Seeds once cited whole sections. Section 19 reached 86 KB; a Phase 3 seed citing sections 6, 9-13, 15, 18, and 19 measured 186,586 characters before ticket text against a 120,000-character headroom. About 30 of the v2-seed build's 113 plan repairs only moved files between "embedded" and "read on demand" to fit, inventing the on-demand category 27 times against a rule that every fenced file be embedded.
- **Authoring-time seeding-test snapshots (`19.L`, section 0 prompt 17).** Seeding tests that compared recorded sizes with live files, or rescanned live markers, reddened historical tests whenever a later activation legitimately edited a fenced file -- five migration repairs in one build. The snapshot fix then cost the v3-seed build 19 `tests: resync` commits and 25,000 lines of recorded sizes, because pins over an UNRUN seed's bytes redden whenever the recovery order legitimately edits that seed in place; hence identity-and-structure pins only.
- **Construction/activation evidence contract (`19.I`).** Applied separately, section 9's production-exercise rule and the dormancy rule reject every mid-chain ticket in alternation; an AST scan missing the `from tempo import X` idiom is the convergent false-green dormancy proof. An absence assertion has no reference edge to the new symbol, so closure tracing never finds it.
- **Exit reads on committed artifacts (`19.L`).** The exit ticket runs on a clean checkout without the gitignored state dir, harness member journals die with their temp dirs, and a healthy self-build never produces a live flake, storm, quota, or spiral event to read.
- **Machinery/producer split and run-lane admission (`19.L`).** The engine lifts and validates from merged main, so a deliverable's own OUTBOX registration is not live during its own build: it can never lift its own report. The v2-seed soak landed schema and writer with no runner, and its no-code run was then rejected for an empty diff.
- **Retro invoker (`19.P5`).** Without the drain as invoker, nothing fires a retro before cutover: `phase5-exit` either parks `premise_failed` with no release or passes on fixture-only evidence with the loop never fired in production.
- **Phase 4 split from Phase 3.** The daemon's crash-recovery core is proven before provider and watchdog complexity is layered on.
- **GO-grade size and controls (`19.P6`).** Fifty, not twenty, planted defects: a 20-sample catch rate cannot statistically separate a 60% reviewer from a 90% one, and this number gates unsupervised merge. Clean controls: the v2-seed GO-grade report scored 50 known-bad, 0 clean, catch rate 1.0 -- a reviewer that snags everything scores perfectly, so the number could not tell judgment from reflex. The per-call ceiling and partial-NO-GO rule came from the same run exhausting its flat cap mid-set.
- **Suggestion Box admission (section 12).** After the v2-seed build completed, triage authored 45 draft tickets -- mostly config-strictness and test-only hardening -- with roughly 270 decision and tombstone records, re-invoking Author on every daemon scan for items whose authoring kept failing.
- **Registry rows as the whole contract; no entry-unit headings (`19.L` SPEC DEPTH, section 8 lint).** The v3 seed demanded owner, records, observable, and tests per row but gave the row no field for them, so the v3-seed build wrote 63 free-prose `### 19.P<n>.<stem>` units at run time -- 494 KB, median 6.5 KB, max 19 KB -- through 47 harden and 43 plan-gap tickets, discarded all of Phase 3 once to regenerate under them, and grew the plan from 386 KB to 903 KB. The facts were right; the container was unbounded. Four capped fields on the row hold the same facts at roughly a fifth of the bytes, and a fact that will not fit is the signal the row is two tickets.
- **Size caps restored, whole-plan cap added (section 8).** The v3-seed build deleted the v3 lint caps on day 2 because entry units would breach them, and nothing replaced them; section 19 reached 576 KB. Caps are the only thing that stops the plan from becoming the new render problem, so a cap change now needs a rationale entry here in the same commit.
- **One flat render bound (section 8).** Replacing the per-effort ladder (120,000 usable characters at `max`) with one 400,000-character bound and 300,000 usable ended render-bound stops outright: zero in 251 v3-seed tickets against five in the v2-seed build plus about 30 budget-shuffling plan commits. Climbing the ladder never shrinks the bound.
- **Cite roles and copy refusal (`19.L`, section 13).** With `19.I` + own row + the row's cites, the v3-seed build's 63 implementing tickets injected a median 32 KB of plan against the v2-seed build's 77 KB, and all 63 obeyed the role list. The one 37 KB ticket that inlined its unit's prose took ten attempts; after copy refusal, activation seeds were 6-8 KB. Row cites are subsections because a median 20 KB of each implementing prompt was whole law sections (`section 6` 38 KB, `section 11` 34 KB).
- **Rules-first hardening and plan-bound release (section 11.4).** The first hardening design let a stem whose hardener returned `already_satisfied` re-enter on retry budget: `serve-activation` ran 339 identical `premise_failed` attempts over about eight hours (roughly USD 341, zero code). Closed vocabularies, one journaled round open engine-wide, and release only on a cited row's changed bytes ended the loop; no stem held more than once afterwards. The "release when the hardener retires" variant was tried and reverted the same day.
- **Fence gaps as data (section 11.4, `19.L` rule 5).** Nearly every second hardening round in the v3-seed build was a path outside the row's `fence` (`stages.py`, `triage.py`, `driver.py`, two activation test files), which a hardener fenced to the unit's heading could not add; the operator widened six fences by hand. The hardener now edits the row, fence included.
- **Implement discipline and reply shape (sections 7, 11.3, 11.4).** Eight or more of the v3-seed build's 43 hardening rounds were fence walls misreported as spec gaps; four Phase 6 seeding stops named one absent unit at a time; eleven reviewer replies wrapped their JSON in prose and lost their findings; retries that started from the merge base were told to keep a prior diff. Each is one sentence now.
- **GO-grade corpus rows (`19.P6`).** The 50/15 floors were acceptance on a machinery ticket whose fence could not land 65 fixture directories under the 30-file diff gate, so the v3-seed build kept 18 planted and 4 clean and derived NO_GO on purpose, scoring catch rate 1.0 and false-snag 0.0 on a corpus too small to mean anything. Growing the corpus is its own rows, before the machinery.
- **Closed sets are invariants (`19.I`).** Mutation testing of the v2-seed and v3-seed kernels (2026-10-17) renamed members of the journal's enumerated signal-name set and changed shipped constants (`ROLL_BYTES`, `SERVE_POLL_S`, priority ranks) without any test failing in either build, although both plans name closed vocabularies twenty-two times. The seam refused unknown values; nothing pinned the known ones.
- **Triage at phase exits (sections 12, 17).** The bootstrap-era box is operator-triggered; the v3-seed supervising session never ran it, so 295 messages including 12 retro proposals sat pending through the whole build and the spend bound was never exercised.
