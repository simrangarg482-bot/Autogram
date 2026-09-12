"""
Phase 2b — cross-process control signalling (`automation/coordination/control.py`)
and its wiring into `runner.py`'s signal functions.

The invariant: with no Redis, everything is a no-op and `runner.py` behaves
exactly as it did before 2b (signal the local handle, or return False). With
Redis and a lease held elsewhere, the signal is handed to the owner instead of
being dropped.
"""

import fakeredis
import pytest

from app.core import redis_client
from automation.agents.autonomous import runner
from automation.coordination import control, lease


@pytest.fixture
def fake_redis(monkeypatch):
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "sync_redis", lambda: client)
    return client


@pytest.fixture
def no_redis(monkeypatch):
    monkeypatch.setattr(redis_client, "sync_redis", lambda: None)


@pytest.fixture(autouse=True)
def _clear_registry():
    runner._REGISTRY.clear()
    yield
    runner._REGISTRY.clear()


# ---------- flags ----------

def test_signal_round_trip_and_clear_on_read(fake_redis):
    assert control.send_signal("autonomous_task", "t1", "resume") is True
    assert control.consume_signal("autonomous_task", "t1", "resume") is True
    # consumed — gone now
    assert control.consume_signal("autonomous_task", "t1", "resume") is False


def test_signals_are_independent(fake_redis):
    control.send_signal("autonomous_task", "t1", "cancel")
    assert control.consume_signal("autonomous_task", "t1", "resume") is False
    assert control.consume_signal("autonomous_task", "t1", "cancel") is True


def test_clear_all_drops_pending_flags(fake_redis):
    control.send_signal("application", "a1", "approve")
    control.clear_all("application", "a1")
    assert control.consume_signal("application", "a1", "approve") is False


def test_invalid_kind_or_signal_rejected(fake_redis):
    with pytest.raises(ValueError, match="control kind"):
        control.send_signal("resume_thing", "x", "resume")
    with pytest.raises(ValueError, match="control signal"):
        control.send_signal("autonomous_task", "x", "explode")


# ---------- secret mailbox ----------

def test_secret_deposited_and_collected_exactly_once(fake_redis):
    assert control.deposit_secret("t1", "req-9", "123456") is True
    got = control.collect_secret("t1")
    assert got == {"request_id": "req-9", "value": "123456"}
    assert control.collect_secret("t1") is None  # one-shot


def test_secret_value_is_not_in_the_redis_key_name(fake_redis):
    control.deposit_secret("t1", "req-9", "123456")
    assert not any("123456" in k for k in fake_redis.keys("*"))


# ---------- degrade path ----------

def test_everything_is_a_noop_without_redis(no_redis):
    assert control.send_signal("autonomous_task", "t1", "resume") is False
    assert control.consume_signal("autonomous_task", "t1", "resume") is False
    assert control.deposit_secret("t1", "r", "code") is False
    assert control.collect_secret("t1") is None
    control.clear_all("autonomous_task", "t1")  # must not raise


# ---------- runner.py wiring ----------

def test_signal_resume_hands_off_to_lease_holder(fake_redis, monkeypatch):
    monkeypatch.setattr(lease, "is_held", lambda kind, rid: True)
    assert runner.signal_resume("t1") is True
    assert control.consume_signal("autonomous_task", "t1", "resume") is True


def test_signal_resume_returns_false_when_nobody_owns_the_run(fake_redis, monkeypatch):
    monkeypatch.setattr(lease, "is_held", lambda kind, rid: False)
    assert runner.signal_resume("t1") is False


def test_request_cancel_hands_off_to_lease_holder(fake_redis, monkeypatch):
    monkeypatch.setattr(lease, "is_held", lambda kind, rid: True)
    assert runner.request_cancel("t1") is True
    assert control.consume_signal("autonomous_task", "t1", "cancel") is True


def test_deliver_secret_hands_off_to_lease_holder(fake_redis, monkeypatch):
    monkeypatch.setattr(lease, "is_held", lambda kind, rid: True)
    assert runner.deliver_secret("t1", "req-1", "999888") is True
    assert control.collect_secret("t1") == {"request_id": "req-1", "value": "999888"}


def test_deliver_secret_returns_false_when_unowned(fake_redis, monkeypatch):
    monkeypatch.setattr(lease, "is_held", lambda kind, rid: False)
    assert runner.deliver_secret("t1", "req-1", "999888") is False


def test_local_handle_still_wins_over_redis(fake_redis, monkeypatch):
    """A live in-process handle is signalled directly — the Redis path is only
    for runs this process doesn't hold."""
    import threading

    handle = runner._get_or_create_handle("t1")
    monkeypatch.setattr(lease, "is_held", lambda kind, rid: True)
    assert isinstance(handle.resume_event, threading.Event)

    assert runner.signal_resume("t1") is True
    assert handle.resume_event.is_set()
    # nothing was pushed to Redis
    assert control.consume_signal("autonomous_task", "t1", "resume") is False
