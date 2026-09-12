"""
Phase 1 — Redis-backed rate limiter.

Covers the shared sliding window, proxy-aware client identity, and the
fail-open contract (a Redis outage must not 429 or 500 real traffic).
"""

from types import SimpleNamespace

import pytest

from app.core import middleware
from tests._asyncio_helpers import run_async


@pytest.fixture
def fake_async_redis(monkeypatch):
    """A fresh fakeredis async client per test, created inside the test's own
    loop (see `run_async`) so it never inherits a closed loop from an earlier
    test."""
    def factory():
        import fakeredis.aioredis

        client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        monkeypatch.setattr(middleware, "async_redis", lambda: client)
        return client

    return factory


def _request(ip="1.2.3.4", forwarded=None):
    headers = {}
    if forwarded is not None:
        headers["x-forwarded-for"] = forwarded
    return SimpleNamespace(
        client=SimpleNamespace(host=ip),
        headers=headers,
        url=SimpleNamespace(path="/resumes"),
        method="GET",
    )


# ---------- shared window ----------

def test_window_allows_up_to_the_limit_then_blocks(fake_async_redis, monkeypatch):
    monkeypatch.setattr(middleware, "RATE_LIMIT_PER_MINUTE", 3)

    async def scenario():
        fake_async_redis()
        return [await middleware._is_rate_limited("ipA") for _ in range(4)]

    assert run_async(scenario) == [False, False, False, True]


def test_two_ips_have_independent_buckets(fake_async_redis, monkeypatch):
    monkeypatch.setattr(middleware, "RATE_LIMIT_PER_MINUTE", 2)

    async def scenario():
        fake_async_redis()
        a = [await middleware._is_rate_limited("ipA") for _ in range(3)]
        b = [await middleware._is_rate_limited("ipB") for _ in range(3)]
        return a, b

    a, b = run_async(scenario)
    assert a == [False, False, True]
    assert b == [False, False, True]


def test_old_entries_fall_out_of_the_window(fake_async_redis, monkeypatch):
    monkeypatch.setattr(middleware, "RATE_LIMIT_PER_MINUTE", 1)
    real_time = middleware.time.time

    async def scenario():
        fake_async_redis()
        monkeypatch.setattr(middleware.time, "time", lambda: 1_000.0)
        first = await middleware._is_rate_limited("ipC")
        monkeypatch.setattr(middleware.time, "time", lambda: 1_002.0)
        second = await middleware._is_rate_limited("ipC")
        monkeypatch.setattr(middleware.time, "time", lambda: 1_064.0)
        third = await middleware._is_rate_limited("ipC")
        monkeypatch.setattr(middleware.time, "time", real_time)
        return first, second, third

    assert run_async(scenario) == (False, True, False)


# ---------- fail open ----------

def test_redis_error_fails_open(monkeypatch):
    class Boom:
        def pipeline(self, *a, **k):
            raise ConnectionError("redis down")

    monkeypatch.setattr(middleware, "async_redis", lambda: Boom())
    assert run_async(lambda: middleware._is_rate_limited("ipX")) is False


def test_no_redis_uses_in_process_limiter(monkeypatch):
    monkeypatch.setattr(middleware, "async_redis", lambda: None)
    monkeypatch.setattr(middleware, "RATE_LIMIT_PER_MINUTE", 2)
    middleware._hits.clear()

    async def scenario():
        return [await middleware._is_rate_limited("ipL") for _ in range(3)]

    assert run_async(scenario) == [False, False, True]


# ---------- client identity ----------

def test_client_ip_ignores_forwarded_header_by_default(monkeypatch):
    monkeypatch.setattr(middleware, "TRUST_PROXY", False)
    assert middleware._client_ip(_request(ip="10.0.0.1", forwarded="9.9.9.9")) == "10.0.0.1"


def test_client_ip_uses_first_forwarded_hop_when_proxy_trusted(monkeypatch):
    monkeypatch.setattr(middleware, "TRUST_PROXY", True)
    req = _request(ip="10.0.0.1", forwarded="9.9.9.9, 10.0.0.1")
    assert middleware._client_ip(req) == "9.9.9.9"


def test_client_ip_falls_back_when_no_peer(monkeypatch):
    monkeypatch.setattr(middleware, "TRUST_PROXY", False)
    req = _request(forwarded=None)
    req.client = None
    assert middleware._client_ip(req) == "unknown"
