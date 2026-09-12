"""
Cover-letter generation for the deterministic auto-apply flow
(`ApplicationFlowManager` / `automation.ats.base.ATSAdapter`).

Only ever invoked when `ATSAdapter.detect_cover_letter_field()` finds a REAL
field on the current page asking for one (see
`ApplicationFlowManager._handle_cover_letter_if_requested`) — most
applications never do, and this module does nothing for them. Grounded in
only two sources, both already flowing through this exact run: the job
description already available to it (the same optional hint
`ApplicationAnswerEngine` receives — see `app/api/applications.py`'s
`_run_application`/`ApplicationFlowManager.job_description`) and the user's
own Master Profile (`CandidateProfile`, the same object every other fill path
in `automation/` already reads). Never invents a fact about the candidate.

Note: an earlier cover-letter generator existed in this codebase
(`app/services/tailoring_service.py`) and was removed at the owner's request.
This is a new, separate implementation, not a restoration of that one.

The generated file (when a page's field is a file upload rather than a text
box) is a plain local temp file — never a `ProfileDocument`/`StorageBackend`
write, so it never enters the user's permanent document library and there is
nothing durable to clean up afterward beyond that one temp file. See
`ApplicationFlowManager._cover_letter_file_path` / `_cleanup_cover_letter_file`
for creation/deletion.
"""

from __future__ import annotations

import tempfile

import docx

from automation.interfaces import CandidateProfile, generate_answer

SYSTEM_PROMPT = """You write a short, professional cover letter for a job application.

Rules you must follow strictly:
- Use ONLY the candidate facts given to you below (name, current role/company, years of experience, skills, professional summary). NEVER invent an employer, title, skill, achievement, or fact not given to you.
- Ground the letter in the job description given to you — reference the role/company by name where given, and connect the candidate's real experience to what the posting actually asks for.
- 3-4 short paragraphs, no more than about 350 words total.
- No placeholder text like "[Your Name]" or "[Company]" — if a fact isn't given to you, write around it rather than leaving a blank or inventing one.
- Plain prose only: no markdown, no bullet points, no subject line, no letterhead/address block, no closing signature block beyond a simple sign-off.
- Output ONLY the letter body text — no preamble, no explanation, no surrounding quotes.
"""


def _profile_facts(profile: CandidateProfile) -> str:
    """A short, plain-text summary of the real facts the LLM may draw on —
    the same fields every other fill path in `automation/` already reads
    directly off `profile` (see `automation/forms/answer_engine.py`)."""
    skills = profile.skills or {}
    flat_skills = [
        skill
        for key in ("programming_languages", "frameworks", "tools", "technical_skills")
        for skill in (skills.get(key) or [])
    ]
    lines = [
        f"Name: {profile.full_name}" if profile.full_name else None,
        (
            f"Current role: {profile.current_role} at {profile.current_company}"
            if profile.current_role or profile.current_company else None
        ),
        f"Years of experience: {profile.years_of_experience}" if profile.years_of_experience is not None else None,
        f"Professional summary: {profile.professional_summary}" if profile.professional_summary else None,
        f"Skills: {', '.join(flat_skills)}" if flat_skills else None,
    ]
    return "\n".join(line for line in lines if line) or "(no additional profile facts available)"


def generate_cover_letter_text(
    *,
    profile: CandidateProfile,
    job_description: str,
    company: str | None = None,
    position: str | None = None,
) -> str:
    """One LLM call — the letter's full text, ready to type into a field or
    write to a file. Raises `automation.interfaces.LLMRouterError` on
    failure; the caller (`ApplicationFlowManager._generate_cover_letter`)
    treats that as "could not generate one," never a run-ending crash."""
    heading = f" — {position} at {company}" if position or company else ""
    user_prompt = (
        f"Job posting{heading}:\n\"\"\"\n{job_description.strip()}\n\"\"\"\n\n"
        f"Candidate facts:\n\"\"\"\n{_profile_facts(profile)}\n\"\"\"\n\n"
        "Write the cover letter now."
    )
    text = generate_answer(task="cover_letter_generation", system=SYSTEM_PROMPT, prompt=user_prompt)
    return text.strip()


def write_cover_letter_docx(text: str) -> str:
    """Writes `text` to a brand-new temporary `.docx` file and returns its
    path — used only when a page's cover-letter field is a file upload, not
    a text box. Deliberately bypasses `ProfileDocument`/`StorageBackend`
    entirely: this file is never part of the user's document library, isn't
    a database row, and exists only to hand real bytes to Playwright's
    `set_input_files`. The caller deletes it once the run is done (see
    `ApplicationFlowManager._cleanup_cover_letter_file`)."""
    document = docx.Document()
    for paragraph in text.split("\n\n"):
        if paragraph.strip():
            document.add_paragraph(paragraph.strip())

    handle = tempfile.NamedTemporaryFile(prefix="cover_letter_", suffix=".docx", delete=False)
    handle.close()  # closed before docx writes to the same path — avoids a Windows file-lock conflict
    document.save(handle.name)
    return handle.name
