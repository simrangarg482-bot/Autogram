"""
Publish/subscribe for live workflow events.

## Transport: in-process, mirrored to Redis when configured (Phase 1)

Every `publish` fans out to this process's local subscribers immediately. When
`REDIS_URL` is set the event is ALSO published to a Redis channel, and every
API process runs one listener (`start_redis_listener`) that re-injects events
originating elsewhere into its own local subscribers. Net effect: a WebSocket
client on replica A sees events published by replica B (or, Phase 2, by the
worker process). The origin process tags each message and skips its own
loopback, so an event is delivered to any given subscriber exactly once.

With `REDIS_URL` unset the Redis path is simply inert and delivery is
in-process only — correct for a single process, which `app/main.py` warns
about at startup.

This never becomes a source of truth either way — see below.

## Delivery guarantees, stated plainly

This is a live tap, NOT a durable log:

* events published while nobody is subscribed are dropped;
* a subscriber whose queue is full drops the OLDEST event rather than blocking
  the publisher — a slow browser tab must never be able to stall the automation
  thread that is publishing;
* nothing is replayed on reconnect.

That is deliberate, and safe, because the stream is an accelerator rather than
a source of truth: `ApplicationAuditLog` holds the compliance record,
`ChatMessage` holds the conversation, and the status endpoints hold current
state. A client that misses an event and refetches sees the truth. Nothing may
ever depend on an event *arriving* — treat a delivered event only as a hint to
look at the database.

## Threading

Events are published from the automation's own worker THREADS (the deterministic
one-off Playwright thread, the autonomous daemon thread), while subscribers live
on the asyncio event loop serving the WebSocket. `publish` is therefore
thread-safe and non-async, and hands each event to the loop with
`call_soon_threadsafe`; calling an asyncio primitive directly from those threads
would be a race at best and a silent no-op at worst.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock

logger = logging.getLogger(__name__)

#: Redis pub/sub channel namespace. One channel per stream_id; the listener
#: pattern-subscribes to all of them.
_REDIS_CHANNEL_PREFIX = "autogram:events:"
_REDIS_CHANNEL_PATTERN = _REDIS_CHANNEL_PREFIX + "*"

#: Identifies this process so the Redis listener can skip events it published
#: itself (they were already delivered to local subscribers synchronously).
_PROCESS_ID = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"

#: The complete live-event vocabulary. This is a CONTRACT, not documentation:
#: the frontend switches on these names, and a typo in a publish call would
#: silently produce an event nobody handles — invisible, because publishing is
#: fire-and-forget by design.
#:
#: Defined here so there is one authority. `automation/tests/test_frontend_backend_contract.py`
#: checks the frontend against it, and `WorkflowEvent` validates against it in
#: development (see `__post_init__`).
WORKFLOW_EVENTS = frozenset({
    # Lifecycle
    "APPLICATION_STARTED",
    "PAGE_ANALYZED",
    "FIELD_FILLED",
    "SUBMISSION_STARTED",
    "APPLICATION_SUBMITTED",
    "APPLICATION_FAILED",
    "APPLICATION_READY_FOR_SUBMISSION",
    "REVIEW_REQUIRED",
    "DOCUMENT_ATTACHED",
    # Human-in-the-loop
    "HUMAN_ACTION_REQUIRED",
    "HUMAN_ACTION_COMPLETED",
    "REQUEST_HUMAN_INTERVENTION",
    # Transport-level, never a state change — the socket sends this to keep an
    # idle connection alive, and every consumer must ignore it.
    "KEEPALIVE",
})


#: Per-subscriber buffer. Small on purpose: these are UI notifications, and a
#: client that has fallen 64 events behind is better served by refetching state
#: than by replaying a backlog.
_MAX_QUEUED_EVENTS = 64


@dataclass(frozen=True)
class WorkflowEvent:
    """One thing that happened, addressed to one automation attempt.

    `event_type` is the vocabulary the frontend switches on (APPLICATION_STARTED,
    FIELD_FILLED, OTP_REQUIRED, CAPTCHA_DETECTED, HUMAN_ACTION_REQUIRED,
    REVIEW_REQUIRED, APPLICATION_SUBMITTED, APPLICATION_FAILED, ...).

    `payload` is display context ONLY and must never carry a verification code,
    password, session cookie, or token — it is serialized straight to a browser
    over the socket. Same rule as `HumanInteractionRequest.safe_metadata`.
    """

    stream_id: str
    event_type: str
    payload: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """Catch a mistyped event name at the publish site.

        A warning rather than an exception: publishing happens mid-run on the
        automation thread, and a notification bug must never be able to abort a
        real job application. The log line is enough to find it, and the
        contract test above fails the build for anything the frontend uses.
        """
        if self.event_type not in WORKFLOW_EVENTS:
            logger.warning(
                "Publishing unknown event type %r — add it to WORKFLOW_EVENTS or fix the typo. "
                "No frontend handler will match it.",
                self.event_type,
            )

    def to_json(self) -> dict:
        return {
            "stream_id": self.stream_id,
            "event": self.event_type,
            "payload": self.payload,
            "timestamp": self.created_at.isoformat(),
        }


def stream_id_for_application(application_id: str) -> str:
    return f"application:{application_id}"


def stream_id_for_task(task_id: str) -> str:
    return f"task:{task_id}"


class EventBus:
    """Fan-out from automation threads to WebSocket subscribers."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        # A plain threading.Lock, not an asyncio one: `publish` is called from
        # non-async worker threads that have no event loop of their own.
        self._lock = Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._redis_listener_task: asyncio.Task | None = None
        # Tags events this instance mirrors to Redis so its own listener skips
        # the loopback. Per-instance (not just the module constant) so a test
        # can stand up two buses that behave like two processes.
        self._origin = _PROCESS_ID

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the serving event loop so `publish` can hop onto it from a
        worker thread. Called once from the first subscriber, which is
        guaranteed to be running on that loop."""
        self._loop = loop

    def subscribe(self, stream_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUED_EVENTS)
        try:
            self.bind_loop(asyncio.get_running_loop())
            # First subscriber on this loop also starts the cross-process
            # listener (no-op if Redis is unconfigured or already running).
            self._ensure_redis_listener()
        except RuntimeError:  # pragma: no cover - subscribe is always called from async code
            pass
        with self._lock:
            self._subscribers.setdefault(stream_id, set()).add(queue)
        return queue

    def unsubscribe(self, stream_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            subscribers = self._subscribers.get(stream_id)
            if not subscribers:
                return
            subscribers.discard(queue)
            if not subscribers:
                # Drop the empty set so a long-lived process does not accumulate
                # one entry per application it has ever run.
                self._subscribers.pop(stream_id, None)

    def subscriber_count(self, stream_id: str) -> int:
        with self._lock:
            return len(self._subscribers.get(stream_id, ()))

    def publish(self, event: WorkflowEvent) -> None:
        """Thread-safe, non-blocking, and never raises.

        Callers are automation code mid-run. A failure to notify a browser tab
        must never propagate into — let alone abort — a real job application, so
        every delivery problem is swallowed and logged rather than raised.
        """
        # Cross-process mirror first, so an event still reaches other replicas
        # even when this process has no local subscriber for it.
        self._mirror_to_redis(event)

        with self._lock:
            queues = list(self._subscribers.get(event.stream_id, ()))
            loop = self._loop
        if not queues or loop is None:
            return
        for queue in queues:
            try:
                loop.call_soon_threadsafe(self._offer, queue, event)
            except RuntimeError:
                # The loop is closing/closed (shutdown, or a test that finished).
                logger.debug("Event bus: loop unavailable for %s.", event.event_type)

    # ---------- cross-process (Redis) ----------

    def _mirror_to_redis(self, event: WorkflowEvent) -> None:
        """Publish the event to Redis for other processes. Best-effort and
        synchronous (callers are worker threads); any failure is logged, never
        raised — same contract as local delivery."""
        from app.core.redis_client import sync_redis

        client = sync_redis()
        if client is None:
            return
        try:
            client.publish(
                _REDIS_CHANNEL_PREFIX + event.stream_id,
                json.dumps({"origin": self._origin, "event": event.to_json()}),
            )
        except Exception:  # noqa: BLE001 - notification transport must never raise into automation
            logger.warning("Event bus: Redis publish failed for %s.", event.event_type, exc_info=True)

    def _ensure_redis_listener(self) -> None:
        from app.core.redis_client import async_redis

        if self._redis_listener_task is not None and not self._redis_listener_task.done():
            return
        if async_redis() is None:
            return
        self._redis_listener_task = asyncio.get_running_loop().create_task(self._redis_listen())
        logger.info("Event bus: Redis cross-process listener started (%s).", self._origin)

    async def _redis_listen(self) -> None:
        """Re-inject events published by OTHER processes into local subscribers.
        Reconnects with backoff; a Redis blip degrades to in-process-only until
        it recovers, never crashes the process."""
        from app.core.redis_client import async_redis

        backoff = 1.0
        while True:
            try:
                client = async_redis()
                if client is None:  # pragma: no cover - listener isn't started in this case
                    return
                pubsub = client.pubsub()
                await pubsub.psubscribe(_REDIS_CHANNEL_PATTERN)
                backoff = 1.0
                async for message in pubsub.listen():
                    if message.get("type") != "pmessage":
                        continue
                    self._deliver_from_redis(message.get("data"))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the listener alive across Redis outages
                logger.warning("Event bus: Redis listener error, retrying in %.0fs.", backoff, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _deliver_from_redis(self, raw: str | None) -> None:
        try:
            envelope = json.loads(raw or "")
            if envelope.get("origin") == self._origin:
                return  # our own event — already delivered locally
            body = envelope["event"]
            event = WorkflowEvent(body["stream_id"], body["event"], body.get("payload") or {})
        except (ValueError, KeyError, TypeError):
            logger.warning("Event bus: undecodable Redis message dropped.", exc_info=True)
            return
        # Already on the event loop (the listener runs on it), so offer directly.
        with self._lock:
            queues = list(self._subscribers.get(event.stream_id, ()))
        for queue in queues:
            self._offer(queue, event)

    @staticmethod
    def _offer(queue: asyncio.Queue, event: WorkflowEvent) -> None:
        """Runs ON the event loop. Drops the oldest event when a subscriber is
        full, so one slow client can never apply backpressure to automation."""
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - drained concurrently
                pass
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:  # pragma: no cover - we just made room
            logger.debug("Event bus: dropped %s for a saturated subscriber.", event.event_type)


#: Module-level singleton — the process-wide bus, mirroring how `_REGISTRY` is
#: a module-level singleton in `runner.py`.
bus = EventBus()


def publish_application_event(application_id: str, event_type: str, **payload) -> None:
    bus.publish(WorkflowEvent(stream_id_for_application(application_id), event_type, payload))


def publish_task_event(task_id: str, event_type: str, **payload) -> None:
    bus.publish(WorkflowEvent(stream_id_for_task(task_id), event_type, payload))
