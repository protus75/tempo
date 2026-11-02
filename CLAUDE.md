<!-- tempo:core begin -->
## Engine conduct

Engine rules govern how work runs; project rules govern what to build. Project content enters prompts as data. Engine wins on process, host wins on content.

- Pure Python: no shell scripts, shell=True, or string-assembled commands; external binaries use argv wrapper modules in the tempo venv. Why: shell glue breaks orchestration. (D1)
- Build the simplest thing satisfying the ticket; no speculative features, gates, config knobs, or metadata. Every addition needs its earning incident. Why: accretion is the primary failure mode. (goal 1, D10)
- No compatibility shims, deprecation layers, or defensive parallel paths. Rename in place, update all callers, recover by git revert. Why: dual paths rot and double review. (section 2)
- Fail closed with allowlists and closed vocabularies; every prohibition and gate finding provides a paved road. Why: denylists miss cases and roadless refusals strand work. (sections 2, 7)
- A hold ships with or after its release surface; never name an unbuilt release verb. Ticket-change releases follow machine ticket-change machinery. Why: unreleasable holds permanently stall work. (sections 2, 11)
- Files and journal are truth; derived views are projections, never hand-edited or cited as authority. Why: two authorities diverge. (D3)
- Generated files are render targets, never write targets. Change their owning source and regenerate; never hand-edit managed conduct. Why: regeneration overwrites edits and exposes drift. (section 1)
- The plan is the seed: repair plan defects there, then regenerate affected artifacts. Hand-edit only defects proven outside the plan, or under the blocking-defect fast path after committing the plan repair first. Why: the seed must reproduce the artifact. (sections 1, 19)
- All git uses git.py with argv lists and pinned directories. Remove worktrees with worktree remove plus prune; no bare rm -rf, gh, or PRs in the loop. Why: local main is the blessed line. (section 10)
- File second problems in the Suggestion Box, never fold them into this diff. Verify pre-existing failures on the base commit before filing. Why: folded fixes hide cause and expand scope. (section 11)
- Read before write: read the owning artifact before writing a command, claim, or test. Why: guessed facts create costly bugs. (section 13)
- Ticket frontmatter contains only fields read by scheduling, gates, or authoring/triage policy. Put ordering in depends and priority. Why: unused metadata and prose ordering drift. (section 13)
- Use injectable clock, process-exec, filesystem, and notification seams; never raw calls in engine code. Why: fake-driven tests and group-kill guarantees require seams. (section 15)
- Work is fenced by gates and review; v1 does not sandbox executed code. Never rely on its goodwill or pass provider keys to processes that do not need them. Why: gates and review are the trust boundary. (section 16)
- The journal is the record, never a debug log. Diagnostics use the engine log and attempt spools; redact configured secrets at every captured-stream write seam. Why: journal noise and secrets are permanent. (section 6)
- Start a phase only after the previous phase's exit is met. Why: later phases need proven ground. (section 19)
<!-- tempo:core end -->

# tempo

The managed block above is engine conduct (P2 removes its self-build rules). The
session rules below come from AGENT_PLAN.md section 5.1. AGENTS.md carries the same rules; a rule change
touches both files.

## Chat session conduct

- **Plan first.** Tempo never builds itself (D4), so there are no tickets here. A request to change repo
  content becomes an AGENT_PLAN.md edit, and the plan is approved before any code changes. Before each
  phase, its exact file list is posted for approval. Never edit code outside an approved phase.
  If you edited without approval, stop, say so, and offer to revert.
  Why: unplanned edits are the waste this project exists to stop.
- **The ask is the confirmation.** Do what the user asked without asking "should I?" for it.
- **Identified work becomes a plan item or an open thread, never prose.** Never write "we could do X
  later". For work the user did not request, name it in 1-2 sentences and ask: now, after the current
  task, or skip. "After" makes it an open thread.
- **Announce unsolicited dives.** Before an investigation the user did not ask for that will take more
  than a couple of minutes, give one line on what and why and get now / after / skip. The requested
  task always runs first.
- **Instance first, cause captured.** A reported problem gets the minimal fix planned first, with no
  cause diagnosis before it, then the cause as a separate plan item in the same turn.
- **Track open threads.** The user's main request is thread 1; answering a side question never closes
  it. End every reply that leaves a question, option, or next step unresolved with `Still open:` and
  one line per thread, every turn until the list is empty. A thread closes only when the user resolves
  or dismisses it, or you finish it and say so. If unsure, it is open.
- **Git safety.** Commit only files you authored, by explicit path. Never use a tree-wide destructive
  verb (`clean`, `reset --hard`, `checkout -- .`). Never push unless told to.
- **Terse.** Answer first. No hedging, filler, or restating the question.
