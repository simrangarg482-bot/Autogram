"""
Submission safety matrix.

Final submission is the one irreversible action in the product, so it is
gated in three independent places and this file walks the combinations:

  * `validate_action_grounding` — is the submit ref even real, and still
    current? (runs FIRST, in `loop.py`, before any executor exists)
  * `ActionExecutor.execute`    — is submission approved? (the backstop; the
    ONLY thing in the autonomous path that can dispatch a click at all)
  * `state_machine`             — can this task's substate legally become
    SUBMITTED/READY_FOR_SUBMISSION at all?

The two must-have cases are `test_ready_for_submission_decision_can_never_
click_a_submit_control` and `test_an_approved_but_stale_submit_ref_is_
rejected_before_the_executor_gate`; the rest fill in the truth table around
them.
"""

import ast
import inspect
import textwrap
import itertools
import threading

import pytest

from automation.agents.autonomous import executor as executor_module
from automation.agents.autonomous import loop as loop_module
from automation.agents.autonomous.action_semantics import classify_semantic_action
from automation.agents.autonomous.actions import AgentAction, validate_action_grounding
from automation.agents.autonomous.decision import Decision
from automation.agents.autonomous.executor import ActionExecutor
from automation.agents.autonomous.loop import AutonomousAgentLoop, TaskHandle
from automation.agents.autonomous.observer import PageElement, PageState
from automation.agents.autonomous.state_machine import validate_substate_transition
from automation.applications.page_navigator import PageSignature


# --- doubles ---------------------------------------------------------------

class FakeLocator:
    def __init__(self):
        self.clicked = False

    def scroll_into_view_if_needed(self, timeout=None):
        pass

    def click(self, timeout=None, position=None):
        self.clicked = True

    def inner_text(self, timeout=None):
        return "text"

    def evaluate(self, script):
        return "BUTTON"

    def get_attribute(self, name):
        return None


class FakePage:
    def __init__(self):
        self.locators: dict[str, FakeLocator] = {}
        self.url = "https://jobs.example/apply/review"

    def locator(self, selector: str) -> FakeLocator:
        return self.locators.setdefault(selector, FakeLocator())

    def wait_for_timeout(self, ms):
        pass


class ExplodingPage:
    """Any browser interaction at all is a test failure here."""

    url = "https://jobs.example/apply/review"

    def __getattr__(self, name):
        raise AssertionError(f"the browser must not be touched, but page.{name} was accessed")


class FakeDB:
    def refresh(self, obj): pass
    def rollback(self): pass
    def commit(self): pass


class FakeTask:
    def __init__(self, **overrides):
        self.task_id = "submission-matrix"
        self.user_id = "user_test"
        self.job_url = "https://jobs.example/apply/review"
        self.candidate_profile = {"profile": {}}
        self.confirmed_answers = {}
        self.action_history = []
        self.field_attempt_ledger = {}
        self.question_ledger = {}
        self.application_progress = {}
        self.uploaded_documents = []
        self.auto_submit_approved = False
        self.__dict__.update(overrides)


def _button(ref=9, *, name="Submit Application", semantic="SUBMIT", confidence="HIGH"):
    return PageElement(
        ref=ref, tag="button", type="submit", name=name, value=None, required=False,
        disabled=False, checked=None, options=None,
        semantic_action=semantic, action_confidence=confidence,
        irreversible=semantic == "SUBMIT",
    )


def _review_state(elements=None):
    return PageState(
        url="https://jobs.example/apply/review", title="Review your application",
        visible_text="Review your application before submitting",
        elements=list(elements if elements is not None else [_button()]),
        page_type="review",
    )


_REVIEW_SIG = PageSignature(
    url="https://jobs.example/apply/review", title="Review your application",
    heading="Review", step_indicator="Step 3 of 3", controls=("button:submit application",),
)
_MOVED_ON_SIG = PageSignature(
    url="https://jobs.example/apply/review", title="Review your application",
    heading="Please fix 1 error", step_indicator="Step 3 of 3",
    controls=("input:phone", "button:submit application"),
)


def _wired_loop(monkeypatch, *, page, current_signature, last_signature=_REVIEW_SIG):
    from app.services import autonomous_task_repository as task_repo

    loop = AutonomousAgentLoop(
        "submission-matrix",
        TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event(), page=page),
    )
    loop.handle.last_page_signature = last_signature
    recorded = {"actions": [], "audits": [], "paused": [], "ready": []}
    monkeypatch.setattr(loop, "_safe_page_signature", lambda: current_signature)
    monkeypatch.setattr(loop, "_record_audit", lambda db, t, kind, data: recorded["audits"].append((kind, data)))
    monkeypatch.setattr(loop, "_pause_for_human", lambda *a, **k: recorded["paused"].append(a))
    monkeypatch.setattr(loop, "_record_chat_action", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_set_substate", lambda *a, **k: None)
    monkeypatch.setattr(task_repo, "append_action", lambda db, t, entry: recorded["actions"].append(entry))
    monkeypatch.setattr(
        task_repo, "mark_ready_for_approval",
        lambda db, t, evidence: recorded["ready"].append(evidence),
    )
    return loop, recorded


# ---------------------------------------------------------------------------
# MUST-HAVE 1 — an APPLICATION_READY_FOR_SUBMISSION decision cannot click.
# ---------------------------------------------------------------------------

def test_ready_for_submission_decision_carries_no_action_to_execute():
    """`decision.py` only populates `Decision.action` for EXECUTE_ACTION, so
    an APPLICATION_READY_FOR_SUBMISSION decision has nothing for the executor
    to dispatch even if some future caller tried."""
    from automation.agents.autonomous.decision import _parse_response

    decision = _parse_response(
        '{"decision": "APPLICATION_READY_FOR_SUBMISSION", "evidence": "all steps complete",'
        ' "action": {"action_type": "click", "element_ref": 9}}'
    )
    assert decision.decision_type == "APPLICATION_READY_FOR_SUBMISSION"
    assert decision.action is None


def test_ready_for_submission_branch_of_the_loop_never_dispatches_an_action():
    """Structural proof for the branch itself: the source of the
    APPLICATION_READY_FOR_SUBMISSION arm contains no executor construction,
    no `.execute(`, and no `_handle_execute_action` call — it marks the task
    ready for approval and waits for a human."""
    source = inspect.getsource(loop_module.AutonomousAgentLoop._loop_body)
    tree = ast.parse(textwrap.dedent(source))
    branch_source = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = ast.unparse(node.test)
        if "APPLICATION_READY_FOR_SUBMISSION" in test and "decision_type" in test:
            branch_source = "\n".join(ast.unparse(stmt) for stmt in node.body)
            break
    assert branch_source is not None, "the APPLICATION_READY_FOR_SUBMISSION branch moved — re-point this test"
    for forbidden in ("ActionExecutor", ".execute(", "_handle_execute_action", "safe_click"):
        assert forbidden not in branch_source, forbidden
    assert "mark_ready_for_approval" in branch_source
    assert "_wait_for_resume" in branch_source


def test_the_executor_is_the_only_thing_that_can_dispatch_a_click():
    """Defense in depth is only depth if the executor is unconditionally in
    the path. Nothing else in the autonomous package clicks a page: the
    click primitives (`safe_click`, `advance_to_next_page`) are imported by
    `executor.py` alone, and `ActionExecutor.__init__` makes
    `auto_submit_approved` a REQUIRED keyword, so no caller can construct one
    that silently defaults to approved."""
    import pathlib

    package = pathlib.Path(loop_module.__file__).parent
    for path in sorted(package.glob("*.py")):
        if path.name == "executor.py":
            continue
        text = path.read_text()
        for primitive in ("safe_click", "advance_to_next_page"):
            assert f"import {primitive}" not in text and f"{primitive}," not in text, (path.name, primitive)

    parameters = inspect.signature(ActionExecutor.__init__).parameters
    approved = parameters["auto_submit_approved"]
    assert approved.kind is inspect.Parameter.KEYWORD_ONLY
    assert approved.default is inspect.Parameter.empty


def test_every_executor_construction_in_the_loop_passes_the_tasks_real_flag():
    """No `ActionExecutor(...)` anywhere in `loop.py` hardcodes approval: each
    one either forwards `task.auto_submit_approved` or passes a literal
    False (the pre-application navigation executor)."""
    tree = ast.parse(inspect.getsource(loop_module))
    constructions = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "ActionExecutor"
    ]
    assert constructions, "ActionExecutor is no longer constructed in loop.py — re-point this test"
    for call in constructions:
        keywords = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
        assert "auto_submit_approved" in keywords
        assert keywords["auto_submit_approved"] in (
            "bool(task.auto_submit_approved)", "False",
        ), keywords["auto_submit_approved"]


def test_an_unapproved_submit_click_is_refused_however_it_is_labelled():
    """And the runtime backstop under all of the above."""
    for name in ("Submit Application", "Submit", "Finish & Submit", "Complete Application"):
        page = FakePage()
        result = ActionExecutor(page, auto_submit_approved=False).execute(
            AgentAction(action_type="click", element_ref=9),
            element_name=name, element_semantic_action="SUBMIT",
        )
        assert result.blocked_reason == "submit_requires_approval", name
        assert page.locator('[data-agent-ref="9"]').clicked is False, name


# ---------------------------------------------------------------------------
# MUST-HAVE 2 — approval does not rescue a stale submit ref.
# ---------------------------------------------------------------------------

def test_an_approved_but_stale_submit_ref_is_rejected_before_the_executor_gate(monkeypatch):
    """The user approved submission, so `executor.py`'s submit gate WOULD let
    this click through — but the page changed since ref 9 was observed, so
    grounding refuses it first and the loop re-observes. `ExplodingPage`
    makes any executor dispatch an immediate failure rather than a silent
    pass."""
    loop, recorded = _wired_loop(monkeypatch, page=ExplodingPage(), current_signature=_MOVED_ON_SIG)
    task = FakeTask(auto_submit_approved=True)
    decision = Decision(
        decision_type="EXECUTE_ACTION",
        action=AgentAction(action_type="click", element_ref=9),
        reasoning="submit the application",
    )

    keep_going = loop._handle_execute_action(FakeDB(), task, _review_state(), decision)

    assert keep_going is True, "a stale ref must send the loop back to observe, not to a human"
    assert recorded["paused"] == []
    assert recorded["ready"] == [], "a stale ref must not be reinterpreted as 'ready for approval'"
    logged = recorded["actions"][-1]
    assert logged["failure_code"] == "STALE_REFERENCE"
    assert logged["action_attempted"] is False


def test_the_same_approved_submit_ref_on_an_unchanged_page_does_reach_the_executor(monkeypatch):
    """Control for the test above: with the page unchanged, grounding passes
    and the click is dispatched — so the rejection there really is the
    staleness check and not a blanket refusal of submit clicks."""
    page = FakePage()
    loop, recorded = _wired_loop(monkeypatch, page=page, current_signature=_REVIEW_SIG)
    monkeypatch.setattr(loop, "_record_question_ledger", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_record_field_attempt", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_track_verification_streak", lambda *a, **k: None)
    task = FakeTask(auto_submit_approved=True)
    decision = Decision(
        decision_type="EXECUTE_ACTION",
        action=AgentAction(action_type="click", element_ref=9),
        reasoning="submit the application",
    )

    loop._handle_execute_action(FakeDB(), task, _review_state(), decision)

    assert page.locator('[data-agent-ref="9"]').clicked is True
    assert recorded["actions"][-1]["blocked_reason"] is None


def test_an_invented_submit_ref_never_reaches_the_executor_either(monkeypatch):
    """Approval plus a ref that was never observed at all: grounding refuses,
    and this one escalates (it is not a staleness problem)."""
    loop, recorded = _wired_loop(monkeypatch, page=ExplodingPage(), current_signature=_REVIEW_SIG)
    monkeypatch.setattr(loop, "_attempt_vision_assisted_decision", lambda task, state: (None, None))
    decision = Decision(
        decision_type="EXECUTE_ACTION",
        action=AgentAction(action_type="click", element_ref=404),
        reasoning="submit",
    )

    keep_going = loop._handle_execute_action(
        FakeDB(), FakeTask(auto_submit_approved=True), _review_state(), decision,
    )

    assert keep_going is False
    assert recorded["paused"]


# ---------------------------------------------------------------------------
# The matrix itself.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "observed,approved,stale,high_confidence",
    list(itertools.product([True, False], repeat=4)),
)
def test_submit_click_matrix(observed, approved, stale, high_confidence):
    """A submit click is dispatched if and only if EVERY one of: the ref was
    observed, the page has not changed since, and the user approved. The
    semantic-confidence label is deliberately NOT part of that conjunction —
    it is prompt context, and the executor's name-based gate holds whatever
    the classifier thought."""
    element = _button(
        semantic="SUBMIT" if high_confidence else "UNKNOWN",
        confidence="HIGH" if high_confidence else "LOW",
    )
    state = _review_state([element] if observed else [])
    action = AgentAction(action_type="click", element_ref=9)

    grounding = validate_action_grounding(
        action, state,
        expected_signature=_REVIEW_SIG,
        current_signature=_MOVED_ON_SIG if stale else _REVIEW_SIG,
    )
    assert grounding.grounded is (observed and not stale)
    if not grounding.grounded:
        return  # loop.py never builds an executor for an ungrounded action

    page = FakePage()
    result = ActionExecutor(page, auto_submit_approved=approved).execute(
        action, element_name=element.name,
        element_semantic_action=element.semantic_action,
    )
    clicked = page.locator('[data-agent-ref="9"]').clicked
    assert clicked is approved
    assert (result.blocked_reason == "submit_requires_approval") is not approved


def test_a_submit_control_is_never_treated_as_an_intermediate_step_control():
    """`_do_click` routes NEXT/CONTINUE/SAVE_AND_CONTINUE/REVIEW through
    `advance_to_next_page` — a path with no approval gate of its own. A
    submit-labelled control must never be classified into that set, or the
    approval gate could be sidestepped by mislabelling."""
    for name in ("Submit Application", "Finish & Submit", "Review and Submit", "Complete Application"):
        semantic, _, irreversible = classify_semantic_action(
            _button(name=name), page_context="Final review of your job application",
        )
        assert semantic == "SUBMIT", name
        assert irreversible is True, name
        assert semantic not in ("NEXT", "CONTINUE", "SAVE_AND_CONTINUE", "REVIEW")


def test_intermediate_controls_do_not_need_submission_approval():
    """The converse, so the gate cannot be "fixed" by blocking everything:
    Next/Continue/Save & Continue dispatch normally without approval."""
    for name in ("Next", "Continue", "Save and Continue"):
        page = FakePage()
        result = ActionExecutor(page, auto_submit_approved=False).execute(
            AgentAction(action_type="click", element_ref=9), element_name=name,
        )
        assert result.blocked_reason != "submit_requires_approval", name


# ---------------------------------------------------------------------------
# UNKNOWN_SUBMISSION_STATE. Cross-references `test_state_machine.py`, which
# already pins the two individual refusals; this adds the exhaustive form.
# ---------------------------------------------------------------------------

def test_unknown_submission_state_can_only_ever_become_created():
    """Exhaustive counterpart to `test_state_machine.py`'s two targeted cases:
    of every substate in the vocabulary, CREATED is the only legal successor —
    so no decision, however confident, can move an unknown submission outcome
    to SUBMITTED or READY_FOR_SUBMISSION."""
    from automation.agents.autonomous.state_machine import SUBSTATES

    allowed = {
        target for target in SUBSTATES
        if target != "UNKNOWN_SUBMISSION_STATE"
        and validate_substate_transition("UNKNOWN_SUBMISSION_STATE", target).allowed
    }
    assert allowed == {"CREATED"}
    for forbidden in ("SUBMITTED", "READY_FOR_SUBMISSION"):
        check = validate_substate_transition("UNKNOWN_SUBMISSION_STATE", forbidden)
        assert check.allowed is False
        assert forbidden in check.reason


def test_reaching_unknown_submission_state_does_not_unlock_a_second_submit():
    """A task that ended in UNKNOWN_SUBMISSION_STATE has to start over from
    CREATED — it can reach SUBMITTED again only by walking the whole graph,
    never by a direct hop."""
    assert validate_substate_transition("UNKNOWN_SUBMISSION_STATE", "CREATED").allowed is True
    assert validate_substate_transition("SUBMITTED", "UNKNOWN_SUBMISSION_STATE").allowed is True
    assert validate_substate_transition("UNKNOWN_SUBMISSION_STATE", "SUBMITTED").allowed is False
