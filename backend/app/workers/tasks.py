"""
Queued units of work (Phase 2c). One apply run = one task.

Task names are STABLE STRINGS (`autogram.apply.*`), not module-path-derived, so
a future module move doesn't orphan in-flight messages.

`max_retries=0`: an apply run must never be silently retried by Celery — a
redelivery after a lost worker is fine (the lease + `automation_ownership`
re-check state), but an *automatic* retry of a run that raised could resubmit
to a real employer. Retries are a deliberate user action
(`POST /applications/{id}/retry`), never automatic.
"""

from __future__ import annotations

from app.workers import runtime
from app.workers.celery_app import celery_app


@celery_app.task(name="autogram.apply.run_application", max_retries=0, acks_late=True)
def run_application_task(
    application_id: str, resume_document_id: str, job_description: str | None = None
) -> dict:
    return runtime.run_application(application_id, resume_document_id, job_description)


@celery_app.task(name="autogram.apply.run_autonomous_task", max_retries=0, acks_late=True)
def run_autonomous_task(task_id: str) -> dict:
    return runtime.run_autonomous_task(task_id)
