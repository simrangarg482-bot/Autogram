# Resume-Based Master Profile Filling

Implements the flow: a user uploads a résumé (PDF/DOCX) → it's stored → text is
extracted with MarkItDown → an LLM maps it onto the **existing** Master
Profile schema → the result is validated/normalized and **auto-saved** onto
the user's own `CandidateProfile` (never overwriting facts already there) →
the user reviews/edits it on the existing Profile page.

This builds on an existing, partially-built pipeline (résumé upload, storage,
extraction, LLM parsing, and a read-only "preview" endpoint already existed).
The work here was: swap in MarkItDown as the extraction engine, extend what
the parser maps onto the profile, and add the auto-save step + its onboarding
entry point — no new profile system, no duplicated storage/auth/validation.

## Flow

```
Signup (new user only)
  → redirected to /profile with an "Upload your resume" prompt
  → user picks a PDF/DOCX
      → POST /profile/documents/upload            (existing — stores the original file,
                                                     auto-creates an empty profile row)
      → POST /profile/documents/{id}/apply-resume-draft   (new)
          1. extract_text(stored_path)              — MarkItDown, PDF/DOCX (text_extraction.py)
          2. parse_resume_text(text)                — LLM call via llm_router (resume_parser.py),
                                                        validated into ParsedResume (Pydantic).
                                                        Every skill/technology found is classified
                                                        into one of the 6 Master Profile skill
                                                        buckets in this same call (see "Skill
                                                        classification" below) — never dumped
                                                        into one bucket.
          3. draft_from_document(...)               — maps ParsedResume → Master Profile field
                                                        names (profile_draft.py)
          4. apply_resume_draft_to_profile(...)      — merges the draft onto the user's
                                                        CandidateProfile row: fills only
                                                        blank scalar fields, unions each
                                                        classified skill bucket, adds only
                                                        new education/experience rows
      → returns the now-SAVED profile
  → Profile page reloads and shows the populated, saved profile for review/edit
```

Re-uploading a résumé (or uploading a second one later, from either the
Profile page or the existing Resumes/document-library page) is safe to
re-run: nothing already on the profile is ever replaced, and duplicate
education/experience rows are skipped.

The original read-only preview endpoint (`POST /profile/documents/{id}/profile-draft`,
used by the pre-existing "Build profile" button on the document library page)
is untouched — it still returns a draft for manual, per-item accept/dismiss
without saving anything, which is a different, already-tested UX path.

## Files modified

**Backend**

| File | What changed |
|---|---|
| `backend/app/services/text_extraction.py` | MarkItDown is now the primary extraction engine for both PDF and DOCX (`_extract_with_markitdown`, `_extract_pdf`, `_extract_docx`). The pre-existing pdfplumber (column-aware) and python-docx (table-aware) extractors are kept, unchanged, as the fallback when MarkItDown isn't installed or returns too little text — same for the OCR fallback on scanned PDFs. Public `extract_text()` entry point and its `ExtractionError`/`MIN_TEXT_LENGTH` contract are unchanged. |
| `backend/app/models/parsed_resume.py` | Added `linkedin_url`, `github_url`, `portfolio_url`, `professional_summary` to `ParsedResume` — resume-derivable Master Profile fields the parser wasn't extracting before. Added `SkillCategories` (the 6 Master Profile skill buckets) and a `skill_categories: SkillCategories` field on `ParsedResume` — see "Skill classification" below. |
| `backend/app/services/resume_parser.py` | Extended the LLM system prompt and JSON schema hint for the four new contact/summary fields, plus the full skill-classification ruleset and `skill_categories` schema (see below). |
| `backend/app/models/profile.py` | Added the four new scalar fields to `ResumeDraftResponse`. Expanded `ResumeDraftSkills` from 2 fields (`technical_skills`, `certifications`) to all 6 (`programming_languages`, `frameworks`, `tools`, `certifications`, `technical_skills`, `soft_skills`) — same field set as `SkillsRequest`. |
| `backend/app/services/profile_draft.py` | `draft_from_document()` now includes the four new scalar fields and builds `draft["skills"]` from the classified `skill_categories` (one key per non-empty bucket) instead of always writing `technical_skills`/`certifications`. Added `apply_resume_draft_to_profile(db, profile, draft)` — the auto-save merge: scalar fields fill only currently-blank columns, every skill bucket present in the draft is unioned into the matching profile bucket case-insensitively (via `_SKILL_CATEGORY_FIELDS`, all 6 keys), and education/experience entries are only inserted if no existing row already matches on the same identity key (degree+university+grad-year, or company+title+start-date — the latter reusing the exact duplicate-detection rule `ExperienceBatchCreate` already used for bulk-create). |
| `backend/app/api/profile.py` | Added `POST /profile/documents/{document_id}/apply-resume-draft`. Same ownership/auth pattern as every other profile route (`_get_owned_profile`/`_get_owned_document`, 404-not-403). |
| `backend/requirements.txt` | Added `markitdown[pdf,docx]`. |
| `backend/tests/test_profile_draft.py` | Added tests for the new endpoint (saves the draft; rejects non-résumé documents). |
| `backend/tests/test_profile_draft_extraction.py` | Replaced the old "skills grouped as technical_skills/certifications" tests with tests asserting `draft["skills"]` reflects whatever categories `skill_categories` actually populated. |
| `backend/tests/test_profile_draft_apply.py` | Added a test that the skill-union merge covers all 6 categories (not just `technical_skills`/`certifications`). |

**Frontend**

| File | What changed |
|---|---|
| `frontend/src/api.js` | Added `applyResumeDraft(documentId)`. |
| `frontend/src/components/AuthPage.jsx` | `onAuthed` now also reports `isNewSignup` (mode === "signup"). |
| `frontend/src/App.jsx` | `handleAuthed`: a brand-new signup with no pending deep-link redirect now lands on `/profile` (with `state: { promptResumeUpload: true }`) instead of `/dashboard`; login is unchanged. |
| `frontend/src/pages/Profile.jsx` | Added the onboarding upload prompt (shown when `promptResumeUpload` is set): a button that uploads a file via the existing `uploadDocument` call, then calls the new `applyResumeDraft`, then reloads the (now-saved) profile, education, experience, and skills. Also added a `professional_summary` textarea to the Professional section, since the parser can now fill that field but the page had no input for it. |
| `frontend/src/__tests__/EntryFlow.test.jsx` | Updated the signup test to assert the new post-signup destination (profile page + upload prompt) instead of the dashboard. |

## Files created

- `backend/tests/test_profile_draft_apply.py` — unit tests for `apply_resume_draft_to_profile`'s merge rules (fills blanks, never overwrites, dedupes education/experience, unions skills).
- `backend/tests/test_text_extraction_markitdown.py` — tests that MarkItDown is used when available/sufficient and that extraction falls back to the legacy extractors otherwise (import failure, exception, or too-short output).
- `user-profile-filling.md` — this file.

## Skill classification

Previously every explicitly-listed resume skill landed in `technical_skills`,
leaving `programming_languages`, `frameworks`, `tools`, and `soft_skills`
always empty from an import. The parser now classifies each skill/technology
it finds into whichever of the Master Profile's 6 skill buckets it actually
belongs to, in the same LLM call that already parses the rest of the resume:

- **`programming_languages`** — actual programming/query languages (Python, JavaScript, TypeScript, C++, Java, SQL, ...).
- **`frameworks`** — frameworks and libraries (React, Next.js, FastAPI, Django, Flask, LangChain, LangGraph, ...).
- **`tools`** — developer tools, platforms, cloud providers, IDEs, utilities (Git, GitHub, Docker, Kubernetes, Postman, VS Code, Azure, AWS, ...).
- **`technical_skills`** — technical concepts/domains that are not themselves a language, framework, or tool (RAG, REST APIs, Microservices, Machine Learning, NLP, Generative AI, Data Structures, ...).
- **`soft_skills`** — interpersonal/professional skills (Communication, Leadership, Teamwork, Problem Solving, ...).
- **`certifications`** — only actual named professional certifications (e.g. "AWS Certified Solutions Architect") — never a plain technology name.

**LLM prompt/schema changes** (`resume_parser.py`): `SYSTEM_PROMPT` gained the
rule list above (each item goes in exactly one category, no duplicates across
categories, never invent a skill not in the text), and `JSON_SCHEMA_HINT`
gained a `skill_categories` object with the 6 array fields. The pre-existing
flat `skills` / `inferred_skills` / `certifications` fields are asked for
unchanged in the same response — they're left alone because the job-matching
pipeline (`app/services/matching/ranker.py`, `resume_text_builder.py`,
`normalizer.py`, `app/api/resumes.py`) already depends on them as plain lists,
and touching their shape was out of scope for a Master-Profile-only change.

**Validation** (`app/models/parsed_resume.py`): the new `SkillCategories`
Pydantic model has one `list[str]` field per bucket (default empty) — the
same validation every other list field on `ParsedResume` already gets from
Pydantic; nothing bespoke was added.

**Flow into the profile**: `draft_from_document()` (`profile_draft.py`) reads
`parsed.skill_categories` and puts only the non-empty buckets into
`draft["skills"]` (so the example in the task — Python/TypeScript/C++ →
`programming_languages`, FastAPI/React/LangChain/LangGraph → `frameworks`,
Docker/Git/Azure → `tools`, REST APIs/RAG/Machine Learning →
`technical_skills`, Communication → `soft_skills` — comes through as 5
separate keys, not one). `apply_resume_draft_to_profile()` then unions each
bucket present in the draft into the matching bucket already on
`CandidateProfile.skills`, case-insensitively deduped, leaving every other
existing bucket and every existing entry untouched — the same
"only add, never remove or overwrite" rule the rest of the auto-apply merge
already follows for scalar fields and education/experience rows.

The frontend needed no changes for this: `Profile.jsx`'s skills section and
its `mergeSkillsText()` helper already iterate over all 6 `SKILL_KEYS`
generically, so a `ResumeDraftResponse.skills` payload that now uses all 6
keys (instead of just 2) is already displayed and saved correctly.

## New API endpoints

- `POST /profile/documents/{document_id}/apply-resume-draft` → `ProfileResponse`
  Extracts and **saves** a résumé's contents onto the caller's own profile. 400 if the document isn't a `resume`; 422 if extraction/parsing fails; 404 if the document doesn't belong to the caller (existing `_get_owned_document` pattern).

(The existing `POST /profile/documents/{document_id}/profile-draft` preview endpoint is unchanged.)

## New dependencies

- `markitdown[pdf,docx]` (backend) — primary resume text-extraction engine.
- Skill classification added no new dependency — it's entirely a prompt/schema change to the existing `resume_parse` LLM call.

## Database / schema changes

**None.** Every field the resume flow now fills (`linkedin_url`, `github_url`,
`portfolio_url`, `professional_summary`, plus the fields already mapped)
already existed as columns on `CandidateProfile` from prior work — this
feature only adds a new way of populating them, not new storage.

## Design notes

- **No new profile system.** Everything is written through the existing `profile_repository.py` functions (`update_profile`, `update_skills`, `add_education`, `add_experiences`) — the same functions the manual profile-edit UI uses, so encryption (phone/address), validation, and read paths all stay in one place.
- **Never invents data.** The LLM prompt (`resume_parser.py`) explicitly forbids guessing; `apply_resume_draft_to_profile` only ever writes a field that is currently blank, so a manually entered fact is never silently replaced by a résumé guess — re-uploading a résumé later is always safe.
- **Modularity for the auto-fill engine.** `apply_resume_draft_to_profile` takes a plain `dict` (the same shape `draft_from_document` already produces) and a `CandidateProfile` row — it doesn't know or care that the source was a résumé upload versus, say, a future import from another source, which keeps it reusable for the auto-apply job-application engine mentioned as the next phase.
- **Classification is additive, not a rename.** `ParsedResume.skills`/`.certifications`/`.inferred_skills` were left exactly as they were rather than reshaped into categories, specifically so the unrelated job-matching pipeline that already reads them as flat lists needed zero changes. `skill_categories` is a new, separate field just for the Master Profile mapping.
