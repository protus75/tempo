"""The `run <stem>` scaffold verb (TEMPO_PLAN.md sections 11.2, 18): one ticket through intake, the
single-writer lockfile, and the stage-dispatch seam, all in the one process holding the lock.

Exit codes (section 18): 0 merged, 1 a non-ok ticket terminal, 2 an engine-plane refusal.
"""

import asyncio
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, assert_never, cast

if TYPE_CHECKING:
    from tempo.daemon import PauseConsumer

import yaml

from tempo.artifacts import Finding, Harvest, StageResult
from tempo.box import BOX_DIR
from tempo.storm import arrival_id
from tempo.caps import capability, consume, next_rung, spent, spent_reason
from tempo.config import Config, ConfigSnapshot, snapshot_config
from tempo.driver import Driver
from tempo.effects import Effects
from tempo.git import Git
from tempo.journal import TERMINAL_STATES, Dispatch as JournalDispatch, Event, EventType, Journal, dispatch_of
from tempo.llm import LLM
from tempo.lockfile import Lockfile
from tempo.merge import compose_pipeline, merge
from tempo.providers import ProviderLLM
from tempo.reconcile import reconcile
from tempo.seams import Clock, FileSystem, GroupExec, Sleep
from tempo.stages import (DiagnosisMaterial, StageContext, diagnose, lift_outbox,
                          run_stages, write_diagnosis)
from tempo.status import last_states, reject_queue
from tempo.tickets import (INTAKE_SIGNAL, Ticket, TicketInvalid, intake,
                           parse_ticket, split_frontmatter, stamp, stem_findings, ticket_path, validate_ticket)

EXIT_MERGED = 0
EXIT_TICKET = 1
EXIT_REFUSED = 2

# Engine plane: the prompt specs ship with the engine, not the host checkout it runs against.
SPECS_DIR = Path(__file__).resolve().parent.parent / "specs"
HARVEST_TAIL_CHARS = 4_000
IDENTICAL_K = 3

# The stage seam: drives the validated ticket through the stages and merge, journals the run's single
# terminal transition (section 11), and returns that terminal state.
Dispatch = Callable[[Ticket], Awaitable[str]]



# The provider backstop is the drain ceiling every ticket stuck budget fits under (section 9.7):
# the ticket's own stuck budget is the bound the driver enforces, never a shorter per-call cap.
def call_timeout(config: Config) -> float:
    return config.drain.max_ticket_minutes * 60.0

class Refusal(Exception):
    """An engine-plane refusal (exit 2): nothing was dispatched."""

    def __init__(self, message: str, paved_road: str) -> None:
        super().__init__(f"{message} -- {paved_road}")


@dataclass(frozen=True)
class Checkout:
    """The composed seams one invocation runs against; the stage pipeline is built from it."""

    repo: Path
    config: Config | ConfigSnapshot
    env: Mapping[str, str]
    exec_: GroupExec
    git: Git
    journal: Journal
    fs: FileSystem
    clock: Clock
    sleep: Sleep = asyncio.sleep
    control: "PauseConsumer | None" = None


Pipeline = Callable[[Checkout], Dispatch]


def pipeline(checkout: Checkout) -> Dispatch:
    """Preparation belongs to the outer lock-held run/drain lifetime."""
    class PreparedDispatch:
        callback = None
        snapshot = None
        stages = None
        async def prepare(self):
            snapshot = snapshot_config(checkout.control.load_config())
            if self.callback is not None and snapshot == self.snapshot:
                return
            local = replace(checkout, config=snapshot)
            self.callback = await prepare_pipeline(local)
            self.stages = self.callback.stages
            self.snapshot = snapshot
        async def __call__(self, ticket):
            if self.callback is None:
                await self.prepare()
            try:
                return await self.callback(ticket)
            finally:
                self.callback = None
        async def abort_current(self):
            if self.callback is not None:
                await self.callback.abort_current()
    return PreparedDispatch()


async def prepare_pipeline(checkout: Checkout) -> Dispatch:
    """Preflight this checkout's providers before constructing any stage consumers."""
    llm = ProviderLLM(checkout.config, exec_=checkout.exec_, fs=checkout.fs, env=checkout.env,
                      cwd=checkout.repo, timeout=call_timeout(checkout.config),
                      session=checkout.control.providers)
    problems = await llm.preflight()
    for notice in llm.preflight_notices:
        print(f"tempo: provider preflight notice: {notice}", file=sys.stderr)
    if problems:
        raise Refusal("provider preflight failed: " + "; ".join(problems),
                      "fix each named provider, then run the same command again (section 6 provider preflight)")
    return bind(checkout, llm)


def bind(checkout: Checkout, llm: LLM) -> Dispatch:
    driver = Driver.from_config(checkout.config, llm=llm, env=checkout.env, clock=checkout.clock,
                                sleep=checkout.sleep, fs=checkout.fs)
    driver.journal = checkout.journal
    driver.effects = Effects(checkout.journal)
    ctx = StageContext(repo=checkout.repo, config=checkout.config, env=checkout.env, exec_=checkout.exec_,
                       git=checkout.git, fs=checkout.fs, driver=driver, specs_dir=SPECS_DIR)

    def escalate(event: Event) -> None:
        # Queue signals are already journaled; the CLI run path has no notification transport.
        return None

    from tempo.daemon import TicketWriter

    queue = compose_pipeline(ctx, escalate=escalate, control=checkout.control)
    return TicketWriter(ctx, queue)


async def drive(ctx: StageContext, ticket: Ticket, *, admission=None, consume_handoff=None) -> str:
    """Implement -> Check -> Review -> Merge; harvest, dispatch, journal, then wipe a non-ok run."""
    if ctx.boundary.run is None:
        tier, effort = capability(ticket, ctx.driver.journal.read())
        ticket = replace(ticket, frontmatter=ticket.frontmatter.model_copy(
            update={"agent_tier": tier, "agent_effort": effort}))
    run = await run_stages(ctx, ticket)
    if ctx.boundary.next_stage is not None:
        return "running"
    stage, result = run.last
    if stage == "review" and result.outcome == "ok":
        from tempo.mergequeue import ConflictHandoff

        stage, result = "merge", await (admission(ticket, attempt=run.attempt) if admission is not None
                                       else merge(ctx, ticket, attempt=run.attempt))
        if isinstance(result, ConflictHandoff):
            assert consume_handoff is not None
            return await consume_handoff(ticket, result, attempt=run.attempt)
        if result.outcome == "ok":
            return "merged"
    if result.outcome != "already_satisfied":
        await harvest_failure(ctx, ticket, attempt=run.attempt, stage=stage, outcome=result.outcome,
                              findings=result.findings)
    drought = getattr(result, 'provider_drought', None)
    quota = any(f.code == 'quota_exhausted' for f in result.findings)
    if drought is not None or quota:
        return await failure_terminal(ctx, ticket, outcome=result.outcome, stage=stage,
            findings=result.findings, attempt=run.attempt, provider_drought=drought)
    if result.outcome in {"infra_error", "timeout"}:
        ticket_sha = await ctx.git.rev_parse(ctx.repo, f"HEAD:{ticket_path(ticket.stem)}")
        consume(ctx.driver.journal, ticket.stem, "infra", ticket_sha)
    over_bound = result.outcome == "premise_failed" and any(
        f.code == "render_over_bound" for f in result.findings)
    if result.outcome == "premise_failed" and not over_bound:
        ticket_sha = await ctx.git.rev_parse(ctx.repo, f"HEAD:{ticket_path(ticket.stem)}")
        consume(ctx.driver.journal, ticket.stem, "premise_bounce", ticket_sha)
    diagnosed = not over_bound
    if (diagnosed and isinstance(ctx.driver.llm, ProviderLLM)
            and not spent(ctx.config.caps, ctx.driver.journal.read(), ticket.stem)
            and ctx.worktree(ticket.stem).exists()):
        tier, _ = capability(ticket, ctx.driver.journal.read())
        refusal = ctx.driver.llm.session.drought(ctx.config, tier, 'diagnose')
        if refusal:
            return await failure_terminal(ctx, ticket, outcome='infra_error', stage=stage,
                findings=result.findings, attempt=run.attempt, provider_drought=refusal.record)
    if diagnosed:
        worktree = ctx.worktree(ticket.stem)
        mechanical = None if worktree.exists() else "workspace gone"
        if mechanical is None and (cap := spent(ctx.config.caps, ctx.driver.journal.read(), ticket.stem)):
            mechanical = spent_reason(cap)
        if mechanical is not None:
            if worktree.exists():
                diagnosis = await write_diagnosis(
                    ctx, ticket, attempt=run.attempt, terminal=result.outcome, stage=stage,
                    verdict="abandon-human", lessons=[], mechanical=mechanical,
                )
            else:
                # A lost workspace has no outbox; retain the mechanical verdict in the journal.
                diagnosis = None
            verdict = "abandon-human"
            lessons = []
        else:
            ticket_sha = await ctx.git.rev_parse(ctx.repo, f"HEAD:{ticket_path(ticket.stem)}")
            consume(ctx.driver.journal, ticket.stem, "diagnosis", ticket_sha)
            harvest_path = ctx.repo / "tickets" / ticket.stem / "attempts" / str(run.attempt) / "harvest.json"
            harvest_artifact = Harvest.model_validate_json(harvest_path.read_text()) if harvest_path.is_file() else None
            record_path = ctx.repo / "tickets" / ticket.stem / "run.md"
            material = DiagnosisMaterial(
                ticket=(ctx.repo / ticket_path(ticket.stem)).read_text(), terminal=result.outcome, stage=stage,
                harvest=harvest_artifact, run_record=record_path.read_text() if record_path.is_file() else None,
            )
            diagnosis = await diagnose(ctx, ticket, material, attempt=run.attempt)
            verdict, lessons = diagnosis.verdict, diagnosis.lessons
        ctx.driver.journal.append(EventType.SIGNAL,
                                  {"signal": "diagnosis", "attempt": run.attempt, "verdict": verdict,
                                   "lessons": lessons, "mechanical": mechanical if diagnosis is None else diagnosis.mechanical},
                                  ticket=ticket.stem)
    reworked = None
    requested = over_bound or (diagnosed and verdict == "split")
    if requested and (over_bound or not spent(ctx.config.caps, ctx.driver.journal.read(), ticket.stem)):
        from tempo.daemon import apply_rework

        reworked = await apply_rework(ctx, ticket, result.findings, attempt=run.attempt)
    return await failure_terminal(ctx, ticket, outcome=result.outcome, stage=stage,
                                  findings=result.findings, attempt=run.attempt, reworked=reworked,
                                  rework_requested=requested, mechanical=over_bound,
                                  verdict=verdict if diagnosed else None)


async def harvest_failure(ctx: StageContext, ticket: Ticket, *, attempt: int, stage: str,
                          outcome: str, findings: list[Finding]) -> None:
    """A failed harvest never interrupts failure dispatch or the producing terminal."""
    if ctx.worktree(ticket.stem).exists():
        try:
            await harvest(ctx, ticket.stem, attempt=attempt, stage=stage, terminal=outcome,
                          findings=findings, wall_seconds=run_wall_seconds(ctx, ticket.stem))
        except Exception as exc:
            ctx.driver.journal.append(EventType.SIGNAL,
                                      {"signal": "harvest_failed", "error": ctx.driver.redactor.scrub(f"{type(exc).__name__}: {exc}")},
                                      ticket=ticket.stem)


async def failure_terminal(ctx: StageContext, ticket: Ticket, *, outcome: str, stage: str,
                           findings: list[Finding], attempt: int, reworked: StageResult | None = None,
                           rework_requested: bool = False, mechanical: bool = False,
                           verdict: str | None = None, provider_drought: dict | None = None) -> str:
    """Preserve the producing terminal, then retire a published split before wiping and returning."""
    from tempo.rework import ReworkOrder
    from tempo.daemon import storm_producer

    reason = ",".join(sorted({f.code for f in findings})) or outcome
    terminal = {"to": outcome, "stage": stage, "reason": reason}
    if provider_drought is None and reworked is not None:
        provider_drought = getattr(reworked, 'provider_drought', None)
    if provider_drought is not None:
        terminal.update(to='infra_error', reason='provider_drought', provider_drought=provider_drought)
        rework_requested, verdict = False, None
        outcome = 'infra_error'
    history = ctx.driver.journal.read()
    tier, effort = capability(ticket, history)
    split = False
    dispatch: JournalDispatch | None = None
    if rework_requested:
        if reworked is not None and reworked.outcome == "ok":
            order = reworked.artifact
            assert isinstance(order, ReworkOrder)
            split = order.action == "split"
            verdict = "retry" if order.action == "update" else order.action
        else:
            verdict = "reject"
        if reworked is not None and (reworked.outcome != "ok" or (mechanical and verdict == "escalate")):
            extra = reworked.findings or [Finding(code="render_over_bound", message="Rework did not shrink the ticket",
                paved_road="shrink or split the committed ticket text and rerun drain")]
            if ctx.worktree(ticket.stem).exists():
                try:
                    await harvest(ctx, ticket.stem, attempt=attempt, stage=stage, terminal=outcome,
                                  findings=[*findings, *extra],
                                  wall_seconds=run_wall_seconds(ctx, ticket.stem), kind="harvest-rework")
                except Exception:
                    storm_producer(root=ctx.config.state_dir / BOX_DIR, fs=ctx.fs,
                               journal=ctx.driver.journal, clock=ctx.driver.clock).enqueue(
                        message_class="failure_report", origin=ticket.stem, stage=stage, outcome=outcome,
                        summary="; ".join(f"{f.message}; {f.paved_road}" for f in extra),
                        occurrence_id=arrival_id("rework-failure", ticket.stem, attempt, stage, outcome))
            else:
                storm_producer(root=ctx.config.state_dir / BOX_DIR, fs=ctx.fs,
                               journal=ctx.driver.journal, clock=ctx.driver.clock).enqueue(
                    message_class="failure_report", origin=ticket.stem, stage=stage, outcome=outcome,
                    summary="; ".join(f"{f.message}; {f.paved_road}" for f in extra),
                        occurrence_id=arrival_id("rework-failure", ticket.stem, attempt, stage, outcome))
    if not mechanical and not split and verdict is not None:
        previous = [e.body for e in history if e.type == EventType.STATE_TRANSITION
                    and e.ticket == ticket.stem and e.body.get("to") in TERMINAL_STATES]
        match prior_dispatch := dispatch_of(previous[-1]) if previous else None:
            case "retry":
                identical = verdict == "retry" and previous[-1].get("reason") == reason
            case "escalate" | "reject_queue" | None:
                identical = False
            case _:
                assert_never(prior_dispatch)
        identical = identical or (len(previous) >= IDENTICAL_K - 1 and all(
            e.get("reason") == reason for e in previous[-(IDENTICAL_K - 1):]))
        # Reviewed updates have an explicit retry decision; the identical-wall rule belongs to diagnosis.
        if spent(ctx.config.caps, history, ticket.stem) or verdict in {"reject", "abandon-human"}:
            dispatch = "reject_queue"
        elif verdict == "escalate" or (verdict is not None and identical and not rework_requested):
            rung = next_rung(ctx.config, tier, effort)
            if rung is None:
                dispatch = "reject_queue"
            else:
                dispatch = "escalate"
                terminal["rung"] = rung
        elif verdict == "retry":
            dispatch = "retry"
        elif verdict is not None:
            dispatch = "reject_queue"
    if dispatch is not None:
        terminal["dispatch"] = dispatch
    match terminal_dispatch := dispatch_of(terminal):
        case "reject_queue":
            routed = True
        case "retry" | "escalate" | None:
            routed = bool(spent(ctx.config.caps, history, ticket.stem))
        case _:
            assert_never(terminal_dispatch)
    if not mechanical and not split and routed and provider_drought is None:
        terminal["routed"] = "reject_queue"
    ctx.driver.journal.append(EventType.STATE_TRANSITION, terminal, ticket=ticket.stem)
    if split:
        checkout = Checkout(ctx.repo, ctx.config, ctx.env, ctx.exec_, ctx.git, ctx.driver.journal,
                            ctx.fs, ctx.driver.clock, ctx.driver.sleep)
        await reject_ticket(ticket.stem, checkout)
    if ctx.worktree(ticket.stem).exists():
        await ctx.git.worktree_remove(ctx.repo, ctx.worktree(ticket.stem))
    return "rejected" if split else outcome


def run_wall_seconds(ctx: StageContext, stem: str) -> float | None:
    """Wall-clock seconds since the stem's latest `running` transition; None when it never ran."""
    started = next((e.ts for e in reversed(ctx.driver.journal.read()) if e.type == EventType.STATE_TRANSITION
                    and e.ticket == stem and e.body.get("to") == "running"), None)
    return None if started is None else (ctx.driver.clock() - datetime.fromisoformat(started)).total_seconds()


async def harvest(
    ctx: StageContext, stem: str, *, attempt: int, stage: str | None, terminal: str,
    findings: list[Finding], wall_seconds: float | None, kind: str = "harvest",
) -> None:
    """Extract the allowlisted failed-run record and lift it through the one outbox lane."""
    spool = ctx.config.state_dir / "spools" / stem / str(attempt)
    errors = list(spool.rglob("error.txt")) if spool.is_dir() else []
    newest_error = max(errors, key=lambda p: (p.stat().st_mtime_ns, str(p))) if errors else None
    reason = None if findings else (newest_error.read_text(errors="replace") if newest_error else terminal)
    # Prompts are inputs, not stage logs: Review's prompt carries the unreviewed code diff.
    logs = sorted(p for p in spool.rglob("*") if p.is_file() and p.name != "prompt.md") if spool.is_dir() else []
    stage_log_tail = "".join(p.read_text(errors="replace") for p in logs)[-HARVEST_TAIL_CHARS:]
    providers = ctx.config.state_dir / "spools" / "providers" / stem
    events = list(providers.rglob("events.jsonl")) if providers.is_dir() else []
    newest_events = max(events, key=lambda p: (p.stat().st_mtime_ns, str(p))) if events else None
    events_tail = (newest_events.read_text(errors="replace") if newest_events else "")[-HARVEST_TAIL_CHARS:]
    record = f"tickets/{stem}/run.md"
    artifact = Harvest(
        attempt=attempt, stage=stage, terminal=terminal, findings=findings, reason=reason,
        diff_stat=await ctx.git.diff_stat(ctx.repo, "main", stem),
        stage_log_tail=stage_log_tail, events_tail=events_tail,
        wall_seconds=wall_seconds,
        run_record=record if (ctx.repo / record).is_file() else None,
    )
    path = ctx.worktree(stem) / "tickets" / stem / "attempts" / str(attempt) / "harvest.json"
    ctx.fs.write(path, (artifact.model_dump_json(indent=2) + "\n").encode())
    await lift_outbox(ctx, stem, kind, attempt=attempt, only=f"attempts/{attempt}/harvest.json")


async def harvest_orphan(checkout: Checkout, stem: str, attempt: int) -> None:
    """Use the run-terminal harvest with an effects context; an orphan makes no model call."""
    driver = Driver.from_config(checkout.config, llm=cast(LLM, None), env=checkout.env,
                                clock=checkout.clock, sleep=checkout.sleep, fs=checkout.fs)
    driver.journal = checkout.journal
    driver.effects = Effects(checkout.journal)
    ctx = StageContext(repo=checkout.repo, config=checkout.config, env=checkout.env, exec_=checkout.exec_,
                       git=checkout.git, fs=checkout.fs, driver=driver, specs_dir=SPECS_DIR)
    await harvest(ctx, stem, attempt=attempt, stage=None, terminal="abandoned", findings=[], wall_seconds=None)


async def run_ticket(stem: str, checkout: Checkout, dispatch: Dispatch) -> int:
    if findings := stem_findings(stem):
        raise Refusal(findings[0].message, findings[0].paved_road)
    lock = Lockfile(checkout.config.state_dir, instance_id=await checkout.git.describe(checkout.repo),
                    clock=checkout.clock)
    lock.acquire()
    try:
        assert checkout.config.worktree_root is not None  # resolved at config load
        await reconcile(checkout.journal, checkout.git, checkout.repo, checkout.config.worktree_root,
                        lambda orphan, attempt: harvest_orphan(checkout, orphan, attempt))
        if checkout.control is not None:
            checkout.control.providers.timers.reconstruct()
            checkout.control.providers.timers.fire_due()
        if hasattr(dispatch, 'prepare'):
            await dispatch.prepare()
        from tempo.daemon import storm_producer

        storm_producer(root=checkout.config.state_dir / BOX_DIR, fs=checkout.fs,
                       journal=checkout.journal, clock=checkout.clock).recover()
        ticket = await _admit(stem, checkout)
        if checkout.control is not None:
            tier, _ = capability(ticket, checkout.journal.read())
            drought = checkout.control.providers.drought(checkout.config, tier, 'implement')
            if drought:
                checkout.control.providers.park(stem, drought)
                return EXIT_TICKET
        checkout.journal.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket=stem)
        terminal = await dispatch(ticket)
        if terminal not in TERMINAL_STATES:
            raise ValueError(f"stage seam returned {terminal!r}, not a terminal state {sorted(TERMINAL_STATES)}")
        return EXIT_MERGED if terminal == "merged" else EXIT_TICKET
    finally:
        try:
            if checkout.control is not None:
                await checkout.control.providers.close()
        finally:
            lock.release()


async def verdict(stem: str, checkout: Checkout, *, kill: bool) -> int:
    """Resolve a draft or Reject item while holding the same writer lock as run and drain."""
    if findings := stem_findings(stem):
        raise Refusal(findings[0].message, findings[0].paved_road)
    lock = Lockfile(checkout.config.state_dir, instance_id=await checkout.git.describe(checkout.repo),
                    clock=checkout.clock)
    lock.acquire()
    try:
        intake_result = await intake(checkout.repo, checkout.git, checkout.journal, checkout.fs)
        if refused := intake_result.refused.get(stem):
            raise Refusal(f"{ticket_path(stem)} failed intake: " + "; ".join(f.message for f in refused),
                          "fix the ticket and retry the verdict")
        history = checkout.journal.read()
        rel = ticket_path(stem)
        path = checkout.repo / rel
        if not path.is_file() and not any(e.ticket == stem for e in history):
            raise Refusal(f"unknown stem {stem}", "name a journaled stem or an existing ticket.md")
        last = last_states(history).get(stem)
        if last == "running":
            raise Refusal(f"{stem} is running", "wait for its terminal, then retry")
        if kill:
            if last in {"merged", "already_satisfied"}:
                raise Refusal(f"{stem} is settled", "leave settled work in place")
            changed = await reject_ticket(stem, checkout)
            print(f"rejected {stem}" if changed else f"already rejected {stem}")
            return EXIT_MERGED
        if path.is_file() and _ticket_state(path) == "draft":
            checkout.fs.write(path, stamp(path.read_text(), "state", "confirmed").encode())
            await checkout.git.add(checkout.repo, [rel])
            await checkout.git.commit(checkout.repo, f"tempo({stem}): confirmed", only=[rel])
            sha = await checkout.git.rev_parse(checkout.repo, "HEAD")
            checkout.journal.append(EventType.SIGNAL, {"signal": "draft_confirmed", "commit": sha}, ticket=stem)
            print(f"confirmed draft {stem}")
            return EXIT_MERGED
        ticket_sha = await checkout.git.rev_parse(checkout.repo, f"HEAD:{rel}") if path.is_file() else None
        prior_keep = next((e for e in reversed(history) if e.type == EventType.SIGNAL and e.ticket == stem
                           and e.body.get("signal") == "reject_verdict" and e.body.get("verdict") == "keep"
                           and e.body.get("actor") == "operator"), None)
        if prior_keep is not None and prior_keep.body.get("ticket_sha") == ticket_sha:
            raise Refusal(f"{stem} was already kept at this ticket content",
                          "edit the ticket before re-enqueueing")
        if stem not in reject_queue(history):
            raise Refusal(f"{stem} is neither a draft nor in the Reject queue",
                          "run `status` to list the Reject queue")
        checkout.journal.append(EventType.SIGNAL,
                                {"signal": "reject_verdict", "verdict": "keep", "actor": "operator",
                                 "ticket_sha": ticket_sha}, ticket=stem)
        print(f"kept {stem}")
        return EXIT_MERGED
    finally:
        lock.release()


def committed_draft(checkout, stem: str) -> bool:
    path = checkout.repo / ticket_path(stem)
    return (path.is_file() and _ticket_state(path) == "draft" and any(
        e.ticket == stem and e.body.get("signal") == INTAKE_SIGNAL
        for e in checkout.journal.read()))


async def confirm_draft(stem: str, request_id: str, ctx) -> None:
    rel = ticket_path(stem)
    text = await ctx.git._run(ctx.repo, "show", f"main:{rel}")
    ticket = validate_ticket(stem, text, ctx.repo)
    if ticket.frontmatter.state not in ("draft", "confirmed") or (ctx.repo / rel).read_text() != text:
        raise ValueError("restore the committed draft before publishing a fresh confirmation")
    async def commit():
        if ticket.frontmatter.state == "confirmed":
            return {"commit": (await ctx.git._run(ctx.repo, "log", "-1", "--format=%H", "main", "--", rel)).strip()}
        ctx.fs.write(ctx.repo / rel, stamp(text, "state", "confirmed").encode())
        await ctx.git.add(ctx.repo, [rel])
        await ctx.git.commit(ctx.repo, f"tempo({stem}): confirmed", only=[rel])
        return {"commit": await ctx.git.rev_parse(ctx.repo, "HEAD")}
    result = await Effects(ctx.driver.journal).run(commit, key=f"ticket-plane/{stem}/confirmed/{request_id}", ticket=stem)
    if not any(e.ticket == stem and e.body.get("signal") == "draft_confirmed"
               and e.body.get("commit") == result["commit"] for e in ctx.driver.journal.read()):
        ctx.driver.journal.append(EventType.SIGNAL, {"signal": "draft_confirmed", **result}, ticket=stem)


async def reject_ticket(stem: str, checkout: Checkout) -> bool:
    """Reject mutation for a caller already holding the writer lock."""
    rel = ticket_path(stem)
    path = checkout.repo / rel
    last = last_states(checkout.journal.read()).get(stem)
    if path.is_file() and _ticket_state(path) != "rejected":
        checkout.fs.write(path, stamp(path.read_text(), "state", "rejected").encode())
        await checkout.git.add(checkout.repo, [rel])
        await checkout.git.commit(checkout.repo, f"tempo({stem}): rejected", only=[rel])
    if last != "rejected":
        checkout.journal.append(EventType.SIGNAL,
                                {"signal": "reject_verdict", "verdict": "kill", "actor": "operator"}, ticket=stem)
        checkout.journal.append(EventType.STATE_TRANSITION, {"to": "rejected"}, ticket=stem)
    await _dead_dependents(stem, checkout)
    return last != "rejected"


def _ticket_state(path: Path) -> str | None:
    split = split_frontmatter(path.read_text())
    return yaml.safe_load(split[0]).get("state") if split else None


async def _dead_dependents(dead: str, checkout: Checkout) -> None:
    history = checkout.journal.read()
    from tempo.rework import dead_dependencies

    last = last_states(history)
    deaths = dead_dependencies(history)
    from tempo.daemon import storm_producer

    box = storm_producer(root=checkout.config.state_dir / BOX_DIR, fs=checkout.fs,
                         journal=checkout.journal, clock=checkout.clock)
    for path in sorted((checkout.repo / "tickets").glob("*/ticket.md")):
        stem = path.parent.name
        if stem == dead or _ticket_state(path) != "confirmed" or last.get(stem) in {"merged", "already_satisfied", "rejected"}:
            continue
        ticket = parse_ticket(stem, path.read_text(), checkout.repo)
        for dependency in sorted(set(ticket.depends) & deaths):
            if not any(e.type == EventType.SIGNAL and e.ticket == stem
                   and e.body.get("signal") == "dead_dependency" and e.body.get("dead") == dependency
                       for e in history):
                box.enqueue(message_class="failure_report", origin=stem, stage="depends", outcome="rejected",
                            summary=f"{stem} depends on {dependency}, which was rejected or abandoned; re-wire, re-scope, or reject it",
                            occurrence_id=arrival_id("dead-dependency", stem, dependency))
                checkout.journal.append(EventType.SIGNAL, {"signal": "dead_dependency", "dead": dependency}, ticket=stem)


async def _admit(stem: str, checkout: Checkout) -> Ticket:
    """Intake every pending ticket, then validate the one to dispatch; refuse anything not dispatchable."""
    repo = checkout.repo
    result = await intake(repo, checkout.git, checkout.journal, checkout.fs)
    if refused := result.refused.get(stem):
        raise Refusal(f"{ticket_path(stem)} failed intake: "
                      + "; ".join(f"[{f.code}] {f.message} ({f.paved_road})" for f in refused),
                      f"fix the ticket, then `run {stem}` again")
    path = repo / ticket_path(stem)
    if not path.is_file():
        raise Refusal(f"{ticket_path(stem)} does not exist",
                      f"author it with `python -m tempo new {stem}`, fill it, then `run {stem}`")
    try:
        ticket = validate_ticket(stem, path.read_text(), repo)
    except TicketInvalid as e:
        raise Refusal(f"{ticket_path(stem)} is invalid: "
                      + "; ".join(f"[{f.code}] {f.message} ({f.paved_road})" for f in e.findings),
                      f"fix the ticket, then `run {stem}` again") from None
    last = last_states(checkout.journal.read())
    if last.get(stem) == "merged" or ticket.frontmatter.state == "merged":
        raise Refusal(f"{stem} is already merged", "author follow-up work as a new stem with `new`")
    if ticket.frontmatter.state != "confirmed":
        raise Refusal(f"{stem} is `state: {ticket.frontmatter.state}`, not confirmed",
                      f"set `state: confirmed` in {ticket_path(stem)} to dispatch it, or author a new stem")
    from tempo.rework import settled_dependencies

    settled = settled_dependencies(checkout.journal.read())
    if unmerged := [d for d in ticket.depends if d not in settled]:
        raise Refusal(f"{stem} depends on unmerged {', '.join(unmerged)}",
                      " then ".join(f"`run {d}`" for d in unmerged) + f" first, then `run {stem}`")
    return ticket
