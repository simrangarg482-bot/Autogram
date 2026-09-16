"""Unit tests for `automation.agents.autonomous.question_ledger`."""

from automation.agents.autonomous.question_ledger import QuestionLedger, QuestionLedgerEntry


def _entry(**kwargs):
    defaults = dict(
        question_id="q1", page_id="page-abc", element_ref=12, raw_label="Phone",
        normalized_category="CONTACT_INFORMATION", field_type="text", answer="555-1234",
        answer_source="profile", answer_provenance="USER_PROFILE", confidence=0.95,
        sensitivity=False, status="RESOLVED_AUTO",
    )
    defaults.update(kwargs)
    return QuestionLedgerEntry(**defaults)


def test_round_trips_through_dict():
    ledger = QuestionLedger()
    ledger.record("field-1", _entry())
    restored = QuestionLedger.from_dict(ledger.to_dict())
    assert restored.get("field-1").raw_label == "Phone"


def test_explain_includes_provenance_text():
    ledger = QuestionLedger()
    ledger.record("field-1", _entry())
    explanation = ledger.get("field-1").explain()
    assert "555-1234" in explanation
    assert "verified profile" in explanation


def test_explain_for_llm_generated_answer_flags_review():
    entry = _entry(
        raw_label="Why do you want to work here?", answer="Because...", answer_source="llm",
        answer_provenance="LLM_GENERATED", confidence=0.4, requires_human=True, status="PENDING_HUMAN",
    )
    explanation = entry.explain()
    assert "LLM-generated" in explanation
    assert "requires human review" in explanation


def test_unanswered_entry_explains_itself():
    entry = _entry(answer=None, status="PENDING_HUMAN")
    assert "has not been answered yet" in entry.explain()


def test_attempt_count_never_decreases_on_re_record():
    ledger = QuestionLedger()
    ledger.record("field-1", _entry(attempt_count=2))
    ledger.record("field-1", _entry(attempt_count=1))
    assert ledger.get("field-1").attempt_count == 2


def test_pending_human_entries_filters_correctly():
    ledger = QuestionLedger()
    ledger.record("field-1", _entry(requires_human=True, status="PENDING_HUMAN"))
    ledger.record("field-2", _entry(requires_human=False, status="RESOLVED_AUTO"))
    pending = ledger.pending_human_entries()
    assert len(pending) == 1


def test_from_dict_skips_malformed_rows_without_crashing():
    ledger = QuestionLedger.from_dict({"bad-row": {"unexpected_key": 1}})
    assert ledger.all_entries() == []
