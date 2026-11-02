"""The `run <stem>` scaffold verb (TEMPO_PLAN.md sections 11.2, 18): one ticket through intake, the
single-writer lockfile, and the stage-dispatch seam, all in the one process holding the lock.

Exit codes (section 18): 0 merged, 1 a non-ok ticket terminal, 2 an engine-plane refusal.
"""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import assert_never


from tempo.artifacts import Finding, Harvest, StageResult
from tempo.box import BOX_DIR
from tempo.storm import arrival_id
from tempo.backlog import admit, dead_dependents, ticket_state
from tempo.checkout import SPECS_DIR, Checkout, Refusal
from tempo.caps import capability, consume, next_rung, spent, spent_reason
from tempo.driver import Driver
from tempo.effects import Effects
from tempo.journal import TERMINAL_STATES, Dispatch as JournalDispatch, Event, EventType, dispatch_of
from tempo.llm import LLM
from tempo.lockfile import Lockfile
from tempo.merge import compose_pipeline, merge
from tempo.reconcile import reconcile
from tempo.stages import (DiagnosisMaterial, StageContext, diagnose, harvest, harvest_orphan, run_stages,
                          run_wall_seconds, write_diagnosis)
from tempo.status import last_states, reject_queue
from tempo.tickets import (INTAKE_SIGNAL, Ticket, intake,
                           stamp, stem_findings, ticket_path)

EXIT_MERGED = 0
EXIT_TICKET = 1
EXIT_REFUSED = 2

IDENTICAL_K = 3

# The stage seam: drives the validated ticket through the stages and merge, journals the run's single
# terminal transition (section 11), and returns that terminal state.
Dispatch = Callable[[Ticket], Awaitable[str]]


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
    if any(f.code == 'quota_exhausted' for f in result.findings):
        return await failure_terminal(ctx, ticket, outcome=result.outcome, stage=stage,
            findings=result.findings, attempt=run.attempt)
    if result.outcome in {"infra_error", "timeout"}:
        ticket_sha = await ctx.git.rev_parse(ctx.repo, f"HEAD:{ticket_path(ticket.stem)}")
        consume(ctx.driver.journal, ticket.stem, "infra", ticket_sha)
    over_bound = result.outcome == "premise_failed" and any(
        f.code == "render_over_bound" for f in result.findings)
    if result.outcome == "premise_failed" and not over_bound:
        ticket_sha = await ctx.git.rev_parse(ctx.repo, f"HEAD:{ticket_path(ticket.stem)}")
        consume(ctx.driver.journal, ticket.stem, "premise_bounce", ticket_sha)
    diagnosed = not over_bound
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
            consume(ctx.driver.journal, ticket.stem, "decisions", ticket_sha)
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
                           verdict: str | None = None) -> str:
    """Preserve the producing terminal, then retire a published split before wiping and returning."""
    from tempo.rework import ReworkOrder
    from tempo.storm import storm_producer

    reason = ",".join(sorted({f.code for f in findings})) or outcome
    terminal = {"to": outcome, "stage": stage, "reason": reason}
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
            # The scripted runner has no session host; it ladders on the first configured host.
            rung = next_rung(ctx.config, next(iter(ctx.config.hosts)), tier, effort)
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
    if not mechanical and not split and routed:
        terminal["routed"] = "reject_queue"
    ctx.driver.journal.append(EventType.STATE_TRANSITION, terminal, ticket=ticket.stem)
    if split:
        checkout = Checkout(ctx.repo, ctx.config, ctx.env, ctx.exec_, ctx.git, ctx.driver.journal,
                            ctx.fs, ctx.driver.clock, ctx.driver.sleep)
        await reject_ticket(ticket.stem, checkout)
    if ctx.worktree(ticket.stem).exists():
        await ctx.git.worktree_remove(ctx.repo, ctx.worktree(ticket.stem))
    return "rejected" if split else outcome


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
            checkout.control.timers.reconstruct()
            checkout.control.timers.fire_due()
        if hasattr(dispatch, 'prepare'):
            await dispatch.prepare()
        from tempo.storm import storm_producer

        storm_producer(root=checkout.config.state_dir / BOX_DIR, fs=checkout.fs,
                       journal=checkout.journal, clock=checkout.clock).recover()
        ticket = await admit(stem, checkout)
        checkout.journal.append(EventType.STATE_TRANSITION, {"to": "running"}, ticket=stem)
        terminal = await dispatch(ticket)
        if terminal not in TERMINAL_STATES:
            raise ValueError(f"stage seam returned {terminal!r}, not a terminal state {sorted(TERMINAL_STATES)}")
        return EXIT_MERGED if terminal == "merged" else EXIT_TICKET
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
        if path.is_file() and ticket_state(path) == "draft":
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
    return (path.is_file() and ticket_state(path) == "draft" and any(
        e.ticket == stem and e.body.get("signal") == INTAKE_SIGNAL
        for e in checkout.journal.read()))


async def reject_ticket(stem: str, checkout: Checkout) -> bool:
    """Reject mutation for a caller already holding the writer lock."""
    rel = ticket_path(stem)
    path = checkout.repo / rel
    last = last_states(checkout.journal.read()).get(stem)
    if path.is_file() and ticket_state(path) != "rejected":
        checkout.fs.write(path, stamp(path.read_text(), "state", "rejected").encode())
        await checkout.git.add(checkout.repo, [rel])
        await checkout.git.commit(checkout.repo, f"tempo({stem}): rejected", only=[rel])
    if last != "rejected":
        checkout.journal.append(EventType.SIGNAL,
                                {"signal": "reject_verdict", "verdict": "kill", "actor": "operator"}, ticket=stem)
        checkout.journal.append(EventType.STATE_TRANSITION, {"to": "rejected"}, ticket=stem)
    await dead_dependents(stem, checkout)
    return last != "rejected"
