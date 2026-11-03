"""One model call run by a host subagent (AGENT_PLAN.md section 3.1).

`prepare` renders, spools the prompt in parts, snapshots the trees, and journals the effect intent; the host
spawns the named subagent, which writes `output.txt`; `submit` checks confinement and the deadline, classifies
`stop.txt`, runs steps 5-8 (`check_reply` plus the review hook), and journals the completion.

A charged outcome completes the effect, so the next `prepare` opens the next call_seq with its
findings. An uncharged one (infra_error, provider_drought) and a timeout are journaled as a
`subagent_call_closed` note; the next `prepare` re-opens the same call_seq under a new call id.
"""

import hashlib
import json
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from tempo.artifacts import Finding, Outcome, StageResult
from tempo.caps import consume
from tempo.checkout import Refusal
from tempo.config import Config, ConfigSnapshot, Severity
from tempo.driver import LlmStage, Spool, check_reply
from tempo.effects import Effects
from tempo.enginelog import BACKUPS as LOG_BACKUPS
from tempo.git import Git
from tempo.hosts import agent_path
from tempo.journal import Event, EventType, Journal, render_ts
from tempo.journal import run_seq as journal_run_seq
from tempo.llm import AgentEffort, AgentTier
from tempo.llmeffect import llm_key
from tempo.providers import FAILURE_MARKERS, WRITING_SURFACES
from tempo.redact import Redactor
from tempo.seams import Clock, FileSystem
from tempo.specs import check_render_bound
from tempo.thresh import record_outcome
from tempo.tickets import TICKET_FILE, TICKETS_DIR
from tempo.timers import Timers

# Contract constants (TEMPO_PLAN.md section 22), set from the P4c live gate.
READ_PART_CHARS = 20_000
RETRO_STUCK_S = 5400.0  # chupa's call_timeout default: the retro call deadline
CALL_CLOSED = "subagent_call_closed"
OWN_FILES = ("output.txt", "stop.txt", "call.json")
# State the session, hooks, jobs, and engine log write while a subagent runs: outside the confinement snapshot.
SIDE_STATE = ("session.json", "hooks", "jobs", "engine.log", *(f"engine.log.{i}" for i in range(1, LOG_BACKUPS + 1)))
# Every surface that runs as a subagent; `pins` keys come from this set.
SURFACES = ("implement", "review", "requisition_review", "triage", "author", "retro")
# Codex carries effort; `max` is the highest level every Codex model offers (gpt-6-luna has no `ultra`).
CODEX_EFFORT = {"low": "low", "medium": "medium", "high": "high", "max": "max"}
AUTH_MARKERS = ("invalid api key", "please run /login", "not logged in", "oauth token has expired",
                "401 unauthorized", "token has expired")
DROUGHT = frozenset({"quota_exhausted", "rate_limited", "outage"})

State = Literal["pending", "completed", "closed"]


def resolve_model(config: Config | ConfigSnapshot, host: str, surface: str, tier: AgentTier) -> str:
    entry = config.hosts[host]
    return entry.pins.get(surface) or getattr(entry.models_by_tier, tier)


def agent_name(host: str, surface: str, model: str, effort: AgentEffort) -> str:
    clean = "".join(c if c.isalnum() or c in ".-" else "-" for c in model)
    name = f"tempo-{surface}-{clean}"
    return f"{name}-{CODEX_EFFORT[effort]}" if host == "codex" else name


def classify_stop(message: str) -> str | None:
    text = message.lower()
    markers = (("auth_error", AUTH_MARKERS), *FAILURE_MARKERS)
    return next((kind for kind, words in markers if any(w in text for w in words)), None)


@dataclass(frozen=True)
class CallSpec:
    """What `Driver.run` takes, plus the read root named in the prompt header."""

    stage: LlmStage
    consumed: Any
    ticket: str | None
    attempt: int
    workspace: Path
    tier: AgentTier
    effort: AgentEffort
    stuck_budget: float
    run_seq: int | None = None
    slot: str | None = None  # the key's stem when it is neither the ticket nor the surface
    findings: tuple[Finding, ...] | None = None  # re-prompt past a final reply with these findings
    context: Mapping[str, Any] | None = None  # what `submit` needs to finish a call outside the ticket flow


@dataclass(frozen=True)
class Prepared:
    call: str
    state: State  # "pending": spawn `agent` with `spawn_prompt`; "completed": nothing to spawn
    agent: str | None = None
    spawn_prompt: str | None = None
    parts: tuple[str, ...] = ()
    reply: str | None = None


@dataclass(frozen=True)
class Submitted:
    state: State  # "pending": call submit again; "completed": charged; "closed": uncharged or timeout
    outcome: Outcome | Literal["provider_drought"] | None = None
    findings: list[Finding] = field(default_factory=list)
    final: bool = False  # the stage is over: ok, a terminal finding, or the retry cap spent
    result: StageResult | None = None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(snapshot: Mapping[str, Any]) -> str:
    return _sha(json.dumps(snapshot, sort_keys=True).encode())


def _intents(history: list[Event]) -> dict[str, Event]:
    return {e.body["call_id"]: e for e in history
            if e.type == EventType.EFFECT_INTENT and "call_id" in e.body}


def _closures(history: list[Event]) -> dict[str, Event]:
    out = {}
    for e in history:
        if e.type == EventType.EFFECT_COMPLETION and "call" in e.body:
            out[e.body["call"]["call_id"]] = e
        elif e.type == EventType.SIGNAL and e.body.get("signal") == CALL_CLOSED:
            out[e.body["call_id"]] = e
    return out


def call_record(intent: Event) -> dict[str, Any]:
    return json.loads((Path(intent.body["dir"]) / "call.json").read_text())


def issued(history: list[Event], call_id: str) -> dict[str, Any] | None:
    """The call.json of a call `prepare` issued, or None."""
    intent = _intents(history).get(call_id)
    return call_record(intent) if intent is not None else None


def closed(history: list[Event], call_id: str) -> bool:
    return call_id in _closures(history)


def open_calls(history: list[Event]) -> list[dict[str, Any]]:
    """Every issued call with no completion or closing note, read back from its call.json."""
    closed = _closures(history)
    return [call_record(e) for cid, e in _intents(history).items() if cid not in closed]


async def _no_notify(failure: str, message: str) -> None:
    return None


class Calls:
    def __init__(self, *, config: Config | ConfigSnapshot, host: str, session: str, repo: Path,
                 journal: Journal, spool: Spool, redactor: Redactor, git: Git, fs: FileSystem,
                 clock: Clock, timers: Timers, severity: Mapping[str, Severity], retry_cap: int,
                 notify: Callable[[str, str], Awaitable[None]] = _no_notify,
                 new_id: Callable[[], str] = lambda: secrets.token_hex(16)) -> None:
        self.config, self.host, self.session, self.repo = config, host, session, repo
        self.journal, self.effects, self.spool, self.redactor = journal, Effects(journal), spool, redactor
        self.git, self.fs, self.clock, self.timers = git, fs, clock, timers
        self.severity, self.retry_cap, self.notify, self.new_id = severity, retry_cap, notify, new_id

    def open_calls(self) -> list[dict[str, Any]]:
        return open_calls(self.journal.read())

    # --- prepare ---------------------------------------------------------------------------------

    async def prepare(self, spec: CallSpec) -> Prepared:
        stage = spec.stage
        if stage.surface not in SURFACES:
            raise Refusal(f"surface {stage.surface!r} does not run as a subagent", f"use one of {SURFACES}")
        stem = spec.slot or spec.ticket or stage.surface
        if spec.run_seq is not None and spec.ticket is not None:
            raise ValueError("run_seq requires ticket=None")
        history = self.journal.read()
        seq = spec.run_seq if spec.run_seq is not None else journal_run_seq(history, stem)
        prefix = llm_key(stem, seq, stage.surface, spec.attempt, 0)[:-1]
        for record in self.open_calls():
            if record["session"] == self.session:
                raise Refusal(f"call {record['call_id']} is open", "submit it before preparing another call")
            if record["key"].startswith(prefix):
                self._abandon(record)
        history = self.journal.read()  # the snapshot starts after the closes, which are not this call's changes

        completions = sorted(
            (e for e in history if e.type == EventType.EFFECT_COMPLETION and "call" in e.body
             and e.key is not None and e.key.startswith(prefix)),
            key=lambda e: int(e.key.rsplit("/", 1)[1]))
        findings: list[Finding] = []
        call_seq = 1
        if completions:
            last = completions[-1].body["call"]
            if last["final"] and spec.findings is None:
                return Prepared(call=last["call_id"], state="completed")
            call_seq = int(completions[-1].key.rsplit("/", 1)[1]) + 1
            findings = (list(spec.findings) if last["final"] else
                        [Finding.model_validate(f) for f in last["findings"]])

        call_id = self.new_id()
        key = llm_key(stem, seq, stage.surface, spec.attempt, call_seq)
        spool_stem = f"{stem}/{spec.run_seq}" if spec.run_seq is not None else stem
        name = f"{stage.surface}/call-{call_seq:02d}"
        call_dir = self.spool.root / spool_stem / str(spec.attempt) / name
        root = spec.workspace
        prompt = f"tempo read root: {root}\n\n" + stage.render(spec.consumed, findings)
        check_render_bound(prompt)
        self.spool.write(spool_stem, spec.attempt, f"{name}/prompt.md", prompt)
        parts = []
        for i in range(0, len(prompt), READ_PART_CHARS):
            part = f"prompt-{len(parts) + 1:02d}.md"
            self.spool.write(spool_stem, spec.attempt, f"{name}/{part}", prompt[i:i + READ_PART_CHARS])
            parts.append(str(call_dir / part))
        reply = str(call_dir / "output.txt")

        model = resolve_model(self.config, self.host, stage.surface, spec.tier)
        agent = agent_name(self.host, stage.surface, model, spec.effort)
        if not agent_path(self.repo, self.host, agent).is_file():
            raise Refusal(f"agent {agent} has no file {agent_path(self.repo, self.host, agent)}",
                          "run `tempo render` to write the agent files for config.yaml")
        snapshot = await self._snapshot(root, call_dir, len(history))
        deadline = render_ts(self.clock() + timedelta(seconds=spec.stuck_budget))
        self.effects.begin(key, ticket=spec.ticket, body={
            "call_id": call_id, "deadline": deadline, "snapshot": _digest(snapshot), "dir": str(call_dir)})
        spawn = "\n".join([f"call: {call_id}", *(f"part: {p}" for p in parts), f"reply: {reply}"])
        record = {
            "call_id": call_id, "session": self.session, "host": self.host, "agent": agent, "model": model,
            "host_reported_model": None, "surface": stage.surface, "key": key, "ticket": spec.ticket,
            "attempt": spec.attempt, "call_seq": call_seq, "spool_stem": spool_stem, "read_root": str(root),
            "deadline": deadline, "parts": parts, "reply": reply, "spawn_prompt": spawn, "snapshot": snapshot,
            "context": dict(spec.context or {}),
        }
        self.fs.write(call_dir / "call.json", (json.dumps(record, indent=2) + "\n").encode())
        return Prepared(call=call_id, state="pending", agent=agent, spawn_prompt=spawn,
                        parts=tuple(parts), reply=reply)

    def _abandon(self, record: dict[str, Any]) -> None:
        """A dead session's call on this key: timeout past its deadline, else uncharged infra_error."""
        late = self.clock() > datetime.fromisoformat(record["deadline"])
        self._close(record, "timeout" if late else "infra_error", "abandoned by an ended session")

    def _close(self, record: dict[str, Any], outcome: str, reason: str) -> None:
        self.journal.append(EventType.SIGNAL, {"signal": CALL_CLOSED, "call_id": record["call_id"],
                                                "key": record["key"], "outcome": outcome, "reason": reason},
                            ticket=record["ticket"], key=None)

    # --- snapshot (section 3.4) ------------------------------------------------------------------

    async def _snapshot(self, root: Path, call_dir: Path, journal_len: int) -> dict[str, Any]:
        trees = {}
        for tree in dict.fromkeys([self.repo, root]):
            changed = {}
            for rel in await self.git.changed_paths(tree):
                path = tree / rel
                changed[rel] = _sha(path.read_bytes()) if path.is_file() else None
            trees[str(tree)] = {"head": await self.git.rev_parse(tree, "HEAD"), "changed": changed}
        state_dir = Path(self.config.state_dir)
        skip = {state_dir / "journal", Path(self.config.worktree_root), state_dir / "tempo.lock",
                *(state_dir / name for name in SIDE_STATE), *(call_dir / name for name in OWN_FILES)}
        state = {}
        if state_dir.is_dir():
            for path in sorted(state_dir.rglob("*")):
                if path.is_file() and not any(path == s or path.is_relative_to(s) for s in skip):
                    state[str(path.relative_to(state_dir))] = _sha(path.read_bytes())
        return {"trees": trees, "state": state, "journal": journal_len}

    def _confinement(self, record: dict[str, Any], now: dict[str, Any], call_id: str) -> list[str]:
        """Every path changed since `prepare` outside this call's own files; implement owns its worktree."""
        then = record["snapshot"]
        stray = []
        writes = record["surface"] in WRITING_SURFACES
        for tree, before in then["trees"].items():
            after = now["trees"][tree]
            own = writes and tree == record["read_root"] and tree != str(self.repo)
            if before["head"] != after["head"]:
                stray.append(f"{tree}: HEAD moved")
            for rel in sorted(set(before["changed"]) | set(after["changed"])):
                if before["changed"].get(rel) == after["changed"].get(rel):
                    continue
                if own and rel != f"{TICKETS_DIR}/{record['ticket']}/{TICKET_FILE}":
                    continue
                stray.append(f"{tree}/{rel}")
        for rel in sorted(set(then["state"]) | set(now["state"])):
            if then["state"].get(rel) != now["state"].get(rel):
                stray.append(f"{self.config.state_dir}/{rel}")
        for e in self.journal.read()[then["journal"]:]:
            if e.body.get("call_id") != call_id:
                stray.append(f"journal: {e.type} {e.key or e.body.get('signal')}")
        return stray

    # --- submit ----------------------------------------------------------------------------------

    async def submit(self, call_id: str, stage: LlmStage) -> Submitted:
        history = self.journal.read()
        intent = _intents(history).get(call_id)
        if intent is None:
            raise Refusal(f"call {call_id} was never issued", "submit only a call id that prepare returned")
        record = call_record(intent)
        if stage.surface != record["surface"]:
            raise ValueError(f"call {call_id} is a {record['surface']} call, not {stage.surface}")
        closing = _closures(history).get(call_id)
        if closing is not None:
            return self._recorded(closing, stage, record)

        call_dir = Path(intent.body["dir"])
        now = await self._snapshot(Path(record["read_root"]), call_dir, record["snapshot"]["journal"])
        if stray := self._confinement(record, now, call_id):
            finding = Finding(code="confinement", message=f"changed outside the call's reply: {', '.join(stray)}",
                              paved_road="write only the reply file the spawn prompt names")
            return await self._complete(record, stage, self._read(call_dir / "output.txt") or "",
                                        "invalid_artifact", [finding], None)

        deadline = datetime.fromisoformat(record["deadline"])
        stop_path = call_dir / "stop.txt"
        if not stop_path.is_file():
            if self.clock() > deadline:
                return self._timeout(record)
            return Submitted(state="pending")
        stop = json.loads(stop_path.read_text())
        if datetime.fromisoformat(stop["stopped_at"]) > deadline:
            return self._timeout(record)

        text = self._read(call_dir / "output.txt")
        checked = check_reply(stage, text, Path(record["read_root"]), self.severity) if text is not None else None
        if checked is None or checked.artifact is None:
            failure = classify_stop(stop.get("message", ""))
            if failure in DROUGHT:
                minutes = self.config.hosts[self.host].quota_window_minutes
                until = self.clock() + timedelta(minutes=minutes)
                if not any(t.body.timer_id.startswith(f"provider-cooldown/{self.host}/")
                           for t in self.timers.pending.values()):
                    self.timers.arm(f"provider-cooldown/{self.host}/{render_ts(until)}", until, ticket=None)
                self._breaker(record, failure)
                if record["ticket"] is not None:
                    # A session cannot loop on an exhausted quota: each drought draws caps.infra.
                    ticket_sha = await self.git.rev_parse(self.repo, f"HEAD:{TICKETS_DIR}/{record['ticket']}/{TICKET_FILE}")
                    consume(self.journal, record["ticket"], "infra", ticket_sha)
                self._close(record, "provider_drought", failure)
                return Submitted(state="closed", outcome="provider_drought")
            if failure in ("auth_error", "model_error"):
                self._breaker(record, failure)
                await self.notify(failure, self.redactor.scrub(stop.get("message", "")))
                self._close(record, "infra_error", failure)
                return Submitted(state="closed", outcome="infra_error")
            missing = Finding(code="invalid_artifact", message="the subagent wrote no reply file",
                              paved_road=f"write the reply JSON to {record['reply']}")
            self._breaker(record, "unclassified" if text is None else None)
            return await self._complete(record, stage, text or "", "invalid_artifact",
                                        checked.hard if checked else [missing], None)

        self._breaker(record, None)
        hard = list(checked.hard)
        if stage.review is not None:
            review = await stage.review(checked.artifact, record["call_seq"])
            if review.verdict == "fail":
                hard.extend(review.findings)
        if hard:
            terminal = any(f.code in stage.terminal_findings for f in hard)
            return await self._complete(record, stage, text, "gate_failed", hard, None, terminal=terminal)
        if record["surface"] in WRITING_SURFACES and getattr(checked.artifact, "outcome", None) == "ok":
            await self._commit(record)
        return await self._complete(record, stage, text, "ok", checked.soft, checked.artifact)

    def _breaker(self, record: dict[str, Any], failure: str | None) -> None:
        record_outcome(self.journal, self.clock, self.config.circuit_breaker, self.host, failure,
                       ticket=record["ticket"], call_key=record["key"])

    @staticmethod
    def _read(path: Path) -> str | None:
        return path.read_text() if path.is_file() else None

    def _timeout(self, record: dict[str, Any]) -> Submitted:
        self._close(record, "timeout", "past the call deadline")
        return Submitted(state="closed", outcome="timeout", final=True,
                         result=StageResult(outcome="timeout", artifact=None, findings=[],
                                            provider=self.host, model=record["model"]))

    async def _commit(self, record: dict[str, Any]) -> None:
        """Implement never runs git: commit its worktree changes outside the ticket dir to its branch."""
        tree = Path(record["read_root"])
        outbox = f"{TICKETS_DIR}/{record['ticket']}/"
        paths = [p for p in await self.git.changed_paths(tree) if not p.startswith(outbox)]
        if paths:
            await self.git.add(tree, paths)
            await self.git.commit(tree, f"tempo({record['ticket']}): implement call {record['call_seq']}",
                                  only=paths)

    async def _complete(self, record: dict[str, Any], stage: LlmStage, text: str, outcome: Outcome,
                        findings: list[Finding], artifact: BaseModel | None, *, terminal: bool = False) -> Submitted:
        call_dir = Path(record["reply"]).parent
        scrubbed = self.redactor.scrub(text)
        self.spool.write(record["spool_stem"], record["attempt"], f"{record['surface']}/call-{record['call_seq']:02d}/output.txt", scrubbed)
        final = outcome == "ok" or terminal or record["call_seq"] >= self.retry_cap + 1
        head = None
        if record["surface"] in WRITING_SURFACES:
            head = await self.git.rev_parse(Path(record["read_root"]), "HEAD")
        current = json.loads((call_dir / "call.json").read_text())
        self.effects.complete(
            record["key"], {"text": scrubbed, "provider": self.host, "model": record["model"]},
            ticket=record["ticket"], served=lambda r: {"provider": r["provider"], "model": r["model"]},
            extra={"model_source": {"resolved": record["model"], "host_reported": current.get("host_reported_model")},
                   "call": {"call_id": record["call_id"], "outcome": outcome, "final": final, "head": head,
                            "findings": [f.model_dump(mode="json") for f in findings]}})
        result = StageResult(outcome=outcome, artifact=artifact, findings=findings,
                             provider=self.host, model=record["model"]) if final else None
        return Submitted(state="completed", outcome=outcome, findings=findings, final=final, result=result)

    def _recorded(self, closing: Event, stage: LlmStage, record: dict[str, Any]) -> Submitted:
        """A closed call answers with what was journaled; a completed ok reply re-validates to its artifact."""
        if closing.type == EventType.SIGNAL:
            outcome = closing.body["outcome"]
            if outcome == "timeout":
                return Submitted(state="closed", outcome="timeout", final=True,
                                 result=StageResult(outcome="timeout", artifact=None, findings=[],
                                                    provider=self.host, model=record["model"]))
            return Submitted(state="closed", outcome=outcome)
        call = closing.body["call"]
        findings = [Finding.model_validate(f) for f in call["findings"]]
        artifact = None
        if call["outcome"] == "ok":
            artifact = check_reply(stage, closing.body["result"]["text"], Path(record["read_root"]),
                                   self.severity).artifact
        result = StageResult(outcome=call["outcome"], artifact=artifact, findings=findings,
                             provider=closing.body["result"]["provider"],
                             model=closing.body["result"]["model"]) if call["final"] else None
        return Submitted(state="completed", outcome=call["outcome"], findings=findings,
                         final=call["final"], result=result)
