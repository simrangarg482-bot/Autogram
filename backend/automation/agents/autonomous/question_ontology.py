"""
Universal, portal-agnostic application-field ontology.

Maps a field's raw label/context to a normalized semantic concept, WITHOUT
depending on which ATS rendered it — "Mobile Number", "Phone", "Telephone",
and "Contact Number" all normalize to the same coarse category regardless of
vendor markup. Per the project's "don't build parallel systems" rule, this
does not re-implement the fine-grained EEO/logistics question classifier
that already exists and is already tested for the deterministic engine:
`automation/forms/question_classifier.py::classify_question`. That module
recognizes ~20 specific categories (`work_authorized`, `requires_sponsorship`,
`demographic_gender`, `years_of_experience`, ...) from full question
sentences. This module REUSES it as the fine-grained layer and adds:

1. A small set of additional keyword/regex matchers for the field KINDS that
   `question_classifier.py` was never asked to recognize (it targets
   screening QUESTIONS, not structural fields like "first name" or "resume
   upload").
2. A mapping from every fine-grained category (old or new) onto the spec's
   coarser, product-facing bucket vocabulary (§2).

CRITICAL — semantic similarity is a LABEL, never an AUTHORIZATION. Nothing in
this module decides whether a value may be auto-filled; that is
`truth_store.py`'s job (provenance + policy) and `executor.py`'s job
(sensitive-field gate, submission gate). A field classified as
`WORK_AUTHORIZATION` is exactly as unfillable-without-a-verified-source after
running through this module as before — classification only tells the loop
WHAT KIND of question this is, for logging, the question ledger, and for
building a better LLM prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from automation.forms.question_classifier import classify_question, is_demographic

#: The spec's closed, coarse vocabulary (§2). `ADDITIONAL_QUESTIONS` is the
#: deliberate catch-all for a field that is clearly an application question
#: but doesn't fit any more specific bucket — never `None` disguised as a
#: category, so a caller can always tell "classified as general" apart from
#: "not a question field at all" (`None`).
COARSE_CATEGORIES = frozenset({
    "PERSONAL_INFORMATION", "CONTACT_INFORMATION", "ADDRESS", "EDUCATION",
    "WORK_EXPERIENCE", "SKILLS", "RESUME", "COVER_LETTER", "LINKS",
    "WORK_AUTHORIZATION", "VISA", "DEMOGRAPHICS", "VOLUNTARY_DISCLOSURE",
    "LEGAL_DECLARATION", "ASSESSMENT", "REFERENCES", "ADDITIONAL_QUESTIONS",
    "REVIEW", "SUBMISSION",
})

#: Coarse categories that are NEVER eligible for silent inference regardless
#: of how confident the classifier is — the categories the spec calls out by
#: name (§5: "never infer sensitive or legally significant answers"). This is
#: a LABEL used by `truth_store.py`'s policy layer and by
#: `executor.py::is_sensitive_field_name`'s pattern set — kept as its own
#: frozenset here so a caller can ask "is this coarse category inherently
#: sensitive" without reaching into either of those.
SENSITIVE_COARSE_CATEGORIES = frozenset({
    "WORK_AUTHORIZATION", "VISA", "DEMOGRAPHICS", "VOLUNTARY_DISCLOSURE",
    "LEGAL_DECLARATION",
})

#: `question_classifier.py`'s fine-grained categories -> this module's coarse
#: bucket. Every category that module currently defines is listed explicitly
#: (never a fallback guess) so a new fine category added there without an
#: entry here fails loudly in tests (`test_question_ontology.py`) instead of
#: silently landing in the wrong bucket.
_FINE_TO_COARSE = {
    "requires_sponsorship": "WORK_AUTHORIZATION",
    "work_authorized": "WORK_AUTHORIZATION",
    "visa_type": "VISA",
    "notice_period_days": "ADDITIONAL_QUESTIONS",
    "expected_salary": "ADDITIONAL_QUESTIONS",
    "current_salary": "ADDITIONAL_QUESTIONS",
    "preferred_name": "PERSONAL_INFORMATION",
    "referral_source": "ADDITIONAL_QUESTIONS",
    "employment_type_preference": "ADDITIONAL_QUESTIONS",
    "willing_background_check": "VOLUNTARY_DISCLOSURE",
    "language_fluency": "SKILLS",
    "years_of_experience": "WORK_EXPERIENCE",
    "highest_education_level": "EDUCATION",
    "willing_to_relocate": "ADDITIONAL_QUESTIONS",
    "requires_relocation_assistance": "ADDITIONAL_QUESTIONS",
    "willing_to_travel": "ADDITIONAL_QUESTIONS",
    "willing_drug_test": "VOLUNTARY_DISCLOSURE",
    "has_drivers_license": "ADDITIONAL_QUESTIONS",
    "age_over_18": "LEGAL_DECLARATION",
    "security_clearance": "LEGAL_DECLARATION",
    "referrer_name": "ADDITIONAL_QUESTIONS",
    "time_zone": "ADDITIONAL_QUESTIONS",
    "demographic_gender": "DEMOGRAPHICS",
    "demographic_veteran_status": "DEMOGRAPHICS",
    "demographic_disability_status": "DEMOGRAPHICS",
    "demographic_race_ethnicity": "DEMOGRAPHICS",
    "demographic_pronouns": "DEMOGRAPHICS",
}

#: New structural-field matchers `question_classifier.py` was never asked to
#: cover (it classifies whole screening-question SENTENCES; these are field
#: KINDS keyed on label/name/type/section text). Most-specific-first, first
#: match wins. Deliberately vendor-agnostic: every phrase here is generic
#: form vocabulary, never an ATS-specific field name.
_STRUCTURAL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("RESUME", re.compile(r"r[ée]sum[ée]|\bcv\b|curriculum\s*vitae", re.IGNORECASE)),
    ("COVER_LETTER", re.compile(r"cover\s*letter", re.IGNORECASE)),
    ("LINKS", re.compile(
        r"linkedin|github|portfolio|personal\s*website|website\s*url|\bgit\s*repo|behance|dribbble",
        re.IGNORECASE,
    )),
    # CONTACT_INFORMATION is checked BEFORE ADDRESS on purpose: the single
    # most common label for an email field is "Email Address", and ADDRESS's
    # deliberately generic `\baddress\b` claimed it first, bucketing an email
    # field as a postal-address field (a bare "Email" landed correctly, so the
    # mis-bucketing depended entirely on the portal's wording). Nothing in the
    # CONTACT_INFORMATION pattern can claim a genuine postal-address label —
    # it only matches phone/cell/mobile/telephone/contact-number/e-mail — so
    # ordering it first is safe in the other direction.
    ("CONTACT_INFORMATION", re.compile(
        # Real portals label the same fact "Phone", "Mobile", "Cell Number",
        # "Telephone", "Contact Number"; and the same address field "Email",
        # "Email Address" or "E-mail". A bare "Mobile"/"E-mail" previously
        # matched nothing at all and fell through unclassified. The `mobile`
        # lookahead keeps a SKILLS question ("Mobile app development
        # experience") out of the contact bucket.
        r"\bphone\b|telephone|contact\s*number|e-?mail"
        r"|\bcell\s*(?:phone|number|no\b|#)"
        r"|\bmobile\b(?!\s*(?:app|application|development|developer|platform|device|engineer|game|web))",
        re.IGNORECASE,
    )),
    ("ADDRESS", re.compile(
        r"\baddress\b|\bstreet\b|\bcity\b|\bstate\b|\bprovince\b|\bzip\b|postal\s*code|\bcountry\b",
        re.IGNORECASE,
    )),
    ("EDUCATION", re.compile(
        # "Institution" and "Qualification" are as common as "University" and
        # "Degree" on non-US portals and previously matched nothing.
        r"\bdegree\b|\buniversity\b|\bcollege\b|\bschool\b|\bgpa\b|major|field\s*of\s*study|graduation"
        r"|\binstitution\b|\bqualification",
        re.IGNORECASE,
    )),
    ("WORK_EXPERIENCE", re.compile(
        r"employer|job\s*title|current\s*(company|role|position)|previous\s*(company|role)|"
        r"work\s*history|\bexperience\b(?!.{0,15}(sponsor|visa|authoriz))",
        re.IGNORECASE,
    )),
    ("SKILLS", re.compile(r"\bskills?\b|technical\s*proficienc|programming\s*languages?", re.IGNORECASE)),
    ("REFERENCES", re.compile(r"\breferences?\b|reference\s*contact|referee", re.IGNORECASE)),
    ("PERSONAL_INFORMATION", re.compile(
        # `given name`/`forename` and `surname`/`family name` are the ordinary
        # non-US spellings of first/last name and previously matched nothing.
        r"first\s*name|last\s*name|full\s*name|\bmiddle\s*name\b|preferred\s*name|\bpronouns?\b"
        r"|given\s*name|\bfore\s*name\b|\bforenames?\b|\bsur\s*name\b|\bsurnames?\b|family\s*name",
        re.IGNORECASE,
    )),
]

#: Sensitive / legally-significant field KINDS, checked BEFORE
#: `_STRUCTURAL_PATTERNS` above and only after `question_classifier.py`'s
#: full-sentence pass declined to classify the text.
#:
#: Why this exists: `executor.py::SENSITIVE_FIELD_PATTERNS` (the independent
#: code-level gate) already recognized these phrasings, but this module did
#: not — so a real-world label like "Country of Citizenship", "Sex", "Have
#: you ever been convicted of a felony?" or "I certify that the above is
#: true" came back `coarse_category=None, sensitive=False` here while the
#: executor gate correctly called it sensitive. The two layers are supposed
#: to agree (see `test_sensitive_field_corpus.py`); worse, "Country of
#: Citizenship" matched `_STRUCTURAL_PATTERNS`' generic `\bcountry\b` and was
#: bucketed as a plain, NON-sensitive `ADDRESS` field. Checking these first
#: closes both gaps. Over-matching here is the safe direction: the only
#: consequence of a false positive is a field left for a human.
_SENSITIVE_STRUCTURAL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("WORK_AUTHORIZATION", re.compile(
        r"work\s*authoriz|authoriz\w*\s*(?:to\s*work|for\s*employment)"
        r"|authoriz\w*.{0,20}\bwork\b|\bwork\b.{0,20}authoriz"
        r"|eligib\w*\s*(?:to\s*work|for\s*employment)|eligib\w*.{0,20}\bwork\b"
        r"|legally\s*(?:authorized|authorised|permitted|entitled|eligible)"
        r"|right\s*to\s*work|work\s*permit|employment\s*eligibility",
        re.IGNORECASE,
    )),
    ("VISA", re.compile(
        r"\bvisa\b|sponsor|citizenship|\bcitizens?\b|\bimmigration\b|\bnationalit"
        r"|permanent\s*resident|green\s*card|\bwork\s*status\b",
        re.IGNORECASE,
    )),
    ("DEMOGRAPHICS", re.compile(
        r"\brace\b|ethnicit|\bgender\b|\bsex\b|veteran|disabilit|\bpronouns?\b"
        r"|date\s*of\s*birth|\bdob\b|sexual\s*orientation|marital\s*status",
        re.IGNORECASE,
    )),
    ("LEGAL_DECLARATION", re.compile(
        r"criminal|felony|convict|background\s*(?:check|screening|verification)"
        r"|drug\s*(?:test|screen)|substance\s*screening|social\s*security|\bssn\b"
        r"|i\s*certify|i\s*declare|i\s*attest|i\s*acknowledge|i\s*agree|i\s*consent"
        r"|under\s*penalty|legally\s*binding|arbitrat|\bconsent\b|\battestation\b"
        r"|\bdeclaration\b|security\s*clearance|\bai\s*(?:use\s*)?policy\b",
        re.IGNORECASE,
    )),
]


@dataclass(frozen=True)
class QuestionClassification:
    #: One of `COARSE_CATEGORIES`, or `None` when nothing here recognized the
    #: field at all (never guessed — `None` is the honest "unclassified", not
    #: a silently-wrong bucket).
    coarse_category: str | None
    #: The finer category from `question_classifier.py`, when a full-sentence
    #: match fired; `None` for a purely structural match or no match.
    fine_category: str | None
    #: True when this field's coarse category is inherently sensitive
    #: (`SENSITIVE_COARSE_CATEGORIES`) OR `question_classifier.is_demographic`
    #: flags the fine category — i.e. "never infer this, ever" (spec §5), as
    #: distinct from "merely unclassified".
    sensitive: bool


def _question_text(element) -> str:
    """The best available text describing what this field is asking —
    accessible name first (what a screen reader would announce), falling
    back to nearby form context (`surrounding_text`) for a field whose own
    name is generic (a bare "Yes"/"No" radio pair, for instance, is only
    meaningful together with its section's question text)."""
    parts = [element.name or "", getattr(element, "aria_label", "") or ""]
    surrounding = getattr(element, "surrounding_text", "") or ""
    section = getattr(element, "section", "") or ""
    return " ".join([*parts, section, surrounding]).strip()


def classify_field(element) -> QuestionClassification:
    """Classifies one `PageElement` (see `observer.py`). Never raises —
    worst case is `QuestionClassification(None, None, False)`, meaning
    "could not confidently classify this field," which callers treat as
    "ask the LLM / a human," never as license to guess."""
    text = _question_text(element)
    if not text.strip():
        return QuestionClassification(None, None, False)

    fine = classify_question(text)
    if fine is not None:
        coarse = _FINE_TO_COARSE.get(fine)
        if coarse is None:
            # A fine category `question_classifier.py` added without a
            # mapping entry above — surfaced as ADDITIONAL_QUESTIONS (never
            # silently dropped) so it's still a legitimate, logged bucket
            # while `test_question_ontology.py::test_every_fine_category_mapped`
            # catches the gap for a human to fix properly.
            coarse = "ADDITIONAL_QUESTIONS"
        return QuestionClassification(coarse, fine, is_demographic(fine) or coarse in SENSITIVE_COARSE_CATEGORIES)

    # Sensitive/legally-significant kinds FIRST — see
    # `_SENSITIVE_STRUCTURAL_PATTERNS`. A label like "Country of Citizenship"
    # must never be claimed by the generic ADDRESS matcher below.
    for coarse, pattern in _SENSITIVE_STRUCTURAL_PATTERNS:
        if pattern.search(text):
            return QuestionClassification(coarse, None, True)

    for coarse, pattern in _STRUCTURAL_PATTERNS:
        if pattern.search(text):
            return QuestionClassification(coarse, None, coarse in SENSITIVE_COARSE_CATEGORIES)

    return QuestionClassification(None, None, False)
