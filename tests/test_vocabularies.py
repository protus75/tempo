"""Closed promoted journal vocabularies (TEMPO_PLAN.md section 6.2)."""

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

import pytest

from tempo import journal as journal_module
from tempo.journal import EventType, Journal


def test_dispatch_vocabulary_is_closed():
    Dispatch = journal_module.Dispatch
    dispatch_of = journal_module.dispatch_of
    assert get_args(Dispatch) == ("retry", "escalate", "reject_queue", "park")
    assert dispatch_of({}) is None
    for name in get_args(Dispatch):
        assert dispatch_of({"dispatch": name}) == name
    for value in ("other", None, 1, [], {}):
        with pytest.raises(ValueError, match="dispatch"):
            dispatch_of({"dispatch": value})


def _engine_signal_names():
    root = Path(__file__).resolve().parents[1]
    modules = {".".join(path.relative_to(root).with_suffix("").parts): ast.parse(path.read_text())
               for directory in ("tempo", "eval") for path in (root / directory).rglob("*.py")}
    constants = {}
    for module, tree in modules.items():
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[module, target.id] = node.value.value
    found = set()
    for module, tree in modules.items():
        names = {key[1]: value for key, value in constants.items() if key[0] == module}
        assignments = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if (node.module, alias.name) in constants:
                        names[alias.asname or alias.name] = constants[node.module, alias.name]
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and node.value is not None:
                        assignments.setdefault(target.id, []).append(node.value)

        def literal(node):
            if isinstance(node, ast.Constant):
                return node.value
            if isinstance(node, ast.Name):
                return names.get(node.id)
            return None

        def body_names(node, seen=frozenset()):
            if isinstance(node, ast.Name) and node.id not in seen:
                return set().union(*(body_names(value, seen | {node.id})
                                     for value in assignments.get(node.id, [])))
            pairs = []
            nested = []
            if isinstance(node, ast.Dict):
                pairs = [(literal(key), value) for key, value in zip(node.keys, node.values)
                         if key is not None]
                nested = [value for key, value in zip(node.keys, node.values) if key is None]
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr == "model_dump":
                    return body_names(node.func.value, seen)
                pairs = [(kw.arg, kw.value) for kw in node.keywords]
                nested = [kw.value for kw in node.keywords if kw.arg is None]
                # Typed signal bodies carry their name as a class field's literal default.
                if isinstance(node.func, ast.Name):
                    for cls in tree.body:
                        if isinstance(cls, ast.ClassDef) and cls.name == node.func.id:
                            pairs += [(field.target.id, field.value) for field in cls.body
                                      if isinstance(field, ast.AnnAssign)
                                      and isinstance(field.target, ast.Name)]
            result = {literal(value) for key, value in pairs if key in {"signal", "kind"}
                      and isinstance(literal(value), str)}
            for value in nested:
                result.update(body_names(value, seen))
            return result

        def signal_append(node):
            return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "append" and len(node.args) >= 2
                    and (ast.unparse(node.args[0]) == "EventType.SIGNAL"
                         or literal(node.args[0]) == "signal"))

        # Include helper call bodies as well as direct and locally assembled append bodies.
        helpers = {node.name for node in ast.walk(tree)
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and any(arg.arg == "body" for arg in node.args.args)
                   and any(signal_append(call) for call in ast.walk(node))}
        for node in ast.walk(tree):
            if signal_append(node):
                found.update(body_names(node.args[1]))
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr in helpers and len(node.args) >= 2):
                found.update(body_names(node.args[1]))
    return found


def test_every_engine_signal_name_is_listed():
    # Equality also refuses unused names that would enlarge the write vocabulary.
    assert _engine_signal_names() == journal_module.SIGNAL_NAMES
    assert {"tombstone_auto_reopened", "retro_ticket_authored", "retro_prompt_spec_change_merged"} <= journal_module.SIGNAL_NAMES


@pytest.mark.parametrize("body", [
    {"signal": "spec_gap_hold"}, {},
    {"signal": "handoff_written", "kind": "handoff_written"},
    {"kind": "other"}, {"signal": None}, {"kind": []},
])
def test_unknown_signal_name_is_refused_at_append(tmp_path, body):
    journal = Journal(tmp_path, lambda: datetime(2026, 10, 8, tzinfo=UTC))
    with pytest.raises(ValueError, match="SIGNAL_NAMES"):
        journal.append(EventType.SIGNAL, body)
    assert not journal.dir.exists()
    assert journal.read() == []
    journal.append(EventType.SIGNAL, {"signal": "handoff_written"})
    before = {p: p.read_bytes() for p in journal.dir.iterdir()}
    with pytest.raises(ValueError, match="SIGNAL_NAMES"):
        journal.append(EventType.SIGNAL, body)
    assert {p: p.read_bytes() for p in journal.dir.iterdir()} == before


@pytest.mark.parametrize("field", ["signal", "kind"])
def test_listed_signal_names_are_accepted(tmp_path, field):
    journal = Journal(tmp_path, lambda: datetime(2026, 10, 8, tzinfo=UTC))
    for name in sorted(journal_module.SIGNAL_NAMES):
        journal.append(EventType.SIGNAL, {field: name})
    assert [event.body[field] for event in journal.read()] == sorted(journal_module.SIGNAL_NAMES)
