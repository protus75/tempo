# tempo orchestrator

You run tempo in this chat. Every action is a shell command `tempo tool <name> '<json args>'`, which
prints one JSON result. Your shell runs nothing else, and you never write or edit files: hooks deny
both. You may read and search files.

A result with `"ok": false` is a refusal. Do what its `do_instead` says, or end the session.

## Start

1. `tempo tool begin_session '{}'` returns your session token. Pass it as `"session"` in every later
   call.
2. `tempo tool status '{"session": "<token>"}'`. If it lists an open call of your session, finish that
   call with step 3c before anything else.

## One ticket, in this order

1. `list_eligible`. Take the first stem. If none is eligible, end the session.
2. `start` with `{"stem": "<stem>"}`.
3. For each surface in this order: `requisition_review`, `implement`, `review`:
   a. `prepare` with `{"stem": "<stem>", "surface": "<surface>"}`.
      - `"state": "completed"`: nothing to spawn; go to c.
      - `"state": "pending"`: spawn the subagent named in `"agent"`, with exactly `"spawn_prompt"` as
        its whole prompt. Add no other text, and spawn nothing else.
   b. Wait for the subagent to finish. Ignore its final message.
   c. `submit` with `{"call": "<call>"}`, again until `"state"` is not `"pending"`. When the call is
      past its `"deadline"`, stop the subagent with your host's stop tool first, then submit.
   d. `"final": false`: go back to a; prepare re-prompts with the findings. `"final": true` with
      `"outcome": "ok"`: next surface. Anything else is a failure.
   e. After implement, run `check` with `{"stem": "<stem>"}`. It returns a job: call `job` with
      `{"id": "<id>"}` until `"state"` is `"done"`. A result outcome other than `"ok"` is a failure.
4. `merge` with `{"stem": "<stem>"}`, then poll `job` the same way. `"ok"` means merged: go to step 1.

## End

On any failure, a refusal you cannot clear, a `provider_drought` or `infra_error` outcome, or a
refusal for `max_tickets` or `max_minutes`, end the session: call `write_handoff` with
`{"text": "<the stem, stage, outcome, and findings, and what the next session should do>"}`, then
stop. Also end it when nothing is eligible.
