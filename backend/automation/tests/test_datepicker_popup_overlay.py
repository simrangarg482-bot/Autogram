"""
Adversarial regression test: a react-datepicker-style text input that opens a
calendar POPUP on focus/typing and does not auto-close on blur (it stays open
until something explicitly dismisses it — a day click or an outside click).
This is an extremely common real-world "Start date" / "Available from"
pattern on job applications (react-datepicker, MUI DatePicker, Ant Design
DatePicker all behave this way), and `DateHandler` only supports native
`input[type=date]` (see `field_handlers.py`), so this text-input variant falls
through to `TextInputHandler`, which types via `human_type()` and never
dismisses the resulting popup.

Reproduces the failure class: the leftover popup overlays the NEXT field in
the form, and Playwright's `click()` (used by `human_type`'s `click_first`
step) times out because the overlay intercepts pointer events at that
location — a `LOCATOR_FAILURE`/`ELEMENT_NOT_INTERACTABLE` on a field that has
nothing wrong with it at all; the previous field's leftover UI is the actual
cause.
"""

from __future__ import annotations

import os

from automation.forms.field_handlers import describe_field, fill_field

os.environ.setdefault("AUTOMATION_HUMAN_PACING", "1")


def _render_form(page):
    page.set_content(
        """<!DOCTYPE html><html><body>
        <label for="start_date">Available start date</label>
        <input id="start_date" type="text" autocomplete="off">
        <div id="calendar-popup" style="display:none; position:fixed; top:0; left:0;
             width:100%; height:100%; background:rgba(0,0,0,0.01); z-index:9999;">
          <div style="position:absolute; top:40px; left:40px; background:#fff; border:1px solid #ccc; padding:8px;">
            <button type="button" class="day" data-day="15">15</button>
          </div>
        </div>
        <label for="referral">How did you hear about us?</label>
        <input id="referral" type="text">
        <script>
          const input = document.getElementById('start_date');
          const popup = document.getElementById('calendar-popup');
          input.addEventListener('focus', () => { popup.style.display = 'block'; });
          input.addEventListener('input', () => { popup.style.display = 'block'; });
          popup.querySelector('.day').addEventListener('click', () => {
            popup.style.display = 'none';
          });
        </script>
        </body></html>"""
    )


def test_leftover_datepicker_popup_does_not_block_the_next_field(page):
    _render_form(page)

    date_field = describe_field(page.locator("#start_date"), label="Available start date", page=page)
    outcome = fill_field(date_field, "2026-10-01")
    assert outcome.filled, outcome.failure

    # The popup is still open at this point (nothing dismissed it) — this is
    # the buggy state being reproduced, not an assertion of correct behavior.
    assert page.locator("#calendar-popup").is_visible()

    referral_field = describe_field(page.locator("#referral"), label="How did you hear about us?", page=page)
    outcome2 = fill_field(referral_field, "LinkedIn")
    assert outcome2.filled, (
        "Leftover date-picker popup blocked the next field's fill "
        f"(failure={outcome2.failure})"
    )
    assert outcome2.actual_value == "LinkedIn"
