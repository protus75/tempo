# tempo

Interim conduct for chat sessions in this repo, from AGENT_PLAN.md section 5.1 (P0). P1 replaces this
file with the carried one; AGENTS.md carries the same rules, and a rule change touches both files.

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
