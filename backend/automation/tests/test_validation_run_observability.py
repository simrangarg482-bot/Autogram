"""
Minimum console-logging coverage for a real-browser validation run (see the
production-hardening spec's Phase 3 §3): a human tailing process output must
be able to follow observe -> decide -> act -> result -> (pause) without
querying the DB audit trail. This does not test NEW behavior — every assertion
here is about a `logger.info(...)` call added alongside existing, unchanged
control flow; the log lines never carry PII, resume text, or full LLM
reasoning/evidence, only structural facts (page_type/url/element counts,
decision/action types, refs, success/verified/blocked flags).
"""

import logging
import threading

from automation.agents.autonomous.actions import AgentAction
from automation.agents.autonomous.decision import Decision
from automation.agents.autonomous.loop import AutonomousAgentLoop, TaskHandle
from automation.agents.autonomous.observer import PageElement, PageState


class FakeLocator:
    def scroll_into_view_if_needed(self, timeout=None):
        pass

    def click(self, timeout=None, position=None):
        pass

    def fill(self, text, timeout=None):
        self._value = text

    def clear(self, timeout=None):
        self._value = ""

    def is_checked(self, timeout=None):
        return False

    def input_value(self, timeout=None):
        return getattr(self, "_value", "")

    def inner_text(self, timeout=None):
        return ""

    def evaluate(self, script):
        return "BUTTON"

    def get_attribute(self, name):
        return None


class FakePage:
    url = "https://jobs.example/apply"

    def locator(self, selector):
        return FakeLocator()

    def wait_for_timeout(self, ms):
        pass


class FakeDB:
    def refresh(self, obj):
        pass

    def rollback(self):
        pass

    def commit(self):
        pass


class FakeTask:
    def __init__(self, **overrides):
        self.task_id = "observability-test"
        self.user_id = "user_test"
        self.job_url = "https://jobs.example/apply"
        self.candidate_profile = {"profile": {}}
        self.confirmed_answers = {}
        self.action_history = []
        self.field_attempt_ledger = {}
        self.question_ledger = {}
        self.application_progress = {}
        self.uploaded_documents = []
        self.auto_submit_approved = False
        self.__dict__.update(overrides)


def _element(ref=1, *, name="Next", semantic=None):
    return PageElement(
        ref=ref, tag="button", type="button", name=name, value=None, required=False,
        disabled=False, checked=None, options=None, semantic_action=semantic,
    )


def _loop():
    return AutonomousAgentLoop(
        "observability-test",
        TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event(), page=FakePage()),
    )


def test_a_dispatched_action_logs_its_type_and_outcome_never_the_value(monkeypatch, caplog):
    from app.services import autonomous_task_repository as task_repo

    loop = _loop()
    monkeypatch.setattr(loop, "_record_audit", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_record_chat_action", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_set_substate", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_safe_page_signature", lambda: None)
    monkeypatch.setattr(loop, "_answer_decision", lambda *a, **k: (True, None, None))
    monkeypatch.setattr(loop, "_record_question_ledger", lambda *a, **k: None)
    monkeypatch.setattr(task_repo, "append_action", lambda *a, **k: None)
    monkeypatch.setattr(task_repo, "record_field_attempt", lambda *a, **k: None)

    match = _element(ref=1, name="Phone Number")
    state = PageState(
        url="https://jobs.example/apply", title="Apply", visible_text="",
        elements=[match], page_type="application_page",
    )
    action = AgentAction(action_type="fill", element_ref=1, value="the-secret-phone-value")
    decision = Decision(decision_type="EXECUTE_ACTION", action=action, reasoning="fill phone")

    with caplog.at_level(logging.INFO, logger="automation.agents.autonomous.loop"):
        loop._handle_execute_action(FakeDB(), FakeTask(), state, decision)

    action_lines = [r.message for r in caplog.records if "action=fill" in r.message]
    assert action_lines, "expected an action-result log line for the dispatched fill"
    assert "success=" in action_lines[0] and "verified=" in action_lines[0]
    assert "the-secret-phone-value" not in action_lines[0], (
        "the field VALUE must never reach process logs, only the fact that an action ran"
    )


def test_a_human_pause_logs_the_request_type_and_id(monkeypatch, caplog):
    from app.services import human_interaction_repository as human_interaction_repo

    class FakeRequest:
        request_id = "req-123"
        expires_at = None
        safe_metadata = {}

    loop = _loop()
    monkeypatch.setattr(loop, "_record_audit", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_record_chat_milestone", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_pause_context", lambda *a, **k: {})
    monkeypatch.setattr(
        human_interaction_repo, "create_request", lambda *a, **k: FakeRequest()
    )
    monkeypatch.setattr(
        "automation.agents.autonomous.loop.publish_task_event", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "automation.agents.autonomous.loop.chat_repository.record_agent_message", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "app.services.autonomous_task_repository.request_human_intervention", lambda *a, **k: None
    )

    with caplog.at_level(logging.INFO, logger="automation.agents.autonomous.loop"):
        loop._pause_for_human(
            FakeDB(), FakeTask(),
            {"type": "otp", "reason": "code required", "message": "Enter the code sent to you.",
             "information_required": None},
        )

    pause_lines = [r.message for r in caplog.records if "PAUSED for human" in r.message]
    assert pause_lines, "expected a console line when the loop pauses for a human"
    assert "OTP_REQUIRED" in pause_lines[0]
    assert "req-123" in pause_lines[0]
