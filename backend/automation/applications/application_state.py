"""
Application State — one coherent, inspectable object tracking where a single
`ApplicationFlowManager` run actually is: which page it's on, which pages are
done, what happened to every field it touched, which fields still need a
human's input (and why), what validation errors the form is currently
showing, and a timestamped log of the decisions made along the way.

This does NOT replace anything. `ApplicationFlowManager` already tracks
pieces of this — `_steps_completed`, `LIVE_RUN_STATE`, the `all_results`
list of `FieldFillResult`s, and (once a run concludes) `Application`/
`AutomationRun` in the database. This module gives that existing bookkeeping
one coherent shape, fed from the SAME events the flow manager already
produces (see `ApplicationFlowManager.checkpoint`/`_process_page`), rather
than introducing a second, competing source of truth.

In-memory and per-run, same as `LIVE_RUN_STATE` — deliberately NOT a new
database table. A run's durable record remains exactly what it was before
this existed (`Application`/`AutomationRun`, written once the run
concludes); this is the same kind of best-effort, single-process
observability view `LIVE_RUN_STATE` already is, just with real structure
instead of a loose bag of the last checkpoint's name and page number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

#: Reasons a field is worth a human's attention rather than automation's
#: best guess — the literal "sensitive, unknown, or low-confidence" cases:
#: no answer could be found at all, an answer existed but wasn't trusted
#: enough to use (see `automation.ats.base.ANSWER_REVIEW_CONFIDENCE_THRESHOLD`),
#: or the form is outright missing a value with no fallback anywhere in the
#: candidate's profile. Deliberately excludes `"fill_failed"` (a value WAS
#: chosen; the field handler just couldn't get it to stick) — that is a
#: mechanical problem, not a "should a human decide this" one.
PENDING_INPUT_REASONS = frozenset({"no_answer_available", "low_confidence", "missing_required"})

#: How many recent decisions `snapshot()` reports — a long-running,
#: many-page application must never make this grow unbounded in memory or
#: in what `GET /applications/{id}/live` sends over the wire.
_MAX_REPORTED_DECISIONS = 50


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PageRecord:
    """One page's outcome, in the order pages were actually processed."""

    page_number: int
    fields_seen: int
    fields_filled: int


@dataclass
class PendingUserInput:
    """One field this run could not confidently answer on the candidate's
    behalf. Distinct from (and a finer-grained companion to) the page-level
    `manual_required`/`needs_review`/`copilot_review` handoff, which already
    leaves the whole browser open for a human to act on any of these — this
    is what lets a reviewer see WHICH fields and WHY without having to find
    them on the page themselves."""

    field_key: str
    reason: str  # see PENDING_INPUT_REASONS
    page_number: int


@dataclass
class Decision:
    """One entry in the run's own timeline — the same events
    `ApplicationFlowManager.checkpoint()` already logs and reports via
    `LIVE_RUN_STATE`, just kept as a structured list instead of only the
    single most recent one."""

    timestamp: str
    event: str
    page_number: int | None = None


@dataclass
class ApplicationState:
    """Owned by one `ApplicationFlowManager` instance (`self.state`). Every
    method here is additive bookkeeping only — nothing in this class makes a
    decision, touches the page, or blocks anything; it only ever records
    what the flow manager already decided/observed."""

    application_id: str
    current_page_number: int = 0
    pages_completed: list[PageRecord] = field(default_factory=list)
    #: field_key -> "filled" | "unfilled:<reason>" — the field's LATEST known
    #: state. A field can be looked at more than once across a run (a
    #: multi-round page, a later page's résumé re-check); the latest entry
    #: always wins, so this reflects where things actually ended up, not a
    #: history of every attempt (the `decisions` log below is where the
    #: full timeline lives, for anyone who needs that).
    fields_processed: dict[str, str] = field(default_factory=dict)
    pending_user_input: list[PendingUserInput] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)

    def record_decision(self, event: str, *, page_number: int | None = None) -> None:
        """Appends one entry to the run's own timeline. Called from the same
        places `checkpoint()` already logs a step, so this never needs a
        second call site threaded through the page-processing loop."""
        self.decisions.append(Decision(timestamp=_now(), event=event, page_number=page_number))
        if page_number is not None:
            self.current_page_number = page_number

    def record_page_results(self, page_number: int, results) -> None:
        """Folds one page's `FieldFillResult`s (a slice of
        `ApplicationFlowManager.all_results` — just the ones produced while
        processing THIS page, not the whole run's) into the running field
        state. A field successfully filled here clears any earlier
        "needs input" entry for the same key — e.g. the vision pass filling
        on round 2 what the label sweep couldn't on round 1."""
        filled_count = 0
        for result in results:
            if result.filled:
                filled_count += 1
                self.fields_processed[result.field_key] = "filled"
                self.pending_user_input = [
                    p for p in self.pending_user_input if p.field_key != result.field_key
                ]
                continue

            reason = result.unfilled_reason or "unknown"
            self.fields_processed[result.field_key] = f"unfilled:{reason}"
            if reason in PENDING_INPUT_REASONS:
                self._add_pending(result.field_key, reason, page_number)

        self.pages_completed.append(
            PageRecord(page_number=page_number, fields_seen=len(results), fields_filled=filled_count)
        )
        self.current_page_number = page_number

    def record_validation_errors(self, errors) -> None:
        """The form's OWN complaints about what's already filled — replaces
        (rather than accumulates) the previous list, since these describe
        the page's CURRENT state, not a history of every error ever seen."""
        self.validation_errors = list(errors)

    def record_missing_required(self, field_names, *, page_number: int) -> None:
        """Required fields with no value anywhere in the candidate's profile
        — `ApplicationFlowManager._still_missing_required`'s own finding,
        recorded here as the same kind of "needs a human" entry a low-
        confidence answer produces, just with a different reason."""
        for name in field_names:
            self._add_pending(name, "missing_required", page_number)

    def _add_pending(self, field_key: str, reason: str, page_number: int) -> None:
        if any(p.field_key == field_key for p in self.pending_user_input):
            return
        self.pending_user_input.append(PendingUserInput(field_key=field_key, reason=reason, page_number=page_number))

    def snapshot(self) -> dict:
        """A plain-dict view — merged into `LIVE_RUN_STATE` (see
        `ApplicationFlowManager.checkpoint`) for `GET /applications/{id}/live`,
        and just as useful for logging/debugging a run directly."""
        return {
            "current_page_number": self.current_page_number,
            "pages_completed": [
                {"page_number": p.page_number, "fields_seen": p.fields_seen, "fields_filled": p.fields_filled}
                for p in self.pages_completed
            ],
            "fields_processed": dict(self.fields_processed),
            "pending_user_input": [
                {"field_key": p.field_key, "reason": p.reason, "page_number": p.page_number}
                for p in self.pending_user_input
            ],
            "validation_errors": list(self.validation_errors),
            "decisions": [
                {"timestamp": d.timestamp, "event": d.event, "page_number": d.page_number}
                for d in self.decisions[-_MAX_REPORTED_DECISIONS:]
            ],
        }
