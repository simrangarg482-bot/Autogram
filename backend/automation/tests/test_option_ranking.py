"""
`_find_matching_option` must prefer an EXACT (normalized) match over a merely
containing/contained match, regardless of DOM order — the general form of the
country-dropdown bug (India / British Indian Ocean Territory both contain
"india"). This is not country-specific: any dropdown where one option's text
is a substring of another's can hit it (e.g. "Contract" vs "Contract to Hire").

Two-pass behavior:
  pass 1 - exact normalized match, across ALL options, wins if any exists
  pass 2 (only if no exact match) - first loose/containment match, as before
"""

from __future__ import annotations

from automation.forms.field_handlers import _find_matching_option, _values_match


def _render_listbox(page, options: list[str]):
    items = "".join(f'<div role="option">{o}</div>' for o in options)
    page.set_content(f'<div role="listbox">{items}</div>')


def test_exact_match_wins_even_when_a_containing_option_renders_first(page):
    """The reported class of bug: "Contract to Hire" (contains "contract") comes
    before the exact "Contract" option in DOM order."""
    _render_listbox(page, ["Contract to Hire", "Contract", "Full-time"])

    option = _find_matching_option(page, "Contract", _values_match)

    assert option.text_content() == "Contract"


def test_exact_match_wins_when_it_is_first_too(page):
    _render_listbox(page, ["Contract", "Contract to Hire"])

    option = _find_matching_option(page, "Contract", _values_match)

    assert option.text_content() == "Contract"


def test_falls_back_to_first_loose_match_when_no_exact_match_exists(page):
    """Unchanged behavior: no option equals "United States" exactly, so the
    first containing option is used, same as before this fix."""
    _render_listbox(page, ["United States of America", "United States Minor Outlying Islands"])

    option = _find_matching_option(page, "United States", _values_match)

    assert option.text_content() == "United States of America"


def test_no_match_at_all_still_returns_none(page):
    _render_listbox(page, ["France", "Germany"])

    assert _find_matching_option(page, "Japan", _values_match) is None


def test_case_and_whitespace_insensitive_exact_match_still_wins(page):
    _render_listbox(page, ["Contracting", "  contract  "])

    option = _find_matching_option(page, "Contract", _values_match)

    assert option.text_content().strip().lower() == "contract"


# ---------------------------------------------------------------------------
# VirtualizedListboxHandler — same fix, same bug class, for an always-open
# role="listbox" panel (the shape most likely to be a long country/
# university/location list where substring collisions are common).
# ---------------------------------------------------------------------------

from automation.forms.field_handlers import VirtualizedListboxHandler, describe_field


def _render_virtualized_listbox(page, options: list[str]):
    items = "".join(f'<div role="option">{o}</div>' for o in options)
    page.set_content(f'<div role="listbox" style="height:200px;overflow:auto">{items}</div>')


def test_virtualized_listbox_prefers_exact_match_over_a_containing_one(page):
    _render_virtualized_listbox(page, ["Contract to Hire", "Contract"])
    field = describe_field(page.locator('[role="listbox"]'), label="Employment type", page=page)

    option = VirtualizedListboxHandler()._find_option(field, "Contract")

    assert option.text_content() == "Contract"
