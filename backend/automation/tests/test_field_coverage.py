from types import SimpleNamespace

from automation.agents.autonomous.actions import AgentAction
from automation.agents.autonomous.decision import Decision
from automation.agents.autonomous.field_coverage import unresolved_fields
from automation.agents.autonomous.loop import AutonomousAgentLoop
from automation.agents.autonomous.observer import observe_page


def test_long_page_retains_fields_below_navigation(page):
    page.set_content(''.join(f'<a href="#{n}">Link {n}</a>' for n in range(150))
                     + '<label>Earliest start date<input></label><label>Website<input></label>')
    assert [e.name for e in unresolved_fields(observe_page(page))] == ['Earliest start date', 'Website']


def test_dropdown_mirrors_and_committed_selection(page):
    page.set_content('''<span id="q">Relocation</span><span id="r">preference</span>
      <div class="select__control"><div class="select__single-value">Yes</div>
        <input role="combobox" aria-labelledby="q r" aria-required="true">
        <input required aria-hidden="true" tabindex="-1"></div>''')
    state = observe_page(page)
    assert len(state.elements) == 1
    assert state.elements[0].name == 'Relocation preference'
    assert state.elements[0].selected_options == ['Yes']
    assert unresolved_fields(state) == []


def test_uncommitted_search_and_select_placeholder_are_missing(page):
    page.set_content('''<label>Relocation<input role="combobox" value="Yes"></label>
      <label>Start date<select><option value="">Select...</option><option>Now</option></select></label>''')
    assert len(unresolved_fields(observe_page(page))) == 2


def test_optional_questions_require_answer_or_explicit_skip(page):
    page.set_content('''<label>Website<input></label><label>Full name<input required></label>
      <label>Newsletter<input type="checkbox"></label>''')
    state = observe_page(page)
    assert len(unresolved_fields(state)) == 3
    assert [e.name for e in unresolved_fields(state, {'Website': 'leave blank', 'Full name': 'leave blank',
                                                   'Newsletter': 'no'})] == ['Full name']


def test_radio_group_is_one_question_and_readonly_fields_are_ignored(page):
    page.set_content('''<label>Yes<input type="radio" name="relocation" required checked></label>
      <label>No<input type="radio" name="relocation" required></label>
      <input readonly><div contenteditable="true" aria-label="Additional information">Details</div>''')
    assert unresolved_fields(observe_page(page)) == []


def test_asks_for_missing_answer_then_rechecks_before_advancing(page, monkeypatch):
    page.set_content('''<h1>Job application</h1><label>Earliest start date<input></label>
      <button>Next</button>''')
    loop = AutonomousAgentLoop('coverage-test', SimpleNamespace())
    task = SimpleNamespace(confirmed_answers={})
    requests = []
    monkeypatch.setattr(loop, '_pause_for_human', lambda db, task, request: requests.append(request))
    state = observe_page(page)
    button = next(e for e in state.elements if e.tag == 'button')
    decision = Decision(decision_type='EXECUTE_ACTION', action=AgentAction(action_type='click', element_ref=button.ref))
    assert not loop._handle_execute_action(None, task, state, decision)
    assert requests[0]['information_required'] == 'Earliest start date'
    task.confirmed_answers['Earliest start date'] = 'October 1'
    # A saved answer alone does not mean the browser field was filled.
    assert not loop._ensure_field_coverage(None, task, observe_page(page))
    page.get_by_label('Earliest start date').fill('October 1')
    assert loop._ensure_field_coverage(None, task, observe_page(page))
