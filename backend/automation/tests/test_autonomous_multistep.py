"""Real-browser regressions for embedded applications and delayed wizard steps."""

from automation.agents.autonomous.actions import AgentAction
from automation.agents.autonomous.executor import ActionExecutor
from automation.agents.autonomous.observer import observe_page


def test_embedded_application_with_sticky_apply(page):
    page.set_content('''<h1>Engineering role</h1><p>Responsibilities and qualifications</p>
        <a href="#application">Apply</a><form id="application">
        <label>First name<input name="first_name"></label>
        <label>Email<input type="email"></label>
        <label>Resume<input type="file"></label>
        <button>Submit application</button></form>''')
    assert observe_page(page).page_type == "application_page"


def test_listing_with_email_alert_remains_listing(page):
    page.set_content('''<h1>Engineering role</h1><p>Qualifications</p>
        <a href="/apply">Apply</a><form><label>Email alert<input type="email"></label>
        <button>Subscribe</button></form>''')
    assert observe_page(page).page_type == "JOB_LISTING"


def test_required_field_prevents_advance(page, monkeypatch):
    from automation.agents.autonomous import executor as executor_module
    original = executor_module.advance_to_next_page
    monkeypatch.setattr(executor_module, 'advance_to_next_page',
                        lambda *args, **kwargs: original(*args, **kwargs, timeout_ms=300))
    page.set_content('''<h1>Application step 1</h1><form>
        <label>Full name<input required></label><button>Next</button></form>''')
    button = next(e for e in observe_page(page).elements if e.tag == 'button')
    result = ActionExecutor(page, auto_submit_approved=False).execute(
        AgentAction(action_type='click', element_ref=button.ref),
        element_name=button.name, element_semantic_action=button.semantic_action)
    assert not result.verified
    assert result.result_code == 'STEP_NOT_ADVANCED'
    assert page.locator('h1').inner_text() == 'Application step 1'


def test_five_step_application_with_delayed_navigation(page):
    page.set_content(r'''<main></main><script>
        let step = 1; window.answers = [];
        function render() {
          document.querySelector('main').innerHTML = step <= 5
            ? `<h1>Application step ${step}</h1><form onsubmit="advance(event)">
              <label>Answer ${step}<input name="answer_${step}" required></label>
              <button>${step === 5 ? 'Review' : 'Save and continue'}</button></form>`
            : '<h1>Review your application</h1><button onclick="document.querySelector(\'main\').innerHTML=\'Application submitted\'">Submit application</button>';
        }
        function advance(event) {
          event.preventDefault(); window.answers.push(document.querySelector('input').value);
          setTimeout(() => { step++; render(); }, 2600);
        }
        render();
    </script>''')
    executor = ActionExecutor(page, auto_submit_approved=False)
    for step in range(1, 6):
        state = observe_page(page)
        field = next(e for e in state.elements if e.tag == 'input')
        result = executor.execute(AgentAction(action_type='fill', element_ref=field.ref, value=f'answer {step}'))
        assert result.verified
        button = next(e for e in observe_page(page).elements if e.tag == 'button')
        result = executor.execute(AgentAction(action_type='click', element_ref=button.ref),
                                  element_name=button.name, element_semantic_action=button.semantic_action)
        assert result.verified, result.detail
        assert f'Application step {step}<' not in page.content()
    assert page.evaluate('window.answers') == [f'answer {n}' for n in range(1, 6)]
    state = observe_page(page)
    assert state.page_type == 'review'
    submit = next(e for e in state.elements if e.tag == 'button')
    action = AgentAction(action_type='click', element_ref=submit.ref)
    assert executor.execute(action, element_name=submit.name).blocked_reason == 'submit_requires_approval'
    executor.auto_submit_approved = True
    assert executor.execute(action, element_name=submit.name).verified
    assert observe_page(page).page_type == 'confirmation'
