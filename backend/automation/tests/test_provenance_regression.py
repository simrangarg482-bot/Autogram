"""
Phase 7 — provenance regression matrix.

One explicit test per provenance value for `truth_store.decide()`, asserting
the three outputs that together decide whether a value is ever typed into a
real employer's form: `autofill_permitted`, `requires_human`, and
`fact.verified`.

The load-bearing regression here is the LLM_GENERATED row. `decide()` takes a
`review_required_for_generated` parameter which a caller could once pass as
`False` to get `autofill_permitted=True` on a fact whose `verified` is
`False` — an LLM's guess silently promoted to a verified user fact, which is
precisely what the provenance model exists to prevent. That gate is now
hard-coded, and the tests below prove it by passing every value the parameter
accepts (including keyword forms a future caller might use) and asserting the
outcome does not move.

The second regression is `QuestionLedgerEntry.explain()`. Its "not a verified
user fact" caveat used to be conditional on the entry's `requires_human`
flag, so an entry recorded with `answer_provenance="UNKNOWN",
requires_human=False` (which `loop.py::_record_question_ledger` does whenever
a value was written for a field the truth store could not resolve) rendered a
sentence a human or a review UI would read as settled and verified. The
caveat is now driven by the PROVENANCE, so these tests assert it appears for
entries with `requires_human=True` and `requires_human=False` alike.

Fixture conventions follow `test_truth_store.py` / `test_question_ledger.py`:
plain dataclasses, no browser, no DB.
"""

import pytest

from automation.agents.autonomous import truth_store as truth_store_module
from automation.agents.autonomous.question_ledger import (
    _VERIFIED_PROVENANCE_TEXT,
    QuestionLedgerEntry,
)
from automation.agents.autonomous.truth_store import (
    PROVENANCE_VALUES,
    AnswerDecision,
    TruthStore,
    VerifiedFact,
    decide,
)

#: Text the LLM_GENERATED / UNKNOWN caveat must contain. Taken verbatim from
#: the current `explain()` implementation rather than paraphrased, so a
#: reword that accidentally drops the warning fails here.
_UNVERIFIED_CAVEAT = "NOT a verified user fact and requires human review"


def _entry(**overrides) -> QuestionLedgerEntry:
    defaults = dict(
        question_id="q1",
        page_id="p1",
        element_ref=3,
        raw_label="Why do you want to work here?",
        normalized_category="ADDITIONAL_QUESTIONS",
        field_type="textarea",
        answer="Because I admire the team.",
        answer_source="llm",
        answer_provenance="LLM_GENERATED",
        confidence=0.4,
        sensitivity=False,
        status="PENDING_HUMAN",
    )
    defaults.update(overrides)
    return QuestionLedgerEntry(**defaults)


# ---------------------------------------------------------------------------
# One row per provenance value, non-sensitive field.
# ---------------------------------------------------------------------------


def test_profile_fact_is_verified_and_autofills():
    fact = VerifiedFact("Ada", "USER_PROFILE", True)
    d = decide(fact=fact, coarse_category="PERSONAL_INFORMATION", sensitive=False)
    assert (d.can_answer, d.autofill_permitted, d.requires_human) == (True, True, False)
    assert d.fact is fact and d.fact.verified is True


def test_resume_fact_is_verified_and_autofills_for_a_non_sensitive_field():
    fact = VerifiedFact("ada@example.com", "RESUME", True)
    d = decide(fact=fact, coarse_category="CONTACT_INFORMATION", sensitive=False)
    assert (d.can_answer, d.autofill_permitted, d.requires_human) == (True, True, False)
    assert d.fact.verified is True


def test_user_confirmed_answer_is_verified_and_autofills():
    fact = VerifiedFact("Yes", "USER_CONFIRMED", True)
    d = decide(fact=fact, coarse_category="ADDITIONAL_QUESTIONS", sensitive=False)
    assert (d.can_answer, d.autofill_permitted, d.requires_human) == (True, True, False)
    assert d.fact.verified is True


def test_approved_answer_memory_is_verified_and_autofills():
    fact = VerifiedFact("2 weeks", "APPROVED_ANSWER_MEMORY", True)
    d = decide(fact=fact, coarse_category="ADDITIONAL_QUESTIONS", sensitive=False)
    assert (d.can_answer, d.autofill_permitted, d.requires_human) == (True, True, False)
    assert d.fact.verified is True


def test_safe_deterministic_derivation_is_verified_and_autofills():
    """A value produced by a deterministic rule (not free-form generation) is
    trusted exactly like a stored fact — this is the row that distinguishes
    "derived" from "generated"."""
    fact = VerifiedFact("5", "DETERMINISTIC_DERIVATION", True)
    d = decide(fact=fact, coarse_category="WORK_EXPERIENCE", sensitive=False)
    assert (d.can_answer, d.autofill_permitted, d.requires_human) == (True, True, False)
    assert d.fact.verified is True


def test_llm_generated_answer_is_unverified_and_never_autofills():
    d = decide(fact=None, coarse_category="ADDITIONAL_QUESTIONS", sensitive=False, llm_can_generate=True)
    assert d.can_answer is True, "the system CAN draft an answer here"
    assert d.autofill_permitted is False
    assert d.requires_human is True
    assert d.fact is not None
    assert d.fact.source == "LLM_GENERATED"
    assert d.fact.verified is False


def test_unknown_answer_cannot_be_answered_and_requires_a_human():
    d = decide(fact=None, coarse_category=None, sensitive=False, llm_can_generate=False)
    assert (d.can_answer, d.autofill_permitted, d.requires_human) == (False, False, True)
    assert d.fact is None


def test_a_fact_carrying_an_untrusted_provenance_never_autofills():
    """Defensive branch: `TruthStore.resolve` cannot produce this, but a fact
    rehydrated from persisted state could. It must fail closed."""
    for source in ("LLM_GENERATED", "UNKNOWN"):
        fact = VerifiedFact("something", source, False)
        d = decide(fact=fact, coarse_category="ADDITIONAL_QUESTIONS", sensitive=False)
        assert d.autofill_permitted is False, source
        assert d.requires_human is True, source


# ---------------------------------------------------------------------------
# THE critical regression: the LLM_GENERATED hard-code.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("review_flag", [True, False, None, 0, 1, "", "no"])
def test_llm_generated_is_hardcoded_regardless_of_review_required_flag(review_flag):
    """REGRESSION: `review_required_for_generated` must be advisory only. No
    value a caller can pass — truthy, falsy, or the historic `False` that used
    to flip the gate — may produce `autofill_permitted=True` or
    `requires_human=False` for an LLM-generated answer."""
    d = decide(
        fact=None,
        coarse_category="ADDITIONAL_QUESTIONS",
        sensitive=False,
        llm_can_generate=True,
        review_required_for_generated=review_flag,
    )
    assert d.autofill_permitted is False, review_flag
    assert d.requires_human is True, review_flag
    assert d.fact.verified is False, review_flag
    assert d.fact.source == "LLM_GENERATED", review_flag


def test_llm_generated_hardcode_holds_for_every_coarse_category():
    """The gate is not category-conditional — an open-ended cover-letter
    prompt is as un-autofillable as anything else when the answer is
    generated."""
    for category in ("COVER_LETTER", "ADDITIONAL_QUESTIONS", "SKILLS", "ASSESSMENT", None):
        d = decide(
            fact=None, coarse_category=category, sensitive=False,
            llm_can_generate=True, review_required_for_generated=False,
        )
        assert (d.autofill_permitted, d.requires_human) == (False, True), category


def test_decide_returns_an_answer_decision_with_a_nonempty_reason():
    """Every refusal must carry a human-readable WHY — the ledger renders it
    and a support session relies on it."""
    d = decide(fact=None, coarse_category="COVER_LETTER", sensitive=False, llm_can_generate=True)
    assert isinstance(d, AnswerDecision)
    assert d.reason.strip()
    assert "unverified" in d.reason or "human review" in d.reason


def test_no_decision_ever_pairs_autofill_permitted_with_an_unverified_fact():
    """The invariant the whole module exists to hold, asserted across the
    entire decision surface at once."""
    cases = []
    for source in sorted(PROVENANCE_VALUES):
        verified = source not in ("LLM_GENERATED", "UNKNOWN")
        for sensitive in (True, False):
            cases.append(decide(
                fact=VerifiedFact("v", source, verified),
                coarse_category="ADDITIONAL_QUESTIONS", sensitive=sensitive,
            ))
    for sensitive in (True, False):
        for gen in (True, False):
            for flag in (True, False):
                cases.append(decide(
                    fact=None, coarse_category="ADDITIONAL_QUESTIONS",
                    sensitive=sensitive, llm_can_generate=gen,
                    review_required_for_generated=flag,
                ))
    for d in cases:
        if d.autofill_permitted:
            assert d.fact is not None and d.fact.verified is True, d
            assert d.requires_human is False, d


def test_truth_store_never_resolves_an_unverified_fact():
    """`decide()`'s trust in `fact.verified` is only sound because the store
    itself cannot mint an unverified one."""
    store = TruthStore(
        profile_facts={"First Name": "Ada"},
        resume_facts={"Email": "ada@example.com"},
        confirmed_answers={"Notice Period": "2 weeks"},
        answer_memory={"Expected Salary": "100000"},
    )
    for label in ("First Name", "Email", "Notice Period", "Expected Salary"):
        fact = store.resolve(label)
        assert fact is not None, label
        assert fact.verified is True, label
        assert fact.source in truth_store_module._VERIFIED_PROVENANCE, label
    assert store.resolve("Nothing Known About This") is None


# ---------------------------------------------------------------------------
# QuestionLedgerEntry.explain() — caveat driven by provenance, not by a flag.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("requires_human", [True, False])
@pytest.mark.parametrize("provenance", ["LLM_GENERATED", "UNKNOWN"])
def test_explain_flags_an_unverified_entry_regardless_of_requires_human(provenance, requires_human):
    """REGRESSION: the caveat used to be emitted only when `requires_human`
    was True, so an `UNKNOWN`-provenance entry recorded with
    `requires_human=False` rendered as if it were a settled, verified answer."""
    text = _entry(answer_provenance=provenance, requires_human=requires_human).explain()
    assert _UNVERIFIED_CAVEAT in text, text


def test_explain_names_the_llm_as_the_source_not_a_user_fact():
    text = _entry(answer_provenance="LLM_GENERATED", requires_human=False).explain()
    assert "LLM-generated answer (not a verified user fact)" in text
    assert _UNVERIFIED_CAVEAT in text


def test_explain_for_a_verified_entry_carries_no_unverified_caveat():
    """The other direction — a genuinely verified answer must not be smeared
    with a warning it doesn't deserve, or the warning stops meaning anything."""
    text = _entry(
        answer_provenance="USER_CONFIRMED", answer_source="confirmed_answer",
        requires_human=False, status="RESOLVED_CONFIRMED",
    ).explain()
    assert _UNVERIFIED_CAVEAT not in text
    assert "an answer the user explicitly confirmed for this task" in text
    assert "requires human review" not in text


def test_explain_for_a_verified_entry_still_reports_a_pending_human_review():
    text = _entry(
        answer_provenance="USER_PROFILE", answer_source="profile",
        requires_human=True, status="PENDING_HUMAN",
    ).explain()
    assert "still requires human review" in text
    assert _UNVERIFIED_CAVEAT not in text


def test_explain_for_an_unanswered_entry_makes_no_claim_at_all():
    text = _entry(answer=None, answer_source=None, answer_provenance="UNKNOWN").explain()
    assert "has not been answered yet" in text


@pytest.mark.parametrize("provenance", sorted(PROVENANCE_VALUES))
def test_explain_renders_every_provenance_value_without_leaking_the_raw_token(provenance):
    """Each provenance has a prose rendering; a new value added to
    `PROVENANCE_VALUES` without one would show a raw enum token to a human."""
    text = _entry(answer_provenance=provenance).explain()
    assert provenance not in text, f"raw provenance token leaked into explain(): {text}"


def test_ledger_verified_provenance_text_stays_in_sync_with_truth_store():
    """`question_ledger.py` deliberately does not import `truth_store.py`, so
    its local copy of the verified-provenance set can drift. It must not —
    a provenance missing from the ledger's copy would render a caveat on a
    verified answer, and one wrongly added would silently suppress the caveat
    on an unverified one. (`question_ledger.py`'s own docstring names this
    test as the guard.)"""
    assert _VERIFIED_PROVENANCE_TEXT == truth_store_module._VERIFIED_PROVENANCE


def test_verified_provenance_is_exactly_provenance_values_minus_the_unverified_two():
    assert truth_store_module._VERIFIED_PROVENANCE == PROVENANCE_VALUES - {"LLM_GENERATED", "UNKNOWN"}
