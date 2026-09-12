"""
`app/services/profile_draft.py::apply_resume_draft_to_profile` — the
auto-save half of resume import. Pins the "never clobber a manually entered
fact" contract: scalars only fill blanks, skills union rather than replace,
and education/experience only add rows that aren't already present.
"""

from types import SimpleNamespace
from unittest.mock import patch

from app.services.profile_draft import apply_resume_draft_to_profile


def _profile(**overrides):
    base = dict(
        user_id="user-1", profile_id="profile-1", skills=None,
        full_name=None, email=None, phone=None, location=None,
        linkedin_url=None, github_url=None, portfolio_url=None, professional_summary=None,
        current_company=None, current_role=None, years_of_experience=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_fills_blank_scalar_fields():
    profile = _profile()
    draft = {"full_name": "Ada Lovelace", "email": "ada@example.com"}
    with patch("app.services.profile_draft.repo") as repo:
        repo.update_profile.side_effect = lambda db, p, data: SimpleNamespace(**{**p.__dict__, **data})
        repo.list_education.return_value = []
        repo.list_experience.return_value = []
        repo.get_by_user_id.return_value = "final-profile"
        result = apply_resume_draft_to_profile(object(), profile, draft)
    repo.update_profile.assert_called_once()
    written = repo.update_profile.call_args.args[2]
    assert written == {"full_name": "Ada Lovelace", "email": "ada@example.com"}
    assert result == "final-profile"


def test_never_overwrites_an_existing_scalar_value():
    profile = _profile(full_name="Already Set", email=None)
    draft = {"full_name": "Ada Lovelace", "email": "ada@example.com"}
    with patch("app.services.profile_draft.repo") as repo:
        repo.update_profile.side_effect = lambda db, p, data: SimpleNamespace(**{**p.__dict__, **data})
        repo.list_education.return_value = []
        repo.list_experience.return_value = []
        apply_resume_draft_to_profile(object(), profile, draft)
    written = repo.update_profile.call_args.args[2]
    assert written == {"email": "ada@example.com"}  # full_name NOT included


def test_no_scalar_updates_when_everything_already_set():
    profile = _profile(full_name="Set", email="set@example.com")
    draft = {"full_name": "Draft Name", "email": "draft@example.com"}
    with patch("app.services.profile_draft.repo") as repo:
        repo.list_education.return_value = []
        repo.list_experience.return_value = []
        apply_resume_draft_to_profile(object(), profile, draft)
    repo.update_profile.assert_not_called()


def test_skills_union_with_existing_deduped_case_insensitively():
    profile = _profile(skills={"technical_skills": ["Python"], "programming_languages": ["Go"]})
    draft = {"skills": {"technical_skills": ["python", "SQL"], "certifications": ["AWS"]}}
    with patch("app.services.profile_draft.repo") as repo:
        repo.list_education.return_value = []
        repo.list_experience.return_value = []
        apply_resume_draft_to_profile(object(), profile, draft)
    written_skills = repo.update_skills.call_args.args[2]
    assert written_skills["technical_skills"] == ["Python", "SQL"]  # kept first-seen casing, deduped
    assert written_skills["certifications"] == ["AWS"]
    assert written_skills["programming_languages"] == ["Go"]  # untouched keys preserved


def test_skills_union_covers_every_classified_category_not_just_two():
    profile = _profile(skills={"tools": ["Postman"]})
    draft = {"skills": {
        "programming_languages": ["Python", "C++"],
        "frameworks": ["FastAPI", "LangChain"],
        "tools": ["docker", "Git"],
        "technical_skills": ["RAG"],
        "soft_skills": ["Communication"],
        "certifications": ["AWS Certified Solutions Architect"],
    }}
    with patch("app.services.profile_draft.repo") as repo:
        repo.list_education.return_value = []
        repo.list_experience.return_value = []
        apply_resume_draft_to_profile(object(), profile, draft)
    written = repo.update_skills.call_args.args[2]
    assert written["programming_languages"] == ["Python", "C++"]
    assert written["frameworks"] == ["FastAPI", "LangChain"]
    assert written["tools"] == ["Postman", "docker", "Git"]  # existing kept, new appended, no dupe of "Docker"-style casing needed here
    assert written["technical_skills"] == ["RAG"]
    assert written["soft_skills"] == ["Communication"]
    assert written["certifications"] == ["AWS Certified Solutions Architect"]


def test_education_skips_entries_matching_an_existing_row():
    profile = _profile()
    existing = SimpleNamespace(degree="BSc", university="MIT", end_date="2020")
    draft = {"education": [
        {"degree": "BSc", "university": "MIT", "field_of_study": "CS", "end_date": "2020"},
        {"degree": "MSc", "university": "MIT", "field_of_study": "CS", "end_date": "2022"},
    ]}
    with patch("app.services.profile_draft.repo") as repo:
        repo.list_education.return_value = [existing]
        repo.list_experience.return_value = []
        apply_resume_draft_to_profile(object(), profile, draft)
    assert repo.add_education.call_count == 1
    assert repo.add_education.call_args.args[2]["degree"] == "MSc"


def test_experience_skips_entries_matching_an_existing_row():
    profile = _profile()
    existing = SimpleNamespace(company_name="Acme", job_title="Engineer", start_date="2021-01")
    draft = {"experience": [
        {"company_name": "Acme", "job_title": "Engineer", "start_date": "2021-01", "end_date": None, "description": "x"},
        {"company_name": "Globex", "job_title": "Dev", "start_date": "2018-01", "end_date": "2020-01", "description": "y"},
    ]}
    with patch("app.services.profile_draft.repo") as repo:
        repo.list_education.return_value = []
        repo.list_experience.return_value = [existing]
        apply_resume_draft_to_profile(object(), profile, draft)
    repo.add_experiences.assert_called_once()
    new_entries = repo.add_experiences.call_args.args[2]
    assert len(new_entries) == 1
    assert new_entries[0]["company_name"] == "Globex"


def test_empty_draft_touches_nothing():
    profile = _profile()
    with patch("app.services.profile_draft.repo") as repo:
        repo.list_education.return_value = []
        repo.list_experience.return_value = []
        apply_resume_draft_to_profile(object(), profile, {})
    repo.update_profile.assert_not_called()
    repo.update_skills.assert_not_called()
    repo.add_education.assert_not_called()
    repo.add_experiences.assert_not_called()
