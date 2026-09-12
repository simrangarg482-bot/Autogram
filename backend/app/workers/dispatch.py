"""
Broker-aware dispatch (Phase 2c-2f) — the one place the "queue or in-process?"
choice is made, so API routes don't each branch on config.

- `CELERY_BROKER_URL` set  → `.delay()` the run to the worker pool — autopilot
  AND copilot runs alike. Through 2c-2e a copilot run had to stay in-process
  because its finished browser only lived in `_OPEN_REVIEW_SESSIONS` memory
  in whatever process ran it, and `POST /applications/{id}/approve` could
  only ever reach that same process. 2f removes that constraint: the worker
  that finishes a copilot run keeps its lease + browser and polls for a
  cross-process `approve` signal (`app/workers/runtime.py`,
  `automation/coordination/control.py`), so any API replica can request the
  approval and the worker that's actually holding the browser services it.
- unset                    → run it in this process (`BackgroundTasks` for the
  deterministic run, a daemon thread for the autonomous loop).

**Both paths go through `app/workers/runtime.py`**, so every run acquires the
same Redis lease + heartbeat regardless of where it executes. That's what lets
the periodic orphan sweep (Phase 2d) tell a live in-process run from an
abandoned one — without it, the sweep would reconcile runs this very process
is driving.
"""

from __future__ import annotations

import logging
import threading

from fastapi import BackgroundTasks

from app.core.config import CELERY_BROKER_URL
from app.workers import runtime

logger = logging.getLogger(__name__)


def dispatch_application_run(
    background_tasks: BackgroundTasks,
    application_id: str,
    resume_document_id: str,
    job_description: str | None = None,
) -> str:
    """Returns 'queued' or 'background' for logging/response context."""
    if CELERY_BROKER_URL:
        from app.workers.tasks import run_application_task

        run_application_task.delay(application_id, resume_document_id, job_description)
        return "queued"
    background_tasks.add_task(
        runtime.run_application, application_id, resume_document_id, job_description
    )
    return "background"


def dispatch_autonomous_task(task_id: str) -> str:
    if CELERY_BROKER_URL:
        from app.workers.tasks import run_autonomous_task

        run_autonomous_task.delay(task_id)
        logger.info(
            "Task %s: queued to Celery — it will not run until a worker "
            "(`celery -A app.workers.celery_app.celery_app worker`) consumes it.",
            task_id,
        )
        return "queued"
    threading.Thread(
        target=runtime.run_autonomous_task, args=(task_id,), daemon=True, name=f"agent-task-{task_id}"
    ).start()
    logger.info("Task %s: dispatched to an in-process background thread.", task_id)
    return "background"
