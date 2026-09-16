"""
Stale-element-reference protection in `actions.validate_action_grounding`.

An `element_ref` is only meaningful relative to the page it was observed on.
These tests pin the new `expected_signature`/`current_signature` pair: it must
reject a targeted action whose observation-time page signature no longer
matches the live page, must stay silent when there is nothing to compare
against, and must not change the behavior of any existing call that omits it.
"""

from automation.agents.autonomous.actions import AgentAction, validate_action_grounding
from automation.agents.autonomous.observer import PageElement, PageState
from automation.applications.page_navigator import PageSignature


def _element(ref=5, *, name="Full name", semantic=None, confidence=None):
    return PageElement(
        ref=ref, tag="input", type="text", name=name, value=None,
        required=False, disabled=False, checked=None, options=None,
        semantic_action=semantic, action_confidence=confidence,
    )


def _state():
    return PageState(
        url="https://jobs.example/apply", title="Apply", visible_text="Apply",
        elements=[_element()],
    )


_OBSERVED = PageSignature(
    url="https://jobs.example/apply", title="Apply", heading="Your details",
    step_indicator="Step 1 of 3", controls=("input:full name",),
)
_MOVED_ON = PageSignature(
    url="https://jobs.example/apply", title="Apply", heading="Work history",
    step_indicator="Step 2 of 3", controls=("input:employer",),
)


def test_targeted_action_is_rejected_when_the_page_moved_on():
    result = validate_action_grounding(
        AgentAction(action_type="fill", element_ref=5, value="Jane Doe"), _state(),
        expected_signature=_OBSERVED, current_signature=_MOVED_ON,
    )
    assert result.grounded is False
    assert result.stale_reference is True
    assert "stale element reference" in result.reason


def test_identical_signature_is_not_treated_as_stale():
    result = validate_action_grounding(
        AgentAction(action_type="fill", element_ref=5, value="Jane Doe"), _state(),
        expected_signature=_OBSERVED, current_signature=_OBSERVED,
    )
    assert result.grounded is True
    assert result.stale_reference is False


def test_first_ever_observation_has_no_prior_signature_and_is_not_stale():
    """No prior signature (or no readable current one) means "unknown", never
    "changed" — the check only fires when both sides are actually known."""
    for expected, current in ((None, _MOVED_ON), (_OBSERVED, None), (None, None)):
        result = validate_action_grounding(
            AgentAction(action_type="fill", element_ref=5, value="Jane Doe"), _state(),
            expected_signature=expected, current_signature=current,
        )
        assert result.grounded is True, (expected, current)


def test_page_changing_actions_are_never_rejected_as_stale():
    """`navigate`/`go_back`/`scroll`/`wait` carry no element reference and are
    expected to change the page, so a signature difference is not a defect."""
    state = _state()
    for action in (
        AgentAction(action_type="navigate", url=state.url),
        AgentAction(action_type="go_back"),
        AgentAction(action_type="scroll", direction="down"),
        AgentAction(action_type="wait", wait_ms=500),
    ):
        result = validate_action_grounding(
            action, state, canonical_urls=(state.url,),
            expected_signature=_OBSERVED, current_signature=_MOVED_ON,
        )
        assert result.stale_reference is False, action.action_type


def test_existing_call_signature_is_unchanged():
    """Regression guard for every current caller: the new parameters are
    keyword-only with `None` defaults, so the old call is byte-identical in
    behavior."""
    state = _state()
    assert validate_action_grounding(
        AgentAction(action_type="fill", element_ref=5, value="Jane"), state,
        canonical_urls=(state.url,), allowlisted_urls=(),
    ).grounded is True
