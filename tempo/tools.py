"""The orchestrator's tools (AGENT_PLAN.md section 4.1): `tempo tool <name> '<json>'` prints one JSON result.

P4 builds the Read and Run tools, `begin_session`, and `write_handoff`; P5a builds the Decide tools for a
failed run and the rest of Record; P5b adds triage, author, and retro calls, `split_propose`, `split_commit`, and
`confirm`. Every tool except `begin_session` and `status` needs the token `begin_session`
returned. Writing tools take the single-writer lock for their whole call, and refuse while a subagent
call is open or a job is running. `check`, `merge`, and a call's validation in `submit` run as
start-then-poll jobs in a detached worker that holds the writer lock (`run_job`).
"""

import inspect
import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from tempo import render
from tempo.artifacts import REWORK_ORDER, Finding, StageResult
from tempo.author import (AUTHOR_STUCK_S, AuthorReply, author_render, author_spec, author_stage, discard,
                          failure_decision, publish, stamped)
from tempo.backlog import (PREMISE, REOFFERED, SETTLED, admit, confirm_draft, eligible, parked, reject_ticket,
                           start_run, supersedes_maps)
from tempo.box import BOX_DIR, MESSAGE_CLASSES, Box, BoxError, Message, RereportJournal
from tempo.call import RETRO_STUCK_S, Calls, CallSpec, closed, issued, open_calls
from tempo.caps import IDENTICAL_K, capability, consume, identical, next_rung, spent
from tempo.checkout import SPECS_DIR, Checkout, Refusal
from tempo.control import CONTROL_DECISION
from tempo.driver import Driver, LlmStage, unrendered, unwrap_fence
from tempo.effects import Effects
from tempo.git import GitError
from tempo.hooks import CLAIM, HOOKS_DIR
from tempo.hosts import LAYOUTS
from tempo.jobs import Jobs
from tempo.journal import TERMINAL_STATES, Dispatch, EventType, render_ts, run_seq
from tempo.llmeffect import llm_key
from tempo.lockfile import Lockfile, LockHeld, Sessions
from tempo.merge import merge
from tempo.notify import send
from tempo.requisition import REQUISITION_STUCK_S, RequisitionReply, requisition
from tempo.retro import Retro, retro_stage
from tempo.seams import ExecutableNotFound, Notifications
from tempo.specs import RenderOverBound
from tempo.split import TicketProposal, check_stems, check_tickets, record_supersedes
from tempo.stages import (ImplementReply, StageContext, check, findings_text, finish_implement, finish_review,
                          harvest, implement_stage, packing_slip, prepare_worktree, prior_attempts, review_stage,
                          run_wall_seconds)
from tempo.status import last_states, project
from tempo.triage import TRIAGE_STUCK_S, attribute_escape, report_intake, settle, settle_ready, triage_render, triage_spec, triage_stage
from tempo.storm import StormLedger
from tempo.status import render as render_status
from tempo.thresh import fold_breakers
from tempo.tickets import (TICKET_FILE, TICKETS_DIR, Ticket, TicketInvalid, _porcelain, parse_ticket, stem_findings,
                           ticket_path, validate_ticket)
from tempo.timers import Timers

# Contract constant (TEMPO_PLAN.md section 22): below Claude's 120 s shell-command timeout, with margin.
TOOL_CALL_S = 90.0
# The surfaces of the ticket flow; triage, author, retro, and the other requisition reviews take other arguments.
FLOW_SURFACES = ("requisition_review", "implement", "review")
PREPARE_ROAD = ('pass one of {"surface": "requisition_review"|"implement"|"review", "stem"}, {"surface": "triage"'
                '|"author"|"requisition_review", "message"}, {"surface": "requisition_review", "stem", "proposal"},'
                ' or {"surface": "retro"}')
# A requisition review rebuilt for submit: its reply checks need no ticket.
_REQUISITION = LlmStage(surface="requisition_review", emits=RequisitionReply, gates=[], render=unrendered)
HANDOFF = f"{TICKETS_DIR}/orchestrator/handoff.md"
DECISIONS = "decisions.md"
STAGE_FAILED = "stage_failed"
NOT_FAILED = ("ok", "already_satisfied")


@dataclass(frozen=True)
class _Tool:
    handler: str
    session: bool = True  # needs the session token
    writes: bool = False  # takes the writer lock; refused while a call is open or a job runs
    late: bool = False  # still served past orchestrator.session.max_minutes


TOOLS = {
    "begin_session": _Tool("begin_session", session=False, writes=True),
    "status": _Tool("status", session=False, late=True),
    "list_eligible": _Tool("list_eligible"),
    "read_ticket": _Tool("read_ticket"),
    "read_attempts": _Tool("read_attempts"),
    "read_box": _Tool("read_box", writes=True),
    "start": _Tool("start", writes=True),
    "prepare": _Tool("prepare", writes=True),
    "submit": _Tool("submit", late=True),
    "check": _Tool("check", writes=True),
    "merge": _Tool("merge", writes=True),
    "job": _Tool("job", late=True),
    "write_handoff": _Tool("write_handoff", writes=True, late=True),
    "retry": _Tool("retry", writes=True),
    "escalate": _Tool("escalate", writes=True),
    "reject": _Tool("reject", writes=True),
    "park": _Tool("park", writes=True),
    "reoffer": _Tool("reoffer", writes=True),
    "release_hold": _Tool("release_hold", writes=True),
    "split_propose": _Tool("split_propose", writes=True),
    "split_commit": _Tool("split_commit", writes=True),
    "confirm": _Tool("confirm", writes=True),
    "file_box": _Tool("file_box", writes=True),
    "notify": _Tool("notify", writes=True),
}


def _json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_json(v) for v in value]
    return value


def _reason(outcome: str, findings: list[Any]) -> str:
    """The terminal reason, derived from the outcome alone (section 4.1), never from orchestrator text."""
    codes = {f["code"] if isinstance(f, dict) else f.code for f in findings}
    return ",".join(sorted(codes)) or outcome


def _need(lessons: str) -> None:
    if not lessons.strip():
        raise Refusal("lessons is blank", "pass what this attempt taught, for the next attempt and the handoff")


def _result(result: StageResult) -> dict[str, Any]:
    return {"outcome": result.outcome, "findings": _json(result.findings),
            "artifact": _json(result.artifact) if result.artifact is not None else None}


class Tools:
    def __init__(self, checkout: Checkout, *, launch: Callable[[str, int], None], notifications: Notifications,
                 new_id: Callable[[], str] = lambda: secrets.token_hex(16)) -> None:
        self.c = checkout
        config, clock, fs = checkout.config, checkout.clock, checkout.fs
        self.launch, self.notifications, self.new_id = launch, notifications, new_id
        self.state = Path(config.state_dir)
        self.sessions = Sessions(self.state, clock=clock, fs=fs)
        self.jobs = Jobs(self.state, fs=fs, clock=clock, new_id=new_id)
        self.lock = Lockfile(self.state, instance_id="tempo-tool", clock=clock)
        driver = Driver.from_config(config, clock=clock, fs=fs)
        driver.journal = checkout.journal
        driver.effects = Effects(checkout.journal)
        self.driver = driver
        self.ctx = StageContext(repo=checkout.repo, config=config, env=checkout.env, exec_=checkout.exec_,
                                git=checkout.git, fs=fs, driver=driver, specs_dir=SPECS_DIR)
        self.session: dict[str, Any] | None = None

    # --- dispatch --------------------------------------------------------------------------------

    async def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        tool = TOOLS.get(name)
        if tool is None:
            raise Refusal(f"no tool {name!r}", f"use one of {', '.join(TOOLS)}")
        handler = getattr(self, tool.handler)
        args = dict(args)
        token = args.pop("session", None)
        try:
            inspect.signature(handler).bind(**args)
        except TypeError as e:
            raise Refusal(f"bad arguments for {name}: {e}",
                          f"pass {list(inspect.signature(handler).parameters)} plus session") from None
        if tool.session:
            self.session = self.sessions.check(token)
            minutes = self.c.config.orchestrator.session.max_minutes
            started = datetime.fromisoformat(self.session["started_at"])
            if not tool.late and self.c.clock() > started + timedelta(minutes=minutes):
                raise Refusal(f"the session is past orchestrator.session.max_minutes ({minutes})",
                              "end the session with write_handoff")
        if tool.writes:
            if name != "begin_session":
                self._one_thing_at_a_time()
            try:
                self.lock.acquire()
            except LockHeld:
                raise Refusal("another tempo process holds the writer lock",
                              "poll `job` until the running job is done, then retry") from None
            try:
                return await handler(**args)
            finally:
                self.lock.release()
        return await handler(**args)

    def _one_thing_at_a_time(self) -> None:
        assert self.session is not None
        if mine := [c for c in open_calls(self.c.journal.read()) if c["session"] == self.session["token"]]:
            raise Refusal(f"call {mine[0]['call_id']} is open", "submit it until it is no longer pending")
        if running := self.jobs.active():
            raise Refusal(f"job {running[0]['id']} ({running[0]['kind']}) is running",
                          f"poll `job` with id {running[0]['id']} until it is done")

    def _calls(self, host: str, session: str) -> Calls:
        timers = Timers(journal=self.c.journal, clock=self.c.clock, sleep=self.c.sleep)
        timers.reconstruct()
        return Calls(config=self.c.config, host=host, session=session, repo=self.c.repo, journal=self.c.journal,
                     spool=self.driver.spool, redactor=self.driver.redactor, git=self.c.git, fs=self.c.fs,
                     clock=self.c.clock, timers=timers, severity=self.driver.severity,
                     retry_cap=self.c.config.caps.retry, notify=self._notify, new_id=self.new_id)

    async def _notify(self, failure: str, message: str) -> None:
        """Chupa's notification transport, behind the `notify` tool and tempo's own escalations."""
        argv = self.c.config.notify
        if argv is None:
            return
        await send(self.driver.effects, self.notifications, argv, owner="tempo", escalation=failure,
                   identity=render_ts(self.c.clock()), message=message, ticket=None)

    def _ticket(self, stem: str) -> Ticket:
        if findings := stem_findings(stem):
            raise Refusal(findings[0].message, findings[0].paved_road)
        path = self.c.repo / ticket_path(stem)
        if not path.is_file():
            raise Refusal(f"{ticket_path(stem)} does not exist", "pick a stem from list_eligible")
        try:
            return validate_ticket(stem, path.read_text(), self.c.repo)
        except TicketInvalid as e:
            raise Refusal(f"{ticket_path(stem)} is invalid: " + "; ".join(f.message for f in e.findings),
                          "fix the ticket in a chat outside the tempo session") from None

    def _running(self, stem: str) -> tuple[Ticket, int]:
        """The ticket at its effective capability and its run number; refuse unless `start` began the run."""
        ticket = self._ticket(stem)
        events = self.c.journal.read()
        if last_states(events).get(stem) != "running":
            raise Refusal(f"{stem} is not running", f"call start with {{\"stem\": \"{stem}\"}} first")
        if not self.ctx.worktree(stem).is_dir():
            raise Refusal(f"{stem} has no worktree", f"call start with {{\"stem\": \"{stem}\"}} again")
        tier, effort = capability(ticket, events)
        ticket = replace(ticket, frontmatter=ticket.frontmatter.model_copy(
            update={"agent_tier": tier, "agent_effort": effort}))
        return ticket, run_seq(events, stem)

    def _cooldown(self, host: str) -> str | None:
        timers = Timers(journal=self.c.journal, clock=self.c.clock, sleep=self.c.sleep)
        timers.reconstruct()
        now = self.c.clock()
        ends = [t.deadline for t in timers.pending.values()
                if t.body.timer_id.startswith(f"provider-cooldown/{host}/") and t.deadline > now]
        breaker = fold_breakers(self.c.journal, now).get(host)
        if breaker is not None and breaker.open_until is not None:
            ends.append(breaker.open_until)
        return render_ts(max(ends)) if ends else None

    # --- session ---------------------------------------------------------------------------------

    async def begin_session(self) -> dict[str, Any]:
        claim_path = self.state / HOOKS_DIR / CLAIM
        if not claim_path.is_file():
            raise Refusal("no hook recorded this chat's begin_session",
                          "run `tempo tool begin_session '{}'` from the /tempo-run chat; if hooks are missing,"
                          " run `tempo render`")
        claim = json.loads(claim_path.read_text())
        claim_path.unlink()
        host = claim["host"]
        if host not in self.c.config.orchestrator.models:
            raise Refusal(f"host {host} has no orchestrator.models entry",
                          f"add orchestrator.models.{host} to config.yaml and run `tempo render`")
        if stale := render.drift(self.c.repo, self.c.config, only={LAYOUTS[host].hooks}):
            raise Refusal(f"hook file {stale[0]} is not current", "run `tempo render`")
        number = 1 + sum(1 for e in self.c.journal.read()
                         if e.type == EventType.SIGNAL and e.body.get("signal") == "session_begun")
        record = self.sessions.begin(token=self.new_id(), host=host, chat=claim["chat"], number=number)
        self.c.journal.append(EventType.SIGNAL, {"signal": "session_begun", "number": number, "host": host},
                              ticket=None)
        session = self.c.config.orchestrator.session
        return {"session": record["token"], "host": host, "number": number,
                "max_tickets": session.max_tickets, "max_minutes": session.max_minutes}

    async def write_handoff(self, text: str) -> dict[str, Any]:
        assert self.session is not None
        number = self.session["number"]
        path = self.c.repo / HANDOFF
        data = f"<!-- tempo session {number} -->\n{text.rstrip()}\n".encode()
        if not path.is_file() or path.read_bytes() != data:
            self.c.fs.write(path, data)
            await self.c.git.add(self.c.repo, [HANDOFF])
            await self.c.git.commit(self.c.repo, f"tempo: session {number} handoff", only=[HANDOFF])
        self.c.journal.append(EventType.SIGNAL, {"signal": "handoff_written", "number": number}, ticket=None)
        self.sessions.end(self.session["token"])
        return {"handoff": HANDOFF, "number": number}

    # --- read ------------------------------------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        events = self.c.journal.read()
        box = Box(self.state / BOX_DIR, self.c.fs, rereport=RereportJournal(self.c.journal))
        held = self.sessions.read()
        session = None if held is None else {k: v for k, v in held.items() if k != "token"}
        calls = [{k: c[k] for k in ("call_id", "surface", "ticket", "agent", "deadline", "spawn_prompt")}
                 | {"live_session": held is not None and c["session"] == held["token"]}
                 for c in open_calls(events)]
        return {"text": render_status(project(events, box=box, policy=self.c.config.box_policy)),
                "session": session, "open_calls": calls,
                "jobs": self.jobs.records()[-5:],
                "parked": await self._parked(events),
                "holds": StormLedger(journal=self.c.journal, clock=self.c.clock).holds(),
                "retro_due": await Retro(self.c, self.driver).due(events) is not None,
                "cooldowns": {h: end for h in self.c.config.orchestrator.models if (end := self._cooldown(h))}}

    async def _parked(self, events: list) -> list[str]:
        out = []
        for stem, to in sorted(last_states(events).items()):
            if to in TERMINAL_STATES and (self.c.repo / ticket_path(stem)).is_file() and parked(
                    events, stem, await self.c.git.rev_parse(self.c.repo, f"HEAD:{ticket_path(stem)}")):
                out.append(stem)
        return out

    async def list_eligible(self) -> dict[str, Any]:
        assert self.session is not None
        if (until := self._cooldown(self.session["host"])) is not None:
            return {"eligible": [], "cooldown_until": until}
        repo = self.c.repo
        dirty = {p for _, p in _porcelain(await self.c.git.status_porcelain(repo))}
        tickets = {}
        for path in sorted((repo / TICKETS_DIR).glob(f"*/{TICKET_FILE}")):
            stem = path.parent.name
            rel = ticket_path(stem)
            if any(p == rel or (p.endswith("/") and rel.startswith(p)) for p in dirty):
                continue
            try:
                tickets[stem] = parse_ticket(stem, path.read_text(), repo)
            except TicketInvalid:
                continue
        events = self.c.journal.read()
        last = last_states(events)
        held = set(StormLedger(journal=self.c.journal, clock=self.c.clock).holds().values())
        out = []
        for ticket in eligible(tickets, events):
            stem = ticket.stem
            sha = await self.c.git.rev_parse(repo, f"HEAD:{ticket_path(stem)}")
            if stem in held or parked(events, stem, sha):
                continue
            if spent(self.c.config.caps, events, stem) is not None:
                continue
            if ticket.stuck_minutes > self.c.config.tickets.max_minutes:
                continue
            out.append({"stem": stem, "priority": ticket.frontmatter.priority, "last": last.get(stem)})
        return {"eligible": out}

    async def read_ticket(self, stem: str) -> dict[str, Any]:
        self._ticket(stem)
        return {"stem": stem, "text": (self.c.repo / ticket_path(stem)).read_text()}

    async def read_attempts(self, stem: str) -> dict[str, Any]:
        self._ticket(stem)
        return {"stem": stem, "text": prior_attempts(self.ctx, stem)}

    async def read_box(self) -> dict[str, Any]:
        """Ingest host reports, retry escape attribution for resolved bug reports, and list what is pending."""
        box = report_intake(self.c)
        for message in box.messages():
            if (message.message_class == "bug_report" and message.resolution is not None
                    and message.resolution.kind == "ticket"):
                await self._attribute(box, message.id)
        return {"pending": [m.model_dump(mode="json") for m in box.pending()]}

    # --- run -------------------------------------------------------------------------------------

    async def start(self, stem: str) -> dict[str, Any]:
        assert self.session is not None
        limit = self.c.config.orchestrator.session.max_tickets
        if self.session["tickets"] >= limit:
            raise Refusal(f"the session has started orchestrator.session.max_tickets ({limit}) tickets",
                          "end the session with write_handoff")
        await admit(stem, self.c)
        last = last_states(self.c.journal.read()).get(stem)
        sha = await self.c.git.rev_parse(self.c.repo, f"HEAD:{ticket_path(stem)}")
        start_run(self.c.journal, stem, sha, reoffer=last in TERMINAL_STATES)
        worktree = await prepare_worktree(self.ctx, stem)
        self.sessions.update({**self.session, "tickets": self.session["tickets"] + 1})
        return {"stem": stem, "attempt": run_seq(self.c.journal.read(), stem), "worktree": str(worktree)}

    async def prepare(self, surface: str, stem: str | None = None, message: str | None = None,
                      proposal: str | None = None) -> dict[str, Any]:
        assert self.session is not None
        if surface in FLOW_SURFACES and stem is not None and message is None and proposal is None:
            return await self._prepare_ticket(stem, surface)
        if surface == "triage" and message is not None and stem is None and proposal is None:
            built = await self._triage(message)
        elif surface == "author" and message is not None and stem is None and proposal is None:
            built = await self._author(message)
        elif surface == "requisition_review" and message is not None and stem is None and proposal is None:
            built = await self._candidate(message)
        elif surface == "requisition_review" and stem is not None and proposal is not None and message is None:
            built = await self._proposal(stem, proposal)
        elif surface == "retro" and stem is None and message is None and proposal is None:
            built = await self._retro()
        else:
            raise Refusal(f"prepare has no {surface!r} call for these arguments", PREPARE_ROAD)
        if isinstance(built, dict):
            return {"state": "completed", "final": True, **built}
        return await self._issue(built)

    async def _issue(self, spec: CallSpec) -> dict[str, Any]:
        assert self.session is not None
        prepared = await self._calls(self.session["host"], self.session["token"]).prepare(spec)
        out = {"call": prepared.call, "state": prepared.state}
        if prepared.state == "pending":
            record = issued(self.c.journal.read(), prepared.call)
            assert record is not None
            out |= {"agent": prepared.agent, "spawn_prompt": prepared.spawn_prompt, "deadline": record["deadline"]}
        return out

    async def _prepare_ticket(self, stem: str, surface: str) -> dict[str, Any]:
        ticket, attempt = self._running(stem)
        worktree = self.ctx.worktree(stem)
        text = (self.c.repo / ticket_path(stem)).read_text()
        stuck = ticket.stuck_minutes * 60.0
        if surface == "implement":
            stage, consumed = implement_stage(self.ctx, ticket, worktree)[0], ticket
        elif surface == "review":
            diff = await self.c.git.diff(self.c.repo, "main", stem)
            stage, consumed = review_stage(self.ctx, text, diff)[0], None
        else:
            req = requisition(self.c.repo, stem, text, SPECS_DIR)
            if (snag := req.precheck()) is not None:
                self._failed(stem, surface, "gate_failed", list(snag.findings))
                return {"state": "completed", "final": True, "outcome": "gate_failed", "verdict": _json(snag)}
            stage, consumed, stuck = req.stage(), None, REQUISITION_STUCK_S
        spec = CallSpec(stage=stage, consumed=consumed, ticket=stem, attempt=attempt, workspace=worktree,
                        tier=ticket.frontmatter.agent_tier, effort=ticket.frontmatter.agent_effort,
                        stuck_budget=stuck)
        try:
            return await self._issue(spec)
        except RenderOverBound as e:
            self._failed(stem, surface, "premise_failed", [e.finding])
            return {"state": "completed", "final": True, "outcome": "premise_failed", "findings": [_json(e.finding)]}

    # --- box: triage and author ------------------------------------------------------------------

    def _box(self) -> Box:
        return Box(self.state / BOX_DIR, self.c.fs, rereport=RereportJournal(self.c.journal))

    def _pending(self, box: Box, id: str) -> Message:
        try:
            message = box.get(id)
        except BoxError:
            message = None
        if message is None or message.status != "pending":
            raise Refusal(f"box message {id!r} is not pending", "pick a message from read_box")
        return message

    def _triage_pass(self) -> int:
        """The session's triage pass: the first triage `prepare` in a session journals it."""
        assert self.session is not None
        passes = [e.body for e in self.c.journal.read()
                  if e.type == EventType.SIGNAL and e.body.get("signal") == "triage_pass"]
        if mine := [b for b in passes if b.get("session") == self.session["number"]]:
            return mine[0]["pass"]
        self.c.journal.append(EventType.SIGNAL, {"signal": "triage_pass", "pass": len(passes),
                                                 "session": self.session["number"]}, ticket=None)
        return len(passes)

    async def _triage(self, id: str) -> CallSpec | dict[str, Any]:
        box = report_intake(self.c)
        message = self._pending(box, id)
        pass_no = self._triage_pass()
        spec = triage_spec()
        if (line := await settle_ready(self.c, self.driver, box, message, spec)) is not None:
            return {"outcome": "ok", "line": line}
        if message.verdict is not None and message.verdict.verdict == "author":
            return {"outcome": "ok", "next": "author"}
        render_message = await triage_render(self.c, box, message, spec)
        return CallSpec(stage=triage_stage(render_message), consumed=message, ticket=None, attempt=pass_no,
                        workspace=self.c.repo, tier=spec.meta.tier, effort=spec.meta.effort,
                        stuck_budget=TRIAGE_STUCK_S, run_seq=message.seq, context={"kind": "triage", "message": id})

    def _author_pass(self, message: Message) -> int | None:
        return next((e.body["pass"] for e in self.c.journal.read() if e.type == EventType.SIGNAL
                     and e.body.get("signal") == "author_invoked" and e.body.get("message") == message.id), None)

    def _completions(self, slot: str, seq: int, surface: str, attempt: int) -> list[Any]:
        prefix = llm_key(slot, seq, surface, attempt, 0)[:-1]
        return sorted((e for e in self.c.journal.read() if e.type == EventType.EFFECT_COMPLETION and "call" in e.body
                       and e.key is not None and e.key.startswith(prefix)),
                      key=lambda e: int(e.key.rsplit("/", 1)[1]))

    def _final_reply(self, slot: str, seq: int, surface: str, attempt: int) -> tuple[int, str] | None:
        """The call_seq and text of a final ok reply on this key prefix, or None."""
        done = self._completions(slot, seq, surface, attempt)
        if done and done[-1].body["call"]["final"] and done[-1].body["call"]["outcome"] == "ok":
            return int(done[-1].key.rsplit("/", 1)[1]), done[-1].body["result"]["text"]
        return None

    def _author_message(self, id: str) -> tuple[Box, Message]:
        box = self._box()
        message = self._pending(box, id)
        if message.verdict is None or message.verdict.verdict != "author":
            raise Refusal(f"triage has not sent box message {id} to author",
                          f'prepare triage with {{"message": "{id}"}}')
        return box, message

    def _candidate_reply(self, message: Message, pass_no: int) -> tuple[int, AuthorReply] | None:
        final = self._final_reply("author", pass_no, "author", message.seq)
        if final is None:
            return None
        return final[0], AuthorReply.model_validate_json(unwrap_fence(final[1]))

    async def _author(self, id: str) -> CallSpec | dict[str, Any]:
        box, message = self._author_message(id)
        pass_no = self._author_pass(message)
        findings = None
        if pass_no is not None and (made := self._candidate_reply(message, pass_no)) is not None:
            reviewed = self._final_reply(f"author.{message.seq}", pass_no, "requisition_review", made[0])
            if reviewed is None:
                raise Refusal(f"the candidate from author call {made[0]} is not reviewed",
                              f'prepare requisition_review with {{"message": "{id}"}}')
            findings = tuple(RequisitionReply.model_validate_json(unwrap_fence(reviewed[1])).findings)
        render_author = await author_render(self.c, box, message)
        if isinstance(render_author, str):
            outcome = await failure_decision(self.c, self.driver, box, message, render_author)
            return {"outcome": "gate_failed", "line": f"{id}: decision -> {outcome.decision}"}
        if pass_no is None:
            pass_no = self._triage_pass()
            self.c.journal.append(EventType.SIGNAL, {"signal": "author_invoked", "message": id, "pass": pass_no})
        spec = author_spec()
        return CallSpec(stage=author_stage(self.c, message, render_author), consumed=None, ticket=None,
                        attempt=message.seq, workspace=self.c.repo, tier=spec.meta.tier, effort=spec.meta.effort,
                        stuck_budget=AUTHOR_STUCK_S, run_seq=pass_no, findings=findings,
                        context={"kind": "author", "message": id})

    async def _candidate(self, id: str) -> CallSpec | dict[str, Any]:
        _, message = self._author_message(id)
        pass_no = self._author_pass(message)
        made = None if pass_no is None else self._candidate_reply(message, pass_no)
        if pass_no is None or made is None:
            raise Refusal(f"box message {id} has no finished author candidate", f'prepare author with {{"message": "{id}"}}')
        call_seq, reply = made
        text, _ = stamped(self.c, message, reply)
        req = requisition(self.c.repo, reply.stem, text, SPECS_DIR)
        return CallSpec(stage=req.stage(), consumed=None, ticket=None, attempt=call_seq, workspace=self.c.repo,
                        tier=req.spec.meta.tier, effort=req.spec.meta.effort, stuck_budget=REQUISITION_STUCK_S,
                        run_seq=pass_no, slot=f"author.{message.seq}", context={"kind": "candidate", "message": id})

    # --- split and retro calls -------------------------------------------------------------------

    def _split_tree(self, stem: str) -> Path:
        assert self.c.config.worktree_root is not None
        return Path(self.c.config.worktree_root) / f"{stem}.split"

    def _split_order(self, stem: str) -> dict[str, Any] | None:
        return next((e.body for e in reversed(self.c.journal.read()) if e.ticket == stem
                     and e.type == EventType.SIGNAL and e.body.get("signal") == REWORK_ORDER), None)

    async def _split_pending(self, stem: str) -> dict[str, Any] | None:
        """The latest split order while the stem is parked by it and not yet superseded."""
        events = self.c.journal.read()
        terminal = next((e.body for e in reversed(events) if e.ticket == stem
                         and e.type == EventType.STATE_TRANSITION), None)
        if terminal is None or terminal.get("dispatch") != "split" or stem in supersedes_maps(events):
            return None
        sha = await self.c.git.rev_parse(self.c.repo, f"HEAD:{ticket_path(stem)}")
        return self._split_order(stem) if parked(events, stem, sha) else None

    async def _proposal(self, stem: str, proposal: str) -> CallSpec | dict[str, Any]:
        self._ticket(stem)
        order = await self._split_pending(stem)
        text = next((t["ticket"] for t in order["tickets"] if t["stem"] == proposal), None) if order else None
        if order is None or text is None or not self._split_tree(stem).is_dir():
            raise Refusal(f"{stem} has no pending split proposing {proposal!r}",
                          "call split_propose, then review each proposal it returns")
        req = requisition(self._split_tree(stem), proposal, text, SPECS_DIR)
        if (snag := req.precheck()) is not None:
            return {"outcome": "gate_failed", "verdict": _json(snag)}
        return CallSpec(stage=req.stage(), consumed=None, ticket=None, attempt=1, workspace=self._split_tree(stem),
                        tier=req.spec.meta.tier, effort=req.spec.meta.effort, stuck_budget=REQUISITION_STUCK_S,
                        run_seq=order["order"], slot=f"split.{stem}.{proposal}",
                        context={"kind": "proposal", "stem": stem, "proposal": proposal})

    async def _retro(self) -> CallSpec | dict[str, Any]:
        begun = await Retro(self.c, self.driver).begin()
        if begun is None:
            raise Refusal("retro is not due", "run retro only when status shows retro_due")
        if isinstance(begun, str):
            return {"outcome": "ok", "line": begun}
        stage, consumed, context = begun
        return CallSpec(stage=stage, consumed=consumed, ticket=None, attempt=1, workspace=self.c.repo,
                        tier=context["tier"], effort=context["effort"], stuck_budget=RETRO_STUCK_S,
                        run_seq=context["sequence"], context=context)

    async def submit(self, call: str) -> dict[str, Any]:
        assert self.session is not None
        events = self.c.journal.read()
        record = issued(events, call)
        if record is None:
            raise Refusal(f"call {call} was never issued", "submit only a call id that prepare returned")
        job = self.jobs.latest("submit", call=call)
        if job is not None and job["state"] == "running":
            return {"state": "pending", "job": job["id"]}
        if job is not None and job["state"] == "done" and job["result"]["state"] != "pending":
            return job["result"]
        call_dir = Path(record["reply"]).parent
        if (not closed(events, call) and not (call_dir / "stop.txt").is_file()
                and self.c.clock() <= datetime.fromisoformat(record["deadline"])):
            return {"state": "pending"}
        job = self._start_job("submit", call=call, host=record["host"], session=record["session"])
        return job["result"] if job["state"] == "done" else {"state": "pending", "job": job["id"]}

    def _implement_reply(self, stem: str, attempt: int) -> ImplementReply:
        prefix = llm_key(stem, run_seq(self.c.journal.read(), stem), "implement", attempt, 0)[:-1]
        done = [e for e in self.c.journal.read() if e.type == EventType.EFFECT_COMPLETION and "call" in e.body
                and e.key is not None and e.key.startswith(prefix)]
        if not done or not done[-1].body["call"]["final"] or done[-1].body["call"]["outcome"] != "ok":
            raise Refusal(f"{stem} has no ok implement result in run {attempt}",
                          "prepare and submit implement until it is final and ok")
        reply = ImplementReply.model_validate_json(unwrap_fence(done[-1].body["result"]["text"]))
        if reply.outcome not in ("ok", "already_satisfied"):
            raise Refusal(f"implement replied {reply.outcome}", "end the session with write_handoff")
        return reply

    async def check(self, stem: str) -> dict[str, Any]:
        _, attempt = self._running(stem)
        self._implement_reply(stem, attempt)
        return self._start_job("check", stem=stem)

    async def merge(self, stem: str) -> dict[str, Any]:
        self._running(stem)
        return self._start_job("merge", stem=stem)

    async def job(self, id: str) -> dict[str, Any]:
        return self.jobs.read(id)

    def _start_job(self, kind: str, **args: Any) -> dict[str, Any]:
        return self.jobs.start(kind, args, self.launch)

    # --- job worker ------------------------------------------------------------------------------

    async def run_job(self, id: str) -> None:
        """The detached worker: run one job under the writer lock and record its result."""
        record = self.jobs.read(id)
        self.lock.acquire(wait=True)
        try:
            try:
                result = await getattr(self, f"_job_{record['kind']}")(**record["args"])
            except Refusal as e:
                result = {"outcome": "infra_error", "findings": [{"code": "refused", "message": str(e),
                                                                  "paved_road": "see the message"}]}
        finally:
            self.lock.release()
        self.jobs.finish(id, result)

    async def _job_check(self, stem: str) -> dict[str, Any]:
        ticket, attempt = self._running(stem)
        _, spec = implement_stage(self.ctx, ticket, self.ctx.worktree(stem))
        slip = await packing_slip(self.ctx, spec, stem, self._implement_reply(stem, attempt))
        return self._failed_result(stem, "check", _result(await check(self.ctx, ticket, slip, attempt=attempt)))

    async def _job_merge(self, stem: str) -> dict[str, Any]:
        ticket, attempt = self._running(stem)
        return self._failed_result(stem, "merge", _result(await merge(self.ctx, ticket, attempt=attempt)))

    async def _job_submit(self, call: str, host: str, session: str) -> dict[str, Any]:
        record = issued(self.c.journal.read(), call)
        assert record is not None
        stage, finish = await self._surface(record)
        submitted = await self._calls(host, session).submit(call, stage)
        out: dict[str, Any] = {"state": submitted.state, "outcome": submitted.outcome, "final": submitted.final,
                               "findings": _json(submitted.findings)}
        if submitted.result is not None:
            out |= await finish(submitted.result) | {"final": True}
        if out["final"] and not record.get("context"):
            self._failed_result(record["ticket"], record["surface"], out)
        return out

    async def _surface(self, record: dict[str, Any]) -> tuple[LlmStage, Callable[[StageResult], Awaitable[dict[str, Any]]]]:
        """Rebuild the call's stage from call.json, and what follows a final result."""
        context = record.get("context") or {}
        kind = context.get("kind")
        if kind == "triage":
            return triage_stage(), lambda result: self._finish_triage(context["message"], result)
        if kind == "author":
            _, message = self._author_message(context["message"])
            return author_stage(self.c, message), lambda result: self._finish_author(message, result)
        if kind == "candidate":
            return _REQUISITION, lambda result: self._finish_candidate(context["message"], result)
        if kind == "proposal":
            return _REQUISITION, self._finish_proposal
        if kind == "retro":
            return retro_stage(), lambda result: self._finish_retro(context, result)
        stem, surface, attempt = record["ticket"], record["surface"], record["attempt"]
        ticket = self._ticket(stem)
        tier, effort = capability(ticket, self.c.journal.read())
        ticket = replace(ticket, frontmatter=ticket.frontmatter.model_copy(
            update={"agent_tier": tier, "agent_effort": effort}))
        text = (self.c.repo / ticket_path(stem)).read_text()
        if surface == "implement":
            stage, spec = implement_stage(self.ctx, ticket, self.ctx.worktree(stem))

            async def implemented(result: StageResult) -> dict[str, Any]:
                return _result(await finish_implement(self.ctx, ticket, spec, result, attempt=attempt))
            return stage, implemented
        if surface == "review":
            reviewed = await self.c.git.rev_parse(self.c.repo, stem)
            stage, spec = review_stage(self.ctx, text, await self.c.git.diff(self.c.repo, "main", stem))

            async def judged(result: StageResult) -> dict[str, Any]:
                return _result(await finish_review(self.ctx, ticket, spec, result, reviewed, attempt=attempt))
            return stage, judged
        req = requisition(self.c.repo, stem, text, SPECS_DIR)

        async def verdict(result: StageResult) -> dict[str, Any]:
            if result.outcome != "ok":
                return _result(result)
            made = req.verdict(result.artifact, result.provider, result.model)
            return _result(StageResult(outcome="ok" if made.verdict == "approve" else "gate_failed", artifact=made,
                                       findings=list(made.findings), provider=result.provider, model=result.model))
        return req.stage(), verdict

    async def _finish_triage(self, id: str, result: StageResult) -> dict[str, Any]:
        box = self._box()
        message = self._pending(box, id)
        if result.outcome != "ok":
            return _result(result) | {"line": f"{id}: {result.outcome}; remains pending"}
        line = await settle(self.c, self.driver, box, message, result.artifact, triage_spec())
        return _result(result) | ({"next": "author"} if line is None else {"line": line})

    async def _finish_author(self, message: Message, result: StageResult) -> dict[str, Any]:
        if result.outcome == "ok":
            return _result(result) | {"next": "requisition_review"}
        outcome = await failure_decision(self.c, self.driver, self._box(), message,
                                         findings_text(result.findings) or result.outcome)
        return _result(result) | {"line": f"{message.id}: decision -> {outcome.decision}"}

    async def _finish_candidate(self, id: str, result: StageResult) -> dict[str, Any]:
        box, message = self._author_message(id)
        pass_no = self._author_pass(message)
        assert pass_no is not None
        made = self._candidate_reply(message, pass_no)
        assert made is not None
        call_seq, reply = made
        text, state = stamped(self.c, message, reply)
        req = requisition(self.c.repo, reply.stem, text, SPECS_DIR)
        if result.outcome == "ok":
            verdict = req.verdict(result.artifact, result.provider, result.model)
        else:
            verdict = req.invalid(findings_text(result.findings) or f"requisition review ended {result.outcome}",
                                  result.provider, result.model)
        if verdict.verdict == "approve":
            outcome = await publish(self.c, self.driver, box, message, reply, text, state, verdict)
            if outcome.stem is None:
                return {"outcome": "gate_failed", "verdict": _json(verdict),
                        "line": f"{id}: decision -> {outcome.decision}"}
            line = f"{id}: ticket -> {outcome.stem}"
            if message.message_class == "bug_report":
                line += await self._attribute(box, id)
            return {"outcome": "ok", "verdict": _json(verdict), "findings": [], "line": line}
        discard(self.c, reply.stem)
        if verdict.verdict == "rma" or call_seq >= self.c.config.caps.retry + 1:
            outcome = await failure_decision(self.c, self.driver, box, message, findings_text(verdict.findings))
            return {"outcome": "gate_failed", "verdict": _json(verdict), "findings": _json(verdict.findings),
                    "line": f"{id}: decision -> {outcome.decision}"}
        return {"outcome": "gate_failed", "verdict": _json(verdict), "findings": _json(verdict.findings),
                "next": "author"}

    async def _attribute(self, box: Box, id: str) -> str:
        try:
            await attribute_escape(self.c, self.driver, box.get(id))
        except (GitError, OSError, ExecutableNotFound, ValueError, KeyError, yaml.YAMLError) as exc:
            self.driver.log.event("escape_attribution_incomplete", box_id=id, error=str(exc),
                                  paved_road="repair repository/evidence access and retry triage")
            return "; attribution incomplete"
        return ""

    async def _finish_proposal(self, result: StageResult) -> dict[str, Any]:
        if result.outcome != "ok":
            return _result(result)
        reply = result.artifact
        assert isinstance(reply, RequisitionReply)
        return {"outcome": "ok" if reply.verdict == "approve" else "gate_failed", "verdict": _json(reply),
                "findings": _json(reply.findings)}

    async def _finish_retro(self, context: dict[str, Any], result: StageResult) -> dict[str, Any]:
        await Retro(self.c, self.driver).finish(context, result.outcome, result.artifact)
        return _result(result)

    # --- decide ----------------------------------------------------------------------------------

    def _failed(self, stem: str, stage: str, outcome: str, findings: list[Any]) -> None:
        """Record a stage that ended final and not ok: the fact every Decide tool reads."""
        if outcome not in NOT_FAILED:
            self.c.journal.append(EventType.SIGNAL, {"signal": STAGE_FAILED, "stage": stage, "outcome": outcome,
                                                     "reason": _reason(outcome, findings),
                                                     "findings": _json(findings)}, ticket=stem)

    def _failed_result(self, stem: str, stage: str, result: dict[str, Any]) -> dict[str, Any]:
        self._failed(stem, stage, result["outcome"], result["findings"])
        return result

    def _failure(self, stem: str) -> dict[str, Any]:
        """The current run's failed stage; refuse while the run has not failed."""
        failure = None
        for e in self.c.journal.read():
            if e.ticket != stem:
                continue
            if e.type == EventType.STATE_TRANSITION and e.body.get("to") == "running":
                failure = None
            elif e.type == EventType.SIGNAL and e.body.get("signal") == STAGE_FAILED:
                failure = e.body
        if failure is None:
            raise Refusal(f"{stem}'s run has no failed stage", "continue the ticket flow; decide only after a failure")
        return failure

    async def _lessons(self, stem: str, decision: str, lessons: str) -> None:
        rel = f"{TICKETS_DIR}/{stem}/{DECISIONS}"
        path = self.c.repo / rel
        before = path.read_text() if path.is_file() else ""
        attempt = run_seq(self.c.journal.read(), stem)
        self.c.fs.write(path, f"{before}## Run {attempt}: {decision}\n\n{lessons.strip()}\n\n".encode())
        await self.c.git.add(self.c.repo, [rel])
        await self.c.git.commit(self.c.repo, f"tempo({stem}): {decision}", only=[rel])

    async def _decide(self, stem: str, lessons: str, dispatch: Dispatch, *,
                      rung: dict[str, str] | None = None) -> dict[str, Any]:
        """What chupa's `runner.drive` did after a failure: harvest, caps, lessons, the terminal, the worktree."""
        _need(lessons)
        _, attempt = self._running(stem)
        failure = self._failure(stem)
        events = self.c.journal.read()
        outcome, stage, reason = failure["outcome"], failure["stage"], failure["reason"]
        if dispatch in ("retry", "escalate") and (cap := spent(self.c.config.caps, events, stem)) is not None:
            raise Refusal(f"{stem}'s {cap} cap is spent", "park or reject it")
        if dispatch == "retry" and identical(events, stem, reason):
            raise Refusal(f"{stem} has failed for {reason!r} {IDENTICAL_K} times running",
                          "escalate, park, or reject it")
        sha = await self.c.git.rev_parse(self.c.repo, f"HEAD:{ticket_path(stem)}")
        if outcome in ("infra_error", "timeout"):
            consume(self.c.journal, stem, "infra", sha)
        if outcome == PREMISE and "render_over_bound" not in reason.split(","):
            consume(self.c.journal, stem, "premise_bounce", sha)
        consume(self.c.journal, stem, "decisions", sha)
        try:
            await harvest(self.ctx, stem, attempt=attempt, stage=stage, terminal=outcome,
                          findings=[Finding.model_validate(f) for f in failure["findings"]],
                          wall_seconds=run_wall_seconds(self.ctx, stem))
        except Exception as exc:  # noqa: BLE001 -- a failed harvest never blocks the decision
            self.c.journal.append(EventType.SIGNAL, {"signal": "harvest_failed",
                                                     "error": self.driver.redactor.scrub(f"{type(exc).__name__}: {exc}")},
                                  ticket=stem)
        terminal: dict[str, Any] = {"to": outcome, "stage": stage, "reason": reason, "dispatch": dispatch}
        if rung is not None:
            terminal["rung"] = rung
        self.c.journal.append(EventType.STATE_TRANSITION, terminal, ticket=stem)
        await self._lessons(stem, dispatch, lessons)
        if self.ctx.worktree(stem).exists():
            await self.c.git.worktree_remove(self.c.repo, self.ctx.worktree(stem))
        return {"stem": stem, **terminal}

    async def retry(self, stem: str, lessons: str) -> dict[str, Any]:
        return await self._decide(stem, lessons, "retry")

    async def escalate(self, stem: str, lessons: str) -> dict[str, Any]:
        assert self.session is not None
        ticket, _ = self._running(stem)
        rung = next_rung(self.c.config, self.session["host"], ticket.frontmatter.agent_tier,
                         ticket.frontmatter.agent_effort)
        if rung is None:
            raise Refusal(f"{stem} is already on {self.session['host']}'s top implement rung", "park or reject it")
        return await self._decide(stem, lessons, "escalate", rung=rung)

    async def park(self, stem: str, lessons: str) -> dict[str, Any]:
        return await self._decide(stem, lessons, "park")

    async def reject(self, stem: str, lessons: str) -> dict[str, Any]:
        last = last_states(self.c.journal.read()).get(stem)
        if last == "running":
            await self._decide(stem, lessons, "reject_queue")
        else:
            _need(lessons)
            self._ticket(stem)
            if last in SETTLED:
                raise Refusal(f"{stem} is {last}", "leave settled work in place")
            await self._lessons(stem, "reject", lessons)
        await reject_ticket(stem, self.c, actor="orchestrator")
        return {"stem": stem, "to": "rejected"}

    async def reoffer(self, stem: str, lessons: str) -> dict[str, Any]:
        _need(lessons)
        self._ticket(stem)
        events = self.c.journal.read()
        sha = await self.c.git.rev_parse(self.c.repo, f"HEAD:{ticket_path(stem)}")
        if not parked(events, stem, sha):
            raise Refusal(f"{stem} is not parked", "start it, or pick a stem from list_eligible")
        if (cap := spent(self.c.config.caps, events, stem)) is not None:
            raise Refusal(f"{stem}'s {cap} cap is spent", "reject it")
        await self._lessons(stem, "reoffer", lessons)
        self.c.journal.append(EventType.SIGNAL, {"signal": REOFFERED, "ticket_sha": sha}, ticket=stem)
        return {"stem": stem, "reoffered": True}

    async def release_hold(self, hold_id: str, lessons: str) -> dict[str, Any]:
        assert self.session is not None
        _need(lessons)
        holds = StormLedger(journal=self.c.journal, clock=self.c.clock).holds()
        if hold_id not in holds:
            raise Refusal(f"hold {hold_id!r} is not live", f"release one of {sorted(holds) or 'no holds'}")
        await self._lessons(holds[hold_id], "release_hold", lessons)
        self.c.journal.append(EventType.SIGNAL, {
            "kind": CONTROL_DECISION, "request_id": self.new_id(), "lifecycle_id": self.session["token"],
            "verb": "resume", "hold_id": hold_id, "decision": "accepted", "reason": "orchestrator release_hold"},
            ticket=None)
        return {"hold_id": hold_id, "stem": holds[hold_id], "released": True}

    async def split_propose(self, stem: str, tickets: list[dict[str, Any]], lessons: str) -> dict[str, Any]:
        """Check the successors in a tree beside each other, journal the order, then end the run as `split`."""
        _need(lessons)
        try:
            proposals = [TicketProposal.model_validate(t) for t in tickets]
        except ValidationError as e:
            raise Refusal(f"tickets is malformed: {e.error_count()} error(s)",
                          'pass tickets as [{"stem": "<new stem>", "ticket": "<whole ticket.md>"}, ...]') from None
        running = last_states(self.c.journal.read()).get(stem) == "running"
        if running:
            self._running(stem)
            self._failure(stem)
        elif await self._split_pending(stem) is None:
            raise Refusal(f"{stem} has no failed run or pending split", "split a ticket only after a failed stage")
        original = self._ticket(stem)
        if errors := check_stems(self.ctx, original, proposals):
            raise Refusal(f"split proposal refused: {findings_text(errors)}", errors[0].paved_road)
        tree = self._split_tree(stem)
        if tree.exists():
            await self.c.git.worktree_remove(self.c.repo, tree)
        await self.c.git.worktree_add_detached(self.c.repo, tree, "HEAD")
        for proposal in proposals:
            self.c.fs.write(tree / ticket_path(proposal.stem), proposal.ticket.encode())
        if errors := check_tickets(original, proposals, tree):
            await self.c.git.worktree_remove(self.c.repo, tree)
            raise Refusal(f"split proposal refused: {findings_text(errors)}", errors[0].paved_road)
        order = 1 + sum(1 for e in self.c.journal.read() if e.ticket == stem
                        and e.type == EventType.SIGNAL and e.body.get("signal") == REWORK_ORDER)
        self.c.journal.append(EventType.SIGNAL, {
            "signal": REWORK_ORDER, "attempt": run_seq(self.c.journal.read(), stem), "action": "split",
            "order": order, "tickets": [p.model_dump() for p in proposals]}, ticket=stem)
        if running:
            out = await self._decide(stem, lessons, "split")
        else:
            await self._lessons(stem, "split", lessons)
            out = {"stem": stem}
        return out | {"order": order, "proposals": [p.stem for p in proposals], "next": "requisition_review"}

    async def split_commit(self, stem: str) -> dict[str, Any]:
        """Publish every approved proposal of the pending split and record the supersession."""
        original = self._ticket(stem)
        order = await self._split_pending(stem)
        if order is None:
            raise Refusal(f"{stem} has no pending split", "call split_propose first")
        proposals = [TicketProposal.model_validate(t) for t in order["tickets"]]
        for proposal in proposals:
            reviewed = self._final_reply(f"split.{stem}.{proposal.stem}", order["order"], "requisition_review", 1)
            if (reviewed is None
                    or RequisitionReply.model_validate_json(unwrap_fence(reviewed[1])).verdict != "approve"):
                raise Refusal(f"proposal {proposal.stem} has no approve verdict",
                              f'prepare requisition_review with {{"stem": "{stem}", "proposal": "{proposal.stem}"}},'
                              " or call split_propose again")
        key = f"ticket-plane/{stem}/split/{order['order']}"
        if (not any(e.type == EventType.EFFECT_COMPLETION and e.key == key for e in self.c.journal.read())
                and (errors := check_stems(self.ctx, original, proposals))):
            raise Refusal(f"split refused: {findings_text(errors)}", "call split_propose again with fresh stems")
        paths = [ticket_path(p.stem) for p in proposals]

        async def commit() -> dict[str, str]:
            for proposal in proposals:
                self.c.fs.write(self.c.repo / ticket_path(proposal.stem), proposal.ticket.encode())
            await self.c.git.add(self.c.repo, paths)
            await self.c.git.commit(self.c.repo, f"tempo({stem}): split into {', '.join(p.stem for p in proposals)}",
                                    only=paths)
            return {"commit": await self.c.git.rev_parse(self.c.repo, "HEAD")}
        await self.driver.effects.run(commit, key=key, ticket=stem)
        if errors := await record_supersedes(self.ctx, stem, proposals):
            raise Refusal(f"supersession refused: {findings_text(errors)}", errors[0].paved_road)
        if self._split_tree(stem).exists():
            await self.c.git.worktree_remove(self.c.repo, self._split_tree(stem))
        return {"stem": stem, "successors": [p.stem for p in proposals]}

    async def confirm(self, stem: str, lessons: str) -> dict[str, Any]:
        _need(lessons)
        ticket = self._ticket(stem)
        if ticket.frontmatter.state != "draft":
            raise Refusal(f"{stem} is state: {ticket.frontmatter.state}, not draft", "confirm only a draft ticket")
        try:
            await confirm_draft(stem, self.new_id(), self.ctx)
        except ValueError as e:
            raise Refusal(str(e), "commit or discard the uncommitted edit to the draft") from None
        await self._lessons(stem, "confirm", lessons)
        return {"stem": stem, "state": "confirmed"}

    # --- record ----------------------------------------------------------------------------------

    async def file_box(self, summary: str, message_class: str = "suggestion") -> dict[str, Any]:
        if message_class not in MESSAGE_CLASSES:
            raise Refusal(f"message_class {message_class!r} is unknown", f"use one of {', '.join(MESSAGE_CLASSES)}")
        box = Box(self.state / BOX_DIR, self.c.fs, rereport=RereportJournal(self.c.journal))
        try:
            id, created = box.enqueue(message_class=message_class, origin="orchestrator", summary=summary)
        except BoxError as e:
            raise Refusal(str(e), "fix the message and file it again") from None
        return {"id": id, "created": created}

    async def notify(self, message: str) -> dict[str, Any]:
        await self._notify("orchestrator", message)
        return {"sent": self.c.config.notify is not None}
