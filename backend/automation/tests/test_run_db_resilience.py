"""
A run must not depend on a database session that outlived its connection.

The reported failure (Warp/Greenhouse, application 46665f90): the run held ONE
SQLAlchemy session across a ~405s browser phase. Neon dropped the idle connection,
and the first later touch of an ORM object - `getattr(self.profile, "profile_id")`
at `answer_engine.py:737`, which looks like a plain attribute read - raised
`PendingRollbackError`. `answer_batch` raised, `base.py` swallowed it at debug
level and abandoned EVERY question in the batch, so nothing was filled and nothing
in the log said why.

Pinned here, one behaviour per test:

- the engine keeps working when the ORM profile it was handed can no longer be read;
- a dead session is recovered and retried exactly once (and only for a dead session);
- saved demographics can be preloaded, so the browser phase needs no DB for them;
- demographics are loaded through the user's OWN profile and ownership is verified;
- one question failing no longer discards the answers to the others, and the
  failure is logged at WARNING with the real exception.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import PendingRollbackError

from app.core.crypto import encrypt_field
from app.models.db_models import CandidateDemographics, CandidateProfile, ProfileDocument
from app.services import profile_repository as repo
from automation.ats.base import _AUTOMATION_EXAMINED_ATTR
from automation.ats.greenhouse.greenhouse_adapter import GreenhouseAdapter
from automation.forms import answer_engine as answer_engine_module
from automation.forms.answer_engine import SOURCE_NEEDS_USER_INPUT, AnswerResult, ApplicationAnswerEngine, Question

GH_GENDER = ["Male", "Female", "Decline To Self Identify"]

DEAD = "Can't reconnect until invalid transaction is rolled back.  Please rollback() fully before proceeding"


def _dead_session_error() -> PendingRollbackError:
    return PendingRollbackError(DEAD)


def _llm_must_not_be_called(**_kwargs):
    raise AssertionError("the LLM must never be consulted for a demographic question")


class _ProfileWhoseSessionDied:
    """Stands in for an ORM profile after its session failed: the plain values are
    readable while the engine is being built, and after that ANY attribute read -
    including one with a `getattr` default, exactly as at answer_engine.py:737 -
    raises `PendingRollbackError`."""

    def __init__(self, **values):
        object.__setattr__(self, "_values", values)
        object.__setattr__(self, "_dead", False)

    def kill(self):
        object.__setattr__(self, "_dead", True)

    def __getattr__(self, name):
        if self._dead:
            raise _dead_session_error()
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(name) from None


# ---------------------------------------------------------------------------
# 1. answer_engine.py:737 - no ORM attribute read after construction
# ---------------------------------------------------------------------------

def test_the_engine_answers_a_demographic_question_after_the_profiles_session_died(monkeypatch):
    profile = _ProfileWhoseSessionDied(profile_id="p1", user_id="u1")
    row = CandidateDemographics(id="d1", candidate_id="p1", gender="male")
    monkeypatch.setattr(answer_engine_module, "get_candidate_demographics", lambda db, profile_id: row)
    engine = ApplicationAnswerEngine(profile=profile, db=object(), user_id="u1", llm_fn=_llm_must_not_be_called)
    profile.kill()   # the browser phase ran, the connection dropped, the session is dead

    result = engine.answer_batch([Question("Gender", GH_GENDER)])[0]

    assert result.answer == "Male"          # before: PendingRollbackError escaped answer_batch


def test_the_demographics_lookup_uses_the_profile_id_captured_at_construction(monkeypatch):
    profile = _ProfileWhoseSessionDied(profile_id="p-42", user_id="u1")
    seen = []
    monkeypatch.setattr(
        answer_engine_module, "get_candidate_demographics",
        lambda db, profile_id: seen.append(profile_id),
    )
    engine = ApplicationAnswerEngine(profile=profile, db=object(), user_id="u1", llm_fn=_llm_must_not_be_called)
    profile.kill()

    engine.answer_batch([Question("Gender", GH_GENDER)])

    assert seen == ["p-42"]


# ---------------------------------------------------------------------------
# 2. preloaded plain data: the browser phase needs no DB for demographics
# ---------------------------------------------------------------------------

def test_preloaded_demographics_are_used_with_no_database_at_all(monkeypatch):
    monkeypatch.setattr(
        answer_engine_module, "get_candidate_demographics",
        lambda *a: pytest.fail("a preloaded engine must not query demographics"),
    )
    engine = ApplicationAnswerEngine(
        profile=CandidateProfile(profile_id="p1", user_id="u1"), user_id="u1",
        demographics=CandidateDemographics(id="d1", candidate_id="p1", gender="decline_to_answer"),
        llm_fn=_llm_must_not_be_called,
    )

    assert engine.answer_batch([Question("Gender", GH_GENDER)])[0].answer == "Decline To Self Identify"


def test_preloaded_none_means_nothing_saved_and_is_not_looked_up_again(monkeypatch):
    monkeypatch.setattr(
        answer_engine_module, "get_candidate_demographics",
        lambda *a: pytest.fail("`None` means 'loaded, none saved' - it must not trigger a lookup"),
    )
    engine = ApplicationAnswerEngine(
        profile=CandidateProfile(profile_id="p1", user_id="u1"), user_id="u1", db=object(),
        demographics=None, llm_fn=_llm_must_not_be_called,
    )

    result = engine.answer_batch([Question("Gender", GH_GENDER)])[0]

    assert result.source == SOURCE_NEEDS_USER_INPUT and result.answer == ""


def test_the_engine_refuses_demographics_when_its_user_is_not_the_profiles_owner(monkeypatch, caplog):
    row = CandidateDemographics(id="d1", candidate_id="p1", gender="male")
    monkeypatch.setattr(answer_engine_module, "get_candidate_demographics", lambda db, profile_id: row)
    engine = ApplicationAnswerEngine(
        profile=CandidateProfile(profile_id="p1", user_id="owner"), db=object(), user_id="someone-else",
        llm_fn=_llm_must_not_be_called,
    )

    with caplog.at_level(logging.ERROR):
        result = engine.answer_batch([Question("Gender", GH_GENDER)])[0]

    assert result.answer == "" and result.source == SOURCE_NEEDS_USER_INPUT   # never the other user's answer
    assert "Refusing to use saved demographics" in caplog.text


# ---------------------------------------------------------------------------
# 3. a dead session is recovered and retried once - and only a dead session
# ---------------------------------------------------------------------------

class _Factory:
    """A session factory whose first `failures` sessions die on use."""

    def __init__(self, failures=1):
        self.failures = failures
        self.sessions = []

    def __call__(self):
        session = MagicMock(name=f"session{len(self.sessions) + 1}")
        session.dead = len(self.sessions) < self.failures
        self.sessions.append(session)
        return session


def _engine(factory=None, **kw):
    return ApplicationAnswerEngine(
        profile=CandidateProfile(profile_id="p1", user_id="u1"), user_id="u1",
        session_factory=factory, llm_fn=_llm_must_not_be_called, **kw,
    )


def test_a_dead_session_is_replaced_and_the_operation_retried_once():
    factory = _Factory(failures=1)
    engine = _engine(factory)

    def operation(session):
        if session.dead:
            raise _dead_session_error()
        return "ok"

    assert engine._run_db(operation) == "ok"
    assert len(factory.sessions) == 2                      # a brand-new session for the retry
    assert all(s.close.called for s in factory.sessions)   # and neither is left open


def test_it_retries_only_once():
    factory = _Factory(failures=5)
    engine = _engine(factory)
    calls = []

    def operation(session):
        calls.append(session)
        raise _dead_session_error()

    with pytest.raises(PendingRollbackError):
        engine._run_db(operation)
    assert len(calls) == 2


def test_an_ordinary_error_is_not_retried():
    factory = _Factory(failures=0)
    engine = _engine(factory)
    calls = []

    def operation(session):
        calls.append(1)
        raise ValueError("a genuine query bug")

    with pytest.raises(ValueError):
        engine._run_db(operation)
    assert len(calls) == 1


def test_without_a_factory_the_callers_session_is_rolled_back_then_reused():
    db = MagicMock()
    engine = ApplicationAnswerEngine(profile=CandidateProfile(profile_id="p1", user_id="u1"), db=db, user_id="u1")
    state = {"n": 0}

    def operation(session):
        state["n"] += 1
        if state["n"] == 1:
            raise _dead_session_error()
        return "recovered"

    assert engine._run_db(operation) == "recovered"
    db.rollback.assert_called_once()


def test_a_dead_session_does_not_crash_the_answer_batch(monkeypatch):
    """The reported symptom end to end: the cache lookup and ledger write hit a
    dead session on their first attempt. The batch must still return answers."""
    factory = _Factory(failures=2)      # the first two sessions used die; later ones work
    cache_calls = []

    def get_cached(db, user_id, text):
        cache_calls.append(db)
        if db.dead:
            raise _dead_session_error()
        return None

    monkeypatch.setattr(answer_engine_module.answer_cache_repository, "get_cached_answer", get_cached)
    monkeypatch.setattr(answer_engine_module.answer_cache_repository, "find_similar_answer", lambda *a: None)
    engine = ApplicationAnswerEngine(
        profile=CandidateProfile(profile_id="p1", user_id="u1", work_authorized=True), user_id="u1",
        session_factory=factory, demographics=None, llm_fn=_llm_must_not_be_called,
    )

    results = engine.answer_batch([Question("Are you legally authorized to work in the United States?", ["Yes", "No"])])

    assert results[0].answer == "Yes"
    assert len(cache_calls) >= 2      # the failed attempt and the retry


def test_the_resume_context_is_read_through_a_short_lived_session(monkeypatch):
    factory = _Factory(failures=0)
    seen = []
    monkeypatch.setattr(
        answer_engine_module, "load_resume_context",
        lambda db, profile: seen.append(db) or answer_engine_module.ResumeContext(),
    )
    engine = _engine(factory)

    engine._get_resume_context()

    assert seen == [factory.sessions[0]] and factory.sessions[0].close.called


# ---------------------------------------------------------------------------
# 4. plain-data snapshots and ownership (profile_repository)
# ---------------------------------------------------------------------------

def test_detached_copy_holds_every_column_and_no_session():
    profile = CandidateProfile(profile_id="p1", user_id="u1", first_name="Ada", email="ada@example.com")
    profile.phone_encrypted = encrypt_field("+1-555-0100")

    copy = repo.detached_copy(profile)

    assert copy is not profile
    assert (copy.profile_id, copy.user_id, copy.first_name, copy.email) == ("p1", "u1", "Ada", "ada@example.com")
    assert copy.phone_encrypted == profile.phone_encrypted
    assert sa_inspect(copy).transient        # never attached: nothing it holds can be flushed or lazy-loaded


def test_reading_a_detached_copy_never_touches_a_database():
    profile = CandidateProfile(profile_id="p1", user_id="u1", first_name="Ada")
    copy = repo.detached_copy(profile)

    # Every mapped column is readable with no session anywhere in sight.
    for column in sa_inspect(CandidateProfile).column_attrs:
        getattr(copy, column.key)


def _db_returning(*, email="t@example.com", owner="u1"):
    db = MagicMock()
    db.query.return_value.filter.return_value.scalar.side_effect = [email, owner]
    return db


def test_load_owned_demographics_returns_a_detached_copy_for_the_users_own_profile(caplog):
    profile = CandidateProfile(profile_id="p1", user_id="u1")
    row = CandidateDemographics(id="d1", candidate_id="p1", gender="male", hispanic_latino="no")

    with patch.object(repo, "get_demographics", return_value=row), caplog.at_level(logging.INFO):
        loaded = repo.load_owned_demographics(_db_returning(), user_id="u1", profile=profile)

    assert loaded is not row and loaded.gender == "male" and loaded.hispanic_latino == "no"
    assert sa_inspect(loaded).transient
    # The log names the ACCOUNT and which fields are set - and never a value.
    assert "t@example.com" in caplog.text and "u1" in caplog.text
    assert "gender" in caplog.text and "'male'" not in caplog.text


def test_load_owned_demographics_returns_none_when_nothing_is_saved(caplog):
    profile = CandidateProfile(profile_id="p1", user_id="u1")

    with patch.object(repo, "get_demographics", return_value=None), caplog.at_level(logging.INFO):
        loaded = repo.load_owned_demographics(_db_returning(), user_id="u1", profile=profile)

    assert loaded is None
    assert "none saved for account t@example.com" in caplog.text


def test_it_fails_when_the_profile_belongs_to_another_user():
    profile = CandidateProfile(profile_id="p1", user_id="owner")

    with patch.object(repo, "get_demographics", return_value=CandidateDemographics(id="d", candidate_id="p1")):
        with pytest.raises(repo.DemographicsOwnershipError):
            repo.load_owned_demographics(_db_returning(), user_id="someone-else", profile=profile)


def test_it_fails_when_the_row_hangs_off_a_profile_of_another_user():
    """The defence the join provides: even if the row lookup somehow returned a row,
    it is used only if ITS profile is this user's."""
    profile = CandidateProfile(profile_id="p1", user_id="u1")
    row = CandidateDemographics(id="d1", candidate_id="p-other", gender="male")

    with patch.object(repo, "get_demographics", return_value=row):
        with pytest.raises(repo.DemographicsOwnershipError):
            repo.load_owned_demographics(_db_returning(owner="u-other"), user_id="u1", profile=profile)


def test_it_fails_for_a_missing_profile():
    with pytest.raises(repo.DemographicsOwnershipError):
        repo.load_owned_demographics(MagicMock(), user_id="u1", profile=None)


# ---------------------------------------------------------------------------
# 5. base.py: a failed batch is a WARNING, and one bad question does not sink the rest
# ---------------------------------------------------------------------------

_TWO_QUESTIONS = """
<html><body>
<li class="application-question custom-question">
  <div>Are you able to work in person from our Mountain View office?</div>
  <div class="application-field"><input type="text" name="cards[a][field0]" placeholder="Type your response"></div>
</li>
<li class="application-question custom-question">
  <div>What is your favourite programming language for backend work?</div>
  <div class="application-field"><input type="text" name="cards[b][field1]" placeholder="Type your response"></div>
</li>
</body></html>
"""


class _BatchFailsOneQuestionFails:
    """`answer_batch` raises for a batch of more than one question, and for the
    question containing "favourite" even alone."""

    def __init__(self):
        self.calls = []

    def answer_batch(self, questions):
        self.calls.append([q.text for q in questions])
        if len(questions) > 1:
            raise PendingRollbackError(DEAD)
        if "favourite" in questions[0].text:
            raise RuntimeError("this one question cannot be answered")
        return [AnswerResult(question=questions[0].text, answer="Yes", source="llm", confidence=0.95)]


@pytest.fixture
def resume_file(tmp_path):
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.4 fake resume bytes")
    return path


def _adapter(page, resume_file, engine):
    profile = CandidateProfile(profile_id="profile-1", user_id="user-1", first_name="Ada", email="ada@example.com")
    profile.phone_encrypted = encrypt_field("+1-555-0100")
    document = ProfileDocument(
        document_id="doc-1", profile_id="profile-1", document_type="resume", original_filename="resume.pdf",
        stored_path=str(resume_file), file_hash="abc123", is_default=True,
    )
    return GreenhouseAdapter(page=page, profile=profile, resume_document=document, answer_engine=engine)


def test_a_failed_batch_is_logged_as_a_warning_with_the_real_exception(page, resume_file, caplog):
    page.set_content(_TWO_QUESTIONS)
    adapter = _adapter(page, resume_file, _BatchFailsOneQuestionFails())
    adapter._fill_questions_by_nearby_text()

    with caplog.at_level(logging.WARNING):
        adapter._fill_questions_via_answer_engine()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("PendingRollbackError" in r.getMessage() for r in warnings)   # the actual exception, not silence
    assert any("cannot be answered" in r.getMessage() for r in warnings)     # and the per-question one


def test_one_question_failing_does_not_discard_the_answers_to_the_others(page, resume_file):
    page.set_content(_TWO_QUESTIONS)
    engine = _BatchFailsOneQuestionFails()
    adapter = _adapter(page, resume_file, engine)
    adapter._fill_questions_by_nearby_text()

    results = adapter._fill_questions_via_answer_engine()

    assert page.locator("input[name='cards[a][field0]']").input_value() == "Yes"     # answered on its own
    assert page.locator("input[name='cards[b][field1]']").input_value() == ""        # left for a human
    assert sorted(r.filled for r in results) == [False, True]
    assert len(engine.calls[0]) == 2 and [len(c) for c in engine.calls[1:]] == [1, 1]  # batch, then each alone
    assert page.locator(f"input[{_AUTOMATION_EXAMINED_ATTR}]").count() == 2         # both still marked examined


def test_a_healthy_batch_is_still_a_single_call(page, resume_file):
    class Healthy:
        def __init__(self):
            self.calls = 0

        def answer_batch(self, questions):
            self.calls += 1
            return [AnswerResult(question=q.text, answer="Yes", source="llm", confidence=0.95) for q in questions]

    page.set_content(_TWO_QUESTIONS)
    engine = Healthy()
    adapter = _adapter(page, resume_file, engine)
    adapter._fill_questions_by_nearby_text()

    adapter._fill_questions_via_answer_engine()

    assert engine.calls == 1        # the fallback costs nothing unless something failed
