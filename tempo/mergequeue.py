"""Dormant serial admission (19.P3.merge-queue); production routing remains in merge.py."""

import asyncio
import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

if TYPE_CHECKING:
    from tempo.daemon import PauseConsumer

from pydantic import BaseModel, ConfigDict, field_validator

from tempo import merge, stages
from tempo.baseline import review_coverage
from tempo.control import releases
from tempo.effects import Effects
from tempo.artifacts import Cost, Finding, StageResult
from tempo.drain import authored_at, sort_key
from tempo.gates import GateReport, run_gates
from tempo.git import GitError, RebaseRefused
from tempo.journal import Event, EventType, TERMINAL_STATES, run_seq
from tempo.providers import child_env
from tempo.seams import ExecutableNotFound
from tempo.stages import CommandResult, Evidence, StageContext
from tempo.tickets import TICKETS_DIR, Ticket, ticket_path, validate_ticket

CONFLICT_FACTS = "merge_conflict_facts"
RED_STREAK = "merge_red_streak"
TREE_MISMATCH = "merge_tree_mismatch"
RED_STREAK_LIMIT = 3
MERGE_REGATE = "merge-regate"


def regate_receipts(events) -> dict[str, dict]:
    receipts = {}
    road = "restore consistent merge-regate evidence and retained reviewed artifacts before restarting"
    fields = {"stem", "attempt", "reviewed_sha", "candidate_base_sha", "candidate_sha", "checked_tree",
              "review_provider", "review_model", "admission", "reason"}
    for event in events:
        if event.type != EventType.EFFECT_COMPLETION or not (event.key or "").startswith(MERGE_REGATE + "/"):
            continue
        value = event.body.get("result")
        if (not isinstance(value, dict) or set(value) != fields
                or type(value.get("attempt")) is not int or value["attempt"] < 0
                or not isinstance(value.get("stem"), str) or event.ticket != value["stem"]
                or any(not isinstance(value[k], str) or not re.fullmatch(r"[0-9a-f]{40}", value[k])
                       for k in ("reviewed_sha", "candidate_base_sha", "candidate_sha", "checked_tree"))
                or any(value[k] is not None and (not isinstance(value[k], str) or not value[k].strip())
                       for k in ("review_provider", "review_model"))
                or value["admission"] not in ("READY", "HELD")
                or value["reason"] not in ("bootstrap", "foreground", "confirmed", "go_covered", "go_absent", "go_revoked", "review_off_baseline")
                or (value["admission"] == "HELD") != (value["reason"] in ("go_absent", "go_revoked", "review_off_baseline"))
                or event.key != f"{MERGE_REGATE}/{value['stem']}/{value['attempt']}/{value['candidate_base_sha']}/{value['candidate_sha']}"
                or event.key in receipts and receipts[event.key] != value):
            raise ValueError(road)
        receipts[event.key] = value
    return receipts


def held_admissions(events) -> dict[str, tuple[str, dict]]:
    """A completed HELD owns its run until terminal, including a later READY regate."""
    events = tuple(events)
    receipts = regate_receipts(events)
    live, reviewed = {}, {}
    for position, event in enumerate(events):
        stem = event.ticket
        if event.type == EventType.STATE_TRANSITION and event.body.get("to") in TERMINAL_STATES:
            live.pop(stem, None)
        if event.type == EventType.EFFECT_COMPLETION and event.key in receipts:
            value = receipts[event.key]
            if value["admission"] == "HELD" or stem in live:
                identity = stem, value["attempt"]
                if (value["attempt"] != run_seq(events[:position], stem)
                        or identity in reviewed and reviewed[identity] != value["reviewed_sha"]):
                    raise ValueError("contradictory held run; restore its producing attempt and approval evidence")
                reviewed[identity] = value["reviewed_sha"]
                # Keep the original authorization identity after a READY regate.
                hold = event.key if stem not in live else live[stem][0]
                live[stem] = hold, value
    return live


class ConflictHandoff(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    stem: str
    reviewed_sha: str
    conflicted_paths: list[str]
    findings: list[Finding]
    approval_invalidated: Literal[True] = True

    @field_validator("conflicted_paths")
    @classmethod
    def _ordered_paths(cls, paths: list[str]) -> list[str]:
        if paths != sorted(set(paths)):
            raise ValueError("conflicted_paths must be sorted and unique")
        return paths


class TreeMismatch(RuntimeError):
    """Main has moved, but differs from the checked tree: pause without retiring the branch."""


class MergeQueue:
    def __init__(self, ctx: StageContext, *, escalate: Callable[[Event], None],
                 control: "PauseConsumer | None" = None) -> None:
        self.ctx = ctx
        self.escalate = escalate
        self.pending: dict[str, tuple[Ticket, int]] = {}
        self.active: str | None = None
        self.control = control
        self.hold_id = (control.admission if control is not None and
                        control.admission not in control.projection.released_hold_ids else None)
        self.paused = self.hold_id is not None
        self.red_stems: list[str] = []
        self._slot = asyncio.Lock()
        self.supervised = False
        self.held: dict[str, tuple[Ticket, int, str, dict]] = {}
        self.deliveries: list[tuple[Ticket, int, object]] = []
        self.contexts: dict[str, StageContext] = {}

    def offer(self, ticket: Ticket, *, attempt: int) -> None:
        self.pending[ticket.stem] = (ticket, attempt)
        self.contexts[ticket.stem] = self.ctx

    def _release(self) -> None:
        if (self.paused and self.control is not None and
                self.hold_id in self.control.projection.released_hold_ids):
            self.paused = False
            self.red_stems.clear()
            self.hold_id = None

    def _hold(self, stem: str, body: dict) -> None:
        hold_id = uuid4().hex
        event = self.ctx.driver.journal.append(
            EventType.SIGNAL, {**body, "hold_id": hold_id}, ticket=stem, key=None)
        self.paused, self.hold_id = True, hold_id
        if self.control is not None:
            self.control.hold(hold_id)
        self.escalate(event)

    async def process(self) -> list[StageResult | ConflictHandoff | merge.Held]:
        results = []
        self.deliveries = []
        async with self._slot:
            if self.supervised and self.control is not None:
                await self.control.inbox.settle_admissions(self.confirm_draft)
            for stem, (ticket, attempt, hold, receipt) in tuple(self.held.items()):
                release = releases(self.ctx.driver.journal.read()).get(hold)
                covered, _, _ = await self._policy(ticket, attempt, receipt["reviewed_sha"])
                squashed = any(e.type == EventType.EFFECT_COMPLETION and e.key == f"merge/{stem}/{attempt}"
                               for e in self.ctx.driver.journal.read())
                if release or covered or squashed:
                    self.pending[stem] = ticket, attempt
            while True:
                self._release()
                if not self.pending or self.paused:
                    break
                ages = authored_at(self.ctx.driver.journal.read())
                ticket, attempt = min(self.pending.values(), key=lambda row: sort_key(row[0], ages))
                del self.pending[ticket.stem]
                self.active = ticket.stem
                try:
                    self.ctx = self.contexts.get(ticket.stem, self.ctx)
                    result = await self._admit(ticket, attempt)
                    results.append(result)
                    self.deliveries.append((ticket, attempt, result))
                finally:
                    self.active = None
        return results

    async def confirm_draft(self, stem, request_id):
        from tempo import runner
        await runner.confirm_draft(stem, request_id, self.ctx)

    async def _policy(self, ticket, attempt, reviewed):
        served = await stages.served_review(self.ctx, ticket.stem, attempt, reviewed)
        config = (self.control.load_config() if self.control is not None and hasattr(self.control, "load_config")
                  else self.ctx.config)
        from tempo.caps import capability
        tier, _ = capability(ticket, self.ctx.driver.journal.read())
        covered, reason = review_coverage(config, self.ctx.driver.journal.read(), specs_dir=self.ctx.specs_dir,
                                         tier=tier, served=served)
        return covered, reason, served

    async def retained_ticket(self, stem, receipt):
        ctx = self.ctx
        road = "restore the retained committed ticket, reviewed evidence and candidate; then restart serve"
        try:
            text = await ctx.git._run(ctx.repo, "show", f"main:{ticket_path(stem)}")
            ticket = validate_ticket(stem, text, ctx.repo)
            review = stages.read_review(await ctx.git._run(ctx.repo, "show", f"main:tickets/{stem}/review.md"))
            if (not isinstance(review, stages.ApprovedInvoice) or review.stem != stem
                    or review.reviewed_sha != receipt["reviewed_sha"] or not ctx.worktree(stem).is_dir()
                    or await stages.dirty_code(ctx, stem)
                    or await ctx.git.rev_parse(ctx.worktree(stem), "HEAD") != await ctx.git.rev_parse(ctx.repo, stem)):
                raise ValueError(road)
            lifts = [e.body["result"] for e in ctx.driver.journal.read()
                     if e.type == EventType.EFFECT_COMPLETION and e.key == f"ticket-plane/{stem}/{receipt['attempt']}/review"]
            if lifts:
                commit = lifts[-1]["commit"]
                for path in (ticket_path(stem), f"tickets/{stem}/review.md"):
                    if await ctx.git.rev_parse(ctx.repo, f"main:{path}") != await ctx.git.rev_parse(ctx.repo, f"{commit}:{path}"):
                        raise ValueError(road)
            if any(receipt[k] is not None for k in ("review_provider", "review_model")):
                if await stages.served_review(ctx, stem, receipt["attempt"], receipt["reviewed_sha"]) != (
                        receipt["review_provider"], receipt["review_model"]):
                    raise ValueError(road)
            return ticket
        except Exception as exc:
            raise ValueError(f"retained admission {stem} is unavailable: {exc}; {road}") from exc

    async def reconstruct(self):
        events = self.ctx.driver.journal.read()
        releases(events)
        live = held_admissions(events)
        for stem, (hold, receipt) in live.items():
            ticket = await self.retained_ticket(stem, receipt)
            self.held[stem] = ticket, receipt["attempt"], hold, receipt
        receipts = regate_receipts(events)
        retained_runs = {(r["stem"], r["attempt"]) for r in receipts.values() if r["admission"] == "HELD"}
        latest = {(r["stem"], r["attempt"]): r for r in receipts.values()}
        for identity in retained_runs:
            receipt = latest[identity]
            stem, attempt = receipt["stem"], receipt["attempt"]
            if stem in live or not self.ctx.worktree(stem).exists() or run_seq(events, stem) != attempt + 1:
                continue
            terminal = next(e.body for e in reversed(events) if e.ticket == stem and e.type == EventType.STATE_TRANSITION)
            if terminal["to"] == "merged":
                admitted = terminal["commit"] or receipt["candidate_base_sha"]
                if await self.ctx.git.rev_parse(self.ctx.repo, admitted + "^{tree}") != receipt["checked_tree"]:
                    raise ValueError("restore the squash's exact checked tree before retiring held custody")
            if terminal["to"] in ("merged", "rejected"):
                if terminal["to"] == "rejected":
                    from tempo import runner
                    ctx = self.ctx
                    await runner._dead_dependents(stem, runner.Checkout(ctx.repo, ctx.config, ctx.env, ctx.exec_,
                        ctx.git, ctx.driver.journal, ctx.fs, ctx.driver.clock, ctx.driver.sleep))
                await merge.retire(self.ctx, stem)

    async def _finish(self, ticket, attempt, reviewed, checked_tree, started, findings=()):
        admission = await merge.write_squash(self.ctx, ticket, reviewed, attempt=attempt)
        main_tree = await self.ctx.git.rev_parse(self.ctx.repo, "main^{tree}")
        if main_tree != checked_tree:
            self._hold(ticket.stem, {"kind": TREE_MISMATCH, "checked_tree": checked_tree, "main_tree": main_tree})
            raise TreeMismatch(f"{ticket.stem}: checked tree {checked_tree} differs from main tree {main_tree}")
        await merge.retire(self.ctx, ticket.stem)
        self.held.pop(ticket.stem, None)
        return StageResult(outcome="ok", artifact=admission, findings=list(findings),
                           cost=Cost(seconds=(self.ctx.driver.clock() - started).total_seconds()))

    def _signal(self, stem: str, body: dict, *, escalate: bool = False) -> None:
        event = self.ctx.driver.journal.append(EventType.SIGNAL, body, ticket=stem, key=None)
        if escalate:
            self.escalate(event)

    def _facts(self, stem: str, conflicts: set[str], strategies: set[str], *,
               handoff: bool = False, red: Sequence[str] = ()) -> None:
        self._signal(stem, {"kind": CONFLICT_FACTS, "conflicted_paths": sorted(conflicts),
                           "resolving_rung": "rework" if handoff else "mechanical" if strategies else "none",
                           "strategy_paths": sorted(strategies), "integration_red_paths": sorted(set(red))})

    def _finding(self, message: str, road: str, *, path: str | None = None,
                 code: str = "post_rebase_regate") -> Finding:
        return Finding(code=code, path=path, message=self.ctx.driver.redactor.scrub(message), paved_road=road)

    async def _command(self, ticket: Ticket, attempt: int, argv: list[str], code: str,
                       name: str) -> tuple[CommandResult, GateReport]:
        road = "make the configured command exit 0 on the rebased candidate and retry"
        try:
            rc, out, err = await self.ctx.exec_.run(
                argv, cwd=self.ctx.worktree(ticket.stem), env=child_env(self.ctx.env, self.ctx.config),
                timeout=ticket.stuck_minutes * 60.0)
        except TimeoutError:
            rc, out, err = None, "", f"timed out after the ticket's stuck budget ({ticket.stuck_minutes}m)"
            road = "make the command finish within the ticket's stuck budget or correct that budget and retry"
        except ExecutableNotFound as exc:
            rc, out, err = None, "", str(exc)
            road = "install the executable or correct argv/PATH in the child environment and retry"
        self.ctx.driver.spool.write(ticket.stem, attempt, name,
                                    f"$ {' '.join(argv)}\n[exit {rc}]\n--- stdout\n{out}\n--- stderr\n{err}")
        tail = self.ctx.driver.redactor.scrub((out + err)[-stages.OUTPUT_TAIL_CHARS:])
        result = CommandResult(argv=list(argv), rc=rc, tail=tail, base_red=False)
        findings = [] if rc == 0 else [self._finding(
            f"{argv!r} exited {rc}: {tail.strip() or '(no output)'}", road, code=code)]
        return result, GateReport(code=code, verdict="fail" if findings else "pass", findings=findings)

    async def host_checks(self, ticket: Ticket, *, attempt: int, safety: bool
                          ) -> tuple[list[CommandResult], list[GateReport]]:
        entries = list(enumerate(self.ctx.config.review.mechanical, 1))
        reports = []
        if safety:
            selected = set()
            for code in self.ctx.config.merge.safety_checks:
                matches = [(n, entry) for n, entry in entries if entry.code == code]
                if len(matches) != 1 or matches[0][1].severity != "hard":
                    finding = self._finding(
                        f"merge.safety_checks designation {code!r} does not resolve to exactly one hard entry",
                        "declare exactly one hard review.mechanical entry for this code, or remove it from merge.safety_checks")
                    reports.append(GateReport(code=finding.code, verdict="fail", findings=[finding]))
                else:
                    selected.add(matches[0][0])
            if reports:
                return [], reports
            entries = [(n, entry) for n, entry in entries if n in selected]
        else:
            entries = [(n, entry) for n, entry in entries
                       if entry.trigger == "always" and entry.severity == "hard"]
        results = []
        tier = "safety" if safety else "integration"
        for n, entry in entries:
            result, report = await self._command(ticket, attempt, entry.argv, entry.code,
                                                 f"merge-{tier}/host-{n:02d}.txt")
            results.append(result)
            reports.append(report)
        return results, reports

    def _strategy(self, path: str, ticket: Ticket):
        matches = [s for s in self.ctx.config.merge.strategies if stages._in_fence(path, s.paths)]
        road = "declare exactly one strategy for this path; regenerate paths must be outside the ticket fence"
        if len(matches) != 1:
            return None, self._finding(f"{path} has {len(matches)} matching strategies", road, path=path)
        strategy = matches[0]
        if strategy.strategy == "regenerate" and stages._in_fence(path, ticket.scope_fence):
            return None, self._finding(f"{path} is owned by both regenerate and the ticket fence", road, path=path)
        return strategy, None

    def _scoped(self, evidence: Evidence, ticket: Ticket) -> tuple[Evidence, list[Finding]]:
        exempt, findings = [], []
        for path in evidence.changed_files:
            if any(stages._in_fence(path, s.paths) for s in self.ctx.config.merge.strategies):
                strategy, finding = self._strategy(path, ticket)
                if finding:
                    findings.append(finding)
                elif strategy:
                    exempt.append(path)
        return evidence.model_copy(update={"scope_fence": [*evidence.scope_fence, *exempt]}), findings

    async def _resolve(self, ticket: Ticket, attempt: int, paths: list[str], hits: set[str]) -> list[Finding]:
        worktree = self.ctx.worktree(ticket.stem)
        for path in paths:
            strategy, finding = self._strategy(path, ticket)
            if finding:
                return [finding]
            try:
                if strategy.strategy == "union":
                    base, ours, theirs = [await self.ctx.git._run(worktree, "show", f":{n}:{path}")
                                          for n in (1, 2, 3)]
                    if ("\0" in base + ours + theirs or not ours.startswith(base) or not theirs.startswith(base)):
                        return [self._finding(f"{path} is not an append-only union",
                                              "resolve the conflicting edits through Rework and obtain a new approval",
                                              path=path)]
                    text = ours + (theirs[len(base):] if ours != theirs else "")
                    self.ctx.fs.write(worktree / path, text.encode())
                else:
                    await self.ctx.git._run(worktree, "checkout", "--ours", "--", path)
                    _, report = await self._command(ticket, attempt, strategy.argv, "post_rebase_regate",
                                                    f"merge-resolution/generator-{len(hits) + 1:02d}.txt")
                    if report.verdict == "fail":
                        return report.findings
                await self.ctx.git.add(worktree, [path])
                hits.add(path)
            except GitError as exc:
                return [self._finding(f"cannot resolve {path}: {exc}",
                                      "resolve the conflict through Rework and obtain a new approval", path=path)]
        return []

    async def _admit(self, ticket: Ticket, attempt: int) -> StageResult | ConflictHandoff | merge.Held:
        ctx, stem = self.ctx, ticket.stem
        worktree = ctx.worktree(stem)
        started = ctx.driver.clock()
        conflicts, hits = set(), set()
        rebasing = False
        retained = self.held.get(stem)
        release = None
        if retained is not None:
            _, attempt, hold, receipt = retained
            await self.retained_ticket(stem, receipt)
            release = releases(ctx.driver.journal.read()).get(hold)
            if release and release["verdict"] == "reject":
                from tempo import runner
                ctx.driver.journal.append(EventType.STATE_TRANSITION, {"to": "rejected"}, ticket=stem)
                checkout = runner.Checkout(ctx.repo, ctx.config, ctx.env, ctx.exec_, ctx.git, ctx.driver.journal,
                                           ctx.fs, ctx.driver.clock, ctx.driver.sleep)
                await runner._dead_dependents(stem, checkout)
                await merge.retire(ctx, stem)
                self.held.pop(stem, None)
                return StageResult(outcome="ok", artifact=None, findings=[], cost=Cost())
            reviewed = receipt["reviewed_sha"]
            covered, _, _ = await self._policy(ticket, attempt, reviewed)
            completed = [e for e in ctx.driver.journal.read() if e.type == EventType.EFFECT_COMPLETION
                         and e.key == f"merge/{stem}/{attempt}"]
            if completed:
                return await self._finish(ticket, attempt, reviewed, receipt["checked_tree"], started)
            if not release and not covered:
                return merge.Held(stem, attempt, hold)
            if (await ctx.git.rev_parse(ctx.repo, "main") == receipt["candidate_base_sha"]
                    and await ctx.git.rev_parse(worktree, "HEAD") == receipt["candidate_sha"]):
                if await ctx.git.rev_parse(worktree, "HEAD^{tree}") != receipt["checked_tree"]:
                    raise ValueError("restore the retained checked candidate before admission")
                return await self._finish(ticket, attempt, reviewed, receipt["checked_tree"], started)

        def refused(findings: list[Finding]) -> StageResult:
            return merge._refused(findings, Cost(seconds=(ctx.driver.clock() - started).total_seconds()))

        try:
            reviewed = receipt["reviewed_sha"] if retained else await ctx.git.rev_parse(ctx.repo, stem)
            candidate_base = await ctx.git.rev_parse(ctx.repo, "main")
            seeds, checks_text = await merge.gather_seeds(ctx, ticket)
            await ctx.git.restore(worktree, [TICKETS_DIR], source=stem)
            rebasing = True
            step = ctx.git.rebase_stop_at_conflict(worktree, stages.MAIN)
            while True:
                try:
                    await step
                    rebasing = False
                    break
                except RebaseRefused as exc:
                    rebasing = False
                    self._facts(stem, conflicts, hits)
                    return refused([self._finding(f"rebase onto main refused: {exc.err.strip()}",
                                   f"re-run `run {stem}` so Implement re-branches from current main")])
                except GitError:
                    paths = await ctx.git.conflicted_paths(worktree)
                    conflicts.update(paths)
                    findings = await self._resolve(ticket, attempt, paths, hits)
                    if findings:
                        await ctx.git._call(worktree, "rebase", "--abort")
                        rebasing = False
                        self._facts(stem, conflicts, hits, handoff=True)
                        return ConflictHandoff(stem=stem, reviewed_sha=reviewed,
                                               conflicted_paths=sorted(conflicts), findings=findings)
                    step = ctx.git.rebase_continue(worktree)

            evidence = await stages.gather_safety_evidence(ctx, ticket, "ok")
            candidate = merge.read_candidate(ctx, ticket, reviewed, evidence.changed_files, seeds, checks_text)
            severity = stages.check_severity(ctx, ticket)
            if candidate.seeds:
                severity["requisition_review"] = "hard"
            scoped, strategy_findings = self._scoped(evidence, ticket)
            safety = [run_gates((g for g in stages.CHECK_GATES if g.code in {"scope_fence", "run_record"}),
                                scoped, worktree, severity=severity),
                      run_gates(merge.MERGE_GATES, candidate, ctx.repo, severity=severity)]
            _, host = await self.host_checks(ticket, attempt=attempt, safety=True)
            hard = strategy_findings + [f for g in safety for r in g.hard_failures for f in r.findings]
            hard += [f for r in host if r.verdict == "fail" for f in r.findings]
            if hard:
                self._facts(stem, conflicts, hits)
                return refused(hard)

            evidence = await stages.gather_evidence(ctx, ticket, "ok", attempt=attempt, stage="merge-integration")
            scoped, strategy_findings = self._scoped(evidence, ticket)
            candidate = merge.read_candidate(ctx, ticket, reviewed, evidence.changed_files, seeds, checks_text)
            integration = [run_gates(stages.CHECK_GATES, scoped, worktree, severity=severity),
                           run_gates(merge.MERGE_GATES, candidate, ctx.repo, severity=severity)]
            _, host = await self.host_checks(ticket, attempt=attempt, safety=False)
            hard = strategy_findings + [f for g in integration for r in g.hard_failures for f in r.findings]
            hard += [f for r in host if r.verdict == "fail" for f in r.findings]
            self._facts(stem, conflicts, hits, red=evidence.changed_files if hard else [])
            if hard:
                if stem not in self.red_stems:
                    self.red_stems.append(stem)
                if len(self.red_stems) == RED_STREAK_LIMIT:
                    self._hold(stem, {"kind": RED_STREAK, "stems": list(self.red_stems),
                                      "limit": RED_STREAK_LIMIT})
                return refused(hard)
            self.red_stems.clear()
            checked_tree = await ctx.git.rev_parse(worktree, "HEAD^{tree}")
            candidate_sha = await ctx.git.rev_parse(worktree, "HEAD")
            covered, reason, served = await self._policy(ticket, attempt, reviewed)
            if not self.supervised:
                covered, reason = True, "bootstrap"
            elif release:
                covered, reason = True, "confirmed"
            key = f"{MERGE_REGATE}/{stem}/{attempt}/{candidate_base}/{candidate_sha}"
            async def receipt_action():
                return dict(stem=stem, attempt=attempt, reviewed_sha=reviewed, candidate_base_sha=candidate_base,
                            candidate_sha=candidate_sha, checked_tree=checked_tree,
                            review_provider=served[0], review_model=served[1],
                            admission="READY" if covered else "HELD", reason=reason)
            receipt = await Effects(ctx.driver.journal).run(receipt_action, key=key, ticket=stem)
            if self.supervised and not covered:
                self.held[stem] = ticket, attempt, key, receipt
                return merge.Held(stem, attempt, key)
            return await self._finish(ticket, attempt, reviewed, checked_tree, started,
                                      [f for g in safety + integration for f in g.findings])
        finally:
            if rebasing:
                # Shield cleanup from the cancellation that triggered unwinding.
                abort = asyncio.create_task(ctx.git._call(worktree, "rebase", "--abort"))
                try:
                    await asyncio.shield(abort)
                except asyncio.CancelledError:
                    await abort
                    raise
