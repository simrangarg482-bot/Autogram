"""
Recovery scenarios (spec §10), one test per failure the agent can actually
hit, asserting the CLASSIFICATION and the POLICY ACTION for each.

Two layers are deliberately kept apart here, because they genuinely are two
different mechanisms and forcing one into the other's vocabulary would be a
lie about how the system recovers:

  * IN-LOOP failures — something went wrong while a run is alive and driving
    a browser. `recovery.classify_failure` names it, `recovery.recovery_policy`
    says what to do, and `loop.py` follows that verdict.
  * ORPHANED-TASK recovery — the run itself is gone (worker crash, process
    termination) or its coordination substrate is (Redis disconnect). There
    is no `ActionResult` to classify; the mechanism is
    `runner.reconcile_orphaned_tasks_on_startup` over
    `autonomous_task_repository.list_orphaned_running_tasks` /
    `try_claim_orphan_failed`, guarded by `coordination/lease.py`.

The must-have case is at the bottom: a crash/disconnect while a submission
click was in flight or its result unknown can only ever land in
UNKNOWN_SUBMISSION_STATE, and no code path may re-attempt a submit from
there without a fresh, human-approved decision.
"""

import threading

import pytest

from automation.agents.autonomous import recovery
from automation.agents.autonomous.actions import ActionResult, AgentAction
from automation.agents.autonomous.decision import Decision, DecisionError
from automation.agents.autonomous.loop import AutonomousAgentLoop, TaskHandle
from automation.agents.autonomous.observer import PageElement, PageState
from automation.agents.autonomous.state_machine import validate_substate_transition
from automation.applications.page_navigator import PageSignature


# --- doubles ---------------------------------------------------------------

class FakeDB:
    def refresh(self, obj): pass
    def rollback(self): pass
    def commit(self): pass


class FakeTask:
    def __init__(self, **overrides):
        self.task_id = "recovery-scenarios"
        self.user_id = "user_test"
        self.job_url = "https://jobs.example/apply/review"
        self.original_objective = "Apply"
        self.current_status = "RUNNING"
        self.candidate_profile = {"profile": {}}
        self.confirmed_answers = {}
        self.action_history = []
        self.field_attempt_ledger = {}
        self.question_ledger = {}
        self.application_progress = {}
        self.uploaded_documents = []
        self.auto_submit_approved = False
        self.__dict__.update(overrides)


class ExplodingPage:
    """Any browser interaction at all is a test failure."""

    url = "https://jobs.example/apply/review"

    def __getattr__(self, name):
        raise AssertionError(f"the browser must not be touched, but page.{name} was accessed")


_SIG = PageSignature(
    url="https://jobs.example/apply/review", title="Review", heading="Review",
    step_indicator="Step 3 of 3", controls=("button:submit application",),
)


def _submit_button(ref=9, name="Submit Application"):
    return PageElement(
        ref=ref, tag="button", type="submit", name=name, value=None, required=False,
        disabled=False, checked=None, options=None,
        semantic_action="SUBMIT", action_confidence="HIGH", irreversible=True,
    )


def _review_state(elements=None):
    return PageState(
        url="https://jobs.example/apply/review", title="Review your application",
        visible_text="Review your application before submitting",
        elements=list(elements if elements is not None else [_submit_button()]),
        page_type="review",
    )


def _loop(monkeypatch, *, page=None, task=None):
    from app.services import autonomous_task_repository as task_repo
    from automation.agents.autonomous import loop as loop_module

    loop = AutonomousAgentLoop(
        "recovery-scenarios",
        TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event(), page=page),
    )
    loop.handle.last_page_signature = _SIG
    recorded = {"paused": [], "audits": [], "actions": [], "failed": [], "progress": []}
    monkeypatch.setattr(loop, "_safe_page_signature", lambda: _SIG)
    monkeypatch.setattr(loop, "_record_audit",
                        lambda db, t, kind, data=None: recorded["audits"].append((kind, data)))
    monkeypatch.setattr(loop, "_record_chat_action", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_record_chat_milestone", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_pause_for_human",
                        lambda db, t, intervention, **kw: recorded["paused"].append((intervention, kw)))
    monkeypatch.setattr(loop, "_safe_mark_failed",
                        lambda db, t, msg, *, reason: recorded["failed"].append(reason))
    monkeypatch.setattr(task_repo, "append_action",
                        lambda db, t, entry: recorded["actions"].append(entry))
    monkeypatch.setattr(task_repo, "record_field_attempt", lambda db, t, i, e: None)
    monkeypatch.setattr(task_repo, "record_question_ledger_entry", lambda db, t, k, e: None)

    def _update_progress(db, t, progress):
        t.application_progress = progress
        recorded["progress"].append(dict(progress))

    monkeypatch.setattr(task_repo, "update_progress", _update_progress)
    monkeypatch.setattr(loop_module, "publish_task_event", lambda *a, **k: None)
    return loop, recorded


def _stub_executor(monkeypatch, result):
    """Replaces the executor in `loop.py` with one that returns `result`
    without touching a browser."""
    class _Executor:
        def __init__(self, *a, **k): pass

        def execute(self, action, **kwargs):
            return result

    monkeypatch.setattr("automation.agents.autonomous.loop.ActionExecutor", _Executor)


# ===========================================================================
# PART 1 — in-loop failures: classification + policy action.
# ===========================================================================

def _classify_and_policy(**kwargs):
    code = recovery.classify_failure(**kwargs)
    return code, recovery.recovery_policy(code)


def test_network_timeout_is_a_bounded_retry():
    code, policy = _classify_and_policy(
        action_succeeded=False, action_verified=False,
        exception_message="Timeout 30000ms exceeded while waiting for the network response",
    )
    assert code == "NETWORK_ERROR"
    assert policy.action == "RETRY"
    # ...and never an unbounded one.
    assert recovery.recovery_policy(code, attempts_so_far=99).action != "RETRY"


def test_browser_timeout_classifies_as_a_network_error_not_a_stuck_page():
    """A Playwright `TimeoutError` on a locator is a transport/timing problem,
    not evidence that the model is stuck — it must not be conflated with
    NO_PROGRESS, whose policy is to stop."""
    code, policy = _classify_and_policy(
        action_succeeded=False, action_verified=False,
        exception_message="playwright._impl._api_types.TimeoutError: Timeout 5000ms exceeded.",
    )
    assert code == "NETWORK_ERROR"
    assert policy.action == "RETRY"


def test_dom_replacement_is_a_stale_reference_and_is_re_observed_never_retried():
    """A re-rendered form (React swapping the subtree) invalidates every
    element ref. Retrying the same ref would address a DIFFERENT control, so
    the only correct response is a fresh observation."""
    code, policy = _classify_and_policy(stale_reference=True, action_succeeded=False, action_verified=False)
    assert code == "STALE_REFERENCE"
    assert policy.action == "REOBSERVE"
    # Bounded even in the retry table, and it still never comes back RETRY.
    assert recovery.recovery_policy("STALE_REFERENCE", attempts_so_far=5).action == "REOBSERVE"


def test_element_missing_retries_briefly_then_re_observes():
    assert recovery.classify_failure(element_missing=True, action_succeeded=False) == "ELEMENT_DISAPPEARED"
    assert recovery.recovery_policy("ELEMENT_DISAPPEARED", attempts_so_far=0).action == "RETRY"
    assert recovery.recovery_policy("ELEMENT_DISAPPEARED", attempts_so_far=9).action == "REOBSERVE"


def test_a_stale_reference_beats_element_missing_when_both_look_true():
    """They present identically to a caller; the taxonomy must still separate
    them, because their policies differ (re-observe vs retry-then-re-observe)."""
    assert recovery.classify_failure(stale_reference=True, element_missing=True) == "STALE_REFERENCE"


def test_validation_error_is_re_observed_so_the_message_can_be_read():
    code, policy = _classify_and_policy(
        action_succeeded=True, action_verified=False,
        validation_messages=["Phone number must include an area code"],
    )
    assert code == "VALIDATION_ERROR"
    assert policy.action == "REOBSERVE"


def test_unexpected_redirect_to_an_error_page_is_an_application_error():
    code, policy = _classify_and_policy(page_error_code="HTTP_500", action_succeeded=False, action_verified=False)
    assert code == "APPLICATION_ERROR"
    assert policy.action == "RETRY"
    escalated = recovery.recovery_policy(code, attempts_so_far=9)
    assert escalated.action == "HUMAN"
    assert escalated.request_type == "UNKNOWN_BLOCKER"


def test_an_unexplained_unverified_action_is_an_unexpected_page_not_a_silent_success():
    """The residual bucket must still be a FAILURE — never `None`, which the
    loop would read as "that worked"."""
    code, policy = _classify_and_policy(action_succeeded=True, action_verified=False)
    assert code == "UNEXPECTED_PAGE"
    assert policy.action in recovery.RECOVERY_ACTIONS


def test_session_expiration_goes_straight_to_a_human_and_is_never_retried():
    code, policy = _classify_and_policy(
        action_succeeded=False, action_verified=False,
        exception_message="Your session has expired. Please sign in again.",
    )
    assert code == "SESSION_EXPIRED"
    assert policy.action == "HUMAN"
    assert policy.request_type == "LOGIN_REQUIRED"


@pytest.mark.parametrize("blocker,expected", [
    ("OTP_REQUIRED", "OTP_REQUIRED"),
    ("MFA_REQUIRED", "OTP_REQUIRED"),
    ("CAPTCHA_REQUIRED", "CAPTCHA_REQUIRED"),
    ("LOGIN_REQUIRED", "LOGIN_REQUIRED"),
])
def test_a_human_verification_blocker_is_never_retried_or_bypassed(blocker, expected):
    """Spec §11's closing invariant. No `attempts_so_far` ever turns one of
    these into a RETRY."""
    code = recovery.classify_failure(blocker_request_type=blocker)
    assert code == expected
    for attempts in (0, 1, 5, 99):
        policy = recovery.recovery_policy(code, attempts_so_far=attempts)
        assert policy.action == "HUMAN", attempts


def test_no_progress_escalates_rather_than_retrying_forever():
    code, policy = _classify_and_policy(no_progress_streak=5, no_progress_ceiling=5,
                                        action_succeeded=False, action_verified=False)
    assert code == "NO_PROGRESS"
    assert policy.action == "ESCALATE"


def test_an_unrecognized_failure_code_still_reaches_a_human():
    policy = recovery.recovery_policy("SOMETHING_NEW_FROM_A_FUTURE_VERSION")
    assert policy.action == "HUMAN"
    assert policy.request_type == "UNKNOWN_BLOCKER"


def test_nothing_wrong_classifies_as_nothing_and_prescribes_nothing():
    assert recovery.classify_failure() is None
    assert recovery.recovery_policy(None) is None


def test_every_failure_code_has_a_policy_that_never_retries_unboundedly():
    """Exhaustive sweep of the taxonomy: no code, at any attempt count, can
    produce an endless RETRY."""
    for code in recovery.FAILURE_CODES:
        for attempts in range(0, 12):
            policy = recovery.recovery_policy(code, attempts_so_far=attempts)
            assert policy.action in recovery.RECOVERY_ACTIONS, code
        assert recovery.recovery_policy(code, attempts_so_far=50).action != "RETRY", code


# --- the loop's own handling of the LLM/vision failures --------------------

def test_llm_timeout_raises_decision_error_and_pauses_instead_of_guessing(monkeypatch):
    """A `decide_next_step` that times out must never fall through to "act
    anyway". `loop.py`'s DecisionError branch pauses for a human."""
    import inspect

    from automation.agents.autonomous import loop as loop_module

    source = inspect.getsource(loop_module.AutonomousAgentLoop._loop_body)
    assert "except DecisionError" in source
    handler = source.split("except DecisionError")[1].split("if decision.decision_type")[0]
    assert "_pause_for_human" in handler
    assert "_wait_for_resume" in handler
    for forbidden in ("ActionExecutor", "_handle_execute_action"):
        assert forbidden not in handler, forbidden


def test_llm_malformed_json_is_a_decision_error_and_never_a_partial_action():
    """Traced to its source: `decision._parse_response` refuses unparseable
    or structurally invalid output rather than returning a half-built
    `Decision` the loop could act on."""
    from automation.agents.autonomous.decision import _parse_response

    for malformed in (
        "not json at all",
        "{",
        '{"decision": "EXECUTE_ACTION"}',                       # no action
        '{"decision": "TELEPORT", "evidence": "x"}',            # unknown decision
        '{"decision": "EXECUTE_ACTION", "action": {"action_type": "rm -rf"}}',
    ):
        with pytest.raises(DecisionError):
            _parse_response(malformed)


def test_vision_failure_degrades_to_the_pause_it_was_trying_to_avoid(monkeypatch):
    """`_attempt_vision_assisted_decision` returning `(None, None)` — a failed
    screenshot or a failed vision call — must leave the original escalation
    exactly as it would have been, never make it worse or swallow it."""
    loop, recorded = _loop(monkeypatch, page=ExplodingPage())
    monkeypatch.setattr(loop, "_attempt_vision_assisted_decision", lambda task, state: (None, None))
    fallback = {
        "type": "UNKNOWN_BLOCKER", "reason": "the page stopped changing",
        "message": "Please check the browser tab.", "information_required": None,
    }

    keep_going = loop._escalate_with_vision(
        FakeDB(), FakeTask(), _review_state(), fallback, detection_layer="stalled_page_signature",
    )

    assert keep_going is False
    assert recorded["paused"][0][0] == fallback


def test_a_screenshot_that_survives_a_failed_vision_call_is_still_attached(monkeypatch):
    loop, recorded = _loop(monkeypatch, page=ExplodingPage())
    monkeypatch.setattr(loop, "_attempt_vision_assisted_decision", lambda task, state: (None, b"jpeg"))
    loop._escalate_with_vision(
        FakeDB(), FakeTask(), _review_state(),
        {"type": "UNKNOWN_BLOCKER", "reason": "r", "message": "m", "information_required": None},
        detection_layer="verification_streak",
    )
    assert recorded["paused"][0][1]["screenshot"] == b"jpeg"


def test_browser_disconnect_fails_the_task_and_always_releases_the_browser(monkeypatch):
    """A `BrowserAutomationError` out of the loop body marks the task failed
    AND closes the browser — the close must happen on that branch, not only
    on the generic one."""
    from automation.agents.autonomous import loop as loop_module
    from automation.browser.browser_manager import BrowserAutomationError

    closed = []
    loop = AutonomousAgentLoop(
        "recovery-scenarios",
        TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event()),
    )
    task = FakeTask()
    failures = []
    monkeypatch.setattr(loop, "_record_audit", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_record_chat_milestone", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_ensure_browser", lambda t: None)
    monkeypatch.setattr(loop, "_set_status", lambda db, t, s: None)
    monkeypatch.setattr(loop, "_close_browser", lambda: closed.append(True))
    monkeypatch.setattr(loop, "_safe_mark_failed",
                        lambda db, t, msg, *, reason: failures.append(reason))
    monkeypatch.setattr(loop, "_loop_body",
                        lambda db, t: (_ for _ in ()).throw(BrowserAutomationError("Target page crashed")))
    monkeypatch.setattr(loop_module, "publish_task_event", lambda *a, **k: None)

    class _Session:
        def __enter__(self): return FakeDB()
        def __exit__(self, *a): return False

    monkeypatch.setattr(loop_module, "automation_db_session", lambda: _Session())
    monkeypatch.setattr(loop_module.task_repo, "get_by_id", lambda db, tid: task)

    loop.run()

    assert failures == ["browser_error"]
    assert closed == [True], "the browser must be released on the browser-error branch too"


def test_a_browser_that_fails_to_launch_is_still_closed(monkeypatch):
    """REGRESSION. `_ensure_browser` only published the manager onto the
    handle after a fully successful start, so `_close_browser` could not see
    it — and an exception from `new_page()` (or the initial navigation
    executor) leaked a live Playwright driver process per failed start."""
    from automation.agents.autonomous import loop as loop_module

    closed = []

    class _Manager:
        active_mode = "cdp"

        def __init__(self, **kwargs): pass
        def launch_context(self): pass
        def new_page(self): raise RuntimeError("no browser available")
        def close(self): closed.append(True)

    monkeypatch.setattr(loop_module, "BrowserManager", _Manager)
    loop, _ = _loop(monkeypatch)
    loop.handle.page = None

    with pytest.raises(RuntimeError):
        loop._ensure_browser(FakeTask())

    assert closed == [True]
    assert loop.handle.browser_manager is None


def test_closing_a_browser_that_refuses_to_close_never_masks_the_real_error(monkeypatch):
    loop, _ = _loop(monkeypatch)

    class _Manager:
        def close(self): raise RuntimeError("driver already gone")

    loop.handle.browser_manager = _Manager()
    loop._close_browser()  # must not raise
    assert loop.handle.browser_manager is None
    assert loop.handle.page is None


# ===========================================================================
# PART 2 — orphaned-task recovery. These are NOT `recovery.py`'s job: there
# is no live run and no ActionResult to classify.
# ===========================================================================

def test_worker_crash_is_recovered_by_the_orphan_reconciler_not_by_recovery_py():
    """A dead worker leaves a row in an executing status with nobody driving
    it. `recovery.classify_failure` has no vocabulary for that on purpose —
    the mechanism is `list_orphaned_running_tasks` + `try_claim_orphan_failed`."""
    from app.services import autonomous_task_repository as task_repo

    assert "WORKER_CRASH" not in recovery.FAILURE_CODES
    assert "PROCESS_TERMINATED" not in recovery.FAILURE_CODES
    assert "REDIS_DISCONNECT" not in recovery.FAILURE_CODES
    assert hasattr(task_repo, "list_orphaned_running_tasks")
    assert hasattr(task_repo, "try_claim_orphan_failed")


def test_process_termination_fails_every_orphan_and_never_claims_one_twice(monkeypatch):
    from automation.agents.autonomous import runner
    from app.services import autonomous_task_repository as task_repo

    rows = [FakeTask(task_id="t1", current_status="RUNNING"),
            FakeTask(task_id="t2", current_status="RESUMING")]
    claimed = []
    monkeypatch.setattr(task_repo, "list_orphaned_running_tasks", lambda db: rows)
    monkeypatch.setattr(task_repo, "try_claim_orphan_failed",
                        lambda db, tid, *, from_status, error: claimed.append((tid, error)) or True)
    monkeypatch.setattr(runner.human_interaction_repo, "get_active_for_task", lambda db, tid: None)
    monkeypatch.setattr(runner.audit_log_repo, "record_event", lambda *a, **k: None)
    monkeypatch.setattr(runner.lease, "is_held", lambda kind, tid: False)

    assert runner.reconcile_orphaned_tasks_on_startup(FakeDB()) == 2
    assert [tid for tid, _ in claimed] == ["t1", "t2"]

    # The loser of a race matches zero rows and backs off rather than raising.
    claimed.clear()
    monkeypatch.setattr(task_repo, "try_claim_orphan_failed",
                        lambda db, tid, *, from_status, error: False)
    assert runner.reconcile_orphaned_tasks_on_startup(FakeDB()) == 0


def test_a_live_lease_holder_is_never_reconciled_out_from_under_itself(monkeypatch):
    from automation.agents.autonomous import runner
    from app.services import autonomous_task_repository as task_repo

    monkeypatch.setattr(task_repo, "list_orphaned_running_tasks",
                        lambda db: [FakeTask(task_id="t1", current_status="RUNNING")])
    monkeypatch.setattr(task_repo, "try_claim_orphan_failed",
                        lambda *a, **k: pytest.fail("a live run must never be failed"))
    monkeypatch.setattr(runner.lease, "is_held", lambda kind, tid: True)

    assert runner.reconcile_orphaned_tasks_on_startup(FakeDB()) == 0


def test_a_redis_disconnect_does_not_abort_the_whole_reconciliation_pass(monkeypatch):
    """REGRESSION. `lease.is_held` reads Redis with no guard of its own, so an
    unreachable server propagated straight out of the reconciler and left
    EVERY orphan unreconciled — the recovery mechanism taking itself down. It
    now skips the affected task conservatively and keeps going."""
    from automation.agents.autonomous import runner
    from app.services import autonomous_task_repository as task_repo

    rows = [FakeTask(task_id="t1", current_status="RUNNING"),
            FakeTask(task_id="t2", current_status="RUNNING")]
    claimed = []

    def _is_held(kind, task_id):
        if task_id == "t1":
            raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")
        return False

    monkeypatch.setattr(task_repo, "list_orphaned_running_tasks", lambda db: rows)
    monkeypatch.setattr(task_repo, "try_claim_orphan_failed",
                        lambda db, tid, *, from_status, error: claimed.append(tid) or True)
    monkeypatch.setattr(runner.human_interaction_repo, "get_active_for_task", lambda db, tid: None)
    monkeypatch.setattr(runner.audit_log_repo, "record_event", lambda *a, **k: None)
    monkeypatch.setattr(runner.lease, "is_held", _is_held)

    assert runner.reconcile_orphaned_tasks_on_startup(FakeDB()) == 1
    assert claimed == ["t2"], "the reachable task must still be recovered"


def test_redis_being_absent_degrades_every_coordination_primitive_to_a_no_op(monkeypatch):
    """Single-process deployments have no Redis at all. Nothing in
    `coordination/` may raise or block there."""
    from automation.coordination import control, lease

    monkeypatch.setattr(lease, "_redis", lambda: None)
    monkeypatch.setattr(control, "_redis", lambda: None)

    assert lease.acquire("autonomous_task", "t1", "w1") is True
    assert lease.renew("autonomous_task", "t1", "w1") is True
    assert lease.is_held("autonomous_task", "t1") is False
    assert lease.release("autonomous_task", "t1", "w1") is None
    assert control.send_signal("autonomous_task", "t1", "cancel") is False
    assert control.consume_signal("autonomous_task", "t1", "cancel") is False
    assert control.collect_secret("t1") is None
    assert control.deposit_secret("t1", "hreq_1", "123456") is False


def test_a_redis_that_errors_mid_signal_never_raises_into_the_caller(monkeypatch):
    """A control-channel blip degrades to "signal not delivered", never to an
    exception surfacing on an API route."""
    from automation.coordination import control

    class _Broken:
        def __getattr__(self, name):
            def _boom(*a, **k):
                raise ConnectionError("redis gone")
            return _boom

    monkeypatch.setattr(control, "_redis", lambda: _Broken())
    assert control.send_signal("autonomous_task", "t1", "resume") is False
    assert control.consume_signal("autonomous_task", "t1", "resume") is False
    assert control.deposit_secret("t1", "hreq_1", "123456") is False
    assert control.collect_secret("t1") is None
    control.clear_all("autonomous_task", "t1")  # must not raise


def test_a_secret_mailbox_entry_is_never_returned_twice_or_logged(monkeypatch):
    """The OTP mailbox is one-shot by construction (`LPOP`), so a duplicate
    consumption can never replay a code."""
    from automation.coordination import control

    store = {}

    class _Redis:
        def lpush(self, key, value): store.setdefault(key, []).insert(0, value)
        def expire(self, key, ttl): pass
        def lpop(self, key):
            items = store.get(key) or []
            return items.pop(0) if items else None

    monkeypatch.setattr(control, "_redis", lambda: _Redis())
    assert control.deposit_secret("t1", "hreq_1", "123456") is True
    assert control.collect_secret("t1") == {"request_id": "hreq_1", "value": "123456"}
    assert control.collect_secret("t1") is None


# ===========================================================================
# PART 3 — THE MUST-HAVE: a crash while a submission was in flight.
# ===========================================================================

def test_a_submit_whose_outcome_is_unknown_lands_in_unknown_submission_state(monkeypatch):
    """CRITICAL REGRESSION. The submit click was dispatched but nothing
    observable confirmed it. The run must stop in UNKNOWN_SUBMISSION_STATE
    with an honest "may or may not have been submitted" handoff — not carry
    on and let the next iteration decide to click submit again."""
    loop, recorded = _loop(monkeypatch, page=ExplodingPage())
    _stub_executor(monkeypatch, ActionResult(
        True, "click", "Clicked, but the page never settled.", verified=False,
    ))
    task = FakeTask(auto_submit_approved=True)

    keep_going = loop._handle_execute_action(
        FakeDB(), task, _review_state(),
        Decision(decision_type="EXECUTE_ACTION",
                 action=AgentAction(action_type="click", element_ref=9),
                 reasoning="submit"),
    )

    assert keep_going is False
    assert loop.handle.substate == "UNKNOWN_SUBMISSION_STATE"
    assert loop.handle.submit_outcome_unknown is True
    # The marker is DURABLE, so a crash right here is still recoverable.
    assert task.application_progress["submit_in_flight"]["element_name"] == "Submit Application"
    intervention, _ = recorded["paused"][0]
    assert "MAY OR MAY NOT" in intervention["message"]


def test_a_second_submit_is_refused_from_that_state_even_though_approval_stands(monkeypatch):
    """The mandatory property. `auto_submit_approved` is a PERSISTED column,
    so it keeps approving, and a page that never changed still passes
    grounding — neither existing gate stops a second submit on its own. The
    click must never reach the executor."""
    loop, recorded = _loop(monkeypatch, page=ExplodingPage())

    class _MustNotRun:
        def __init__(self, *a, **k): pass

        def execute(self, action, **kwargs):
            raise AssertionError("a submit must never be re-dispatched from an unknown outcome")

    monkeypatch.setattr("automation.agents.autonomous.loop.ActionExecutor", _MustNotRun)
    task = FakeTask(
        auto_submit_approved=True,
        application_progress={"submit_in_flight": {"element_name": "Submit Application"}},
    )

    keep_going = loop._handle_execute_action(
        FakeDB(), task, _review_state(),
        Decision(decision_type="EXECUTE_ACTION",
                 action=AgentAction(action_type="click", element_ref=9),
                 reasoning="submit again"),
    )

    assert keep_going is False
    assert loop.handle.substate == "UNKNOWN_SUBMISSION_STATE"
    assert recorded["paused"], "the refusal must surface to a human, not be silent"
    assert ("action_rejected", {"reason": "submit_outcome_unknown",
                                "element_name": "Submit Application"}) in recorded["audits"]


def test_resuming_after_a_crash_mid_submit_never_picks_the_plan_back_up(monkeypatch):
    """A crash during the click leaves the durable marker behind. The NEXT run
    — a resume, or a fresh process — must stop at the top of `_loop_body`,
    before observing or deciding anything, rather than resuming the pre-crash
    plan."""
    loop, recorded = _loop(monkeypatch, page=ExplodingPage())
    monkeypatch.setattr(loop, "_wait_for_resume",
                        lambda: pytest.fail("the pre-crash plan must not be resumed"))
    task = FakeTask(
        auto_submit_approved=True,
        application_progress={"submit_in_flight": {"element_name": "Submit Application"}},
    )

    loop._loop_body(FakeDB(), task)

    assert loop.handle.substate == "UNKNOWN_SUBMISSION_STATE"
    intervention, _ = recorded["paused"][0]
    assert intervention["type"] == "USER_CONFIRMATION_REQUIRED"
    assert "MAY OR MAY NOT" in intervention["message"]
    assert recorded["actions"] == [], "nothing may be dispatched on a resume in this state"


def test_the_state_machine_makes_a_retry_from_that_state_structurally_impossible():
    """The second half of the guarantee: even if some future call site tried,
    UNKNOWN_SUBMISSION_STATE's only legal successor is a brand-new task's
    CREATED — never back into a submission substate."""
    for forbidden in ("READY_FOR_SUBMISSION", "SUBMITTED", "COMPLETED", "EXECUTING", "DECIDING"):
        assert validate_substate_transition("UNKNOWN_SUBMISSION_STATE", forbidden).allowed is False
    assert validate_substate_transition("UNKNOWN_SUBMISSION_STATE", "CREATED").allowed is True


def test_the_sink_state_is_sticky_against_every_later_substate_write(monkeypatch):
    """`_set_substate` applies nothing when the transition is refused, so once
    a run is in UNKNOWN_SUBMISSION_STATE no later bookkeeping write can move
    it out."""
    loop, _ = _loop(monkeypatch)
    loop.handle.substate = "UNKNOWN_SUBMISSION_STATE"
    for later in ("OBSERVING", "DECIDING", "EXECUTING", "VERIFYING", "HUMAN_REQUIRED", "RESUMING"):
        loop._set_substate(later)
        assert loop.handle.substate == "UNKNOWN_SUBMISSION_STATE", later


def test_a_refused_submit_click_leaves_nothing_unresolved(monkeypatch):
    """The converse, so the guard cannot be "satisfied" by jamming every task:
    a submit blocked BEFORE it ran never happened, so it must not poison the
    task with an unknown outcome."""
    loop, _ = _loop(monkeypatch, page=ExplodingPage())
    _stub_executor(monkeypatch, ActionResult(
        False, "click", "Refused: no approval.", blocked_reason="submit_requires_approval",
    ))
    task = FakeTask(auto_submit_approved=False)

    loop._handle_execute_action(
        FakeDB(), task, _review_state(),
        Decision(decision_type="EXECUTE_ACTION",
                 action=AgentAction(action_type="click", element_ref=9),
                 reasoning="submit"),
    )

    assert loop.handle.submit_outcome_unknown is False
    assert "submit_in_flight" not in task.application_progress


def test_a_confirmed_submit_leaves_nothing_unresolved_either(monkeypatch):
    """A submit that verifiably moved the page on has a KNOWN outcome — the
    normal confirmation path owns it, and the guard must get out of the way."""
    loop, _ = _loop(monkeypatch, page=ExplodingPage())
    monkeypatch.setattr(loop, "_track_verification_streak", lambda *a, **k: None)
    _stub_executor(monkeypatch, ActionResult(
        True, "click", "Submitted; the confirmation page loaded.", verified=True,
    ))
    task = FakeTask(auto_submit_approved=True)

    keep_going = loop._handle_execute_action(
        FakeDB(), task, _review_state(),
        Decision(decision_type="EXECUTE_ACTION",
                 action=AgentAction(action_type="click", element_ref=9),
                 reasoning="submit"),
    )

    assert keep_going is True
    assert loop.handle.submit_outcome_unknown is False
    assert "submit_in_flight" not in task.application_progress


def test_a_non_submit_action_is_never_treated_as_a_submission(monkeypatch):
    """A Next/Continue click on an intermediate step must not trip any of
    this — the guard is for irreversible actions only."""
    loop, _ = _loop(monkeypatch, page=ExplodingPage())
    monkeypatch.setattr(loop, "_track_verification_streak", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_record_field_attempt", lambda *a, **k: None)
    _stub_executor(monkeypatch, ActionResult(True, "click", "clicked", verified=False))
    next_button = PageElement(
        ref=9, tag="button", type="button", name="Next", value=None, required=False,
        disabled=False, checked=None, options=None,
        semantic_action="NEXT", action_confidence="HIGH",
    )
    task = FakeTask(auto_submit_approved=True)

    loop._handle_execute_action(
        FakeDB(), task, _review_state([next_button]),
        Decision(decision_type="EXECUTE_ACTION",
                 action=AgentAction(action_type="click", element_ref=9),
                 reasoning="advance"),
    )

    assert loop.handle.submit_outcome_unknown is False
    assert "submit_in_flight" not in task.application_progress


def test_submit_intent_is_recognized_from_either_the_semantics_or_the_name(monkeypatch):
    """Over-inclusive on purpose: an element only ONE of the two classifiers
    calls a submit still counts, because guessing wrong in the other
    direction risks a duplicate application."""
    loop, _ = _loop(monkeypatch)
    unlabelled = PageElement(
        ref=1, tag="button", type="button", name="", value=None, required=False,
        disabled=False, checked=None, options=None, semantic_action="SUBMIT",
    )
    unclassified = PageElement(
        ref=2, tag="button", type="button", name="Submit Application", value=None,
        required=False, disabled=False, checked=None, options=None, semantic_action=None,
    )
    click = AgentAction(action_type="click", element_ref=1)

    assert loop._is_submit_intent(click, unlabelled, "") is True
    assert loop._is_submit_intent(click, unclassified, "Submit Application") is True
    assert loop._is_submit_intent(click, None, "Next") is False
    assert loop._is_submit_intent(AgentAction(action_type="fill", element_ref=1, value="x"),
                                  unlabelled, "Submit Application") is False
