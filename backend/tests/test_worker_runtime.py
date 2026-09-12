"""
Phase 2c — the lease-guarded execution wrappers (`app/workers/runtime.py`).

The run body itself is the same `_run_application` / `execute_task_sync` the
in-process path calls; these tests pin only what `runtime` adds: acquire →
heartbeat → release, and skip-when-held-elsewhere.
"""

import time

import fakeredis
import pytest

from app.core import redis_client
from automation.coordination import control, lease
from app.workers import runtime


@pytest.fixture
def fake_redis(monkeypatch):
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "sync_redis", lambda: client)
    return client


def test_run_application_acquires_then_releases(fake_redis, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "app.api.applications._run_application",
        lambda app_id, doc_id, jd=None: seen.update(app_id=app_id, doc_id=doc_id, jd=jd),
    )

    result = runtime.run_application("app-1", "doc-1", "a JD")

    assert result == {"status": "done", "application_id": "app-1"}
    assert seen == {"app_id": "app-1", "doc_id": "doc-1", "jd": "a JD"}
    assert lease.is_held("application", "app-1") is False


def test_run_is_skipped_when_another_worker_holds_the_lease(fake_redis, monkeypatch):
    lease.acquire("application", "app-2", "another-worker")
    called = []
    monkeypatch.setattr("app.api.applications._run_application", lambda *a, **k: called.append(a))

    result = runtime.run_application("app-2", "doc-2")

    assert result["status"] == "skipped"
    assert called == []
    assert lease.current_holder("application", "app-2") == "another-worker"


def test_lease_released_even_when_the_run_raises(fake_redis, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("run blew up")

    monkeypatch.setattr("app.api.applications._run_application", boom)

    with pytest.raises(RuntimeError, match="run blew up"):
        runtime.run_application("app-3", "doc-3")

    assert lease.is_held("application", "app-3") is False


def test_heartbeat_renews_the_lease_during_a_long_run(fake_redis, monkeypatch):
    monkeypatch.setattr(lease, "HEARTBEAT_SECONDS", 0.05)
    renewals = []
    real_renew = lease.renew
    monkeypatch.setattr(
        lease, "renew",
        lambda *a, **k: (renewals.append(1), real_renew(*a, **k))[1],
    )
    monkeypatch.setattr(
        "app.api.applications._run_application", lambda *a, **k: time.sleep(0.25)
    )

    runtime.run_application("app-4", "doc-4")

    assert len(renewals) >= 2  # heartbeat fired while the run was in progress
    assert lease.is_held("application", "app-4") is False


def test_autonomous_wrapper_uses_the_task_lease_namespace(fake_redis, monkeypatch):
    monkeypatch.setattr(
        "automation.agents.autonomous.runner.execute_task_sync", lambda t: None
    )
    result = runtime.run_autonomous_task("t-1")
    assert result == {"status": "done", "task_id": "t-1"}
    assert lease.is_held("autonomous_task", "t-1") is False


# ---------------------------------------------------------------------------
# Phase 2f — cross-worker copilot approve: `run_application` keeps the lease
# held past `_run_application` returning, polling for the `approve` signal,
# whenever the run left a browser open for review.
# ---------------------------------------------------------------------------

def test_watch_for_copilot_approval_is_a_noop_without_an_open_session(fake_redis, monkeypatch):
    """The overwhelmingly common case: the run wasn't `copilot_review` at all
    (or was, but nothing was left open), so the lease releases immediately —
    unchanged from before 2f."""
    monkeypatch.setattr(
        "automation.applications.application_flow_manager.list_open_review_sessions", lambda: []
    )
    monkeypatch.setattr("app.api.applications._run_application", lambda *a, **k: None)

    result = runtime.run_application("app-5", "doc-5")

    assert result == {"status": "done", "application_id": "app-5"}
    assert lease.is_held("application", "app-5") is False


def test_watch_for_copilot_approval_applies_the_approval_once_signalled(fake_redis, monkeypatch):
    """A signal arriving mid-poll is consumed exactly once and drives the
    exact same `apply_copilot_approval` write the API's own fast path uses."""
    monkeypatch.setattr(runtime, "COPILOT_APPROVAL_POLL_SECONDS", 0.01)
    open_sessions = ["app-6"]
    monkeypatch.setattr(
        "automation.applications.application_flow_manager.list_open_review_sessions",
        lambda: list(open_sessions),
    )
    monkeypatch.setattr("app.api.applications._run_application", lambda *a, **k: None)

    signalled = {"n": 0}

    def fake_consume(kind, rid, signal):
        signalled["n"] += 1
        if signalled["n"] >= 2:
            open_sessions.clear()  # the approval "closes" the review session
            return True
        return False

    monkeypatch.setattr(control, "consume_signal", fake_consume)
    applied = []
    monkeypatch.setattr(
        "app.api.applications.apply_copilot_approval",
        lambda db, aid: applied.append(aid),
    )

    result = runtime.run_application("app-6", "doc-6")

    assert applied == ["app-6"]
    assert result == {"status": "done", "application_id": "app-6"}
    assert lease.is_held("application", "app-6") is False


def test_watch_for_copilot_approval_gives_up_after_the_max_wait(fake_redis, monkeypatch):
    """Nobody approves in time: the watcher stops polling and lets the lease
    release, exactly like a process that died mid-review — the next orphan
    sweep is what moves the row to needs_review from there."""
    monkeypatch.setattr(runtime, "COPILOT_APPROVAL_POLL_SECONDS", 0.01)
    monkeypatch.setattr(runtime, "COPILOT_APPROVAL_MAX_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(
        "automation.applications.application_flow_manager.list_open_review_sessions",
        lambda: ["app-7"],
    )
    monkeypatch.setattr("app.api.applications._run_application", lambda *a, **k: None)
    monkeypatch.setattr(control, "consume_signal", lambda *a, **k: False)

    result = runtime.run_application("app-7", "doc-7")

    assert result == {"status": "done", "application_id": "app-7"}
    assert lease.is_held("application", "app-7") is False


def test_watch_for_copilot_approval_is_a_noop_without_redis(monkeypatch):
    """No Redis -> no cross-process channel at all; the same-process fast
    path in `POST /applications/{id}/approve` handles it directly instead."""
    monkeypatch.setattr("app.core.redis_client.sync_redis", lambda: None)
    checked = []
    monkeypatch.setattr(
        "automation.applications.application_flow_manager.list_open_review_sessions",
        lambda: checked.append(1) or ["app-8"],
    )
    monkeypatch.setattr("app.api.applications._run_application", lambda *a, **k: None)

    runtime.run_application("app-8", "doc-8")

    # Bailed out before ever asking whether app-8 has an open session.
    assert checked == []
