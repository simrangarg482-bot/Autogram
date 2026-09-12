"""
Phase 2c/2f — broker-aware dispatch (`app/workers/dispatch.py`).

`CELERY_BROKER_URL` set  -> `.delay()` to the worker — autopilot and copilot
runs alike (2f made copilot approve work cross-worker, so copilot no longer
needs to stay pinned to the API process).
`CELERY_BROKER_URL` unset -> the existing in-process path.
"""

import threading
from unittest.mock import MagicMock

from app.workers import dispatch


# ---------- deterministic apply ----------

def test_broker_set_enqueues_to_celery(monkeypatch):
    monkeypatch.setattr(dispatch, "CELERY_BROKER_URL", "redis://x")
    delay = MagicMock()
    monkeypatch.setattr("app.workers.tasks.run_application_task.delay", delay)
    bg = MagicMock()

    mode = dispatch.dispatch_application_run(bg, "app-1", "doc-1", "jd")

    assert mode == "queued"
    delay.assert_called_once_with("app-1", "doc-1", "jd")
    bg.add_task.assert_not_called()


def test_copilot_run_also_queues_to_celery_when_broker_set(monkeypatch):
    """Phase 2f: a copilot run is no longer special-cased to stay in-process —
    the worker that finishes it keeps the browser + lease and polls for the
    cross-process approve signal."""
    monkeypatch.setattr(dispatch, "CELERY_BROKER_URL", "redis://x")
    delay = MagicMock()
    monkeypatch.setattr("app.workers.tasks.run_application_task.delay", delay)
    bg = MagicMock()

    mode = dispatch.dispatch_application_run(bg, "app-1", "doc-1", "jd")

    assert mode == "queued"
    delay.assert_called_once_with("app-1", "doc-1", "jd")
    bg.add_task.assert_not_called()


def test_no_broker_uses_background_tasks(monkeypatch):
    monkeypatch.setattr(dispatch, "CELERY_BROKER_URL", None)
    bg = MagicMock()

    mode = dispatch.dispatch_application_run(bg, "app-1", "doc-1", "jd")

    assert mode == "background"
    from app.workers import runtime
    bg.add_task.assert_called_once_with(runtime.run_application, "app-1", "doc-1", "jd")


# ---------- autonomous ----------

def test_autonomous_broker_set_enqueues(monkeypatch):
    monkeypatch.setattr(dispatch, "CELERY_BROKER_URL", "redis://x")
    delay = MagicMock()
    monkeypatch.setattr("app.workers.tasks.run_autonomous_task.delay", delay)

    assert dispatch.dispatch_autonomous_task("t-1") == "queued"
    delay.assert_called_once_with("t-1")


def test_autonomous_no_broker_runs_in_a_thread_via_runtime(monkeypatch):
    monkeypatch.setattr(dispatch, "CELERY_BROKER_URL", None)
    ran = threading.Event()
    monkeypatch.setattr(
        "app.workers.runtime.run_autonomous_task",
        lambda tid: ran.set() or {"status": "done"},
    )

    assert dispatch.dispatch_autonomous_task("t-1") == "background"
    assert ran.wait(timeout=2)  # the daemon thread invoked the lease-guarded runtime
