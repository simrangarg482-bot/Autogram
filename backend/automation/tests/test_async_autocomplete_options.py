"""
Adversarial regression test: a location/address/employer-name-style
autocomplete whose options are populated by a real (simulated) NETWORK-LATENCY
debounce — e.g. Google Places-style city/address lookup — rather than an
instant, purely-local filter.

`_DropdownHandler.fill()`'s search-input path used to wait a single fixed
300ms after typing before searching for a matching option — long enough for
most instant local filters, but not for a genuinely async lookup. Worse than
just failing slowly: when the option never showed up within that single
300ms window, `fill()` silently gave up (no click, no exception), and
`verify()`'s weakest fallback tier (the raw text still sitting in the search
box, since nothing else was inspectable in time) then matched the just-typed
text against itself — reporting a fill that never actually selected anything
as a successful, verified fill. Fixed by polling (bounded, ~1.8s) instead of
sleeping once.
"""

from __future__ import annotations

from automation.forms.field_handlers import describe_field, fill_field

# A combobox whose options only mount ~1.5s after the debounced 'input'
# event — clicking an option is what actually "selects" it (sets
# window.__selected and the input's displayed value); nothing else does.
ASYNC_AUTOCOMPLETE_HTML = """<!DOCTYPE html><html><body>
<label for="city">City</label>
<input id="city" role="combobox" aria-autocomplete="list" aria-expanded="false" autocomplete="off">
<div id="menu-host"></div>
<script>
  const input = document.getElementById('city');
  const host = document.getElementById('menu-host');
  let timer;
  input.addEventListener('input', function () {
    input.setAttribute('aria-expanded', 'true');
    host.innerHTML = '';
    clearTimeout(timer);
    timer = setTimeout(function () {
      host.innerHTML = '<div role="listbox">'
        + '<div role="option" id="opt-sf">San Francisco, CA</div>'
        + '<div role="option" id="opt-sd">San Diego, CA</div>'
        + '</div>';
      document.getElementById('opt-sf').addEventListener('click', function () {
        input.value = 'San Francisco, CA';
        window.__selected = 'San Francisco, CA';
        host.innerHTML = '';
      });
      document.getElementById('opt-sd').addEventListener('click', function () {
        input.value = 'San Diego, CA';
        window.__selected = 'San Diego, CA';
        host.innerHTML = '';
      });
    }, 1500);
  });
</script>
</body></html>"""


def test_fills_correctly_when_matching_options_load_after_a_network_style_delay(page):
    page.set_content(ASYNC_AUTOCOMPLETE_HTML)
    field = describe_field(page.locator("#city"), label="City", page=page)

    outcome = fill_field(field, "San Diego, CA")

    assert outcome.filled, outcome.failure
    # The real proof this wasn't a false-positive text-match: a genuine
    # option click is the only thing in this fixture that ever sets this.
    assert page.evaluate("window.__selected") == "San Diego, CA"


def test_does_not_false_positive_select_the_wrong_option_under_the_same_delay(page):
    page.set_content(ASYNC_AUTOCOMPLETE_HTML)
    field = describe_field(page.locator("#city"), label="City", page=page)

    fill_field(field, "San Francisco, CA")

    assert page.evaluate("window.__selected") == "San Francisco, CA"
