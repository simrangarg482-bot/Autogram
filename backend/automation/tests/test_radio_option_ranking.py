"""RadioHandler must prefer an exact match over a containing one — the same
class of bug fixed in `_find_matching_option` (see test_option_ranking.py),
for radio groups: "Yes" / "Yes, with restrictions" is a real, common pair."""

from __future__ import annotations

from automation.forms.field_handlers import RadioHandler, describe_field


def test_exact_radio_option_wins_even_when_a_containing_option_renders_first(page):
    page.set_content(
        '<input type="radio" name="auth" id="r1" value="a"><label for="r1">Yes, with restrictions</label>'
        '<input type="radio" name="auth" id="r2" value="b"><label for="r2">Yes</label>'
    )
    field = describe_field(page.locator("#r1"), label="Authorized?", page=page)

    option = RadioHandler()._matching_option(field, "Yes")

    assert option.get_attribute("id") == "r2"


def test_falls_back_to_first_loose_match_when_no_exact_option_exists(page):
    page.set_content(
        '<input type="radio" name="auth" id="r1" value="a"><label for="r1">Yes, with restrictions</label>'
        '<input type="radio" name="auth" id="r2" value="b"><label for="r2">No</label>'
    )
    field = describe_field(page.locator("#r1"), label="Authorized?", page=page)

    option = RadioHandler()._matching_option(field, "Yes")

    assert option.get_attribute("id") == "r1"
