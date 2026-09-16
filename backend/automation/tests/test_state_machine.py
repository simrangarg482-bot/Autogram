"""Unit tests for `automation.agents.autonomous.state_machine`."""

from automation.agents.autonomous.state_machine import (
    validate_coarse_transition,
    validate_substate_transition,
)


def test_created_to_analyzing_job_is_allowed():
    assert validate_coarse_transition("CREATED", "ANALYZING_JOB").allowed


def test_running_to_waiting_for_human_is_allowed():
    assert validate_coarse_transition("RUNNING", "WAITING_FOR_HUMAN").allowed


def test_terminal_status_has_no_outbound_transitions():
    result = validate_coarse_transition("COMPLETED", "RUNNING")
    assert not result.allowed
    assert "terminal" in result.reason


def test_created_cannot_jump_straight_to_completed():
    assert not validate_coarse_transition("CREATED", "COMPLETED").allowed


def test_same_status_is_a_no_op():
    assert validate_coarse_transition("RUNNING", "RUNNING").allowed


def test_substate_observe_decide_act_cycle():
    assert validate_substate_transition("OBSERVING", "DECIDING").allowed
    assert validate_substate_transition("DECIDING", "EXECUTING").allowed
    assert validate_substate_transition("EXECUTING", "VERIFYING").allowed
    assert validate_substate_transition("VERIFYING", "OBSERVING").allowed


def test_unknown_submission_state_cannot_go_to_submitted():
    result = validate_substate_transition("UNKNOWN_SUBMISSION_STATE", "SUBMITTED")
    assert not result.allowed


def test_unknown_submission_state_cannot_go_to_ready_for_submission():
    result = validate_substate_transition("UNKNOWN_SUBMISSION_STATE", "READY_FOR_SUBMISSION")
    assert not result.allowed


def test_unknown_submission_state_can_only_start_a_new_task():
    assert validate_substate_transition("UNKNOWN_SUBMISSION_STATE", "CREATED").allowed


def test_terminal_substates_have_no_outbound_transitions():
    for terminal in ("COMPLETED", "FAILED", "CANCELLED"):
        result = validate_substate_transition(terminal, "OBSERVING")
        assert not result.allowed, terminal


def test_unknown_substate_name_is_rejected():
    assert not validate_substate_transition("OBSERVING", "NOT_A_REAL_SUBSTATE").allowed
