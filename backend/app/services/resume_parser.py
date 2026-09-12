import json
import re
from pydantic import ValidationError

from app.ai.llm import llm_router
from app.models.parsed_resume import ParsedResume

SYSTEM_PROMPT = """You are a precise resume-parsing engine. You extract only information explicitly present in the resume text.

Rules you must follow strictly:
- NEVER invent, guess, or infer facts that aren't in the text (no fake dates, companies, degrees, or numbers).
- If a field isn't present in the resume, leave it null or as an empty list. Do not fill in placeholders.
- "skills" = skills explicitly listed by the candidate (e.g. a Skills section, or clearly named tools/languages).
- "inferred_skills" = skills you can reasonably infer from experience descriptions (e.g. "built REST APIs in Django" implies Python, Django, REST) — keep these SEPARATE from explicit skills.
- "skill_categories" classifies EVERY skill/technology/tool named anywhere in the resume (Skills section, experience descriptions, projects) into exactly ONE of these six categories — never dump everything into "technical_skills":
  - "programming_languages": actual programming or query languages only (e.g. Python, JavaScript, TypeScript, C++, Java, SQL).
  - "frameworks": frameworks and libraries (e.g. React, Next.js, FastAPI, Django, Flask, LangChain, LangGraph).
  - "tools": developer tools, platforms, cloud providers, IDEs, and utilities (e.g. Git, GitHub, Docker, Kubernetes, Postman, VS Code, Azure, AWS).
  - "technical_skills": technical concepts or domains that are NOT themselves a language, framework, or tool (e.g. RAG, REST APIs, Microservices, Machine Learning, NLP, Generative AI, Data Structures).
  - "soft_skills": interpersonal/professional skills (e.g. Communication, Leadership, Teamwork, Problem Solving).
  - "certifications": ONLY actual named professional certifications the candidate holds (e.g. "AWS Certified Solutions Architect", "PMP") — a plain technology name is never a certification, even one people commonly get certified in.
  Put each item in exactly one category (no duplicates across categories) unless the resume genuinely uses the same term in two distinct senses. Never invent a skill that isn't in the text.
- "achievements" = quantified or notable accomplishments within a role, separate from the general description.
- "linkedin_url"/"github_url"/"portfolio_url" = only if a literal URL (or handle clearly identifying one of these) appears in the text — never construct or guess one from a name.
- "professional_summary" = the candidate's own summary/objective section, copied as written — never a summary you compose yourself from their experience. Leave null if the resume has no such section.
- Compute nothing yourself except total_years_experience, which you should estimate from the date ranges given.
- Output ONLY valid JSON matching the schema. No markdown fences, no preamble, no explanation text.
"""

JSON_SCHEMA_HINT = """
Respond with ONLY a JSON object with this exact shape (use null/[] for missing data):
{
  "full_name": string or null,
  "email": string or null,
  "phone": string or null,
  "location": string or null,
  "linkedin_url": string or null,
  "github_url": string or null,
  "portfolio_url": string or null,
  "professional_summary": string or null,
  "total_years_experience": number or null,
  "skills": [string],
  "inferred_skills": [string],
  "skill_categories": {
    "programming_languages": [string],
    "frameworks": [string],
    "tools": [string],
    "technical_skills": [string],
    "soft_skills": [string],
    "certifications": [string]
  },
  "experience": [
    {
      "company": string or null,
      "title": string or null,
      "start_date": "YYYY-MM" or null,
      "end_date": "YYYY-MM" or "present" or null,
      "description": string or null,
      "achievements": [string]
    }
  ],
  "education": [
    {
      "institution": string or null,
      "degree": string or null,
      "field": string or null,
      "graduation_year": number or null
    }
  ],
  "certifications": [string]
}
"""


class ParsingError(Exception):
    pass


def _strip_json_fences(text: str) -> str:
    """Models sometimes wrap output in ```json ... ``` even when told not to."""
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    return text


def _call_llm(resume_text: str, strict_followup: bool = False) -> str:
    user_prompt = f"{JSON_SCHEMA_HINT}\n\nResume text:\n\"\"\"\n{resume_text}\n\"\"\""

    if strict_followup:
        user_prompt = (
            "Your previous response was not valid JSON matching the schema. "
            "Respond again with ONLY the raw JSON object, nothing else — "
            "no markdown, no commentary.\n\n" + user_prompt
        )

    return llm_router.run(
        task="resume_parse",
        system=SYSTEM_PROMPT,
        prompt=user_prompt,
    )


def parse_resume_text(resume_text: str) -> tuple[ParsedResume, float]:
    """
    Parses raw resume text into a validated ParsedResume.
    Retries once with a stricter prompt if the first attempt fails.
    Returns (parsed_resume, confidence_score).
    Raises ParsingError if both attempts fail.
    """
    last_error = None

    for attempt in range(2):
        try:
            raw_output = _call_llm(resume_text, strict_followup=(attempt == 1))
            cleaned = _strip_json_fences(raw_output)
            data = json.loads(cleaned)
            parsed = ParsedResume(**data)
            confidence = _compute_confidence(parsed)
            return parsed, confidence
        except (json.JSONDecodeError, ValidationError) as e:
            last_error = e
            continue

    raise ParsingError(f"Failed to parse resume after 2 attempts: {last_error}")


def _compute_confidence(parsed: ParsedResume) -> float:
    """
    Simple heuristic confidence score based on how much of the expected
    structure was actually extracted. Not a guarantee of correctness,
    just a signal for downstream systems to down-weight sparse profiles.
    """
    checks = [
        bool(parsed.full_name),
        bool(parsed.email),
        len(parsed.skills) > 0,
        len(parsed.experience) > 0,
        all(exp.start_date for exp in parsed.experience) if parsed.experience else False,
        len(parsed.education) > 0,
    ]
    return round(sum(checks) / len(checks), 2)