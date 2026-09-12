"""
Lease-guarded execution wrappers the Celery tasks delegate to (Phase 2c).

Each wrapper: acquire the run's lease → start a heartbeat that renews it every
`lease.HEARTBEAT_SECONDS` → run the actual body → stop the heartbeat and
release. The lease is what lets reconciliation (Phase 2d) tell a live run from
an abandoned one, and what stops a redelivered task (`acks_late`) from
double-driving a run another worker already picked up.

The bodies are the SAME code the in-process path runs:
`app.api.applications._run_application` and
`automation.agents.autonomous.runner.execute_task_sync`. Nothing is
reimplemented here — this module only adds the lease + heartbeat around them.

Phase 2f adds one more thing on the application path: after `_run_application`
returns, `run_application` doesn't release the lease right away if the run
left a browser open for copilot review — `_watch_for_copilot_approval` below
keeps holding it (the heartbeat thread started by `_held_lease` is still
running) and polls for the cross-process `approve` signal, so a copilot run
started on this worker can be approved from any API replica.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from contextlib import contextmanager

from app.core.config import COPILOT_APPROVAL_MAX_WAIT_SECONDS, COPILOT_APPROVAL_POLL_SECONDS
from automation.coordination import control, lease

logger = logging.getLogger(__name__)

#: Identifies this worker process for lease ownership. Stable for the life of
#: the process; unique across processes/hosts.
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class LeaseHeldElsewhere(Exception):
    def __init__(self, kind: str, run_id: str, holder: str | None):
        super().__init__(f"{kind}:{run_id} lease held by {holder!r}")
        self.holder = holder


@contextmanager
def _held_lease(kind: str, run_id: str):
    if not lease.acquire(kind, run_id, WORKER_ID):
        raise LeaseHeldElsewhere(kind, run_id, lease.current_holder(kind, run_id))
    stop = threading.Event()

    def _heartbeat() -> None:
        # `stop.wait` returns True the moment the run finishes, so this exits
        # promptly rather than sleeping out a final interval.
        while not stop.wait(lease.HEARTBEAT_SECONDS):
            if not lease.renew(kind, run_id, WORKER_ID):
                # Lost the lease mid-run (Redis blip long enough for the TTL to
                # lapse, or a reconcile reclaimed it). We can't safely abort a
                # Playwright run from another thread; log loudly so the
                # double-drive is at least visible, and stop renewing so the
                # other holder isn't fought.
                logger.error(
                    "Lease for %s:%s LOST mid-run on %s — another worker may now own it.",
                    kind, run_id, WORKER_ID,
                )
                return

    beat = threading.Thread(target=_heartbeat, daemon=True, name=f"lease-{kind}-{run_id}")
    beat.start()
    try:
        yield
    finally:
        stop.set()
        beat.join(timeout=2)
        lease.release(kind, run_id, WORKER_ID)


def run_application(application_id: str, resume_document_id: str, job_description: str | None = None) -> dict:
    from app.api.applications import _run_application  # lazy — avoids an import cycle

    try:
        with _held_lease("application", application_id):
            _run_application(application_id, resume_document_id, job_description)
            _watch_for_copilot_approval(application_id)
    except LeaseHeldElsewhere as e:
        logger.warning("Application %s already owned by %s — not running here.", application_id, e.holder)
        return {"status": "skipped", "reason": "lease_held_elsewhere", "holder": e.holder}
    return {"status": "done", "application_id": application_id}


def _watch_for_copilot_approval(application_id: str) -> None:
    """Phase 2f. `_run_application` may have just left a browser open with
    status `copilot_review` (see
    `application_flow_manager.should_keep_browser_open`) — a no-op check via
    `list_open_review_sessions` if not, so every other run (autopilot,
    needs_review, failed, applied) returns from this immediately exactly like
    before 2f existed.

    When it IS a copilot review, this worker is the only process that can
    drive the submit click — `_OPEN_REVIEW_SESSIONS` is process-local memory
    — so it keeps polling the cross-process `approve` signal
    (`automation.coordination.control`, deposited by
    `POST /applications/{id}/approve` on whichever API replica received the
    request) for up to `COPILOT_APPROVAL_MAX_WAIT_SECONDS`. The caller
    (`run_application`) is still inside `_held_lease`'s `with` block the whole
    time, so the heartbeat thread keeps renewing the lease — that's what stops
    the orphan sweep (Phase 2d) from reclaiming a review nobody has acted on
    yet.

    Gives up (returns, letting `_held_lease` release normally) after the max
    wait — a review nobody acts on for that long is treated exactly like a
    process that died mid-review always was: the row stays `copilot_review`
    with no lease, and the next orphan sweep moves it to `needs_review`
    (`automation_recovery.DETERMINISTIC_RECOVERY`).

    No-op when Redis is unset: `control.consume_signal` would never see a
    signal (there's no cross-process channel at all in that mode), so this
    would just occupy a thread doing nothing useful for up to the max wait.
    The single-process fast path in `POST /applications/{id}/approve` already
    handles this case directly, synchronously, exactly as it did before 2f."""
    from app.core.redis_client import sync_redis

    if sync_redis() is None:
        return

    from automation.applications.application_flow_manager import list_open_review_sessions

    if application_id not in list_open_review_sessions():
        return

    deadline = time.monotonic() + COPILOT_APPROVAL_MAX_WAIT_SECONDS
    while application_id in list_open_review_sessions() and time.monotonic() < deadline:
        if control.consume_signal("application", application_id, "approve"):
            from app.api.applications import apply_copilot_approval  # lazy — avoids an import cycle
            from app.core.database import SessionLocal

            db = SessionLocal()
            try:
                apply_copilot_approval(db, application_id)
            finally:
                db.close()
            return
        time.sleep(COPILOT_APPROVAL_POLL_SECONDS)

    if application_id in list_open_review_sessions():
        logger.info(
            "Application %s: no approval received within %ss — releasing the lease; "
            "the next orphan sweep will move it to needs_review.",
            application_id, COPILOT_APPROVAL_MAX_WAIT_SECONDS,
        )


def run_autonomous_task(task_id: str) -> dict:
    from automation.agents.autonomous.runner import execute_task_sync

    try:
        with _held_lease("autonomous_task", task_id):
            execute_task_sync(task_id)
    except LeaseHeldElsewhere as e:
        logger.warning("Task %s already owned by %s — not running here.", task_id, e.holder)
        return {"status": "skipped", "reason": "lease_held_elsewhere", "holder": e.holder}
    return {"status": "done", "task_id": task_id}
