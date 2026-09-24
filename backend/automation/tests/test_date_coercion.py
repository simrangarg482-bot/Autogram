"""
`_coerce_date_string` / `DateHandler` against the date shapes résumé-derived
education/experience entries actually store (`app/models/db_models.py`
documents `start_date`/`end_date` as free-form: "2018" or "2018-08" - a résumé
rarely gives a full date). A native `<input type=date>` needs a full
YYYY-MM-DD; a partial value used to pass through unchanged, which such a field
silently rejects (no exception, no value set) - a start/end date field on any
ATS was left blank whenever the profile only had a year or year-month.
"""

from __future__ import annotations

import pytest

from automation.forms.field_handlers import (
    DateHandler,
    _coerce_date_string,
    _coerce_month_string,
    describe_field,
    fill_field,
)


@pytest.mark.parametrize("raw, expected", [
    ("2018", "2018-01-01"),
    ("2018-08", "2018-08-01"),
    ("2018-08-14", "2018-08-14"),      # already full - unchanged
    ("08/14/2018", "2018-08-14"),
    ("14/08/2018", "2018-08-14"),
    ("August 14, 2018", "2018-08-14"),
    ("not a date", "not a date"),      # unparseable - passed through, never guessed
])
def test_coerce_date_string(raw, expected):
    assert _coerce_date_string(raw) == expected


def test_a_bare_year_fills_a_native_date_input(page):
    page.set_content('<input type="date" id="d">')
    field = describe_field(page.locator("#d"), label="Start date", page=page)

    DateHandler().fill(field, "2018")

    assert page.locator("#d").input_value() == "2018-01-01"


def test_a_year_month_fills_a_native_date_input(page):
    page.set_content('<input type="date" id="d">')
    field = describe_field(page.locator("#d"), label="Start date", page=page)

    DateHandler().fill(field, "2018-08")

    assert page.locator("#d").input_value() == "2018-08-01"


def test_verification_uses_the_same_coercion(page):
    page.set_content('<input type="date" id="d">')
    field = describe_field(page.locator("#d"), label="Start date", page=page)
    DateHandler().fill(field, "2018-08")

    ok, actual = DateHandler().verify(field, "2018-08")

    assert ok and actual == "2018-08-01"


# ---------------------------------------------------------------------------
# `<input type="month">` — previously had NO handler at all (not `date`, and
# `month` is not one of `TextInputHandler`'s `_TEXT_FILLABLE_INPUT_TYPES`
# either), so every such field — the common shape for "expected graduation" /
# certification-expiry questions — failed `no_handler_matched` 100% of the
# time regardless of the value supplied. Regression-pinned here.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("2026-05", "2026-05"),
    ("2026-05-14", "2026-05"),
    ("2026", "2026-01"),
    ("May 2026", "2026-05"),
    ("05/2026", "2026-05"),
])
def test_coerce_month_string(raw, expected):
    assert _coerce_month_string(raw) == expected


def test_month_input_now_has_a_handler_and_fills_correctly(page):
    page.set_content('<label for="grad">Expected graduation</label><input type="month" id="grad">')
    field = describe_field(page.locator("#grad"), label="Expected graduation", page=page)

    outcome = fill_field(field, "2026-05")

    assert outcome.filled, outcome.failure
    assert outcome.actual_value == "2026-05"
    assert page.locator("#grad").input_value() == "2026-05"
