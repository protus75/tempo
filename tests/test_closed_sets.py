"""Closed sets are invariants (plan 19.I).

Every closed vocabulary, enumerated set, and shipped constant the engine defines is
pinned here by exact value: a renamed, added, or dropped member, or a changed shipped
constant, fails this file. Values were taken from the merged code; changing one is a
plan change first (the owning section names the set), then a deliberate edit here.
"""
import typing
from typing import get_args

import pytest

from tempo import (artifacts, audit, author, backlog, box, call, caps, checkpoint, config, control, enginelog, gates, hooks, hosts, inbox, jobs, journal, llm, lockfile, merge, mergequeue, notify, providers, requisition, runner, serve, specs, stages, storm, thresh, tickets, tools, triage)


def ident(v):
    """A stable name for a registered object: a bound validator's model, a named record, a class or instance."""
    if hasattr(v, "__self__") and hasattr(v.__self__, "__name__"):
        return v.__self__.__name__
    if isinstance(getattr(v, "name", None), str):
        return v.name
    return getattr(v, "__name__", None) or type(v).__name__


def get_args_deep(t):
    """Members of a Literal alias, flattening unions of Literals."""
    if typing.get_origin(t) is typing.Literal:
        return list(get_args(t))
    return [a for part in get_args(t) for a in get_args_deep(part)]


def test_artifacts_closed_sets_and_constants():
    assert artifacts.ARTIFACT_SCHEMA_VERSION == 1
    assert artifacts.DIAGNOSIS_LESSON_CHARS == 300
    assert artifacts.DIAGNOSIS_MAX_LESSONS == 5
    assert artifacts.DIAGNOSIS_VERDICTS == ('retry', 'escalate', 'split', 'reject', 'abandon-human')
    assert set(get_args_deep(artifacts.DiagnosisVerdictName)) == {'abandon-human', 'escalate', 'reject', 'retry', 'split'}
    assert artifacts.OUTCOMES == frozenset({'already_satisfied', 'gate_failed', 'infra_error', 'invalid_artifact', 'ok', 'premise_failed', 'timeout'})
    assert set(get_args_deep(artifacts.Outcome)) == {'already_satisfied', 'gate_failed', 'infra_error', 'invalid_artifact', 'ok', 'premise_failed', 'timeout'}
    assert artifacts.REVIEW_SNAG_CODES == frozenset({'acceptance', 'leak', 'logic', 'scope'})
    assert set(get_args_deep(artifacts.ReviewVerdictName)) == {'approve', 'rma', 'snag'}

def test_audit_closed_sets_and_constants():
    assert audit.INVARIANTS == ('one_terminal_per_run', 'declared_cap', 'merged_effects_paired', 'merged_carries_commit', 'closed_run_states', 'segment_ts_monotonic')

def test_author_closed_sets_and_constants():
    assert author.AUTHOR_STUCK_S == 900.0

def test_box_closed_sets_and_constants():
    assert box.MESSAGE_CLASSES == ('suggestion', 'failure_report', 'override_report', 'retro_finding', 'bug_report')
    assert set(get_args_deep(box.MessageClass)) == {'bug_report', 'failure_report', 'override_report', 'retro_finding', 'suggestion'}

def test_call_closed_sets_and_constants():
    assert call.READ_PART_CHARS == 20_000
    assert call.RETRO_STUCK_S == 5400.0
    assert call.SIDE_STATE == ('session.json', 'hooks', 'jobs', 'engine.log', 'engine.log.1', 'engine.log.2',
                               'engine.log.3')
    assert call.CALL_CLOSED == 'subagent_call_closed'
    assert call.OWN_FILES == ('output.txt', 'stop.txt', 'call.json')
    assert call.SURFACES == ('implement', 'review', 'requisition_review', 'triage', 'author', 'retro')
    assert call.CODEX_EFFORT == {'low': 'low', 'medium': 'medium', 'high': 'high', 'max': 'max'}
    assert call.AUTH_MARKERS == ('invalid api key', 'please run /login', 'not logged in', 'oauth token has expired', '401 unauthorized', 'token has expired')
    assert call.DROUGHT == frozenset({'quota_exhausted', 'rate_limited', 'outage'})
    assert set(get_args_deep(config.Surface)) == set(call.SURFACES)
    assert set(get_args_deep(config.HostName)) == {'claude', 'codex', 'copilot'}

def test_session_and_tool_constants():
    assert lockfile.LOCK_STALE_S == 900.0
    assert lockfile.SESSION_FILE == 'session.json'
    assert tools.TOOL_CALL_S == 90.0
    assert tools.FLOW_SURFACES == ('requisition_review', 'implement', 'review')
    assert tuple(tools.TOOLS) == ('begin_session', 'status', 'list_eligible', 'read_ticket', 'read_attempts', 'read_box', 'start', 'prepare', 'submit', 'check', 'merge', 'job', 'write_handoff')
    assert jobs.KINDS == ('check', 'merge', 'submit')
    assert jobs.FINISHED == frozenset({'done', 'lost'})
    assert hosts.HOSTS == ('claude', 'copilot', 'codex')
    assert set(hosts.EVENTS) == {'PreToolUse', 'PostToolUse', 'SubagentStart', 'SubagentStop'}
    assert hooks.READ_COMMANDS == frozenset({'cat', 'sed', 'head', 'tail', 'rg', 'grep'})
    assert hooks.DENIED_BINARIES == frozenset({'git', 'gh', 'tempo', 'claude', 'codex', 'copilot', 'gemini', 'aider', 'cursor-agent', 'opencode', 'amp'})

def test_caps_closed_sets_and_constants():
    assert caps.CAPS == ('decisions', 'retry', 'infra', 'premise_bounce')
    assert caps.LEVELS == ('low', 'medium', 'high', 'max')

def test_checkpoint_closed_sets_and_constants():
    assert checkpoint.CHECKPOINT_MERGES == 5

def test_config_closed_sets_and_constants():
    assert dict(config.BOX_POLICY_DEFAULTS) == {'bug_report_player_no_repro': 'draft', 'bug_report_player_repro': 'confirmed', 'bug_report_self_diagnosed': 'confirmed', 'failure_report': 'confirmed', 'override_report': 'draft', 'retro_finding': 'confirmed', 'suggestion': 'draft'}
    assert set(get_args_deep(config.BoxRow)) == {'bug_report_player_no_repro', 'bug_report_player_repro', 'bug_report_self_diagnosed', 'failure_report', 'override_report', 'retro_finding', 'suggestion'}
    assert config.SCHEMA_VERSION == 1
    assert set(get_args_deep(config.Severity)) == {'hard', 'soft'}
    assert set(get_args_deep(config.StartState)) == {'confirmed', 'draft'}
    assert set(get_args_deep(config.Tier)) == {'high', 'low', 'max', 'medium'}

def test_control_closed_sets_and_constants():
    assert set(get_args_deep(control.Verb)) == {'kill', 'pause', 'resume'}

def test_backlog_closed_sets_and_constants():
    assert dict(backlog.PRIORITY) == {'P0': 0, 'P1': 1, 'P2': 2, 'P3': 3}
    assert backlog.RETIRED == frozenset({'rejected'})
    assert backlog.SETTLED == frozenset({'already_satisfied', 'merged'})

def test_enginelog_closed_sets_and_constants():
    assert enginelog.BACKUPS == 3
    assert enginelog.MAX_BYTES == 16777216

def test_gates_closed_sets_and_constants():
    assert dict(gates.DEFAULT_GATE_SEVERITY) == {'bug_evidence': 'hard', 'core_drift': 'hard', 'correctness_review': 'hard', 'diff_budget': 'hard', 'post_rebase_regate': 'hard', 'requisition_review': 'hard', 'run_record': 'hard', 'scope_fence': 'hard', 'ticket_schema': 'hard', 'verification': 'hard'}
    assert gates.ENGINE_GATE_CODES == frozenset({'bug_evidence', 'core_drift', 'correctness_review', 'diff_budget', 'post_rebase_regate', 'requisition_review', 'run_record', 'scope_fence', 'ticket_schema', 'verification'})

def test_inbox_closed_sets_and_constants():
    assert dict(inbox.CAPS) == {'log': 65536, 'replay': 1048576}

def test_journal_closed_sets_and_constants():
    assert set(get_args_deep(journal.Dispatch)) == {'escalate', 'reject_queue', 'retry'}
    assert dict(journal.EVENT_VERSIONS) == {'cap_consumed': 1, 'checkpoint': 1, 'effect_completion': 1, 'effect_intent': 1, 'signal': 1, 'state_transition': 1, 'timer_armed': 1, 'timer_fired': 1}
    assert {e.value for e in journal.EventType} == {'cap_consumed', 'checkpoint', 'effect_completion', 'effect_intent', 'signal', 'state_transition', 'timer_armed', 'timer_fired'}
    assert journal.ROLL_BYTES == 67108864
    assert journal.SIGNAL_NAMES == frozenset({'author_invoked', 'checkpoint_push_failed', 'control_decision', 'dead_dependency', 'diagnosis', 'draft_confirmed', 'drain_halted', 'flake_detected', 'flake_released', 'handoff_written', 'harvest_failed', 'kill_applied', 'merge_conflict_facts', 'merge_red_streak', 'merge_tree_mismatch', 'provider_call_outcome', 'recovery_alert', 'reject_arrival', 'reject_verdict', 'requisition_verdict', 'retro_failed', 'retro_prompt_spec_change_merged', 'retro_ticket_authored', 'rework_order', 'session_begun', 'storm_breaker_trip', 'storm_occurrence', 'subagent_call_closed', 'supersedes', 'ticket_intake', 'tombstone_auto_reopened', 'triage_pass', 'watcher_parse_failure'})
    assert journal.TERMINAL_STATES == frozenset({'abandoned', 'already_satisfied', 'gate_failed', 'infra_error', 'invalid_artifact', 'merged', 'premise_failed', 'rejected', 'timeout'})

def test_llm_closed_sets_and_constants():
    assert set(get_args_deep(llm.AgentEffort)) == {'high', 'low', 'max', 'medium'}
    assert set(get_args_deep(llm.AgentTier)) == {'high', 'low', 'max', 'medium'}

def test_merge_closed_sets_and_constants():
    assert tuple(ident(g) for g in merge.MERGE_GATES) == ('TicketSchemaGate', 'PostRebaseGate', 'ApprovalGate')
    assert merge.MERGE_SPEC_VERSION == 1

def test_mergequeue_closed_sets_and_constants():
    assert mergequeue.RED_STREAK_LIMIT == 3

def test_notify_closed_sets_and_constants():
    assert notify.NOTIFY_RETRY_S == 60.0
    assert notify.NOTIFY_TIMEOUT_S == 30.0

def test_providers_closed_sets_and_constants():
    assert providers.FAILURE_MARKERS == (('quota_exhausted', ('quota exhausted', 'usage limit reached', 'out of extra usage')), ('rate_limited', ('rate limit', 'too many requests')), ('outage', ('service unavailable', 'internal server error', 'stream disconnected', 'at capacity')), ('model_error', ('model unavailable', 'model not found', 'model is not supported')))
    assert set(get_args_deep(providers.FailureClass)) == {'auth_error', 'model_error', 'outage', 'quota_exhausted', 'rate_limited', 'unclassified'}
    assert providers.WRITING_SURFACES == frozenset({'implement'})

def test_requisition_closed_sets_and_constants():
    assert requisition.REQUISITION_STUCK_S == 600.0

def test_runner_closed_sets_and_constants():
    assert runner.EXIT_MERGED == 0
    assert runner.EXIT_REFUSED == 2
    assert runner.EXIT_TICKET == 1
    assert stages.HARVEST_TAIL_CHARS == 4000
    assert runner.IDENTICAL_K == 3

def test_serve_closed_sets_and_constants():
    assert serve.SERVE_POLL_S == 0.1

def test_specs_closed_sets_and_constants():
    assert specs.ENGINE_LLM_SURFACES == frozenset({'author', 'diagnose', 'implement', 'requisition_review', 'retro', 'review', 'rework', 'triage'})
    assert specs.RENDER_BOUND_CHARS == 400000
    assert specs.REQ_RENDER_HEADROOM == 0.75
    assert specs.SPEC_MAX_LINES == 200
    assert specs.SPEC_SECTIONS == ('Role', 'Task', 'Inputs', 'Output format', 'On-failure')

def test_stages_closed_sets_and_constants():
    assert tuple(ident(g) for g in stages.CHECK_GATES) == ('ScopeFenceGate', 'VerificationGate', 'RunRecordGate', 'DiffBudgetGate', 'CoreDriftGate', 'BugEvidenceGate')
    assert stages.CHECK_SPEC_VERSION == 1
    assert stages.DIAGNOSIS_STUCK_S == 600.0
    assert stages.DIFF_BUDGET_FILES == 30
    assert stages.DIFF_BUDGET_INSERTED == 1500
    assert {k: ident(x) for k, x in stages.EMITS_BY_VERDICT.items()} == {'approve': 'ApprovedInvoice', 'rma': 'Rma', 'snag': 'SnagList'}
    assert stages.OUTPUT_TAIL_CHARS == 2000
    assert stages.RUN_RECORD_SECTIONS == ('Outcome', 'Surprises / judgment calls', 'Dead ends', 'Second problems filed', 'Resolved engine/model')
    assert set(get_args_deep(stages.StageName)) == {'check', 'implement', 'review'}

def test_storm_closed_sets_and_constants():
    assert storm.STORM_THRESHOLD == 5

def test_thresh_closed_sets_and_constants():
    assert thresh.PROVIDER_CALL_OUTCOME == 'provider_call_outcome'
    assert thresh._COUNTED == frozenset({'outage', 'unclassified'})

def test_tickets_closed_sets_and_constants():
    assert set(get_args_deep(tickets.Level)) == {'high', 'low', 'max', 'medium'}
    assert tickets.OPTIONAL_SECTIONS == ('On-demand', 'Regression')
    assert tickets.REQUIRED_SECTIONS == ('Depends on', 'Context', 'Goal / Why', 'Scope in / Scope out', 'Scope fence', 'Acceptance criteria', 'Verification', 'Definition of rejected', 'Time budget')
    assert tickets.RESERVED_STEMS == frozenset({'decisions', 'retro'})
    assert tickets.SECTIONS == ('Depends on', 'Context', 'Goal / Why', 'Scope in / Scope out', 'Scope fence', 'Acceptance criteria', 'Verification', 'Definition of rejected', 'Time budget', 'On-demand', 'Regression')

def test_triage_closed_sets_and_constants():
    assert triage.TRIAGE_STUCK_S == 600.0

# --- The seams refuse what the sets exclude -------------------------------------
from datetime import UTC, datetime  # noqa: E402

from pydantic import TypeAdapter, ValidationError  # noqa: E402

from tempo.box import Message  # noqa: E402
from tempo.journal import EventType, Journal  # noqa: E402
from tempo.tickets import Frontmatter  # noqa: E402

LEVELS = {"low", "medium", "high", "max"}
FRONTMATTER_VOCABULARIES = {
    "state": {"draft", "confirmed", "rejected", "merged"},
    "priority": {"P0", "P1", "P2", "P3"},
    "kind": {"bug", "feature", "chore"},
    "agent_tier": LEVELS,
    "agent_effort": LEVELS,
}
VALID_FRONTMATTER = {"state": "confirmed", "source": "human", "priority": "P1", "kind": "bug"}


def _clock():
    return datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def test_frontmatter_field_vocabularies_are_closed():
    assert set(Frontmatter.model_fields) == {"state", "source", "priority", "kind", "agent_tier",
                                             "agent_effort", "gate_bypass"}
    for field, members in FRONTMATTER_VOCABULARIES.items():
        assert set(get_args_deep(Frontmatter.model_fields[field].annotation)) == members, field


@pytest.mark.parametrize("field", sorted(FRONTMATTER_VOCABULARIES))
def test_frontmatter_refuses_a_value_outside_its_vocabulary(field):
    Frontmatter.model_validate(VALID_FRONTMATTER)
    with pytest.raises(ValidationError):
        Frontmatter.model_validate({**VALID_FRONTMATTER, field: "not_a_member"})


def test_frontmatter_refuses_an_undeclared_key():
    with pytest.raises(ValidationError):
        Frontmatter.model_validate({**VALID_FRONTMATTER, "owner": "someone"})


@pytest.mark.parametrize("name", sorted(journal.SIGNAL_NAMES))
def test_journal_accepts_every_listed_signal_name(tmp_path, name):
    Journal(tmp_path, _clock).append(EventType.SIGNAL, {"signal": name})


def test_journal_refuses_an_unlisted_signal_name(tmp_path):
    with pytest.raises(ValueError, match="SIGNAL_NAMES"):
        Journal(tmp_path, _clock).append(EventType.SIGNAL, {"signal": "not_a_signal"})


def test_journal_refuses_an_unlisted_event_type(tmp_path):
    with pytest.raises(ValueError):
        Journal(tmp_path, _clock).append("not_an_event_type", {})


def test_box_message_class_and_status_are_closed_at_the_record():
    message_class = TypeAdapter(Message.model_fields["message_class"].annotation)
    for member in box.MESSAGE_CLASSES:
        message_class.validate_python(member)
    with pytest.raises(ValidationError):
        message_class.validate_python("not_a_class")
    assert set(get_args_deep(Message.model_fields["status"].annotation)) == {"pending", "resolved"}
