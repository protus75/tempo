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
      `"outcome": "ok"`: next surface. Anything else is a failure: go to Decide.
   e. After implement, run `check` with `{"stem": "<stem>"}`. It returns a job: call `job` with
      `{"id": "<id>"}` until `"state"` is `"done"`. A result outcome other than `"ok"` is a failure: go to Decide.
4. `merge` with `{"stem": "<stem>"}`, then poll `job` the same way. `"ok"` means merged: go to step 1.
   Anything else is a failure: go to Decide.

## Decide

After a failure, call `read_attempts` with `{"stem": "<stem>"}` and read the findings. A
`provider_drought` or `auth_error` outcome ends the session instead (see End). Otherwise call exactly
one of these with `{"stem": "<stem>", "lessons": "<what this attempt taught, for the next attempt>"}`:

- `retry`: the findings name a fixable mistake; the same model can clear them.
- `escalate`: the attempt fell short of the task; a stronger model is needed.
- `park`: the ticket itself is wrong or unclear, so no attempt can pass; it waits for a ticket edit.
- `reject`: the ticket is not worth doing.

A refusal says which of the others to use. Then go to step 1 of One ticket.

`status` lists parked tickets and storm `holds`. Call `reoffer` with `{"stem", "lessons"}` on a parked
ticket, or `release_hold` with `{"hold_id", "lessons"}` on a hold, only when you can say what changed
since. Call `file_box` with `{"summary": "<text>"}` for a problem outside the ticket you are on.

## End

On a refusal you cannot clear, a `provider_drought` or `auth_error` outcome, or a refusal for
`max_tickets` or `max_minutes`, end the session: call `write_handoff` with
`{"text": "<what happened, what is parked or held, and what the next session should do>"}`, then
stop. Also end it when nothing is eligible. On `auth_error`, first call `notify` with
`{"message": "<what a person must fix>"}`.
