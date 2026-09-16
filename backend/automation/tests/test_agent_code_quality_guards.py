"""
Regression guards for the code-quality audit (spec §18).

Only the findings that were CLEAR, LOW-RISK and TESTABLE are pinned here.
The structural ones (the lease lost mid-run in `app/workers/runtime.py`,
`signal_resume` reporting success for a Redis delivery that failed, the
coarse-status graph not being consulted from inside the repository) are
documented in the audit report and in the relevant docstrings rather than
changed, because each would be a layering or concurrency change rather than
a targeted fix.
"""

import ast
import inspect
import pathlib
import textwrap
import threading

import pytest

from automation.agents.autonomous import loop as loop_module
from automation.agents.autonomous import runner as runner_module


# --- the in-process double-dispatch race -----------------------------------

def test_claiming_a_task_is_atomic_against_a_concurrent_dispatch(monkeypatch):
    """REGRESSION. `is_running()` then `_get_or_create_handle()` took the
    registry lock TWICE, so two dispatches arriving together could both
    observe "not running" and both drive the same `TaskHandle` — two loops
    sharing one browser. Without `REDIS_URL` the worker lease is a no-op, so
    this claim is the only guard."""
    runner_module._REGISTRY.clear()
    started = threading.Barrier(2, timeout=5)
    claims = []
    lock = threading.Lock()

    def _worker():
        started.wait()
        handle = runner_module._claim_handle("race-task")
        with lock:
            claims.append(handle)

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    try:
        assert sum(1 for c in claims if c is not None) == 1, "exactly one thread may claim a task"
        assert sum(1 for c in claims if c is None) == 1
    finally:
        runner_module._REGISTRY.clear()


def test_execute_task_sync_never_runs_a_second_loop_for_a_live_task(monkeypatch):
    runner_module._REGISTRY.clear()
    runs = []
    monkeypatch.setattr(runner_module, "_run_and_cleanup", lambda tid, loop: runs.append(tid))

    runner_module.execute_task_sync("t1")          # claims and runs
    assert runs == ["t1"]

    # A handle left live (a run still in flight) must block a second entry.
    runner_module._REGISTRY["t1"] = object()
    runner_module.execute_task_sync("t1")
    assert runs == ["t1"], "a task with a live handle must not be driven twice"
    runner_module._REGISTRY.clear()


def test_the_claim_happens_under_a_single_lock_acquisition():
    """Structural, so the two-step form cannot come back: `_claim_handle`'s
    body contains exactly one `with _REGISTRY_LOCK`, and `execute_task_sync`
    does not call `is_running` before claiming."""
    claim_src = inspect.getsource(runner_module._claim_handle)
    assert claim_src.count("_REGISTRY_LOCK") == 1

    exec_src = inspect.getsource(runner_module.execute_task_sync)
    assert "_claim_handle" in exec_src
    assert "is_running(" not in exec_src


# --- browser lifecycle ------------------------------------------------------

def test_every_terminal_branch_of_run_releases_the_browser():
    """Structural sweep of `run()`: each `except` handler closes the browser,
    so no exception path can leak a driver process."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(loop_module.AutonomousAgentLoop.run)))
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "run() has no exception handlers — re-point this test"
    for handler in handlers:
        body = "\n".join(ast.unparse(stmt) for stmt in handler.body)
        assert "_close_browser()" in body, ast.unparse(handler.type)


def test_ensure_browser_closes_a_manager_it_never_published(monkeypatch):
    """The handle-publishing happens last, so `_close_browser` cannot see a
    manager that failed partway through — `_ensure_browser` has to close it
    itself on every failing exit."""
    source = inspect.getsource(loop_module.AutonomousAgentLoop._ensure_browser)
    assert "_safe_close(manager" in source
    assert "except BaseException" in source


def test_safe_close_logs_rather_than_swallowing():
    """Not a bare `except: pass` — a failed close is a leaked driver process
    and must leave a traceback behind."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(loop_module._safe_close)))
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    assert handlers
    for handler in handlers:
        body = "\n".join(ast.unparse(stmt) for stmt in handler.body)
        assert "logger.exception" in body
        assert not all(isinstance(stmt, ast.Pass) for stmt in handler.body)


# --- exception handling hygiene --------------------------------------------

def test_no_truly_bare_except_anywhere_in_the_agent_package():
    """`except Exception` with a logged reason is fine; a bare `except:` that
    also catches KeyboardInterrupt/SystemExit is not."""
    package = pathlib.Path(loop_module.__file__).parent
    offenders = []
    for path in sorted(package.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def test_no_silent_pass_only_handler_in_the_agent_package():
    """Every swallowed exception must at least say something — a handler
    whose entire body is `pass` hides a real failure."""
    package = pathlib.Path(loop_module.__file__).parent
    offenders = []
    for path in sorted(package.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], f"handlers that swallow without a trace: {offenders}"


def test_the_coordination_package_never_lets_a_redis_error_escape():
    """Every public function in `coordination/` is called from an API route
    or the loop's hot path, so none may raise on a Redis blip. `is_held` /
    `current_holder` are the documented exceptions — see the audit report and
    `runner.reconcile_orphaned_tasks_on_startup`, which guards its own call."""
    from automation.coordination import control

    guarded = ("send_signal", "consume_signal", "clear_all", "deposit_secret", "collect_secret")
    for name in guarded:
        source = inspect.getsource(getattr(control, name))
        assert "except Exception" in source, name


# --- database session hygiene ----------------------------------------------

def test_the_failure_path_rolls_back_before_writing():
    """`_safe_mark_failed` is the last thing between a crashed run and a task
    stuck in RUNNING, so it rolls back a possibly-poisoned session first and
    never raises."""
    source = inspect.getsource(loop_module.AutonomousAgentLoop._safe_mark_failed)
    source = textwrap.dedent(source)
    rollback_at = source.index("db.rollback()")
    mark_at = source.index("task_repo.mark_failed")
    assert rollback_at < mark_at
    assert "except TerminalTaskError" in source


def test_safe_mark_failed_cannot_raise(monkeypatch):
    """Behavioral counterpart: every write inside it failing still returns
    normally."""
    loop = loop_module.AutonomousAgentLoop(
        "quality", loop_module.TaskHandle(
            resume_event=threading.Event(), cancel_requested=threading.Event()),
    )

    class _BrokenDB:
        def rollback(self): raise RuntimeError("session is gone")

    class _Task:
        task_id = "quality"
        user_id = "u"
        current_status = "RUNNING"

    monkeypatch.setattr(loop_module.task_repo, "mark_failed",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("write failed")))
    loop._safe_mark_failed(_BrokenDB(), _Task(), "boom", reason="unexpected_error")  # must not raise


# --- coupling ---------------------------------------------------------------

def test_nothing_in_the_agent_package_imports_the_loop():
    """`loop.py` is the orchestrator and must stay a leaf: the modules it
    drives may not import it back, or the package acquires an import cycle
    and every helper becomes untestable without the whole loop."""
    package = pathlib.Path(loop_module.__file__).parent
    for path in sorted(package.glob("*.py")):
        if path.name in ("loop.py", "runner.py", "__init__.py"):
            continue
        text = path.read_text()
        assert "import loop" not in text, path.name
        assert "autonomous.loop" not in text, path.name


def test_the_agent_package_imports_form_an_acyclic_graph():
    package = pathlib.Path(loop_module.__file__).parent
    prefix = "automation.agents.autonomous"
    edges: dict[str, set[str]] = {}
    for path in sorted(package.glob("*.py")):
        module = path.stem
        targets = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(prefix):
                tail = node.module[len(prefix):].lstrip(".")
                if tail:
                    targets.add(tail)
                else:
                    targets.update(alias.name for alias in node.names)
        edges[module] = {t for t in targets if t != module}

    visiting, done = set(), set()

    def _walk(node: str, trail: tuple) -> None:
        if node in done:
            return
        assert node not in visiting, f"import cycle: {' -> '.join(trail + (node,))}"
        visiting.add(node)
        for target in sorted(edges.get(node, ())):
            if target in edges:
                _walk(target, trail + (node,))
        visiting.discard(node)
        done.add(node)

    for module in sorted(edges):
        _walk(module, ())


# --- safety checks that exist in code, not only in the prompt --------------

@pytest.mark.parametrize("rule,backstop", [
    # "SUBMIT ... requires explicit user approval"
    ("submit", "executor.is_submit_control_name"),
    # "Policy acknowledgements, arbitration agreements and consent require
    # the user's confirmation"
    ("sensitive", "executor.is_sensitive_field_name"),
    # "element_ref MUST be one of the ref values ... never invent one"
    ("grounding", "actions.validate_action_grounding"),
])
def test_each_prompt_safety_rule_has_a_code_level_backstop(rule, backstop):
    """The system prompt states these in prose; none of them may rely on the
    model honouring that prose. Each has an independent code gate."""
    import importlib

    module_name, attribute = backstop.rsplit(".", 1)
    module = importlib.import_module(f"automation.agents.autonomous.{module_name}")
    assert callable(getattr(module, attribute)), rule


def test_the_navigation_allowlist_rule_is_enforced_in_code_not_just_stated():
    """"Never construct /apply, /application, or any other guessed URL" is a
    prompt rule AND a grounding check."""
    from automation.agents.autonomous.actions import validate_action_grounding, AgentAction
    from automation.agents.autonomous.observer import PageState

    state = PageState(url="https://jobs.example/role/1", title="Role", visible_text="", elements=[])
    guessed = AgentAction(action_type="navigate", url="https://jobs.example/apply")
    grounding = validate_action_grounding(
        guessed, state, canonical_urls=("https://jobs.example/role/1", state.url),
    )
    assert grounding.grounded is False


def test_the_verification_code_rule_is_enforced_by_the_executor_not_the_prompt():
    from automation.agents.autonomous.executor import ActionExecutor
    from automation.agents.autonomous.actions import AgentAction

    class _Page:
        url = "https://jobs.example/verify"

        def __getattr__(self, name):
            raise AssertionError("a verification-code fill must be refused before touching the page")

    result = ActionExecutor(_Page(), auto_submit_approved=True).execute(
        AgentAction(action_type="fill", element_ref=1, value="123456"),
        element_name="One-time passcode",
    )
    assert result.blocked_reason == "verification_code_requires_deterministic_path"
