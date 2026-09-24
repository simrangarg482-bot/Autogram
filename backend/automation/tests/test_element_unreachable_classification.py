"""
`fill_field`'s failure reason must distinguish two genuinely different
situations, not report both as "verification_failed":

- the field was TOUCHED every attempt, but the value never stuck (a widget
  resets it, a mask rejects it, ...) -- "verification_failed", unchanged.
- the field's locator never resolved to anything actionable on ANY attempt
  (a react-select-style remount regenerated its id between `describe_field()`
  and `fill_field()`, so every one of the 3 retries hits the exact same
  permanently-broken selector) -- "element_unreachable", new.

Reproduced empirically (real Chromium, not simulated): an id-based locator
resolved BEFORE a same-origin id-changing rerender is caught by Playwright as
"Target crashed"/timeout on every attempt; a relationship-based locator
(`label + input`) built the same way survives the same rerender because its
selector doesn't depend on the id — pinning that the classification change
does not paper over a class of failure `ats/base.py`'s locator strategy could
instead avoid by preferring relationship-based selectors, which is a
separate, bigger change out of scope here.

Also bounds `TextInputHandler`/`TextAreaHandler`'s own fill/verify calls and
`_capture_element_html`'s debug snapshot to a short explicit timeout — without
that, a dead locator burns Playwright's UNBOUNDED 30s default on every one of
these calls; empirically measured at 334s for 3 such tests before the fix
(background run, this session). `fill_field(..., registry=...)` is used below
to scope the scenario to JUST `TextInputHandler`, deliberately bypassing
`FieldHandlerRegistry.get_handler()`'s own full probe of every OTHER
handler's `supports()` — several of those (`ReactSelectHandler`,
`ComboboxHandler`, ...) also call `locator.get_attribute(...)` with no
explicit timeout, a second, separate, systemic instance of the same root
cause across the handler-DETECTION phase rather than the fill/verify phase
fixed here. Logged as a known follow-up (see AUTOGRAM_AUTOFILL_FAILURES.md)
rather than swept in this change — bounding every `supports()`'s live reads
consistently is a wider, separate, more carefully-audited change.
"""

from __future__ import annotations

from automation.forms.field_handlers import FieldHandlerRegistry, TextInputHandler, describe_field, fill_field

_ONLY_TEXT_INPUT = FieldHandlerRegistry([TextInputHandler()])


def test_a_locator_that_never_resolves_is_reported_as_element_unreachable(page):
    page.set_content(
        "<label for=\"g_v1\">Phone</label><input id=\"g_v1\" type=\"text\">"
        "<script>window.rerenderG = () => {"
        "  document.querySelector('label').setAttribute('for', 'g_v2');"
        "  document.querySelector('input').id = 'g_v2';"
        "};</script>"
    )
    field = describe_field(page.locator('[id="g_v1"]'), label="Phone", page=page)
    page.evaluate("window.rerenderG()")  # id changes AFTER describe_field, BEFORE fill

    outcome = fill_field(field, "555-1234", registry=_ONLY_TEXT_INPUT)

    assert outcome.filled is False
    assert outcome.failure.failure_reason == "element_unreachable"


def test_a_relationship_based_locator_survives_the_same_rerender(page):
    """Not every locator shape is vulnerable — pinned so a future change to
    `describe_field`'s locator-building strategy has a concrete baseline."""
    page.set_content(
        "<label for=\"f_v1\">Email</label><input id=\"f_v1\" type=\"text\">"
        "<script>window.rerender = () => {"
        "  document.querySelector('label').setAttribute('for', 'f_v2');"
        "  document.querySelector('input').id = 'f_v2';"
        "};</script>"
    )
    field = describe_field(page.locator("label:has-text('Email') + input"), label="Email", page=page)
    page.evaluate("window.rerender()")

    outcome = fill_field(field, "a@b.com", registry=_ONLY_TEXT_INPUT)

    assert outcome.filled is True


def test_a_fill_that_always_succeeds_but_never_verifies_stays_verification_failed(page):
    """The existing, unchanged contract (test_field_handlers.py pins this too;
    duplicated narrowly here so this file alone documents the FULL contrast)."""
    page.set_content(
        "<input id=\"f\" type=\"text\">"
        "<script>document.getElementById('f').addEventListener('input', (e) => { e.target.value = ''; });</script>"
    )
    field = describe_field(page.locator("#f"), label="Stubborn Field", page=page)

    outcome = fill_field(field, "Ada", registry=_ONLY_TEXT_INPUT)

    assert outcome.filled is False
    assert outcome.failure.failure_reason == "verification_failed"
