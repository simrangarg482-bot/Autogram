"""
Production middleware: request logging, rate limiting, global error handling.

Rate limiter — sliding window per client IP, `RATE_LIMIT_PER_MINUTE` requests
per 60s:

- **With `REDIS_URL` set** (Phase 1): a Redis sorted-set window shared across
  every API replica, so the limit is a true global, not per-process. If Redis
  is unreachable the limiter FAILS OPEN (logged) — a rate-limiter outage must
  never take the API down.
- **Without Redis**: an in-process window. Correct for a single process only;
  `app/main.py` warns at startup when running this way.

Client identity: `request.client.host` normally; the first hop of
`X-Forwarded-For` when `TRUST_PROXY=true` (the app is behind a trusted proxy
that terminates the client connection — otherwise every caller looks like the
proxy and shares one bucket).
"""

import asyncio
import logging
import time
import uuid
from collections import defaultdict, deque

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.config import RATE_LIMIT_PER_MINUTE, TRUST_PROXY
from app.core.database import is_disconnect_error
from app.core.redis_client import async_redis

logger = logging.getLogger("app.request")

_WINDOW_SECONDS = 60
_EXEMPT_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}

# ip -> deque of request timestamps within the current window (in-process fallback)
_hits: dict[str, deque] = defaultdict(deque)
_MAX_TRACKED_IPS = 10_000  # prune threshold so the map can't grow unbounded


def _client_ip(request: Request) -> str:
    if TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # First hop is the original client; the rest are proxies.
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------- in-process fallback ----------

def _prune_idle_ips(now: float) -> None:
    if len(_hits) <= _MAX_TRACKED_IPS:
        return
    for ip in [ip for ip, window in _hits.items()
               if not window or now - window[-1] > _WINDOW_SECONDS]:
        del _hits[ip]


def _is_rate_limited_local(client_ip: str) -> bool:
    now = time.monotonic()
    _prune_idle_ips(now)
    window = _hits[client_ip]
    while window and now - window[0] > _WINDOW_SECONDS:
        window.popleft()
    if len(window) >= RATE_LIMIT_PER_MINUTE:
        return True
    window.append(now)
    return False


# ---------- Redis-backed shared window ----------

async def _is_rate_limited_redis(redis, client_ip: str) -> bool:
    """Sorted-set sliding window. Not wrapped in MULTI: a rate limiter that
    occasionally lets one extra request through under a race is fine, and the
    pipeline still saves the round trips. Any Redis error propagates to the
    caller, which fails open."""
    now = time.time()
    key = f"rl:{client_ip}"
    cutoff = now - _WINDOW_SECONDS
    async with redis.pipeline(transaction=False) as pipe:
        pipe.zremrangebyscore(key, 0, cutoff)
        pipe.zadd(key, {f"{now}:{uuid.uuid4().hex}": now})
        pipe.zcard(key)
        pipe.expire(key, _WINDOW_SECONDS * 2)
        _, _, count, _ = await pipe.execute()
    return count > RATE_LIMIT_PER_MINUTE


# A slow managed Redis must not add latency to every request. The async client
# carries no socket_timeout (the event-bus listener needs it that way), so the
# rate limiter bounds its own call here and fails open when it's exceeded.
_RATE_LIMIT_REDIS_TIMEOUT = 2.0


async def _is_rate_limited(client_ip: str) -> bool:
    redis = async_redis()
    if redis is None:
        return _is_rate_limited_local(client_ip)
    try:
        return await asyncio.wait_for(
            _is_rate_limited_redis(redis, client_ip), timeout=_RATE_LIMIT_REDIS_TIMEOUT
        )
    except Exception:  # noqa: BLE001 - fail open on any Redis problem (incl. timeout)
        logger.warning("Rate limiter: Redis slow/unavailable, allowing the request.", exc_info=True)
        return False


def register_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_pipeline(request: Request, call_next):
        # --- rate limiting ---
        if request.url.path not in _EXEMPT_PATHS:
            if await _is_rate_limited(_client_ip(request)):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded. Try again shortly."},
                )

        # --- timing + logging + last-resort error handling ---
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as e:
            # Nothing unhandled ever leaks a stack trace to the client.
            # A dropped Neon connection isn't a bug in the request — it's a
            # transient upstream outage, so it gets 503 (retryable) rather than
            # 500, which would tell the caller their request was malformed.
            if is_disconnect_error(e):
                logger.warning(
                    "Database connection lost on %s %s", request.method, request.url.path,
                )
                return JSONResponse(
                    status_code=503,
                    content={"detail": "Database temporarily unavailable. Please retry."},
                    headers={"Retry-After": "2"},
                )
            logger.exception("Unhandled error on %s %s", request.method, request.url.path)
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal server error."},
            )

        duration_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "%s %s -> %d (%.0fms)",
            request.method, request.url.path, response.status_code, duration_ms,
        )
        return response
