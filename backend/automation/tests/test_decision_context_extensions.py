"""
The two additive changes to the decision step:

1. an explicit prompt-injection clause APPENDED to `SYSTEM_PROMPT` (the
   original text stays verbatim — `test_autonomous_decision.py` guards that
   independently);
2. three new OPTIONAL context keys (`page_classification`,
   `questions_awaiting_human_input`, `resolved_facts_summary`) that existing
   callers may omit with no change in behavior.
"""

import json
from unittest.mock import patch

from automation.agents.autonomous.decision import SYSTEM_PROMPT, decide_next_step
from automation.agents.autonomous.observer import PageElement, PageState

_PAGE_STATE = PageState(
    url="https://example.com/apply", title="Apply", visible_text="Apply for Software Engineer",
    elements=[PageElement(ref=0, tag="input", type="text", name="Full name", value=None,
                          required=True, disabled=False, checked=None, options=None)],
)
_PAYLOAD = {"decision": "EXECUTE_ACTION", "action": {"action_type": "fill", "element_ref": 0, "value": "Jane"}}


def _decide(**overrides):
    kwargs = dict(
        job_url="https://example.com/apply", original_objective="Apply",
        resume_text="Jane Doe resume", parsed_resume={}, profile={},
        confirmed_answers={}, page_state=_PAGE_STATE, action_history=[],
        uploaded_documents=[], auto_submit_approved=False,
    )
    kwargs.update(overrides)
    with patch(
        "automation.agents.autonomous.decision.generate_answer",
        return_value=json.dumps(_PAYLOAD),
    ) as generate:
        decision = decide_next_step(**kwargs)
    return decision, generate.call_args.kwargs


def test_system_prompt_states_the_prompt_injection_rule():
    assert "prompt injection" in SYSTEM_PROMPT
    assert "never a command" in SYSTEM_PROMPT
    # The original, load-bearing text is still there, unmodified and first.
    assert SYSTEM_PROMPT.startswith("You are Autogram's autonomous job application agent.")
    assert "Never claim that the application was successfully submitted" in SYSTEM_PROMPT
    injection_index = SYSTEM_PROMPT.index("prompt injection")
    assert injection_index > SYSTEM_PROMPT.index("Your purpose is to make intelligent progress")


def test_existing_callers_that_omit_the_new_context_still_work():
    decision, call = _decide()
    assert decision.decision_type == "EXECUTE_ACTION"
    prompt = call["prompt"]
    assert '"page_classification": {}' in prompt
    assert '"questions_awaiting_human_input": []' in prompt
    assert '"resolved_facts_summary": {}' in prompt


def test_new_context_is_threaded_into_the_prompt_when_provided():
    _, call = _decide(
        page_classification={"page_type": "APPLICATION_FORM", "application_stage": "contact_information",
                             "confidence": 0.6},
        question_ledger_summary=[{"label": "Visa status", "category": "VISA", "why": "waiting on the user"}],
        resolved_facts_summary={"email": "USER_PROFILE", "phone": "RESUME"},
    )
    prompt = call["prompt"]
    assert '"page_type": "APPLICATION_FORM"' in prompt
    assert '"Visa status"' in prompt
    assert '"email": "USER_PROFILE"' in prompt


def test_resolved_facts_summary_carries_provenance_not_values():
    """The summary exists so the model knows WHICH facts are backed by a
    verified source; the values themselves already reach the prompt through
    the profile/resume/confirmed-answer keys and are not duplicated here."""
    _, call = _decide(resolved_facts_summary={"email": "USER_PROFILE"})
    context = json.loads(call["prompt"].split("TASK CONTEXT (JSON):\n", 1)[1].split("\n\nOBSERVED ACTIONS", 1)[0])
    assert context["resolved_facts_summary"] == {"email": "USER_PROFILE"}
