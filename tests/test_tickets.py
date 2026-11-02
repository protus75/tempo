import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tempo.git import Git
from tempo.journal import EventType, Journal
from tempo.seams import LocalFileSystem, SubprocessExec
from tempo.tickets import (
    INTAKE_SIGNAL,
    IntakeRefused,
    TicketInvalid,
    depends_cycle,
    intake,
    validate_ticket,
)

ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/nonexistent",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}

PLAN = """# Plan

## 11. Failure spine

Spine prose.

## 19. Implementation phases

### 19.L Build laws

Laws.

### 19.P2 Phase 2 -- Failure spine

Phase two.

## 22. Appendix: rationale

Never cited.
"""

FRONT = "---\npriority: P2\nkind: feature\n---\n"

BODY = """
## Depends on
none

## Context
- tempo/thing.py

## Plan contract
- section 11

## Goal / Why
`tempo thing` prints ok. Operators need it.

## Scope in / Scope out
In: the verb. Out: everything else.

## Scope fence
- tempo/thing.py
- tests/test_thing.py

## Acceptance criteria
1. `uv run pytest tests/test_thing.py` exits 0.
2. `tempo/thing.py` exposes `thing()`.

## Verification
```
uv run pytest tests/test_thing.py
```

## Definition of rejected
The verb needs a new dependency.

## Time budget
- expected: 20m
- stuck: 40m
"""

VALID = FRONT + BODY


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __call__(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path):
    g = Git(SubprocessExec(), env=ENV, timeout=30.0)
    root = tmp_path / "repo"
    (root / "tempo").mkdir(parents=True)
    (root / "specs").mkdir()
    (root / "tempo" / "thing.py").write_text("")
    (root / "specs" / "review.md").write_text("spec\n")
    (root / "TEMPO_PLAN.md").write_text(PLAN)

    async def seed():
        await g.init(root, branch="main")
        await g.add(root, ["tempo", "specs", "TEMPO_PLAN.md"])
        await g.commit(root, "base")

    run(seed())
    return root, g, Journal(tmp_path / "state", Clock())


def write(root: Path, stem: str, text: str) -> Path:
    path = root / "tickets" / stem / "ticket.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def do_intake(root, g, journal):
    return run(intake(root, g, journal, LocalFileSystem()))


def complete(text: str) -> str:
    """The frontmatter intake would stamp onto a new human stem."""
    return text if "source:" in text else text.replace("---\npriority", "---\nstate: confirmed\nsource: human\npriority")


def refusal(root, text, stem="t-one"):
    write(root, stem, text)
    text = complete(text)
    with pytest.raises(TicketInvalid) as e:
        validate_ticket(stem, text, root)
    return e.value.findings


def messages(findings) -> str:
    return "\n".join(f"{f.message} || {f.paved_road}" for f in findings)


# --- the four named behaviors ----------------------------------------------------------------


def test_valid_ticket_validates_and_commits(repo):
    root, g, journal = repo
    path = write(root, "t-one", VALID)
    ticket = validate_ticket("t-one", VALID.replace("---\npriority", "---\nstate: draft\nsource: human\npriority"),
                             root)
    assert ticket.plan_contract == ("11",)
    assert ticket.verification == (("uv", "run", "pytest", "tests/test_thing.py"),)
    assert (ticket.expected_minutes, ticket.stuck_minutes) == (20, 40)
    assert ticket.frontmatter.agent_tier == ticket.frontmatter.agent_effort == "medium"

    result = do_intake(root, g, journal)

    assert result.committed == ("t-one",) and not result.refused
    assert run(g.status_porcelain(root)) == ""
    text = path.read_text()
    assert "source: human" in text and "state: confirmed" in text and text.endswith(BODY)
    subject = run(g._run(root, "log", "-1", "--format=%s%n%b")).strip()
    assert subject == "tempo(t-one): ticket"
    sha = run(g.rev_parse(root, "HEAD"))
    [event] = [e for e in journal.read() if e.type == EventType.SIGNAL]
    assert event.ticket == "t-one"
    assert event.body == {"signal": INTAKE_SIGNAL, "source": "human", "state": "confirmed", "new": True,
                          "commit": sha}


def test_bad_schema_ticket_is_refused_with_paved_road(repo):
    root, g, journal = repo
    bad = VALID.replace("priority: P2", "priority: urgent\ntags: [ui]")
    path = write(root, "t-bad", bad)

    result = do_intake(root, g, journal)

    assert result.committed == ()
    findings = result.refused["t-bad"]
    assert {f.code for f in findings} == {"ticket_schema"}
    text = messages(findings)
    assert "priority" in text and "tags" in text
    assert all(f.paved_road.strip() for f in findings)
    assert "frontmatter carries only state, source, priority, kind" in findings[0].paved_road
    assert path.read_text() == bad  # untouched, uncommitted
    assert run(g.status_porcelain(root)).startswith("?? tickets/")
    assert journal.read() == []


def test_context_citing_the_plan_is_refused_naming_plan_contract(repo):
    root, g, journal = repo
    findings = refusal(root, VALID.replace("- tempo/thing.py\n\n## Plan", "- tempo/thing.py\n- TEMPO_PLAN.md\n\n## Plan"))
    [f] = findings
    assert "TEMPO_PLAN.md" in f.message
    assert "## Plan contract" in f.paved_road
    assert do_intake(root, g, journal).refused["t-one"] == findings


def test_unresolvable_plan_contract_section_is_refused(repo):
    root, g, journal = repo
    for bad in ("section 99", "section 22", "19.P5", "chapter 3"):
        findings = refusal(root, VALID.replace("- section 11", f"- {bad}"))
        assert len(findings) == 1 and "Plan contract" in findings[0].message, bad
    assert do_intake(root, g, journal).committed == ()


# --- the rest of the section 13 grammar ------------------------------------------------------


@pytest.mark.parametrize("stuck", [19, 20, 21])
def test_time_budget_stuck_must_exceed_expected(repo, stuck):
    root, _, _ = repo
    text = complete(VALID.replace("- stuck: 40m", f"- stuck: {stuck}m"))
    if stuck <= 20:
        [finding] = refusal(root, text)
        assert finding.code == "ticket_schema"
        assert finding.path == "tickets/t-one/ticket.md"
        assert "Time budget" in finding.message
        assert f"stuck: {stuck}m" in finding.message
        assert "expected: 20m" in finding.message
        assert finding.paved_road == "set stuck greater than expected (the stage deadline is the stuck budget)"
    else:
        ticket = validate_ticket("t-one", text, root)
        assert (ticket.expected_minutes, ticket.stuck_minutes) == (20, stuck)


@pytest.mark.parametrize(
    ("old", "new", "needle"),
    [
        ("- tempo/thing.py\n\n## Plan", "- specs/review.md\n\n## Plan", "prompt spec"),
        ("- tempo/thing.py\n\n## Plan", "- tempo/later.py\n\n## Plan", "does not exist"),
        ("- tempo/thing.py\n\n## Plan", "- ../etc/passwd\n\n## Plan", "repo-relative"),
        ("## Depends on\nnone", "## Depends on\n- ghost", "does not resolve"),
        ("## Depends on\nnone", "## Depends on\n- t-one", "self-edge"),
        ("## Definition of rejected\nThe verb needs a new dependency.\n", "", "Definition of rejected"),
        ("## Goal / Why", "## Notes\nhi\n\n## Goal / Why", "unknown body section"),
        ("- expected: 20m\n- stuck: 40m", "- expected: 20 minutes\n- stuck: 40m", "Time budget"),
        ("uv run pytest tests/test_thing.py\n", "uv run pytest 'unterminated\n", "argv-parseable"),
        ("```\nuv run pytest tests/test_thing.py\n```", "`uv run pytest`", "fenced command block"),
        ("exits 0.", "exits 0 and the code is cleaner.", "unmeasurable"),
        ("2. `tempo/thing.py` exposes `thing()`.", "2. The thing works.", "names no check"),
        ("kind: feature", "kind: bug", "no `## Regression`"),
        ("## Time budget", "## Regression\n```\npytest\n```\n- carries: tests/\n\n## Time budget", "non-bug"),
        ("## Time budget", "## Exit-read window\n- nonsense\n\n## Time budget", "Exit-read window"),
        ("kind: feature", "kind: feature\ngate_bypass: [{code: vibes, reason: x}]", "gate_bypass"),
    ],
)
def test_grammar_refusals(repo, old, new, needle):
    root, _, _ = repo
    assert old in VALID
    findings = refusal(root, VALID.replace(old, new))
    assert needle in messages(findings)
    assert all(f.code == "ticket_schema" and f.paved_road for f in findings)


def test_bug_ticket_with_regression_validates(repo):
    root, _, _ = repo
    text = VALID.replace("kind: feature", "kind: bug\nstate: draft\nsource: human").replace(
        "## Time budget", "## Regression\n```\nuv run pytest tests/test_thing.py\n```\n- carries: tests/\n\n## Time budget")
    write(root, "t-one", text)
    ticket = validate_ticket("t-one", text, root)
    assert ticket.frontmatter.kind == "bug"
    assert ticket.regression == ("uv", "run", "pytest", "tests/test_thing.py")
    assert ticket.carries == ("tests/",)


@pytest.mark.parametrize("regression", [
    "```\n```\n- carries: tests/",
    "```\ntrue\nfalse\n```\n- carries: tests/",
    "```\ntrue\n```",
    "```\ntrue\n```\n- carries: ../tests",
    "```\ntrue\n```\n- carries: /tests",
    "```\n\"\"\n```\n- carries: tests/",
])
def test_regression_grammar_is_closed(repo, regression):
    root, _, _ = repo
    text = complete(VALID.replace("kind: feature", "kind: bug").replace(
        "## Time budget", f"## Regression\n{regression}\n\n## Time budget"))
    assert "Regression" in messages(refusal(root, text))


def test_on_demand_must_be_fenced_existing_and_not_context(repo):
    root, _, _ = repo
    base = VALID.replace("## Plan contract", "## On-demand\n- {}\n\n## Plan contract")
    (root / "tempo" / "big.py").write_text("")
    assert "not covered" in messages(refusal(root, base.format("tempo/big.py")))
    fenced = base.replace("- tests/test_thing.py", "- tests/test_thing.py\n- tempo/big.py")
    assert "also in `## Context`" in messages(refusal(root, fenced.format("tempo/thing.py")))
    ok = fenced.format("tempo/big.py").replace("---\npriority", "---\nstate: draft\nsource: human\npriority")
    write(root, "t-one", ok)
    assert validate_ticket("t-one", ok, root).on_demand == ("tempo/big.py",)


def test_seed_must_cite_build_laws_and_phase_unit_never_refused_sections(repo):
    root, _, _ = repo
    seed = VALID.replace("---\npriority", "---\nstate: confirmed\nsource: seed\npriority")
    assert "must cite `19.L`" in messages(refusal(root, seed))
    cited = seed.replace("- section 11", "- 19.L\n- 19.P2\n- section 11")
    write(root, "t-one", cited)
    assert validate_ticket("t-one", cited, root).plan_contract == ("19.L", "19.P2", "11")
    assert "never cites section(s) 19" in messages(refusal(root, cited.replace("- section 11", "- section 19")))


ROW_PLAN = PLAN.replace("## 22. Appendix", """### 19.P3 Phase 3

```yaml
# BEGIN_REGISTRY_P3
phase: 3
admissions:
  - [t-row]
  - [t-exit]
seeds:
  t-row: {cite: ["11.4"], fence: [tempo/thing.py]}
  t-exit: {exit: true, fence: [tickets]}
# END_REGISTRY_P3
```

### 19.P3.t-row Row

- **Owner:** o
- **Records:** r
- **Observable:** b
- **Tests:** t

## 22. Appendix""").replace("Spine prose.", "Spine prose.\n\n### 11.4 Dispatch\n\nDispatch.")


def seed_citing(*ids: str) -> str:
    seed = VALID.replace("---\npriority", "---\nstate: confirmed\nsource: seed\npriority")
    return seed.replace("- section 11", "\n".join(f"- {i}" for i in ids))


def test_row_seed_cites_its_entry_unit_never_its_phase(repo):
    root, _, _ = repo
    (root / "TEMPO_PLAN.md").write_text(ROW_PLAN)
    good = seed_citing("19.I", "19.P3.t-row", "section 11.4")
    write(root, "t-row", good)
    (root / "TEMPO_PLAN.md").write_text(ROW_PLAN.replace("### 19.L Build laws", "### 19.I Impl\n\nI.\n\n### 19.L Build laws"))
    assert validate_ticket("t-row", good, root).plan_contract == ("19.I", "19.P3.t-row", "11.4")
    assert "never cites its phase unit" in messages(refusal(root, seed_citing("19.I", "19.P3", "19.P3.t-row",
                                                                             "section 11.4"), stem="t-row"))
    assert "also cite 11.4" in messages(refusal(root, seed_citing("19.I", "19.P3.t-row"), stem="t-row"))


def test_fence_anchor_names_one_entry_unit_of_the_plan(repo):
    root, _, _ = repo
    anchored = VALID.replace("- tests/test_thing.py", "- tests/test_thing.py\n- TEMPO_PLAN.md#19.P3.t-row")
    write(root, "t-one", anchored)
    assert "TEMPO_PLAN.md#19.P3.t-row" in validate_ticket("t-one", complete(anchored), root).scope_fence
    for bad in ("README.md#19.P3.t-row", "TEMPO_PLAN.md#19.L", "TEMPO_PLAN.md#section 11"):
        assert "anchor" in messages(refusal(root, VALID.replace("- tests/test_thing.py", f"- {bad}")))


def test_reserved_and_malformed_stems_are_refused(repo):
    root, _, _ = repo
    assert "reserved" in messages(refusal(root, VALID, stem="retro"))
    assert "kebab-case" in messages(refusal(root, VALID, stem="Bad_Stem"))


def test_dependency_cycle_is_refused(repo):
    root, g, journal = repo
    write(root, "t-a", VALID)
    assert do_intake(root, g, journal).committed == ("t-a",)
    # t-b depends on t-a, then t-a is edited to depend on t-b: the edit closes a cycle.
    write(root, "t-b", VALID.replace("## Depends on\nnone", "## Depends on\n- t-a"))
    assert do_intake(root, g, journal).committed == ("t-b",)
    a = (root / "tickets" / "t-a" / "ticket.md").read_text().replace("## Depends on\nnone", "## Depends on\n- t-b")
    write(root, "t-a", a)
    result = do_intake(root, g, journal)
    assert "cycle: t-a -> t-b -> t-a" in messages(result.refused["t-a"])
    assert depends_cycle("x", {"x": ["y"], "y": ["z"]}) is None


# --- intake source stamp ---------------------------------------------------------------------


def test_new_stem_claiming_a_machine_source_is_refused(repo):
    root, g, journal = repo
    for source in ("seed", "box:bug_report"):
        write(root, "t-one", VALID.replace("---\npriority", f"---\nsource: {source}\npriority"))
        [f] = do_intake(root, g, journal).refused["t-one"]
        assert "only the machine writes" in f.message and "intake stamps `source: human`" in f.paved_road


def test_new_stem_authored_rejected_is_refused(repo):
    root, g, journal = repo
    write(root, "t-one", VALID.replace("---\npriority", "---\nstate: rejected\npriority"))
    assert "state: rejected" in messages(do_intake(root, g, journal).refused["t-one"])


def test_established_seed_keeps_its_source_across_an_edit(repo):
    root, g, journal = repo
    seed = VALID.replace("---\npriority", "---\nstate: confirmed\nsource: seed\npriority").replace(
        "- section 11", "- 19.L\n- 19.P2")
    write(root, "t-seed", seed)
    run(g.add(root, ["tickets/t-seed/ticket.md"]))
    run(g.commit(root, "conductor seed"))
    edited = seed.replace("Out: everything else.", "Out: everything else, still.")
    path = write(root, "t-seed", edited)

    result = do_intake(root, g, journal)

    assert result.committed == ("t-seed",)
    assert path.read_text() == edited  # never restamped human
    [event] = journal.read()
    assert event.body["source"] == "seed" and event.body["new"] is False


def test_staged_change_outside_ticket_files_refuses_intake(repo):
    root, g, journal = repo
    (root / "tempo" / "thing.py").write_text("x = 1\n")
    run(g.add(root, ["tempo/thing.py"]))
    write(root, "t-one", VALID)
    with pytest.raises(IntakeRefused) as e:
        do_intake(root, g, journal)
    assert "tempo/thing.py" in e.value.finding.message and e.value.finding.paved_road
    assert journal.read() == []


def test_staged_refused_ticket_is_in_no_intake_commit(repo):
    root, g, journal = repo
    write(root, "a-bad", VALID.replace("priority: P2", "priority: urgent"))
    run(g.add(root, ["tickets/a-bad/ticket.md"]))
    write(root, "b-good", VALID)

    result = do_intake(root, g, journal)

    assert "a-bad" in result.refused and result.committed == ("b-good",)
    show = run(g._run(root, "show", "--name-only", "--format=", "HEAD"))
    assert show.split() == ["tickets/b-good/ticket.md"]


def test_nothing_pending_is_a_no_op(repo):
    root, g, journal = repo
    result = do_intake(root, g, journal)
    assert result.committed == () and not result.refused
