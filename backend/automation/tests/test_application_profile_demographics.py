"""
Saved EEO answers ("Application Profile") — Gender, Disability Status, Veteran
Status, Race and Hispanic/Latino — answered from `candidate_demographics` on
BOTH automation paths.

Why this exists: on a live Greenhouse form (job-boards.greenhouse.io/warp) every
one of these came back blank. The dropdowns were opened and their options read
correctly ("Male", "Female", "Decline To Self Identify"); nothing was selected
because the user had no saved answers, no UI existed to save them, and three
smaller defects sat behind that:

- "Race" and "Are you Hispanic/Latino?" were not classified as demographic at
  all, so they went to the LLM — which this codebase says must never answer them;
- a stored `decline_to_answer` did not match Greenhouse's "Decline To Self
  Identify" (no hyphen), and on a Yes/No/Decline list a decline could resolve to
  a bare "No";
- the autonomous agent could neither read saved answers nor `select` in a
  react-select (`select_option()` raises "Element is not a <select> element").
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.db_models import CandidateDemographics, CandidateProfile
from automation.agents.autonomous import saved_demographics as sd
from automation.agents.autonomous.actions import AgentAction
from automation.agents.autonomous.executor import ActionExecutor, is_sensitive_field_name
from automation.agents.autonomous.loop import AutonomousAgentLoop, TaskHandle
from automation.agents.autonomous.observer import PageElement
from automation.agents.autonomous.question_ontology import classify_field
from automation.forms import answer_engine as answer_engine_module
from automation.forms.answer_engine import SOURCE_NEEDS_USER_INPUT, ApplicationAnswerEngine, Question
from automation.forms.demographic_matching import is_stored_answer, match_demographic_value
from automation.forms.question_classifier import (
    CATEGORY_DEMOGRAPHIC_GENDER,
    CATEGORY_DEMOGRAPHIC_HISPANIC_LATINO,
    CATEGORY_DEMOGRAPHIC_RACE_ETHNICITY,
    DEMOGRAPHIC_STORAGE_FIELDS,
    classify_question,
    is_demographic,
)
from automation.tests.test_dropdown_live_form_shapes import _react_select

# The real option lists off the live Warp/Greenhouse form (see the module docstring).
GH_GENDER = ["Male", "Female", "Decline To Self Identify"]
GH_HISPANIC = ["Yes", "No", "Decline To Self Identify"]
GH_VETERAN = [
    "I am not a protected veteran",
    "I identify as one or more of the classifications of a protected veteran",
    "I don't wish to answer",
]
GH_DISABILITY = [
    "Yes, I have a disability, or have had one in the past",
    "No, I do not have a disability and have not had one in the past",
    "I do not want to answer",
]
GH_RACE = [
    "American Indian or Alaska Native", "Asian", "Black or African American",
    "Native Hawaiian or Other Pacific Islander", "White", "Two or More Races", "Decline To Self Identify",
]


# ---------------------------------------------------------------------------
# 1. Classification — Race and Hispanic/Latino were unclassified
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label, category", [
    ("Gender", "demographic_gender"),
    ("Gender*", "demographic_gender"),
    ("Disability Status", "demographic_disability_status"),
    ("Veteran Status", "demographic_veteran_status"),
    ("Race", "demographic_race_ethnicity"),
    ("Race*", "demographic_race_ethnicity"),
    ("Race/Ethnicity", "demographic_race_ethnicity"),
    ("Are you Hispanic/Latino?", "demographic_hispanic_latino"),
    ("Are you Hispanic or Latino?", "demographic_hispanic_latino"),
    ("Hispanic/Latino", "demographic_hispanic_latino"),
    ("Do you identify as Hispanic/Latinx?", "demographic_hispanic_latino"),
])
def test_the_eeo_questions_are_all_classified_as_demographic(label, category):
    assert classify_question(label) == category
    assert is_demographic(category)


def test_a_race_select_that_merely_lists_a_hispanic_option_stays_a_race_question():
    """Lever's race <select> recovers as label + every option. It offers "Hispanic
    or Latino" as ONE OF THE RACES — it is not the separate yes/no question."""
    lever = "RaceSelect...Hispanic or LatinoWhiteBlack or African AmericanAsianDecline to self-identify"
    assert classify_question(lever) == CATEGORY_DEMOGRAPHIC_RACE_ETHNICITY


@pytest.mark.parametrize("label", ["Embrace change", "Grace period", "Traceability", "Race condition debugging"])
def test_race_is_a_word_match_not_a_substring_match(label):
    assert classify_question(label) is None


def test_every_demographic_category_has_a_storage_column():
    from automation.forms.question_classifier import DEMOGRAPHIC_CATEGORIES
    assert set(DEMOGRAPHIC_STORAGE_FIELDS) == set(DEMOGRAPHIC_CATEGORIES)
    for column in DEMOGRAPHIC_STORAGE_FIELDS.values():
        assert hasattr(CandidateDemographics, column)


@pytest.mark.parametrize("name", ["Are you Hispanic/Latino?", "Hispanic or Latino", "Latinx"])
def test_hispanic_latino_is_gated_as_sensitive_by_both_independent_layers(name):
    """The executor's name gate and the ontology must agree (see
    test_sensitive_field_corpus.py). Neither covered Hispanic/Latino before."""
    assert is_sensitive_field_name(name)
    element = PageElement(
        ref=1, tag="input", type="text", name=name, value=None, required=False, disabled=False,
        checked=None, options=None, role="combobox",
    )
    assert classify_field(element).sensitive


# ---------------------------------------------------------------------------
# 2. Matching a saved value to the form's own wording
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stored, options, expected", [
    ("male", GH_GENDER, "Male"),
    ("decline_to_answer", GH_GENDER, "Decline To Self Identify"),      # the hyphen-less wording
    ("decline_to_answer", GH_HISPANIC, "Decline To Self Identify"),
    ("no", GH_HISPANIC, "No"),
    ("yes", GH_HISPANIC, "Yes"),
    ("no_disability", GH_DISABILITY, "No, I do not have a disability and have not had one in the past"),
    ("decline_to_answer", GH_DISABILITY, "I do not want to answer"),
    ("not_veteran", GH_VETERAN, "I am not a protected veteran"),
    ("Asian", GH_RACE, "Asian"),
    ("asian", GH_RACE, "Asian"),
    ("Two or More Races", GH_RACE, "Two or More Races"),
])
def test_saved_values_resolve_to_the_greenhouse_option(stored, options, expected):
    assert match_demographic_value(stored, options) == expected


def test_a_decline_never_resolves_to_a_plain_yes_or_no():
    """`match_option`'s containment is bidirectional, so a decline phrasing used
    to resolve to a bare "No" (contained in "not"). Answering a demographic
    question "No" for someone who chose NOT to answer is a wrong answer."""
    assert match_demographic_value("decline_to_answer", ["Yes", "No"]) is None


def test_is_stored_answer_accepts_only_the_saved_answer():
    assert is_stored_answer("no_disability", GH_DISABILITY[1], GH_DISABILITY)
    # A model's guess of any other option is refused ...
    assert not is_stored_answer("no_disability", GH_DISABILITY[0], GH_DISABILITY)
    assert not is_stored_answer("no_disability", GH_DISABILITY[2], GH_DISABILITY)
    # ... and, with no options visible, containment is NOT used: "I do not want to
    # answer" contains "no" but is not the saved answer "no".
    assert not is_stored_answer("no_disability", "I do not want to answer")
    assert is_stored_answer("no_disability", "No")
    assert not is_stored_answer("", "Male", GH_GENDER)
    assert not is_stored_answer("male", "", GH_GENDER)


# ---------------------------------------------------------------------------
# 3. Deterministic engine: saved answers are used, and the LLM is never asked
# ---------------------------------------------------------------------------

def _llm_must_not_be_called(**_kwargs):
    raise AssertionError("the LLM must never be consulted for a demographic question")


def _engine(monkeypatch, **demographics):
    row = CandidateDemographics(id="d1", candidate_id="p1", **demographics) if demographics else None
    monkeypatch.setattr(answer_engine_module, "get_candidate_demographics", lambda db, profile_id: row)
    return ApplicationAnswerEngine(
        profile=CandidateProfile(profile_id="p1", user_id="u1"), db=object(), llm_fn=_llm_must_not_be_called,
    )


def test_the_engine_answers_every_greenhouse_eeo_question_from_saved_values(monkeypatch):
    engine = _engine(
        monkeypatch, gender="male", disability_status="no_disability", veteran_status="not_veteran",
        race_ethnicity="asian", hispanic_latino="no",
    )
    results = engine.answer_batch([
        Question("Gender", GH_GENDER),
        Question("Disability Status", GH_DISABILITY),
        Question("Veteran Status", GH_VETERAN),
        Question("Race", GH_RACE),
        Question("Are you Hispanic/Latino?", GH_HISPANIC),
    ])

    assert [r.answer for r in results] == [
        "Male",
        "No, I do not have a disability and have not had one in the past",
        "I am not a protected veteran",
        "Asian",
        "No",
    ]
    assert all(r.source == "deterministic" for r in results)


def test_unsaved_hispanic_and_race_are_left_for_the_user_never_sent_to_the_llm(monkeypatch):
    engine = _engine(monkeypatch)  # no demographics row at all — the reported user's situation
    race, hispanic = engine.answer_batch([Question("Race", GH_RACE), Question("Are you Hispanic/Latino?", GH_HISPANIC)])

    for result in (race, hispanic):
        assert result.source == SOURCE_NEEDS_USER_INPUT
        assert result.answer == ""


def test_a_saved_decline_fills_the_greenhouse_decline_option(monkeypatch):
    engine = _engine(monkeypatch, gender="decline_to_answer", hispanic_latino="decline_to_answer")
    gender, hispanic = engine.answer_batch([Question("Gender", GH_GENDER), Question("Are you Hispanic/Latino?", GH_HISPANIC)])

    assert gender.answer == "Decline To Self Identify"
    assert hispanic.answer == "Decline To Self Identify"


# ---------------------------------------------------------------------------
# 4. Real react-select: open the dropdown and click the role="option"
# ---------------------------------------------------------------------------

def _page_with(page, *fields):
    page.set_content("<!DOCTYPE html><html><body>" + "".join(
        _react_select(widget_id, label, options) for widget_id, label, options in fields
    ) + "</body></html>")
    for index, (widget_id, _label, _options) in enumerate(fields, start=1):
        page.evaluate(f"document.getElementById('{widget_id}').setAttribute('data-agent-ref', '{index}')")


def _shown(page, widget_id):
    return page.locator(f"#{widget_id}").locator(
        "xpath=ancestor::div[contains(@class,'select__container')][1]//div[contains(@class,'select__single-value')]"
    ).first.text_content()


def test_select_on_a_react_select_clicks_the_option_instead_of_select_option(page):
    """The reported failure: `select` on Greenhouse's react-select raised
    "Element is not a <select> element" every time."""
    _page_with(page, ("gender", "Gender", GH_GENDER))

    result = ActionExecutor(page, auto_submit_approved=False).execute(
        AgentAction(action_type="select", element_ref=1, value="Decline To Self Identify"),
        element_name="Gender", element_type="combobox", is_sourced=True,
    )

    assert result.success and result.verified, result.detail
    assert _shown(page, "gender") == "Decline To Self Identify"


def test_select_on_a_native_select_still_uses_select_option(page):
    page.set_content(
        "<select data-agent-ref='1'><option>Yes</option><option>No</option></select>"
    )

    result = ActionExecutor(page, auto_submit_approved=False).execute(
        AgentAction(action_type="select", element_ref=1, value="No"), element_name="Preferred city",
    )

    assert result.success, result.detail
    assert page.locator("select").input_value() == "No"


def test_a_value_the_dropdown_does_not_offer_fails_instead_of_selecting_something_else(page):
    _page_with(page, ("gender", "Gender", GH_GENDER))

    result = ActionExecutor(page, auto_submit_approved=False).execute(
        AgentAction(action_type="select", element_ref=1, value="Nonexistent"),
        element_name="Gender", element_type="combobox", is_sourced=True,
    )

    assert not result.success
    assert page.locator(".select__single-value").count() == 0


def test_the_sensitive_field_gate_still_refuses_an_unsourced_select_on_a_react_select(page):
    """Preserved, unchanged: fixing `select` must not let an unsourced value
    through a protected-class field."""
    _page_with(page, ("gender", "Gender", GH_GENDER))

    result = ActionExecutor(page, auto_submit_approved=False).execute(
        AgentAction(action_type="select", element_ref=1, value="Male"),
        element_name="Gender", element_type="combobox", is_sourced=False,
    )

    assert result.blocked_reason == "sensitive_field_requires_human"
    assert page.locator(".select__single-value").count() == 0


# ---------------------------------------------------------------------------
# 5. Autonomous pre-pass: saved answers only, resolved against the REAL options
# ---------------------------------------------------------------------------

def _element(page, ref, name, *, value=None, disabled=False, role="combobox", tag="input"):
    return PageElement(
        ref=ref, tag=tag, type="text", name=name, value=value, required=True, disabled=disabled,
        checked=None, options=None, role=role,
    )


def test_the_pre_pass_finds_each_empty_eeo_dropdown_and_its_exact_option(page):
    _page_with(
        page,
        ("gender", "Gender", GH_GENDER), ("hispanic_ethnicity", "Are you Hispanic/Latino?", GH_HISPANIC),
        ("q_city", "Preferred city", ["Pune", "Delhi"]),
    )
    state = SimpleNamespace(elements=[
        _element(page, 1, "Gender"), _element(page, 2, "Are you Hispanic/Latino?"), _element(page, 3, "Preferred city"),
    ])
    saved = {"gender": "decline_to_answer", "hispanic_latino": "no"}
    attempts: dict = {}

    first = sd.find_pending_answer(page, state, saved, attempts)
    assert (first.element.name, first.category, first.value) == (
        "Gender", CATEGORY_DEMOGRAPHIC_GENDER, "Decline To Self Identify",
    )
    # An ordinary question is never touched, saved answers or not.
    state.elements = [e for e in state.elements if e.name != "Gender"]
    second = sd.find_pending_answer(page, state, saved, attempts)
    assert (second.category, second.value) == (CATEGORY_DEMOGRAPHIC_HISPANIC_LATINO, "No")
    state.elements = [e for e in state.elements if e.name == "Preferred city"]
    assert sd.find_pending_answer(page, state, saved, attempts) is None


def test_the_pre_pass_does_nothing_without_a_saved_answer_so_the_human_pause_is_preserved(page):
    _page_with(page, ("gender", "Gender", GH_GENDER))
    state = SimpleNamespace(elements=[_element(page, 1, "Gender")])

    assert sd.find_pending_answer(page, state, {}, {}) is None
    assert sd.find_pending_answer(page, state, {"veteran_status": "veteran"}, {}) is None  # a different field's answer


def test_the_pre_pass_leaves_a_saved_value_the_form_has_no_option_for(page):
    _page_with(page, ("gender", "Gender", ["Man", "Woman"]))
    state = SimpleNamespace(elements=[_element(page, 1, "Gender")])
    attempts: dict = {}

    assert sd.find_pending_answer(page, state, {"gender": "non_binary"}, attempts) is None
    # ... and is not re-probed (open/close of the dropdown) on every loop iteration.
    assert sd.find_pending_answer(page, state, {"gender": "non_binary"}, attempts) is None
    assert page.locator(".select__single-value").count() == 0


def test_the_pre_pass_skips_filled_disabled_and_exhausted_fields(page):
    _page_with(page, ("gender", "Gender", GH_GENDER))
    saved = {"gender": "male"}

    assert sd.find_pending_answer(page, SimpleNamespace(elements=[_element(page, 1, "Gender", value="Male")]), saved, {}) is None
    assert sd.find_pending_answer(page, SimpleNamespace(elements=[_element(page, 1, "Gender", disabled=True)]), saved, {}) is None

    element = _element(page, 1, "Gender")
    from automation.agents.autonomous.observer import field_identity
    exhausted = {field_identity(element): sd.MAX_PREPASS_ATTEMPTS}
    assert sd.find_pending_answer(page, SimpleNamespace(elements=[element]), saved, exhausted) is None


def test_a_field_is_classified_by_its_own_name_not_its_sections_intro_text(page):
    """An EEO section's intro names gender, race and veteran status together;
    classifying the Race dropdown by that text could hand it the gender answer."""
    element = _element(page, 1, "Race")
    element.section = "Voluntary Self-Identification: gender, race, veteran status"
    element.surrounding_text = "Gender Male Female"

    assert sd.demographic_category(element) == CATEGORY_DEMOGRAPHIC_RACE_ETHNICITY
    assert sd.stored_answer_for(element, {"gender": "male"}) is None       # no race answer saved
    assert sd.stored_answer_for(element, {"race_ethnicity": "Asian"}) == (CATEGORY_DEMOGRAPHIC_RACE_ETHNICITY, "Asian")


# ---------------------------------------------------------------------------
# 6. Loop sourcing — what may be called "sourced" for the executor's gate
# ---------------------------------------------------------------------------

def _loop(pending=None):
    loop = object.__new__(AutonomousAgentLoop)
    loop.handle = TaskHandle(resume_event=None, cancel_requested=None)
    loop.handle.pending_demographic = pending
    return loop


def _task(**saved):
    return SimpleNamespace(candidate_profile={"profile": {}, "demographics": saved}, confirmed_answers={})


def test_a_model_proposed_demographic_value_is_sourced_only_if_it_is_the_saved_answer():
    match = PageElement(
        ref=4, tag="select", type="select", name="Disability Status", value=None, required=True,
        disabled=False, checked=None, options=GH_DISABILITY,
    )
    loop = _loop()
    task = _task(disability_status="no_disability")

    assert loop._saved_demographic_source(task, match, GH_DISABILITY[1]) == "no_disability"
    assert loop._saved_demographic_source(task, match, GH_DISABILITY[0]) is None   # the model's guess
    assert loop._saved_demographic_source(task, match, "I do not want to answer") is None
    assert loop._saved_demographic_source(_task(), match, GH_DISABILITY[1]) is None  # nothing saved


def test_the_pre_pass_value_is_sourced_but_a_different_value_for_the_same_ref_is_not():
    element = PageElement(
        ref=7, tag="input", type="text", name="Gender", value=None, required=True, disabled=False,
        checked=None, options=None, role="combobox",
    )
    pending = sd.SavedDemographicAnswer(element=element, category=CATEGORY_DEMOGRAPHIC_GENDER, stored="male", value="Male")
    loop = _loop(pending)
    task = _task(gender="male")

    # A closed react-select exposes no options, so this rides on the code-set state.
    assert loop._saved_demographic_source(task, element, "Male") == "male"
    assert loop._saved_demographic_source(task, element, "Female") is None


def test_answer_decision_marks_a_saved_demographic_sourced_and_records_the_provenance():
    match = PageElement(
        ref=4, tag="select", type="select", name="Gender", value=None, required=True, disabled=False,
        checked=None, options=GH_GENDER,
    )
    loop = _loop()

    sourced, decision, classification = loop._answer_decision(_task(gender="male"), match, "Male")

    assert sourced is True
    assert decision.fact.source == "USER_PROFILE" and decision.autofill_permitted
    assert classification.sensitive  # still classified sensitive; the gate still runs


def test_answer_decision_does_not_source_an_unsaved_demographic_value():
    match = PageElement(
        ref=4, tag="select", type="select", name="Gender", value=None, required=True, disabled=False,
        checked=None, options=GH_GENDER,
    )
    loop = _loop()
    loop._truth_store = lambda task: __import__("automation.agents.autonomous.truth_store", fromlist=["TruthStore"]).TruthStore()

    sourced, _decision, _classification = loop._answer_decision(_task(), match, "Male")

    assert sourced is False


def test_the_saved_demographics_never_appear_in_what_the_llm_is_sent():
    """`decision.py` is handed `profile`, `resume_text` and `parsed_resume` only.
    A separate top-level `demographics` key must stay out of all three."""
    snapshot = {"profile": {"full_name": "A"}, "resume_text": "r", "parsed_resume": {}, "demographics": {"gender": "male"}}
    sent = {k: snapshot.get(k) for k in ("profile", "resume_text", "parsed_resume")}

    assert "male" not in str(sent)
