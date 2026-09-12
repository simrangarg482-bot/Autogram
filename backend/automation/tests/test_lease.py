"""
Phase 2a — run-ownership leases (`automation/coordination/lease.py`).

The one guarantee that matters: two workers can never both hold one run's
lease. Plus the no-Redis degrade path (single process = implicit ownership).
"""

import time

import fakeredis
import pytest

from app.core import redis_client
from automation.coordination import lease


@pytest.fixture
def fake_redis(monkeypatch):
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "sync_redis", lambda: client)
    return client


@pytest.fixture
def no_redis(monkeypatch):
    monkeypatch.setattr(redis_client, "sync_redis", lambda: None)


# ---------- the core guarantee ----------

def test_only_one_worker_holds_a_lease(fake_redis):
    assert lease.acquire("application", "app1", "worker-A") is True
    assert lease.acquire("application", "app1", "worker-B") is False
    assert lease.current_holder("application", "app1") == "worker-A"


def test_same_worker_reacquiring_is_a_refresh(fake_redis):
    assert lease.acquire("application", "app1", "worker-A") is True
    assert lease.acquire("application", "app1", "worker-A") is True


def test_release_frees_the_lease_for_another_worker(fake_redis):
    lease.acquire("application", "app1", "worker-A")
    lease.release("application", "app1", "worker-A")
    assert lease.is_held("application", "app1") is False
    assert lease.acquire("application", "app1", "worker-B") is True


def test_a_non_holder_cannot_release(fake_redis):
    lease.acquire("application", "app1", "worker-A")
    lease.release("application", "app1", "worker-B")  # not the holder — ignored
    assert lease.current_holder("application", "app1") == "worker-A"


# ---------- heartbeat ----------

def test_renew_extends_only_while_held(fake_redis):
    lease.acquire("application", "app1", "worker-A", ttl_seconds=1)
    assert lease.renew("application", "app1", "worker-A", ttl_seconds=45) is True
    # Another worker can't renew a lease it doesn't hold.
    assert lease.renew("application", "app1", "worker-B") is False


def test_lease_expires_when_not_renewed(fake_redis):
    lease.acquire("autonomous_task", "t1", "worker-A", ttl_seconds=1)
    assert lease.is_held("autonomous_task", "t1") is True
    time.sleep(1.15)
    assert lease.is_held("autonomous_task", "t1") is False
    assert lease.acquire("autonomous_task", "t1", "worker-B") is True


# ---------- degrade path ----------

def test_without_redis_ownership_is_implicit(no_redis):
    assert lease.acquire("application", "app1", "worker-A") is True
    assert lease.renew("application", "app1", "worker-A") is True
    assert lease.is_held("application", "app1") is False  # nothing to check against
    lease.release("application", "app1", "worker-A")  # no-op, must not raise


# ---------- validation ----------

def test_unknown_lease_kind_is_rejected(fake_redis):
    with pytest.raises(ValueError, match="lease kind"):
        lease.acquire("resume", "r1", "worker-A")
