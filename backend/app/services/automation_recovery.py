"""
Recovery of orphaned automation ownership.

## The problem this exists to solve

Both automation paths persist "this attempt owns this job" as a STATUS, and
drive it from process memory:

* autonomous — `runner.py::_REGISTRY` (a `TaskHandle` per task, holding the
  `threading.Event`s for resume/cancel) plus the daemon thread running
  `AutonomousAgentLoop`;
* deterministic — FastAPI `BackgroundTasks` dispatching
  `applications.py::_run_application`, which launches Playwright on a one-off
  thread, plus `application_flow_manager._OPEN_REVIEW_SESSIONS` holding a
  `copilot_review` run's deliberately-still-open browser.

`automation_ownership.find_active_automation` reads ONLY the status — it has no
liveness signal, by design. So a status that outlives the thread behind it is
indistinguishable from automation that is genuinely running, and the job stays
blocked with `409 active_automation_exists` forever.

Empirically reproduced in `automation/tests/test_crash_recovery.py` before this
module existed. Two genuinely different failure shapes came out of that audit,
and they need different fixes:

1. **The thread dies, the process lives.** `_run_application`'s outer
   `except Exception` logged and rolled back without ever writing a terminal
   status, so `processing` was left behind while the server carried on serving
   requests. No restart follows, so no startup hook can help — this must be
   fixed where it happens, and is: `_run_application` now calls
   `try_recover_application` in that handler.
2. **The process dies.** Nothing is left to write anything, so the fix has to
   run on the way back up — `reconcile_orphaned_automation_on_startup`, called
   from `app/main.py` next to the autonomous reconciler that already existed.

## Why startup reconciliation is sound here

**Without `REDIS_URL`:** at process start these registries are EMPTY by
construction (fresh module-level dicts), so any active status in the database
was left by a previous process. This runs at startup only — single process, so
there is never a live run in another process to misjudge.

**With `REDIS_URL` (Phase 2d):** every run — in-process or in a Celery worker —
holds a Redis **lease** it heartbeats (`automation/coordination/lease.py`,
taken by `app/workers/runtime.py`). So "is this row orphaned?" stops being an
inference from process lifetime and becomes a direct check: `lease.is_held()`.
That check holds for a *periodic* pass too, so this same function is scheduled
every ~60 s (`app/core/scheduler.py`) — a dead worker's run is released within
about a minute instead of waiting for an API restart. The lease skip is a
no-op when Redis is unset (`is_held` is always False), so the single-process
behaviour above is unchanged.

Recovery is race-safe either way (advisory lock + atomic conditional `UPDATE`):
two recovery passes, or a recovery racing a fresh start, can't disagree.

## Why `processing` is recovered to `needs_review` and never to `failed`

This is the most consequential decision in the module.

`failed` is in `applications.py::RETRYABLE_STATUSES`, and `retry_application`
resets such a row to `pending` and runs the automation again. A run that died at
`processing` may already have clicked Submit — the employer's system is outside
our database transaction, so the row genuinely cannot tell us. Recovering to
`failed` would therefore invite a duplicate application to a real employer, for
no better reason than that our own persistence lost the race.

`needs_review` is the status this repository ALREADY uses for precisely this
ambiguity: `application_flow_manager.submit_and_confirm` returns it when submit
was clicked but no confirmation could be detected, with a message telling the
user to verify on the ATS before retrying. Reusing it means the crash case and
the in-run case say the same thing to the user, and no new state had to be
invented. It is still retryable — but only by a deliberate user action, never
automatically, which is exactly the required behaviour.

`pending` is different and is recovered to `failed`: `mark_processing` is the
first thing `_run_application` does, well before Playwright launches, so a
`pending` row provably never opened a browser and carries no submission
ambiguity at all.

`copilot_review` is recovered to `needs_review` too, but for a different
reason: such a run definitively did NOT submit (awaiting approval is the whole
point), so there is no ambiguity — but its open browser died with the process,
so `POST /applications/{id}/approve` can never succeed again
(`submit_open_review_session` finds no session and raises 409 forever). The form
work is real and a human should look at it, which is what `needs_review` means.

## REMAINING LIMITATIONS

* A Redis outage long enough for a live run's lease to lapse (>45 s with no
  successful heartbeat) makes that run briefly look orphaned. The window is
  small, the heartbeat logs loudly on loss, and a recover-to-`needs_review` is
  conservative (never `failed`, never auto-resubmit) — but it is possible.
* A crash between the browser's Submit click and ANY local persistence is
  recovered to `needs_review`, which is honest but not precise: we cannot say
  whether the employer received it. Exactly-once submission is not claimed
  anywhere, and cannot be — see `submit_and_confirm`.
* Copilot runs still keep their held-open browser in the process that ran them
  (`_OPEN_REVIEW_SESSIONS`), so a recovered `copilot_review` row means that
  browser is gone — the run is sent to `needs_review` for a human to restart.
  Cross-worker copilot approve is roadmap 2f.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.db_models import Application
from app.services import audit_log_repository as audit_log_repo
from app.services import automation_ownership
from automation.coordination import lease

logger = logging.getLogger(__name__)

#: Deterministic statuses that mean "an attempt owns this job", mapped to what
#: an ORPHANED one of them is recovered to, plus the reason persisted on the row
#: so the user is told what actually happened rather than just "failed".
#:
#: MUST stay consistent with `applications.py::IN_PROGRESS_STATUSES` — the same
#: three statuses, because those are exactly the ones that block a job. A test
#: (`test_crash_recovery.py`) pins the two together: a new in-progress status
#: that nothing recovers would be a new permanent-orphan state.
DETERMINISTIC_RECOVERY: dict[str, tuple[str, str]] = {
    "pending": (
        "failed",
        "Automation never started — the server stopped before this application's "
        "run could begin (no browser was ever opened for it). Nothing was "
        "submitted; you can safely try again.",
    ),
    "processing": (
        "needs_review",
        "Automation was interrupted while it was running, so its outcome could "
        "not be recorded. This application may or may not have been submitted — "
        "please verify on the employer's site before trying again, since "
        "resubmitting an application that did go through would apply twice.",
    ),
    "copilot_review": (
        "needs_review",
        "This application was filled in and waiting for your approval, but the "
        "server restarted and the browser session it was holding open is gone. "
        "Nothing was submitted. Review it and start a new attempt if you still "
        "want to apply.",
    ),
}


def try_recover_application(
    db: Session, *, application_id: str, from_status: str, reason_override: str | None = None,
) -> str | None:
    """Atomically move ONE deterministic attempt out of an orphaned active
    status. Returns the new status, or `None` if nothing was changed.

    The conditional `UPDATE ... WHERE application_id = ? AND status = ?` is the
    load-bearing part, and it does three jobs at once:

    * two recovery passes cannot both recover the same row — the loser matches
      zero rows;
    * recovery cannot clobber a status something else legitimately wrote in the
      meantime. This is not hypothetical: `_mark_waiting_for_human` writes
      `manual_required` from the flow manager's own thread, on its own session,
      and can land at any moment;
    * it never touches `applied` — a submitted attempt is not in
      `DETERMINISTIC_RECOVERY` at all, so recovery structurally cannot roll back
      submission history.

    Deliberately does NOT take the advisory lock itself: the in-process crash
    handler calls this from a background thread where the extra round trip buys
    nothing (the row it is recovering is the one its own dead run owned), while
    the startup reconciler takes the lock around its own call — see
    `reconcile_orphaned_applications_on_startup`.
    """
    mapping = DETERMINISTIC_RECOVERY.get(from_status)
    if mapping is None:
        raise ValueError(
            f"{from_status!r} is not a recoverable deterministic status; "
            f"expected one of {sorted(DETERMINISTIC_RECOVERY)}"
        )
    to_status, reason = mapping
    rows = (
        db.query(Application)
        .filter(Application.application_id == application_id, Application.status == from_status)
        .update(
            {
                "status": to_status,
                "failure_reason": reason_override or reason,
                "updated_at": datetime.now(timezone.utc),
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if rows == 0:
        # Not an error: the row moved on under us, which is the good outcome.
        logger.debug(
            "Application %s was no longer %s — nothing to recover.", application_id, from_status,
        )
        return None
    return to_status


def reconcile_orphaned_applications_on_startup(db: Session) -> int:
    """Deterministic-path counterpart to
    `runner.reconcile_orphaned_tasks_on_startup`.

    Called at process start from `app/main.py` AND (Phase 2d — despite the
    `_on_startup` name, kept to avoid churning ~40 call sites) periodically
    from `app/core/scheduler.py` when `REDIS_URL` is set.

    A row in a `DETERMINISTIC_RECOVERY` status is orphaned **unless a live run
    lease says a worker is driving it right now** (`lease.is_held`). Before
    Phase 2 the argument was "these registries are empty in a fresh process";
    now it's the lease, which also holds for a *periodic* pass and across
    replicas/workers. With `REDIS_URL` unset `lease.is_held` is always False,
    so this reduces to the original startup-only behaviour — which is why the
    scheduler only runs it periodically when Redis is present (see
    `scheduler.py`).

    Returns the number of applications recovered, for a one-line log.
    """
    # Snapshot the columns needed, rather than iterating live ORM objects.
    # `SessionLocal` leaves `expire_on_commit` at its default, so every commit
    # inside the loop below would expire the remaining objects and re-SELECT
    # them one at a time on next attribute access — and raise
    # `ObjectDeletedError` for any row that vanished in between. A snapshot
    # cannot go stale in a way that matters: the conditional UPDATE in
    # `try_recover_application` re-checks the status at write time, which is
    # exactly its job.
    orphaned = (
        db.query(
            Application.application_id, Application.user_id, Application.job_url, Application.status,
        )
        .filter(Application.status.in_(tuple(DETERMINISTIC_RECOVERY)))
        .all()
    )
    recovered = 0
    for application_id, user_id, job_url, prior_status in orphaned:
        # A worker currently holds this run's lease -> it's alive, not orphaned.
        # No-op without Redis (is_held is always False), so single-process
        # startup recovery is unchanged.
        if lease.is_held("application", application_id):
            continue
        # ERROR ISOLATION, same rule as the autonomous reconciler: one
        # unrecoverable row must not abort the pass and strand every later
        # orphan. `SQLAlchemyError` only — a programming bug must stay visible
        # rather than being silently turned into "recovered 0 applications".
        try:
            recovered += _recover_one(db, application_id, user_id, job_url, prior_status)
        except SQLAlchemyError:
            db.rollback()
            logger.exception(
                "Application %s: could not be recovered (was %s) — skipping and continuing.",
                application_id, prior_status,
            )
    return recovered


def _recover_one(
    db: Session, application_id: str, user_id: str, job_url: str, prior_status: str,
) -> int:
    """Recover exactly ONE orphaned attempt. Returns 1 if this call performed
    the transition, 0 if the row had already moved on (an expected race)."""
    # Serialize against a concurrent `POST /applications/start` or
    # `POST /agent/tasks` for the same job, reusing the SAME advisory lock
    # those routes take rather than introducing a second locking scheme.
    # Consequence, and it is the correct one: a start that arrives first
    # holds the lock, still sees this attempt as active, and is refused —
    # the stale attempt stays authoritative until recovery resolves it. The
    # user's retry then succeeds.
    automation_ownership.reserve_job_automation(db, user_id=user_id, job_url=job_url)
    new_status = try_recover_application(
        db, application_id=application_id, from_status=prior_status,
    )
    if new_status is None:
        return 0
    audit_log_repo.record_event(
        db, user_id=user_id, application_id=application_id,
        event_type="automation_recovered", actor="system",
        metadata={
            "reason": "orphaned_at_startup",
            "prior_status": prior_status,
            "new_status": new_status,
            # The distinction that matters for submission safety: only a
            # run that reached `processing` could possibly have clicked
            # Submit. Recorded so this is answerable after the fact without
            # re-deriving it from status alone.
            "may_have_submitted": prior_status == "processing",
        },
    )
    logger.warning(
        "Application %s: recovered orphaned %s -> %s at startup.",
        application_id, prior_status, new_status,
    )
    return 1


def reconcile_orphaned_automation_on_startup(db: Session) -> dict[str, int]:
    """Single entry point covering BOTH paths — used at process start
    (`app/main.py`) and periodically (`app/core/scheduler.py`, Redis only;
    `_on_startup` in the name is historical).

    Imported lazily because `runner` pulls in the agent loop (and through it
    Playwright), which has no business being imported by a service module at
    definition time.

    Each path is reconciled independently: a failure in one must not stop the
    other from running, since either left unreconciled means jobs stay blocked.
    """
    from automation.agents.autonomous.runner import reconcile_orphaned_tasks_on_startup

    counts = {"autonomous_tasks": 0, "applications": 0}
    for key, fn in (
        ("autonomous_tasks", reconcile_orphaned_tasks_on_startup),
        ("applications", reconcile_orphaned_applications_on_startup),
    ):
        try:
            counts[key] = fn(db)
        except Exception:
            db.rollback()
            logger.exception("Orphan reconciliation failed for %s — continuing.", key)
    return counts
