"""Unit tests for `automation.agents.autonomous.truth_store`."""

from automation.agents.autonomous.truth_store import TruthStore, VerifiedFact, decide


def test_confirmed_answer_outranks_profile():
    store = TruthStore(
        profile_facts={"expected salary": "100000"},
        confirmed_answers={"expected salary": "120000"},
    )
    fact = store.resolve("expected salary")
    assert fact.source == "USER_CONFIRMED"
    assert fact.value == "120000"


def test_profile_outranks_resume():
    store = TruthStore(profile_facts={"email": "a@example.com"}, resume_facts={"email": "b@example.com"})
    assert store.resolve("email").source == "USER_PROFILE"


def test_resolve_any_tries_aliases_in_order():
    store = TruthStore(profile_facts={"mobile number": "555-1234"})
    fact = store.resolve_any(["phone", "mobile number", "contact number"])
    assert fact is not None and fact.value == "555-1234"


def test_unresolved_field_returns_none():
    store = TruthStore()
    assert store.resolve("years of experience") is None


def test_verified_field_autofills():
    fact = VerifiedFact("Jane", "USER_PROFILE", True)
    decision = decide(fact=fact, coarse_category="PERSONAL_INFORMATION", sensitive=False)
    assert decision.can_answer is True
    assert decision.autofill_permitted is True
    assert decision.requires_human is False


def test_sensitive_field_never_autofills_without_a_direct_user_source():
    fact = VerifiedFact("yes", "DETERMINISTIC_DERIVATION", True)
    decision = decide(fact=fact, coarse_category="WORK_AUTHORIZATION", sensitive=True)
    assert decision.autofill_permitted is False
    assert decision.requires_human is True


def test_sensitive_field_confirmed_by_user_is_permitted():
    fact = VerifiedFact("yes", "USER_CONFIRMED", True)
    decision = decide(fact=fact, coarse_category="WORK_AUTHORIZATION", sensitive=True)
    assert decision.autofill_permitted is True
    assert decision.requires_human is False


def test_sensitive_field_with_no_fact_never_inferred():
    decision = decide(fact=None, coarse_category="VISA", sensitive=True)
    assert decision.can_answer is False
    assert decision.autofill_permitted is False
    assert decision.requires_human is True


def test_open_ended_question_may_generate_but_flagged_for_review():
    decision = decide(fact=None, coarse_category="ADDITIONAL_QUESTIONS", sensitive=False, llm_can_generate=True)
    assert decision.can_answer is True
    assert decision.fact.source == "LLM_GENERATED"
    assert decision.fact.verified is False
    assert decision.autofill_permitted is False
    assert decision.requires_human is True


def test_unanswerable_non_sensitive_field_requires_human():
    decision = decide(fact=None, coarse_category="EDUCATION", sensitive=False)
    assert decision.can_answer is False
    assert decision.requires_human is True
