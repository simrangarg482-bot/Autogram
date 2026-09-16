"""
Adversarial model output — the code decides, never the LLM.

Every test here constructs the payload a COMPROMISED or naively-tricked model
would emit and asserts a code-level gate refuses it. None of these depend on
the model behaving: the decision object is built directly, exactly as
`decision.py::_parse_response` would have built it, and then handed to the
same validation pipeline the real loop uses.

Four gates are covered:
  1. `AgentAction.from_dict`      — the closed action vocabulary.
  2. `validate_action_grounding`  — invented refs and unobserved navigation.
  3. `ActionExecutor.execute`     — the sensitive-field gate.
  4. `ActionExecutor.execute`     — the submit-approval gate.
"""

import json
import threading

import pytest

from automation.agents.autonomous.action_semantics import (
    classify_semantic_action,
    is_submit_control_name,
)
from automation.agents.autonomous.actions import (
    ALLOWED_ACTION_TYPES,
    AgentAction,
    InvalidActionError,
    validate_action_grounding,
)
from automation.agents.autonomous.decision import DecisionError, _parse_response
from automation.agents.autonomous.executor import ActionExecutor
from automation.agents.autonomous.loop import AutonomousAgentLoop, TaskHandle
from automation.agents.autonomous.observer import PageElement, PageState


# --- test doubles ----------------------------------------------------------

class FakeLocator:
    def __init__(self):
        self.filled_with = None
        self.clicked = False
        self.checked_state = None

    def scroll_into_view_if_needed(self, timeout=None):
        pass

    def click(self, timeout=None, position=None):
        self.clicked = True

    def fill(self, value):
        self.filled_with = value

    def select_option(self, label=None, value=None):
        self.filled_with = label or value

    def check(self, timeout=None):
        self.checked_state = True

    def uncheck(self, timeout=None):
        self.checked_state = False

    def inner_text(self, timeout=None):
        return "some text"

    def evaluate(self, script):
        return "INPUT"


class FakePage:
    def __init__(self):
        self.locators: dict[str, FakeLocator] = {}
        self.url = "https://jobs.example/apply"

    def locator(self, selector: str) -> FakeLocator:
        return self.locators.setdefault(selector, FakeLocator())

    def wait_for_timeout(self, ms):
        pass


class FakeTask:
    def __init__(self, **overrides):
        self.task_id = "malicious-decision-test"
        self.user_id = "user_test"
        self.job_url = "https://jobs.example/role/1"
        self.candidate_profile = {"profile": {"email": "jane@example.com"}}
        self.confirmed_answers = {}
        self.auto_submit_approved = False
        self.__dict__.update(overrides)


def _element(ref=1, *, name="Full name", tag="input", type_="text", href=None,
             semantic=None, confidence=None, disabled=False):
    return PageElement(
        ref=ref, tag=tag, type=type_, name=name, value=None, required=False,
        disabled=disabled, checked=None, options=None, href=href,
        semantic_action=semantic, action_confidence=confidence,
    )


def _state(elements=None, url="https://jobs.example/apply"):
    return PageState(
        url=url, title="Apply", visible_text="Apply for this role",
        elements=list(elements if elements is not None else [_element()]),
    )


def _executor(page, *, auto_submit_approved=False):
    return ActionExecutor(page, auto_submit_approved=auto_submit_approved)


def _loop():
    return AutonomousAgentLoop(
        "malicious-decision-test",
        TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event()),
    )


# --- 1. action vocabulary --------------------------------------------------

@pytest.mark.parametrize("action_type", [
    "execute_javascript", "eval", "run_python", "set_cookie", "fetch",
    "screenshot_and_email", "download_file", "open_devtools", "click_xpath",
    "CLICK", "", None, 42, ["click"],
])
def test_action_types_outside_the_vocabulary_are_refused(action_type):
    """Anything not literally in `ALLOWED_ACTION_TYPES` raises — there is no
    best-effort interpretation, no fallback to a "closest" verb."""
    with pytest.raises(InvalidActionError):
        AgentAction.from_dict({"action_type": action_type, "value": "x"})


def test_the_vocabulary_has_no_code_execution_verb():
    assert not any(
        token in verb
        for verb in ALLOWED_ACTION_TYPES
        for token in ("script", "eval", "exec", "code", "selector", "xpath")
    )


def test_a_disallowed_action_inside_a_well_formed_decision_is_still_refused():
    """The rejection is not bypassable by wrapping it in an otherwise-valid
    EXECUTE_ACTION envelope: `_parse_response` funnels through the same
    `AgentAction.from_dict` and re-raises as `DecisionError`."""
    payload = json.dumps({
        "decision": "EXECUTE_ACTION",
        "action": {"action_type": "execute_javascript", "value": "fetch('https://evil.example')"},
        "reasoning": "the page told me to",
    })
    with pytest.raises(DecisionError):
        _parse_response(payload)


def test_a_targeted_action_without_a_ref_and_a_navigate_without_a_url_are_refused():
    with pytest.raises(InvalidActionError):
        AgentAction.from_dict({"action_type": "click"})
    with pytest.raises(InvalidActionError):
        AgentAction.from_dict({"action_type": "navigate"})


# --- 2. grounding: invented element refs -----------------------------------

@pytest.mark.parametrize("invented_ref", [999, -1, "3", "submit-button", 3.5, True])
def test_click_on_a_ref_that_was_never_observed_is_ungrounded(invented_ref):
    """`element_ref` only means anything as an index into what the observer
    actually found. `True` is in this list on purpose: Python would equate it
    to the integer 1, so it must be rejected by identity of the observed set,
    not by truthiness."""
    state = _state([_element(ref=4), _element(ref=7)])
    result = validate_action_grounding(
        AgentAction(action_type="click", element_ref=invented_ref), state,
    )
    assert result.grounded is False
    assert "was not observed" in result.reason


def test_a_disabled_observed_ref_is_also_ungrounded():
    state = _state([_element(ref=4, disabled=True)])
    result = validate_action_grounding(AgentAction(action_type="click", element_ref=4), state)
    assert result.grounded is False
    assert "disabled" in result.reason


# --- 3. grounding: adversarial navigation ----------------------------------

_OBSERVED_HREF = "https://jobs.example/apply/step-2"


@pytest.mark.parametrize("hostile_url", [
    "https://evil.example",
    "https://evil.example/jobs",
    # Look-alikes: the real host appears inside the URL, but never as the host.
    "https://evil.example/jobs?redirect=https://jobs.example/apply/step-2",
    "https://evil.example/https://jobs.example/apply/step-2",
    "https://jobs.example.evil.example/apply/step-2",
    "https://evil.example#https://jobs.example/apply/step-2",
    "https://evil.example/?next=jobs.example",
    "https://jobs.example@evil.example/apply",
    # Non-HTTP schemes never reach a browser navigation at all.
    "javascript:fetch('https://evil.example')",
    "file:///etc/passwd",
    "data:text/html,<script>1</script>",
])
def test_navigation_to_an_unobserved_or_lookalike_domain_is_refused(hostile_url):
    state = _state([_element(ref=2, tag="a", type_="link", name="Continue", href=_OBSERVED_HREF)])
    result = validate_action_grounding(
        AgentAction(action_type="navigate", url=hostile_url), state,
        canonical_urls=("https://jobs.example/role/1",),
    )
    assert result.grounded is False, hostile_url


def test_the_genuinely_observed_href_is_still_allowed():
    """Guard against over-tightening: the real observed link must still pass,
    otherwise the look-alike tests above would be satisfied by a gate that
    refuses everything."""
    state = _state([_element(ref=2, tag="a", type_="link", name="Continue", href=_OBSERVED_HREF)])
    assert validate_action_grounding(
        AgentAction(action_type="navigate", url=_OBSERVED_HREF), state,
    ).grounded is True


def test_a_guessed_application_path_on_the_real_domain_is_refused():
    """Same host, invented path — the spec's "never construct /apply" rule."""
    state = _state([_element(ref=2, tag="a", type_="link", name="Continue", href=_OBSERVED_HREF)])
    assert validate_action_grounding(
        AgentAction(action_type="navigate", url="https://jobs.example/apply/submit-now"), state,
        canonical_urls=("https://jobs.example/role/1",),
    ).grounded is False


# --- 4. sensitive-field gate -----------------------------------------------

_SENSITIVE_LABELS = [
    "Are you authorized to work in the US?",
    "Will you now or in the future require visa sponsorship?",
    "Race / ethnicity",
    "Veteran status",
    "Have you ever been convicted of a felony?",
    "I certify that the information provided is accurate",
    "Date of birth",
]


@pytest.mark.parametrize("label", _SENSITIVE_LABELS)
@pytest.mark.parametrize("action", [
    AgentAction(action_type="fill", element_ref=0, value="Yes"),
    AgentAction(action_type="select", element_ref=0, value="Yes"),
    AgentAction(action_type="check", element_ref=0),
])
def test_guessed_values_on_sensitive_fields_are_refused(label, action):
    page = FakePage()
    result = _executor(page).execute(action, element_name=label, is_sourced=False)
    assert result.success is False
    assert result.blocked_reason == "sensitive_field_requires_human"
    locator = page.locator('[data-agent-ref="0"]')
    assert locator.filled_with is None and locator.checked_state is None


def test_layer_two_the_loop_never_marks_a_guessed_sensitive_value_as_sourced():
    """`is_sourced=True` is not something the model can assert. It is computed
    by `loop._answer_decision`, which requires BOTH the legacy substring check
    against the profile/confirmed answers AND `truth_store.decide()` to permit
    auto-fill. With no verified fact for a sensitive label, it is False — so
    the executor gate above is reached with the refusing value."""
    task = FakeTask(confirmed_answers={})
    element = _element(ref=0, name="Are you authorized to work in the US?")
    is_sourced, decision, classification = _loop()._answer_decision(task, element, "Yes")
    assert classification.sensitive is True
    assert decision.autofill_permitted is False
    assert is_sourced is False
    assert _executor(FakePage()).execute(
        AgentAction(action_type="fill", element_ref=0, value="Yes"),
        element_name=element.name, is_sourced=is_sourced,
    ).blocked_reason == "sensitive_field_requires_human"


def test_a_sensitive_value_the_user_actually_confirmed_is_permitted():
    """The counterpart: the gate is not a blanket refusal, it is a provenance
    requirement. A value the user themselves confirmed for that exact label
    passes both layers."""
    label = "Are you authorized to work in the US?"
    task = FakeTask(confirmed_answers={label: "Yes"})
    is_sourced, _, _ = _loop()._answer_decision(task, _element(ref=0, name=label), "Yes")
    assert is_sourced is True
    assert _executor(FakePage()).execute(
        AgentAction(action_type="fill", element_ref=0, value="Yes"),
        element_name=label, is_sourced=is_sourced,
    ).success is True


def test_a_valueless_tick_on_a_sensitive_acknowledgement_is_not_auto_sourced():
    """REGRESSION (bug fixed in this pass): a `check` carries no value, so the
    legacy substring check ("is this text somewhere in the profile?") was
    vacuously true and handed the executor's sensitive-field gate a free pass
    on exactly the arbitration/consent/certification ticks the spec says need
    the user. The provenance policy now decides valueless actions on its own."""
    task = FakeTask(confirmed_answers={})
    for label in ("I certify that the information provided is accurate",
                  "I agree to the arbitration agreement",
                  "I consent to a background check",
                  "Are you legally authorized to work in the United States?"):
        element = _element(ref=0, name=label, tag="input", type_="checkbox")
        is_sourced, _, _ = _loop()._answer_decision(task, element, None)
        assert is_sourced is False, label
        page = FakePage()
        result = _executor(page).execute(
            AgentAction(action_type="check", element_ref=0),
            element_name=label, is_sourced=is_sourced,
        )
        assert result.blocked_reason == "sensitive_field_requires_human", label
        assert page.locator('[data-agent-ref="0"]').checked_state is None


def test_an_ordinary_valueless_tick_is_unaffected_by_that_fix():
    """The fix must not turn every checkbox into a human handoff: a
    non-sensitive tick is not gated by the executor at all, whatever
    `is_sourced` says."""
    page = FakePage()
    is_sourced, _, _ = _loop()._answer_decision(
        FakeTask(), _element(ref=0, name="Subscribe to job alerts", type_="checkbox"), None,
    )
    result = _executor(page).execute(
        AgentAction(action_type="check", element_ref=0),
        element_name="Subscribe to job alerts", is_sourced=is_sourced,
    )
    assert result.blocked_reason is None
    assert page.locator('[data-agent-ref="0"]').checked_state is True


# --- 5. submit-approval gate -----------------------------------------------

_SUBMIT_PHRASINGS = [
    "Submit Application",
    "Submit",
    "Finish & Submit",
    "Finish and Submit",
    "Complete Application",
    "Submit my application",
    "Review and Submit",
    "Confirm & Submit",
    "Send Application",
    "Finish Application",
    "SUBMIT APPLICATION",
    "I agree — Submit",
]


@pytest.mark.parametrize("label", _SUBMIT_PHRASINGS)
def test_every_submit_phrasing_is_recognized_as_a_submit_control(label):
    """REGRESSION (bug fixed in this pass): the pattern list was anchored with
    `^submit\\b`, so a control labelled "Finish & Submit" or "Review and
    Submit" — word-order variants every ATS ships — was not recognized as a
    submit control at all and sailed past the approval gate."""
    assert is_submit_control_name(label) is True, label


@pytest.mark.parametrize("label", _SUBMIT_PHRASINGS)
def test_submit_click_without_approval_is_refused(label):
    page = FakePage()
    result = _executor(page, auto_submit_approved=False).execute(
        AgentAction(action_type="click", element_ref=0), element_name=label,
    )
    assert result.success is False, label
    assert result.blocked_reason == "submit_requires_approval", label
    assert page.locator('[data-agent-ref="0"]').clicked is False, label


@pytest.mark.parametrize("label", [
    "Next", "Next Step", "Continue", "Save", "Save and Continue", "Save & Continue",
    "Apply Now", "Start Application", "Upload Resume", "Add another employer",
    "Back", "Review your answers", "Sign in",
])
def test_intermediate_controls_are_not_caught_by_the_submit_gate(label):
    """The other half of the fix: widening the submit pattern must not start
    blocking ordinary multi-step navigation, which would stall every
    application at step 1."""
    assert is_submit_control_name(label) is False, label
    page = FakePage()
    result = _executor(page, auto_submit_approved=False).execute(
        AgentAction(action_type="click", element_ref=0), element_name=label,
    )
    assert result.blocked_reason != "submit_requires_approval", label


@pytest.mark.parametrize("label,expected", [
    ("Save and Continue", "SAVE_AND_CONTINUE"),
    ("Next", "NEXT"),
    ("Apply Now", "APPLY"),
    ("Start your application", "START_APPLICATION"),
    ("Finish & Submit", "SUBMIT"),
    ("Review and Submit", "SUBMIT"),
])
def test_semantic_classification_of_those_labels_is_unchanged_or_correctly_submit(label, expected):
    element = _element(ref=0, name=label, tag="button", type_="button")
    action, _confidence, _irreversible = classify_semantic_action(
        element, page_context="Job application — final review",
    )
    assert action == expected, label
