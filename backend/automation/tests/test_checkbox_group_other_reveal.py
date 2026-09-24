"""
Adversarial regression test: a checkbox-GROUP (native `<input type=checkbox>`
options, "select all that apply") where checking an "Other" checkbox reveals
a conditional free-text field — a very common shape for "Which of these
technologies have you used?" / "Race/ethnicity (select all that apply)"
questions, distinct from the single react-select "Other" case already covered
in `test_conditional_other_reveal.py`.

`CheckboxHandler` only ever answers ONE boolean checkbox at a time (see its
docstring — it takes a yes/no intent, not a set of labels), so a checkbox
GROUP is filled by locating and checking each individual box the caller
decided should be ticked. This test exercises that: check the "Other" box via
`CheckboxHandler`/`fill_field`, confirm the conditional field mounts, and
confirm the other (unrelated) boxes in the group are left untouched.
"""

from __future__ import annotations

from automation.forms.field_handlers import describe_field, fill_field


def _render_form(page):
    page.set_content(
        """<!DOCTYPE html><html><body>
        <fieldset>
          <legend>Which of these have you used professionally?</legend>
          <label><input type="checkbox" id="skill_python" name="skills" value="Python"> Python</label>
          <label><input type="checkbox" id="skill_go" name="skills" value="Go"> Go</label>
          <label><input type="checkbox" id="skill_other" name="skills" value="Other"> Other</label>
        </fieldset>
        <div id="other-host"></div>
        <script>
          document.getElementById('skill_other').addEventListener('change', function(e) {
            const host = document.getElementById('other-host');
            if (e.target.checked) {
              host.innerHTML = '<label for="skill_other_text">Please specify</label>'
                + '<input type="text" id="skill_other_text">';
            } else {
              host.innerHTML = '';
            }
          });
        </script>
        </body></html>"""
    )


def test_checking_the_other_box_in_a_group_mounts_and_fills_the_follow_up(page):
    _render_form(page)
    assert page.locator("#skill_other_text").count() == 0

    other_box = describe_field(page.locator("#skill_other"), label="Other", page=page)
    outcome = fill_field(other_box, "yes")
    assert outcome.filled, outcome.failure

    assert page.locator("#skill_other_text").count() == 1, (
        "Checking the 'Other' checkbox (native check()/uncheck()) did not fire "
        "the page's own 'change' listener that mounts the follow-up field."
    )

    # Sibling boxes must be untouched by filling one box in the group.
    assert not page.locator("#skill_python").is_checked()
    assert not page.locator("#skill_go").is_checked()

    follow_up = describe_field(page.locator("#skill_other_text"), label="Please specify", page=page)
    follow_up_outcome = fill_field(follow_up, "Rust")
    assert follow_up_outcome.filled, follow_up_outcome.failure
    assert follow_up_outcome.actual_value == "Rust"


def test_unchecking_the_other_box_unmounts_the_follow_up(page):
    _render_form(page)
    other_box = describe_field(page.locator("#skill_other"), label="Other", page=page)
    fill_field(other_box, "yes")
    assert page.locator("#skill_other_text").count() == 1

    other_box_again = describe_field(page.locator("#skill_other"), label="Other", page=page)
    outcome = fill_field(other_box_again, "no")
    assert outcome.filled, outcome.failure
    assert page.locator("#skill_other_text").count() == 0
