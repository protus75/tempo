"""Split proposal checks and journal supersession (AGENT_PLAN.md section 4.1, "Box, split, confirm, and retro")."""

from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from tempo.artifacts import Finding, NonBlank
from tempo.backlog import SUPERSEDES, successor_leaves, supersedes_maps
from tempo.git import GitError
from tempo.journal import EventType
from tempo.stages import StageContext
from tempo.tickets import (
    Ticket,
    TicketInvalid,
    stem_findings,
    ticket_path,
    validate_ticket,
)


class TicketProposal(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    stem: NonBlank
    ticket: NonBlank


def _finding(message: str, road: str, *, path: str | None = None) -> Finding:
    return Finding(code="ticket_schema", path=path, message=message, paved_road=road)


def check_stems(ctx: StageContext, original: Ticket, proposals: Sequence[TicketProposal]) -> list[Finding]:
    """At least two distinct successor stems, each fresh: absent from ticket dirs and journal identities."""
    stems = [p.stem for p in proposals]
    if len(stems) < 2 or len(stems) != len(set(stems)):
        return [_finding("a split needs at least two distinct successor stems",
                         "propose two or more tickets with distinct stems")]
    identities = {e.ticket for e in ctx.driver.journal.read() if e.ticket is not None}
    errors = []
    for proposal in proposals:
        errors.extend(stem_findings(proposal.stem))
        if (proposal.stem == original.stem or (ctx.repo / "tickets" / proposal.stem).exists()
                or proposal.stem in identities):
            errors.append(_finding(f"successor {proposal.stem!r} is not fresh",
                                   "choose a stem absent from ticket directories and journal identities"))
    return errors


def check_tickets(original: Ticket, proposals: Sequence[TicketProposal], tree: Path) -> list[Finding]:
    """Each proposal, written into `tree` beside its siblings, is valid and keeps the original's fields."""
    errors: list[Finding] = []
    expected = {"source": original.frontmatter.source, "state": "confirmed",
                "agent_tier": original.frontmatter.agent_tier, "agent_effort": original.frontmatter.agent_effort}
    for proposal in proposals:
        try:
            candidate = validate_ticket(proposal.stem, proposal.ticket, tree)
        except TicketInvalid as exc:
            errors.extend(exc.findings)
            continue
        for key, value in expected.items():
            if getattr(candidate.frontmatter, key) != value:
                errors.append(_finding(f"proposal must preserve {key}: {value}",
                                       f"set {key} to {value} in every proposal", path=ticket_path(proposal.stem)))
        if original.stem in candidate.depends:
            errors.append(_finding("a successor depends on the superseded original",
                                   "depend on a buildable sibling or original predecessor instead",
                                   path=ticket_path(proposal.stem)))
    return errors


async def record_supersedes(ctx: StageContext, stem: str, proposals: Sequence[TicketProposal]) -> list[Finding]:
    """The lock-owning publisher calls this AFTER committing all reviewed split proposals."""
    successors = tuple(sorted(p.stem for p in proposals))
    maps = supersedes_maps(ctx.driver.journal.read())
    road = "retain the recorded map; author fresh, acyclic successors and commit every reviewed ticket first"
    if stem in maps:
        return [] if maps[stem] == successors else [_finding(
            "supersedes map cannot replace an existing map", road)]
    if (not successors or len(successors) != len(set(successors)) or stem in successors
            or any(stem in successor_leaves(s, maps) for s in successors)):
        return [_finding("supersedes map would close a cycle or self-edge", road)]
    for proposal in proposals:
        if errors := stem_findings(proposal.stem):
            return errors
        path = ticket_path(proposal.stem)
        try:
            committed = await ctx.git._run(ctx.repo, "show", f"HEAD:{path}")
        except GitError:
            return [_finding(f"successor {proposal.stem!r} is not committed", road, path=path)]
        if committed != proposal.ticket:
            return [_finding(f"committed successor {proposal.stem!r} differs from its reviewed proposal",
                             "commit the exact reviewed proposal before recording supersession", path=path)]
    ctx.driver.journal.append(EventType.SIGNAL, {"signal": SUPERSEDES, "successors": list(successors)},
                              ticket=stem, key=None)
    return []
