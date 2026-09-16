"""
Phase 5 — synthetic career-portal page corpus (generalization).

Every page below is rendered for real in Chromium via `page.set_content(...)`
and read back through `observer.observe_page` (the same pattern
`test_field_coverage.py` and `test_observer_blocker_detection.py` use), so
these exercise the real extraction script rather than hand-built
`PageState`s. `test_page_classifier.py` already covers the hand-built
`PageState` unit cases; this file is deliberately the end-to-end,
"what does a real rendered portal page classify as" layer.

Scenarios are named by SHAPE, never by vendor, and no production code
consulted here is allowed to branch on a vendor: `test_no_vendor_specific_
branching_in_production_code` asserts that directly against the module
source.

Where a synthetic page is genuinely ambiguous (an unrecognized custom
widget, a form sealed inside an `<iframe>`), the expected outcome is
`UNKNOWN` with low confidence — the honest "a human should look at this"
answer — NOT a forced specific classification. Forcing one would only be
achievable with vendor/heuristic special-casing, which is the thing this
file exists to prevent.
"""

import re
from pathlib import Path

import pytest

from automation.agents.autonomous import executor as executor_module
from automation.agents.autonomous import loop as loop_module
from automation.agents.autonomous import observer as observer_module
from automation.agents.autonomous import page_classifier as page_classifier_module
from automation.agents.autonomous import question_ontology as question_ontology_module
from automation.agents.autonomous.field_coverage import unresolved_fields
from automation.agents.autonomous.observer import (
    classify_page_type,
    compute_page_completion,
    field_identity,
    observe_page,
)
from automation.agents.autonomous.page_classifier import PAGE_TYPES, classify_page
from automation.agents.autonomous.question_ontology import classify_field

# ---------------------------------------------------------------------------
# The corpus. Each entry: (scenario_name, html, acceptable_page_types).
# `acceptable_page_types` is a SET on purpose — the contract under test is
# "reaches a reasonable, honest classification", not "produces one exact
# string a heuristic was tuned to produce".
# ---------------------------------------------------------------------------

_SINGLE_PAGE_APPLICATION = """
<h1>Apply: Senior Backend Engineer</h1>
<form>
  <label>First Name<input name="fn"></label>
  <label>Last Name<input name="ln"></label>
  <label>Email Address<input type="email" name="em"></label>
  <label>Resume<input type="file" name="rs"></label>
  <button type="button">Submit Application</button>
</form>
"""

_MULTI_STEP_APPLICATION = """
<h1>Application &mdash; Step 2 of 4</h1>
<h2>Contact details</h2>
<form>
  <label>Email Address<input type="email"></label>
  <label>Phone<input type="tel"></label>
  <label>City<input></label>
  <button type="button">Next</button>
</form>
"""

_DYNAMIC_REACT_FORM = """
<h1>Apply: Platform Engineer</h1>
<form id="f">
  <label>First Name<input></label>
  <label>Last Name<input></label>
  <div id="certs"></div>
  <button type="button" id="addcert">Add another certification</button>
  <button type="button">Submit Application</button>
</form>
<script>
  let n = 0;
  document.getElementById('addcert').addEventListener('click', () => {
    n += 1;
    const wrap = document.createElement('label');
    wrap.textContent = 'Certification ' + n;
    const input = document.createElement('input');
    wrap.appendChild(input);
    document.getElementById('certs').appendChild(wrap);
  });
</script>
"""

_CUSTOM_DROPDOWN = """
<h1>Apply: Data Analyst</h1>
<form>
  <label>First Name<input></label>
  <span id="lbl">Preferred start month</span>
  <div role="combobox" aria-labelledby="lbl" tabindex="0" style="width:200px;height:32px">Select...</div>
  <ul role="listbox" aria-labelledby="lbl">
    <li role="option" aria-selected="false">January</li>
    <li role="option" aria-selected="false">February</li>
    <li role="option" aria-selected="false">March</li>
  </ul>
  <button type="button">Submit Application</button>
</form>
"""

_NATIVE_DROPDOWN = """
<h1>Apply: Data Analyst</h1>
<form>
  <label>First Name<input></label>
  <label>Highest level of education
    <select><option value="">Select...</option><option>Bachelors</option><option>Masters</option></select>
  </label>
  <button type="button">Submit Application</button>
</form>
"""

_RADIO_GROUP = """
<h1>Additional questions</h1>
<form>
  <fieldset>
    <legend>Are you willing to relocate?</legend>
    <label>Yes<input type="radio" name="reloc" value="y"></label>
    <label>No<input type="radio" name="reloc" value="n"></label>
  </fieldset>
  <button type="button">Next</button>
</form>
"""

_CHECKBOX_GROUP = """
<h1>Additional questions</h1>
<form>
  <fieldset>
    <legend>Which programming languages are you proficient in?</legend>
    <label>Python<input type="checkbox" name="lang" value="py"></label>
    <label>Go<input type="checkbox" name="lang" value="go"></label>
    <label>Rust<input type="checkbox" name="lang" value="rs"></label>
  </fieldset>
  <button type="button">Next</button>
</form>
"""

_CONDITIONAL_QUESTION = """
<h1>Additional questions</h1>
<form>
  <fieldset>
    <legend>Have you worked with us before?</legend>
    <label>Yes<input type="radio" name="prior" id="prior-yes"></label>
    <label>No<input type="radio" name="prior" id="prior-no"></label>
  </fieldset>
  <div id="followup"></div>
  <button type="button">Next</button>
</form>
<script>
  document.getElementById('prior-yes').addEventListener('change', () => {
    document.getElementById('followup').innerHTML =
      '<label>Which team did you work with?<input id="team"></label>';
  });
</script>
"""

_DYNAMIC_FIELD = """
<h1>Apply: Support Engineer</h1>
<form>
  <label>First Name<input></label>
  <div id="late"></div>
  <button type="button">Submit Application</button>
</form>
<script>
  setTimeout(() => {
    document.getElementById('late').innerHTML =
      '<label>Earliest start date<input id="startdate"></label>';
  }, 50);
</script>
"""

_RESUME_UPLOAD = """
<h1>Apply: Product Designer</h1>
<form>
  <label>First Name<input></label>
  <label>Resume / CV<input type="file" id="rs"></label>
  <button type="button">Submit Application</button>
</form>
"""

_COVER_LETTER_UPLOAD = """
<h1>Apply: Product Designer</h1>
<form>
  <label>Full Name<input></label>
  <label>Cover Letter<input type="file" id="cl"></label>
  <button type="button">Submit Application</button>
</form>
"""

_VALIDATION_ERROR = """
<h1>Apply: Site Reliability Engineer</h1>
<p>Please correct the highlighted fields below.</p>
<form>
  <label>First Name<input value="Jane"></label>
  <label>Last Name<input value=""  aria-invalid="true" required></label>
  <div role="alert" class="field-error">Last Name is required.</div>
  <button type="button">Submit Application</button>
</form>
"""

_INLINE_ERROR = """
<h1>Apply: Site Reliability Engineer</h1>
<form>
  <label>First Name<input value="Jane"></label>
  <label>Email Address<input type="email" value="not-an-email" aria-invalid="true"></label>
  <span class="validation-error">Enter a valid email address.</span>
  <button type="button">Submit Application</button>
</form>
"""

_MODAL_FORM = """
<h1>Senior Backend Engineer</h1>
<p>About the role. Responsibilities. Requirements. Employment type: full time.</p>
<div role="dialog" aria-label="Application">
  <h2>Apply for this role</h2>
  <form>
    <label>First Name<input></label>
    <label>Last Name<input></label>
    <label>Email Address<input type="email"></label>
    <button type="button">Submit Application</button>
  </form>
</div>
"""

_EMBEDDED_APPLICATION = """
<h1>Senior Backend Engineer</h1>
<p>About the role. Responsibilities. Requirements. Job id 44812.</p>
<h2>Apply for this role</h2>
<form>
  <label>First Name<input></label>
  <label>Last Name<input></label>
  <label>Resume<input type="file"></label>
  <button type="button">Submit Application</button>
</form>
"""

_IFRAME_APPLICATION = """
<h1>Senior Backend Engineer</h1>
<iframe width="640" height="480" srcdoc="
  &lt;form&gt;
    &lt;label&gt;First Name&lt;input&gt;&lt;/label&gt;
    &lt;label&gt;Last Name&lt;input&gt;&lt;/label&gt;
    &lt;button type=&quot;button&quot;&gt;Submit Application&lt;/button&gt;
  &lt;/form&gt;"></iframe>
"""

_EXTERNAL_REDIRECT = """
<h1>Senior Backend Engineer</h1>
<p>About the role. Responsibilities. Qualifications. Employment type: full time.</p>
<a href="https://apply.another-host.example.org/postings/44812">Apply Now</a>
"""

_LOGIN_REQUIRED = """
<h1>Sign in to continue</h1>
<form>
  <label>Email<input type="email"></label>
  <label>Password<input type="password"></label>
  <button type="button">Sign in</button>
</form>
"""

_OTP_REQUIRED = """
<h1>Enter your verification code</h1>
<p>We sent a code to j***@example.com.</p>
<form>
  <label>Verification code<input autocomplete="one-time-code" inputmode="numeric" maxlength="6"></label>
  <button type="button">Verify</button>
</form>
"""

_CAPTCHA_REQUIRED = """
<h1>Quick check</h1>
<p>Please verify you are human before continuing to the application.</p>
<div role="checkbox" aria-checked="false" style="width:24px;height:24px">I'm not a robot</div>
<button type="button">Continue</button>
"""

_SESSION_EXPIRED = """
<h1>Your session has expired</h1>
<p>For your security, this application session has timed out. Please sign in again to continue.</p>
<button type="button">Sign in</button>
"""

_UNKNOWN_WIDGET = """
<h1>Rate your proficiency</h1>
<div role="slider" aria-valuemin="1" aria-valuemax="5" aria-valuenow="3"
     aria-label="Python proficiency" style="width:200px;height:24px"></div>
<div data-widget="star-rating" aria-label="Communication rating" style="width:120px;height:24px">
  <span>&#9733;</span><span>&#9733;</span><span>&#9733;</span>
</div>
"""

_REVIEW_PAGE = """
<h1>Almost done</h1>
<p>Please review your application before submitting.</p>
<dl><dt>Name</dt><dd>Jane Doe</dd><dt>Email</dt><dd>jane@example.com</dd></dl>
<button type="button">Submit Application</button>
"""

_SUBMISSION_CONFIRMATION = """
<h1>Application received</h1>
<p>Thank you for your application. We have received your application and will be in touch.</p>
"""

#: (scenario, html, acceptable page types)
CORPUS: list[tuple[str, str, set[str]]] = [
    ("single_page_application", _SINGLE_PAGE_APPLICATION, {"APPLICATION_FORM"}),
    ("multi_step_application", _MULTI_STEP_APPLICATION, {"APPLICATION_FORM"}),
    ("dynamic_react_form", _DYNAMIC_REACT_FORM, {"APPLICATION_FORM"}),
    ("custom_dropdown", _CUSTOM_DROPDOWN, {"APPLICATION_FORM"}),
    ("native_dropdown", _NATIVE_DROPDOWN, {"APPLICATION_FORM"}),
    ("radio_group", _RADIO_GROUP, {"APPLICATION_FORM"}),
    ("checkbox_group", _CHECKBOX_GROUP, {"APPLICATION_FORM"}),
    ("conditional_question", _CONDITIONAL_QUESTION, {"APPLICATION_FORM"}),
    ("dynamic_field", _DYNAMIC_FIELD, {"APPLICATION_FORM"}),
    ("resume_upload", _RESUME_UPLOAD, {"APPLICATION_FORM"}),
    ("cover_letter_upload", _COVER_LETTER_UPLOAD, {"APPLICATION_FORM"}),
    ("validation_error", _VALIDATION_ERROR, {"APPLICATION_FORM"}),
    ("inline_error", _INLINE_ERROR, {"APPLICATION_FORM"}),
    ("modal_form", _MODAL_FORM, {"APPLICATION_FORM"}),
    ("embedded_application", _EMBEDDED_APPLICATION, {"APPLICATION_FORM"}),
    # Honest outcome for a form the observer structurally cannot see into —
    # see `test_iframe_application_degrades_safely` for the full contract.
    ("iframe_application", _IFRAME_APPLICATION, {"UNKNOWN"}),
    ("external_redirect", _EXTERNAL_REDIRECT, {"JOB_DETAILS"}),
    ("login_required", _LOGIN_REQUIRED, {"LOGIN"}),
    ("otp_required", _OTP_REQUIRED, {"OTP"}),
    ("captcha_required", _CAPTCHA_REQUIRED, {"CAPTCHA"}),
    ("session_expired", _SESSION_EXPIRED, {"LOGIN"}),
    # A control type the observer has never seen: not a form field, not a
    # link, not a button. UNKNOWN + low confidence is the correct, honest
    # answer; anything more specific would require guessing.
    ("unknown_widget", _UNKNOWN_WIDGET, {"UNKNOWN"}),
    ("review_page", _REVIEW_PAGE, {"REVIEW"}),
    ("submission_confirmation", _SUBMISSION_CONFIRMATION, {"SUBMISSION_CONFIRMATION"}),
]

SCENARIO_NAMES = [name for name, _, _ in CORPUS]


def _observe(page, html):
    page.set_content(html)
    return observe_page(page)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario,html,acceptable", CORPUS, ids=SCENARIO_NAMES)
def test_page_reaches_a_reasonable_classification(page, scenario, html, acceptable):
    state = _observe(page, html)
    result = classify_page(state)
    assert result.page_type in PAGE_TYPES, scenario
    assert result.page_type in acceptable, (
        f"{scenario}: classified {result.page_type!r}, expected one of {sorted(acceptable)} "
        f"(signals={result.signals})"
    )


@pytest.mark.parametrize("scenario,html,acceptable", CORPUS, ids=SCENARIO_NAMES)
def test_ambiguous_pages_report_low_confidence(page, scenario, html, acceptable):
    """An UNKNOWN classification must come with LOW confidence — "I don't
    know" has to read as "I don't know" to the loop, never as a confident
    call a caller could act on."""
    result = classify_page(_observe(page, html))
    if result.page_type == "UNKNOWN":
        assert result.confidence < 0.5, scenario
    else:
        assert result.confidence >= 0.5, scenario


@pytest.mark.parametrize("scenario,html,acceptable", CORPUS, ids=SCENARIO_NAMES)
def test_legacy_page_type_stays_in_its_own_vocabulary(page, scenario, html, acceptable):
    """`observer.classify_page_type` is the older, coarser classifier
    `page_classifier` composes. It must keep returning only its own documented
    strings for every page in the corpus (no crash, no new value)."""
    legacy = classify_page_type(_observe(page, html))
    assert legacy in {
        "JOB_LISTING", "application_page", "login", "verification",
        "captcha", "review", "confirmation", "unknown",
    }, f"{scenario}: unexpected legacy page type {legacy!r}"


# ---------------------------------------------------------------------------
# Grounding / action extraction must never crash, on any of them
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario,html,acceptable", CORPUS, ids=SCENARIO_NAMES)
def test_grounding_and_action_extraction_never_raise(page, scenario, html, acceptable):
    """Every downstream consumer of a `PageState` runs cleanly on every page
    in the corpus — including the ones whose right answer is "a human needs
    to look at this". A page the agent cannot handle must produce a
    classification, not an exception."""
    state = _observe(page, html)

    # Refs are the ONLY grounding handle the LLM ever gets, and they must be
    # unique and resolvable back to a real DOM node.
    refs = [element.ref for element in state.elements]
    assert len(refs) == len(set(refs)), scenario
    for element in state.elements:
        assert page.locator(f'[data-agent-ref="{element.ref}"]').count() >= 1, (
            f"{scenario}: ref {element.ref} does not resolve back to a DOM node"
        )
        # Stable identity + field classification + semantic action, all
        # best-effort and all non-raising.
        assert isinstance(field_identity(element), str)
        classification = classify_field(element)
        assert classification.coarse_category is None or isinstance(classification.coarse_category, str)
        assert isinstance(classification.sensitive, bool)
        assert element.semantic_action is None or isinstance(element.semantic_action, str)

    assert isinstance(unresolved_fields(state), list)
    completion = compute_page_completion(state)
    assert isinstance(completion.ready, bool)
    assert isinstance(completion.reason, str)
    assert isinstance(state.as_dict(), dict)
    assert isinstance(classify_page(state).as_dict(), dict)


# ---------------------------------------------------------------------------
# Shape-specific behaviour the classification assertions above don't cover
# ---------------------------------------------------------------------------


def test_dynamic_react_form_picks_up_fields_added_after_a_js_event(page):
    state = _observe(page, _DYNAMIC_REACT_FORM)
    assert not any("Certification" in e.name for e in state.elements)
    page.click("#addcert")
    page.click("#addcert")
    after = observe_page(page)
    added = [e.name for e in after.elements if "Certification" in e.name]
    assert added == ["Certification 1", "Certification 2"]
    assert classify_page(after).page_type == "APPLICATION_FORM"


def test_conditional_question_field_appears_only_after_the_triggering_answer(page):
    state = _observe(page, _CONDITIONAL_QUESTION)
    assert page.locator("#team").count() == 0
    assert not any("Which team" in e.name for e in state.elements)
    page.check("#prior-yes")
    after = observe_page(page)
    assert any("Which team did you work with?" in e.name for e in after.elements)


def test_dynamic_field_appears_after_the_page_settles(page):
    page.set_content(_DYNAMIC_FIELD)
    page.wait_for_selector("#startdate")
    state = observe_page(page)
    assert any(e.name == "Earliest start date" for e in state.elements)


def test_custom_dropdown_options_become_groundable_refs(page):
    """A div/li listbox is not a native <select>: its options only become
    actionable at all because the extraction script includes
    `[role="option"]`. Without that the agent would have a trigger it can
    click and nothing to click afterwards."""
    state = _observe(page, _CUSTOM_DROPDOWN)
    combobox = next(e for e in state.elements if e.role == "combobox")
    assert combobox.name == "Preferred start month"
    assert combobox.options is None  # not a native select
    options = [e for e in state.elements if e.type == "option"]
    assert [e.name for e in options] == ["January", "February", "March"]
    assert all(e.aria_selected is False for e in options)


def test_native_dropdown_exposes_its_options_inline(page):
    state = _observe(page, _NATIVE_DROPDOWN)
    select = next(e for e in state.elements if e.tag == "select")
    assert select.options == ["Select...", "Bachelors", "Masters"]
    assert select.selected_options == []


def test_radio_and_checkbox_groups_carry_their_group_name_and_question(page):
    radios = [e for e in _observe(page, _RADIO_GROUP).elements if e.type == "radio"]
    assert len(radios) == 2
    assert {e.group_name for e in radios} == {"reloc"}
    # The Yes/No labels are meaningless alone — the legend is what makes the
    # question answerable, and it must reach the element's context.
    assert all("willing to relocate" in e.surrounding_text.lower() for e in radios)

    boxes = [e for e in _observe(page, _CHECKBOX_GROUP).elements if e.type == "checkbox"]
    assert len(boxes) == 3
    assert {e.group_name for e in boxes} == {"lang"}


def test_validation_and_inline_errors_are_surfaced_not_swallowed(page):
    state = _observe(page, _VALIDATION_ERROR)
    assert any("Last Name is required" in m for m in state.validation_messages)
    invalid = [e for e in state.elements if e.invalid]
    assert invalid, "the re-rendered field marked aria-invalid was not reported as invalid"
    assert compute_page_completion(state).ready is False

    inline = _observe(page, _INLINE_ERROR)
    assert any("valid email address" in m for m in inline.validation_messages)
    assert any(e.invalid for e in inline.elements)


def test_modal_form_is_reported_as_a_dialog_and_still_classified_as_a_form(page):
    state = _observe(page, _MODAL_FORM)
    assert state.dialogs, "an application opened in a role=dialog must be reported as a dialog"
    assert classify_page(state).page_type == "APPLICATION_FORM"
    # A blocking dialog means the page is not "complete" — the agent must not
    # treat a modal as finished just because the fields behind it are.
    assert compute_page_completion(state).ready is False


def test_embedded_application_keeps_both_the_job_text_and_the_fields(page):
    state = _observe(page, _EMBEDDED_APPLICATION)
    assert "Responsibilities" in state.visible_text
    assert {"First Name", "Last Name"} <= {e.name for e in state.elements}
    assert classify_page(state).page_type == "APPLICATION_FORM"


def test_external_redirect_apply_link_is_captured_with_its_foreign_host(page):
    """Leaving for a different domain is a navigation decision the loop makes
    from the link's own href — the observer's job is to report it faithfully
    (and never to rewrite or hide it)."""
    state = _observe(page, _EXTERNAL_REDIRECT)
    apply_link = next(e for e in state.elements if e.tag == "a")
    assert apply_link.semantic_action == "APPLY"
    assert apply_link.href == "https://apply.another-host.example.org/postings/44812"
    assert "another-host.example.org" not in (state.url or "")


def test_unknown_widget_is_not_invented_into_a_field(page):
    """A custom slider / star-rating control is not in the extraction
    script's selector list. The correct behaviour is to report nothing for it
    (and classify the page UNKNOWN) rather than to fabricate a field the
    agent would then try to fill."""
    state = _observe(page, _UNKNOWN_WIDGET)
    assert [e for e in state.elements if e.tag in ("input", "select", "textarea")] == []
    result = classify_page(state)
    assert result.page_type == "UNKNOWN"
    assert result.confidence < 0.5


def test_blocker_pages_pause_before_the_llm_is_ever_consulted(page):
    """LOGIN / OTP / CAPTCHA / expired session must all be caught by the
    deterministic Layer 1/2 detector, i.e. `blocker_hint` is set — that is
    what makes `loop.py` request human intervention without spending an LLM
    call on the page."""
    expected = {
        "login_required": "LOGIN_REQUIRED",
        "otp_required": "OTP_REQUIRED",
        "captcha_required": "CAPTCHA_REQUIRED",
        "session_expired": "LOGIN_REQUIRED",
    }
    for scenario, html, _ in CORPUS:
        if scenario not in expected:
            continue
        state = _observe(page, html)
        assert state.blocker_hint is not None, scenario
        assert state.blocker_hint["request_type"] == expected[scenario], scenario


def test_no_blocker_is_invented_on_an_ordinary_application_page(page):
    """The other side of the gate: a plain application form must NOT trip the
    blocker detector, or every application would stall on a human request."""
    for scenario in ("single_page_application", "multi_step_application", "radio_group",
                     "checkbox_group", "native_dropdown", "resume_upload", "review_page"):
        html = next(h for name, h, _ in CORPUS if name == scenario)
        assert _observe(page, html).blocker_hint is None, scenario


# ---------------------------------------------------------------------------
# iframe: a real, documented structural limitation — the contract is that it
# degrades SAFELY, not that it works.
# ---------------------------------------------------------------------------


def test_iframe_application_degrades_safely(page):
    """KNOWN LIMITATION, asserted rather than hidden: `observer
    ._EXTRACT_ELEMENTS_JS` runs against the MAIN frame's `document` only and
    `document.querySelectorAll` does not descend into an `<iframe>`, so an
    application form rendered inside one is completely invisible. Crawling
    into frames would be a large new feature; what this test pins down is
    that the invisible case degrades honestly:

    * zero fields are reported (nothing is fabricated),
    * the page classifies as UNKNOWN with low confidence — never as a
      finished or empty page,
    * `content_frame_count` records that there IS unobserved content, and
    * `compute_page_completion` refuses to call the page complete, which is
      what stops `loop.py`'s pre-submit gate from offering an unfilled
      application up for approval.
    """
    state = _observe(page, _IFRAME_APPLICATION)

    assert state.elements == [], "fields inside an iframe must not be fabricated"
    assert state.content_frame_count == 1
    assert "First Name" not in (state.visible_text or "")

    result = classify_page(state)
    assert result.page_type == "UNKNOWN"
    assert result.confidence < 0.5
    assert result.signals.get("unobservable_embedded_frames") == 1

    completion = compute_page_completion(state)
    assert completion.ready is False, (
        "a page whose only content is an unobservable embedded frame must never "
        "report itself complete — that is how an unfilled application reaches "
        "the final-submission approval gate"
    )
    assert any("embedded frame" in message for message in completion.blocking_messages)


def test_tracking_pixel_iframes_do_not_count_as_unobservable_content(page):
    """The frame counter must not fire on analytics pixels, or every ordinary
    page would look like it was hiding content."""
    page.set_content(
        '<h1>Application received</h1>'
        '<p>Thank you for your application.</p>'
        '<iframe width="1" height="1" srcdoc="&lt;b&gt;x&lt;/b&gt;"></iframe>'
    )
    state = observe_page(page)
    assert state.content_frame_count == 0
    assert classify_page(state).page_type == "SUBMISSION_CONFIRMATION"


def test_an_iframe_alongside_real_fields_does_not_block_completion(page):
    """The completion refusal above is deliberately narrow: it only applies
    when there is NOTHING observable on the page. A page with real, filled
    fields plus an embedded video must still be able to complete."""
    page.set_content(
        '<h1>Apply</h1><form><label>First Name<input required value="Jane"></label></form>'
        '<iframe width="560" height="315" srcdoc="&lt;b&gt;video&lt;/b&gt;"></iframe>'
    )
    state = observe_page(page)
    assert state.content_frame_count == 1
    assert compute_page_completion(state).ready is True


# ---------------------------------------------------------------------------
# The hard, whole-file guarantee: no vendor branching, anywhere.
# ---------------------------------------------------------------------------

_AUTONOMOUS_DIR = Path(observer_module.__file__).parent
#: Names of real ATS vendors. None of these may appear in the production
#: modules that decide page type / field category / sensitivity.
_VENDOR_NAMES = (
    "greenhouse", "workday", "lever", "taleo", "icims", "smartrecruiters",
    "ashby", "bamboohr", "jobvite", "successfactors", "brassring", "myworkday",
    "recruitee", "teamtailor", "personio", "workable", "breezy", "jazzhr",
)
#: Modules whose CONTROL FLOW must be vendor-independent.
_VENDOR_FREE_MODULES = (
    page_classifier_module, question_ontology_module, observer_module, executor_module,
)


@pytest.mark.parametrize("module", _VENDOR_FREE_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_no_vendor_specific_branching_in_production_code(module):
    """A vendor name may appear in a comment explaining WHY something is
    generic (`observer.py` cites a real widget as motivation), but never in
    executable code — no `if platform == "..."`, no vendor string in a
    pattern table. Comment lines and docstring bodies are stripped before the
    check so the prose stays allowed and the code does not."""
    source = Path(module.__file__).read_text(encoding="utf-8")
    # Drop triple-quoted blocks (module/function docstrings) and # comments.
    source = re.sub(r'(?s)""".*?"""', "", source)
    source = re.sub(r"(?m)#.*$", "", source)
    lowered = source.lower()
    found = sorted({vendor for vendor in _VENDOR_NAMES if vendor in lowered})
    assert not found, f"{module.__name__} references ATS vendor(s) in executable code: {found}"


def test_detected_ats_platform_never_changes_the_classification(page):
    """`PageState.detected_ats_platform` exists as informational LLM prompt
    context. Setting it to any vendor — or to nonsense — must not move a
    single classification in the corpus."""
    for scenario, html, _ in CORPUS:
        baseline = classify_page(_observe(page, html))
        for vendor in ("greenhouse", "workday", "lever", "not-a-real-ats"):
            state = _observe(page, html)
            state.detected_ats_platform = vendor
            tagged = classify_page(state)
            assert tagged.page_type == baseline.page_type, f"{scenario} shifted under {vendor!r}"
            assert tagged.confidence == baseline.confidence, f"{scenario} shifted under {vendor!r}"


def test_loop_does_not_branch_on_the_detected_platform():
    """The autonomous loop itself must not read `detected_ats_platform` for
    control flow. It is allowed to pass it through into the LLM prompt."""
    source = Path(loop_module.__file__).read_text(encoding="utf-8")
    source = re.sub(r'(?s)""".*?"""', "", source)
    source = re.sub(r"(?m)#.*$", "", source)
    for line in source.splitlines():
        if "detected_ats_platform" in line:
            assert not line.strip().startswith(("if ", "elif ")), (
                f"loop.py branches on the detected ATS platform: {line.strip()!r}"
            )
