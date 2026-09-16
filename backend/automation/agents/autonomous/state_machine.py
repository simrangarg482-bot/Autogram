"""
Explicit application-lifecycle state machine.

Two layers, deliberately kept separate:

1. **The coarse, PERSISTED status** — `AutonomousTask.current_status`
   (`app.models.db_models.VALID_AUTONOMOUS_TASK_STATUSES`): CREATED,
   ANALYZING_JOB, RUNNING, WAITING_FOR_HUMAN, WAITING_FOR_APPROVAL, RESUMING,
   COMPLETED, FAILED, CANCELLED. This is the column every existing API route,
   the frontend, and the crash-recovery reconciler
   (`runner.py::reconcile_orphaned_tasks_on_startup`) already reads and
   writes. `validate_coarse_transition` describes its legal graph as
   documentation and a regression guard; it deliberately does NOT replace
   `autonomous_task_repository.set_status`'s own guard
   (`_guard_not_terminal`) — that function's behavior is unchanged.

   WHERE IT IS ACTUALLY CALLED. Only from `loop.py::_set_status`, which is
   the agent loop's own wrapper around `task_repo.set_status`. It is NOT
   called from inside the repository, so the transitions that bypass
   `set_status` — `request_human_intervention` (-> WAITING_FOR_HUMAN),
   `mark_ready_for_approval` (-> WAITING_FOR_APPROVAL), `approve_submission`
   and `try_claim_for_resume` (-> RESUMING), `mark_completed`, `mark_failed`,
   `cancel_task`, `try_claim_orphan_failed` — are never checked against this
   graph, not even for a log line. The SAFETY property still holds for all of
   them (every one either calls `_guard_not_terminal` or, for `cancel_task`
   and the conditional-UPDATE claims, has its own terminal check), so what is
   missing is graph-level observability rather than a gate. Pushing this call
   down into the repository would make `app/services/` import
   `automation.agents.autonomous`, inverting the layering that this module's
   own `app.models.db_models` import establishes — a structural change
   deliberately left out of this pass.

   Runs in warn-only mode by default (see `STRICT_MODE` below) so a legitimate
   transition this graph didn't anticipate degrades to a logged warning
   instead of breaking task execution — changing the DB's own status enum or
   making this raise unconditionally is a larger, riskier change intentionally
   left out of this pass (see the accompanying report's "Known limitations").

2. **The finer, IN-MEMORY/observability sub-state** the spec asks for
   (NAVIGATING, JOB_PAGE, OBSERVING, DECIDING, EXECUTING, VERIFYING, REVIEW,
   READY_FOR_SUBMISSION, SUBMITTED, ORPHANED, UNKNOWN_SUBMISSION_STATE, ...).
   These are NOT new database columns — they are recorded in
   `AutonomousTask.application_progress` (already a free-form JSONB column,
   exactly like `PageState.workflow_state` already tracks a coarser
   "ANALYZING" label) as `application_progress["substate"]`, validated by
   `validate_substate_transition` below. This is the layer `loop.py`'s
   observe/decide/act/verify cycle actually moves through every iteration.

`UNKNOWN_SUBMISSION_STATE` is the one substate with a hard rule enforced in
code, not just documentation: `validate_substate_transition` refuses every
outbound transition from it except back to a brand-new task's `CREATED`
substate — mirroring the real, already-shipped behavior in
`runner.py::_reconcile_one_task`'s `may_have_submitted` branch, which never
resubmits and always tells the human the outcome is uncertain.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.db_models import (
    AUTONOMOUS_TASK_TERMINAL_STATUSES,
    VALID_AUTONOMOUS_TASK_STATUSES,
)

#: The coarse graph, expressed as "from -> allowed next states". Anything not
#: listed as a key has no allowed outbound transitions here beyond what
#: `AUTONOMOUS_TASK_TERMINAL_STATUSES` already implies (nothing — terminal
#: states are final, exactly as `_guard_not_terminal` already enforces).
_COARSE_GRAPH: dict[str, frozenset[str]] = {
    "CREATED": frozenset({"ANALYZING_JOB", "FAILED", "CANCELLED"}),
    "ANALYZING_JOB": frozenset({"RUNNING", "FAILED", "CANCELLED"}),
    "RUNNING": frozenset({
        "RUNNING", "WAITING_FOR_HUMAN", "WAITING_FOR_APPROVAL",
        "COMPLETED", "FAILED", "CANCELLED",
    }),
    "WAITING_FOR_HUMAN": frozenset({"RESUMING", "CANCELLED", "FAILED"}),
    "WAITING_FOR_APPROVAL": frozenset({"RESUMING", "CANCELLED", "FAILED"}),
    "RESUMING": frozenset({
        "RUNNING", "WAITING_FOR_HUMAN", "WAITING_FOR_APPROVAL",
        "COMPLETED", "FAILED", "CANCELLED",
    }),
}

assert set(_COARSE_GRAPH) <= VALID_AUTONOMOUS_TASK_STATUSES
assert not (set(_COARSE_GRAPH) & AUTONOMOUS_TASK_TERMINAL_STATUSES)

#: Whether an unrecognized coarse transition raises (`True`) or only logs —
#: default `False` per this module's docstring: the coarse DB status
#: continues to be governed by `autonomous_task_repository`'s own guard, this
#: is an additive check, not a replacement gate. A caller integrating this
#: into a NEW code path (rather than the existing, already-safe
#: `set_status`) may set this `True` for a hard guarantee.
STRICT_MODE = False


class InvalidStateTransition(ValueError):
    pass


@dataclass(frozen=True)
class TransitionCheck:
    allowed: bool
    reason: str


def validate_coarse_transition(from_status: str, to_status: str) -> TransitionCheck:
    if from_status not in VALID_AUTONOMOUS_TASK_STATUSES or to_status not in VALID_AUTONOMOUS_TASK_STATUSES:
        return TransitionCheck(False, f"unknown status in transition {from_status!r} -> {to_status!r}")
    if from_status in AUTONOMOUS_TASK_TERMINAL_STATUSES:
        return TransitionCheck(False, f"{from_status} is terminal — no further transitions are allowed")
    if from_status == to_status:
        return TransitionCheck(True, "no-op transition")
    allowed_next = _COARSE_GRAPH.get(from_status, frozenset())
    if to_status in allowed_next:
        return TransitionCheck(True, "allowed")
    return TransitionCheck(False, f"{from_status} -> {to_status} is not in the known transition graph")


#: The finer, spec-requested sub-state vocabulary (§6), layered over the
#: coarse status as described in the module docstring.
SUBSTATES = frozenset({
    "CREATED", "NAVIGATING", "JOB_PAGE", "APPLICATION_STARTED", "OBSERVING",
    "DECIDING", "EXECUTING", "VERIFYING", "HUMAN_REQUIRED", "RESUMING",
    "REVIEW", "READY_FOR_SUBMISSION", "SUBMITTED", "COMPLETED", "FAILED",
    "CANCELLED", "ORPHANED", "UNKNOWN_SUBMISSION_STATE",
})

_TERMINAL_SUBSTATES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})

#: The observe -> decide -> act -> verify -> (re-observe) cycle, plus the
#: entry/exit points around it. This is intentionally a graph, not a strict
#: sequence: `VERIFYING` can go back to `OBSERVING` (the ordinary next
#: iteration), sideways to `HUMAN_REQUIRED` (a blocker was hit), or forward
#: to `REVIEW`/`READY_FOR_SUBMISSION` (the application looks done).
_SUBSTATE_GRAPH: dict[str, frozenset[str]] = {
    "CREATED": frozenset({"NAVIGATING", "FAILED", "CANCELLED"}),
    "NAVIGATING": frozenset({"JOB_PAGE", "OBSERVING", "FAILED", "CANCELLED", "HUMAN_REQUIRED"}),
    "JOB_PAGE": frozenset({"APPLICATION_STARTED", "OBSERVING", "HUMAN_REQUIRED", "FAILED", "CANCELLED"}),
    "APPLICATION_STARTED": frozenset({"OBSERVING", "HUMAN_REQUIRED", "FAILED", "CANCELLED"}),
    # INBOUND edges to `UNKNOWN_SUBMISSION_STATE` exist from every substate in
    # which a submit-intent click can already have been dispatched with its
    # outcome left unestablished — see `loop.py::_handle_execute_action`'s
    # submit guard and the resume check at the top of `_loop_body`. Being
    # able to REACH the most restrictive state in this graph can only ever
    # stop a task earlier; the hard rule is the single OUTBOUND edge below,
    # which is unchanged.
    "OBSERVING": frozenset({"DECIDING", "HUMAN_REQUIRED", "UNKNOWN_SUBMISSION_STATE", "FAILED", "CANCELLED"}),
    "DECIDING": frozenset({"EXECUTING", "HUMAN_REQUIRED", "REVIEW", "READY_FOR_SUBMISSION", "UNKNOWN_SUBMISSION_STATE", "COMPLETED", "FAILED", "CANCELLED"}),
    "EXECUTING": frozenset({"VERIFYING", "HUMAN_REQUIRED", "UNKNOWN_SUBMISSION_STATE", "FAILED", "CANCELLED"}),
    "VERIFYING": frozenset({"OBSERVING", "HUMAN_REQUIRED", "REVIEW", "UNKNOWN_SUBMISSION_STATE", "FAILED", "CANCELLED"}),
    "HUMAN_REQUIRED": frozenset({"RESUMING", "UNKNOWN_SUBMISSION_STATE", "FAILED", "CANCELLED"}),
    "RESUMING": frozenset({"OBSERVING", "HUMAN_REQUIRED", "UNKNOWN_SUBMISSION_STATE", "FAILED", "CANCELLED"}),
    "REVIEW": frozenset({"READY_FOR_SUBMISSION", "OBSERVING", "HUMAN_REQUIRED", "FAILED", "CANCELLED"}),
    "READY_FOR_SUBMISSION": frozenset({"SUBMITTED", "HUMAN_REQUIRED", "OBSERVING", "UNKNOWN_SUBMISSION_STATE", "FAILED", "CANCELLED"}),
    "SUBMITTED": frozenset({"COMPLETED", "UNKNOWN_SUBMISSION_STATE"}),
    "ORPHANED": frozenset({"UNKNOWN_SUBMISSION_STATE", "FAILED"}),
    # Hard rule (spec §6): a crash/timeout/connection-loss whose submission
    # outcome could not be established must NEVER automatically retry
    # submission. The only legal way out is starting an entirely new task —
    # represented here as the one allowed edge to a fresh CREATED substate —
    # never back into READY_FOR_SUBMISSION/SUBMITTED/COMPLETED from this
    # state. See `runner.py::_reconcile_one_task`'s `may_have_submitted`
    # branch, which is the real code path that lands a task here.
    "UNKNOWN_SUBMISSION_STATE": frozenset({"CREATED"}),
}

for _substate in SUBSTATES:
    if _substate in _TERMINAL_SUBSTATES:
        continue
    assert _substate in _SUBSTATE_GRAPH, f"substate {_substate!r} has no transition entry"


def validate_substate_transition(from_substate: str | None, to_substate: str) -> TransitionCheck:
    if to_substate not in SUBSTATES:
        return TransitionCheck(False, f"unknown substate {to_substate!r}")
    if from_substate is None:
        return TransitionCheck(True, "initial substate")
    if from_substate not in SUBSTATES:
        return TransitionCheck(False, f"unknown current substate {from_substate!r}")
    if from_substate in _TERMINAL_SUBSTATES:
        return TransitionCheck(False, f"{from_substate} is terminal — no further substate transitions are allowed")
    if from_substate == to_substate:
        return TransitionCheck(True, "no-op transition")
    allowed_next = _SUBSTATE_GRAPH.get(from_substate, frozenset())
    if to_substate in allowed_next:
        return TransitionCheck(True, "allowed")
    return TransitionCheck(False, f"{from_substate} -> {to_substate} is not in the known substate graph")
