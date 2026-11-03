# Tempo tickets

Written by `tempo init`; a later `tempo init` leaves an edited copy alone.

A ticket is one file, `tickets/<stem>/ticket.md`: YAML frontmatter between `---` fences, then body
sections. `tempo new <stem>` writes an empty one. `tempo lint <stem>` checks a filled one and prints
each finding with what to do instead. A ticket runs only once lint exits 0.

## Stem

The directory name is the stem: lowercase kebab-case, 2-64 characters, starting with a letter or digit
(`^[a-z0-9][a-z0-9-]{1,63}$`). It is also the branch name. `decisions`, `orchestrator`, and `retro` are
reserved.

## Frontmatter

Only these fields, nothing else:

| Field | Values |
|---|---|
| `state` | `draft`, `confirmed`, `rejected`, `merged`. Write `confirmed`; a `source: human` ticket confirms itself. |
| `source` | `human`. `box:<class>` is machine-only. |
| `priority` | `P0`, `P1`, `P2`, `P3` |
| `kind` | `bug`, `feature`, `chore`. Set it explicitly. |
| `agent_tier` | `low`, `medium`, `high`, `max`. Default `medium`; raise it only for a known-hard ticket. |
| `agent_effort` | same scale. Default `medium`. |
| `gate_bypass` | a list of `{code, reason}`; `code` must be a gate code. Usually absent. |

Ordering goes in `priority` and `## Depends on`, never in prose.

## Body sections

Every section below is required and non-empty, except `On-demand` and `Regression`.

- `## Depends on`: bullets of existing ticket stems, or `none`. The graph must stay acyclic. Add an edge
  whenever this ticket uses another unmerged ticket's work.
- `## Context`: bullets, one existing repo-relative file per bullet, read before writing. A file the
  ticket creates goes in `## Scope fence` and `## Verification` only.
- `## On-demand` (optional): bullets of existing files too large to embed. Each must be covered by
  `## Scope fence` and absent from `## Context`.
- `## Goal / Why`: one observable outcome after merge, and why it matters.
- `## Scope in / Scope out`: what changes, and the nearby changes that must not happen.
- `## Scope fence`: bullets, one repo-relative path prefix per bullet. The ticket may write only there.
- `## Acceptance criteria`: each item measurable ("exits 0 when...", "is unchanged") and naming its
  check in backticks. "improved", "better", and "cleaner" are refused.
- `## Verification`: one fenced code block, one command per line. Commands are split like a POSIX shell
  line but run without a shell, so no pipes, `&&`, or redirects.
- `## Regression` (exactly when `kind: bug`): one fenced command that reproduces the defect, then one or
  more `- carries: <path-prefix>` bullets naming the test files the branch adds for it. The command
  must pass on the branch and fail on the merge base.
- `## Definition of rejected`: when to stop and give up rather than keep trying.
- `## Time budget`: one bullet, `- stuck: <minutes>m`.

## Splitting

One ticket per feature and set of files it changes, never sized to fit a context window.
