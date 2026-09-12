"""
Phase 2d — lease-aware orphan reconciliation.

The behaviour change: a run in an active status is only recovered if NO worker
holds its lease. Without Redis `lease.is_held` is always False, so this reduces
to the pre-Phase-2 "recover every active row" startup behaviour.
"""

from unittest.mock import MagicMock

from app.services import automation_recovery
from app.core import scheduler


def _db_returning(rows):
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = rows
    return db


def test_a_run_with_a_held_lease_is_not_recovered(monkeypatch):
    rows = [
        ("a1", "u1", "https://x/1", "processing"),  # lease held -> live, skip
        ("a2", "u1", "https://x/2", "processing"),  # no lease -> orphaned
    ]
    monkeypatch.setattr(automation_recovery.lease, "is_held", lambda kind, rid: rid == "a1")
    recovered_ids = []
    monkeypatch.setattr(
        automation_recovery, "_recover_one",
        lambda db, app_id, *a: (recovered_ids.append(app_id), 1)[1],
    )

    count = automation_recovery.reconcile_orphaned_applications_on_startup(_db_returning(rows))

    assert count == 1
    assert recovered_ids == ["a2"]


def test_without_redis_every_active_row_is_recovered(monkeypatch):
    rows = [("a1", "u1", "u", "processing"), ("a2", "u1", "u", "copilot_review")]
    monkeypatch.setattr(automation_recovery.lease, "is_held", lambda kind, rid: False)
    monkeypatch.setattr(automation_recovery, "_recover_one", lambda *a: 1)

    assert automation_recovery.reconcile_orphaned_applications_on_startup(_db_returning(rows)) == 2


# ---------- the periodic sweep ----------

def test_sweep_only_scheduled_when_redis_is_set(monkeypatch):
    added = []
    fake_sched = MagicMock()
    fake_sched.add_job.side_effect = lambda *a, **k: added.append(k.get("id"))
    monkeypatch.setattr(scheduler, "BackgroundScheduler", lambda **k: fake_sched)
    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setattr(scheduler, "JOB_SYNC_QUERIES", [])

    monkeypatch.setattr(scheduler, "REDIS_URL", "redis://x")
    scheduler.start_scheduler()
    assert "orphan_sweep" in added

    added.clear()
    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setattr(scheduler, "REDIS_URL", None)
    scheduler.start_scheduler()
    assert "orphan_sweep" not in added


def test_sweep_calls_the_combined_reconciler(monkeypatch):
    called = {}
    monkeypatch.setattr(
        "app.services.automation_recovery.reconcile_orphaned_automation_on_startup",
        lambda db: called.setdefault("hit", True) or {"autonomous_tasks": 0, "applications": 0},
    )
    monkeypatch.setattr(scheduler, "SessionLocal", MagicMock())
    scheduler._sweep_orphaned_automation()
    assert called.get("hit") is True
