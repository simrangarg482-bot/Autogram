"""
Dynamic-DOM scenarios for the stale-element-reference gate.

`test_stale_reference_grounding.py` pins the MECHANISM (both signatures
present -> compare; either missing -> no opinion; targeted actions only).
This file pins the SCENARIOS a real ATS produces — a modal opening, a
conditional field appearing, a validation banner, an SPA route change that
keeps the URL — and, at the end, that `loop.py` reacts to the rejection by
re-observing rather than crashing or proceeding.

Every scenario is expressed as a pair of `PageSignature`s, which is exactly
what `loop.py` compares at the call site (`handle.last_page_signature` vs a
fresh `capture_page_signature`), so the fakes here are the real type.
"""

import threading

import pytest

from automation.agents.autonomous.actions import (
    AgentAction,
    GroundingResult,
    validate_action_grounding,
)
from automation.agents.autonomous.decision import Decision
from automation.agents.autonomous.loop import AutonomousAgentLoop, TaskHandle
from automation.agents.autonomous.observer import PageElement, PageState
from automation.applications.page_navigator import PageSignature, capture_page_signature


def _element(ref=5, *, name="Full name", tag="input", type_="text", disabled=False):
    return PageElement(
        ref=ref, tag=tag, type=type_, name=name, value=None, required=False,
        disabled=disabled, checked=None, options=None,
    )


def _state(elements=None, url="https://jobs.example/apply"):
    return PageState(
        url=url, title="Apply", visible_text="Apply",
        elements=list(elements if elements is not None else [_element()]),
    )


def _sig(**overrides) -> PageSignature:
    base = dict(
        url="https://jobs.example/apply", title="Apply — Acme",
        heading="Your details", step_indicator="Step 1 of 3",
        controls=("input:full name", "input:email", "button:next"),
    )
    base.update(overrides)
    return PageSignature(**base)


_OBSERVED = _sig()


def _ground(action, state=None, *, current) -> GroundingResult:
    return validate_action_grounding(
        action, state if state is not None else _state(),
        canonical_urls=("https://jobs.example/apply",),
        expected_signature=_OBSERVED, current_signature=current,
    )


# ---------------------------------------------------------------------------
# The scenarios. Each `current` signature is what `capture_page_signature`
# would return after the described DOM event.
# ---------------------------------------------------------------------------

_SCENARIOS = {
    # The ref number survives, but now belongs to a different control: the
    # observer re-numbers from scratch on every observation, so ref 5 can be
    # a date picker on the page where it used to be a name input.
    "element_replaced": _sig(controls=("select:country", "input:email", "button:next")),
    # A modal opened: new controls appear on top of the page.
    "modal_opened": _sig(
        heading="Confirm your email",
        controls=("input:full name", "input:email", "button:next",
                  "input:confirmation code", "button:confirm", "button:cancel"),
    ),
    # A modal closed again: the controls it contributed disappear.
    "modal_closed": _sig(
        heading="Your details",
        controls=("input:full name",),
    ),
    # Partial re-render: some controls persist, some are gone, some are new.
    "partial_rerender": _sig(controls=("input:full name", "button:next", "input:phone")),
    # Answering "Other" reveals a conditional text input.
    "conditional_field_revealed": _sig(
        controls=("input:full name", "input:email", "input:please specify", "button:next"),
    ),
    # Validation failed: an error banner is rendered as a new region and the
    # heading changes to reflect it.
    "validation_error_rendered": _sig(
        heading="Please fix 2 errors before continuing",
        controls=("input:full name", "input:email", "button:next"),
    ),
    # SPA route change: same URL, different step entirely.
    "spa_route_change_same_url": _sig(
        heading="Work history", step_indicator="Step 2 of 3",
        controls=("input:employer", "input:job title", "button:next"),
    ),
    # A plain step advance, for completeness.
    "step_advanced": _sig(step_indicator="Step 2 of 3", heading="Work history",
                          controls=("input:employer",)),
}


@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
@pytest.mark.parametrize("action", [
    AgentAction(action_type="click", element_ref=5),
    AgentAction(action_type="fill", element_ref=5, value="Jane Doe"),
    AgentAction(action_type="select", element_ref=5, value="Yes"),
    AgentAction(action_type="check", element_ref=5),
    AgentAction(action_type="uncheck", element_ref=5),
    AgentAction(action_type="upload_file", element_ref=5, file_path="/tmp/resume.pdf"),
])
def test_every_dynamic_dom_scenario_makes_a_targeted_ref_stale(scenario, action):
    """A ref observed before any of these events is refused afterwards, for
    every targeted action type — including the ones (`check`/`upload_file`)
    that carry no value for another gate to catch."""
    result = _ground(action, current=_SCENARIOS[scenario])
    assert result.grounded is False, scenario
    assert result.stale_reference is True, scenario
    assert "stale element reference" in result.reason


def test_a_modal_opening_makes_the_modals_own_new_ref_unusable_too():
    """The controls the modal ADDED are only addressable after a fresh
    observation: acting on a page state captured before the modal opened is
    refused whether the ref is an old one or one the modal introduced."""
    pre_modal_state = _state([_element(ref=5), _element(ref=6, name="Email")])
    for ref in (5, 6, 7):
        result = _ground(
            AgentAction(action_type="click", element_ref=ref), pre_modal_state,
            current=_SCENARIOS["modal_opened"],
        )
        assert result.grounded is False, ref


def test_an_unchanged_page_is_never_flagged_stale():
    """The control case: no DOM event, no rejection. Without this the
    scenarios above would be satisfied by a gate that refuses everything and
    the agent could never act at all."""
    result = _ground(AgentAction(action_type="fill", element_ref=5, value="Jane"), current=_OBSERVED)
    assert result.grounded is True
    assert result.stale_reference is False


def test_an_element_moving_within_the_page_is_a_conservative_false_positive():
    """DOCUMENTED BEHAVIOR, not a bug. `PageSignature.controls` is a TUPLE in
    DOM order, so reordering the same controls — a drag-and-drop list, a
    re-sorted group — compares as different and the ref is refused as stale
    even though it still resolves to the same element.

    The remedy for a stale ref is "re-observe and decide again", which is
    always safe and costs one observation; the opposite error (acting on a ref
    that silently moved) is not recoverable. Recorded here so a future change
    to structural-identity comparison is a deliberate one."""
    reordered = _sig(controls=("input:email", "input:full name", "button:next"))
    result = _ground(AgentAction(action_type="fill", element_ref=5, value="Jane"), current=reordered)
    assert result.grounded is False
    assert result.stale_reference is True


def test_a_value_only_change_does_not_make_refs_stale():
    """Filling a field must not invalidate every other ref on the same page,
    or a multi-field form could never be completed: `PageSignature` carries
    control IDENTITIES, never their values (see `page_navigator.py`)."""
    real_page_unchanged = _sig()
    assert _ground(
        AgentAction(action_type="fill", element_ref=5, value="Jane"), current=real_page_unchanged,
    ).grounded is True


def test_a_value_change_on_a_live_page_is_invisible_to_the_signature(page):
    """The same claim against a real rendered page rather than a fake: typing
    into an input does not change `capture_page_signature`, but revealing a
    conditional field does."""
    page.set_content(
        '<h1>Your details</h1><label>Full name<input id="n"></label>'
        '<label>Email<input id="e"></label><button>Next</button>'
    )
    before = capture_page_signature(page)
    page.fill("#n", "Jane Doe")
    assert capture_page_signature(page).differs_from(before) is False

    page.evaluate(
        "document.body.insertAdjacentHTML('beforeend',"
        "'<label>Please specify<input id=\"s\"></label>')"
    )
    after = capture_page_signature(page)
    assert after.differs_from(before) is True
    assert _ground(
        AgentAction(action_type="fill", element_ref=5, value="Jane"), current=after,
    ).stale_reference is True


def test_page_changing_actions_survive_every_scenario():
    """`navigate`/`go_back`/`scroll`/`wait` carry no element reference — a
    changed page is their expected precondition, not a defect."""
    state = _state()
    for scenario, current in _SCENARIOS.items():
        for action in (
            AgentAction(action_type="navigate", url=state.url),
            AgentAction(action_type="go_back"),
            AgentAction(action_type="scroll", direction="down"),
            AgentAction(action_type="wait", wait_ms=250),
            AgentAction(action_type="get_page_state"),
        ):
            result = _ground(action, state, current=current)
            assert result.stale_reference is False, (scenario, action.action_type)


# ---------------------------------------------------------------------------
# What `loop.py` does with the rejection.
# ---------------------------------------------------------------------------

class FakeDB:
    def refresh(self, obj): pass
    def rollback(self): pass
    def commit(self): pass


class FakeTask:
    def __init__(self, **overrides):
        self.task_id = "stale-ref-scenarios"
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


def _wired_loop(monkeypatch, *, current_signature):
    from app.services import autonomous_task_repository as task_repo

    loop = AutonomousAgentLoop(
        "stale-ref-scenarios",
        TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event()),
    )
    loop.handle.last_page_signature = _OBSERVED
    recorded = {"actions": [], "audits": [], "paused": [], "executed": []}
    monkeypatch.setattr(loop, "_safe_page_signature", lambda: current_signature)
    monkeypatch.setattr(loop, "_record_audit", lambda db, t, kind, data: recorded["audits"].append((kind, data)))
    monkeypatch.setattr(loop, "_pause_for_human", lambda *a, **k: recorded["paused"].append(a))
    monkeypatch.setattr(loop, "_record_chat_action", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_set_substate", lambda *a, **k: None)
    monkeypatch.setattr(task_repo, "append_action", lambda db, t, entry: recorded["actions"].append(entry))
    return loop, recorded


@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
def test_the_loop_reobserves_instead_of_dispatching_a_stale_ref(scenario, monkeypatch):
    """The contract at the call site: a stale-reference rejection returns True
    (keep looping -> the loop body `continue`s straight into a fresh
    `observe_page`), never dispatches to the executor, never pauses for a
    human, and never raises. `handle.page` is left as `None` on purpose — if
    anything tried to touch the browser after the rejection this would blow up
    with an AttributeError instead of passing."""
    loop, recorded = _wired_loop(monkeypatch, current_signature=_SCENARIOS[scenario])
    decision = Decision(
        decision_type="EXECUTE_ACTION",
        action=AgentAction(action_type="fill", element_ref=5, value="Jane Doe"),
        reasoning="fill the name field",
    )

    keep_going = loop._handle_execute_action(FakeDB(), FakeTask(), _state(), decision)

    assert keep_going is True, scenario
    assert recorded["paused"] == []
    assert [kind for kind, _ in recorded["audits"]] == ["action_rejected"]
    reason, payload = "action_rejected", recorded["audits"][0][1]
    assert payload["reason"] == "stale_element_reference"
    assert payload["recovery_action"] == "REOBSERVE"
    logged = recorded["actions"][-1]
    assert logged["blocked_reason"] == "action_not_grounded"
    assert logged["failure_code"] == "STALE_REFERENCE"
    assert logged["action_attempted"] is False
    assert loop.handle.last_action_verified is False


def test_a_non_stale_grounding_failure_still_escalates_rather_than_looping(monkeypatch):
    """Cross-check that the stale branch is specific: an ordinary ungrounded
    action (invented ref, same page) takes the human-escalation path, so
    "return True and re-observe" cannot be reached by any other refusal."""
    loop, recorded = _wired_loop(monkeypatch, current_signature=_OBSERVED)
    monkeypatch.setattr(loop, "_attempt_vision_assisted_decision", lambda task, state: (None, None))
    decision = Decision(
        decision_type="EXECUTE_ACTION",
        action=AgentAction(action_type="click", element_ref=999),
        reasoning="click something that does not exist",
    )

    keep_going = loop._handle_execute_action(FakeDB(), FakeTask(), _state(), decision)

    assert keep_going is False
    assert recorded["paused"], "an ungrounded (non-stale) action must reach a human"
    assert recorded["actions"][-1]["failure_code"] is None
