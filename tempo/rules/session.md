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
