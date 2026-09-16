"""
`ActionResult.failure_code` — the executor's best-effort
`recovery.classify_failure` labelling of an action that didn't work.

Three properties matter and are pinned here: a successful action is never
labelled, a POLICY refusal (a safety gate doing its job) is never labelled as
a recoverable failure, and a real dispatch failure carries a code from the
closed `recovery.FAILURE_CODES` taxonomy so `loop.py` can run
`recovery_policy` on a code rather than parsing prose.
"""

import os

import pytest

os.environ["AUTOMATION_HUMAN_PACING"] = "0"

from playwright.sync_api import Error as PlaywrightError

from automation.agents.autonomous.actions import AgentAction
from automation.agents.autonomous.executor import ActionExecutor
from automation.agents.autonomous.recovery import FAILURE_CODES, recovery_policy


class _Keyboard:
    def __init__(self, error: Exception | None = None):
        self.error = error

    def press(self, key):
        if self.error is not None:
            raise self.error


class FakePage:
    """Minimal page double — only the surface the actions used here touch."""

    def __init__(self, *, press_error: Exception | None = None):
        self.keyboard = _Keyboard(press_error)
        self.url = "https://jobs.example/apply"

    def wait_for_timeout(self, ms):
        pass


def _executor(page):
    return ActionExecutor(page, auto_submit_approved=False, allowed_upload_paths=[])


def test_successful_action_carries_no_failure_code():
    result = _executor(FakePage()).execute(AgentAction(action_type="wait", wait_ms=10))
    assert result.success is True
    assert result.failure_code is None


def test_playwright_timeout_is_classified_as_a_network_error():
    page = FakePage(press_error=PlaywrightError("Timeout 30000ms exceeded"))
    result = _executor(page).execute(AgentAction(action_type="press_key", value="Enter"))
    assert result.success is False
    assert result.failure_code == "NETWORK_ERROR"
    assert result.failure_code in FAILURE_CODES


def test_a_classified_failure_maps_to_a_real_recovery_policy():
    page = FakePage(press_error=PlaywrightError("net::ERR_CONNECTION_RESET"))
    result = _executor(page).execute(AgentAction(action_type="press_key", value="Enter"))
    policy = recovery_policy(result.failure_code, attempts_so_far=0)
    assert policy is not None and policy.action == "RETRY"


def test_policy_refusals_are_not_labelled_as_recoverable_failures():
    """A blocked action is the safety gate working, not a transient fault —
    labelling it would invite the recovery engine to "retry" past a gate."""
    page = FakePage()
    blocked = _executor(page).execute(
        AgentAction(action_type="fill", element_ref=1, value="yes"),
        element_name="Are you legally authorized to work in the US?",
        is_sourced=False,
    )
    assert blocked.blocked_reason == "sensitive_field_requires_human"
    assert blocked.failure_code is None

    refused_upload = _executor(page).execute(
        AgentAction(action_type="upload_file", element_ref=1, file_path="/etc/passwd"),
    )
    assert refused_upload.blocked_reason == "upload_path_not_allowed"
    assert refused_upload.failure_code is None


def test_failure_code_is_serialized_into_the_action_history_record():
    page = FakePage(press_error=PlaywrightError("Timeout 30000ms exceeded"))
    result = _executor(page).execute(AgentAction(action_type="press_key", value="Enter"))
    assert result.as_dict()["failure_code"] == "NETWORK_ERROR"


@pytest.mark.parametrize("unknown_code", ["", "NOT_A_CODE"])
def test_recovery_policy_never_silently_accepts_an_unknown_code(unknown_code):
    """Guard on the contract the executor relies on: an unrecognized code
    escalates to a human rather than being treated as retryable."""
    decision = recovery_policy(unknown_code)
    assert decision.action == "HUMAN"
