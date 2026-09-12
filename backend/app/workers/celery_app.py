"""
Celery application for the apply-job worker (Phase 2).

Broker + result backend are `REDIS_URL` (see `app/core/config.py`). When
`REDIS_URL` is unset the app falls back to an in-memory broker so imports and
tests don't fail — but a real worker cannot run without Redis, and
`_require_broker()` (called from the worker entrypoint) enforces that.

Config choices, and why:

- `task_acks_late=True` + `task_reject_on_worker_lost=True` — a task is only
  acked after it finishes, so a worker that dies mid-run has its task
  redelivered rather than silently lost. Safe here because the run itself is
  idempotency-guarded (`automation_ownership` + the run lease), so a
  redelivered apply re-checks state before doing anything.
- `worker_prefetch_multiplier=1` — apply runs take minutes; a worker must not
  reserve a queue of them it can't start.
- `worker_max_tasks_per_child` — recycle the process periodically so a leaked
  Chromium handle can't accumulate.
"""

from __future__ import annotations

import logging
import ssl

from celery import Celery

from app.core.config import CELERY_BROKER_URL, CELERY_RESULT_BACKEND, WORKER_CONCURRENCY

logger = logging.getLogger(__name__)

_broker = CELERY_BROKER_URL or "memory://"
_backend = CELERY_RESULT_BACKEND or "cache+memory://"

celery_app = Celery("autogram", broker=_broker, backend=_backend)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_concurrency=WORKER_CONCURRENCY,
    worker_max_tasks_per_child=50,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
)

# celery's redis transport refuses a rediss:// (TLS) URL unless ssl_cert_reqs
# is spelled out explicitly — unlike redis-py's from_url (used elsewhere in
# this app, see app/core/redis_client.py), which defaults it to CERT_REQUIRED
# on its own. CERT_REQUIRED here matches that default: Upstash/managed Redis
# present a publicly-trusted cert, so the system trust store already verifies it.
if _broker.startswith("rediss://"):
    celery_app.conf.broker_use_ssl = {"ssl_cert_reqs": ssl.CERT_REQUIRED}
if _backend.startswith("rediss://"):
    celery_app.conf.redis_backend_use_ssl = {"ssl_cert_reqs": ssl.CERT_REQUIRED}

# Import the task module so `@celery_app.task` registrations run on app import.
from app.workers import tasks  # noqa: E402,F401


def _require_broker() -> None:
    """Fail loudly if the worker is starting without a real broker. Called from
    the container start command, not at import (tests import this module)."""
    if CELERY_BROKER_URL is None:
        raise RuntimeError(
            "The apply worker requires REDIS_URL (used as the Celery broker). "
            "Set it in the worker service's environment."
        )
