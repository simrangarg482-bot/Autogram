"""Completeness checks for every editable application question, including optional ones."""

from automation.agents.autonomous.observer import PageElement, PageState


def unresolved_fields(state: PageState, confirmed_answers: dict | None = None) -> list[PageElement]:
    answers = {str(k).strip().casefold(): str(v).strip().casefold()
               for k, v in (confirmed_answers or {}).items()}
    unresolved = []
    seen_radios = set()
    for element in state.elements:
        kind = element.role or element.type
        if element.disabled or element.type in ('hidden', 'submit', 'button', 'reset', 'password', 'search'):
            continue
        if element.tag not in ('input', 'textarea', 'select') and kind not in ('textbox', 'combobox', 'checkbox', 'radio'):
            continue
        if kind == 'radio' and element.group_name:
            group = (element.group_name, element.section)
            if group in seen_radios:
                continue
            seen_radios.add(group)
            if any(e.checked for e in state.elements
                   if (e.role or e.type) == 'radio' and (e.group_name, e.section) == group):
                continue
        answer = answers.get(element.name.strip().casefold(), '')
        if not element.required and answer in ('skip', 'leave blank', 'prefer not to answer'):
            continue
        if kind in ('checkbox', 'radio'):
            complete = bool(element.checked)
            if kind == 'checkbox' and not element.required and answer in ('no', 'false', 'unchecked'):
                complete = True
        elif kind == 'combobox':
            # The input's text can be an uncommitted search query.
            complete = bool(element.selected_options)
        else:
            complete = bool((element.value or '').strip())
        if not complete or element.invalid:
            unresolved.append(element)
    return unresolved
