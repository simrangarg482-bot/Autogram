"""
`capture_page_signature`'s CONTROLS half is scanned from the top-level `page`
by default — correct for URL/title/heading, wrong for `.controls` on a
platform that renders its form inside a same-origin content iframe (iCIMS,
confirmed live — see `automation.browser.selectors.resolve_form_root`).

`ApplicationFlowManager._fill_page`'s round loop used PLAIN `page`-scoped
signatures to detect a conditional field revealing itself after an answer
("Do you require sponsorship?" -> a follow-up visa-type question mounts).
On an iframe-rendered form, `page` has zero visible controls throughout —
not because nothing happened, but because nothing there was ever being
looked at — so the loop always read "revealed nothing" and silently never
re-scanned for (and therefore never filled) a conditional follow-up field
on that whole class of ATS platform. `resolve_form_root` had already fixed
the exact same blind spot for `_still_missing_required` (see that call
site's comment); the round-loop's OWN signature capture was a second,
independent instance of the same root cause.

Fix: `capture_page_signature(page, controls_root=...)` — `url`/`title`/
`heading`/`step_indicator` always come from `page`; `.controls` comes from
`controls_root` (default `page`, unchanged for every other caller).
"""

from __future__ import annotations

from automation.applications.page_navigator import capture_page_signature, visible_control_identities


def _render(page, body: str):
    page.set_content(f"<!DOCTYPE html><html><body>{body}</body></html>")


def test_default_controls_root_is_the_page_unchanged(page):
    """Every existing call site passes no `controls_root` — must be a no-op."""
    _render(page, "<input id='a'>")

    signature = capture_page_signature(page)

    assert signature.control_count == 1


def test_page_level_signature_is_blind_to_an_iframes_controls(page):
    """The bug, demonstrated directly: the form lives entirely in an iframe
    (the iCIMS shape), and a plain page-scoped signature sees nothing."""
    _render(page, "<iframe srcdoc=\"<input id='email'>\"></iframe>")
    frame = page.frames[-1]
    frame.wait_for_selector("#email")

    assert capture_page_signature(page).control_count == 0
    assert visible_control_identities(frame)  # the iframe itself sees it fine


def test_controls_root_scans_the_iframe_when_given_one(page):
    _render(page, "<iframe srcdoc=\"<input id='email'>\"></iframe>")
    frame = page.frames[-1]
    frame.wait_for_selector("#email")

    signature = capture_page_signature(page, controls_root=frame)

    assert signature.control_count == 1


def test_url_and_title_still_come_from_the_top_level_page_not_the_iframe(page):
    """`controls_root` must scope ONLY `.controls` — url/title stay page-level,
    which is what navigation detection (`_advance_to_next_page`) depends on."""
    _render(page, "<title>Application — Step 1</title><iframe srcdoc=\"<input id='email'>\"></iframe>")
    frame = page.frames[-1]
    frame.wait_for_selector("#email")

    signature = capture_page_signature(page, controls_root=frame)

    assert signature.title == page.title()
    assert signature.url == page.url


def test_a_conditional_field_revealed_inside_an_iframe_is_detected_as_new(page):
    """The actual round-loop comparison `_fill_page` makes: before vs. after,
    scoped to the SAME iframe both times, correctly reports the new field."""
    _render(page, "<iframe srcdoc=\"<input id='sponsorship'>\"></iframe>")
    frame = page.frames[-1]
    frame.wait_for_selector("#sponsorship")
    before = capture_page_signature(page, controls_root=frame)

    frame.evaluate("document.body.insertAdjacentHTML('beforeend', '<input id=\\'visa_type\\'>')")
    after = capture_page_signature(page, controls_root=frame)

    assert after.newly_visible_controls(before)


def test_a_page_scoped_before_against_an_iframe_scoped_after_would_have_false_positived():
    """Documents WHY the fix must not let the iframe-scoped signature escape
    `_fill_page` as its return value: mixing scopes makes `.controls` differ
    on every single comparison, which `_advance_to_next_page` would misread as
    "the page navigated" on every page-turn attempt for an iframe-rendered
    form. `_fill_page` re-captures a plain page-level signature before
    returning specifically to avoid this — see its own comment."""
    from automation.applications.page_navigator import PageSignature

    page_scoped = PageSignature(url="https://x", title="t", controls=())          # what `page` alone ever sees
    iframe_scoped = PageSignature(url="https://x", title="t", controls=("a|b",))  # what the iframe has

    assert page_scoped.differs_from(iframe_scoped)  # exactly the false positive being guarded against
