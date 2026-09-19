"""
A country dropdown whose options read "India +91" must be searched with the full
label, not just the name.

Observed on a live Greenhouse form (job-boards.greenhouse.io/warp): typing
"India" leaves two options, "British Indian Ocean Territory +246" (alphabetically
first) and "India +91", both of which `CountryPickerHandler` accepts; the first
one won, the control showed "+246", and all three retries repeated it. Typing
"India +91" leaves exactly one option.

The control also displays only the code once chosen ("+91"), so verification has
to be given the labelled form too - the plain name can never match "+91".

The fixture below reproduces that widget: a search box that FILTERS as you type
(unlike the static one in the other dropdown tests), and a single-value that
shows only the dial code.
"""

from __future__ import annotations

import pytest

from automation.forms.country_dial_codes import COUNTRY_DIAL_CODES
from automation.forms.field_handlers import (
    CountryPickerHandler,
    _country_with_dial_code,
    describe_field,
    fill_field,
)

GREENHOUSE_LIKE = [
    "British Indian Ocean Territory +246", "Germany +49", "India +91", "Ireland +353",
    "Niger +227", "Nigeria +234", "Northern Mariana Islands +1", "United States +1",
]


def _widget(options: list[str], *, show_code_only: bool = True) -> str:
    """react-select-shaped country picker: filters its options by the typed text and,
    once one is chosen, displays only its dialing code (as Greenhouse does)."""
    items = "".join(options_js(o) for o in options)
    return f"""
<!DOCTYPE html><html><body>
<div class="select__container">
  <label for="country">Country</label>
  <div class="select__control"><div class="select__value-container">
    <div class="select__placeholder">Select...</div>
    <input class="select__input" id="country" type="text" role="combobox" aria-autocomplete="list"
           aria-expanded="false" aria-haspopup="true" autocomplete="off">
  </div></div>
  <div id="host"></div>
</div>
<script>
(function() {{
  const OPTIONS = {options!r};
  const CODE_ONLY = {'true' if show_code_only else 'false'};
  const input = document.getElementById('country');
  const host = document.getElementById('host');
  const vc = document.querySelector('.select__value-container');
  window.__typed = [];
  function close() {{ host.innerHTML = ''; input.setAttribute('aria-expanded', 'false'); }}
  function render() {{
    const q = input.value.trim().toLowerCase();
    const shown = OPTIONS.filter(o => o.toLowerCase().includes(q));
    host.innerHTML = '<div class="select__menu"><div class="select__menu-list" role="listbox">' +
      shown.map((o, i) => '<div class="select__option" role="option" id="opt-' + i + '">' + o + '</div>').join('') +
      '</div></div>';
    input.setAttribute('aria-expanded', 'true');
    host.querySelectorAll('.select__option').forEach(function(el) {{
      el.addEventListener('click', function() {{
        let sv = vc.querySelector('.select__single-value');
        if (!sv) {{ sv = document.createElement('div'); sv.className = 'select__single-value'; vc.prepend(sv); }}
        const m = el.textContent.match(/\\+\\d+$/);
        sv.textContent = (CODE_ONLY && m) ? m[0] : el.textContent;
        window.__chosen = el.textContent;
        input.value = ''; close();
      }});
    }});
  }}
  input.addEventListener('click', function() {{ input.getAttribute('aria-expanded') === 'true' ? close() : render(); }});
  input.addEventListener('input', function() {{ window.__typed.push(input.value); render(); }});
  document.addEventListener('keydown', function(e) {{ if (e.key === 'Escape') close(); }});
}})();
</script></body></html>
"""


def options_js(_option: str) -> str:   # options are rendered by the script; kept for readability
    return ""


def _fill(page, value, options=GREENHOUSE_LIKE, **kw):
    page.set_content(_widget(options, **kw))
    field = describe_field(page.locator("#country"), label="Country", page=page, profile_attribute="country")
    assert isinstance(__import__("automation.forms.field_handlers", fromlist=["x"]).DEFAULT_HANDLER_REGISTRY.get_handler(field), CountryPickerHandler)
    return fill_field(field, value)


def _shown(page):
    return page.locator(".select__single-value").first.text_content().strip()


# ---------------------------------------------------------------------------
# The reported bug
# ---------------------------------------------------------------------------

def test_india_selects_india_not_the_alphabetically_first_match(page):
    outcome = _fill(page, "India")

    assert outcome.filled, outcome.failure
    assert page.evaluate("window.__chosen") == "India +91"      # before: British Indian Ocean Territory +246
    assert _shown(page) == "+91"


def test_the_full_label_is_what_gets_typed(page):
    _fill(page, "India")

    typed = page.evaluate("window.__typed")
    assert typed[-1] == "India +91"
    assert "India" not in typed[:-1] or all(t.startswith("India") for t in typed)   # never a bare, ambiguous "India" answer


@pytest.mark.parametrize("value, expected_label", [
    ("Niger", "Niger +227"),          # a substring of "Nigeria"
    ("Nigeria", "Nigeria +234"),
    ("Ireland", "Ireland +353"),      # a substring of "Northern Ireland"-style names
    ("USA", "United States +1"),      # alias -> canonical name -> label
    ("united states", "United States +1"),
])
def test_confusable_countries_and_aliases_pick_the_right_option(page, value, expected_label):
    outcome = _fill(page, value)

    assert outcome.filled, outcome.failure
    assert page.evaluate("window.__chosen") == expected_label


def test_verification_passes_when_the_control_shows_only_the_code(page):
    """"+91" is all the widget shows after a correct pick; the plain name "India"
    could never match it."""
    outcome = _fill(page, "India")

    assert _shown(page) == "+91"
    assert outcome.filled and outcome.failure is None


# ---------------------------------------------------------------------------
# Unchanged behaviour where the dial code does not apply
# ---------------------------------------------------------------------------

def test_a_country_not_in_the_table_is_typed_as_given(page):
    _fill(page, "Narnia")

    assert page.evaluate("window.__typed")[-1] == "Narnia"


def test_a_value_that_already_has_a_code_is_left_alone(page):
    outcome = _fill(page, "India +91")

    assert outcome.filled
    assert page.evaluate("window.__chosen") == "India +91"


def test_a_widget_whose_options_have_no_dial_codes_still_works(page):
    """Other ATS country pickers list plain names: "Germany +49" matches nothing
    there, so the handler falls back to the plain name exactly as before."""
    outcome = _fill(page, "Germany", options=["France", "Germany", "Ireland"], show_code_only=False)

    assert outcome.filled, outcome.failure
    assert page.evaluate("window.__chosen") == "Germany"
    assert _shown(page) == "Germany"


# ---------------------------------------------------------------------------
# The helper and the table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("India", "India +91"),
    ("  india  ", "India +91"),
    ("USA", "United States +1"),
    ("UK", "United Kingdom +44"),
    ("India +91", "India +91"),        # already labelled
    ("+91", "+91"),
    ("Narnia", "Narnia"),              # unknown: unchanged
    ("", ""),
])
def test_country_with_dial_code(value, expected):
    assert _country_with_dial_code(value) == expected


def test_the_table_covers_the_widget_and_every_code_is_digits():
    assert len(COUNTRY_DIAL_CODES) >= 240
    assert COUNTRY_DIAL_CODES["india"] == ("India", "91")
    assert COUNTRY_DIAL_CODES["british indian ocean territory"] == ("British Indian Ocean Territory", "246")
    for key, (name, code) in COUNTRY_DIAL_CODES.items():
        assert key == name.lower() and code.isdigit(), key
