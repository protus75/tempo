"""Lock-held retro receipts and the shared ordered journal window."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from tempo.artifacts import NonBlank
from tempo.box import BOX_DIR, Box, RereportJournal, read_registry
from tempo.driver import LlmStage, unrendered
from tempo.git import GitError
from tempo.journal import Event, EventType
from tempo.specs import RENDER_BOUND_CHARS, REQ_RENDER_HEADROOM, RenderOverBound, load_spec, render

RETRO_FAILED = "retro_failed"
_REPORT = re.compile(r"retro/([0-9]+)\Z")


class RetroProposal(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    fixed_failure: NonBlank
    overcorrection_risk: NonBlank
    proposed_spec_paths: list[Annotated[str, Field(pattern=r"^tempo/specs/[a-z0-9_-]+\.md$")]] = Field(min_length=1)


class RetroArtifact(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    proposals: list[RetroProposal]


@dataclass(frozen=True)
class Window:
    boundary: str
    events: tuple[Event, ...]
    started_at: str | None
    sequence: int
    evidence: dict = field(default_factory=dict, init=False, compare=False)
    completed_checks: dict = field(default_factory=dict, init=False, compare=False)


def project_window(events) -> Window:
    """Choose the boundary in journal order BEFORE any consumer filters its suffix."""
    snapshot = tuple(events)
    index, boundary, sequence = -1, "initial", 1
    for i, event in enumerate(snapshot):
        if event.type == EventType.EFFECT_COMPLETION and (match := _REPORT.fullmatch(event.key or "")):
            index, boundary, sequence = i, event.key, int(match[1]) + 1
    suffix = snapshot[index + 1:]
    started = snapshot[index].ts if index >= 0 else snapshot[0].ts if snapshot else None
    return Window(boundary, suffix, started, sequence)


def suppressing_failure(window: Window) -> Event | None:
    """A matching failure holds only until a merge later in journal order."""
    failure = None
    for event in window.events:
        if (event.type == EventType.SIGNAL and event.body.get("kind") == RETRO_FAILED
                and event.body.get("window_boundary") == window.boundary):
            failure = event
        elif event.type == EventType.STATE_TRANSITION and event.body.get("to") == "merged":
            failure = None
    return failure


def measurements(window: Window) -> dict:
    """Only durable production emitters contribute operands; identities fold per invocation."""
    from tempo.mergequeue import CONFLICT_FACTS
    from tempo.artifacts import REWORK_ORDER

    merges = []
    spikes = {kind: set() for kind in ("override", "rework", "gate-failure", "integration-red")}
    sequences, running, durations = {}, {}, {}
    for event in window.events:
        body, stem = event.body, event.ticket
        identity = (stem, sequences.get(stem, 0))
        if event.type == EventType.STATE_TRANSITION:
            if body.get("to") == "running":
                sequences[stem] = sequences.get(stem, 0) + 1
                running[stem] = event.ts
            elif body.get("to") == "merged":
                merges.append({"ticket": stem, "commit": body.get("commit"), "identity": identity})
                durations[identity] = ((datetime.fromisoformat(event.ts) - datetime.fromisoformat(running[stem]))
                                       .total_seconds() if stem in running else None)
            elif body.get("to") == "gate_failed":
                spikes["gate-failure"].add(identity)
        elif event.type == EventType.SIGNAL:
            if body.get("signal") == REWORK_ORDER:
                spikes["rework"].add((stem, body["attempt"]))
            if body.get("kind") == CONFLICT_FACTS and body.get("integration_red_paths"):
                spikes["integration-red"].add(identity)
    unique = list({(row["ticket"], row["commit"]): row for row in merges}.values())
    return {"merges": unique, "merged_count": len(unique),
            "spikes": {kind: len(ids) for kind, ids in spikes.items()},
            "elapsed_seconds": [{"ticket": row["ticket"], "commit": row["commit"],
                                 "actual_seconds": durations[tuple(row["identity"])]} for row in unique]}


def select_trigger(window: Window, evidence: dict, now: datetime) -> str | None:
    if evidence["merged_count"] >= 25:
        return "merges"
    if window.started_at is not None and now - datetime.fromisoformat(window.started_at) >= timedelta(days=7):
        return "elapsed"
    return next((kind for kind, count in evidence["spikes"].items() if count >= 5), None)


async def production_evidence(window, checkout):
    """Completed Check invoices belong to the ticket-plane commit recorded by their effect."""
    from tempo.stages import Invoice

    evidence = measurements(window)
    checks, counted, invoices, observations = {}, set(), {}, []
    for event in window.events:
        stem = event.ticket
        if (event.type == EventType.EFFECT_COMPLETION
                and (event.key or "").startswith(f"ticket-plane/{stem}/")
                and (event.key or "").endswith("/checks")):
            checks[stem] = event
            if event.key not in invoices:
                commit = event.body["result"]["commit"]
                text = await checkout.git._run(checkout.repo, "show", f"{commit}:tickets/{stem}/checks.json")
                invoice = Invoice.model_validate_json(text)
                invoices[event.key] = invoice
                observations.extend((invoice.stem, report.code, report.verdict,
                                     report.code in invoice.bypassed) for report in invoice.reports)
        if event.type != EventType.STATE_TRANSITION or event.body.get("to") != "merged":
            continue
        check = checks.pop(stem, None)
        if check is not None and check.key not in counted:
            counted.add(check.key)
            evidence["spikes"]["override"] += bool(invoices[check.key].bypassed)
    evidence["check_observations"] = observations
    window.completed_checks.clear()
    window.completed_checks.update({key: tuple((invoice.stem, report.code, report.verdict,
        report.code in invoice.bypassed) for report in invoice.reports) for key, invoice in invoices.items()})
    window.evidence.clear()
    window.evidence.update(evidence)
    return evidence


def render_report(window: Window, evidence: dict, provenance: dict, tombstones: list,
                  artifact: RetroArtifact) -> str:
    record = {"window_boundary": window.boundary, "window_start": window.started_at,
              "window_final_event": asdict(window.events[-1]) if window.events else None,
              "provenance": provenance, "measurements": evidence,
              "tombstone_digest": tombstones,
              "proposals": artifact.model_dump()["proposals"]}
    record["tombstone_reopens"] = [e.body for e in window.events if e.type == EventType.SIGNAL
                                  and e.body.get("kind") == "tombstone_auto_reopened"]
    from tempo.scorecard import project_scorecard

    card = project_scorecard(window)
    table = ["## Surface scorecard", "",
        "| Surface | Evaluated tickets | Catches | Escapes | Bypass count | Catch rate | Escape rate | Prune candidate |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    for row in card.surfaces:
        surface = row.surface.replace("|", "\\|").replace("\n", " ")
        table.append(f"| {surface} | {row.evaluated_tickets} | {row.catches} | {row.escapes} | "
                     f"{row.bypass_count} | {row.catch_rate:.4f} | {row.escape_rate:.4f} | "
                     f"{str(row.prune_candidate).lower()} |")
    return ("# Retro receipt\n\n```json\n" + json.dumps(record, indent=2, sort_keys=True) + "\n```\n\n"
            + "\n".join(table) + "\n")


class Retro:
    """Run by the writer-lock holder, one call through `prepare` and `submit`; constructing it has no effects."""

    def __init__(self, checkout, driver):
        self.checkout, self.driver = checkout, driver

    async def commit_report(self, window, text):
        path = f"tickets/retro/{window.sequence:06d}.md"
        unfinished = any(e.type == EventType.EFFECT_INTENT and e.key == f"retro/{window.sequence}"
                         for e in self.checkout.journal.read())
        async def commit():
            # Git may have committed before the completion append crashed. Finish that
            # same effect; the file remains outside the window boundary until completion.
            recorded = await self.committed_report(window) if unfinished else None
            if recorded is not None:
                return recorded
            self.checkout.fs.write(self.checkout.repo / path, self.driver.redactor.scrub(text).encode())
            await self.checkout.git.add(self.checkout.repo, [path])
            await self.checkout.git.commit(self.checkout.repo, f"tempo: retro {window.sequence:06d}", only=[path])
            return {"commit": await self.checkout.git.rev_parse(self.checkout.repo, "HEAD")}
        result = await self.driver.effects.run(commit, key=f"retro/{window.sequence}", ticket=None)
        await self.emit_proposals(f"retro/{window.sequence}")
        return result
    async def emit_proposals(self, report_key):
        """Only the completed effect's committed receipt can supply proposal evidence."""
        c = self.checkout
        completed = next((e for e in c.journal.read() if e.type == EventType.EFFECT_COMPLETION
                          and e.key == report_key and _REPORT.fullmatch(e.key or "")), None)
        if completed is None:
            return
        sequence = int(_REPORT.fullmatch(report_key)[1])
        text = await c.git._run(c.repo, "show",
            f"{completed.body['result']['commit']}:tickets/retro/{sequence:06d}.md")
        # The predecessor's explicit recovery seam also accepts a plain operator receipt.
        if not text.startswith("# Retro receipt\n\n```json\n"):
            return
        artifact = RetroArtifact.model_validate({"proposals": json.loads(
            text.split("```json\n", 1)[1].split("\n```", 1)[0])["proposals"]})
        from tempo.storm import storm_producer
        from tempo.storm import arrival_id

        box = storm_producer(root=c.config.state_dir / BOX_DIR, fs=c.fs, journal=c.journal, clock=c.clock)
        for proposal in artifact.proposals:
            fields = proposal.model_dump()
            canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            proposal_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
            origin = f"retro-proposal/{report_key}/{proposal_id}"
            box.enqueue(message_class="retro_finding", origin=origin, summary=proposal.fixed_failure,
                        retro_report_key=report_key, **fields,
                        occurrence_id=arrival_id("retro-proposal", report_key, proposal_id))

    def tombstones(self):
        c = self.checkout
        box = Box(c.config.state_dir / BOX_DIR, c.fs, rereport=RereportJournal(c.journal))
        confirmed = {m.id: m for m in box.confirmed_tombstones(c.config.box_policy)}
        # Reopened records remain committed even after their queue resolution clears.
        from tempo.policy import policy_row
        confirmed.update({m.id: m for m in box.messages() if m.reopen
                          and c.config.box_policy[policy_row(m.message_class, m.bug_origin, m.has_repro)] == "confirmed"})
        digest = []
        for record, body in read_registry(c.repo):
            if record.kind != "tombstone":
                continue
            item = {**record.model_dump(), "body": body}
            if message := confirmed.get(record.id.removeprefix("tombstone-")):
                item.update(box_id=message.id, message_class=message.message_class,
                            reports=message.reports, reopen=message.reopen)
            digest.append(item)
        return digest

    async def committed_report(self, window):
        c = self.checkout
        path = f"tickets/retro/{window.sequence:06d}.md"
        try:
            await c.git._run(c.repo, "show", f"HEAD:{path}")
        except GitError:
            return None
        commit = (await c.git._run(c.repo, "log", "-1", "--format=%H", "--", path)).strip()
        return {"commit": commit}

    def failure_report(self, identity):
        from tempo.storm import storm_producer
        c = self.checkout
        storm_producer(root=c.config.state_dir / BOX_DIR, fs=c.fs, journal=c.journal, clock=c.clock).enqueue(
            message_class="failure_report", origin=identity, stage="retro", outcome="infra_error",
            summary="Retro failed; inspect the redacted retro spools and engine log, repair the cause "
                    "and complete a report through the lock-held retro hook to release this window",
            reason=identity, occurrence_id=identity)

    async def due(self, events=None):
        """The window, its evidence, and the trigger when retro is due; None otherwise. Read-only."""
        window = project_window(self.checkout.journal.read() if events is None else events)
        evidence = await production_evidence(window, self.checkout)
        trigger = select_trigger(window, evidence, self.checkout.clock())
        return None if trigger is None else (window, evidence, trigger)

    async def begin(self):
        """Before the call: a line when nothing needs a call, else (stage, consumed, context)."""
        c = self.checkout
        for event in c.journal.read():
            if event.type == EventType.EFFECT_COMPLETION and _REPORT.fullmatch(event.key or ""):
                await self.emit_proposals(event.key)
        journal_len = len(c.journal.read())
        due = await self.due()
        if due is None:
            return None
        window, evidence, trigger = due
        if (failure := suppressing_failure(window)) is not None:
            self.failure_report(failure.key)
            return f"suppressed until {failure.key} is repaired"
        if any(e.type == EventType.EFFECT_INTENT and e.key == f"retro/{window.sequence}"
               for e in window.events) and await self.committed_report(window) is not None:
            await self.commit_report(window, "")
            return f"retro/{window.sequence} committed"
        from tempo.checkout import SPECS_DIR
        spec = load_spec((SPECS_DIR / "retro.md").read_text())
        head = await c.git.rev_parse(c.repo, "HEAD")
        provenance = {"head": head, "spec_version": spec.meta.version, "at": c.clock().isoformat()}
        consumed = {"events": [asdict(e) for e in window.events], "measurements": evidence,
                    "provenance": provenance}
        driver = self.driver

        def render_input(material, findings):
            retries = json.dumps([f.model_dump() for f in findings])
            try:
                prompt = render(spec, {"window": json.dumps(material, sort_keys=True),
                                       "retry_findings": retries})
                if len(prompt) <= RENDER_BOUND_CHARS * REQ_RENDER_HEADROOM:
                    return prompt
            except RenderOverBound:
                pass
            name = "retro/window.jsonl"
            stem = f"retro/{window.sequence}"
            driver.spool.write(stem, 1, name,
                "".join(json.dumps(event, sort_keys=True) + "\n" for event in material["events"]))
            on_demand = {"events_path": str((driver.spool.root / stem / "1" / name).resolve()),
                         "measurements": material["measurements"], "provenance": material["provenance"]}
            return render(spec, {"window": json.dumps(on_demand, sort_keys=True),
                                 "retry_findings": retries})
        context = {"kind": "retro", "journal": journal_len, "sequence": window.sequence, "trigger": trigger,
                   "evidence": evidence, "provenance": provenance, "tier": spec.meta.tier,
                   "effort": spec.meta.effort}
        return retro_stage(render_input), consumed, context

    async def finish(self, context, outcome, artifact):
        """After the final reply: the committed receipt and its proposals, or the failure report."""
        c = self.checkout
        window = project_window(c.journal.read()[:context["journal"]])
        await production_evidence(window, c)  # the window's completed checks, which the scorecard reads
        if outcome == "ok":
            text = render_report(window, context["evidence"], context["provenance"], self.tombstones(), artifact)
            return await self.commit_report(window, text)
        identity = f"retro-failed/{window.boundary}/{context['trigger']}"
        c.journal.append(EventType.SIGNAL, {"kind": RETRO_FAILED, "window_boundary": window.boundary,
            "trigger": context["trigger"], "error_code": outcome}, ticket=None, key=identity)
        self.failure_report(identity)
        return None


def retro_stage(render_input=unrendered) -> LlmStage:
    return LlmStage(surface="retro", emits=RetroArtifact, gates=[], render=render_input)
