"""Explicit lock-held checkpoint pushes (TEMPO_PLAN.md 19.P3.checkpoint-push)."""

from datetime import datetime, timedelta
from pathlib import Path
import re

from tempo.box import Box
from tempo.effects import Effects
from tempo.enginelog import EngineLog
from tempo.git import Git, GitError
from tempo.journal import Event, EventType, Journal, JournalCorruption
from tempo.seams import Clock
from tempo.storm import arrival_id
from tempo.timers import Timers

CHECKPOINT_MERGES = 5
CHECKPOINT_INTERVAL = timedelta(hours=24)
_PUSH = "checkpoint-push/"
_FAILED = "checkpoint-push-failed/"
_KIND = "checkpoint_push_failed"


def _invalid(reason: str) -> ValueError:
    return ValueError(f"invalid checkpoint evidence: {reason}; repair the producing evidence, "
                      "never overwrite journal history")


def _merged(events: list[Event]) -> int:
    return sum(event.type == EventType.STATE_TRANSITION and event.body.get("to") == "merged"
               and event.body.get("commit") is not None for event in events)


def _history(events: list[Event]) -> tuple[list[Event], list[Event]]:
    completed, failures = [], {}
    intents: dict[str, int] = {}
    for event in events:
        key, body = event.key, event.body
        if key is not None and key.startswith(_PUSH):
            if re.fullmatch(r"checkpoint-push/(0|[1-9][0-9]*)", key) is None:
                raise _invalid("push key must name a decimal window")
            if event.ticket is not None or key != f"{_PUSH}{len(completed)}":
                raise _invalid("push must own the current window and a null ticket")
            if event.type == EventType.EFFECT_INTENT and body == {}:
                intents[key] = intents.get(key, 0) + 1
            elif event.type == EventType.EFFECT_COMPLETION:
                result = body.get("result")
                if (set(body) != {"result"} or not isinstance(result, dict)
                        or set(result) != {"merged"} or type(result["merged"]) is not int
                        or result["merged"] < 0 or key not in intents
                        or f"{_FAILED}{len(completed)}/{intents[key]}" in failures):
                    raise _invalid("completion requires a successful intent and exact merged result")
                completed.append(event)
            else:
                raise _invalid("push records must be empty intents or successful completions")
        if body.get("kind") != _KIND and not (key is not None and key.startswith(_FAILED)):
            continue
        if (event.type != EventType.SIGNAL or event.ticket is not None
                or set(body) != {"kind", "window", "attempt", "error_code"}
                or body.get("kind") != _KIND or type(body.get("window")) is not int
                or body["window"] < 0 or type(body.get("attempt")) is not int
                or body["attempt"] < 1 or body.get("error_code") not in ("git_exit", "process_failure")
                or key != f"{_FAILED}{body['window']}/{body['attempt']}"):
            raise _invalid("failure must have the exact signal, key and body shape")
        prior = failures.get(key)
        if prior is not None:
            if prior.body != body:
                raise _invalid("failure key already owns a different body")
            continue
        if (body["window"] != len(completed)
                or intents.get(f"{_PUSH}{body['window']}", 0) != body["attempt"]):
            raise _invalid("failure requires its corresponding preceding push intent")
        failures[key] = event
    return completed, list(failures.values())


class Checkpoint:
    """Idle construction; every poll runs under the supplied writers' existing lock."""

    def __init__(self, repo: Path, *, journal: Journal, effects: Effects, timers: Timers,
                 git: Git, box: Box, clock: Clock, log: EngineLog) -> None:
        self.repo, self.journal, self.effects, self.timers = repo, journal, effects, timers
        self.git, self.box, self.clock, self.log = git, box, clock, log

    def _report(self, event: Event) -> None:
        body = event.body
        self.box.enqueue(message_class="failure_report", origin="checkpoint-push",
                         stage=None, outcome=None, reason="checkpoint push failed",
                         summary=(f"Checkpoint push to origin/main failed ({body['error_code']}); "
                                  "repair the remote or credentials and allow the next checkpoint poll to retry"),
                         occurrence_id=arrival_id("checkpoint-push", body["window"], body["attempt"]))

    async def poll(self) -> None:
        completed, failures = _history(self.journal.read())
        for failure in failures:
            self._report(failure)
        window = len(completed)
        key = f"{_PUSH}{window}"
        baseline = completed[-1].body["result"]["merged"] if completed else 0
        self.timers.reconstruct()
        previous = self.timers.pending.get(key, self.timers.fired.get(key))
        deadline = (previous.deadline if previous is not None else
                    (datetime.fromisoformat(completed[-1].ts) if completed else self.clock())
                    + CHECKPOINT_INTERVAL)
        self.timers.arm(key, deadline, ticket=None)
        self.timers.fire_due()
        if _merged(self.journal.read()) - baseline < CHECKPOINT_MERGES and self.clock() < deadline:
            return

        # Only the await of Git.push can earn a soft failure. Durable writes and
        # history reads outside it must propagate, including Effects' completion.
        pushing, attempt = False, 0

        async def push() -> dict:
            nonlocal pushing, attempt
            events = self.journal.read()
            merged = _merged(events)
            attempt = sum(event.type == EventType.EFFECT_INTENT and event.key == key
                          for event in events)
            pushing = True
            await self.git.push(self.repo, "origin", "main")
            pushing = False
            return {"merged": merged}

        try:
            await self.effects.run(push, key=key, ticket=None)
        except Exception as exc:
            if not pushing or isinstance(exc, JournalCorruption):
                raise
            error_code = "git_exit" if isinstance(exc, GitError) else "process_failure"
            failure = self.journal.append(EventType.SIGNAL, {
                "kind": _KIND, "window": window, "attempt": attempt, "error_code": error_code},
                ticket=None, key=f"{_FAILED}{window}/{attempt}")
            diagnostics = {"stdout": exc.out, "stderr": exc.err} if isinstance(exc, GitError) else {}
            self.log.event(_KIND, window=window, attempt=attempt, error=str(exc), **diagnostics)
            self._report(failure)
