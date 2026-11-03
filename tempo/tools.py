"""The orchestrator's tools (AGENT_PLAN.md section 4.1): `tempo tool <name> '<json>'` prints one JSON result.

P4 builds the Read and Run tools, `begin_session`, and `write_handoff`; P5 builds the Decide tools and
the rest of Record. Every tool except `begin_session` and `status` needs the token `begin_session`
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

from pydantic import BaseModel

from tempo import render
from tempo.artifacts import StageResult
from tempo.backlog import PREMISE, admit, eligible, premise_parked, start_run
from tempo.box import BOX_DIR, Box, RereportJournal
from tempo.call import Calls, CallSpec, closed, issued, open_calls
from tempo.caps import capability, spent
from tempo.checkout import SPECS_DIR, Checkout, Refusal
from tempo.driver import Driver, LlmStage, unwrap_fence
from tempo.effects import Effects
from tempo.hooks import CLAIM, HOOKS_DIR
from tempo.hosts import LAYOUTS
from tempo.jobs import Jobs
from tempo.journal import TERMINAL_STATES, EventType, render_ts, run_seq
from tempo.llm import LLMRequest, LLMResult
from tempo.llmeffect import llm_key
from tempo.lockfile import Lockfile, LockHeld, Sessions
from tempo.merge import merge
from tempo.notify import send
from tempo.requisition import REQUISITION_STUCK_S, requisition
from tempo.seams import Notifications
from tempo.specs import RenderOverBound
from tempo.stages import (ImplementReply, StageContext, check, finish_implement, finish_review, implement_stage,
                          packing_slip, prepare_worktree, prior_attempts, review_stage)
from tempo.status import last_states, project
from tempo.status import render as render_status
from tempo.thresh import fold_breakers
from tempo.tickets import (TICKET_FILE, TICKETS_DIR, Ticket, TicketInvalid, _porcelain, parse_ticket, stem_findings,
                           ticket_path, validate_ticket)
from tempo.timers import Timers

# Contract constant (TEMPO_PLAN.md section 22): below Claude's 120 s shell-command timeout, with margin.
TOOL_CALL_S = 90.0
# The surfaces of the P4 orchestrator flow; triage, author, and retro get their tools in P5.
FLOW_SURFACES = ("requisition_review", "implement", "review")
HANDOFF = f"{TICKETS_DIR}/orchestrator/handoff.md"


class _NoModel:
    """Tempo runs no model itself (R15): every model call is a host subagent through prepare and submit."""

    kind = "api"

    async def call(self, req: LLMRequest) -> LLMResult:
        raise RuntimeError("tempo makes no model call; spawn the subagent prepare names")

    def abort_current(self) -> None:
        return None


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
    "read_box": _Tool("read_box"),
    "start": _Tool("start", writes=True),
    "prepare": _Tool("prepare", writes=True),
    "submit": _Tool("submit", late=True),
    "check": _Tool("check", writes=True),
    "merge": _Tool("merge", writes=True),
    "job": _Tool("job", late=True),
    "write_handoff": _Tool("write_handoff", writes=True, late=True),
}


def _json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_json(v) for v in value]
    return value


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
        driver = Driver.from_config(config, llm=_NoModel(), env=checkout.env, clock=clock, sleep=checkout.sleep, fs=fs)
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
        """Chupa's notification transport, called directly (the `notify` tool is P5)."""
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
                "cooldowns": {h: end for h in self.c.config.orchestrator.models if (end := self._cooldown(h))}}

    async def list_eligible(self) -> dict[str, Any]:
        assert self.session is not None
        if (until := self._cooldown(self.session["host"])) is not None:
            return {"eligible": [], "cooldown_until": until}
        repo = self.c.repo
        dirty = {p for _, p in _porcelain(await self.c.git.status_porcelain(repo))}
        tickets = {}
        for path in sorted((repo / TICKETS_DIR).glob(f"*/{TICKET_FILE}")):
            stem = path.parent.name
            if ticket_path(stem) in dirty:
                continue
            try:
                tickets[stem] = parse_ticket(stem, path.read_text(), repo)
            except TicketInvalid:
                continue
        events = self.c.journal.read()
        last = last_states(events)
        out = []
        for ticket in eligible(tickets, events):
            stem = ticket.stem
            sha = await self.c.git.rev_parse(repo, f"HEAD:{ticket_path(stem)}")
            if last.get(stem) == PREMISE and premise_parked(events, stem, sha):
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
        box = Box(self.state / BOX_DIR, self.c.fs, rereport=RereportJournal(self.c.journal))
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

    async def prepare(self, stem: str, surface: str) -> dict[str, Any]:
        assert self.session is not None
        if surface not in FLOW_SURFACES:
            raise Refusal(f"surface {surface!r} has no tool in P4", f"use one of {', '.join(FLOW_SURFACES)}")
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
                return {"state": "completed", "final": True, "outcome": "gate_failed", "verdict": _json(snag)}
            stage, consumed, stuck = req.stage(), None, REQUISITION_STUCK_S
        spec = CallSpec(stage=stage, consumed=consumed, ticket=stem, attempt=attempt, workspace=worktree,
                        tier=ticket.frontmatter.agent_tier, effort=ticket.frontmatter.agent_effort,
                        stuck_budget=stuck)
        try:
            prepared = await self._calls(self.session["host"], self.session["token"]).prepare(spec)
        except RenderOverBound as e:
            return {"state": "completed", "final": True, "outcome": "premise_failed", "findings": [_json(e.finding)]}
        out = {"call": prepared.call, "state": prepared.state}
        if prepared.state == "pending":
            record = issued(self.c.journal.read(), prepared.call)
            assert record is not None
            out |= {"agent": prepared.agent, "spawn_prompt": prepared.spawn_prompt, "deadline": record["deadline"]}
        return out

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
        return _result(await check(self.ctx, ticket, slip, attempt=attempt))

    async def _job_merge(self, stem: str) -> dict[str, Any]:
        ticket, attempt = self._running(stem)
        return _result(await merge(self.ctx, ticket, attempt=attempt))

    async def _job_submit(self, call: str, host: str, session: str) -> dict[str, Any]:
        record = issued(self.c.journal.read(), call)
        assert record is not None
        stage, finish = await self._surface(record)
        submitted = await self._calls(host, session).submit(call, stage)
        out: dict[str, Any] = {"state": submitted.state, "outcome": submitted.outcome, "final": submitted.final,
                               "findings": _json(submitted.findings)}
        if submitted.result is not None:
            out |= _result(await finish(submitted.result)) | {"final": True}
        return out

    async def _surface(self, record: dict[str, Any]) -> tuple[LlmStage, Callable[[StageResult], Awaitable[StageResult]]]:
        """Rebuild the call's stage from call.json, and the artifact writer that follows a final result."""
        stem, surface, attempt = record["ticket"], record["surface"], record["attempt"]
        ticket = self._ticket(stem)
        tier, effort = capability(ticket, self.c.journal.read())
        ticket = replace(ticket, frontmatter=ticket.frontmatter.model_copy(
            update={"agent_tier": tier, "agent_effort": effort}))
        text = (self.c.repo / ticket_path(stem)).read_text()
        if surface == "implement":
            stage, spec = implement_stage(self.ctx, ticket, self.ctx.worktree(stem))
            return stage, lambda result: finish_implement(self.ctx, ticket, spec, result, attempt=attempt)
        if surface == "review":
            reviewed = await self.c.git.rev_parse(self.c.repo, stem)
            stage, spec = review_stage(self.ctx, text, await self.c.git.diff(self.c.repo, "main", stem))
            return stage, lambda result: finish_review(self.ctx, ticket, spec, result, reviewed, attempt=attempt)
        req = requisition(self.c.repo, stem, text, SPECS_DIR)

        async def verdict(result: StageResult) -> StageResult:
            if result.outcome != "ok":
                return result
            made = req.verdict(result.artifact, result.provider, result.model)
            return StageResult(outcome="ok" if made.verdict == "approve" else "gate_failed", artifact=made,
                               findings=list(made.findings), provider=result.provider, model=result.model)
        return req.stage(), verdict

