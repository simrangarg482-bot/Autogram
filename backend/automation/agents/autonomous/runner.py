"""
Shared execution + signalling for `AutonomousAgentLoop`.

`execute_task_sync(task_id)` runs the loop to completion on the current
thread. It is called two ways (chosen by `app/workers/dispatch.py`):

- **no `CELERY_BROKER_URL`** — from a daemon `threading.Thread` in the API
  process (`dispatch_autonomous_task`), wrapped by `app/workers/runtime.py`
  for the lease + heartbeat;
- **broker set** — inside a Celery worker (`app/workers/tasks.py`), same
  wrapper.

`_REGISTRY` still holds the live `TaskHandle` (the `threading.Event`s and the
open browser) for a run *in this process*. What changed in Phase 2b/2c: when a
resume / cancel / OTP arrives at a process that ISN'T running the task,
`signal_resume` / `request_cancel` / `deliver_secret` check the run's lease
and, if a worker holds it, hand the signal over via
`automation/coordination/control.py` (a Redis flag hash + a one-shot OTP
mailbox). The loop consumes those alongside its local handle
(`loop.py::_wait_for_resume`, `_try_consume_pending_secret`).

Known limitation, unchanged: an in-flight PAUSED task's actual open browser
tab does not survive a process restart (`TaskHandle` is in memory only) — the
DB row does, so status stays accurate, but a resume after a restart starts a
fresh tab at `job_url`. See `AUTONOMOUS_AGENT.md`.
"""

from __future__ import annotations

import logging
import threading

from sqlalchemy.exc import SQLAlchemyError

from app.services import audit_log_repository as audit_log_repo
from app.services import autonomous_task_repository as task_repo
from app.services import human_interaction_repository as human_interaction_repo
from automation.agents.autonomous.loop import AutonomousAgentLoop, TaskHandle
from automation.coordination import control, lease

#: The lease kind for autonomous-agent runs (see automation/coordination/lease.py).
_LEASE_KIND = "autonomous_task"

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, TaskHandle] = {}
_REGISTRY_LOCK = threading.Lock()


def _get_or_create_handle(task_id: str) -> TaskHandle:
    with _REGISTRY_LOCK:
        handle = _REGISTRY.get(task_id)
        if handle is None:
            handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
            _REGISTRY[task_id] = handle
        return handle


def _claim_handle(task_id: str) -> TaskHandle | None:
    """Atomically "claim this task for this thread": returns a fresh handle if
    nobody in this process was already driving `task_id`, or `None` if
    somebody is.

    Check-and-claim has to happen under ONE lock acquisition. `is_running()`
    followed by `_get_or_create_handle()` took the lock twice, so two
    dispatches for the same task arriving together could both observe "not
    running" and both go on to drive the SAME `TaskHandle` — two loops
    sharing one browser and one resume/cancel event. Without `REDIS_URL` the
    lease in `app/workers/runtime.py` is a no-op (`lease.acquire` returns
    True unconditionally in a single process), so this is the only thing
    standing between a double dispatch and a double run."""
    with _REGISTRY_LOCK:
        if task_id in _REGISTRY:
            return None
        handle = TaskHandle(resume_event=threading.Event(), cancel_requested=threading.Event())
        _REGISTRY[task_id] = handle
        return handle


def is_running(task_id: str) -> bool:
    with _REGISTRY_LOCK:
        return task_id in _REGISTRY


def list_live_task_ids() -> list[str]:
    """Task ids with a live in-process handle right now — an open browser, if
    one was ever launched, lives only with one of these. Used at process
    shutdown (`app/main.py`) to log exactly what a restart is about to orphan,
    instead of finding out indirectly from `reconcile_orphaned_tasks_on_startup`
    after the fact."""
    with _REGISTRY_LOCK:
        return list(_REGISTRY.keys())


def execute_task_sync(task_id: str) -> None:
    """Run the loop for `task_id` to completion ON THE CURRENT THREAD.

    This is the shared body: `start_task_background` wraps it in a daemon
    thread (in-process path), and `app/workers/runtime.py::run_autonomous_task`
    calls it directly inside a Celery worker (queued path, lease-guarded).
    Safe to call for a task that already has a live handle — no-op then."""
    handle = _claim_handle(task_id)
    if handle is None:
        logger.debug("execute_task_sync: task %s already has a live handle.", task_id)
        return
    loop = AutonomousAgentLoop(task_id, handle)
    _run_and_cleanup(task_id, loop)


# NOTE: the in-process daemon-thread dispatch moved to
# `app/workers/dispatch.py::dispatch_autonomous_task`, so that path goes
# through `app/workers/runtime.py` (lease + heartbeat) like the queued one.
# `execute_task_sync` above is the shared body both call.


def deliver_secret(task_id: str, request_id: str, value: str) -> bool:
    """Hands a transient OTP/MFA code to the live in-process loop for
    `task_id` and wakes it. Returns False (delivers nothing) if there is no
    live handle — e.g. the process restarted since the task paused — in
    which case the caller (`app/api/human_interaction.py`) must NOT fall back
    to `start_task_background` the way the plain `/resume` route does: a
    fresh tab has no verification field to fill, so silently "resuming" would
    just fail confusingly. The caller instead surfaces a new `LOGIN_REQUIRED`
    request asking the human to restart, per AUTONOMOUS_AGENT.md.

    The value lives only in `TaskHandle.pending_secret` in this process's
    memory, for exactly as long as it takes the loop to notice and consume
    it (`AutonomousAgentLoop._try_consume_pending_secret`, which clears the
    slot immediately after reading) — never persisted, logged, or returned.

    Multi-worker (Phase 2b): if the loop is running in a DIFFERENT process
    (no local handle, but the run's lease is held), the code is handed over
    via `control.deposit_secret` — a one-shot Redis mailbox the owning worker
    `LPOP`s exactly once. See that function's security note on the tradeoff.
    """
    with _REGISTRY_LOCK:
        handle = _REGISTRY.get(task_id)
    if handle is not None:
        handle.pending_secret = {"request_id": request_id, "value": value}
        handle.resume_event.set()
        return True
    if lease.is_held(_LEASE_KIND, task_id):
        return control.deposit_secret(task_id, request_id, value)
    return False


def signal_resume(task_id: str) -> bool:
    """Wakes a paused task's loop. Returns True if the signal was delivered —
    either to a live in-process handle, or (Phase 2b) to the worker that holds
    this run's lease, via the Redis control channel.

    Returns False only when NOBODY owns the run (no handle, no lease) — e.g.
    the process died after the task paused. The caller
    (`app/api/autonomous_agent.py`) then falls back to `start_task_background`,
    which begins a fresh run from persisted state but a NEW browser tab — see
    the module docstring's known limitation."""
    with _REGISTRY_LOCK:
        handle = _REGISTRY.get(task_id)
    if handle is not None:
        handle.resume_event.set()
        return True
    if lease.is_held(_LEASE_KIND, task_id):
        control.send_signal(_LEASE_KIND, task_id, "resume")
        return True
    return False


def request_cancel(task_id: str) -> bool:
    """Signals cancellation to the loop. Callers persist the CANCELLED status
    themselves regardless of the return value; this just makes the running
    loop stop promptly. Delivered locally, or (Phase 2b) to the lease holder."""
    with _REGISTRY_LOCK:
        handle = _REGISTRY.get(task_id)
    if handle is not None:
        handle.cancel_requested.set()
        handle.resume_event.set()  # wake it up if it was blocked waiting on a human
        return True
    if lease.is_held(_LEASE_KIND, task_id):
        control.send_signal(_LEASE_KIND, task_id, "cancel")
        return True
    return False


def _run_and_cleanup(task_id: str, loop: AutonomousAgentLoop) -> None:
    try:
        loop.run()
    finally:
        with _REGISTRY_LOCK:
            _REGISTRY.pop(task_id, None)


def reconcile_orphaned_tasks_on_startup(db) -> int:
    """Called once, at process startup (`app/main.py`), BEFORE anything can
    call `start_task_background`/`signal_resume`/`deliver_secret` — at that
    moment `_REGISTRY` is guaranteed empty (this is a fresh process), so any
    `AutonomousTask` still `RUNNING`/`RESUMING` in the database was left
    mid-flight by a PREVIOUS process that died without a live thread to ever
    finish, pause, or fail it. Left alone, such a task would sit in that
    status forever: nothing in this codebase re-scans for "tasks nobody is
    driving anymore."

    This never pretends automation "resumed" — it fails the task explicitly,
    safely, and observably, with a message telling the human to start a new
    task rather than silently vanishing or (worse) misreporting progress.
    `WAITING_FOR_HUMAN`/`WAITING_FOR_APPROVAL` tasks are untouched here — see
    `task_repo.list_orphaned_running_tasks`'s docstring for why those already
    have correct restart handling elsewhere.

    Also expires any `HumanInteractionRequest` left `PENDING` for one of
    these tasks (there normally isn't one — a RUNNING/RESUMING task has no
    active pause by definition — but a request created an instant before the
    crash, before the task's own status write landed, is possible; this is
    cheap defense against that sliver of a race).

    Returns the number of tasks reconciled (for a one-line startup log)."""
    # Snapshot the columns needed, rather than iterating live ORM objects.
    # `SessionLocal` leaves `expire_on_commit` at its default, so the first
    # claim below expires every remaining object in this list; touching one
    # afterwards re-SELECTs it one row at a time, and raises
    # `ObjectDeletedError` for any row that has since gone — which would abort
    # the whole pass and leave the rest of the orphans blocking their jobs.
    # (Observed for real: a concurrently-deleted task took down reconciliation.)
    # A snapshot cannot go stale in a way that matters, because
    # `try_claim_orphan_failed` re-checks the status at write time.
    orphaned = [
        (t.task_id, t.user_id, t.current_status, bool(t.auto_submit_approved))
        for t in task_repo.list_orphaned_running_tasks(db)
    ]
    reconciled = 0
    for task_id, user_id, prior_status, approved in orphaned:
        # Phase 2d: a worker currently holding this run's lease is alive and
        # driving it — not orphaned. No-op without Redis (is_held always
        # False), so single-process startup recovery is unchanged. This is
        # also what makes it safe to call periodically, not just at startup.
        #
        # A Redis outage must not abort the WHOLE pass. `lease.is_held` reads
        # Redis with no guard of its own, so one unreachable server used to
        # propagate straight out of this function and leave EVERY orphan
        # unreconciled — exactly the failure this function exists to prevent,
        # caused by the recovery mechanism itself. Skipping this one task is
        # the conservative outcome: a task nobody is driving stays put for the
        # next pass, and a task a live worker IS driving is never failed out
        # from under it.
        try:
            if lease.is_held(_LEASE_KIND, task_id):
                continue
        except Exception:  # noqa: BLE001
            logger.warning(
                "Task %s: lease lookup failed (was %s) — skipping it this pass.",
                task_id, prior_status, exc_info=True,
            )
            continue
        # ERROR ISOLATION. One unreconcilable task must not abort the pass and
        # leave every later orphan blocking its job — that is the exact failure
        # this whole function exists to prevent, so it must not be able to
        # happen to the fix itself.
        #
        # Deliberately NOT a bare `except Exception: pass`. `SQLAlchemyError`
        # covers the database-level things a single row can genuinely hit
        # (a row deleted underneath us, a serialization failure, a constraint
        # surprise) and is recorded and skipped. Anything else — a TypeError, an
        # AttributeError, a bug in this function — is left to propagate to
        # `reconcile_orphaned_automation_on_startup`, which logs it with a
        # traceback. Swallowing those would turn a programming bug into a
        # permanently silent no-op.
        try:
            reconciled += _reconcile_one_task(db, task_id, user_id, prior_status, approved)
        except SQLAlchemyError:
            db.rollback()
            logger.exception(
                "Task %s: could not be reconciled (was %s) — skipping and continuing.",
                task_id, prior_status,
            )
    return reconciled


def _reconcile_one_task(db, task_id: str, user_id: str, prior_status: str, approved: bool) -> int:
    """Reconcile exactly ONE orphaned task. Returns 1 if it was claimed and
    failed here, 0 if another worker had already moved it on (an expected
    race, not an error). Split out of the loop above purely so a per-task
    failure can be isolated without wrapping the whole body in a `try`."""
    # Submission ambiguity. The executor refuses to click a final submit
    # control unless `auto_submit_approved` is set
    # (`executor.py::is_submit_control_name`), and only
    # `POST /agent/tasks/{id}/approve` ever sets it. That flag is a
    # PERSISTED column, so it tells us — after the fact, from the row alone
    # — whether this task could possibly have submitted before it died.
    #
    # It stays FAILED either way: FAILED releases the job (the orphan is the
    # bug being fixed) and never resubmits anything by itself, since only a
    # deliberate new user action starts another task. What changes is that
    # the user is TOLD the outcome is uncertain, instead of being handed a
    # bare "interrupted, start a new task" that quietly invites a duplicate
    # application to a real employer.
    # A task in CREATED/ANALYZING_JOB never got past `AutonomousAgentLoop.run()`
    # calling `_ensure_browser` — that only flips the status to RUNNING once a
    # browser context actually opened (see `loop.py`). So a task orphaned at
    # one of these two statuses never had a browser to lose: it was created,
    # dispatched (`dispatch_autonomous_task`), and then nobody ever executed
    # it — e.g. queued to Celery with no worker consuming it. That is a
    # different, more useful thing to tell the user than "a restart killed
    # your browser," and it can never be a submission-ambiguity case (no
    # executor call was ever reachable before a browser exists).
    never_started = prior_status in ("CREATED", "ANALYZING_JOB")
    may_have_submitted = approved and prior_status in ("RUNNING", "RESUMING")
    if never_started:
        error = (
            "Automation never started — this task was queued but no automation worker picked it "
            "up. No browser session was ever opened and nothing was submitted. Please confirm the "
            "automation worker/queue is running, then start a new task."
        )
    elif may_have_submitted:
        error = (
            "Automation was interrupted by a server restart after you had approved submission, "
            "so its outcome could not be recorded. This application MAY OR MAY NOT have been "
            "submitted — please check on the employer's site before starting a new task, since "
            "applying again when the first one went through would submit twice."
        )
    else:
        error = (
            "Automation was interrupted by a server restart while this task was in progress "
            "(no in-progress browser session survives a restart). Nothing was submitted — "
            "please start a new task."
        )
    # Conditional UPDATE rather than `mark_failed`: two recovery passes
    # cannot both claim this task, and a status that changed underneath us
    # (including COMPLETED) is never clobbered. See
    # `task_repo.try_claim_orphan_failed`.
    if not task_repo.try_claim_orphan_failed(db, task_id, from_status=prior_status, error=error):
        logger.info(
            "Task %s: no longer %s at reconciliation time — left alone.", task_id, prior_status,
        )
        return 0
    active_request = human_interaction_repo.get_active_for_task(db, task_id)
    if active_request is not None:
        # Read the id BEFORE `mark_expired` commits. Afterwards the object
        # is expired, and touching it re-SELECTs the row — raising
        # `ObjectDeletedError` if it has since been deleted. Same hazard
        # that took down the task loop above; the fix is the same.
        request_id = active_request.request_id
        human_interaction_repo.mark_expired(db, active_request)
        audit_log_repo.record_event(
            db, user_id=user_id, autonomous_task_id=task_id,
            event_type="human_request_expired", actor="system",
            metadata={"request_id": request_id, "reason": "orphaned_at_startup"},
        )
    audit_log_repo.record_event(
        db, user_id=user_id, autonomous_task_id=task_id,
        event_type="automation_failed", actor="system",
        metadata={
            "reason": "never_started_orphan" if never_started else "orphaned_at_startup",
            "prior_status": prior_status,
            # Recorded so "could this one have reached the employer?" is
            # answerable later from the audit trail alone, rather than
            # having to re-derive it from a status that is now just FAILED.
            "may_have_submitted": may_have_submitted,
        },
    )
    logger.warning(
        "Task %s: reconciled orphaned %s task to FAILED (never_started=%s, may_have_submitted=%s).",
        task_id, prior_status, never_started, may_have_submitted,
    )
    return 1
