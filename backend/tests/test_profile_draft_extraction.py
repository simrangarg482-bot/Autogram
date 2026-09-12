"""
`app/services/profile_draft.py::draft_from_document` — everything the resume
parser can contribute to a reviewable draft, not just the four contact
fields. `extract_text`/`parse_resume_text` are mocked; this only pins the
shaping/derivation logic (current_company/current_role, the skills split,
education/experience field mapping).
"""

from types import SimpleNamespace
from unittest.mock import patch

from app.models.parsed_resume import Education, Experience, ParsedResume, SkillCategories
from app.services.profile_draft import draft_from_document


def _document(filename="resume.pdf", stored_path="storage/resumes/x.pdf"):
    return SimpleNamespace(original_filename=filename, stored_path=stored_path)


def _draft(parsed: ParsedResume) -> dict:
    with patch("app.services.profile_draft.get_storage_backend") as get_backend, \
         patch("app.services.profile_draft.extract_text", return_value="irrelevant"), \
         patch("app.services.profile_draft.parse_resume_text", return_value=(parsed, 0.9)):
        get_backend.return_value.read.return_value = b"fake bytes"
        return draft_from_document(_document())


def test_scalar_fields_pass_through():
    parsed = ParsedResume(full_name="Ada Lovelace", email="ada@example.com", phone="+44 123", location="London")
    draft = _draft(parsed)
    assert draft["full_name"] == "Ada Lovelace"
    assert draft["email"] == "ada@example.com"
    assert draft["phone"] == "+44 123"
    assert draft["location"] == "London"
    assert "years_of_experience" not in draft  # None -> omitted, not sent as null


def test_years_of_experience_included_when_present():
    parsed = ParsedResume(total_years_experience=6.5)
    draft = _draft(parsed)
    assert draft["years_of_experience"] == 6.5


def test_current_role_prefers_the_entry_with_no_end_date():
    parsed = ParsedResume(experience=[
        Experience(company="Old Co", title="Junior Dev", start_date="2018-01", end_date="2020-01"),
        Experience(company="New Co", title="Senior Dev", start_date="2020-02", end_date=None),
    ])
    draft = _draft(parsed)
    assert draft["current_company"] == "New Co"
    assert draft["current_role"] == "Senior Dev"


def test_current_role_falls_back_to_the_first_entry_when_nothing_is_ongoing():
    parsed = ParsedResume(experience=[
        Experience(company="Most Recent Co", title="Lead", start_date="2022-01", end_date="2023-01"),
        Experience(company="Older Co", title="Dev", start_date="2018-01", end_date="2020-01"),
    ])
    draft = _draft(parsed)
    assert draft["current_company"] == "Most Recent Co"
    assert draft["current_role"] == "Lead"


def test_no_experience_means_no_current_role():
    draft = _draft(ParsedResume())
    assert "current_company" not in draft
    assert "current_role" not in draft


def test_classified_skills_are_grouped_by_category():
    parsed = ParsedResume(skill_categories=SkillCategories(
        programming_languages=["Python", "TypeScript"],
        frameworks=["FastAPI", "React"],
        tools=["Docker", "Git"],
        technical_skills=["REST APIs", "Machine Learning"],
        soft_skills=["Communication"],
        certifications=["AWS Certified Solutions Architect"],
    ))
    draft = _draft(parsed)
    assert draft["skills"] == {
        "programming_languages": ["Python", "TypeScript"],
        "frameworks": ["FastAPI", "React"],
        "tools": ["Docker", "Git"],
        "technical_skills": ["REST APIs", "Machine Learning"],
        "soft_skills": ["Communication"],
        "certifications": ["AWS Certified Solutions Architect"],
    }


def test_classified_skills_omit_empty_categories():
    parsed = ParsedResume(skill_categories=SkillCategories(programming_languages=["Python"]))
    draft = _draft(parsed)
    assert draft["skills"] == {"programming_languages": ["Python"]}


def test_no_classified_skills_omits_the_key():
    draft = _draft(ParsedResume())
    assert "skills" not in draft


def test_education_maps_parser_field_names_to_profile_field_names():
    parsed = ParsedResume(education=[
        Education(institution="MIT", degree="BSc", field="Computer Science", graduation_year=2020),
    ])
    draft = _draft(parsed)
    assert draft["education"] == [{
        "degree": "BSc", "university": "MIT", "field_of_study": "Computer Science", "end_date": "2020",
    }]


def test_experience_maps_parser_field_names_and_normalizes_present_to_none():
    parsed = ParsedResume(experience=[
        Experience(company="Acme", title="Engineer", start_date="2021-01", end_date="present", description="Built things."),
    ])
    draft = _draft(parsed)
    assert draft["experience"] == [{
        "company_name": "Acme", "job_title": "Engineer",
        "start_date": "2021-01", "end_date": None, "description": "Built things.",
    }]


def test_no_education_or_experience_omits_those_keys():
    draft = _draft(ParsedResume())
    assert "education" not in draft
    assert "experience" not in draft
