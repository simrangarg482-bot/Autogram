"""
`automation/applications/application_state.py::ApplicationState` — pure unit
tests, no browser involved. This is bookkeeping only: every method here
records something the flow manager already decided/observed, so these tests
exercise the dataclass directly rather than through a real run.
"""

from automation.applications.application_state import ApplicationState, PageRecord, PENDING_INPUT_REASONS
from automation.ats.base import FieldFillResult


def _filled(key: str) -> FieldFillResult:
    return FieldFillResult(field_key=key, profile_path=key, value_used="x", confidence=0.95, filled=True)


def _unfilled(key: str, reason: str | None) -> FieldFillResult:
    return FieldFillResult(field_key=key, profile_path=key, value_used=None, confidence=0.0, filled=False, unfilled_reason=reason)


def test_record_decision_appends_to_the_timeline_and_advances_current_page():
    state = ApplicationState(application_id="app-1")
    state.record_decision("navigated")
    state.record_decision("step_0_filled", page_number=1)

    assert [d.event for d in state.decisions] == ["navigated", "step_0_filled"]
    assert state.current_page_number == 1


def test_record_page_results_tracks_filled_and_unfilled_fields():
    state = ApplicationState(application_id="app-1")
    state.record_page_results(1, [_filled("first_name"), _unfilled("visa_question", "low_confidence")])

    assert state.fields_processed["first_name"] == "filled"
    assert state.fields_processed["visa_question"] == "unfilled:low_confidence"
    assert state.pages_completed == [PageRecord(page_number=1, fields_seen=2, fields_filled=1)]


def test_only_no_answer_low_confidence_and_missing_required_become_pending_user_input():
    state = ApplicationState(application_id="app-1")
    state.record_page_results(1, [
        _unfilled("q_no_answer", "no_answer_available"),
        _unfilled("q_low_confidence", "low_confidence"),
        _unfilled("q_fill_failed", "fill_failed"),  # a mechanical problem, not a "should a human decide" one
        _unfilled("q_unknown", None),
    ])

    pending_keys = {p.field_key for p in state.pending_user_input}
    assert pending_keys == {"q_no_answer", "q_low_confidence"}
    assert PENDING_INPUT_REASONS == frozenset({"no_answer_available", "low_confidence", "missing_required"})


def test_a_field_later_filled_clears_its_earlier_pending_user_input_entry():
    state = ApplicationState(application_id="app-1")
    state.record_page_results(1, [_unfilled("visa_question", "low_confidence")])
    assert any(p.field_key == "visa_question" for p in state.pending_user_input)

    # Round 2 (or the vision pass, or a later page) manages to answer it.
    state.record_page_results(1, [_filled("visa_question")])

    assert state.fields_processed["visa_question"] == "filled"
    assert not any(p.field_key == "visa_question" for p in state.pending_user_input)


def test_record_validation_errors_replaces_rather_than_accumulates():
    state = ApplicationState(application_id="app-1")
    state.record_validation_errors(["Phone number is invalid"])
    state.record_validation_errors([])  # the next check found the form clean

    assert state.validation_errors == []


def test_record_missing_required_adds_one_pending_entry_per_field_without_duplicates():
    state = ApplicationState(application_id="app-1")
    state.record_missing_required(["work_authorization", "notice_period_days"], page_number=3)
    state.record_missing_required(["work_authorization"], page_number=3)  # re-checked on the same page — not doubled

    reasons = {(p.field_key, p.reason) for p in state.pending_user_input}
    assert reasons == {("work_authorization", "missing_required"), ("notice_period_days", "missing_required")}
    assert len(state.pending_user_input) == 2


def test_snapshot_returns_a_plain_json_ready_dict():
    state = ApplicationState(application_id="app-1")
    state.record_decision("navigated", page_number=1)
    state.record_page_results(1, [_filled("first_name"), _unfilled("visa_question", "low_confidence")])
    state.record_validation_errors(["Phone number is invalid"])

    snapshot = state.snapshot()

    assert snapshot["current_page_number"] == 1
    assert snapshot["pages_completed"] == [{"page_number": 1, "fields_seen": 2, "fields_filled": 1}]
    assert snapshot["fields_processed"]["first_name"] == "filled"
    assert snapshot["pending_user_input"] == [{"field_key": "visa_question", "reason": "low_confidence", "page_number": 1}]
    assert snapshot["validation_errors"] == ["Phone number is invalid"]
    assert snapshot["decisions"][0]["event"] == "navigated"


def test_snapshot_decisions_are_capped_so_a_long_run_never_grows_unbounded():
    state = ApplicationState(application_id="app-1")
    for i in range(120):
        state.record_decision(f"step_{i}")

    assert len(state.snapshot()["decisions"]) == 50
    assert state.snapshot()["decisions"][-1]["event"] == "step_119"
