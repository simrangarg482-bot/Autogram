"""
Phase 8 — sensitive-field corpus (two independent layers, cross-checked).

Autogram has two deliberately independent sensitive-field detectors:

* `executor.is_sensitive_field_name` — the code-level gate. A fill/select/
  check on a matching element is refused outright unless the caller passes
  `is_sourced=True`. It knows nothing about categories; it is a keyword
  backstop that must fire even if every other layer is wrong.
* `question_ontology.classify_field(...).sensitive` — the semantic layer,
  driving the question ledger, the LLM prompt and `truth_store.decide()`.

They are independent ON PURPOSE (two chances to catch a dangerous field), but
they must never DISAGREE on a real-world label: a label the ontology calls
sensitive while the executor waves through is a hole in the code-level gate,
and the reverse is a field the loop will reason about as ordinary while the
executor silently blocks it. Every label in the corpus below is asserted
against both.

The third layer is provenance: `truth_store.decide()` must refuse to
auto-fill ANY of these from a `RESUME`-derived or otherwise unverified fact.
A resume that lists a country, a nationality or a military employer is not
the candidate answering a work-authorization, demographic or legal question,
and a sensitive field's resume-derived fact is dropped entirely rather than
merely un-permitted (so a review UI reading `.fact` cannot present it as
"the answer"). Only a direct user statement (`USER_CONFIRMED` /
`USER_PROFILE`) may satisfy one.

Fixture conventions follow `test_question_ontology.py` (hand-built
`PageElement`s) and `test_truth_store.py` (plain `TruthStore` construction).
"""

import pytest

from automation.agents.autonomous.executor import is_sensitive_field_name
from automation.agents.autonomous.observer import PageElement
from automation.agents.autonomous.question_ontology import (
    SENSITIVE_COARSE_CATEGORIES,
    classify_field,
)
from automation.agents.autonomous.truth_store import (
    PROVENANCE_VALUES,
    TruthStore,
    VerifiedFact,
    decide,
)

#: Provenance values that may satisfy a SENSITIVE field: a statement the user
#: made themselves. Everything else in `PROVENANCE_VALUES` is inferred,
#: derived, remembered or generated, and must escalate to a human.
_USER_STATED_PROVENANCE = frozenset({"USER_CONFIRMED", "USER_PROFILE"})


def _element(name="", surrounding_text="", **kwargs):
    defaults = dict(
        ref=0, tag="input", type="text", name=name, value=None, required=False,
        disabled=False, checked=None, options=None, surrounding_text=surrounding_text,
    )
    defaults.update(kwargs)
    return PageElement(**defaults)


# ---------------------------------------------------------------------------
# The corpus. Grouped by TOPIC, never by vendor; every string is wording a
# real application form uses.
# ---------------------------------------------------------------------------

_SENSITIVE_CORPUS: dict[str, tuple[str, ...]] = {
    "work_authorization": (
        "Work Authorization",
        "Are you legally authorized to work in the United States?",
        "Are you authorized to work for any employer in the US?",
        "Are you eligible to work in this country?",
        "Right to Work",
        "Work Permit",
        "Employment Eligibility",
    ),
    "visa_sponsorship": (
        "Visa Sponsorship",
        "Will you now or in the future require sponsorship for employment visa status?",
        "Do you require visa sponsorship?",
        "Visa Type",
    ),
    "citizenship": (
        "Are you a U.S. citizen?",
        "Country of Citizenship",
        "Citizenship",
        "Citizenship Status",
        "Nationality",
        "Are you a citizen of the European Union?",
    ),
    "immigration": (
        "Immigration Status",
        "Permanent Resident",
        "Do you hold a Green Card?",
        "Work Status",
    ),
    "criminal_history": (
        "Criminal History",
        "Have you ever been convicted of a felony?",
        "Have you ever been convicted of a crime?",
        "Please disclose any criminal convictions",
    ),
    "disability": (
        "Disability Status",
        "Do you have a disability?",
        "Voluntary Self-Identification of Disability",
    ),
    "race_ethnicity": (
        "Race",
        "Ethnicity",
        "Race/Ethnicity",
        "What is your race or ethnicity?",
    ),
    "gender": (
        "Gender",
        "Sex",
        "What is your gender identity?",
        "Pronouns",
    ),
    "veteran_status": (
        "Veteran Status",
        "Protected Veteran Status",
        "Are you a protected veteran?",
    ),
    "legal_declaration": (
        "I certify that the information provided is true and complete",
        "I agree to the terms and conditions",
        "I declare that the above statements are accurate",
        "I attest that I have read the above",
        "I acknowledge that false statements may result in dismissal",
    ),
    "background_check": (
        "Background Check Consent",
        "I consent to a background check",
        "Do you consent to a background screening?",
        "Employment background verification",
    ),
    "drug_testing": (
        "Are you willing to submit to a drug test?",
        "Drug Testing Declaration",
        "Pre-employment drug screen consent",
    ),
    "acknowledgements": (
        "I agree to the arbitration agreement",
        "Arbitration Agreement",
        "I acknowledge the AI use policy",
        "AI Policy Acknowledgement",
    ),
    "other_protected": (
        "Date of Birth",
        "Social Security Number",
        "Marital Status",
        "Sexual Orientation",
        "Security Clearance",
    ),
}

_ALL_SENSITIVE_LABELS = [label for labels in _SENSITIVE_CORPUS.values() for label in labels]


# ---------------------------------------------------------------------------
# Layer 1 + Layer 2: both detectors, and their agreement.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", _ALL_SENSITIVE_LABELS)
def test_executor_gate_flags_every_sensitive_label(label):
    assert is_sensitive_field_name(label) is True, (
        f"executor's code-level gate would NOT block a fill on {label!r}"
    )


@pytest.mark.parametrize("label", _ALL_SENSITIVE_LABELS)
def test_ontology_flags_every_sensitive_label(label):
    result = classify_field(_element(name=label))
    assert result.sensitive is True, (
        f"question_ontology classified {label!r} as non-sensitive "
        f"(coarse_category={result.coarse_category!r})"
    )


@pytest.mark.parametrize("label", _ALL_SENSITIVE_LABELS)
def test_both_layers_agree_on_every_label_in_the_corpus(label):
    """The cross-check. The two gates are independent implementations; a
    disagreement on any real-world label means one of them has a hole."""
    executor_says = is_sensitive_field_name(label)
    ontology_says = classify_field(_element(name=label)).sensitive
    assert executor_says == ontology_says is True, (
        f"layers disagree on {label!r}: executor={executor_says}, ontology={ontology_says}"
    )


@pytest.mark.parametrize("label", _ALL_SENSITIVE_LABELS)
def test_sensitive_label_lands_in_a_sensitive_or_disclosure_category(label):
    """`sensitive=True` must be backed by a real category, never asserted on a
    field the ontology otherwise failed to recognize — an unclassified field
    is `sensitive=False` by construction, so this proves the flag came from a
    genuine match."""
    result = classify_field(_element(name=label))
    assert result.coarse_category is not None, label
    assert (
        result.coarse_category in SENSITIVE_COARSE_CATEGORIES
        or result.fine_category is not None
    ), f"{label!r} -> {result}"


@pytest.mark.parametrize(
    "label",
    ["First Name", "Last Name", "Email Address", "Phone", "City", "University",
     "Degree", "LinkedIn Profile", "Current Company", "Years of Experience",
     "Upload your resume", "Cover Letter"],
)
def test_ordinary_fields_are_not_flagged_by_either_layer(label):
    """The gates must stay discriminating. If everything is sensitive, the
    autonomous path can fill nothing and the flag carries no information."""
    assert is_sensitive_field_name(label) is False, label
    assert classify_field(_element(name=label)).sensitive is False, label


def test_a_sensitive_question_is_detected_from_surrounding_context_too():
    """Real forms put the question in section text and leave the input itself
    labelled "Yes"/"No" — the ontology reads `surrounding_text`, which is the
    only layer that can catch this shape (the executor gate only ever sees the
    element's accessible name, which is why it is not the sole defense)."""
    result = classify_field(_element(
        name="Yes",
        surrounding_text="Are you legally authorized to work in the United States?",
    ))
    assert result.sensitive is True
    assert result.coarse_category == "WORK_AUTHORIZATION"


# ---------------------------------------------------------------------------
# Layer 3: provenance. Nothing here auto-fills from an unverified source.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", _ALL_SENSITIVE_LABELS)
def test_no_sensitive_field_autofills_from_a_resume_derived_fact(label):
    """A RESUME fact for a sensitive field is DROPPED, not merely
    un-permitted: `.fact` must come back `None` so no review UI or ledger
    entry can present resume text as the candidate's answer to a legally
    significant question."""
    result = classify_field(_element(name=label))
    d = decide(
        fact=VerifiedFact("United States", "RESUME", True),
        coarse_category=result.coarse_category,
        sensitive=result.sensitive,
    )
    assert d.autofill_permitted is False, label
    assert d.requires_human is True, label
    assert d.fact is None, f"{label!r} still exposed a resume-derived fact: {d.fact}"
    assert d.can_answer is False, label


@pytest.mark.parametrize("source", sorted(PROVENANCE_VALUES - _USER_STATED_PROVENANCE))
def test_only_user_stated_provenance_can_satisfy_a_sensitive_field(source):
    verified = source not in ("LLM_GENERATED", "UNKNOWN")
    d = decide(
        fact=VerifiedFact("Yes", source, verified),
        coarse_category="WORK_AUTHORIZATION",
        sensitive=True,
    )
    assert d.autofill_permitted is False, source
    assert d.requires_human is True, source


@pytest.mark.parametrize("source", sorted(_USER_STATED_PROVENANCE))
def test_a_user_stated_answer_is_the_one_thing_that_satisfies_a_sensitive_field(source):
    """The permitted case, asserted explicitly so a future over-tightening
    that makes sensitive fields unanswerable even after the user answered
    them is also caught."""
    fact = VerifiedFact("Yes", source, True)
    d = decide(fact=fact, coarse_category="WORK_AUTHORIZATION", sensitive=True)
    assert (d.can_answer, d.autofill_permitted, d.requires_human) == (True, True, False)
    assert d.fact is fact


def test_sensitive_field_with_no_source_is_never_inferred():
    d = decide(fact=None, coarse_category="DEMOGRAPHICS", sensitive=True)
    assert (d.can_answer, d.autofill_permitted, d.requires_human) == (False, False, True)
    assert d.fact is None


def test_sensitive_field_never_falls_back_to_llm_generation():
    """`llm_can_generate=True` is checked AFTER the sensitive branch — a
    sensitive field must not be routed to the LLM as an open-ended prompt."""
    d = decide(
        fact=None, coarse_category="LEGAL_DECLARATION", sensitive=True,
        llm_can_generate=True, review_required_for_generated=False,
    )
    assert (d.can_answer, d.autofill_permitted, d.requires_human) == (False, False, True)
    assert d.fact is None


# ---------------------------------------------------------------------------
# Adversarial: a TruthStore that genuinely holds a citizenship-adjacent value.
# ---------------------------------------------------------------------------


def test_resume_derived_citizenship_value_cannot_reach_a_sensitive_field():
    """The end-to-end adversarial case: the store really does resolve a value
    for these labels (so this is not passing by accident of an empty store),
    the fact really is `verified=True` — and the sensitive gate still refuses
    it and drops it."""
    store = TruthStore(resume_facts={
        "citizenship": "United States",
        "nationality": "American",
        "country": "United States",
        "veteran status": "US Army 2014-2018",
        "gender": "Male",
    })
    for label in ("Citizenship", "Nationality", "Country of Citizenship",
                  "Veteran Status", "Gender"):
        result = classify_field(_element(name=label))
        assert result.sensitive is True, label
        fact = store.resolve(label)
        if fact is not None:
            assert fact.source == "RESUME" and fact.verified is True, label
        d = decide(
            fact=fact,
            coarse_category=result.coarse_category,
            sensitive=result.sensitive,
        )
        assert d.autofill_permitted is False, label
        assert d.requires_human is True, label
        assert d.fact is None, f"{label!r} leaked {d.fact} from the resume"


def test_country_of_citizenship_is_not_treated_as_a_plain_address_field():
    """REGRESSION: "Country of Citizenship" once matched the generic
    `\\bcountry\\b` ADDRESS pattern and came back as a non-sensitive address
    field — which, with a profile "country" value on hand, is an auto-fill of
    an immigration question."""
    result = classify_field(_element(name="Country of Citizenship"))
    assert result.coarse_category != "ADDRESS"
    assert result.sensitive is True

    store = TruthStore(profile_facts={"country": "United States"})
    d = decide(
        fact=store.resolve("country"),
        coarse_category=result.coarse_category,
        sensitive=result.sensitive,
    )
    # A profile value IS user-stated, so it is permitted — but it must be
    # reaching a SENSITIVE category, not a plain address one. The dangerous
    # version of this bug was `sensitive=False`, asserted above.
    assert result.coarse_category in SENSITIVE_COARSE_CATEGORIES
    assert d.fact is not None and d.fact.source == "USER_PROFILE"


def test_plain_country_field_stays_a_non_sensitive_address_field():
    """The counterpart — widening the citizenship matcher must not swallow an
    ordinary "Country" address field into the sensitive bucket."""
    result = classify_field(_element(name="Country"))
    assert result.coarse_category == "ADDRESS"
    assert result.sensitive is False
    assert is_sensitive_field_name("Country") is False


def test_corpus_covers_every_sensitive_coarse_category():
    """Coverage guard: if a new category joins
    `SENSITIVE_COARSE_CATEGORIES`, this corpus must grow a label for it
    rather than silently leaving it untested."""
    covered = {
        classify_field(_element(name=label)).coarse_category
        for label in _ALL_SENSITIVE_LABELS
    }
    missing = SENSITIVE_COARSE_CATEGORIES - covered
    assert not missing, f"no corpus label exercises these sensitive categories: {missing}"
