"""
Cross-process coordination primitives (Phase 2 — PLATFORM_ROADMAP.md).

These replace the process-memory mechanisms that forced single-worker
deployment:

- `lease.py`   — "which worker owns this run right now", as a Redis key with a
  TTL that the owning worker heartbeats. Reconciliation (Phase 2d) treats a
  run whose lease is absent/expired as orphaned, instead of assuming an empty
  in-process registry means every active row was abandoned.
- `control.py` (Phase 2b) — resume / cancel / approve / secret signalling to
  the worker that owns a run, over Redis, so any API replica can signal it.

Everything here degrades to a no-op when `REDIS_URL` is unset, so the
single-process path keeps working exactly as before.
"""
