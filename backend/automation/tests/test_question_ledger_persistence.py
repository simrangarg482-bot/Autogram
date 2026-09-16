"""
Persistence for the autonomous path's per-question ledger: the new
`AutonomousTask.question_ledger` JSONB column and
`autonomous_task_repository.record_question_ledger_entry`.

No database here — the repository function's whole job is the JSONB
change-tracking pattern (reassign the dict, never mutate it in place) the
other JSONB writers in that module use, and that is testable against a
session double.
"""

from app.models.db_models import AutonomousTask
from app.services import autonomous_task_repository as task_repo
from automation.agents.autonomous.question_ledger import QuestionLedger, QuestionLedgerEntry


class FakeSession:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1

    def refresh(self, obj):
        pass


class FakeTask:
    def __init__(self, question_ledger=None):
        self.question_ledger = question_ledger
        self.field_attempt_ledger = {"existing": {"attempts": 1}}
        self.updated_at = None


def _entry(label="Email"):
    return QuestionLedgerEntry(
        question_id="q1", page_id="agent_task_1", element_ref=3, raw_label=label,
        normalized_category="CONTACT_INFORMATION", field_type="text",
        answer="jane@example.com", answer_source="user_profile",
        answer_provenance="USER_PROFILE", confidence=1.0, sensitivity=False,
        status="RESOLVED_AUTO",
    ).as_dict()


def test_column_exists_next_to_the_other_per_task_jsonb_columns():
    columns = AutonomousTask.__table__.columns
    assert "question_ledger" in columns
    question_ledger = columns["question_ledger"]
    assert question_ledger.nullable is False
    # Same shape as its neighbours (`field_attempt_ledger` et al.).
    assert type(question_ledger.type) is type(columns["field_attempt_ledger"].type)
    assert question_ledger.default.arg(None) == {}


def test_record_question_ledger_entry_upserts_and_reassigns_the_dict():
    db, task = FakeSession(), FakeTask(question_ledger={"a": _entry("Phone")})
    before = task.question_ledger

    task_repo.record_question_ledger_entry(db, task, "b", _entry())

    assert set(task.question_ledger) == {"a", "b"}
    assert task.question_ledger is not before, "must reassign so SQLAlchemy sees the column as dirty"
    assert db.commits == 1
    assert task.field_attempt_ledger == {"existing": {"attempts": 1}}, "the existing ledger is untouched"


def test_record_question_ledger_entry_handles_a_null_column():
    db, task = FakeSession(), FakeTask(question_ledger=None)
    task_repo.record_question_ledger_entry(db, task, "a", _entry())
    assert list(task.question_ledger) == ["a"]


def test_a_persisted_ledger_round_trips_back_into_the_in_memory_ledger():
    db, task = FakeSession(), FakeTask(question_ledger={})
    task_repo.record_question_ledger_entry(db, task, "a", _entry())

    rehydrated = QuestionLedger.from_dict(task.question_ledger)
    entry = rehydrated.get("a")
    assert entry is not None
    assert entry.answer_provenance == "USER_PROFILE"
    assert "verified profile" in entry.explain()
