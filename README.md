# tempo

tempo is a continuously running orchestration engine that authors and runs
tickets against host repos. TEMPO_PLAN.md is canonical for everything.

The lifecycle is two commands, run in order:

    uv run python -m tempo drain
    uv run python -m tempo serve             # start the continuous daemon on host work

Once `serve` is running, control it with `kill` / `pause` / `resume`; `status`
and `doctor` inspect at any time. Section 18 lists every verb.
