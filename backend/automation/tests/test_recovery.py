"""Unit tests for `automation.agents.autonomous.recovery`."""

from automation.agents.autonomous.recovery import classify_failure, recovery_policy


def test_blocker_hint_classified_as_otp():
    assert classify_failure(blocker_request_type="OTP_REQUIRED") == "OTP_REQUIRED"


def test_blocker_hint_mfa_folds_into_otp_code():
    assert classify_failure(blocker_request_type="MFA_REQUIRED") == "OTP_REQUIRED"


def test_page_error_classified_as_application_error():
    assert classify_failure(page_error_code="HTTP_ERROR") == "APPLICATION_ERROR"


def test_stale_reference_wins_over_element_missing():
    assert classify_failure(element_missing=True, stale_reference=True) == "STALE_REFERENCE"


def test_upload_failure():
    assert classify_failure(upload_failed=True) == "UPLOAD_FAILURE"


def test_network_error_from_exception_text():
    assert classify_failure(exception_message="TimeoutError: net::ERR_CONNECTION_RESET") == "NETWORK_ERROR"


def test_rate_limit_from_exception_text():
    assert classify_failure(exception_message="429 Too Many Requests") == "RATE_LIMIT"


def test_validation_error():
    assert classify_failure(validation_messages=["Please enter a valid email."]) == "VALIDATION_ERROR"


def test_no_progress_after_ceiling():
    assert classify_failure(no_progress_streak=5, no_progress_ceiling=5) == "NO_PROGRESS"


def test_ordinary_success_classifies_as_none():
    assert classify_failure(action_succeeded=True, action_verified=True) is None


def test_otp_policy_always_escalates_to_human_never_retried():
    decision = recovery_policy("OTP_REQUIRED")
    assert decision.action == "HUMAN"
    assert decision.request_type == "OTP_REQUIRED"


def test_captcha_policy_never_bypassed():
    decision = recovery_policy("CAPTCHA_REQUIRED")
    assert decision.action == "HUMAN"
    assert decision.request_type == "CAPTCHA_REQUIRED"


def test_network_error_retries_then_escalates():
    first = recovery_policy("NETWORK_ERROR", attempts_so_far=0)
    assert first.action == "RETRY"
    exhausted = recovery_policy("NETWORK_ERROR", attempts_so_far=3)
    assert exhausted.action == "ESCALATE"


def test_stale_reference_always_reobserves():
    decision = recovery_policy("STALE_REFERENCE", attempts_so_far=0)
    assert decision.action == "REOBSERVE"


def test_no_progress_always_escalates():
    assert recovery_policy("NO_PROGRESS").action == "ESCALATE"


def test_none_code_means_nothing_to_recover():
    assert recovery_policy(None) is None


def test_unrecognized_code_still_escalates_to_human_never_crashes():
    decision = recovery_policy("SOMETHING_NEW")
    assert decision.action == "HUMAN"
    assert decision.request_type == "UNKNOWN_BLOCKER"
