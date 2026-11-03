# tempo

tempo runs tickets against a host repo with agents in the VS Code extensions of Claude Code, GitHub
Copilot, and Codex. A human writes a ticket; tempo carries it to merge or to a recorded decision.
TEMPO_PLAN.md is canonical for the product, AGENT_PLAN.md for the agent design.

Install the `tempo` command, then set a repo up:

    uv tool install tempo-agents
    cd <your repo>
    tempo init                  # config.yaml, managed blocks, hook and agent files, docs/tempo/tickets.md
    git add -A && git commit -m "tempo init"

`tempo init` is safe to rerun; it changes nothing when the repo is current. After editing
`config.yaml`, run `tempo render`.

Write a ticket with `tempo new <stem>`, fill `tickets/<stem>/ticket.md` per `docs/tempo/tickets.md`,
and run `tempo lint <stem>` until it exits 0. Then type `/tempo-run` in the extension's chat to start
the orchestrator. `tempo status` and `tempo doctor` inspect at any time.
