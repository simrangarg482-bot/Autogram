"""
Per-task question ledger — extends the existing HITL question-tracking
pattern (`app.models.db_models.ApplicationQuestion`, the deterministic
engine's per-application answer ledger) to the autonomous-agent path, without
copy-pasting a second SQL table for it.

The deterministic engine's `ApplicationQuestion` is a real table because it
serves a dedicated Answer Review UI keyed by `application_id`. The
autonomous-agent path already keeps its own per-task JSONB state the same
way it keeps `field_attempt_ledger` (spec §16) — this ledger follows that
exact precedent: `AutonomousTask.question_ledger`, a JSONB dict keyed by
`observer.field_identity` (stable across re-observations of the same
logical field, unlike a `PageElement.ref`), persisted via
`app.services.autonomous_task_repository.record_question_ledger_entry`.

Every entry answers, on its own, the question the spec requires the system
be able to answer at any time: "why did Autogram enter this answer?" —
`explain()` renders that in one sentence from the stored fields, so a
support/debugging session never has to reconstruct it from the raw action
history.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field as dc_field
from datetime import datetime, timezone

#: Closed vocabulary for `status` — mirrors the shape (not the exact strings)
#: of `VALID_QUESTION_REVIEW_STATUSES` on the deterministic engine's
#: `ApplicationQuestion`, adapted for a loop that can revisit the same field
#: across several iterations rather than answering it once in a batch.
LEDGER_STATUSES = frozenset({
    "RESOLVED_AUTO", "RESOLVED_CONFIRMED", "PENDING_HUMAN", "REJECTED", "SKIPPED_OPTIONAL", "FAILED",
})

#: The provenance values `truth_store.py` treats as actually verified — kept
#: here as a local literal set (rather than importing `truth_store`, which
#: this module is deliberately independent of) purely so `explain()` can tell
#: "verified user fact" apart from "LLM-generated / unknown" when it renders
#: its sentence. `test_provenance_regression.py` asserts it stays in sync
#: with `truth_store._VERIFIED_PROVENANCE`.
_VERIFIED_PROVENANCE_TEXT = frozenset({
    "USER_PROFILE", "RESUME", "USER_CONFIRMED", "APPROVED_ANSWER_MEMORY",
    "DETERMINISTIC_DERIVATION",
})


@dataclass
class QuestionLedgerEntry:
    question_id: str
    page_id: str
    element_ref: int | None
    raw_label: str
    normalized_category: str | None
    field_type: str
    answer: str | None
    answer_source: str | None       # e.g. "profile" | "resume" | "confirmed_answer" | "llm" | None
    answer_provenance: str          # one of truth_store.PROVENANCE_VALUES
    confidence: float
    sensitivity: bool
    status: str                     # one of LEDGER_STATUSES
    attempt_count: int = 0
    requires_human: bool = False
    updated_at: str = dc_field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def as_dict(self) -> dict:
        return asdict(self)

    def explain(self) -> str:
        """One human-readable sentence answering "why did Autogram enter
        this answer?" — never reconstructs this from the action history;
        the entry itself is the source of truth for the explanation."""
        if self.answer is None:
            return f"'{self.raw_label}' has not been answered yet (status={self.status})."
        provenance_text = {
            "USER_PROFILE": "the candidate's verified profile",
            "RESUME": "the parsed resume",
            "USER_CONFIRMED": "an answer the user explicitly confirmed for this task",
            "APPROVED_ANSWER_MEMORY": "a previously approved answer for this same question",
            "DETERMINISTIC_DERIVATION": "a deterministic rule (not free-form generation)",
            "LLM_GENERATED": "an LLM-generated answer (not a verified user fact)",
            "UNKNOWN": "an unknown/unverified source",
        }.get(self.answer_provenance, self.answer_provenance)
        # An entry whose provenance is NOT one of the verified sources must
        # never render a sentence a human (or a review UI) could read as a
        # settled, verified answer. `requires_human` alone was not enough:
        # `loop.py::_record_question_ledger` records `answer_provenance
        # ="UNKNOWN"` with `requires_human=False` whenever a value was written
        # for a field the truth store could not resolve (an LLM-proposed
        # value), which previously rendered with no caveat at all. The caveat
        # is now driven by the provenance itself, not by the flag.
        if self.answer_provenance not in _VERIFIED_PROVENANCE_TEXT:
            review_note = (
                " This answer is NOT a verified user fact and requires human review."
            )
        else:
            review_note = " This answer still requires human review." if self.requires_human else ""
        return f"'{self.raw_label}' was answered with {self.answer!r} from {provenance_text}.{review_note}"


class QuestionLedger:
    """In-memory, per-task ledger. `loop.py` holds one instance per running
    task (rehydrated from `AutonomousTask.question_ledger` on resume via
    `from_dict`), calls `record(...)` whenever it resolves or attempts a
    field, and persists the whole thing back via
    `autonomous_task_repository.record_question_ledger_entry` /
    `set_question_ledger` the same way `field_attempt_ledger` is persisted."""

    def __init__(self) -> None:
        self._entries: dict[str, QuestionLedgerEntry] = {}

    @classmethod
    def from_dict(cls, data: dict | None) -> "QuestionLedger":
        ledger = cls()
        for key, raw in (data or {}).items():
            try:
                ledger._entries[key] = QuestionLedgerEntry(**raw)
            except TypeError:
                # A row written by a future/older schema version with extra
                # or missing keys — skipped rather than crashing the whole
                # task's resume; the field is simply treated as unresolved
                # again this run, and a fresh entry will be recorded for it.
                continue
        return ledger

    def to_dict(self) -> dict:
        return {key: entry.as_dict() for key, entry in self._entries.items()}

    def get(self, field_identity: str) -> QuestionLedgerEntry | None:
        return self._entries.get(field_identity)

    def record(self, field_identity: str, entry: QuestionLedgerEntry) -> None:
        existing = self._entries.get(field_identity)
        if existing is not None:
            entry.attempt_count = max(entry.attempt_count, existing.attempt_count)
        self._entries[field_identity] = entry

    def all_entries(self) -> list[QuestionLedgerEntry]:
        return list(self._entries.values())

    def pending_human_entries(self) -> list[QuestionLedgerEntry]:
        return [e for e in self._entries.values() if e.requires_human and e.status == "PENDING_HUMAN"]
