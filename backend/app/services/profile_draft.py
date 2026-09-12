"""Extract a reviewable draft without changing saved candidate facts."""
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy.orm import Session

from app.models.db_models import CandidateProfile
from app.services import profile_repository as repo
from app.services.storage import get_storage_backend
from app.services.text_extraction import extract_text
from app.services.resume_parser import parse_resume_text

#: Scalar CandidateProfile columns a resume draft may contribute, in the
#: shape `apply_resume_draft_to_profile` writes them with — reused by both
#: the draft builder below and the auto-apply merge so the two can't drift
#: on which fields are in scope.
_DRAFT_SCALAR_FIELDS = (
    "full_name", "email", "phone", "location",
    "linkedin_url", "github_url", "portfolio_url", "professional_summary",
    "current_company", "current_role", "years_of_experience",
)

#: The six Master Profile skill buckets (`SkillsRequest` in
#: app/models/profile.py, and `SkillCategories` on the parser's output) —
#: shared here so the draft builder and the auto-apply merge classify/merge
#: the same set of categories.
_SKILL_CATEGORY_FIELDS = (
    "programming_languages", "frameworks", "tools",
    "certifications", "technical_skills", "soft_skills",
)


def _is_current(end_date: str | None) -> bool:
    return not end_date or end_date.strip().lower() == "present"


def _current_role(experience: list) -> tuple[str | None, str | None]:
    """`current_company`/`current_role` aren't fields the parser returns
    directly — they're derived from the experience list: the entry with no
    end date (still ongoing), or the first entry if every job already ended
    (résumés list the most recent job first)."""
    for entry in experience:
        if _is_current(entry.end_date):
            return entry.company, entry.title
    if experience:
        return experience[0].company, experience[0].title
    return None, None


def draft_from_document(document) -> dict:
    with TemporaryDirectory(prefix="autogram-profile-") as directory:
        path = Path(directory) / ("resume" + Path(document.original_filename).suffix.lower())
        path.write_bytes(get_storage_backend().read(document.stored_path))
        parsed, _ = parse_resume_text(extract_text(str(path)))

    current_company, current_role = _current_role(parsed.experience)

    draft = {key: value for key, value in {
        "full_name": parsed.full_name,
        "email": parsed.email,
        "phone": parsed.phone,
        "location": parsed.location,
        "linkedin_url": parsed.linkedin_url,
        "github_url": parsed.github_url,
        "portfolio_url": parsed.portfolio_url,
        "professional_summary": parsed.professional_summary,
        "current_company": current_company,
        "current_role": current_role,
        "years_of_experience": parsed.total_years_experience,
    }.items() if value is not None and str(value).strip()}

    classified_skills = {
        field: getattr(parsed.skill_categories, field)
        for field in _SKILL_CATEGORY_FIELDS
        if getattr(parsed.skill_categories, field)
    }
    if classified_skills:
        draft["skills"] = classified_skills

    if parsed.education:
        draft["education"] = [
            {
                "degree": entry.degree,
                "university": entry.institution,
                "field_of_study": entry.field,
                "end_date": str(entry.graduation_year) if entry.graduation_year else None,
            }
            for entry in parsed.education
        ]

    if parsed.experience:
        draft["experience"] = [
            {
                "company_name": entry.company,
                "job_title": entry.title,
                "start_date": entry.start_date,
                "end_date": None if _is_current(entry.end_date) else entry.end_date,
                "description": entry.description,
            }
            for entry in parsed.experience
        ]

    return draft


# ---------------------------------------------------------------------------
# Auto-apply — persists a draft onto the caller's own saved profile
# ---------------------------------------------------------------------------

def _is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _dedupe_union(existing: list[str], new: list[str]) -> list[str]:
    """First-seen order, case-insensitive de-dup — same rule as
    `ExperienceBase._clean_skills_used` in app/models/profile.py."""
    merged: list[str] = []
    seen: set[str] = set()
    for value in (*existing, *new):
        key = value.strip().casefold() if isinstance(value, str) else value
        if not value or key in seen:
            continue
        seen.add(key)
        merged.append(value)
    return merged


def _education_key(degree, university, end_date) -> tuple:
    return (
        (degree or "").strip().casefold(),
        (university or "").strip().casefold(),
        (end_date or "").strip().casefold(),
    )


def _experience_key(company_name, job_title, start_date) -> tuple:
    """Same identity `ExperienceBatchCreate._reject_duplicate_entries` uses."""
    return (
        (company_name or "").strip().casefold(),
        (job_title or "").strip().casefold(),
        (start_date or "").strip().casefold(),
    )


def apply_resume_draft_to_profile(db: Session, profile: CandidateProfile, draft: dict) -> CandidateProfile:
    """Persists a résumé draft (as built by `draft_from_document`) onto the
    caller's own saved `CandidateProfile`, then returns the refreshed row.

    Never overwrites a fact the user already has: a scalar field is only
    written if the existing column is blank, skills are unioned rather than
    replaced, and education/experience entries are only added when no
    existing row already matches the same (degree, university, grad year) or
    (company, title, start date) — the same identity `ExperienceBatchCreate`
    uses for its own duplicate check. Re-uploading the same résumé, or a
    later revision of it, is always safe to re-apply."""
    scalar_updates = {
        field: draft[field]
        for field in _DRAFT_SCALAR_FIELDS
        if field in draft and _is_blank(getattr(profile, field, None))
    }
    if scalar_updates:
        profile = repo.update_profile(db, profile, scalar_updates)

    draft_skills = draft.get("skills")
    if draft_skills:
        existing_skills = dict(profile.skills or {})
        for key in _SKILL_CATEGORY_FIELDS:
            if draft_skills.get(key):
                existing_skills[key] = _dedupe_union(existing_skills.get(key) or [], draft_skills[key])
        profile = repo.update_skills(db, profile, existing_skills)

    draft_education = draft.get("education")
    if draft_education:
        existing_keys = {
            _education_key(entry.degree, entry.university, entry.end_date)
            for entry in repo.list_education(db, profile.profile_id)
        }
        for entry in draft_education:
            key = _education_key(entry.get("degree"), entry.get("university"), entry.get("end_date"))
            if key not in existing_keys and key != ("", "", ""):
                repo.add_education(db, profile.profile_id, entry)
                existing_keys.add(key)

    draft_experience = draft.get("experience")
    if draft_experience:
        existing_keys = {
            _experience_key(entry.company_name, entry.job_title, entry.start_date)
            for entry in repo.list_experience(db, profile.profile_id)
        }
        new_entries = []
        for entry in draft_experience:
            key = _experience_key(entry.get("company_name"), entry.get("job_title"), entry.get("start_date"))
            if key not in existing_keys and key != ("", "", ""):
                new_entries.append(entry)
                existing_keys.add(key)
        if new_entries:
            repo.add_experiences(db, profile.profile_id, new_entries)

    return repo.get_by_user_id(db, profile.user_id)
