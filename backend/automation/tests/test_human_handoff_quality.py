"""
Human-handoff quality (spec §9).

A pause is not "NEEDS_HUMAN". The durable `HumanInteractionRequest` the
person actually sees has to answer, from its own record:

  * WHY automation stopped (a reason, not just a category code),
  * WHICH page it stopped on (the classification),
  * WHICH question/field it is about (the real label),
  * WHAT the person has to do next (an action-required sentence).

These walk the two paths the spec calls out — an objective sensitive-field
refusal, and a low-confidence LLM classification — end to end through
`loop.py::_pause_for_human`, asserting on the record that is actually
created rather than on the intervention dict handed in.
"""

import threading

import pytest

from automation.agents.autonomous.actions import ActionResult, AgentAction
from automation.agents.autonomous.decision import Decision, normalize_intervention_type
from automation.agents.autonomous.loop import (
    _REQUEST_TYPE_MESSAGES,
    AutonomousAgentLoop,
    TaskHandle,
)
from automation.agents.autonomous.observer import PageElement, PageState
from automation.agents.autonomous.question_ledger import QuestionLedgerEntry
from automation.applications.page_navigator import PageSignature


SENSITIVE_LABEL = "Are you legally authorized to work in the United States?"


class FakeDB:
    def refresh(self, obj): pass
    def rollback(self): pass
    def commit(self): pass


class FakeTask:
    def __init__(self, **overrides):
        self.task_id = "handoff-quality"
        self.user_id = "user_test"
        self.job_url = "https://jobs.example/apply"
        self.original_objective = "Apply"
        self.current_status = "RUNNING"
        self.candidate_profile = {"profile": {}}
        self.confirmed_answers = {}
        self.action_history = []
        self.field_attempt_ledger = {}
        self.question_ledger = {}
        self.application_progress = {}
        self.uploaded_documents = []
        self.auto_submit_approved = False
        self.__dict__.update(overrides)


class FakeRequest:
    def __init__(self, **kwargs):
        self.request_id = "hreq_test"
        self.expires_at = None
        self.safe_metadata = {}
        self.__dict__.update(kwargs)


class FakeLocator:
    def scroll_into_view_if_needed(self, timeout=None): pass
    def click(self, timeout=None, position=None): raise AssertionError("must not click")
    def fill(self, *a, **k): raise AssertionError("must not fill a sensitive field")
    def inner_text(self, timeout=None): return ""
    def evaluate(self, script): return "INPUT"
    def get_attribute(self, name): return None


class FakePage:
    url = "https://jobs.example/apply"

    def locator(self, selector):
        return FakeLocator()

    def wait_for_timeout(self, ms): pass


_SIG = PageSignature(
    url="https://jobs.example/apply", title="Apply", heading="Eligibility",
    step_indicator="Step 2 of 3", controls=(f"input:{SENSITIVE_LABEL.lower()}",),
)


@pytest.fixture
def captured(monkeypatch):
    """Captures the real `create_request` / `request_human_intervention`
    payloads a pause produces, without a database."""
    from app.services import autonomous_task_repository as task_repo
    from app.services import human_interaction_repository as human_repo
    from automation.agents.autonomous import loop as loop_module

    record = {"requests": [], "snapshots": []}

    def _create_request(db, **kwargs):
        record["requests"].append(kwargs)
        return FakeRequest(safe_metadata=kwargs.get("safe_metadata") or {})

    monkeypatch.setattr(human_repo, "create_request", _create_request)
    monkeypatch.setattr(
        loop_module.chat_repository, "record_agent_message", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        task_repo, "request_human_intervention",
        lambda db, task, intervention: record["snapshots"].append(intervention),
    )
    monkeypatch.setattr(task_repo, "update_progress", lambda db, task, progress: None)
    monkeypatch.setattr(task_repo, "append_action", lambda db, task, entry: None)
    monkeypatch.setattr(task_repo, "record_field_attempt", lambda db, task, ident, entry: None)
    monkeypatch.setattr(task_repo, "record_question_ledger_entry", lambda db, task, key, entry: None)
    monkeypatch.setattr(loop_module, "publish_task_event", lambda *a, **k: None)
    return record


def _loop(monkeypatch, *, page=None):
    loop = AutonomousAgentLoop(
        "handoff-quality",
        TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event(), page=page),
    )
    monkeypatch.setattr(loop, "_record_audit", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_record_chat_milestone", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_record_chat_action", lambda *a, **k: None)
    return loop


def _sensitive_element(ref=4):
    return PageElement(
        ref=ref, tag="input", type="text", name=SENSITIVE_LABEL, value=None,
        required=True, disabled=False, checked=None, options=None,
    )


def _page_state(elements):
    return PageState(
        url="https://jobs.example/apply", title="Apply",
        visible_text="Eligibility questions", elements=list(elements),
        page_type="application_page",
    )


def _text(request: dict) -> str:
    """Everything a human/UI can read off one created request."""
    return f"{request['message']} {request['safe_metadata']}"


# --- 1. the objective sensitive-field refusal ------------------------------

def test_a_sensitive_field_pause_names_the_field_the_reason_and_the_action(captured, monkeypatch):
    """The whole path: an LLM-proposed fill on a sensitive field is refused by
    `executor.py`'s gate, and the pause `loop.py` raises for it carries the
    real question text, why automation could not answer it, and what the
    person must do — never a bare `sensitive_confirmation` code."""
    loop = _loop(monkeypatch, page=FakePage())
    loop.handle.last_page_signature = _SIG
    monkeypatch.setattr(loop, "_safe_page_signature", lambda: _SIG)
    element = _sensitive_element()
    decision = Decision(
        decision_type="EXECUTE_ACTION",
        action=AgentAction(action_type="fill", element_ref=4, value="Yes"),
        reasoning="answer the eligibility question",
    )

    keep_going = loop._handle_execute_action(
        FakeDB(), FakeTask(), _page_state([element]), decision,
    )

    assert keep_going is False
    assert len(captured["requests"]) == 1
    request = captured["requests"][0]

    # WHICH field — the actual label, not "a field".
    assert SENSITIVE_LABEL in request["message"]
    assert request["safe_metadata"]["information_required"] == SENSITIVE_LABEL
    # WHY — a real reason, carried on the durable record and not only in the
    # task's denormalized snapshot column.
    reason = request["safe_metadata"]["reason"]
    assert reason and "sensitive" in reason.lower()
    # WHAT TO DO — an action-required sentence.
    assert "provide the answer" in request["message"].lower()
    assert request["safe_metadata"]["action_required"]
    # And NOT a bare category code.
    assert request["message"].strip() not in ("NEEDS_HUMAN", request["request_type"])


def test_a_sensitive_field_pause_carries_the_page_classification(captured, monkeypatch):
    loop = _loop(monkeypatch, page=FakePage())
    loop.handle.last_page_signature = _SIG
    monkeypatch.setattr(loop, "_safe_page_signature", lambda: _SIG)
    state = _page_state([_sensitive_element()])
    # Exactly what `_loop_body` does each iteration before deciding.
    loop._record_page_visit(_SIG, loop._classify_page(state))

    loop._handle_execute_action(
        FakeDB(), FakeTask(),
        state,
        Decision(
            decision_type="EXECUTE_ACTION",
            action=AgentAction(action_type="fill", element_ref=4, value="Yes"),
            reasoning="answer",
        ),
    )

    page_type = captured["requests"][0]["safe_metadata"]["page_type"]
    assert page_type and page_type != "UNKNOWN"


# --- 2. the low-confidence classification pause ----------------------------

def test_a_low_confidence_llm_pause_keeps_its_own_prose_and_gains_structure(captured, monkeypatch):
    """A `REQUEST_HUMAN_INTERVENTION` the model itself was unsure about
    normalizes to UNKNOWN_BLOCKER (`decision.LOW_CONFIDENCE_THRESHOLD`). The
    model's own message/reason are used as-is — and the record additionally
    carries the reason and an action-required sentence structurally, so a
    model that explained the situation without saying what is needed still
    produces an actionable request."""
    loop = _loop(monkeypatch)
    intervention = {
        "type": "unclear_question",
        "reason": "The page shows a question Autogram could not classify: 'Describe a conflict you resolved'.",
        "message": "Autogram is not sure what this page is asking for.",
        "information_required": "Describe a conflict you resolved",
        "confidence": 0.2,
    }
    assert normalize_intervention_type(intervention) == "UNKNOWN_BLOCKER"

    loop._pause_for_human(FakeDB(), FakeTask(), intervention,
                          safe_metadata={"detection_layer": "llm_uncertain"})

    request = captured["requests"][0]
    assert request["request_type"] == "UNKNOWN_BLOCKER"
    # The LLM's own prose is never replaced.
    assert request["message"] == intervention["message"]
    meta = request["safe_metadata"]
    # ...but the field, the reason and the action are all on the record.
    assert meta["information_required"] == "Describe a conflict you resolved"
    assert meta["reason"] == intervention["reason"]
    assert meta["action_required"] == _REQUEST_TYPE_MESSAGES["UNKNOWN_BLOCKER"]
    assert meta["detection_layer"] == "llm_uncertain"
    assert "needs your help" in meta["action_required"]


def test_a_pause_about_a_field_the_ledger_knows_carries_its_explanation(captured, monkeypatch):
    """`QuestionLedgerEntry.explain()` is the system's own answer to "why did
    Autogram enter this answer?" — a pause about that same field hands it to
    the human instead of making them reconstruct it from action history."""
    loop = _loop(monkeypatch)
    loop.handle.question_ledger.record("f1", QuestionLedgerEntry(
        question_id="f1", page_id="handoff-quality", element_ref=4,
        raw_label=SENSITIVE_LABEL, normalized_category="WORK_AUTHORIZATION",
        field_type="text", answer="Yes", answer_source=None,
        answer_provenance="UNKNOWN", confidence=0.0, sensitivity=True,
        status="PENDING_HUMAN", attempt_count=1, requires_human=True,
    ))

    loop._pause_for_human(FakeDB(), FakeTask(), {
        "type": "sensitive_confirmation",
        "reason": "Refused a sensitive field without a verified source value.",
        "message": f"This application asks a sensitive question ({SENSITIVE_LABEL!r}).",
        "information_required": SENSITIVE_LABEL,
    })

    explanation = captured["requests"][0]["safe_metadata"]["field_explanation"]
    assert SENSITIVE_LABEL in explanation
    assert "requires human review" in explanation


# --- 3. the recovery-engine pause used to drop the field entirely ----------

def test_a_recovery_engine_pause_names_the_field_it_was_working_on(captured, monkeypatch):
    """REGRESSION. `recovery_policy` resolving to HUMAN used to raise a pause
    built ONLY from `_REQUEST_TYPE_MESSAGES` — so a session that expired while
    filling one specific control produced "Sign-in required." and nothing
    else, with `information_required` hardcoded to None, even though the
    element's name was in scope at the call site."""
    loop = _loop(monkeypatch, page=FakePage())
    loop.handle.last_page_signature = _SIG
    monkeypatch.setattr(loop, "_safe_page_signature", lambda: _SIG)
    monkeypatch.setattr(loop, "_track_verification_streak", lambda *a, **k: None)
    element = PageElement(
        ref=7, tag="input", type="text", name="Current employer", value=None,
        required=True, disabled=False, checked=None, options=None,
    )
    # The executor's own classification of what went wrong.
    monkeypatch.setattr(
        loop.handle, "recovery_attempts", {},
    )

    class _Executor:
        def __init__(self, *a, **k): pass

        def execute(self, action, **kwargs):
            return ActionResult(
                False, "fill", "The session expired.", verified=False,
                failure_code="SESSION_EXPIRED",
            )

    monkeypatch.setattr("automation.agents.autonomous.loop.ActionExecutor", _Executor)

    keep_going = loop._handle_execute_action(
        FakeDB(), FakeTask(),
        _page_state([element]),
        Decision(
            decision_type="EXECUTE_ACTION",
            action=AgentAction(action_type="fill", element_ref=7, value="Acme"),
            reasoning="fill employer",
        ),
    )

    assert keep_going is False
    request = captured["requests"][0]
    assert request["request_type"] == "LOGIN_REQUIRED"
    blob = _text(request)
    assert "Current employer" in blob, "the specific field must survive into the handoff record"
    assert request["safe_metadata"]["information_required"] == "Current employer"
    assert request["safe_metadata"]["reason"]
    # The generic per-type prose is KEPT, not replaced by the specific text.
    assert "Sign-in required" in request["message"]


def test_every_request_type_message_is_an_instruction_not_a_code():
    """The fallback copy every pause can degrade to must itself tell the
    person what to do — this is the floor under all of the above."""
    for request_type, message in _REQUEST_TYPE_MESSAGES.items():
        assert len(message) > 40, request_type
        assert request_type.lower() not in message.lower().replace(" ", "_"), request_type
