"""Unit tests for `automation.agents.autonomous.page_classifier` — purely
deterministic, no Playwright/DB needed. Builds `PageState`/`PageElement`
objects by hand rather than rendering real HTML, mirroring the style of
`test_field_coverage.py`."""

from automation.agents.autonomous.observer import PageElement, PageState
from automation.agents.autonomous.page_classifier import classify_page
from automation.agents.autonomous.page_validation import PageError


def _element(**kwargs) -> PageElement:
    defaults = dict(
        ref=0, tag="input", type="text", name="", value=None, required=False,
        disabled=False, checked=None, options=None,
    )
    defaults.update(kwargs)
    return PageElement(**defaults)


def _page_state(**kwargs) -> PageState:
    defaults = dict(url="https://careers.example.com/job/123", title="", visible_text="", elements=[])
    defaults.update(kwargs)
    return PageState(**defaults)


def test_error_page_wins_over_everything_else():
    page_state = _page_state(title="Apply now", visible_text="job description")
    result = classify_page(page_state, error=PageError("HTTP_ERROR", "destination returned HTTP 500", 500))
    assert result.page_type == "ERROR"
    assert result.confidence >= 0.8


def test_blocker_hint_maps_to_otp():
    page_state = _page_state(blocker_hint={"request_type": "OTP_REQUIRED"})
    result = classify_page(page_state)
    assert result.page_type == "OTP"


def test_blocker_hint_maps_to_captcha():
    page_state = _page_state(blocker_hint={"request_type": "CAPTCHA_REQUIRED"})
    assert classify_page(page_state).page_type == "CAPTCHA"


def test_cookie_consent_banner():
    page_state = _page_state(dialogs=["We use cookies to improve your experience. Accept cookies?"])
    assert classify_page(page_state).page_type == "COOKIE_CONSENT"


def test_account_creation_two_password_fields():
    page_state = _page_state(elements=[
        _element(ref=0, type="password", name="Password"),
        _element(ref=1, type="password", name="Confirm Password"),
    ])
    assert classify_page(page_state).page_type == "ACCOUNT_CREATION"


def test_account_creation_phrase():
    page_state = _page_state(title="Create your account", visible_text="Sign up to apply for this role")
    assert classify_page(page_state).page_type == "ACCOUNT_CREATION"


def test_assessment_phrase():
    page_state = _page_state(visible_text="This coding challenge will take approximately 45 minutes.")
    assert classify_page(page_state).page_type == "ASSESSMENT"


def test_submission_confirmation_phrase():
    page_state = _page_state(visible_text="Thank you for your application. We have received it.")
    assert classify_page(page_state).page_type == "SUBMISSION_CONFIRMATION"


def test_review_phrase(monkeypatch):
    import automation.agents.autonomous.page_classifier as module
    monkeypatch.setattr(module, "REVIEW_PAGE_TEXT_PATTERNS", ["review your application"])
    page_state = _page_state(visible_text="Please review your application before submitting.")
    assert classify_page(page_state).page_type == "REVIEW"


def test_job_listing_falls_back_to_job_details():
    page_state = _page_state(
        title="Senior Backend Engineer",
        visible_text="About the role. Responsibilities. Requirements. Qualifications.",
        elements=[_element(ref=0, tag="button", type="button", name="Apply Now", semantic_action="APPLY", action_confidence="HIGH")],
    )
    result = classify_page(page_state)
    assert result.page_type == "JOB_DETAILS"


def test_unknown_when_nothing_matches():
    page_state = _page_state(title="", visible_text="")
    result = classify_page(page_state)
    assert result.page_type == "UNKNOWN"
    assert result.confidence < 0.5


def test_never_returns_an_ats_vendor_name():
    """Hard guarantee from the spec: the classifier's vocabulary never
    contains a vendor name, however the input is shaped."""
    from automation.agents.autonomous.page_classifier import PAGE_TYPES
    forbidden = {"greenhouse", "workday", "lever", "taleo", "icims", "smartrecruiters", "ashby", "bamboohr", "oracle_hcm"}
    assert not (forbidden & {p.lower() for p in PAGE_TYPES})
