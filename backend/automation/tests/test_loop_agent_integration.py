"""
Integration points wired into `loop.py` by this pass — budgets, the recovery
engine, the question ledger, the truth-store answer policy, the advisory state
machine, and the dynamic page graph.

These exercise the loop's own helper methods against fake DB/task objects
rather than a live browser: the point is the WIRING (does a blown budget reach
the existing human-intervention path? does a sensitive field stop being
"sourced"?), not the already-unit-tested modules behind it.
"""

import threading

import pytest

from automation.agents.autonomous.budgets import AgentBudgets, BudgetTracker
from automation.agents.autonomous.loop import (
    MAX_FIELD_ATTEMPTS,
    MAX_ITERATIONS_PER_RESUME,
    AutonomousAgentLoop,
    TaskHandle,
)
from automation.agents.autonomous.actions import ActionResult, AgentAction
from automation.agents.autonomous.observer import PageElement, PageState, field_identity
from automation.applications.page_navigator import PageSignature


class FakeTask:
    def __init__(self, **overrides):
        self.task_id = "agent_task_test"
        self.user_id = "user_test"
        self.job_url = "https://jobs.example/role/1"
        self.original_objective = "Apply"
        self.current_status = "RUNNING"
        self.candidate_profile = {"profile": {"email": "jane@example.com", "phone": "+1 555 0100"}}
        self.confirmed_answers = {}
        self.action_history = []
        self.field_attempt_ledger = {}
        self.question_ledger = {}
        self.application_progress = {}
        self.uploaded_documents = []
        self.auto_submit_approved = False
        self.__dict__.update(overrides)


class FakeDB:
    def refresh(self, obj):
        pass

    def rollback(self):
        pass

    def commit(self):
        pass


def _loop(**handle_overrides):
    handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event(), **handle_overrides)
    return AutonomousAgentLoop("agent_task_test", handle)


def _silence_audit(loop, monkeypatch):
    monkeypatch.setattr(loop, "_record_audit", lambda *a, **k: None)


def _element(ref=1, *, name="Email", tag="input", type_="text", section=""):
    return PageElement(
        ref=ref, tag=tag, type=type_, name=name, value=None, required=True,
        disabled=False, checked=None, options=None, section=section,
    )


# --- budgets ---------------------------------------------------------------

def test_hard_ceilings_are_not_loosened():
    """The two pre-existing safety ceilings keep their exact values — the new
    budgets are an ADDITIONAL constraint, never a replacement."""
    assert MAX_ITERATIONS_PER_RESUME == 80
    assert MAX_FIELD_ATTEMPTS == 3


def test_unblown_budget_lets_the_run_continue(monkeypatch):
    loop = _loop()
    _silence_audit(loop, monkeypatch)
    assert loop._check_budgets(FakeDB(), FakeTask()) == "CONTINUE"


def test_blown_budget_recommending_human_routes_to_the_existing_pause_path(monkeypatch):
    loop = _loop(budgets=BudgetTracker(AgentBudgets(max_total_actions=2)))
    _silence_audit(loop, monkeypatch)
    paused = []
    monkeypatch.setattr(loop, "_pause_for_human", lambda db, task, intervention, **kw: paused.append(intervention))
    for _ in range(2):
        loop.handle.budgets.record_action("click", succeeded=True, changed_page=True)

    assert loop._check_budgets(FakeDB(), FakeTask()) == "PAUSED"
    assert len(paused) == 1
    assert paused[0]["type"] == "UNKNOWN_BLOCKER"


def test_blown_no_progress_budget_routes_to_the_existing_failure_path(monkeypatch):
    loop = _loop(budgets=BudgetTracker(AgentBudgets(max_no_progress_iterations=2)))
    _silence_audit(loop, monkeypatch)
    failed = []
    monkeypatch.setattr(loop, "_safe_mark_failed", lambda db, task, msg, *, reason: failed.append(reason))
    for _ in range(2):
        loop.handle.budgets.record_action("click", succeeded=False, changed_page=False)

    assert loop._check_budgets(FakeDB(), FakeTask()) == "STOP"
    assert failed == ["budget_max_no_progress_iterations"]


def test_budget_accounting_never_raises_on_a_weird_result(monkeypatch):
    loop = _loop()
    result = ActionResult(True, "click", "ok")
    loop._record_budgeted_action(result, changed_page=True)
    assert loop.handle.budgets.total_actions == 1


# --- recovery engine -------------------------------------------------------

def test_recovery_classifies_a_failed_action_and_never_bypasses_a_captcha():
    loop = _loop()
    result = ActionResult(False, "click", "blocked by a challenge", verified=False,
                          failure_code="CAPTCHA_REQUIRED")
    decision = loop._apply_recovery(result)
    assert decision.action == "HUMAN"
    assert decision.request_type == "CAPTCHA_REQUIRED"


def test_recovery_is_skipped_for_a_policy_refusal():
    loop = _loop()
    blocked = ActionResult(False, "fill", "refused", blocked_reason="sensitive_field_requires_human")
    assert loop._apply_recovery(blocked, blocked_reason_handled=True) is None


def test_recovery_retries_are_bounded_per_failure_code():
    loop = _loop()
    result = ActionResult(False, "click", "network blip", verified=False, failure_code="NETWORK_ERROR")
    actions = [loop._apply_recovery(result).action for _ in range(5)]
    assert actions[0] == "RETRY"
    assert actions[-1] != "RETRY", "an unbounded RETRY loop is exactly what recovery must prevent"


# --- truth store / "sourced" policy ---------------------------------------

def test_a_verified_profile_fact_is_sourced():
    loop = _loop()
    task = FakeTask()
    is_sourced, decision, classification = loop._answer_decision(task, _element(name="Email"), "jane@example.com")
    assert is_sourced is True
    assert decision.autofill_permitted is True
    assert classification.coarse_category == "CONTACT_INFORMATION"


def test_a_sensitive_field_is_never_sourced_without_a_direct_user_answer():
    """Belt and suspenders: even when the legacy substring check would pass
    (the value appears in confirmed_answers), the provenance policy refuses to
    call a sensitive field's value auto-fillable, and `executor.py`'s own gate
    then independently refuses to write it."""
    loop = _loop()
    task = FakeTask(confirmed_answers={"some other question": "Yes"})
    label = "Are you legally authorized to work in the United States?"
    assert loop._value_is_sourced(task, "Yes") is True  # the legacy check alone would allow it
    is_sourced, decision, classification = loop._answer_decision(task, _element(name=label), "Yes")
    assert classification.sensitive is True
    assert decision.requires_human is True
    assert is_sourced is False


def test_answer_policy_degrades_to_the_legacy_check_when_there_is_no_element():
    loop = _loop()
    task = FakeTask()
    is_sourced, decision, classification = loop._answer_decision(task, None, "jane@example.com")
    assert is_sourced is True
    assert decision is None and classification is None


def test_resolved_facts_summary_reports_provenance_only():
    loop = _loop()
    summary = loop._resolved_facts_summary(FakeTask())
    assert summary["email"] == "USER_PROFILE"
    assert "jane@example.com" not in summary.values()


# --- question ledger -------------------------------------------------------

def test_question_ledger_entry_is_recorded_alongside_the_existing_bookkeeping(monkeypatch):
    from app.services import autonomous_task_repository as task_repo

    written = {}
    monkeypatch.setattr(
        task_repo, "record_question_ledger_entry",
        lambda db, task, key, entry: written.__setitem__(key, entry),
    )
    loop = _loop()
    task = FakeTask()
    match = _element(name="Email")
    action = AgentAction(action_type="fill", element_ref=1, value="jane@example.com")
    _, decision, classification = loop._answer_decision(task, match, action.value)

    loop._record_question_ledger(
        FakeDB(), task, match, action,
        ActionResult(True, "fill", "filled", verified=True), decision, classification,
    )

    identity = field_identity(match)
    assert identity in written
    entry = written[identity]
    assert entry["answer_provenance"] == "USER_PROFILE"
    assert entry["status"] == "RESOLVED_AUTO"
    assert entry["normalized_category"] == "CONTACT_INFORMATION"
    assert loop.handle.question_ledger.get(identity).explain().startswith("'Email' was answered")
    # The existing field_attempt_ledger bookkeeping is untouched by this call.
    assert task.field_attempt_ledger == {}


def test_a_blocked_sensitive_field_lands_in_the_ledger_as_pending_human(monkeypatch):
    from app.services import autonomous_task_repository as task_repo

    written = {}
    monkeypatch.setattr(
        task_repo, "record_question_ledger_entry",
        lambda db, task, key, entry: written.__setitem__(key, entry),
    )
    loop = _loop()
    task = FakeTask()
    match = _element(name="Are you legally authorized to work in the United States?")
    action = AgentAction(action_type="fill", element_ref=1, value="Yes")
    _, decision, classification = loop._answer_decision(task, match, action.value)

    loop._record_question_ledger(
        FakeDB(), task, match, action,
        ActionResult(False, "fill", "refused", blocked_reason="sensitive_field_requires_human"),
        decision, classification,
    )

    entry = next(iter(written.values()))
    assert entry["status"] == "PENDING_HUMAN"
    assert entry["requires_human"] is True
    assert entry["sensitivity"] is True
    summary = loop._question_ledger_summary()
    assert summary and summary[0]["category"] == "WORK_AUTHORIZATION"


# --- state machine (advisory only) ----------------------------------------

def test_status_write_is_never_blocked_by_the_advisory_transition_check(monkeypatch):
    from app.services import autonomous_task_repository as task_repo

    calls = []
    monkeypatch.setattr(task_repo, "set_status", lambda db, task, status: calls.append(status))
    loop = _loop()
    task = FakeTask(current_status="COMPLETED")  # a transition the graph forbids outright
    loop._set_status(FakeDB(), task, "RUNNING")
    assert calls == ["RUNNING"], "STRICT_MODE is False — the check logs, it must never block or raise"


def test_unknown_submission_state_can_never_walk_back_into_a_submission_substate():
    loop = _loop()
    loop.handle.substate = "UNKNOWN_SUBMISSION_STATE"
    for forbidden in ("READY_FOR_SUBMISSION", "SUBMITTED", "COMPLETED"):
        loop._set_substate(forbidden)
        assert loop.handle.substate == "UNKNOWN_SUBMISSION_STATE"
    loop._set_substate("CREATED")  # the one legal edge: an entirely new task
    assert loop.handle.substate == "CREATED"


def test_substate_follows_the_normal_cycle():
    loop = _loop()
    for substate in ("CREATED", "NAVIGATING", "OBSERVING", "DECIDING", "EXECUTING", "VERIFYING", "OBSERVING"):
        loop._set_substate(substate)
        assert loop.handle.substate == substate


# --- page graph ------------------------------------------------------------

def test_page_graph_records_visits_and_action_edges():
    loop = _loop()
    first = PageSignature(url="https://jobs.example/apply", title="Apply", heading="Details",
                          step_indicator="1 of 2", controls=("input:email",))
    second = PageSignature(url="https://jobs.example/apply", title="Apply", heading="Work history",
                           step_indicator="2 of 2", controls=("input:employer",))

    loop._record_page_visit(first, loop._classify_page(PageState(url=first.url, title="Apply", visible_text="")))
    loop.handle.page_graph.record_action("click", "Next", succeeded=True)
    loop._record_page_visit(second, None)

    graph = loop.handle.page_graph.as_dict()
    assert len(graph["nodes"]) == 2
    assert len(graph["edges"]) == 1
    assert graph["edges"][0]["action_type"] == "click"


def test_page_graph_bookkeeping_is_a_no_op_without_a_readable_signature():
    loop = _loop()
    loop._record_page_visit(None, None)
    assert loop.handle.page_graph.as_dict()["nodes"] == {}


@pytest.mark.parametrize("attr", ["budgets", "page_graph", "question_ledger"])
def test_task_handle_carries_fresh_per_run_agent_state(attr):
    """Each run gets its own tracker/graph/ledger — never a shared singleton
    accumulating across tasks."""
    assert getattr(_loop().handle, attr) is not getattr(_loop().handle, attr)
