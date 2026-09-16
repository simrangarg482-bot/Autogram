"""
No-progress detection (spec §12), asserted through the REAL wiring.

`test_budgets.py` already unit-tests `BudgetTracker` in isolation. The
question here is the one that actually protects a run: does `loop.py`'s
click/fill/next dispatch feed that tracker, and does a genuinely stuck model
reach a ceiling? So these drive `_handle_execute_action` and
`_check_budgets`, not `record_action` directly, except where the point is the
counting rule itself.

Also pinned here: the two pre-existing ceilings
(`MAX_ITERATIONS_PER_RESUME` = 80, `MAX_FIELD_ATTEMPTS` = 3) are SEPARATE and
untouched — the budgets layer can only ever stop a run earlier.
"""

import threading

import pytest

from automation.agents.autonomous.actions import ActionResult, AgentAction
from automation.agents.autonomous.budgets import (
    NON_PROGRESS_ACTION_TYPES,
    AgentBudgets,
    BudgetTracker,
)
from automation.agents.autonomous.decision import Decision
from automation.agents.autonomous.loop import (
    MAX_FIELD_ATTEMPTS,
    MAX_ITERATIONS_PER_RESUME,
    AutonomousAgentLoop,
    TaskHandle,
)
from automation.agents.autonomous.observer import PageElement, PageState
from automation.applications.page_navigator import PageSignature


class FakeDB:
    def refresh(self, obj): pass
    def rollback(self): pass
    def commit(self): pass


class FakeTask:
    def __init__(self, **overrides):
        self.task_id = "no-progress"
        self.user_id = "user_test"
        self.job_url = "https://jobs.example/apply"
        self.original_objective = "Apply"
        self.current_status = "RUNNING"
        self.candidate_profile = {"profile": {"email": "jane@example.com"}}
        self.confirmed_answers = {}
        self.action_history = []
        self.field_attempt_ledger = {}
        self.question_ledger = {}
        self.application_progress = {}
        self.uploaded_documents = []
        self.auto_submit_approved = False
        self.__dict__.update(overrides)


class ExplodingPage:
    url = "https://jobs.example/apply"

    def __getattr__(self, name):
        raise AssertionError(f"the browser must not be touched, but page.{name} was accessed")


_SIG = PageSignature(
    url="https://jobs.example/apply", title="Apply", heading="Details",
    step_indicator="Step 1 of 2", controls=("input:email", "button:next"),
)


def _element(ref=1, *, name="Email", tag="input", type_="text", semantic=None):
    return PageElement(
        ref=ref, tag=tag, type=type_, name=name, value=None, required=False,
        disabled=False, checked=None, options=None,
        semantic_action=semantic, action_confidence="HIGH" if semantic else None,
    )


def _state(elements):
    return PageState(
        url="https://jobs.example/apply", title="Apply", visible_text="Application form",
        elements=list(elements), page_type="application_page",
    )


def _loop(monkeypatch, *, budgets=None):
    from app.services import autonomous_task_repository as task_repo
    from automation.agents.autonomous import loop as loop_module

    handle = TaskHandle(
        resume_event=threading.Event(), cancel_requested=threading.Event(),
        page=ExplodingPage(),
    )
    if budgets is not None:
        handle.budgets = budgets
    loop = AutonomousAgentLoop("no-progress", handle)
    loop.handle.last_page_signature = _SIG
    recorded = {"paused": [], "failed": [], "audits": []}
    monkeypatch.setattr(loop, "_safe_page_signature", lambda: _SIG)
    monkeypatch.setattr(loop, "_record_audit",
                        lambda db, t, kind, data=None: recorded["audits"].append((kind, data)))
    monkeypatch.setattr(loop, "_record_chat_action", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_record_chat_milestone", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_pause_for_human",
                        lambda db, t, intervention, **kw: recorded["paused"].append(intervention))
    monkeypatch.setattr(loop, "_safe_mark_failed",
                        lambda db, t, msg, *, reason: recorded["failed"].append(reason))
    monkeypatch.setattr(loop, "_escalate_with_vision",
                        lambda db, t, s, intervention, *, detection_layer: (
                            recorded["paused"].append(intervention) or False))
    monkeypatch.setattr(task_repo, "append_action", lambda db, t, e: None)
    monkeypatch.setattr(task_repo, "record_field_attempt", lambda db, t, i, e: None)
    monkeypatch.setattr(task_repo, "record_question_ledger_entry", lambda db, t, k, e: None)
    monkeypatch.setattr(task_repo, "update_progress", lambda db, t, p: None)
    monkeypatch.setattr(loop_module, "publish_task_event", lambda *a, **k: None)
    return loop, recorded


def _stub_executor(monkeypatch, result):
    class _Executor:
        def __init__(self, *a, **k): pass

        def execute(self, action, **kwargs):
            return result

    monkeypatch.setattr("automation.agents.autonomous.loop.ActionExecutor", _Executor)


def _dispatch(loop, task, element, action, monkeypatch, result):
    _stub_executor(monkeypatch, result)
    return loop._handle_execute_action(
        FakeDB(), task, _state([element]),
        Decision(decision_type="EXECUTE_ACTION", action=action, reasoning="try"),
    )


# ===========================================================================
# 1. The wiring actually exists.
# ===========================================================================

def test_the_real_dispatch_path_feeds_the_budget_tracker(monkeypatch):
    """The premise of everything below: a click going through
    `_handle_execute_action` reaches `BudgetTracker.record_action`. Without
    this, `test_budgets.py` would be testing a tracker nothing calls."""
    loop, _ = _loop(monkeypatch)
    monkeypatch.setattr(loop, "_track_verification_streak", lambda *a, **k: None)
    button = _element(ref=1, name="Next", tag="button", type_="button", semantic="NEXT")

    _dispatch(loop, FakeTask(), button, AgentAction(action_type="click", element_ref=1),
              monkeypatch, ActionResult(True, "click", "clicked", verified=True))

    assert loop.handle.budgets.total_actions == 1
    assert loop.handle.budgets.no_progress_streak == 0


# ===========================================================================
# 2. Three consecutive no-effect clicks trip the streak, and the budget.
# ===========================================================================

def test_three_clicks_with_no_page_change_build_a_no_progress_streak(monkeypatch):
    loop, _ = _loop(monkeypatch)
    monkeypatch.setattr(loop, "_track_verification_streak", lambda *a, **k: None)
    button = _element(ref=1, name="Next", tag="button", type_="button", semantic="NEXT")

    for _ in range(3):
        _dispatch(loop, FakeTask(), button, AgentAction(action_type="click", element_ref=1),
                  monkeypatch, ActionResult(True, "click", "clicked, nothing moved", verified=False))

    assert loop.handle.budgets.no_progress_streak == 3
    assert loop.handle.budgets.same_page_actions == 3


def test_a_stuck_click_loop_eventually_fails_the_task_rather_than_running_on(monkeypatch):
    """The no-progress budget's recommended decision is TASK_FAILED — a model
    that is provably stuck is a clean, honest failure, not a question for a
    human."""
    loop, recorded = _loop(monkeypatch, budgets=BudgetTracker(AgentBudgets(max_no_progress_iterations=3)))
    monkeypatch.setattr(loop, "_track_verification_streak", lambda *a, **k: None)
    button = _element(ref=1, name="Next", tag="button", type_="button", semantic="NEXT")
    task = FakeTask()

    for _ in range(3):
        _dispatch(loop, task, button, AgentAction(action_type="click", element_ref=1),
                  monkeypatch, ActionResult(True, "click", "nothing moved", verified=False))

    assert loop._check_budgets(FakeDB(), task) == "STOP"
    assert recorded["failed"] == ["budget_max_no_progress_iterations"]


def test_one_real_advance_clears_the_streak(monkeypatch):
    """Otherwise a long application would accumulate its way into a failure."""
    loop, _ = _loop(monkeypatch)
    monkeypatch.setattr(loop, "_track_verification_streak", lambda *a, **k: None)
    button = _element(ref=1, name="Next", tag="button", type_="button", semantic="NEXT")
    task = FakeTask()

    for _ in range(2):
        _dispatch(loop, task, button, AgentAction(action_type="click", element_ref=1),
                  monkeypatch, ActionResult(True, "click", "nothing moved", verified=False))
    _dispatch(loop, task, button, AgentAction(action_type="click", element_ref=1),
              monkeypatch, ActionResult(True, "click", "advanced to step 2", verified=True))

    assert loop.handle.budgets.no_progress_streak == 0
    assert loop.handle.budgets.same_page_actions == 0


# ===========================================================================
# 3. A fill blocked by validation counts as no progress.
# ===========================================================================

def test_a_fill_whose_value_does_not_stick_counts_as_no_progress(monkeypatch):
    """A field rejected by client-side validation leaves the page signature
    unchanged because the value never landed — indistinguishable from a
    stuck click as far as progress goes, and it must count the same."""
    loop, _ = _loop(monkeypatch)
    monkeypatch.setattr(loop, "_track_verification_streak", lambda *a, **k: None)
    field = _element(ref=1, name="Phone")

    for _ in range(2):
        _dispatch(loop, FakeTask(), field,
                  AgentAction(action_type="fill", element_ref=1, value="555"),
                  monkeypatch,
                  ActionResult(True, "fill", "Phone number must include an area code",
                               verified=False, failure_code="VALIDATION_ERROR"))

    assert loop.handle.budgets.no_progress_streak == 2


def test_a_next_click_that_does_not_advance_the_step_counts_as_no_progress(monkeypatch):
    """The specific multi-step case: "Next" dispatched fine, but the step
    indicator never moved, so nothing was accomplished."""
    loop, _ = _loop(monkeypatch)
    monkeypatch.setattr(loop, "_track_verification_streak", lambda *a, **k: None)
    button = _element(ref=2, name="Next", tag="button", type_="button", semantic="NEXT")

    _dispatch(loop, FakeTask(), button, AgentAction(action_type="click", element_ref=2),
              monkeypatch, ActionResult(True, "click", "clicked; still on step 1", verified=False))

    assert loop.handle.budgets.no_progress_streak == 1


def test_a_hard_action_failure_counts_as_both_failed_and_no_progress(monkeypatch):
    loop, _ = _loop(monkeypatch)
    monkeypatch.setattr(loop, "_track_verification_streak", lambda *a, **k: None)
    _dispatch(loop, FakeTask(), _element(ref=1, name="Email"),
              AgentAction(action_type="fill", element_ref=1, value="jane@example.com"),
              monkeypatch, ActionResult(False, "fill", "element detached", verified=False))

    assert loop.handle.budgets.failed_actions == 1
    assert loop.handle.budgets.no_progress_streak == 1


# ===========================================================================
# 4. Scrolling is NEUTRAL — it neither stalls nor resets.
# ===========================================================================

def test_a_scroll_does_not_count_as_a_stall():
    """Scrolling to reveal a field is ordinary, expected, zero-DOM-change
    behavior. Counting it as a stall would push a perfectly healthy run
    toward TASK_FAILED."""
    tracker = BudgetTracker(budgets=AgentBudgets(max_no_progress_iterations=3))
    for _ in range(5):
        tracker.record_action("scroll", succeeded=True, changed_page=False)
    assert tracker.no_progress_streak == 0
    assert tracker.check() is None


def test_a_scroll_does_not_reset_a_stall_either(monkeypatch):
    """REGRESSION, and the direction that actually mattered. `executor.py`'s
    `_do_scroll` returns success=True with `verified` defaulting True, which
    `loop.py` records as `changed_page=True` — so every scroll used to CLEAR
    `no_progress_streak`. A model alternating an ineffective click with a
    scroll could therefore never trip the no-progress ceiling at all."""
    tracker = BudgetTracker(budgets=AgentBudgets(max_no_progress_iterations=4))
    for _ in range(4):
        tracker.record_action("click", succeeded=True, changed_page=False)
        tracker.record_action("scroll", succeeded=True, changed_page=True)

    assert tracker.no_progress_streak == 4, "a scroll must not hand a stuck model a free reset"
    blown = tracker.check()
    assert blown is not None and blown.budget_name == "max_no_progress_iterations"


def test_a_scroll_is_still_counted_against_the_total_action_budget():
    """Neutral for stuck-detection is not the same as free — a scroll still
    costs an action and an iteration."""
    tracker = BudgetTracker(budgets=AgentBudgets(max_total_actions=3, max_no_progress_iterations=None))
    for _ in range(3):
        tracker.record_action("scroll", succeeded=True, changed_page=False)
    assert tracker.total_actions == 3
    blown = tracker.check()
    assert blown is not None and blown.budget_name == "max_total_actions"


@pytest.mark.parametrize("action_type", sorted(NON_PROGRESS_ACTION_TYPES))
def test_every_neutral_action_type_leaves_the_stuck_counters_alone(action_type):
    tracker = BudgetTracker()
    tracker.record_action("click", succeeded=True, changed_page=False)
    before = (tracker.no_progress_streak, tracker.same_page_actions)
    tracker.record_action(action_type, succeeded=True, changed_page=True)
    assert (tracker.no_progress_streak, tracker.same_page_actions) == before


def test_a_mutating_action_type_is_never_treated_as_neutral():
    """The set must not quietly grow to cover the actions that DO have to
    make progress."""
    for mutating in ("click", "fill", "select", "check", "uncheck", "navigate", "upload_file"):
        assert mutating not in NON_PROGRESS_ACTION_TYPES


def test_a_failed_neutral_action_still_counts_as_a_failure():
    tracker = BudgetTracker()
    tracker.record_action("scroll", succeeded=False, changed_page=False)
    assert tracker.failed_actions == 1
    assert tracker.no_progress_streak == 0


# ===========================================================================
# 5. The pre-existing ceilings are separate and intact.
# ===========================================================================

def test_the_two_hardcoded_ceilings_keep_their_exact_values():
    assert MAX_ITERATIONS_PER_RESUME == 80
    assert MAX_FIELD_ATTEMPTS == 3


def test_the_iteration_ceiling_is_enforced_independently_of_any_budget(monkeypatch):
    """Structural: the `MAX_ITERATIONS_PER_RESUME` check sits ABOVE the budget
    check in `_loop_body` and fails the task on its own, so disabling every
    configurable budget cannot disable it."""
    import ast
    import inspect
    import textwrap

    from automation.agents.autonomous import loop as loop_module

    source = textwrap.dedent(inspect.getsource(loop_module.AutonomousAgentLoop._loop_body))
    assert "iterations > MAX_ITERATIONS_PER_RESUME" in source
    ceiling_at = source.index("MAX_ITERATIONS_PER_RESUME")
    budget_at = source.index("_check_budgets")
    assert ceiling_at < budget_at, "the hardcoded ceiling must be checked before the budgets"
    ast.parse(source)


def test_the_field_attempt_ceiling_is_enforced_from_the_persisted_ledger(monkeypatch):
    """`MAX_FIELD_ATTEMPTS` lives in the DB ledger, so it survives a resume —
    a field already marked failed is refused before any dispatch, regardless
    of what the budgets say."""
    loop, recorded = _loop(monkeypatch)
    field = _element(ref=1, name="Email")
    from automation.agents.autonomous.observer import field_identity

    task = FakeTask(field_attempt_ledger={
        field_identity(field): {"attempts": MAX_FIELD_ATTEMPTS, "status": "failed"},
    })

    class _MustNotRun:
        def __init__(self, *a, **k): pass

        def execute(self, *a, **k):
            raise AssertionError("a field past MAX_FIELD_ATTEMPTS must never be retried")

    monkeypatch.setattr("automation.agents.autonomous.loop.ActionExecutor", _MustNotRun)

    keep_going = loop._handle_execute_action(
        FakeDB(), task, _state([field]),
        Decision(decision_type="EXECUTE_ACTION",
                 action=AgentAction(action_type="fill", element_ref=1, value="jane@example.com"),
                 reasoning="retry the email"),
    )

    assert keep_going is False
    assert recorded["paused"]
    assert str(MAX_FIELD_ATTEMPTS) in recorded["paused"][0]["reason"] or "previously failed" in recorded["paused"][0]["reason"]


def test_the_field_attempt_ledger_marks_a_field_failed_at_exactly_the_ceiling(monkeypatch):
    from app.services import autonomous_task_repository as task_repo
    from automation.agents.autonomous.observer import field_identity

    loop, _ = _loop(monkeypatch)
    field = _element(ref=1, name="Email")
    task = FakeTask()
    written = {}
    monkeypatch.setattr(task_repo, "record_field_attempt",
                        lambda db, t, ident, entry: written.update({ident: entry}))

    action = AgentAction(action_type="fill", element_ref=1, value="jane@example.com")
    failed = ActionResult(True, "fill", "did not stick", verified=False)
    for attempt in range(1, MAX_FIELD_ATTEMPTS + 1):
        loop._record_field_attempt(FakeDB(), task, field, action, failed)
        task.field_attempt_ledger = dict(written)
        status = written[field_identity(field)]["status"]
        assert status == ("failed" if attempt >= MAX_FIELD_ATTEMPTS else "attempted"), attempt


def test_a_budget_can_only_stop_a_run_earlier_never_later():
    """The configurable budgets sit strictly inside the hardcoded ceilings:
    the default no-progress budget trips well before 80 iterations could."""
    assert AgentBudgets().max_no_progress_iterations is not None
    assert AgentBudgets().max_no_progress_iterations < MAX_ITERATIONS_PER_RESUME
    assert AgentBudgets().max_same_page_actions < MAX_ITERATIONS_PER_RESUME


# ===========================================================================
# 6. The other, independent stuck detectors still stand.
# ===========================================================================

def test_the_same_element_failing_three_times_escalates_on_its_own(monkeypatch):
    """`unverified_streak` is a separate, cheaper same-process detector from
    the budgets — three unverified attempts on ONE ref escalates immediately,
    without waiting for any budget."""
    loop, recorded = _loop(monkeypatch)
    field = _element(ref=1, name="Email")
    task = FakeTask()
    action = AgentAction(action_type="fill", element_ref=1, value="jane@example.com")

    keep_going = True
    for _ in range(3):
        keep_going = _dispatch(loop, task, field, action, monkeypatch,
                               ActionResult(True, "fill", "did not stick", verified=False))

    assert keep_going is False
    assert recorded["paused"]
    assert loop.handle.unverified_streak == 0, "the streak resets once it has escalated"


def test_a_different_element_each_time_does_not_build_that_streak(monkeypatch):
    """The converse: trying three DIFFERENT fields is normal form-filling,
    not a stuck loop, for the per-element detector."""
    loop, _ = _loop(monkeypatch)
    task = FakeTask()
    for ref in (1, 2, 3):
        _dispatch(loop, task, _element(ref=ref, name=f"Field {ref}"),
                  AgentAction(action_type="fill", element_ref=ref, value="x"),
                  monkeypatch, ActionResult(True, "fill", "did not stick", verified=False))

    assert loop.handle.unverified_streak == 1
    # ...but the budget layer still sees three iterations of no progress.
    assert loop.handle.budgets.no_progress_streak == 3


def test_the_stalled_signature_check_and_the_budget_are_independent(monkeypatch):
    """`_loop_body`'s byte-identical-signature escalation fires on evidence
    the budgets never see (the page, not the action), so neither can mask the
    other."""
    import inspect

    from automation.agents.autonomous import loop as loop_module

    source = inspect.getsource(loop_module.AutonomousAgentLoop._loop_body)
    assert "differs_from" in source
    assert "stalled_page_signature" in source
    assert "_check_budgets" in source
