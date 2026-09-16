"""Unit tests for `automation.agents.autonomous.question_ontology`."""

import automation.forms.question_classifier as fine_classifier
from automation.agents.autonomous.observer import PageElement
from automation.agents.autonomous.question_ontology import (
    COARSE_CATEGORIES,
    SENSITIVE_COARSE_CATEGORIES,
    _FINE_TO_COARSE,
    classify_field,
)


def _element(name="", surrounding_text="", **kwargs):
    defaults = dict(
        ref=0, tag="input", type="text", name=name, value=None, required=False,
        disabled=False, checked=None, options=None, surrounding_text=surrounding_text,
    )
    defaults.update(kwargs)
    return PageElement(**defaults)


def test_every_fine_category_from_question_classifier_is_mapped():
    """Regression guard: if `question_classifier.py` grows a new category,
    this test fails loudly instead of the new category silently landing in
    the ADDITIONAL_QUESTIONS catch-all forever."""
    fine_categories = {category for category, _ in fine_classifier._CATEGORY_PHRASES}
    missing = fine_categories - set(_FINE_TO_COARSE)
    assert not missing, f"question_classifier categories with no coarse mapping: {missing}"


def test_phone_aliases_all_map_to_contact_information():
    for label in ("Mobile Number", "Phone", "Telephone", "Contact Number"):
        result = classify_field(_element(name=label))
        assert result.coarse_category == "CONTACT_INFORMATION", label


def test_resume_field_classified():
    assert classify_field(_element(name="Upload your resume")).coarse_category == "RESUME"
    assert classify_field(_element(name="CV")).coarse_category == "RESUME"


def test_work_authorization_is_sensitive():
    result = classify_field(_element(name="Are you authorized to work in the US?"))
    assert result.coarse_category == "WORK_AUTHORIZATION"
    assert result.sensitive is True
    assert result.coarse_category in SENSITIVE_COARSE_CATEGORIES


def test_demographic_gender_is_sensitive_via_fine_classifier():
    result = classify_field(_element(name="Gender", surrounding_text="Please select your gender identity"))
    assert result.coarse_category == "DEMOGRAPHICS"
    assert result.sensitive is True


def test_empty_field_is_unclassified_never_guessed():
    result = classify_field(_element(name="   "))
    assert result.coarse_category is None
    assert result.sensitive is False


def test_all_coarse_categories_are_from_the_closed_spec_vocabulary():
    for coarse in _FINE_TO_COARSE.values():
        assert coarse in COARSE_CATEGORIES
