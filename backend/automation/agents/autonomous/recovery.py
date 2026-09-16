"""
Recovery engine — classifies WHY an action or observation didn't produce the
expected effect, and decides what to do about it, so `loop.py` never has one
undifferentiated "something went wrong, ask a human" fallback for every
failure. Every classification here is generic/portal-agnostic (spec §11):
none of it depends on which ATS rendered the page.

This does not replace any existing safety mechanism — `executor.py`'s
sensitive-field/submission gates, `actions.py::validate_action_grounding`,
and `observer.py::detect_blocker` all run exactly as before. This module
only interprets THEIR outputs (an `ActionResult`, a `PageError`, a blocker
hint, a same-page streak) into one clear next step, so `loop.py`'s handling
of "the action didn't work" stops being ad hoc per call site.

Two firm invariants (spec §11's closing line): recovery NEVER attempts to
bypass a CAPTCHA or other anti-bot/human-verification mechanism, and it
NEVER retries a mutating action silently forever — every `RecoveryDecision`
either retries a BOUNDED number of times, re-observes, or escalates to a
human/failure.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Spec §11's closed failure taxonomy.
FAILURE_CODES = frozenset({
    "ELEMENT_DISAPPEARED", "VALIDATION_ERROR", "NETWORK_ERROR", "SESSION_EXPIRED",
    "LOGIN_REQUIRED", "OTP_REQUIRED", "CAPTCHA_REQUIRED", "UPLOAD_FAILURE",
    "UNEXPECTED_PAGE", "UNKNOWN_PAGE", "NO_PROGRESS", "STALE_REFERENCE",
    "RATE_LIMIT", "APPLICATION_ERROR",
})

#: What kind of response a `RecoveryDecision` prescribes.
RECOVERY_ACTIONS = frozenset({"RETRY", "REOBSERVE", "HUMAN", "ESCALATE"})

#: How many times `RETRY` is offered for the same failure code on the same
#: field/action before escalating — deliberately small and independent of
#: `loop.py::MAX_FIELD_ATTEMPTS` (which counts real dispatch attempts across
#: ANY failure reason); this is "how many times is THIS SPECIFIC transient
#: reason worth retrying before it's clearly not transient."
_MAX_RETRIES_BY_CODE = {
    "NETWORK_ERROR": 3,
    "RATE_LIMIT": 3,
    "ELEMENT_DISAPPEARED": 2,
    "STALE_REFERENCE": 1,  # a stale ref is corrected by re-observing, not retried as-is
}

#: Failure codes that map straight to a human-intervention request type
#: (`app.models.db_models.VALID_HUMAN_REQUEST_TYPES`) — never attempted,
#: retried, or worked around automatically.
_HUMAN_REQUIRED_CODES = {
    "SESSION_EXPIRED": "LOGIN_REQUIRED",
    "LOGIN_REQUIRED": "LOGIN_REQUIRED",
    "OTP_REQUIRED": "OTP_REQUIRED",
    "CAPTCHA_REQUIRED": "CAPTCHA_REQUIRED",
    "UNKNOWN_PAGE": "UNKNOWN_BLOCKER",
}


@dataclass(frozen=True)
class RecoveryDecision:
    action: str  # one of RECOVERY_ACTIONS
    reason: str
    request_type: str | None = None  # set only when action == "HUMAN"

    def as_dict(self) -> dict:
        return {"action": self.action, "reason": self.reason, "request_type": self.request_type}


def classify_failure(
    *,
    blocked_reason: str | None = None,
    blocker_request_type: str | None = None,
    page_error_code: str | None = None,
    action_succeeded: bool = True,
    action_verified: bool = True,
    element_missing: bool = False,
    stale_reference: bool = False,
    upload_failed: bool = False,
    validation_messages: list[str] | None = None,
    exception_message: str | None = None,
    no_progress_streak: int = 0,
    no_progress_ceiling: int = 5,
) -> str | None:
    """Classifies the CURRENT situation into one `FAILURE_CODES` member, or
    `None` when nothing here indicates a failure at all (the ordinary,
    successful case). Takes plain scalars/flags rather than the live
    `ActionResult`/`PageState` objects directly so it stays trivially unit
    testable and reusable from more than one call site in `loop.py`.

    Checked most-specific/most-actionable first: a deterministic blocker
    (OTP/CAPTCHA/login) always wins over a generic "nothing happened," and a
    stale reference is distinguished from "the element just isn't there
    anymore" even though both often look similar to a caller."""
    if blocker_request_type:
        mapped = {
            "OTP_REQUIRED": "OTP_REQUIRED", "MFA_REQUIRED": "OTP_REQUIRED",
            "CAPTCHA_REQUIRED": "CAPTCHA_REQUIRED", "LOGIN_REQUIRED": "LOGIN_REQUIRED",
        }.get(blocker_request_type)
        if mapped:
            return mapped

    if page_error_code:
        return "APPLICATION_ERROR"

    if stale_reference:
        return "STALE_REFERENCE"

    if element_missing:
        return "ELEMENT_DISAPPEARED"

    if upload_failed:
        return "UPLOAD_FAILURE"

    text = (exception_message or "").lower()
    if any(hint in text for hint in ("timeout", "net::err", "econnreset", "connection", "dns")):
        return "NETWORK_ERROR"
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return "RATE_LIMIT"
    if "session" in text and ("expired" in text or "invalid" in text):
        return "SESSION_EXPIRED"

    if validation_messages:
        return "VALIDATION_ERROR"

    if no_progress_streak >= no_progress_ceiling:
        return "NO_PROGRESS"

    if not action_succeeded or not action_verified:
        # A generic dispatch/verification failure that didn't match any of
        # the more specific signals above — real, but not yet classifiable
        # further. Treated as a validation error's cheaper cousin: worth one
        # re-observe before anything stronger.
        return "UNEXPECTED_PAGE"

    return None


def recovery_policy(code: str | None, *, attempts_so_far: int = 0) -> RecoveryDecision | None:
    """Maps a failure code to what should happen next. Returns `None` when
    `code` is `None` (nothing to recover from). Never returns an action that
    would bypass a CAPTCHA/anti-bot check or retry indefinitely — every
    `RETRY` is bounded by `_MAX_RETRIES_BY_CODE`, past which this escalates."""
    if code is None:
        return None

    if code not in FAILURE_CODES:
        return RecoveryDecision("HUMAN", f"unrecognized failure code {code!r}", "UNKNOWN_BLOCKER")

    if code in _HUMAN_REQUIRED_CODES:
        return RecoveryDecision(
            "HUMAN",
            f"{code} requires a human — never bypassed automatically",
            _HUMAN_REQUIRED_CODES[code],
        )

    if code == "VALIDATION_ERROR":
        return RecoveryDecision("REOBSERVE", "inspect validation message and correct the supported field")

    if code == "STALE_REFERENCE":
        return RecoveryDecision("REOBSERVE", "element reference no longer matches the live page — re-observe before deciding again")

    if code == "NO_PROGRESS":
        return RecoveryDecision("ESCALATE", "page has not changed across repeated attempts — stop and escalate")

    max_retries = _MAX_RETRIES_BY_CODE.get(code, 1)
    if attempts_so_far < max_retries:
        return RecoveryDecision("RETRY", f"{code}: transient, retrying ({attempts_so_far + 1}/{max_retries})")

    if code == "APPLICATION_ERROR":
        return RecoveryDecision("HUMAN", "destination page is an error page and retries were exhausted", "UNKNOWN_BLOCKER")
    if code == "UPLOAD_FAILURE":
        return RecoveryDecision("HUMAN", "file upload failed after retrying", "FILE_UPLOAD_REQUIRED")
    if code == "ELEMENT_DISAPPEARED":
        return RecoveryDecision("REOBSERVE", "element disappeared; re-observe rather than keep retrying a stale target")

    return RecoveryDecision("ESCALATE", f"{code}: retries exhausted")
