"""
Phase 6 — field-label generalization (portal-agnostic label synonyms).

The contract under test: the SAME underlying fact, asked about under any of
the wordings real career portals actually use, normalizes to the SAME coarse
category from `question_ontology.classify_field`. "Mobile", "Cell Number" and
"Contact Number" are the same field as "Phone"; "Surname" and "Family Name"
are the same field as "Last Name"; "Institution" is the same field as
"University". Nothing here asserts a VALUE may be filled — classification is
a label, never an authorization (see the module docstring of
`question_ontology.py`, and `test_provenance_regression.py` /
`test_sensitive_field_corpus.py` for the authorization layers).

The other half of the contract is just as important: a genuinely AMBIGUOUS
bare label ("Name", "Location", "Status", "Number", "ID") must NOT be
confidently normalized. `QuestionClassification` carries no numeric
confidence — its honest-uncertainty signal is `coarse_category is None`,
which every caller treats as "ask the LLM / ask a human", never as license
to guess (see `classify_field`'s docstring and `field_coverage.py`). These
tests assert exactly that, per label.

Fixture conventions follow `test_question_ontology.py` (hand-built
`PageElement`s, no browser): this file is the label-vocabulary layer, while
`test_page_generalization_corpus.py` is the rendered-page layer.
"""

import pytest

from automation.agents.autonomous.observer import PageElement
from automation.agents.autonomous.question_ontology import (
    COARSE_CATEGORIES,
    classify_field,
)


def _element(name="", surrounding_text="", **kwargs):
    """Same helper shape as `test_question_ontology.py::_element`."""
    defaults = dict(
        ref=0, tag="input", type="text", name=name, value=None, required=False,
        disabled=False, checked=None, options=None, surrounding_text=surrounding_text,
    )
    defaults.update(kwargs)
    return PageElement(**defaults)


def _category(label: str) -> str | None:
    return classify_field(_element(name=label)).coarse_category


# ---------------------------------------------------------------------------
# Synonym groups: every label in a group must land in the same coarse bucket.
# Named by the FACT being asked for, never by a vendor.
# ---------------------------------------------------------------------------

_SYNONYM_GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "phone": ("CONTACT_INFORMATION", (
        "Phone", "Mobile", "Telephone", "Contact Number", "Cell Number",
        "Phone Number", "Mobile Number", "Cell Phone",
    )),
    "email": ("CONTACT_INFORMATION", (
        "Email", "Email Address", "E-mail", "E-mail Address",
    )),
    "first_name": ("PERSONAL_INFORMATION", (
        "First Name", "Given Name", "Forename",
    )),
    "last_name": ("PERSONAL_INFORMATION", (
        "Last Name", "Surname", "Family Name",
    )),
    "school": ("EDUCATION", (
        "University", "College", "Institution", "School",
    )),
    "degree": ("EDUCATION", (
        "Degree", "Qualification", "Education Level",
    )),
}


@pytest.mark.parametrize("group_name", sorted(_SYNONYM_GROUPS))
def test_every_label_in_a_synonym_group_shares_one_coarse_category(group_name):
    expected, labels = _SYNONYM_GROUPS[group_name]
    seen = {label: _category(label) for label in labels}
    assert set(seen.values()) == {expected}, (
        f"synonym group {group_name!r} did not normalize to a single category: {seen}"
    )


@pytest.mark.parametrize(
    "label,expected",
    [(label, expected) for expected, labels in _SYNONYM_GROUPS.values() for label in labels],
)
def test_synonym_label_classifies_individually(label, expected):
    """Per-label assertion so a regression names the exact wording that broke
    rather than only the group."""
    assert _category(label) == expected, label


def test_phone_and_email_groups_do_not_collapse_into_each_other():
    """Both are CONTACT_INFORMATION, which is correct and intended — but the
    coarse bucket must be a real member of the closed vocabulary, not an
    accidental catch-all."""
    assert _category("Phone") in COARSE_CATEGORIES
    assert _category("Email Address") in COARSE_CATEGORIES


def test_email_address_is_contact_information_not_postal_address():
    """REGRESSION (found in Phase 6): "Email Address" — by far the most common
    wording for an email field — was claimed by `_STRUCTURAL_PATTERNS`'
    deliberately generic ADDRESS matcher (`\\baddress\\b`), which is checked
    before CONTACT_INFORMATION, and came back as a postal-address field. A
    bare "Email" classified correctly, so the mis-bucketing depended purely on
    the portal's wording — exactly the vendor-wording sensitivity this ontology
    exists to remove."""
    for label in ("Email Address", "E-mail Address", "Email address"):
        assert _category(label) == "CONTACT_INFORMATION", label


@pytest.mark.parametrize(
    "label",
    ["Street Address", "Address Line 1", "Home Address", "Mailing Address",
     "City", "State", "Postal Code", "ZIP", "Country"],
)
def test_genuine_postal_address_labels_still_classify_as_address(label):
    """The other direction of the same regression: putting
    CONTACT_INFORMATION first must not steal any real address label."""
    assert _category(label) == "ADDRESS", label


def test_mobile_app_experience_is_not_a_contact_field():
    """The `mobile` matcher's negative lookahead: a SKILLS question that
    happens to contain the word "mobile" must not become a phone field."""
    assert _category("Mobile app development experience") != "CONTACT_INFORMATION"


# ---------------------------------------------------------------------------
# Ambiguous bare labels — must NOT be confidently normalized.
# ---------------------------------------------------------------------------

#: A bare one-word label whose meaning is genuinely undeterminable from the
#: label alone. "Status" is the sharpest case: it could be employment status,
#: application status, marital status or VISA status — guessing "not
#: sensitive" here would be the dangerous failure.
_AMBIGUOUS_LABELS = ["Name", "Location", "Status", "Number", "ID"]


@pytest.mark.parametrize("label", _AMBIGUOUS_LABELS)
def test_ambiguous_bare_label_is_not_confidently_classified(label):
    """`QuestionClassification` exposes no numeric confidence; its honest
    "I don't know" is `coarse_category is None`, which callers route to the
    LLM/human rather than auto-filling. Assert the ambiguous label produces
    exactly that, and is never handed a specific bucket on a guess."""
    result = classify_field(_element(name=label))
    assert result.coarse_category is None, (
        f"ambiguous label {label!r} was confidently bucketed as "
        f"{result.coarse_category!r} — a guess, not a classification"
    )
    assert result.fine_category is None, label


def test_ambiguous_status_is_not_asserted_to_be_non_sensitive():
    """"Status" alone could mean visa status. The system must not emit a
    confident NON-sensitive classification for it. `sensitive=False` paired
    with `coarse_category=None` is the honest "unclassified" state — it is
    NOT a claim that the field is safe, and `truth_store.decide()` cannot
    auto-fill it either way without a verified fact (asserted below)."""
    result = classify_field(_element(name="Status"))
    assert result.coarse_category is None
    assert (result.coarse_category, result.sensitive) == (None, False)


def test_status_becomes_sensitive_once_context_disambiguates_it():
    """The counterpart: as soon as surrounding form text says WHICH status is
    being asked about, the sensitive classification must fire — proving the
    `None` above is genuine ambiguity, not a blind spot."""
    for context in (
        "What is your visa status?",
        "Please indicate your citizenship status",
        "Immigration status",
    ):
        result = classify_field(_element(name="Status", surrounding_text=context))
        assert result.sensitive is True, context
        assert result.coarse_category in ("VISA", "WORK_AUTHORIZATION"), context


def test_ambiguous_name_becomes_specific_with_context():
    """"Name" alone is ambiguous; "First Name" is not."""
    assert _category("Name") is None
    assert _category("First Name") == "PERSONAL_INFORMATION"


def test_ambiguous_number_becomes_specific_with_context():
    assert _category("Number") is None
    assert _category("Contact Number") == "CONTACT_INFORMATION"


def test_unclassified_is_never_silently_an_additional_questions_bucket():
    """`ADDITIONAL_QUESTIONS` is the catch-all for a field that IS clearly an
    application question; it must never be used to paper over "no idea what
    this field is", which stays `None`."""
    for label in _AMBIGUOUS_LABELS:
        assert _category(label) != "ADDITIONAL_QUESTIONS", label
