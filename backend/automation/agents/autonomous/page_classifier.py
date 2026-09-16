"""
Universal, portal-agnostic page classification for the autonomous agent.

This module answers ONE question every iteration needs answered before the
LLM ever sees the page: "what KIND of page is this, generically?" — never
"which ATS vendor is this?". It deliberately never returns a vendor name
(Greenhouse/Workday/Lever/...) as a classification outcome; `PageState
.detected_ats_platform` (populated upstream from `automation/ats/detector.py`)
stays exactly what it already was — optional, informational LLM-prompt
context — and is never read here to change the result.

Per the project's "don't build parallel systems" rule, this does NOT
re-implement page-purpose detection from scratch. It composes signals that
already exist and are already independently tested:

- `observer.py::classify_page_type` — the existing coarse classifier
  (JOB_LISTING / application_page / login / verification / captcha / review
  / confirmation / unknown), built from `PageState.blocker_hint` and simple
  text/structure heuristics. Kept UNCHANGED and still called by anything
  that depended on its exact string outputs (e.g. any existing test) —
  this module only maps its result into namely a richer, additive vocabulary.
- `observer.py::CONFIRMATION_PHRASES` and
  `automation/browser/selectors.py::REVIEW_PAGE_TEXT_PATTERNS` — the same
  phrase lists `classify_page_type` already uses, reused directly rather than
  copied, so the two never drift apart.
- `PageState.blocker_hint` — the deterministic Layer 1/2 OTP/MFA/CAPTCHA/
  login detector (`observer.py::detect_blocker`), already computed on every
  observation.
- `page_validation.py::detect_error_page` — the generic, ATS-agnostic
  error-page detector. It needs a live Playwright `Page` (for HTTP status),
  so `loop.py` calls it separately and passes the result in here as `error`.

What's new here is the wider, PRODUCT vocabulary the spec calls for
(§1) — `ACCOUNT_CREATION`, `COOKIE_CONSENT`, `ERROR`, `ASSESSMENT`, and a
split between `REVIEW` and `SUBMISSION_CONFIRMATION` — plus a structured,
stable `PageClassification` result (page_type, application_stage,
confidence) instead of a bare string, and a best-effort `application_stage`
label (which section of a multi-step application this looks like) derived
from `question_ontology.py`'s coarse categories over the page's own
unresolved fields — never from a page-count assumption (see `page_graph.py`
for why navigation is treated as a graph, not a fixed sequence).

Deliberately entirely deterministic (regex/keyword/structural signals only)
— no LLM call. Every iteration of the loop needs this, and spending an LLM
call on "what kind of page is this" before the one call that actually
decides the next action would double the per-iteration LLM cost budget
(see `budgets.py::AgentBudgets.max_llm_calls`) for a question cheap
deterministic signals already answer well. `decision.py` still gets the
final say for any page these heuristics can't confidently place — they
report `UNKNOWN` with low confidence rather than guessing, and the LLM
decision step reasons over the raw `PageState` exactly as it already did.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from automation.agents.autonomous.observer import (
    CONFIRMATION_PHRASES,
    PageState,
    classify_page_type,
)
from automation.agents.autonomous.page_validation import PageError
from automation.agents.autonomous.question_ontology import (
    COARSE_CATEGORIES,
    classify_field,
)
from automation.browser.selectors import REVIEW_PAGE_TEXT_PATTERNS

#: The spec's closed, platform-independent page-purpose vocabulary. Never a
#: vendor/ATS name — see module docstring.
PAGE_TYPES = frozenset({
    "JOB_DETAILS", "APPLICATION_FORM", "LOGIN", "ACCOUNT_CREATION", "OTP",
    "CAPTCHA", "COOKIE_CONSENT", "ERROR", "ASSESSMENT", "REVIEW",
    "SUBMISSION_CONFIRMATION", "UNKNOWN",
})

#: `PageState.blocker_hint["request_type"]` (see `observer.py::detect_blocker`,
#: `app.models.db_models.VALID_HUMAN_REQUEST_TYPES`) mapped to a page type.
#: `MFA_REQUIRED` folds into `OTP` — both are "a code/second-factor is being
#: asked for right now"; the finer distinction still reaches the LLM via the
#: unmodified `blocker_hint` dict itself, this is only the coarse page label.
_BLOCKER_TO_PAGE_TYPE = {
    "OTP_REQUIRED": "OTP",
    "MFA_REQUIRED": "OTP",
    "CAPTCHA_REQUIRED": "CAPTCHA",
    "LOGIN_REQUIRED": "LOGIN",
}

#: Phrases that mark a cookie/consent banner. Deliberately narrow and
#: high-precision (a job description that happens to mention "privacy
#: policy" must not be classified as a consent screen) — checked only
#: against dialog text and short heading text, never the whole page body.
_COOKIE_CONSENT_PHRASES = (
    "we use cookies", "this site uses cookies", "accept cookies",
    "cookie preferences", "manage cookies", "cookie policy", "accept all cookies",
)

#: Account-creation phrasing, distinct from a plain login form: the presence
#: of a *second* password field (confirm password) or explicit "create
#: account"/"sign up" language, as opposed to "sign in"/"log in".
_ACCOUNT_CREATION_PHRASES = (
    "create an account", "create account", "sign up", "register an account",
    "set a password", "choose a password", "confirm password", "create your profile",
)
_LOGIN_ONLY_PHRASES = ("sign in", "log in", "login", "welcome back")

#: Timed-assessment / coding-challenge phrasing — deliberately narrow so an
#: ordinary "years of experience" question never trips this.
_ASSESSMENT_PHRASES = (
    "coding challenge", "coding assessment", "online assessment", "skills assessment",
    "timed test", "you have 30 minutes", "you have 60 minutes", "hackerrank", "codility",
    "start the test", "begin assessment", "test will begin",
)


@dataclass(frozen=True)
class PageClassification:
    page_type: str
    application_stage: str | None = None
    confidence: float = 0.5
    #: Non-authoritative debug signals — never read by any safety gate, only
    #: for the audit trail / page graph (`page_graph.py`) and for tests.
    signals: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "page_type": self.page_type,
            "application_stage": self.application_stage,
            "confidence": self.confidence,
        }


def _dialog_or_heading_text(page_state: PageState) -> str:
    return " ".join([*(page_state.dialogs or []), *(page_state.headings or [])]).lower()


def _guess_application_stage(page_state: PageState) -> str | None:
    """Best-effort label for which part of the application this looks like —
    derived purely from the coarse categories `question_ontology.py` assigns
    to the fields actually visible right now, never from a hardcoded page
    number/order (see `page_graph.py` module docstring on why navigation is a
    graph, not a fixed sequence). Returns `None` when there aren't enough
    classifiable fields to say anything useful (e.g. a page with zero visible
    inputs)."""
    counts: dict[str, int] = {}
    for element in page_state.elements:
        if element.disabled or element.type in ("hidden", "submit", "button", "reset"):
            continue
        classification = classify_field(element)
        if classification.coarse_category in (None, "ADDITIONAL_QUESTIONS"):
            continue
        counts[classification.coarse_category] = counts.get(classification.coarse_category, 0) + 1
    if not counts:
        return None
    winner = max(counts.items(), key=lambda item: item[1])[0]
    return winner.lower()


def classify_page(page_state: PageState, *, error: PageError | None = None) -> PageClassification:
    """Returns a structured, platform-independent classification of the
    CURRENT page. Order of precedence (strongest/cheapest signal first):

    1. An explicit page-load error (`error`, from `page_validation
       .detect_error_page` — HTTP status, error-page URL/title/content).
    2. The deterministic blocker detector (`PageState.blocker_hint`) — OTP,
       MFA, CAPTCHA, or a login wall.
    3. Cookie/consent banner text.
    4. Account-creation vs. plain sign-in phrasing.
    5. Assessment/coding-challenge phrasing.
    6. Submission-confirmation phrasing (`CONFIRMATION_PHRASES`).
    7. Review-page phrasing (`REVIEW_PAGE_TEXT_PATTERNS`).
    8. Falls through to the existing `observer.classify_page_type` result
       for JOB_DETAILS ("JOB_LISTING") / APPLICATION_FORM ("application_page")
       / UNKNOWN.

    Confidence is coarse and intentionally conservative (never claims
    near-certainty from text heuristics alone) — it exists so a caller (or a
    future LLM-assisted disambiguation pass) can tell "confidently placed" from
    "best guess, treat cautiously," not to drive any safety decision on its own.
    """
    signals: dict = {}

    # Non-authoritative, but recorded on EVERY result: a page whose fields
    # live inside an `<iframe>` observes as zero elements (the extraction
    # script never leaves the main frame — see
    # `observer.PageState.content_frame_count`). Surfacing the count keeps
    # "genuinely empty page" distinguishable from "content we cannot see" in
    # the audit trail and page graph, instead of the classifier's UNKNOWN
    # looking like an ordinary ambiguous page.
    unobservable = getattr(page_state, "content_frame_count", 0) or 0
    if unobservable and not page_state.elements:
        signals["unobservable_embedded_frames"] = unobservable

    if error is not None:
        signals["error"] = error.detail
        return PageClassification("ERROR", None, 0.9, signals)

    blocker = page_state.blocker_hint or {}
    blocker_page_type = _BLOCKER_TO_PAGE_TYPE.get(blocker.get("request_type"))
    if blocker_page_type:
        signals["blocker_hint"] = blocker.get("request_type")
        return PageClassification(blocker_page_type, None, 0.95, signals)

    dialog_heading_text = _dialog_or_heading_text(page_state)
    haystack = f"{page_state.title} {page_state.visible_text}".lower()

    if any(phrase in dialog_heading_text for phrase in _COOKIE_CONSENT_PHRASES) or \
            any(phrase in haystack[:600] for phrase in _COOKIE_CONSENT_PHRASES):
        signals["matched"] = "cookie_consent_phrase"
        return PageClassification("COOKIE_CONSENT", None, 0.8, signals)

    has_password_field = any(
        (element.type == "password") for element in page_state.elements
    )
    password_field_count = sum(1 for element in page_state.elements if element.type == "password")
    if any(phrase in haystack for phrase in _ACCOUNT_CREATION_PHRASES) or password_field_count >= 2:
        signals["matched"] = "account_creation_phrase_or_two_password_fields"
        return PageClassification("ACCOUNT_CREATION", None, 0.75, signals)
    if has_password_field and any(phrase in haystack for phrase in _LOGIN_ONLY_PHRASES):
        signals["matched"] = "login_phrase"
        return PageClassification("LOGIN", None, 0.7, signals)

    if any(phrase in haystack for phrase in _ASSESSMENT_PHRASES):
        signals["matched"] = "assessment_phrase"
        return PageClassification("ASSESSMENT", None, 0.7, signals)

    if any(phrase in haystack for phrase in CONFIRMATION_PHRASES):
        signals["matched"] = "confirmation_phrase"
        return PageClassification("SUBMISSION_CONFIRMATION", None, 0.85, signals)

    if any(phrase in haystack for phrase in REVIEW_PAGE_TEXT_PATTERNS):
        signals["matched"] = "review_phrase"
        return PageClassification("REVIEW", _guess_application_stage(page_state), 0.7, signals)

    legacy = classify_page_type(page_state)
    signals["legacy_classify_page_type"] = legacy
    if legacy == "JOB_LISTING":
        return PageClassification("JOB_DETAILS", None, 0.7, signals)
    if legacy == "application_page":
        return PageClassification("APPLICATION_FORM", _guess_application_stage(page_state), 0.6, signals)
    if legacy == "confirmation":
        return PageClassification("SUBMISSION_CONFIRMATION", None, 0.7, signals)
    if legacy == "review":
        return PageClassification("REVIEW", _guess_application_stage(page_state), 0.6, signals)

    return PageClassification("UNKNOWN", None, 0.3, signals)
