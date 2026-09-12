"""
Phase 1 — cross-process event delivery via Redis.

Two `EventBus` instances sharing one (fake) Redis server stand in for two API
replicas: an event published on bus A must reach a WebSocket subscriber on
bus B, exactly once, without bus A double-delivering to its own subscribers.
"""

import asyncio

import fakeredis
import pytest

from app.core import redis_client
from app.services.event_bus import EventBus, WorkflowEvent
from tests._asyncio_helpers import run_async


@pytest.fixture
def shared_redis(monkeypatch):
    """Returns a callable that wires sync + async fakeredis (sharing one
    server) and must be invoked INSIDE the test's event loop so the async
    client binds to it."""
    server = fakeredis.FakeServer()

    def wire():
        import fakeredis.aioredis

        sync = fakeredis.FakeStrictRedis(server=server, decode_responses=True)
        aio = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
        monkeypatch.setattr(redis_client, "sync_redis", lambda: sync)
        monkeypatch.setattr(redis_client, "async_redis", lambda: aio)
        return sync, aio

    return wire


async def _drain(bus: EventBus):
    task = bus._redis_listener_task
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def test_event_crosses_from_one_bus_to_another(shared_redis):
    async def scenario():
        shared_redis()
        replica_b = EventBus()
        replica_b._origin = "replica-B"
        queue = replica_b.subscribe("application:1")  # starts B's listener
        await asyncio.sleep(0.15)

        replica_a = EventBus()
        replica_a._origin = "replica-A"
        replica_a.publish(WorkflowEvent("application:1", "APPLICATION_SUBMITTED", {"ok": True}))

        event = await asyncio.wait_for(queue.get(), timeout=3)
        await _drain(replica_b)
        return event

    event = run_async(scenario)
    assert event.event_type == "APPLICATION_SUBMITTED"
    assert event.payload == {"ok": True}


def test_publisher_does_not_double_deliver_to_its_own_subscribers(shared_redis):
    async def scenario():
        shared_redis()
        bus = EventBus()
        bus._origin = "solo"
        queue = bus.subscribe("application:9")
        await asyncio.sleep(0.15)
        bus.publish(WorkflowEvent("application:9", "FIELD_FILLED"))
        await asyncio.sleep(0.25)
        await _drain(bus)
        return queue.qsize()

    assert run_async(scenario) == 1


def test_events_stay_scoped_to_their_stream_across_the_bridge(shared_redis):
    async def scenario():
        shared_redis()
        b = EventBus()
        b._origin = "B"
        mine = b.subscribe("application:1")
        theirs = b.subscribe("application:2")
        await asyncio.sleep(0.15)

        a = EventBus()
        a._origin = "A"
        a.publish(WorkflowEvent("application:1", "FIELD_FILLED"))

        got = await asyncio.wait_for(mine.get(), timeout=3)
        await asyncio.sleep(0.1)
        empty = theirs.empty()
        await _drain(b)
        return got.event_type, empty

    event_type, other_empty = run_async(scenario)
    assert event_type == "FIELD_FILLED"
    assert other_empty


def test_no_redis_configured_is_still_in_process_only(monkeypatch):
    monkeypatch.setattr(redis_client, "sync_redis", lambda: None)
    monkeypatch.setattr(redis_client, "async_redis", lambda: None)

    async def scenario():
        bus = EventBus()
        queue = bus.subscribe("application:1")
        assert bus._redis_listener_task is None
        bus.publish(WorkflowEvent("application:1", "APPLICATION_SUBMITTED"))
        return await asyncio.wait_for(queue.get(), timeout=2)

    assert run_async(scenario).event_type == "APPLICATION_SUBMITTED"
