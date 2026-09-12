from pydantic import BaseModel, Field
from typing import Optional


class Experience(BaseModel):
    company: Optional[str] = None
    title: Optional[str] = None
    start_date: Optional[str] = None  # "YYYY-MM"
    end_date: Optional[str] = None    # "YYYY-MM" or "present"
    description: Optional[str] = None
    achievements: list[str] = Field(default_factory=list)


class Education(BaseModel):
    institution: Optional[str] = None
    degree: Optional[str] = None
    field: Optional[str] = None
    graduation_year: Optional[int] = None


class SkillCategories(BaseModel):
    """Every skill/technology the LLM found, classified into the same six
    buckets the Master Profile UI already has (`SkillsRequest` in
    app/models/profile.py) — a language goes under `programming_languages`,
    a framework under `frameworks`, and so on. Kept separate from the flat
    `skills`/`certifications` lists below (which existing job-matching code
    — ranker.py, normalizer.py, resume_text_builder.py — already depends on
    as plain lists) so this classification is purely additive."""

    programming_languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    technical_skills: list[str] = Field(default_factory=list)
    soft_skills: list[str] = Field(default_factory=list)


class ParsedResume(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    portfolio_url: Optional[str] = None
    # Only the candidate's own summary/objective section, verbatim — never a
    # composed-from-experience blurb. See resume_parser.SYSTEM_PROMPT.
    professional_summary: Optional[str] = None
    total_years_experience: Optional[float] = None
    skills: list[str] = Field(default_factory=list)
    inferred_skills: list[str] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    # The same skills as `skills`/`certifications` above, but classified by
    # category — see `SkillCategories`. This is what the Master Profile
    # import (profile_draft.py) uses; `skills`/`certifications` stay exactly
    # as they were for the job-matching pipeline that already reads them.
    skill_categories: SkillCategories = Field(default_factory=SkillCategories)