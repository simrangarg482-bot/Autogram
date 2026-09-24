"""
Adversarial regression test: selecting "Other" in a react-select-style custom
dropdown mounts a brand-new conditional text input that did not exist when
the page was first scanned. This is one of the most common real-world
application-form shapes (a "How did you hear about us?" / "Which visa do you
hold?" dropdown with an "Other, please specify" follow-up) and exercises two
things together that individual unit tests don't:

1. `ComboboxHandler.fill()` selects an option via a real `click()` on the
   option element (not `select_option()`), so the click must actually fire
   whatever JS listener mounts the conditional field — a handler that instead
   forced the DOM value via JS would leave the conditional field unmounted.
2. `describe_field()` must be able to find and describe a field that is
   discovered only by an APPLICATION_FLOW_MANAGER-style re-scan (a second
   `page.locator(...)` call after the widget's `fill()` returns), not a
   `Locator` captured before the option was clicked and gone stale.
"""

from __future__ import annotations

from automation.forms.field_handlers import describe_field, fill_field


def _render_form(page):
    page.set_content(
        """<!DOCTYPE html><html><body>
        <div class="select__container">
          <label id="src-label" for="src" class="select__label">How did you hear about us?</label>
          <div class="select__control">
            <div class="select__value-container">
              <div class="select__placeholder" id="src-placeholder">Select...</div>
              <div class="select__input-container">
                <input class="select__input" id="src" type="text" role="combobox"
                       aria-autocomplete="list" aria-expanded="false" aria-haspopup="true"
                       autocomplete="off" tabindex="0" style="opacity:1">
              </div>
            </div>
          </div>
          <div class="menu-host" id="src-host"></div>
        </div>
        <div id="other-host"></div>
        <script>
        (function() {
          const input = document.getElementById('src');
          const host = document.getElementById('src-host');
          const container = input.closest('.select__container');
          const control = container.querySelector('.select__control');
          const placeholder = document.getElementById('src-placeholder');
          const otherHost = document.getElementById('other-host');
          const options = ['LinkedIn', 'Referral', 'Other'];

          function close() { host.innerHTML = ''; input.setAttribute('aria-expanded', 'false'); }
          function open() {
            host.innerHTML = '<div class="select__menu"><div class="select__menu-list" role="listbox">'
              + options.map(o => '<div class="select__option" role="option">' + o + '</div>').join('')
              + '</div></div>';
            input.setAttribute('aria-expanded', 'true');
            host.querySelectorAll('.select__option').forEach(function(opt) {
              opt.addEventListener('click', function() {
                const vc = control.querySelector('.select__value-container');
                let sv = vc.querySelector('.select__single-value');
                if (!sv) { sv = document.createElement('div'); sv.className = 'select__single-value'; vc.prepend(sv); }
                sv.textContent = opt.textContent;
                if (placeholder) placeholder.style.display = 'none';
                input.value = '';
                input.style.opacity = '0';
                close();

                if (opt.textContent === 'Other') {
                  otherHost.innerHTML = '<label for="src_other">Please specify</label>'
                    + '<input type="text" id="src_other">';
                } else {
                  otherHost.innerHTML = '';
                }
              });
            });
          }
          input.addEventListener('click', function() {
            input.getAttribute('aria-expanded') === 'true' ? close() : open();
          });
        })();
        </script>
        </body></html>"""
    )


def test_selecting_other_mounts_and_fills_the_conditional_text_field(page):
    _render_form(page)

    # Before selection: the conditional field genuinely does not exist yet.
    assert page.locator("#src_other").count() == 0

    dropdown_field = describe_field(page.locator("#src"), label="How did you hear about us?", page=page)
    outcome = fill_field(dropdown_field, "Other")
    assert outcome.filled, outcome.failure

    # The dropdown selection's own click handler must have mounted the field —
    # this is what a JS-only value-forcing handler would fail to trigger.
    assert page.locator("#src_other").count() == 1, (
        "Selecting 'Other' via the dropdown handler did not mount the "
        "conditional 'Please specify' field — the option click likely wasn't "
        "a real click event the page's own listener could react to."
    )

    other_field = describe_field(page.locator("#src_other"), label="Please specify", page=page)
    other_outcome = fill_field(other_field, "Conference talk")
    assert other_outcome.filled, other_outcome.failure
    assert other_outcome.actual_value == "Conference talk"


def test_switching_away_from_other_unmounts_the_conditional_field(page):
    _render_form(page)

    dropdown_field = describe_field(page.locator("#src"), label="How did you hear about us?", page=page)
    fill_field(dropdown_field, "Other")
    assert page.locator("#src_other").count() == 1

    # Re-fetch the field description fresh (the widget is stale after the
    # previous selection reset its DOM state) rather than reusing the old
    # `Field`/`Locator`.
    dropdown_field_again = describe_field(page.locator("#src"), label="How did you hear about us?", page=page)
    outcome2 = fill_field(dropdown_field_again, "LinkedIn")
    assert outcome2.filled, outcome2.failure
    assert page.locator("#src_other").count() == 0
