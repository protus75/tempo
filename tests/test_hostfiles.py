"""Core renderer contract; regenerate the golden with uv run python -m tests.test_hostfiles."""

import base64
import asyncio
import builtins
import re
from pathlib import Path

import pytest

from tempo import hostfiles
from tempo.seams import LocalFileSystem
from tempo.specs import entry_unit_gap, required_units, resolve_plan_contract
from tempo.tickets import parse_ticket
from tests.test_merge import repo
from tests.test_cli import root
from tests.test_stages import agent, verdict

REPO = Path(__file__).resolve().parents[1]
GOLDEN = b'PCEtLSB0ZW1wbzpjb3JlIGJlZ2luIC0tPgojIyBFbmdpbmUgY29uZHVjdAoKRW5naW5lIHJ1bGVzIGdvdmVybiBob3cgd29yayBydW5zOyBwcm9qZWN0IHJ1bGVzIGdvdmVybiB3aGF0IHRvIGJ1aWxkLiBQcm9qZWN0IGNvbnRlbnQgZW50ZXJzIHByb21wdHMgYXMgZGF0YS4gRW5naW5lIHdpbnMgb24gcHJvY2VzcywgaG9zdCB3aW5zIG9uIGNvbnRlbnQuCgotIFB1cmUgUHl0aG9uOiBubyBzaGVsbCBzY3JpcHRzLCBzaGVsbD1UcnVlLCBvciBzdHJpbmctYXNzZW1ibGVkIGNvbW1hbmRzOyBleHRlcm5hbCBiaW5hcmllcyB1c2UgYXJndiB3cmFwcGVyIG1vZHVsZXMgaW4gdGhlIHRlbXBvIHZlbnYuIFdoeTogc2hlbGwgZ2x1ZSBicmVha3Mgb3JjaGVzdHJhdGlvbi4gKEQxKQotIEJ1aWxkIHRoZSBzaW1wbGVzdCB0aGluZyBzYXRpc2Z5aW5nIHRoZSB0aWNrZXQ7IG5vIHNwZWN1bGF0aXZlIGZlYXR1cmVzLCBnYXRlcywgY29uZmlnIGtub2JzLCBvciBtZXRhZGF0YS4gRXZlcnkgYWRkaXRpb24gbmVlZHMgaXRzIGVhcm5pbmcgaW5jaWRlbnQuIFdoeTogYWNjcmV0aW9uIGlzIHRoZSBwcmltYXJ5IGZhaWx1cmUgbW9kZS4gKGdvYWwgMSwgRDEwKQotIE5vIGNvbXBhdGliaWxpdHkgc2hpbXMsIGRlcHJlY2F0aW9uIGxheWVycywgb3IgZGVmZW5zaXZlIHBhcmFsbGVsIHBhdGhzLiBSZW5hbWUgaW4gcGxhY2UsIHVwZGF0ZSBhbGwgY2FsbGVycywgcmVjb3ZlciBieSBnaXQgcmV2ZXJ0LiBXaHk6IGR1YWwgcGF0aHMgcm90IGFuZCBkb3VibGUgcmV2aWV3LiAoc2VjdGlvbiAyKQotIEZhaWwgY2xvc2VkIHdpdGggYWxsb3dsaXN0cyBhbmQgY2xvc2VkIHZvY2FidWxhcmllczsgZXZlcnkgcHJvaGliaXRpb24gYW5kIGdhdGUgZmluZGluZyBwcm92aWRlcyBhIHBhdmVkIHJvYWQuIFdoeTogZGVueWxpc3RzIG1pc3MgY2FzZXMgYW5kIHJvYWRsZXNzIHJlZnVzYWxzIHN0cmFuZCB3b3JrLiAoc2VjdGlvbnMgMiwgNykKLSBBIGhvbGQgc2hpcHMgd2l0aCBvciBhZnRlciBpdHMgcmVsZWFzZSBzdXJmYWNlOyBuZXZlciBuYW1lIGFuIHVuYnVpbHQgcmVsZWFzZSB2ZXJiLiBUaWNrZXQtY2hhbmdlIHJlbGVhc2VzIGZvbGxvdyBtYWNoaW5lIHRpY2tldC1jaGFuZ2UgbWFjaGluZXJ5LiBXaHk6IHVucmVsZWFzYWJsZSBob2xkcyBwZXJtYW5lbnRseSBzdGFsbCB3b3JrLiAoc2VjdGlvbnMgMiwgMTEpCi0gRmlsZXMgYW5kIGpvdXJuYWwgYXJlIHRydXRoOyBkZXJpdmVkIHZpZXdzIGFyZSBwcm9qZWN0aW9ucywgbmV2ZXIgaGFuZC1lZGl0ZWQgb3IgY2l0ZWQgYXMgYXV0aG9yaXR5LiBXaHk6IHR3byBhdXRob3JpdGllcyBkaXZlcmdlLiAoRDMpCi0gR2VuZXJhdGVkIGZpbGVzIGFyZSByZW5kZXIgdGFyZ2V0cywgbmV2ZXIgd3JpdGUgdGFyZ2V0cy4gQ2hhbmdlIHRoZWlyIG93bmluZyBzb3VyY2UgYW5kIHJlZ2VuZXJhdGU7IG5ldmVyIGhhbmQtZWRpdCBtYW5hZ2VkIGNvbmR1Y3QuIFdoeTogcmVnZW5lcmF0aW9uIG92ZXJ3cml0ZXMgZWRpdHMgYW5kIGV4cG9zZXMgZHJpZnQuIChzZWN0aW9uIDEpCi0gVGhlIHBsYW4gaXMgdGhlIHNlZWQ6IHJlcGFpciBwbGFuIGRlZmVjdHMgdGhlcmUsIHRoZW4gcmVnZW5lcmF0ZSBhZmZlY3RlZCBhcnRpZmFjdHMuIEhhbmQtZWRpdCBvbmx5IGRlZmVjdHMgcHJvdmVuIG91dHNpZGUgdGhlIHBsYW4sIG9yIHVuZGVyIHRoZSBibG9ja2luZy1kZWZlY3QgZmFzdCBwYXRoIGFmdGVyIGNvbW1pdHRpbmcgdGhlIHBsYW4gcmVwYWlyIGZpcnN0LiBXaHk6IHRoZSBzZWVkIG11c3QgcmVwcm9kdWNlIHRoZSBhcnRpZmFjdC4gKHNlY3Rpb25zIDEsIDE5KQotIEFsbCBnaXQgdXNlcyBnaXQucHkgd2l0aCBhcmd2IGxpc3RzIGFuZCBwaW5uZWQgZGlyZWN0b3JpZXMuIFJlbW92ZSB3b3JrdHJlZXMgd2l0aCB3b3JrdHJlZSByZW1vdmUgcGx1cyBwcnVuZTsgbm8gYmFyZSBybSAtcmYsIGdoLCBvciBQUnMgaW4gdGhlIGxvb3AuIFdoeTogbG9jYWwgbWFpbiBpcyB0aGUgYmxlc3NlZCBsaW5lLiAoc2VjdGlvbiAxMCkKLSBGaWxlIHNlY29uZCBwcm9ibGVtcyBpbiB0aGUgU3VnZ2VzdGlvbiBCb3gsIG5ldmVyIGZvbGQgdGhlbSBpbnRvIHRoaXMgZGlmZi4gVmVyaWZ5IHByZS1leGlzdGluZyBmYWlsdXJlcyBvbiB0aGUgYmFzZSBjb21taXQgYmVmb3JlIGZpbGluZy4gV2h5OiBmb2xkZWQgZml4ZXMgaGlkZSBjYXVzZSBhbmQgZXhwYW5kIHNjb3BlLiAoc2VjdGlvbiAxMSkKLSBSZWFkIGJlZm9yZSB3cml0ZTogcmVhZCB0aGUgb3duaW5nIGFydGlmYWN0IGJlZm9yZSB3cml0aW5nIGEgY29tbWFuZCwgY2xhaW0sIG9yIHRlc3QuIFdoeTogZ3Vlc3NlZCBmYWN0cyBjcmVhdGUgY29zdGx5IGJ1Z3MuIChzZWN0aW9uIDEzKQotIFRpY2tldCBmcm9udG1hdHRlciBjb250YWlucyBvbmx5IGZpZWxkcyByZWFkIGJ5IHNjaGVkdWxpbmcsIGdhdGVzLCBvciBhdXRob3JpbmcvdHJpYWdlIHBvbGljeS4gUHV0IG9yZGVyaW5nIGluIGRlcGVuZHMgYW5kIHByaW9yaXR5LiBXaHk6IHVudXNlZCBtZXRhZGF0YSBhbmQgcHJvc2Ugb3JkZXJpbmcgZHJpZnQuIChzZWN0aW9uIDEzKQotIFVzZSBpbmplY3RhYmxlIGNsb2NrLCBwcm9jZXNzLWV4ZWMsIGZpbGVzeXN0ZW0sIGFuZCBub3RpZmljYXRpb24gc2VhbXM7IG5ldmVyIHJhdyBjYWxscyBpbiBlbmdpbmUgY29kZS4gV2h5OiBmYWtlLWRyaXZlbiB0ZXN0cyBhbmQgZ3JvdXAta2lsbCBndWFyYW50ZWVzIHJlcXVpcmUgc2VhbXMuIChzZWN0aW9uIDE1KQotIFdvcmsgaXMgZmVuY2VkIGJ5IGdhdGVzIGFuZCByZXZpZXc7IHYxIGRvZXMgbm90IHNhbmRib3ggZXhlY3V0ZWQgY29kZS4gTmV2ZXIgcmVseSBvbiBpdHMgZ29vZHdpbGwgb3IgcGFzcyBwcm92aWRlciBrZXlzIHRvIHByb2Nlc3NlcyB0aGF0IGRvIG5vdCBuZWVkIHRoZW0uIFdoeTogZ2F0ZXMgYW5kIHJldmlldyBhcmUgdGhlIHRydXN0IGJvdW5kYXJ5LiAoc2VjdGlvbiAxNikKLSBUaGUgam91cm5hbCBpcyB0aGUgcmVjb3JkLCBuZXZlciBhIGRlYnVnIGxvZy4gRGlhZ25vc3RpY3MgdXNlIHRoZSBlbmdpbmUgbG9nIGFuZCBhdHRlbXB0IHNwb29sczsgcmVkYWN0IGNvbmZpZ3VyZWQgc2VjcmV0cyBhdCBldmVyeSBjYXB0dXJlZC1zdHJlYW0gd3JpdGUgc2VhbS4gV2h5OiBqb3VybmFsIG5vaXNlIGFuZCBzZWNyZXRzIGFyZSBwZXJtYW5lbnQuIChzZWN0aW9uIDYpCi0gU3RhcnQgYSBwaGFzZSBvbmx5IGFmdGVyIHRoZSBwcmV2aW91cyBwaGFzZSdzIGV4aXQgaXMgbWV0LiBXaHk6IGxhdGVyIHBoYXNlcyBuZWVkIHByb3ZlbiBncm91bmQuIChzZWN0aW9uIDE5KQo8IS0tIHRlbXBvOmNvcmUgZW5kIC0tPgoK'


class RecordingFS(LocalFileSystem):
    def __init__(self):
        self.writes = []

    def write(self, path, data):
        self.writes.append((path, data))
        super().write(path, data)


def test_renderer_entry_unit():
    plan = (REPO / "TEMPO_PLAN.md").read_text()
    uid = "19.P6.core-renderer"
    ticket = parse_ticket("core-renderer", (REPO / "tickets/core-renderer/ticket.md").read_text(),
                          REPO, plan=plan)
    assert required_units(plan, ticket.stem, ticket.plan_contract) == (uid,)
    assert required_units(plan, "continuation", (uid,)) == (uid,)
    assert resolve_plan_contract(plan, (*ticket.plan_contract, uid)).count(f"### {uid} ") == 1
    assert entry_unit_gap(plan, uid) is None
    for part in ("Owner", "Records", "Observable", "Tests"):
        for replacement in ("", f"- **{part}:**"):
            broken = re.sub(rf"^- \*\*{part}:\*\*.*$", replacement, plan, flags=re.M)
            assert part in entry_unit_gap(broken, uid)


@pytest.mark.parametrize("project", [None, b"", b"# Project\n\nrules\n", b"one\r\ntwo\nthree\r\nfour",
                                    b"no final newline\xff", hostfiles.parse((REPO / "CLAUDE.md").read_bytes())[1][2:],
                                    hostfiles.parse((REPO / "AGENTS.md").read_bytes())[1][2:]])
def test_core_first_adoption_preserves_project_bytes(tmp_path, project):
    path = tmp_path / "CLAUDE.md"
    if project is not None:
        path.write_bytes(project)
    fs = RecordingFS()
    hostfiles.refresh([path], fs)
    result = path.read_bytes()
    block = hostfiles.BEGIN + b"\n" + hostfiles.CORE_TEMPLATE + hostfiles.END
    assert result == block + b"\n\n" + (project or b"")
    assert fs.writes == [(path, result)]
    assert hostfiles.parse(result) == (b"", b"\n\n" + (project or b""))


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b"\r"])
@pytest.mark.parametrize("suffix", [b"", b"\n\nproject\r\ntail without newline\xff", b"\r\nlast\n"])
def test_core_refresh_preserves_remainder(tmp_path, ending, suffix):
    prefix = b"# Identity\r\n\nproject prefix\xff\r\n"
    previous = prefix + hostfiles.BEGIN + ending + b"old rules" + ending + hostfiles.END + suffix
    expected = prefix + hostfiles.BEGIN + b"\n" + hostfiles.CORE_TEMPLATE + hostfiles.END + suffix
    assert hostfiles.parse(previous) == (prefix, suffix)
    assert hostfiles.render(previous) == expected
    path = tmp_path / "AGENTS.md"
    path.write_bytes(previous)
    hostfiles.refresh([path], RecordingFS())
    assert path.read_bytes() == expected


CORRUPT = [
    b"<!-- tempo:core begin --> extra\n" + hostfiles.END,
    b"<!-- tempo:core beg",
    hostfiles.BEGIN + b"\nold\n",
    hostfiles.END,
    hostfiles.END + b"\n" + hostfiles.BEGIN,
    hostfiles.BEGIN + b"\n" + hostfiles.BEGIN + b"\n" + hostfiles.END,
    hostfiles.BEGIN + b"\n" + hostfiles.END + b"\n" + hostfiles.END,
    b"inline " + hostfiles.BEGIN + b"\n" + hostfiles.END,
    hostfiles.BEGIN + b"\nstray tempo:core\n" + hostfiles.END,
    hostfiles.BEGIN + b"\nold\n" + hostfiles.END + b"\nstray tempo:core",
    b"stray tempo:core\n" + hostfiles.BEGIN + b"\n" + hostfiles.END,
    b" <!-- tempo:core begin -->\n" + hostfiles.END,
    hostfiles.BEGIN + b"\n<!-- tempo:core end --",
]


@pytest.mark.parametrize("corrupt", CORRUPT)
@pytest.mark.parametrize("first", [None, b"project\r\nkeep me"])
def test_core_marker_corruption_refuses_without_writes(tmp_path, corrupt, first):
    clean, broken = tmp_path / "CLAUDE.md", tmp_path / "AGENTS.md"
    if first is not None:
        clean.write_bytes(first)
    broken.write_bytes(corrupt)
    fs = RecordingFS()
    with pytest.raises(hostfiles.MarkerCorruption, match="repair.*retain all project content"):
        hostfiles.render(corrupt)
    with pytest.raises(hostfiles.MarkerCorruption, match="retry core"):
        hostfiles.refresh([clean, broken], fs)
    assert fs.writes == []
    assert clean.exists() == (first is not None)
    if first is not None:
        assert clean.read_bytes() == first
    assert broken.read_bytes() == corrupt


def test_core_render_is_byte_idempotent(tmp_path):
    path = tmp_path / "CLAUDE.md"
    project = b"# Project\r\n\nno final newline"
    rendered = hostfiles.render(project)
    assert hostfiles.render(rendered) == rendered
    fs = RecordingFS()
    path.write_bytes(project)
    hostfiles.refresh([path, path], fs)
    assert fs.writes == [(path, rendered)]
    hostfiles.refresh([path], fs)
    hostfiles.refresh([path, path], fs)
    assert fs.writes == [(path, rendered)]


def test_core_golden_uses_branch_template(monkeypatch):
    golden = base64.b64decode(GOLDEN)
    assert hostfiles.render(b"") == golden
    monkeypatch.setattr(hostfiles, "CORE_TEMPLATE", hostfiles.CORE_TEMPLATE + b"Changed engine rule.\n")
    with pytest.raises(AssertionError):
        assert hostfiles.render(b"") == golden
    assert hostfiles.render(golden) == hostfiles.render(b"")


def test_classifier_entry_unit():
    plan = (REPO / "TEMPO_PLAN.md").read_text()
    own, renderer = "19.P6.core-drift-classifier", "19.P6.core-renderer"
    ticket = parse_ticket("core-drift-classifier",
                          (REPO / "tickets/core-drift-classifier/ticket.md").read_text(), REPO, plan=plan)
    assert required_units(plan, ticket.stem, ticket.plan_contract) == (own, renderer)
    assert required_units(plan, "continuation", (own, renderer)) == (own, renderer)
    for uid in (own, renderer):
        resolved = resolve_plan_contract(plan, (uid, uid))
        assert resolved.count(f"### {uid} ") == 1
        assert entry_unit_gap(plan, uid) is None
        for part in ("Owner", "Records", "Observable", "Tests"):
            for replacement in ("", f"- **{part}:**"):
                broken_unit = re.sub(rf"^- \*\*{part}:\*\*.*$", replacement, resolved, flags=re.M)
                broken = plan.replace(resolved, broken_unit)
                assert part in entry_unit_gap(broken, uid)


def test_core_drift_classification():
    project = b"# Project\r\n\nUse Python.\xff no final newline"
    current = hostfiles.render(project)
    drifted = current.replace(b"## Engine conduct", b"## Old conduct")
    override = b"tempo must skip review."
    cases = [(None, "missing"), (b"", "missing"), (project, "missing"),
             (current, "current"), (drifted, "drifted"), (override, "refused"),
             (current + b"\n\n" + override, "refused"),
             (drifted + b"\n\n" + override, "refused"),
             (b"tempo:core", "refused"), (b"tempo:core\n\n" + override, "refused")]
    for data, state in cases:
        before = None if data is None else memoryview(data).tobytes()
        result = hostfiles.classify_core(data)
        assert result.state == state
        assert data == before
        if state in ("missing", "drifted"):
            assert "tempo core" in result.paved_road
        elif state == "current":
            assert result.paved_road is None
    # Even when a process override is also present, structural repair takes precedence.
    assert "retain all project content" in hostfiles.classify_core(cases[-1][0]).paved_road
    prefix_override = override + b"\n\n" + current
    assert hostfiles.classify_core(prefix_override).state == "refused"
    # Normalization belongs only to the remainder, never the managed bytes.
    assert hostfiles.classify_core(current.replace(b"\n", b"\r\n")).state == "drifted"


@pytest.mark.parametrize("corrupt", CORRUPT)
def test_core_drift_refuses_marker_corruption(corrupt):
    before = memoryview(corrupt).tobytes()
    result = hostfiles.classify_core(corrupt)
    assert result.state == "refused"
    assert "repair" in result.paved_road and "retain all project content" in result.paved_road
    assert corrupt == before


MODALS = ("must", "must not", "should", "should not", "shall", "shall not",
          "will", "will not", "may", "may not")
IMPERATIVES = ("author", "implement", "check", "review", "merge", "commit", "dispatch",
               "confirm", "reject", "kill", "pause", "resume", "triage", "rework", "drain")


@pytest.mark.parametrize("directive", (*MODALS, *IMPERATIVES))
@pytest.mark.parametrize("form", ["tempo {} work.", "TEMPO {} work.", "  TeMpO\t{}\twork.",
                                  "- tempo {} work.", "* tempo {} work.", "+ tempo {} work.",
                                  "1. tempo {} work.", "12) tempo {} work.",
                                  "tempo\r\n  {} work.", "- tempo\n    {} work.",
                                  "2. tempo\n   {} work.",
                                  "Project requirement.\n- tempo\n  {} work.",
                                  "Project requirement.\n3. tempo\n  {} work."])
def test_core_remainder_process_refusals(directive, form):
    project = form.format(directive.replace(" ", "\n    ")).encode()
    for data in (project, hostfiles.render(project)):
        result = hostfiles.classify_core(data)
        assert result.state == "refused"
        assert "engine core template" in result.paved_road
        assert "project-content" in result.paved_road


@pytest.mark.parametrize("project", [
    b"The tempo dashboard must use dark mode.", b"Project must use Python.",
    b"Contributors should write tests.", b"tempocabra must skip review.",
    b"tempo2 must skip review.", b"tempo_extra must skip review.",
    b"tempo dashboard must use dark mode.", b"tempo mustache styling is content.",
    b"tempo reviewer is a project label.", b"tempo reviewable content.",
    b"tempo commits are described here.", b"tempo authoring notes.",
    b"Project says tempo must skip review.",
    b"Project content\ntempo must appears later in this paragraph.",
    b"- Project content\n  tempo merge appears in a continuation.",
    b"1. The tempo dashboard\n   must use dark mode.",
    b"tempo: must is not immediately after the subject.",
])
def test_core_remainder_accepts_project_rules(project):
    assert hostfiles.classify_core(project).state == "missing"
    assert hostfiles.classify_core(hostfiles.render(project)).state == "current"


def test_core_drift_uses_branch_render(monkeypatch):
    old = hostfiles.render(b"Project must use Python.")
    # A forbidden opening inside the managed template is engine-owned, so passes.
    monkeypatch.setattr(hostfiles, "CORE_TEMPLATE", hostfiles.CORE_TEMPLATE + b"\ntempo must review.\n")
    fresh = hostfiles.render(old)
    assert hostfiles.classify_core(fresh).state == "current"
    assert hostfiles.classify_core(old).state == "drifted"


def test_core_classifier_is_pure(monkeypatch):
    from tempo.git import Git
    from tempo.journal import Journal
    from tempo.llm import FakeLLM
    from tempo.providers import ProviderLLM
    from tempo.seams import SubprocessExec

    samples = [None, b"Project", hostfiles.render(b"Project"),
               hostfiles.render(b"Project").replace(b"## Engine", b"## Changed"),
               b"tempo must skip review.", *CORRUPT]
    expected = [hostfiles.classify_core(data) for data in samples]
    def forbidden(*args, **kwargs):
        pytest.fail("pure classification invoked an effect or filesystem read")
    with monkeypatch.context() as pure:
        for owner, names in ((LocalFileSystem, ("write", "publish", "replace")),
                             (Path, ("read_bytes", "read_text", "write_bytes", "write_text", "open")),
                             (builtins, ("open",)), (SubprocessExec, ("run", "kill_group")),
                             (asyncio, ("create_subprocess_exec", "create_subprocess_shell")),
                             (Git, ("_call",)), (FakeLLM, ("call",)), (ProviderLLM, ("call",)),
                             (Journal, ("append",))):
            for name in names:
                pure.setattr(owner, name, forbidden)
        for _ in range(3):
            assert [hostfiles.classify_core(data) for data in samples] == expected


@pytest.mark.parametrize("mode", ["inline", "queue"])
def test_core_drift_reachable_from_production_gates(root, monkeypatch, mode):
    from tempo import merge, stages, runner
    from tempo.gates import run_gates
    from tempo.llm import FakeLLM
    from tests.test_cli import ENV, Clock, write, ticket as ticket_text
    from tempo.__main__ import main

    calls, selections = [], []
    classify = hostfiles.classify_core
    def classified(*args, **kwargs):
        calls.append(args[0])
        return classify(*args, **kwargs)
    monkeypatch.setattr(hostfiles, "classify_core", classified)
    def observe(gates, *args, **kwargs):
        gates = tuple(gates)
        selections.append(tuple(g.code for g in gates))
        return run_gates(gates, *args, **kwargs)
    monkeypatch.setattr(stages, "run_gates", observe)
    monkeypatch.setattr(merge, "run_gates", observe)
    import tempo.mergequeue as queue_module
    monkeypatch.setattr(queue_module, "run_gates", observe)
    if mode == "inline":
        write(root, "work", ticket_text())
        llm = FakeLLM([agent({"tempo/thing.py": "ok\n"}), verdict()])
        assert main(["run", "work"], cwd=root, env=ENV, clock=Clock(),
                    pipeline=lambda checkout: runner.bind(checkout, llm)) == 0
    else:
        from tests.test_serve import admission_graph, admission_ticket, start_admission, until, finish
        async def queued():
            rig = await admission_graph(root, monkeypatch, {"work": {"tempo/thing.py": "ok\n"}})
            admission_ticket(root, "work")
            run = await start_admission(rig, monkeypatch)
            try:
                await until(rig, lambda: ("work", "merged") in rig.returns)
                assert rig.owner.queue.ctx is rig.contexts["work"]
            finally:
                await finish(rig, run)
        asyncio.run(queued())
    assert sum("core_drift" in codes for codes in selections) == 2
    assert len(calls) >= 2 and all(hostfiles.parse(data) is not None for data in calls)


def test_core_activation_entry_unit():
    plan = (REPO / "TEMPO_PLAN.md").read_text()
    own = "19.P6.core-drift-activation"
    units = (own, "19.P6.core-renderer", "19.P6.core-drift-classifier")
    ticket = parse_ticket("core-drift-activation", (REPO / "tickets/core-drift-activation/ticket.md").read_text(),
                          REPO, plan=plan)
    assert set(required_units(plan, ticket.stem, ticket.plan_contract)) == set(units)
    for uid in units:
        resolved = resolve_plan_contract(plan, (uid, uid))
        assert resolved.count(f"### {uid} ") == 1
        assert entry_unit_gap(plan, uid) is None
        for part in ("Owner", "Records", "Observable", "Tests"):
            for replacement in ("", f"- **{part}:**"):
                broken = plan.replace(resolved, re.sub(rf"^- \*\*{part}:\*\*.*$", replacement,
                                                       resolved, flags=re.M))
                assert part in entry_unit_gap(broken, uid)


def test_core_activation_first_adoption():
    for name in ("CLAUDE.md", "AGENTS.md"):
        adopted = (REPO / name).read_bytes()
        prefix, suffix = hostfiles.parse(adopted)
        original = suffix[2:]
        assert not prefix and b"tempo:core" not in original and original.startswith(b"# tempo")
        assert hostfiles.classify_core(original).state == "missing"
        assert hostfiles.render(original) == adopted
        assert hostfiles.classify_core(adopted).state == "current"
        assert hostfiles.render(adopted) == adopted


if __name__ == "__main__":
    path = Path(__file__)
    generated = base64.b64encode(hostfiles.render(b""))
    path.write_text(re.sub(r"^GOLDEN = .*$", lambda _: f"GOLDEN = {generated!r}",
                           path.read_text(), flags=re.M))


@pytest.mark.asyncio
@pytest.mark.parametrize('dirty', [False, True])
async def test_committed_core_preserves_raw_bytes(repo, dirty):
    from tests.test_merge import context
    from tempo.config import load_config
    from tests.test_stages import evidence
    ctx = context(repo, [])
    raw = hostfiles.render(b'project\xff\r\nno final newline')
    path = repo / 'AGENTS.md'
    path.write_bytes(raw)
    await ctx.git.add(repo, ['AGENTS.md'])
    await ctx.git.commit(repo, 'raw conduct bytes')
    if dirty:
        path.write_bytes(raw + b'dirty')
    gathered = await hostfiles.committed_core(repo, load_config(None, cwd=repo), ctx.git)
    report = hostfiles.CoreDriftGate().check(evidence(**gathered), repo)
    if dirty:
        assert report.verdict == 'fail'
        [finding] = report.findings
        assert finding.path == 'AGENTS.md'
        assert finding.paved_road == 'commit or restore AGENTS.md, then rerun Check'
        assert 'AGENTS.md' not in gathered['core_files']
    else:
        assert report.verdict == 'pass' and gathered['core_files']['AGENTS.md'] == raw


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['CORE_TEMPLATE = (\n', 'CORE_TEMPLATE = bytes([65])\n',
                                   'CORE_TEMPLATE = "not bytes"\n', 'CORE_TEMPLATE = 42\n',
                                   'CORE_TEMPLATE = b"a"\nCORE_TEMPLATE = b"b"\n'])
async def test_committed_core_template_extraction_refuses(repo, source):
    from tempo import stages
    from tests.test_merge import context
    from tests.test_stages import author, STEM, evidence
    from tempo.tickets import validate_ticket
    author(repo)
    ctx = context(repo, [])
    ctx.fs.write(repo / 'tempo/hostfiles.py', source.encode())
    await ctx.git.add(repo, ['tempo/hostfiles.py'])
    await ctx.git.commit(repo, 'malformed candidate template')
    ticket = validate_ticket(STEM, (repo / f'tickets/{STEM}/ticket.md').read_text(), repo)
    await ctx.git.worktree_add(repo, ctx.worktree(STEM), STEM, 'main')
    artifact = await stages.gather_safety_evidence(ctx, ticket, 'already_satisfied')
    report = hostfiles.CoreDriftGate().check(artifact, ctx.worktree(STEM))
    assert report.verdict == 'fail'
    [finding] = report.findings
    assert finding.path == 'tempo/hostfiles.py' and finding.code == 'core_drift'
    assert finding.paved_road == 'define CORE_TEMPLATE as one literal bytes assignment'
