"""
Verified-user-truth abstraction + the "can answer / should auto-fill" policy.

Every value the autonomous agent ever writes into a field must be traceable
to WHERE it came from (provenance) and whether that source is one the system
trusts to fill a field without asking a human. This module is the single
place that decision is made — `decision.py` builds LLM context from it,
`loop.py` consults it before telling `executor.py` a value `is_sourced`, and
`question_ledger.py` records its verdict per field so "why did Autogram enter
this answer" (spec §4) is always answerable after the fact.

This does not replace any existing data source. It is a read-only VIEW over
three things the loop already has on hand every iteration:

- the candidate's verified profile snapshot (`AutonomousTask.candidate_profile`
  — see `automation/interfaces.py::get_candidate_profile_dict`),
  `parsed_resume`/`resume_text` (from `app.services.resume_repository`
  et al.), which this module treats as `USER_PROFILE`/`RESUME` provenance;
- `AutonomousTask.confirmed_answers` — per-task human-supplied answers
  (`USER_CONFIRMED`);
- an optional cross-application memory lookup (`APPROVED_ANSWER_MEMORY` —
  see `app/services/answer_cache_repository.py`, already used by the
  deterministic engine's `ApplicationAnswerEngine`; wiring this into the
  autonomous loop is additive and OFF by default — see `TruthStore
  .from_task_context`'s `answer_memory_lookup` parameter).

Nothing here calls an LLM and nothing here ever fabricates a value: a field
this module cannot resolve returns `None` from `resolve()`, full stop — the
loop's only options for such a field are asking the LLM to answer it as
`LLM_GENERATED` (marked for review, never silently treated as a fact — spec
§3) or requesting human input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from automation.agents.autonomous.question_ontology import SENSITIVE_COARSE_CATEGORIES

#: Closed provenance vocabulary (spec §3).
PROVENANCE_VALUES = frozenset({
    "USER_PROFILE", "RESUME", "USER_CONFIRMED", "APPROVED_ANSWER_MEMORY",
    "DETERMINISTIC_DERIVATION", "LLM_GENERATED", "UNKNOWN",
})

#: Provenance categories the policy below treats as "this came from the user
#: themselves or from something the user already reviewed and approved" —
#: eligible for automatic filling of a NON-sensitive field per spec §3's
#: table. `LLM_GENERATED` and `UNKNOWN` are deliberately excluded here; they
#: are handled by their own explicit branches in `is_autofill_eligible`.
_VERIFIED_PROVENANCE = frozenset({
    "USER_PROFILE", "RESUME", "USER_CONFIRMED", "APPROVED_ANSWER_MEMORY",
    "DETERMINISTIC_DERIVATION",
})


@dataclass(frozen=True)
class VerifiedFact:
    """One resolved value plus where it came from. `verified` is
    ALWAYS true for `USER_PROFILE`/`RESUME`/`USER_CONFIRMED`/
    `APPROVED_ANSWER_MEMORY`/`DETERMINISTIC_DERIVATION` (they are truthful by
    construction — see `TruthStore.resolve`) and always false for
    `LLM_GENERATED`/`UNKNOWN`. It is kept as an explicit field rather than
    inferred solely from `source` so a caller reading a serialized fact
    (e.g. from the question ledger) doesn't need this module's provenance
    list memorized to answer "was this actually verified?"."""

    value: str
    source: str  # one of PROVENANCE_VALUES
    verified: bool

    def as_dict(self) -> dict:
        return {"value": self.value, "source": self.source, "verified": self.verified}


@dataclass(frozen=True)
class AnswerDecision:
    """The full §5 reasoning chain's outcome for one field, in order:
    can_answer -> is_verified -> autofill_permitted -> requires_human.
    `fact` is the resolved value backing this decision (`None` if nothing
    could answer it at all)."""

    can_answer: bool
    fact: VerifiedFact | None
    autofill_permitted: bool
    requires_human: bool
    reason: str

    def as_dict(self) -> dict:
        return {
            "can_answer": self.can_answer,
            "fact": self.fact.as_dict() if self.fact else None,
            "autofill_permitted": self.autofill_permitted,
            "requires_human": self.requires_human,
            "reason": self.reason,
        }


def _normalize_key(text: str) -> str:
    return " ".join((text or "").split()).strip().lower()


class TruthStore:
    """A read-only lookup over the verified facts available for ONE task.
    Keys are normalized field labels/aliases (case/whitespace-insensitive) —
    callers are expected to try a handful of plausible aliases for a field
    (see `resolve_many_aliases`) rather than needing an exact canonical key,
    since the same fact is asked about under many different labels across
    portals ("Phone" vs "Mobile Number" vs "Contact Number")."""

    def __init__(
        self,
        *,
        profile_facts: dict[str, str] | None = None,
        resume_facts: dict[str, str] | None = None,
        confirmed_answers: dict[str, str] | None = None,
        answer_memory: dict[str, str] | None = None,
    ) -> None:
        self._profile = {_normalize_key(k): v for k, v in (profile_facts or {}).items() if v not in (None, "")}
        self._resume = {_normalize_key(k): v for k, v in (resume_facts or {}).items() if v not in (None, "")}
        self._confirmed = {_normalize_key(k): v for k, v in (confirmed_answers or {}).items() if v not in (None, "")}
        self._memory = {_normalize_key(k): v for k, v in (answer_memory or {}).items() if v not in (None, "")}

    @classmethod
    def from_task_context(
        cls,
        *,
        candidate_profile: dict | None,
        parsed_resume: dict | None,
        confirmed_answers: dict | None,
        answer_memory_lookup: Callable[[], dict[str, str]] | None = None,
    ) -> "TruthStore":
        """Builds a store from the plain dicts `loop.py` already carries per
        iteration (`AutonomousTask.candidate_profile` / `.confirmed_answers`,
        and the task's parsed resume). `answer_memory_lookup` is an OPTIONAL
        callable (kept lazy — a callable, not a pre-fetched dict — so a
        caller that never needs cross-application memory pays no DB query
        for it); omit it to keep today's behavior (profile/resume/confirmed
        answers only)."""
        profile_facts = _flatten_profile(candidate_profile or {})
        resume_facts = _flatten_resume(parsed_resume or {})
        memory = answer_memory_lookup() if answer_memory_lookup else {}
        return cls(
            profile_facts=profile_facts, resume_facts=resume_facts,
            confirmed_answers=confirmed_answers or {}, answer_memory=memory,
        )

    def resolve(self, label: str) -> VerifiedFact | None:
        """Resolves ONE label against every known source, most-authoritative
        first: a human's own explicit answer for THIS task outranks a stored
        profile value (the person may be answering differently on purpose —
        e.g. a job-specific expected salary), which outranks resume-derived
        text, which outranks older cross-application memory."""
        key = _normalize_key(label)
        if key in self._confirmed:
            return VerifiedFact(self._confirmed[key], "USER_CONFIRMED", True)
        if key in self._profile:
            return VerifiedFact(self._profile[key], "USER_PROFILE", True)
        if key in self._resume:
            return VerifiedFact(self._resume[key], "RESUME", True)
        if key in self._memory:
            return VerifiedFact(self._memory[key], "APPROVED_ANSWER_MEMORY", True)
        return None

    def resolve_any(self, labels: list[str]) -> VerifiedFact | None:
        """Tries several aliases for the same logical field
        (`["phone", "mobile number", "contact number"]`) and returns the
        first hit — used by `question_ontology.py`-classified fields where
        the coarse category implies a small, known set of likely labels."""
        for label in labels:
            fact = self.resolve(label)
            if fact is not None:
                return fact
        return None


def decide(
    *,
    fact: VerifiedFact | None,
    coarse_category: str | None,
    sensitive: bool,
    llm_can_generate: bool = False,
    review_required_for_generated: bool = True,
) -> AnswerDecision:
    """Implements spec §5's ordering for ONE field:

        can the system determine an answer?
            -> is it supported by verified information?
                -> is automatic filling permitted for this question?
                    -> does it require human judgment/confirmation?

    `sensitive` (from `question_ontology.classify_field`) is the hard
    override: a sensitive field NEVER auto-fills, no matter how verified the
    fact is or how confident the classifier was — the value must still be
    written by `executor.py`'s sensitive-field gate, which independently
    requires `is_sourced=True`; this function's `autofill_permitted` only
    tells the loop whether it may even ATTEMPT that, it is not itself the
    gate. `llm_can_generate` covers the one case the spec calls "generation
    may be possible" (an open-ended prompt like "Why do you want to work
    here?") — such an answer is never treated as a verified fact and always
    carries `review_required_for_generated`."""
    if fact is not None:
        if sensitive and fact.source == "RESUME":
            # Resume text is INFERRED, not stated: a resume that happens to
            # list a country, a nationality, a graduation year or a military
            # employer is not the candidate answering a work-authorization,
            # demographic or legal question. Previously such a fact still
            # reached the caller inside `AnswerDecision.fact` (with
            # autofill_permitted=False, so it was never typed) — but a review
            # UI or the question ledger reading `.fact` would have shown a
            # resume-derived string as "the answer" to a sensitive question.
            # Sensitive categories categorically exclude resume-derived facts,
            # so the fact is dropped here, not merely un-permitted.
            return AnswerDecision(
                False, None, False, True,
                "sensitive field; resume-derived text is never a source for a sensitive or "
                "legally significant answer — human confirmation required",
            )
        if sensitive:
            # A verified fact for a sensitive field is exactly the ONE case
            # that MAY be written (spec §3's "USER CONFIRMED -> eligible" and
            # "VERIFIED USER FACT -> generally eligible" rows apply even to a
            # sensitive category when the value is itself something the user
            # already confirmed) — but only when it came from the user
            # directly, never from an inferred/derived source.
            if fact.source in ("USER_CONFIRMED", "USER_PROFILE"):
                return AnswerDecision(
                    True, fact, True, False,
                    f"verified, user-supplied answer ({fact.source}) for a sensitive field — permitted",
                )
            return AnswerDecision(
                True, fact, False, True,
                f"sensitive field; source is {fact.source}, not a direct user statement — human confirmation required",
            )
        if fact.source in _VERIFIED_PROVENANCE:
            return AnswerDecision(True, fact, True, False, f"verified fact from {fact.source} — auto-fill permitted")
        # Should be unreachable (TruthStore.resolve never returns an
        # unverified fact) — defensive fallback, never silently auto-fills.
        return AnswerDecision(True, fact, False, True, f"fact source {fact.source} is not a trusted provenance")

    if sensitive:
        return AnswerDecision(False, None, False, True, "sensitive field with no verified source — never inferred")

    if llm_can_generate:
        # UNCONDITIONAL: an LLM-generated answer is never a verified fact and
        # is never auto-fill-permitted, whatever the caller passed for
        # `review_required_for_generated`. That parameter previously let a
        # caller passing `False` produce `autofill_permitted=True` on a fact
        # whose `verified` is `False` — the exact "an LLM answer silently
        # becomes a verified user fact" path this module exists to prevent.
        # The parameter is kept for call-site compatibility but can now only
        # ever be advisory; it cannot weaken the gate.
        generated = VerifiedFact("", "LLM_GENERATED", False)
        return AnswerDecision(
            True, generated, False, True,
            "no verified source; an LLM-generated answer may be drafted for this open-ended question "
            "but is unverified and always requires human review before it is used",
        )

    return AnswerDecision(False, None, False, True, "no verified source and generation is not appropriate here")


def _flatten_profile(profile: dict) -> dict[str, str]:
    """A small, generic alias map over the candidate-profile dict shape
    (`automation/interfaces.py::CandidateProfileView` / `get_candidate_profile_dict`).
    Deliberately only the common top-level scalar fields — nested
    education/experience/skills are resolved field-by-field by whatever code
    already walks those lists (e.g. the answer engine), not flattened here."""
    aliases = {
        "first name": profile.get("first_name"),
        "last name": profile.get("last_name"),
        "full name": profile.get("full_name"),
        "email": profile.get("email"),
        "phone": profile.get("phone"),
        "mobile number": profile.get("phone"),
        "contact number": profile.get("phone"),
        "telephone": profile.get("phone"),
        "location": profile.get("location"),
        "city": profile.get("city"),
        "state": profile.get("state"),
        "country": profile.get("country"),
        "address": profile.get("address"),
        "linkedin": profile.get("linkedin_url"),
        "linkedin url": profile.get("linkedin_url"),
        "github": profile.get("github_url"),
        "portfolio": profile.get("portfolio_url"),
        "website": profile.get("website_url"),
        "current company": profile.get("current_company"),
        "current role": profile.get("current_role"),
        "years of experience": profile.get("years_of_experience"),
        "notice period": profile.get("notice_period_days"),
        "expected salary": profile.get("expected_salary"),
    }
    return {k: str(v) for k, v in aliases.items() if v not in (None, "")}


def _flatten_resume(parsed_resume: dict) -> dict[str, str]:
    identity = parsed_resume.get("identity") or {}
    out = {
        "first name": (identity.get("name") or "").split(" ")[0] if identity.get("name") else None,
        "full name": identity.get("name"),
        "email": identity.get("email"),
        "phone": identity.get("phone"),
        "location": identity.get("location"),
    }
    skills = parsed_resume.get("skills")
    if skills:
        out["skills"] = ", ".join(skills) if isinstance(skills, list) else str(skills)
    years = parsed_resume.get("total_years_experience")
    if years is not None:
        out["years of experience"] = str(years)
    return {k: str(v) for k, v in out.items() if v not in (None, "")}
