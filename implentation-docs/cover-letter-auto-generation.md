# Cover-Letter Auto-Generation

Adds cover-letter support to the **deterministic auto-apply flow**
(`POST /applications/start` → `ApplicationFlowManager` → per-ATS
`ATSAdapter`s in `automation/ats/`) — the other, separate "autonomous agent"
engine (`POST /agent/tasks` → `AutonomousAgentLoop`) is untouched by this
change, by explicit scope decision.

> An earlier cover-letter generator existed in this codebase
> (`app/services/tailoring_service.py`) and was removed at the owner's
> request. Everything here is a new, separate implementation guided by
> today's requirements — not a restoration of that one.

## Flow

```
Application run reaches a page
  → ATSAdapter.detect_cover_letter_field()
      - no cover-letter field on this page?  -> do nothing, continue exactly
        as before (the whole existing flow is unaffected)
      - a free-text "Cover Letter" box?       -> kind = "text"
      - a dedicated file upload (distinct
        from the résumé's)?                  -> kind = "file"
  → (first time only, cached for the rest of this run)
      ApplicationFlowManager._generate_cover_letter()
          - no job description available for this run?  -> leave it unfilled,
            never invent one; a still-required field surfaces through the
            existing missing-required-field/validation checks like any other
            unanswerable field
          - generate_cover_letter_text(profile, job_description)
              -> one LLM call (task="cover_letter_generation"), grounded ONLY
                 in the real job description + the user's own Master Profile
  → kind == "text":  ATSAdapter.fill_cover_letter_field(locator, letter_text)
     kind == "file":  write_cover_letter_docx(letter_text) -> a local temp
                       .docx file -> ATSAdapter.fill_cover_letter_field(locator, temp_path)
  → ... rest of the application proceeds exactly as before (fill, navigate,
       submit/review, exactly as today) ...
  → once the run is over (any terminal outcome — applied, needs_review,
    failed, cancelled, or left open for copilot review), the temp file (if
    one was created) is deleted. Nothing else is ever touched: the user's
    real résumé (`self.resume_document`) and Master Profile are never
    written to, read destructively, or deleted by any of this.
```

## Files created

| File | Purpose |
|---|---|
| `backend/automation/applications/cover_letter.py` | The generator: `generate_cover_letter_text()` (one LLM call, grounded in job description + Master Profile) and `write_cover_letter_docx()` (renders the letter to a throwaway local `.docx` temp file for the file-upload case). |
| `backend/automation/tests/test_cover_letter.py` | Tests for detection (selectors.py/base.py), generation/rendering (cover_letter.py), and orchestration (ApplicationFlowManager) — see "Testing" below. |
| `cover-letter-auto-generation.md` | This file. |

## Files modified

| File | What changed |
|---|---|
| `backend/automation/browser/selectors.py` | Added `COVER_LETTER_FILE_INPUT_HINTS` and `find_cover_letter_file_input(page)` — finds a file input specifically for a cover letter, distinct from the résumé's `find_file_upload_input()`. Only ever matches when **at least two** file inputs exist on the page (a single file input is always the résumé's, never double-purposed) and one's `name`/`id`/`aria-label` mentions a cover-letter hint. |
| `backend/automation/ats/base.py` | Added `COVER_LETTER_TEXT_HINTS` and three new concrete (non-abstract) `ATSAdapter` methods, inherited by every adapter with no per-adapter changes needed: `find_cover_letter_text_field()` (a free-text box, via the same `_question_text_for()` label-reading helper `upload_resume`'s neighbors already use), `find_cover_letter_upload_input()` (delegates to the new selector), `detect_cover_letter_field()` (text checked first, then file; `(None, None)` when the page asks for neither — by far the common case), and `fill_cover_letter_field(locator, value)` (routes through the existing `describe_field()`/`fill_field()` pipeline — the same one `upload_resume` uses — and marks the element examined so the generic label/name sweep never treats it as an ordinary unmapped screening question and sends it to the answer engine a second time). |
| `backend/app/ai/llm/registry.py` | Added one new `TaskRoute`: `"cover_letter_generation"` (gpt-4.1-mini, temperature 0.5 for natural phrasing, max_tokens 900, plain prose not JSON). |
| `backend/automation/applications/application_flow_manager.py` | Added a `job_description: str | None = None` constructor parameter and three new instance fields (`_cover_letter_text`, `_cover_letter_generation_attempted`, `_cover_letter_temp_path`). Added `_handle_cover_letter_if_requested()` (called once per page, right after résumé upload, before the generic fill sweep), `_generate_cover_letter()`, `_cover_letter_file_path()`, and `_cleanup_cover_letter_file()`. `_finish_browser_session()` now calls the cleanup method in both its branches (browser closed, and browser kept open for review) — see "Cleanup" below for why both. |
| `backend/app/api/applications.py` | `_run_application()` now passes its existing `job_description` local (already resolved from `ApplicationStartRequest.job_description` and already passed to `ApplicationAnswerEngine`) into `ApplicationFlowManager(...)` too — the same value, not a second lookup. |

## Cover-letter detection

`ATSAdapter.detect_cover_letter_field()` runs once per page (inside
`ApplicationFlowManager._process_page`, right after the résumé-upload step),
and only ever finds something on pages that genuinely ask for one:

1. **Text first**: any `<textarea>`/`<input type="text">` whose label (read
   via the same ARIA/`<label for>`/wrapping-`<label>` lookup
   `_question_text_for()` already provides for the vision fallback) or
   `name` attribute mentions "cover letter". Checked first because a page
   offering a dedicated text box wants prose typed into it, not a file.
2. **File second**: `find_cover_letter_file_input()` — only returns a match
   when the page has **two or more** file inputs and one's `name`/`id`/
   `aria-label` mentions a cover-letter hint. A page with exactly one file
   input never triggers this; that one is always the résumé's.

If neither matches, `(None, None)` is returned and the rest of the run
proceeds completely unchanged — no generation, no LLM call, nothing filled.
This is what "without changing or breaking the current application process"
means concretely: every existing code path (résumé upload, personal info,
screening questions, navigation, submission) is untouched, and the new logic
is purely additive, gated on a real field actually being found.

## Job description and Master Profile usage

- **Job description**: reused, not re-fetched. `POST /applications/start`
  already accepts an optional `job_description` hint
  (`ApplicationStartRequest.job_description`) that `_run_application()`
  already threads into `ApplicationAnswerEngine` for screening-question
  answers. The exact same value is now also passed to
  `ApplicationFlowManager`. If it's `None` (no hint was supplied for this
  run), `_generate_cover_letter()` logs that and returns `None` rather than
  inventing a job description — a cover-letter field found on the page is
  then simply left unfilled, and if it was required, the run's existing
  missing-required-field/validation-error checks catch it exactly like any
  other field automation couldn't answer. No new job-description storage or
  scraping was added.
- **Master Profile**: `ApplicationFlowManager.profile` — the same
  `CandidateProfile` ORM object every other fill path in the run already
  reads (`profile_repository.get_by_user_id()` in
  `app/api/applications.py`). `cover_letter.py::_profile_facts()` reads
  `full_name`, `current_role`/`current_company`, `years_of_experience`,
  `professional_summary`, and a flattened view of `skills` (programming
  languages, frameworks, tools, technical skills) directly off it — no new
  query, no second profile fetch.

## LLM generation

One call per application run (never per page, even on a multi-page form
that asks for a cover letter on more than one page — see "caching" below),
via the same `generate_answer(task=..., prompt=..., system=...)` convention
every other part of automation uses (`automation/interfaces.py`, backed by
`app/ai/llm/router.py`) — no new LLM client, no bespoke call path.

`SYSTEM_PROMPT` (in `cover_letter.py`) enforces the same "never invent"
discipline as every other LLM-facing prompt in this codebase (see
`resume_parser.py`): only the given job description and candidate facts may
be used, no placeholder text, no fabricated employer/title/skill, plain
prose only (3-4 short paragraphs, ~350 words).

**Caching**: `ApplicationFlowManager._cover_letter_generation_attempted`
ensures generation is attempted at most once per run — the result (success
or `None`) is cached in `_cover_letter_text` and reused if a later page asks
for a cover letter again.

## Text fields vs. file uploads

Both go through the exact same `ATSAdapter.fill_cover_letter_field()` →
`describe_field()`/`fill_field()` pipeline every other field fill in this
codebase already uses (the same one `upload_resume()` uses) — `fill_field()`
dispatches on the field's actual DOM shape, so no separate "is this a file"
branch was needed:

- **Text field**: the generated letter string is passed straight through as
  the value.
- **File upload**: the letter is first rendered to a local temporary
  `.docx` file (`write_cover_letter_docx()`, using `python-docx` — already a
  project dependency, no new one added), and that file's path is passed as
  the value, exactly like `upload_resume()` passes a résumé's `local_path`.

## Temporary file lifecycle and cleanup

**No new database table, column, or row is created for a generated cover
letter.** This was a deliberate design choice, not an oversight:

- For the **text-field** case, nothing is persisted beyond the ordinary
  `FieldFillResult` already tracked for every other field — there's no file
  at all to clean up.
- For the **file-upload** case, the generated `.docx` is written straight to
  a plain OS temp file via Python's `tempfile` module — **not** through
  `ProfileDocument`/`document_storage.py`/`StorageBackend` at all. This
  means it never appears in the user's permanent document library
  (`GET /profile/documents`), was never a candidate for confusion with a
  real uploaded cover letter, and there is nothing durable to delete
  afterward except that one file on disk.

**Deletion**: `ApplicationFlowManager._cleanup_cover_letter_file()` deletes
the temp path (`Path.unlink(missing_ok=True)`) and is called from
`_finish_browser_session()` — in **both** of its branches (the browser is
about to be closed, or the browser is being deliberately kept open for a
human to review/submit). This is safe in both cases: by the time
`fill_cover_letter_field()`'s `set_input_files()` call returns, Playwright
has already read the file's bytes into the browser/page for the eventual
multipart upload — nothing in this codebase ever re-reads the source path
afterward, including a later resumed run (which builds a brand-new
`ApplicationFlowManager` and would generate its own fresh temp file if it
needed one again). So "after the application is successfully submitted" (the
literal ask) is satisfied as the common case, and the same safe cleanup also
runs for every other terminal outcome rather than leaking a temp file
whenever a run doesn't end in `"applied"`.

**Never touched, by construction**: `self.resume_document` (the user's real
résumé `ProfileDocument`) and `self.profile` (the Master Profile) are never
passed to any delete/cleanup path anywhere in this feature — the only path
ever deleted is `self._cover_letter_temp_path`, a value this same run's own
`write_cover_letter_docx()` call created a few lines earlier.

## API, database, dependency, and storage changes

- **API**: none. No new endpoint — this is internal to the existing
  `POST /applications/start` background run.
- **Database**: none. No migration, no new column, no new table.
- **Dependencies**: none. `python-docx` (renders the temp file) is already
  in `requirements.txt`; the LLM call reuses the existing `openai` provider
  via the existing router.
- **Storage**: none of the existing storage abstractions
  (`StorageBackend`/`document_storage.py`) are used for the generated
  letter — see "Temporary file lifecycle" above for why.

## Testing

`backend/automation/tests/test_cover_letter.py` (22 tests):

- `find_cover_letter_file_input` against real rendered HTML (Playwright
  `page.set_content()`, same convention as `test_selectors.py`): finds a
  second, hint-matching file input; never treats a lone file input as a
  cover-letter field; returns `None` when nothing matches.
- `ATSAdapter.detect_cover_letter_field()` / `fill_cover_letter_field()`,
  exercised through `GenericAdapter` (same convention as
  `test_generic_adapter.py`, since these are concrete base-class methods
  every adapter inherits unchanged): no-field case, text-box detection and
  fill, text-preferred-over-file when both exist, file-input detection and
  upload, and the "marks the element examined" behavior.
- `cover_letter.py`'s `generate_cover_letter_text()` (LLM call mocked —
  asserts the right task/prompt content, including the job description and
  profile facts) and `write_cover_letter_docx()` (real temp-file round trip
  through `python-docx`).
- `ApplicationFlowManager`'s orchestration, with a mock adapter (no real
  browser): does nothing when no field is detected; generates once and fills
  a text field; generation happens at most once across multiple pages;
  leaves the field unfilled (never invents) when no job description is
  available or generation fails; materializes and reuses one temp file
  across pages for the file case; cleanup deletes the temp file and leaves
  `resume_document` untouched; `_finish_browser_session` runs cleanup in
  both its close and keep-open-for-review branches.

All pre-existing test suites relevant to this change were re-run and remain
green: `test_selectors.py` (58), `test_generic_adapter.py`,
`test_workday_adapter.py`, `test_greenhouse_adapter.py`,
`test_lever_adapter.py`, `test_field_handlers.py`,
`test_field_handlers_phase8.py`, `test_field_mapper.py` (206 combined),
`tests/test_applications_api.py` (45), and the full `backend/tests/`
directory (309).

## Deliberate scope decisions

- **Deterministic path only.** This codebase has two separate auto-apply
  engines (see this repo's `ApplicationFlowManager` vs.
  `AutonomousAgentLoop`); only the deterministic, per-ATS-adapter one was
  changed, by explicit choice, to keep this change contained and reviewable.
- **No résumé-style "re-attachment" recheck for the cover letter.** Résumé
  uploads have a documented React-hydration bug where a client-rendered form
  can silently drop an already-verified upload
  (`ATSAdapter.ensure_resume_attached()`), rechecked at the very end of every
  run. The cover-letter file upload does not get an equivalent recheck — a
  deliberate simplification to keep this change small; worth adding later if
  the same drop-on-hydration behavior is ever observed for a cover-letter
  input specifically.
