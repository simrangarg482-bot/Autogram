"""
Answering demographic (EEO) fields from the user's SAVED answers, for the
autonomous agent.

The deterministic per-ATS engine has always answered gender / disability /
veteran / race / Hispanic-Latino only from `CandidateDemographics`
(`automation/forms/answer_engine.py::_demographic_answer`). The autonomous agent
could not, so every one of those fields ended in a human pause even for a user
who had saved their answers. This module gives it the same source with the same
rules:

- The saved value is NEVER sent to the LLM. `decision.py` is handed only
  `profile`, `resume_text` and `parsed_resume`; the snapshot's separate
  `demographics` key (see `app/api/autonomous_agent.py::_saved_demographics`)
  is read only here and in `loop.py`. The loop fills these fields itself,
  BEFORE asking the model what to do, so the model never has to guess one.
- The field's REAL options decide what is written. A saved token
  (`no_disability`) is resolved to the page's own wording through
  `demographic_matching.match_demographic_value`, which refuses anything
  ambiguous. Nothing resolvable -> nothing is written and the field falls
  through to the existing behaviour (the sensitive-field gate and a human).
- Nothing here weakens `executor.py`'s sensitive-field gate. The action this
  module proposes goes through `loop.py::_handle_execute_action` like any other
  and is only let through because `is_stored_answer` confirms the value is
  exactly what the user saved.

A field is classified by its OWN accessible name, not by the section/surrounding
text `question_ontology.classify_field` also folds in: an EEO section's intro
paragraph names gender, race and veteran status together, and classifying the
Race dropdown by that text could hand it the gender answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from playwright.sync_api import Page

from automation.agents.autonomous.observer import PageElement, PageState, field_identity
from automation.forms.demographic_matching import match_demographic_value
from automation.forms.field_handlers import describe_field, read_field_options
from automation.forms.question_classifier import DEMOGRAPHIC_STORAGE_FIELDS, classify_question

logger = logging.getLogger(__name__)

#: How many times the pre-pass will try one field in one run before leaving it to
#: the normal decision loop (and so, ultimately, a human). Independent of — and
#: tighter than — the persisted per-field attempt ledger, so a widget the
#: handlers cannot drive can never spin here.
MAX_PREPASS_ATTEMPTS = 2


@dataclass(frozen=True)
class SavedDemographicAnswer:
    """One demographic field on the page and the exact option to select in it."""

    element: PageElement
    category: str
    stored: str
    value: str


def saved_demographics_of(candidate_profile: dict | None) -> dict:
    """The `{column: value}` dict `_saved_demographics` put in the snapshot."""
    saved = (candidate_profile or {}).get("demographics")
    return saved if isinstance(saved, dict) else {}


def demographic_category(element: PageElement) -> str | None:
    """The demographic category of a field, judged by its own name only."""
    text = f"{element.name or ''} {getattr(element, 'aria_label', '') or ''}".strip()
    if not text:
        return None
    category = classify_question(text)
    return category if category in DEMOGRAPHIC_STORAGE_FIELDS else None


def stored_answer_for(element: PageElement, saved: dict) -> tuple[str, str] | None:
    """`(category, stored_value)` when `element` is a demographic field the user
    has saved an answer for, else `None`."""
    category = demographic_category(element)
    if category is None:
        return None
    stored = saved.get(DEMOGRAPHIC_STORAGE_FIELDS[category])
    if not isinstance(stored, str) or not stored.strip():
        return None
    return category, stored.strip()


def _is_dropdown(element: PageElement) -> bool:
    """Native `<select>` or a custom (ARIA) combobox — the two shapes a fixed
    list of options can be selected from. Radios/checkbox groups and free text
    are left to the normal flow."""
    return element.tag == "select" or element.role == "combobox" or element.type == "combobox"


def _is_empty(element: PageElement) -> bool:
    return not (element.value or element.selected_options)


def _real_options(page: Page, element: PageElement) -> list[str]:
    """The field's options: as observed if the observer already has them, else
    by opening the dropdown and reading them (`read_field_options` clicks the
    trigger, reads, and closes it without touching the value)."""
    if element.options:
        return [o for o in element.options if o]
    try:
        locator = page.locator(f'[data-agent-ref="{element.ref}"]')
        field = describe_field(locator, label=element.name or "", page=page)
        return list(read_field_options(field))
    except Exception:  # noqa: BLE001 - reading options is best-effort; no options means no answer
        logger.debug("Could not read the options of %r.", element.name, exc_info=True)
        return []


def find_pending_answer(
    page: Page, page_state: PageState, saved: dict, attempts: dict[str, int],
) -> SavedDemographicAnswer | None:
    """The next empty demographic dropdown on the page that the user has a saved
    answer for AND that answer resolves to one of its real options, or `None`.

    `attempts` is the caller's in-run counter keyed by `field_identity`; a field
    at `MAX_PREPASS_ATTEMPTS` is skipped."""
    if not saved:
        return None
    for element in page_state.elements:
        if element.disabled or not _is_dropdown(element) or not _is_empty(element):
            continue
        found = stored_answer_for(element, saved)
        if found is None:
            continue
        if attempts.get(field_identity(element), 0) >= MAX_PREPASS_ATTEMPTS:
            continue
        category, stored = found
        options = _real_options(page, element)
        value = match_demographic_value(stored, options) if options else None
        if value is None:
            # Counted so an unresolvable field is not re-probed (an open/close
            # of its dropdown) on every iteration of the loop.
            attempts[field_identity(element)] = MAX_PREPASS_ATTEMPTS
            logger.info(
                "Saved %s answer does not resolve to any option of %r — leaving it to the normal flow.",
                category, element.name,
            )
            continue
        return SavedDemographicAnswer(element=element, category=category, stored=stored, value=value)
    return None
