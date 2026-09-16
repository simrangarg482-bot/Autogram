"""Unit tests for `automation.agents.autonomous.budgets`."""

from automation.agents.autonomous.budgets import AgentBudgets, BudgetTracker


def test_no_violation_when_under_every_limit():
    tracker = BudgetTracker(budgets=AgentBudgets(max_total_actions=10))
    for _ in range(5):
        tracker.record_action("click", succeeded=True, changed_page=True)
    assert tracker.check() is None


def test_total_actions_budget_exceeded():
    tracker = BudgetTracker(budgets=AgentBudgets(max_total_actions=3))
    for _ in range(3):
        tracker.record_action("click", succeeded=True, changed_page=True)
    result = tracker.check()
    assert result is not None
    assert result.budget_name == "max_total_actions"
    assert result.recommended_decision == "REQUEST_HUMAN_INTERVENTION"


def test_no_progress_streak_recommends_task_failed():
    tracker = BudgetTracker(budgets=AgentBudgets(max_no_progress_iterations=3))
    for _ in range(3):
        tracker.record_action("click", succeeded=True, changed_page=False)
    result = tracker.check()
    assert result is not None
    assert result.budget_name == "max_no_progress_iterations"
    assert result.recommended_decision == "TASK_FAILED"


def test_progress_resets_no_progress_streak():
    tracker = BudgetTracker(budgets=AgentBudgets(max_no_progress_iterations=3))
    tracker.record_action("click", succeeded=True, changed_page=False)
    tracker.record_action("click", succeeded=True, changed_page=False)
    tracker.record_action("click", succeeded=True, changed_page=True)  # progress!
    assert tracker.no_progress_streak == 0
    assert tracker.check() is None


def test_failed_actions_budget():
    tracker = BudgetTracker(budgets=AgentBudgets(max_failed_actions=2, max_no_progress_iterations=100))
    tracker.record_action("click", succeeded=False, changed_page=False)
    tracker.record_action("click", succeeded=False, changed_page=False)
    result = tracker.check()
    assert result is not None and result.budget_name == "max_failed_actions"


def test_llm_and_vision_call_counters():
    tracker = BudgetTracker(budgets=AgentBudgets(max_llm_calls=2, max_no_progress_iterations=100))
    tracker.record_llm_call()
    tracker.record_llm_call()
    assert tracker.check().budget_name == "max_llm_calls"


def test_unlimited_budget_never_trips():
    tracker = BudgetTracker(budgets=AgentBudgets(
        max_total_actions=None, max_navigation_actions=None, max_same_page_actions=None,
        max_failed_actions=None, max_time_per_application_seconds=None,
        max_llm_calls=None, max_vision_calls=None, max_no_progress_iterations=None,
    ))
    for _ in range(1000):
        tracker.record_action("click", succeeded=False, changed_page=False)
    assert tracker.check() is None
