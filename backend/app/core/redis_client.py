"""
Redis connections (Phase 1 — PLATFORM_ROADMAP.md).

`REDIS_URL` is a managed-Redis connection string, the same way `DATABASE_URL`
is a managed-Postgres (Neon) one. Use a `rediss://` URL for a TLS endpoint
(Upstash, Redis Cloud, etc.) — `from_url` enables TLS automatically from the
scheme. See `.env.example` for provider notes.

Two clients, one URL:

- `sync_redis()`   — `redis.Redis`, thread-safe pooled. Used from the automation
  worker THREADS (and, Phase 2, the worker process) to publish events. Those
  callers have no event loop, so an async client is not an option there. It
  carries a short `socket_timeout` so a hung Redis can never stall an
  in-flight automation run — a dropped notification is always acceptable.
- `async_redis()`  — `redis.asyncio.Redis`. Used on the asyncio side: the HTTP
  middleware rate limiter and the event-bus pub/sub listener. NO `socket_timeout`
  here: the pub/sub listener holds an idle connection open for long stretches
  between events, and a read timeout would tear it down on a loop. The rate
  limiter instead bounds its own call with `asyncio.wait_for` (see middleware).

Both are lazily created singletons and both return `None` when `REDIS_URL` is
unset — every caller must handle that and fall back to in-process behaviour
(see `middleware.py` and `event_bus.py`).

`decode_responses=True`: every value this app puts in Redis is UTF-8 JSON or a
short ASCII token, never binary.
"""

from __future__ import annotations

import logging

from app.core.config import REDIS_URL

logger = logging.getLogger(__name__)

# Generous enough for a TLS handshake to a managed Redis in another region.
_CONNECT_TIMEOUT = 5.0
# Pings an idle pooled connection this often so a silently-dropped cloud
# connection is discovered and replaced before the next real command.
_HEALTH_CHECK_INTERVAL = 30

_sync_client = None
_async_client = None
_warned_unconfigured = False


def _warn_once() -> None:
    global _warned_unconfigured
    if not _warned_unconfigured:
        logger.debug("REDIS_URL is not set — Redis-backed features fall back to in-process.")
        _warned_unconfigured = True


def sync_redis():
    """Pooled synchronous client, or None if REDIS_URL is unset."""
    global _sync_client
    if REDIS_URL is None:
        _warn_once()
        return None
    if _sync_client is None:
        import redis  # local import: optional dependency path

        _sync_client = redis.Redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=_CONNECT_TIMEOUT,
            socket_timeout=3,            # publish must never block a live run
            retry_on_timeout=True,
            health_check_interval=_HEALTH_CHECK_INTERVAL,
        )
    return _sync_client


def async_redis():
    """Pooled asyncio client, or None if REDIS_URL is unset."""
    global _async_client
    if REDIS_URL is None:
        _warn_once()
        return None
    if _async_client is None:
        import redis.asyncio as aioredis

        _async_client = aioredis.Redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=_CONNECT_TIMEOUT,
            # No socket_timeout: the pub/sub listener idles between events.
            retry_on_timeout=True,
            health_check_interval=_HEALTH_CHECK_INTERVAL,
        )
    return _async_client


def reset_clients_for_tests() -> None:
    """Drop the cached singletons so a test can inject a fake. Test-only."""
    global _sync_client, _async_client
    _sync_client = None
    _async_client = None
