"""
Dynamic page graph — treats application navigation as a graph of
(PageState, Action, resulting PageState) rather than assuming a fixed
`page 1 -> page 2 -> page 3` sequence. Purely for recovery and observability
(spec §7): nothing here changes what the agent may do, only what gets
recorded about what it already did.

Nodes are keyed by a page fingerprint — `page_navigator.PageSignature`'s own
identity is reused rather than inventing a second one (that module already
solves "is this structurally the same page as before," including the
"filling a field must not look like navigating" property its docstring
describes). Edges are the action that was executed to get from one node to
another, so a loop that revisits an earlier page (a "Back" click, a
validation error bouncing it to the same step) is visible as a real cycle in
the graph, not silently overwritten.

Held in-memory on the `TaskHandle` for the running task (same lifetime as
`TaskHandle.last_page_signature`), and periodically flushed into
`AutonomousTask.application_progress["page_graph"]` (already a free-form
JSONB column) via `as_dict()` — durability piggybacks on the existing
`autonomous_task_repository.update_progress` call `loop.py` already makes
each iteration, rather than a new persistence path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


def _stable_url(url: str) -> str:
    """The part of a URL that identifies WHICH PAGE this is, with the query
    string and fragment dropped.

    Node identity here is structural (spec §11): two observations are the
    same node when the form is the same, and different nodes when the form is
    different. Keeping the raw URL broke the second half of that in one
    common, real case — a redirect or a query-parameter change that alters
    nothing observable (`?step=2` on an SPA that never re-renders, a `utm_*`
    tag, a session/tracking parameter appended on the way back from an
    identity provider, a fragment router). Those minted a brand-new node for
    a page nothing had changed about, which is precisely the "treated as
    meaningfully different when nothing observable changed" the graph exists
    to avoid — it inflated the node count and made `is_cycle` blind to a
    revisit.

    The structural fields (heading, step indicator, controls) still do the
    real discriminating: two genuinely different steps that share a path are
    still two nodes because their controls differ, and two different job
    pages still differ by path and heading.

    `PageSignature.differs_from` is deliberately NOT changed to match. That
    comparison answers a different question — "did anything at all happen
    since the last iteration?" — for `loop.py`'s stalled-page check and
    `actions.py`'s staleness gate, where a bare URL change IS evidence that
    something happened and must keep counting."""
    for separator in ("#", "?"):
        url = url.split(separator, 1)[0]
    return url


def _signature_key(page_signature) -> str:
    """A short, stable dict key for a `PageSignature` — reuses its own
    equality-relevant fields (see `PageSignature.differs_from`) rather than
    re-deriving a fingerprint from scratch, modulo the URL normalization
    `_stable_url` documents."""
    import hashlib
    raw = "|".join([
        _stable_url(page_signature.url), page_signature.title, page_signature.heading,
        page_signature.step_indicator, "\x1f".join(page_signature.controls),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class PageGraphNode:
    fingerprint: str
    page_type: str
    url: str
    application_stage: Optional[str] = None
    visits: int = 0
    actions_taken: list[str] = field(default_factory=list)
    validation_state: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "page_type": self.page_type,
            "url": self.url,
            "application_stage": self.application_stage,
            "visits": self.visits,
            "actions_taken": list(self.actions_taken[-20:]),
            "validation_state": self.validation_state,
        }


@dataclass
class PageGraphEdge:
    from_fingerprint: Optional[str]
    to_fingerprint: str
    action_type: str
    action_detail: str
    succeeded: bool

    def as_dict(self) -> dict:
        return {
            "from": self.from_fingerprint,
            "to": self.to_fingerprint,
            "action_type": self.action_type,
            "action_detail": self.action_detail,
            "succeeded": self.succeeded,
        }


class PageGraph:
    def __init__(self) -> None:
        self._nodes: dict[str, PageGraphNode] = {}
        self._edges: list[PageGraphEdge] = []
        self._current_fingerprint: str | None = None

    def visit(
        self,
        page_signature,
        *,
        page_type: str = "UNKNOWN",
        application_stage: str | None = None,
        validation_state: str | None = None,
    ) -> str:
        """Records arriving at (or re-arriving at) a page. Returns its
        fingerprint, which the caller passes to `record_action` next."""
        fingerprint = _signature_key(page_signature)
        node = self._nodes.get(fingerprint)
        if node is None:
            node = PageGraphNode(
                fingerprint=fingerprint, page_type=page_type, url=page_signature.url,
                application_stage=application_stage,
            )
            self._nodes[fingerprint] = node
        node.visits += 1
        if validation_state:
            node.validation_state = validation_state
        self._current_fingerprint = fingerprint
        return fingerprint

    def record_action(self, action_type: str, action_detail: str, *, succeeded: bool) -> None:
        """Records the action taken FROM the current page. The resulting
        page's fingerprint is only known on the NEXT `visit()` call — this
        just labels the outbound edge; `visit()` fills in `to_fingerprint`
        via `close_pending_edge` once the destination is observed."""
        if self._current_fingerprint is not None:
            node = self._nodes.get(self._current_fingerprint)
            if node is not None:
                node.actions_taken.append(f"{action_type}: {action_detail}")
        self._pending_edge = (self._current_fingerprint, action_type, action_detail, succeeded)

    def close_pending_edge(self, resulting_fingerprint: str) -> None:
        pending = getattr(self, "_pending_edge", None)
        if pending is None:
            return
        from_fp, action_type, action_detail, succeeded = pending
        self._edges.append(PageGraphEdge(from_fp, resulting_fingerprint, action_type, action_detail, succeeded))
        self._pending_edge = None

    def is_cycle(self, page_signature) -> bool:
        """True when this page was already visited earlier in this run —
        the graph's answer to "have we been here before," used by recovery
        (a repeated arrival at the same node alongside a same-page action
        streak is corroborating evidence for `recovery.classify_failure`'s
        `NO_PROGRESS` code, never the sole trigger for it)."""
        return _signature_key(page_signature) in self._nodes

    def as_dict(self) -> dict:
        return {
            "nodes": {fp: node.as_dict() for fp, node in self._nodes.items()},
            "edges": [edge.as_dict() for edge in self._edges],
            "current": self._current_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "PageGraph":
        graph = cls()
        if not data:
            return graph
        for fingerprint, raw in (data.get("nodes") or {}).items():
            graph._nodes[fingerprint] = PageGraphNode(
                fingerprint=fingerprint,
                page_type=raw.get("page_type", "UNKNOWN"),
                url=raw.get("url", ""),
                application_stage=raw.get("application_stage"),
                visits=raw.get("visits", 0),
                actions_taken=list(raw.get("actions_taken") or []),
                validation_state=raw.get("validation_state"),
            )
        for raw in (data.get("edges") or []):
            graph._edges.append(PageGraphEdge(
                from_fingerprint=raw.get("from"), to_fingerprint=raw.get("to"),
                action_type=raw.get("action_type", ""), action_detail=raw.get("action_detail", ""),
                succeeded=bool(raw.get("succeeded")),
            ))
        graph._current_fingerprint = data.get("current")
        return graph
