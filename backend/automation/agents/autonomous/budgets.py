"""
Agent budgets — configurable ceilings beyond the two that already exist
(`loop.py::MAX_ITERATIONS_PER_RESUME` = 80, `loop.py::MAX_FIELD_ATTEMPTS` = 3),
which this module does NOT replace (see spec §12: "keep the existing
80-iteration ceiling and 3-attempt field limit"). Both stay exactly where
they are, enforced exactly as before; `BudgetTracker` adds independent,
narrower ceilings so a model that stays "technically making progress" by the
80-iteration/3-attempt measures alone can still be stopped for a more
specific reason (e.g. it keeps navigating away from the application
entirely, or it keeps calling the vision fallback).

Every check here is purely additive counting — no network calls, no model
calls — so evaluating a budget on every iteration is effectively free.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


#: Action types that are NEUTRAL for stuck-detection: they are expected to
#: leave the page's structure exactly as it was, so neither "the page didn't
#: change" (a stall) nor "the page changed" (progress) is the right reading.
#:
#: The direction that actually mattered here is the RESET, not the increment.
#: `loop.py` records a dispatched action as `changed_page=result.success and
#: result.verified`, and `executor.py`'s `_do_scroll`/`_do_wait` return
#: `success=True` with `verified` defaulting True (correctly — "did it have
#: an effect" does not apply to a scroll). So every scroll used to CLEAR
#: `no_progress_streak` and `same_page_actions`, which meant a model
#: alternating an ineffective click with a scroll could never trip the
#: no-progress ceiling at all — the exact stuck-loop the ceiling exists to
#: catch, with a free reset button next to it.
#:
#: Scrolling to reveal a field is ordinary, expected behavior and must not
#: push a run toward TASK_FAILED either, hence neutral rather than
#: stall-counting. `MAX_ITERATIONS_PER_RESUME` remains the ceiling that
#: bounds these regardless, and `total_actions` still counts them.
NON_PROGRESS_ACTION_TYPES = frozenset({
    "scroll", "wait", "extract_text", "get_page_state",
})


@dataclass(frozen=True)
class AgentBudgets:
    """All limits are optional (`None` = unlimited) and generous by default,
    so an existing deployment that never configures these sees NO behavior
    change beyond what `MAX_ITERATIONS_PER_RESUME`/`MAX_FIELD_ATTEMPTS`
    already enforced. Tighten via constructor kwargs (or a config layer
    `loop.py` can wire in later) for a stricter deployment."""

    max_total_actions: int | None = 150
    max_navigation_actions: int | None = 25
    max_same_page_actions: int | None = 40
    max_failed_actions: int | None = 30
    max_time_per_application_seconds: float | None = 30 * 60
    max_llm_calls: int | None = 120
    max_vision_calls: int | None = 15
    max_no_progress_iterations: int | None = 8


@dataclass(frozen=True)
class BudgetExceeded:
    budget_name: str
    limit: int | float
    actual: int | float
    #: What the caller (`loop.py`) should do about it — never "retry
    #: silently forever" (spec §12): a navigation/action/failure/LLM/vision
    #: budget blown is ambiguous enough to hand to a human; a no-progress
    #: budget blown (the model is provably stuck) is a clean, honest failure.
    recommended_decision: str  # "REQUEST_HUMAN_INTERVENTION" | "TASK_FAILED"
    message: str


@dataclass
class BudgetTracker:
    """Per-task-run counters. One instance lives for the lifetime of one
    `AutonomousAgentLoop.run()` call (like `TaskHandle`, it does NOT survive
    a process restart — a resumed task after a crash starts fresh counters,
    which is deliberately conservative: a budget exists to bound ONE
    continuous run's cost/risk, not to accumulate punitively across
    restarts the task itself didn't cause)."""

    budgets: AgentBudgets = field(default_factory=AgentBudgets)
    started_at: float = field(default_factory=time.monotonic)
    total_actions: int = 0
    navigation_actions: int = 0
    same_page_actions: int = 0
    failed_actions: int = 0
    llm_calls: int = 0
    vision_calls: int = 0
    no_progress_streak: int = 0

    def record_action(self, action_type: str, *, succeeded: bool, changed_page: bool) -> None:
        self.total_actions += 1
        if action_type == "navigate":
            self.navigation_actions += 1
        if not succeeded:
            self.failed_actions += 1
        if action_type in NON_PROGRESS_ACTION_TYPES:
            # NEUTRAL: counted in `total_actions` (it still costs an
            # iteration) but it neither advances nor stalls the run, so it
            # must not touch the stuck-detection counters in EITHER
            # direction. See `NON_PROGRESS_ACTION_TYPES` for why the "reset"
            # direction is the one that actually mattered.
            return
        if not changed_page:
            self.same_page_actions += 1
        else:
            self.same_page_actions = 0
        if changed_page and succeeded:
            self.no_progress_streak = 0
        else:
            self.no_progress_streak += 1

    def record_llm_call(self) -> None:
        self.llm_calls += 1

    def record_vision_call(self) -> None:
        self.vision_calls += 1

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    def check(self) -> BudgetExceeded | None:
        """Returns the FIRST budget that's blown, or `None` if the run is
        still within every configured limit. Checked in an order that puts
        the clearest "the model is stuck" signal (no-progress) before the
        more ambiguous ones, since a caller that gets a `BudgetExceeded`
        acts on it immediately rather than collecting every violation."""
        b = self.budgets
        checks: list[tuple[str, int | float | None, int | float, str]] = [
            ("max_no_progress_iterations", b.max_no_progress_iterations, self.no_progress_streak, "TASK_FAILED"),
            ("max_time_per_application_seconds", b.max_time_per_application_seconds, self.elapsed_seconds(), "REQUEST_HUMAN_INTERVENTION"),
            ("max_total_actions", b.max_total_actions, self.total_actions, "REQUEST_HUMAN_INTERVENTION"),
            ("max_navigation_actions", b.max_navigation_actions, self.navigation_actions, "REQUEST_HUMAN_INTERVENTION"),
            ("max_same_page_actions", b.max_same_page_actions, self.same_page_actions, "TASK_FAILED"),
            ("max_failed_actions", b.max_failed_actions, self.failed_actions, "REQUEST_HUMAN_INTERVENTION"),
            ("max_llm_calls", b.max_llm_calls, self.llm_calls, "REQUEST_HUMAN_INTERVENTION"),
            ("max_vision_calls", b.max_vision_calls, self.vision_calls, "REQUEST_HUMAN_INTERVENTION"),
        ]
        for name, limit, actual, recommended in checks:
            if limit is not None and actual >= limit:
                return BudgetExceeded(
                    name, limit, actual, recommended,
                    f"Budget '{name}' exceeded: {actual} >= {limit}.",
                )
        return None
