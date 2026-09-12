"""
Run-ownership leases (Phase 2a — PLATFORM_ROADMAP.md).

A *lease* is "worker W is actively driving run R right now", stored as a Redis
key with a short TTL that W refreshes on a heartbeat while it holds the run.
This is the liveness signal the old design didn't have: `automation_ownership`
and the reconcilers read a *status column* with no way to tell a status that
outlived its process from one that's genuinely running.

- `acquire()` uses `SET NX PX` — atomic, so two workers can never both hold one
  run's lease. This is the load-bearing guarantee.
- `renew()` / `release()` are holder-checked but not atomic; the worst a race
  there can do is briefly extend or drop a lease a moment early, which the next
  heartbeat or the TTL corrects. They are never the thing standing between two
  workers and a double-apply — `acquire`'s NX is.
- Every function DEGRADES TO A NO-OP when `REDIS_URL` is unset: `acquire`
  returns True (you always "own" it in a single process), `is_held` returns
  False, `renew`/`release` do nothing. So the in-process path is unchanged.

Nothing in the codebase calls this yet — 2c wires the worker to it and 2d
switches reconciliation onto it.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Lease lifetime. Long enough to survive a slow heartbeat or a brief Redis
#: blip; short enough that a dead worker's runs become reclaimable quickly.
DEFAULT_TTL_SECONDS = 45
#: How often the owning worker should call `renew()` — comfortably inside the TTL.
HEARTBEAT_SECONDS = 15

_VALID_KINDS = ("application", "autonomous_task")


def _key(kind: str, run_id: str) -> str:
    if kind not in _VALID_KINDS:
        raise ValueError(f"{kind!r} is not a lease kind; expected one of {_VALID_KINDS}")
    return f"autogram:lease:{kind}:{run_id}"


def _redis():
    from app.core.redis_client import sync_redis

    return sync_redis()


def acquire(kind: str, run_id: str, worker_id: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> bool:
    """Claim the run for `worker_id`. Returns True if this worker now holds the
    lease (either it just took a free one, or it already held it and this
    refreshed it), False if a *different* worker holds it."""
    r = _redis()
    if r is None:
        return True  # single process — ownership is implicit
    key = _key(kind, run_id)
    if r.set(key, worker_id, nx=True, px=ttl_seconds * 1000):
        return True
    # Someone holds it — is it us? (Re-enqueue / retry of our own run.)
    if r.get(key) == worker_id:
        r.pexpire(key, ttl_seconds * 1000)
        return True
    return False


def renew(kind: str, run_id: str, worker_id: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> bool:
    """Heartbeat: extend the lease iff this worker still holds it. Returns False
    if the lease was lost (expired and taken by another worker, or released) —
    the caller should treat that as "stop, something else owns this now"."""
    r = _redis()
    if r is None:
        return True
    key = _key(kind, run_id)
    if r.get(key) == worker_id:
        r.pexpire(key, ttl_seconds * 1000)
        return True
    return False


def release(kind: str, run_id: str, worker_id: str) -> None:
    """Drop the lease on a clean exit, so the run is immediately reclaimable
    rather than waiting out the TTL. Only deletes if this worker still holds it."""
    r = _redis()
    if r is None:
        return
    key = _key(kind, run_id)
    try:
        if r.get(key) == worker_id:
            r.delete(key)
    except Exception:  # noqa: BLE001 - releasing is best-effort; the TTL is the backstop
        logger.warning("Lease release failed for %s (TTL will expire it).", key, exc_info=True)


def current_holder(kind: str, run_id: str) -> str | None:
    """The worker_id currently holding the lease, or None. `None` when Redis is
    unset too — callers use `is_held` semantics, not "who"."""
    r = _redis()
    if r is None:
        return None
    return r.get(_key(kind, run_id))


def is_held(kind: str, run_id: str) -> bool:
    """True iff some worker currently holds a live lease for this run.
    Reconciliation (2d): a run in an active status with `is_held() == False`
    is orphaned."""
    return current_holder(kind, run_id) is not None
