"""
Adversarial regression tests for repeated field GROUPS built by cloning a
template row without re-indexing `id`/`name` — invalid HTML, but an
extremely common real-world shape for "Add another employer" / "Add another
education entry" style sections on custom application forms.

Two independent, previously-unfixed bugs (B-005) found this way:

1. `CheckboxHandler`'s label-click fallback: a `<label for="dup_id">` click
   is resolved by the BROWSER via `document.getElementById(id)`, which always
   returns the FIRST element with that id — so clicking row 1's label
   silently toggled row 0's checkbox while leaving row 1 untouched, and
   `fill()` reported SUCCESS because it never verified the click actually
   changed the field it was asked to change.

2. `RadioHandler`'s native-group resolution: `_native_radio_group` looked up
   `input[type=radio][name=X]` PAGE-GLOBALLY, so two structurally identical
   repeated radio groups sharing one `name` (again, a cloned-template
   artifact) were merged into a single group — resolving "Yes" always landed
   on ROW 0's option, and `verify()` re-derives the SAME (wrong) group, so it
   confirmed success while row 0 was silently mutated and the row actually
   being answered was left untouched. This one is more severe: it corrupts a
   DIFFERENT question's answer while reporting the intended one as filled.
"""

from __future__ import annotations

from automation.forms.field_handlers import describe_field, fill_field

DUPLICATE_ID_CHECKBOX_HTML = """<!DOCTYPE html><html><body>
<div class="row">
  <input type="checkbox" id="agree" hidden>
  <label for="agree">I certify Employer A information is correct</label>
</div>
<div class="row">
  <input type="checkbox" id="agree" hidden>
  <label for="agree">I certify Employer B information is correct</label>
</div>
</body></html>"""


def test_checkbox_row_1_does_not_corrupt_row_0_with_a_duplicate_id(page):
    page.set_content(DUPLICATE_ID_CHECKBOX_HTML)
    boxes = page.locator("#agree")

    field = describe_field(boxes.nth(1), label="Employer B checkbox", page=page)
    outcome = fill_field(field, "yes")

    assert outcome.filled, outcome.failure
    assert boxes.nth(1).evaluate("el => el.checked") is True
    assert boxes.nth(0).evaluate("el => el.checked") is False, (
        "Filling row 1's checkbox corrupted row 0's checkbox — a duplicate "
        "`id`'s label click was trusted without verifying it changed the "
        "INTENDED element."
    )


def test_checkbox_row_0_still_uses_the_fast_label_click_path(page):
    """The fix must not regress the common (non-duplicate, or duplicate-but-
    first-match) case — row 0 IS what `getElementById` resolves to, so its
    label click is safe and should still work exactly as before."""
    page.set_content(DUPLICATE_ID_CHECKBOX_HTML)
    boxes = page.locator("#agree")

    field = describe_field(boxes.nth(0), label="Employer A checkbox", page=page)
    outcome = fill_field(field, "yes")

    assert outcome.filled, outcome.failure
    assert boxes.nth(0).evaluate("el => el.checked") is True
    assert boxes.nth(1).evaluate("el => el.checked") is False


DUPLICATE_NAME_RADIO_HTML = """<!DOCTYPE html><html><body>
<fieldset><legend>Employer 1: currently employed?</legend>
  <input type="radio" name="currently_employed" value="yes" id="r1"><label for="r1">Yes</label>
  <input type="radio" name="currently_employed" value="no" id="r2"><label for="r2">No</label>
</fieldset>
<fieldset><legend>Employer 2: currently employed?</legend>
  <input type="radio" name="currently_employed" value="yes" id="r3"><label for="r3">Yes</label>
  <input type="radio" name="currently_employed" value="no" id="r4"><label for="r4">No</label>
</fieldset>
</body></html>"""


def test_radio_row_1_yes_does_not_select_row_0_yes_with_a_duplicate_name(page):
    page.set_content(DUPLICATE_NAME_RADIO_HTML)

    field = describe_field(page.locator("#r3"), label="Employer 2 currently employed", page=page)
    outcome = fill_field(field, "yes")

    assert outcome.filled, outcome.failure
    assert page.locator("#r3").is_checked() is True
    assert page.locator("#r1").is_checked() is False, (
        "Filling Employer 2's 'Yes' radio selected Employer 1's 'Yes' "
        "instead — _native_radio_group merged two separate cloned-template "
        "groups that happen to share one `name` into a single page-global "
        "group."
    )


def test_radio_row_0_no_still_resolves_within_its_own_row(page):
    page.set_content(DUPLICATE_NAME_RADIO_HTML)

    field = describe_field(page.locator("#r2"), label="Employer 1 currently employed", page=page)
    outcome = fill_field(field, "no")

    assert outcome.filled, outcome.failure
    assert page.locator("#r2").is_checked() is True
    assert page.locator("#r1").is_checked() is False
    assert page.locator("#r3").is_checked() is False
    assert page.locator("#r4").is_checked() is False
