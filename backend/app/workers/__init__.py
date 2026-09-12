"""
The apply-job worker (Phase 2 — PLATFORM_ROADMAP.md).

Lives in `app/`, not `automation/`, because the task bodies orchestrate — they
call `app.api.applications._run_application` and the autonomous runner — and
`automation/` must never import `app.api.*` (ARCHITECTURE.md §1a). The worker
is the same kind of thing `app/api/` is: a producer/orchestrator on top of the
`automation/` mechanics.

- `celery_app.py` — the Celery application, broker/backend = `REDIS_URL`.
- `runtime.py`    — lease-guarded (acquire → heartbeat → release) execution
  wrappers each task delegates to.
- `tasks.py`      — `run_application_task` / `run_autonomous_task`.

Run it with:

    celery -A app.workers.celery_app.celery_app worker \
        --loglevel=INFO --concurrency=${WORKER_CONCURRENCY:-2}

Same image as the API (`backend/Dockerfile`), different start command.

**Dispatch is broker-aware** (`_dispatch_*` in the producers): with
`CELERY_BROKER_URL` set, runs are `.delay()`d here; unset, they stay on the
existing in-process path (`BackgroundTasks` / a daemon thread). So the
single-process dev/test setup is unchanged.
"""
