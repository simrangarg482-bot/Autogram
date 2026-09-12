"""
Cross-process control signalling for a running automation (Phase 2b).

When the API replica that receives `POST /agent/tasks/{id}/resume` (or
`/cancel`, or an OTP) is NOT the process running that task, it can't reach the
task's in-process `TaskHandle`. This module is the bridge: the API deposits the
signal in Redis, and the process that owns the run consumes it on its next loop
iteration.

Two shapes:

- **Flags** (`resume`, `cancel`, `approve`) — a per-run Redis hash. The owner
  polls `consume_signal()` each iteration; `HGET` + `HDEL` is not atomic but a
  double-seen resume/cancel is idempotent, so that's fine. Flags carry no
  payload and are safe to keep around (they TTL out on their own).
- **Secret mailbox** (`deposit_secret` / `collect_secret`) — a one-shot Redis
  list for an OTP/MFA code. `LPUSH` then the owner `LPOP`s it exactly once; a
  short `EXPIRE` is the backstop if the owner never reads it.

  SECURITY NOTE. This is the one place a verification code leaves this
  process's memory: it transits Redis for up to `_SECRET_TTL_SECONDS`. That is
  acceptable for a self-hosted Redis and a deliberate, bounded tradeoff for a
  managed one (Upstash etc.) — the alternative is that HITL OTP simply doesn't
  work across replicas. The value is `LPOP`ped (removed) on read, never logged,
  never written anywhere else, and the mailbox key has no other reader.

Everything here DEGRADES TO A NO-OP when `REDIS_URL` is unset: `send_*` do
nothing, `consume_signal` returns False, `collect_secret` returns None. So the
single-process path (signal the local `TaskHandle` directly) is unchanged.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_VALID_KINDS = ("application", "autonomous_task")
_VALID_SIGNALS = ("resume", "cancel", "approve")

#: Flags live a while — a human deciding to cancel/resume can take minutes.
_FLAG_TTL_SECONDS = 1800
#: A code is worthless after a couple of minutes on the website's side anyway.
_SECRET_TTL_SECONDS = 120


def _redis():
    from app.core.redis_client import sync_redis

    return sync_redis()


def _flag_key(kind: str, run_id: str) -> str:
    if kind not in _VALID_KINDS:
        raise ValueError(f"{kind!r} is not a control kind; expected one of {_VALID_KINDS}")
    return f"autogram:ctl:{kind}:{run_id}"


def _secret_key(task_id: str) -> str:
    return f"autogram:secret:{task_id}"


# ---------- flags ----------

def send_signal(kind: str, run_id: str, signal: str) -> bool:
    """Deposit a `resume` / `cancel` / `approve` flag for the process that owns
    this run. Returns True if it was written (Redis present), False if it was a
    no-op (no Redis — caller should handle the signal locally instead)."""
    if signal not in _VALID_SIGNALS:
        raise ValueError(f"{signal!r} is not a control signal; expected one of {_VALID_SIGNALS}")
    r = _redis()
    if r is None:
        return False
    key = _flag_key(kind, run_id)
    try:
        r.hset(key, signal, "1")
        r.expire(key, _FLAG_TTL_SECONDS)
        return True
    except Exception:  # noqa: BLE001 - signalling is best-effort
        logger.warning("control.send_signal(%s, %s) failed.", key, signal, exc_info=True)
        return False


def consume_signal(kind: str, run_id: str, signal: str) -> bool:
    """Owner side: has `signal` been raised for this run since we last checked?
    Clears it on read. Returns False when Redis is unset (nothing to consume)."""
    r = _redis()
    if r is None:
        return False
    key = _flag_key(kind, run_id)
    try:
        if r.hget(key, signal) is None:
            return False
        r.hdel(key, signal)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("control.consume_signal(%s, %s) failed.", key, signal, exc_info=True)
        return False


def clear_all(kind: str, run_id: str) -> None:
    """Drop every flag for a run — called when the run reaches a terminal
    state, so a late resume/cancel can't linger for a reused id."""
    r = _redis()
    if r is None:
        return
    try:
        r.delete(_flag_key(kind, run_id))
    except Exception:  # noqa: BLE001
        logger.debug("control.clear_all(%s) failed.", run_id, exc_info=True)


# ---------- secret mailbox ----------

def deposit_secret(task_id: str, request_id: str, value: str) -> bool:
    """Hand a transient OTP/MFA code to the process that owns `task_id`.
    Returns False (delivered nothing) when Redis is unset — the caller must
    then fall back to `runner.deliver_secret`'s local path or surface a
    LOGIN_REQUIRED, exactly as before. The value is never logged here."""
    r = _redis()
    if r is None:
        return False
    key = _secret_key(task_id)
    try:
        r.lpush(key, json.dumps({"request_id": request_id, "value": value}))
        r.expire(key, _SECRET_TTL_SECONDS)
        return True
    except Exception:  # noqa: BLE001 - never let a delivery failure surface the value in a traceback
        logger.warning("control.deposit_secret failed for task %s.", task_id)
        return False


def collect_secret(task_id: str) -> dict | None:
    """Owner side: pull the pending code for this task, exactly once. Returns
    `{"request_id": ..., "value": ...}` or None. The value is removed from
    Redis by this call."""
    r = _redis()
    if r is None:
        return None
    try:
        raw = r.lpop(_secret_key(task_id))
    except Exception:  # noqa: BLE001
        logger.warning("control.collect_secret failed for task %s.", task_id)
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        return {"request_id": payload["request_id"], "value": payload["value"]}
    except (ValueError, KeyError, TypeError):
        logger.warning("control.collect_secret: undecodable mailbox entry for task %s dropped.", task_id)
        return None
