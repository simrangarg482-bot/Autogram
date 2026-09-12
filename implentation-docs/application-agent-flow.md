# Multi-Page Application Agent Flow

## Read this first: most of this was already built

Before writing any code, the existing deterministic auto-apply engine
(`POST /applications/start` → `ApplicationFlowManager` → per-ATS
`ATSAdapter`s in `automation/ats/`) was inspected in depth. It already
implements almost everything this task describes, in real depth:

| Ask | Already implemented as |
|---|---|
| Detect all form fields on the page | `ApplicationFlowManager._process_page`/`_fill_page`, per-widget introspection in `automation/forms/field_handlers.py::describe_field` |
| Understand what each field is asking | `ATSAdapter._question_text_for` (ARIA/`<label>`/nearby-text recovery) |
| Map fields to the Master Profile | `automation/forms/field_mapper.py::FieldMapper` — a real "website field → canonical field" synonym table (see below) |
| Open-ended questions via LLM + job description + profile | `automation/forms/answer_engine.py::ApplicationAnswerEngine` |
| Stop instead of guessing on low-confidence/unknown answers | `ANSWER_REVIEW_CONFIDENCE_THRESHOLD` gate in `automation/ats/base.py`, both for the text answer engine and the vision fallback |
| Fill text/dropdown/radio/checkbox/date/file fields | `automation/forms/field_handlers.py`'s handler registry (`TextInputHandler`, `NativeSelectHandler`, `ComboboxHandler`, `RadioHandler`, `CheckboxHandler`, `ToggleHandler`, `VirtualizedListboxHandler`, `FileUploadHandler`, ...) |
| Detect and fill dynamic/conditional fields revealed by an answer | `ApplicationFlowManager._fill_page`'s round loop — compares control identities before/after each pass, keeps filling while new controls keep appearing |
| Confirm a page is ready / never assume a click means the next page loaded | `automation/applications/page_navigator.py` — `PageSignature`/`advance_to_next_page` structurally PROVE navigation happened, not just "the click didn't throw" |
| Human review before submit | `NEEDS_REVIEW`/`COPILOT_REVIEW`/`manual_required` statuses, the visible-browser "manual review handoff" (`should_keep_browser_open`), `POST /applications/{id}/approve` |
| Submit only through the existing flow | `submit_and_confirm` — the one place a submit is ever clicked, shared by autopilot and human-approved copilot submission |
| 4-6+ page applications | `MAX_PAGES = 20` page cap (not an assumed length — the loop ends when the adapter says so), already explicitly built and documented for Workday-style 4-6 page forms |

Given that, and the instructions to build on top of what's already working
and touch the minimum number of files, this change is **additive, not a
rewrite**: it introduces one real gap that was missing — an explicit,
inspectable **Application State** object — and closes one specific,
concrete blind spot in the existing reliability logic (a low-confidence or
unanswerable field was invisible as *itself*; it only ever showed up folded
into the page-level "some required field is missing" message). Every other
box above was left exactly as it was.

## Live-tested against a real posting (Atlassian / iCIMS) — a second, more significant gap found and fixed

After the `ApplicationState` work below, the agent was run for real against a
live Atlassian job posting (`careers-apac-atlassian.icims.com`, an iCIMS
career site) using an already-configured account, Master Profile, and
résumé — in copilot mode (visible browser, never auto-submits: this
account's trust level is `FULL_MANUAL_REVIEW` and `"custom"`/`"icims"` were
never in `PUBLIC_ATS_PLATFORMS` anyway, so nothing could reach `AUTO_SUBMIT`
regardless of confidence).

**What happened**: the run correctly detected `icims` as the platform,
reached the real "Enter Your Information" page (an email field, a dropdown,
an "I accept" checkbox), and then filled **nothing** — 0/1 tracked fields,
0% confidence, straight to `needs_review`, even though the fields were
plainly visible in the screenshot.

**Root cause, confirmed with a direct Playwright probe against the live
page**: iCIMS renders its entire candidate-facing form inside an
`<iframe id="icims_content_iframe">`. Every single selector in
`automation/browser/selectors.py` and `automation/ats/base.py` queries
`page.locator(...)` — the TOP-LEVEL document only. The fields were real,
visible, and fillable; the agent was structurally blind to them, because
they don't exist anywhere in the DOM it was ever looking at. Confirmed live:
`page.frame_locator('#icims_content_iframe').locator('input').count()` found
4 real inputs and 1 select; the top-level page had none of its own.
(`automation/ats/detector.py` already had this iframe's selector as an ATS-
*detection* hint — nobody had ever wired the *fill* pipeline to use it.)

**The fix** (same 3 files as `ApplicationState` above, no new files needed):
`automation/browser/selectors.py::resolve_form_root(page)` — compares how
many real form controls (`input`/`select`/`textarea`, deliberately
**excluding `button`**) exist on the top-level page versus in each of its
iframes, and returns whichever has more. `ATSAdapter.form_root` (a property
in `automation/ats/base.py`, recomputed on every access rather than cached,
since a later page of the same multi-page application can switch between
rendering directly and rendering inside an iframe) is `resolve_form_root(self.page)`.
Every field-scanning/filling call site already in `ATSAdapter` that used to
query `self.page` directly now queries `self.form_root` instead: the label
sweep, the name/placeholder sweep, résumé/cover-letter upload detection,
`find_next_control`/`find_submit_control`, `find_page_heading`/
`is_review_page`, and the unfilled-required-field scan. Three call sites in
`ApplicationFlowManager` that scan fields directly (`_still_missing_required`,
the final validation-errors check, and `_recover_before_retry`'s blocking-field
check) were switched to pass `adapter.form_root` instead of the raw `page`.
`self.page` itself is untouched and still used for everything that must
stay page-level: navigation, screenshots (`Frame` has no `.screenshot()`),
and overlay dismissal (a cookie-consent banner is injected by the parent
page, not the content iframe).

**Why "more controls," not "top-level has zero"**: the first attempt at this
fix gated purely on "does the top-level page have zero controls of its
own" — which the SAME live page immediately falsified. Atlassian's own nav
bar (`Products`, `Solutions`, `Get it free`, `Sign In`, ...) is made of real
`<button>` elements, so the top-level page never looked "empty" and the
iframe was never even considered. The corrected heuristic compares actual
field COUNTS and excludes `button` from what counts as a "form control" —
buttons are page chrome everywhere, not evidence of a form. This is captured
directly as a regression test (`test_resolve_form_root_is_not_fooled_by_ordinary_page_chrome_buttons`)
so it can't silently regress again.

**Verified live, twice** — before and after the corrected heuristic, against
the exact same real Atlassian posting, through the actual product (not a
synthetic page): the run went from `0/1 tracked fields filled, confidence
0.00, needs_review` to `3/4 tracked fields filled, confidence 0.75,
copilot_review` — the email field correctly filled with the real profile
email, the dropdown answered via the LLM answer engine, and the consent
checkbox ticked, confirmed both in the run's log lines and visually in its
screenshot. The one field still counted against confidence is the résumé
upload attempt, which correctly fails on this particular page (there is no
résumé field on it at all yet) — a pre-existing, unrelated scoring quirk
(a résumé is unconditionally attempted on page 1 of every application,
counted whether or not that specific page actually has a file input), not
something this change introduced. The run stopped at `copilot_review` with
the browser left open for a human to review and submit — never auto-clicked
Submit or Next, exactly as designed.

**Files touched for this fix** (all already listed above; no additional
files): `automation/browser/selectors.py` (`resolve_form_root`, new),
`automation/ats/base.py` (`form_root` property + the call-site switch),
`automation/applications/application_flow_manager.py` (3 call-site
switches), plus `automation/tests/test_selectors.py` (+5 tests pinning the
exact live bug and the fix).

**Known, deliberately out-of-scope follow-ups** (found during this same live
test, not fixed here to keep this change contained):
- `find_human_gate` (login-wall detection), CAPTCHA detection, and
  submission-confirmation detection are still page-level only. A login
  form embedded the same way iCIMS's application form is would not
  currently be recognized as a human gate — CAPTCHA detection is fine as-is
  (the `<iframe>` element itself, e.g. hCaptcha's, always lives in the
  top-level DOM regardless of what it embeds), but login-wall and
  confirmation detection may need the same `form_root` treatment if a
  platform embeds either of those inside a content iframe too.
- On this specific posting, the "Next" button was visibly enabled in the
  final screenshot but the run treated the page as final (no further
  navigation) rather than clicking it — worth a closer look at
  `is_final_page`/`find_next_control` timing (it may be a race between the
  consent checkbox's change event enabling the button and the moment
  `is_final_page()` is checked) if a similar iCIMS posting shows the same
  behavior again.
- ~~`ATSAdapter.form_root` recomputes `resolve_form_root` ... on every single
  access rather than caching it per page-processing pass~~ — **fixed, see
  "form_root caching" below.**

## `form_root` caching (follow-up to the fix above)

The live test above surfaced a real performance cost: `form_root` was an
uncached property, so every one of its many reads while processing one page
(the label sweep, the name/placeholder sweep, résumé/cover-letter upload
detection, navigation-control lookup, the unfilled-required-fields scan) re-ran
`resolve_form_root` from scratch — an iteration over every frame on the page,
including the hCaptcha frames, each with its own `.count()` DOM round trip.
Harmless to the *outcome* (confirmed live, see above), but a real, avoidable
cost on any page with more than a couple of frames.

**The fix** (same two files as the fix above, no new files): `ATSAdapter`
(`automation/ats/base.py`) gained a `self._form_root_cache` instance attribute
(`None` initially, set in `__init__`). The `form_root` property now returns
the cached value if one exists, computing and caching it only on the first
access after a reset. A new `reset_form_root_cache()` method clears it.
`ApplicationFlowManager._process_page` (`automation/applications/
application_flow_manager.py`) calls `adapter.reset_form_root_cache()` once,
at the very start of processing each page — before any field scanning
happens on it — so `form_root` is resolved at most once per page and reused
for everything else that page needs, while a later page that switches
between rendering directly and rendering inside an iframe is still resolved
fresh rather than reusing a stale value from the page before it.

Deliberately minimal: no change to `resolve_form_root` itself, to what
counts as a "form control", to any field-detection/fill/navigation/
submission logic, or to any other file. The cache is invalidated exactly
once per page (in `_process_page`, the one place every page's processing
begins) — never mid-page, which is what keeps `form_root` stable for the
whole page while it's being scanned and filled.

**Verified live again, on the exact same real Atlassian/iCIMS posting**:
identical outcome (`3/4 tracked fields filled, confidence 0.75,
copilot_review` — email, the dropdown, and the consent checkbox all still
correctly filled, confirmed in the run's log lines), with the fill step
itself measurably faster: **~13.4s** (`resume_upload_failed` at `+13.0s` to
`step_0_filled` at `+26.4s`) versus **~33s** for the identical step before
this change (`resume_upload_failed` at `+25.1s`/2s-in to `step_0_filled` at
`+59.6s`/33s-later, in the pre-caching runs) — over 2x faster on the exact
step that reads `form_root` repeatedly, with no change in behavior.

**New tests** (`automation/tests/test_generic_adapter.py`, +3): `form_root`
is resolved once and the same object is returned across repeated accesses
(a spy on `resolve_form_root` asserts it's called exactly once);
`reset_form_root_cache()` forces the next access to re-resolve (spy asserts
exactly two calls across a reset); a brand-new adapter's cache starts empty
so the very first access still resolves correctly. Re-ran and confirmed
still green: `test_generic_adapter.py` (14), the same broad automation suite
as the fix above (250), the full multi-page `test_application_flow_manager.py`
(46/46 in isolation — same pre-existing, unrelated fixture-collision caveat
as before), and the full `backend/tests/` directory (309).

## What was actually missing, and what was added

**1. No single "Application State" object.** The pieces already existed —
`ApplicationFlowManager._steps_completed` (a flat list of step names),
`LIVE_RUN_STATE` (an in-memory dict keyed by `application_id`, holding only
the single most recent step), the `all_results: list[FieldFillResult]`
local variable threaded through the page loop, and (only once a run
concludes) the `Application`/`AutomationRun` database rows — but nothing
tied "current page / completed pages / every field's outcome / what still
needs a human / current validation errors / a decision timeline" together
as one coherent, named thing. **Added**: `ApplicationState` (new module,
see below), owned by `ApplicationFlowManager` as `self.state`.

**2. A low-confidence or unanswerable question was indistinguishable from
any other unfilled field.** `automation/ats/base.py`'s answer-engine and
vision-fallback passes already refuse to type in an answer they aren't
confident in — but the resulting `FieldFillResult` only ever said
`filled=False`, with nothing recording *why* (no info found? not
confident enough? typed but the page rejected it?). All three collapsed
into the same shape, and the ONLY way this ever surfaced was the page-level
"Required field(s) could not be filled: ..." message once the whole page
was done. **Added**: `FieldFillResult.unfilled_reason` (`"no_answer_available"`
/ `"low_confidence"` / `"fill_failed"`), set at every place this already
happens, and fed into `ApplicationState.pending_user_input` — literally the
"stop and ask the user instead of guessing" fields, now visible as a list
with a reason and a page number, not just inferred from a missing-fields
sentence at the end.

Everything else — field detection, field-type handling, dynamic/conditional
field handling, field-mapping-by-meaning, navigation-proof, human review,
submission — needed no changes and was **not** touched.

## Files created

| File | Purpose |
|---|---|
| `backend/automation/applications/application_state.py` | `ApplicationState` — the explicit state object: current/completed pages, every field's latest outcome, pending-user-input list (with reasons), current validation errors, and a capped decision timeline. Pure bookkeeping — it never makes a decision or touches the page itself. |
| `backend/automation/tests/test_application_state.py` | Unit tests for `ApplicationState` in isolation (no browser). |
| `application-agent-flow.md` | This file. |

## Files modified

| File | What changed |
|---|---|
| `backend/automation/ats/base.py` | Added `FieldFillResult.unfilled_reason: str \| None = None` (backward-compatible — every existing 5-positional-arg construction site in the test suite is unaffected). Set it at the 5 places a field ends up unfilled: the two "no answer"/"low confidence" branches in `_fill_known_questions`'s answer-engine batch loop, the equivalent two branches in `fill_unfilled_fields_with_vision`, and both passes' "typed an answer but the field handler couldn't verify it" branch (`"fill_failed"`). No control-flow, threshold, or behavior change — purely a classification label on an outcome that already happens. **Live-test follow-up**: added `ATSAdapter.form_root` (a property, see below) and switched every field-scanning call site that used to query `self.page` directly — the label sweep, the name/placeholder sweep (`_stamp_candidates`), résumé/cover-letter upload+text-field detection, `find_next_control`/`find_submit_control`, `find_page_heading`/`is_review_page`, and `collect_unfilled_fields_for_vision` — to query `self.form_root` instead. `self.page` is untouched for screenshots, overlay dismissal, and URL/context logging. **Caching follow-up**: added `self._form_root_cache` (set in `__init__`) and `reset_form_root_cache()`; `form_root` now returns the cached value instead of recomputing `resolve_form_root` on every access. |
| `backend/automation/applications/application_flow_manager.py` | Added `self.state = ApplicationState(application_id=application_id)` in `__init__`. `checkpoint()` now also calls `self.state.record_decision(...)` and merges `self.state.snapshot()` into `LIVE_RUN_STATE` under a new `"application_state"` key (existing keys `last_step`/`page_number`/`updated_at` are untouched — purely additive, so `GET /applications/{id}/live` needed no changes at all: it already returns `LIVE_RUN_STATE`'s dict verbatim, `response_model=None`). `_process_page()` now records the `FieldFillResult`s produced on that specific page into `self.state` at every one of its three exit points (the two human-gate/CAPTCHA early stops, and the normal end of the page), not just the final one — a page that gets interrupted still reports what it actually got done. `_run_on_page()`'s existing missing-required-fields and validation-errors checks now also call `self.state.record_missing_required(...)`/`self.state.record_validation_errors(...)` — the exact same findings that already drive the `manual_required`/`needs_review` decisions, now also recorded on the state object. **Live-test follow-up**: `_still_missing_required`, the final validation-errors check, and `_recover_before_retry`'s blocking-field check now scan `adapter.form_root` instead of the raw `page`. **Caching follow-up**: `_process_page()` now calls `adapter.reset_form_root_cache()` once, at the very start of processing each page. |
| `backend/automation/browser/selectors.py` | **Live-test follow-up (new function)**: `resolve_form_root(page) -> Page \| Frame` — compares real form-control counts (`input`/`select`/`textarea`, never `button`) between the top-level page and each of its iframes, returning whichever has more. |
| `backend/automation/tests/test_application_flow_manager.py` | Added 4 tests confirming the wiring above (checkpoint feeds state and `LIVE_RUN_STATE`; missing-required/validation-errors feed `pending_user_input`/`validation_errors`) — `ApplicationState`'s own logic is tested in `test_application_state.py`, these only confirm the flow manager actually calls it. |
| `backend/automation/tests/test_selectors.py` | **Live-test follow-up**: added 5 tests for `resolve_form_root` — no-iframe case, a genuinely empty top-level page with an iframe form, the exact live bug (a nav bar full of `<button>`s must never count as "the page has a form"), an incidental iframe with fewer fields losing to a real top-level form, and picking the richer of two iframes. |
| `backend/automation/tests/test_generic_adapter.py` | **Caching follow-up**: added 3 tests for `form_root` caching — resolved once and reused across repeated accesses (spy asserts `resolve_form_root` called exactly once), `reset_form_root_cache()` forces a re-resolve (spy asserts exactly two calls across a reset), and a brand-new adapter's cache starts empty. |

No other file was touched. In particular: `app/api/applications.py`,
every `automation/ats/<platform>/*_adapter.py`, `automation/forms/field_mapper.py`,
`automation/forms/answer_engine.py`, `automation/forms/vision_fallback.py`,
`automation/applications/page_navigator.py`, and every `field_handlers.py`
widget handler are all unmodified — the existing auto-apply functionality
runs through exactly the same code it always did.

## Application State implementation

`ApplicationState` (`application_state.py`) is a plain dataclass, owned by
one `ApplicationFlowManager` instance for the lifetime of one run — in-memory
and per-run, same as `LIVE_RUN_STATE` already was, and for the same reason:
it's a best-effort, single-process observability view, not a second durable
record. `Application`/`AutomationRun` remain the actual database record of
what happened, written exactly as before once the run concludes.

```python
@dataclass
class ApplicationState:
    application_id: str
    current_page_number: int = 0
    pages_completed: list[PageRecord]            # page_number, fields_seen, fields_filled
    fields_processed: dict[str, str]              # field_key -> "filled" | "unfilled:<reason>"
    pending_user_input: list[PendingUserInput]    # field_key, reason, page_number
    validation_errors: list[str]
    decisions: list[Decision]                     # timestamp, event, page_number
```

Every method is additive bookkeeping only — it records what the flow
manager already decided or observed; it never blocks anything or makes a
decision of its own:

- `record_decision(event, page_number=...)` — one more entry in the run's
  own timeline, called from the exact same place `checkpoint()` already
  logs a step (`"navigated"`, `"step_0_filled"`, `"resume_uploaded"`,
  `"decision_auto_submit"`, ...), so no new call sites had to be threaded
  through the page loop.
- `record_page_results(page_number, results)` — folds one page's
  `FieldFillResult`s into `fields_processed`; a field that ends up `filled`
  clears any earlier `pending_user_input` entry for the same key (e.g. the
  vision pass rescuing on round 2 what the label sweep missed on round 1).
- `record_validation_errors(errors)` — replaces (not accumulates) the list,
  since these describe the form's current state, not a history.
- `record_missing_required(field_names, page_number=...)` — the same
  "required field, no value anywhere in the profile" finding that already
  drives `manual_required`, recorded as a `pending_user_input` entry with
  reason `"missing_required"`.
- `snapshot()` — a plain, JSON-ready dict, capped at the 50 most recent
  decisions so a long multi-page/retry run can't grow this unbounded in
  memory or over the wire.

`ApplicationFlowManager.checkpoint()` merges `state.snapshot()` into
`LIVE_RUN_STATE[application_id]["application_state"]` every time it's
called — which is already very often (every step, every page, every
decision) — so `GET /applications/{id}/live` (unchanged, `response_model=None`,
already returns this dict verbatim) immediately exposes the richer view
with no API change of its own.

## Page detection/processing logic (unchanged, for reference)

`ApplicationFlowManager._process_page` — one page, start to finish, per
call: settle the page → dismiss overlays → check for a human-only gate
(login/OTP — genuinely nothing fillable yet) → upload the résumé if this
page asks for one → fill a cover letter if this page asks for one → run
fill rounds until nothing new gets revealed (`_fill_page`'s
`MAX_FILL_ROUNDS` loop) → vision fallback over whatever is still empty →
check for a CAPTCHA (gates submission, not the fields, so it's checked
last) → report whether the adapter considers this the final page. This is
the existing multi-page loop (`ApplicationFlowManager.MAX_PAGES = 20`,
already built and tested against synthetic 1-5-page forms whose pages
genuinely turn — new heading, old fields gone, new ones appeared, button
relabels to "Submit"); nothing about its order or logic changed.

## Field understanding and mapping (unchanged, for reference)

`automation/forms/field_mapper.py::FieldMapper` is exactly the
"website field → understand meaning → canonical field" layer the task asks
for — and the task's own example is already covered verbatim:

```python
"first_name": ["first name", "given name", "legal first name"],
```

Checked in order of decreasing certainty (`name`/`id` attribute > label
text > placeholder > nearby text), so "Given Name", "First Name", and
"Legal First Name" all resolve to the same canonical `first_name` attribute
regardless of which one a given ATS happens to use. A label that matches no
synonym is genuinely novel/subjective territory and falls through to
`ApplicationAnswerEngine` (Master Profile + job description + LLM) instead
of ever being guessed by string-matching.

## Master Profile integration (unchanged, for reference)

Every fill path already reads the same `CandidateProfile` ORM object
(`ApplicationFlowManager.profile`, fetched once via
`profile_repository.get_by_user_id` in `app/api/applications.py`) — the
generic label/name sweep (`FieldMapper` → `_resolve_profile_value`), the
answer engine (`automation/forms/answer_engine.py`), and the vision fallback
all resolve against this one profile. Nothing in this change reads the
profile a second time or through a different path.

## AI answer generation (unchanged, for reference)

`ApplicationAnswerEngine.answer_batch` — one batched LLM call per form for
every genuinely novel/subjective question (`app/ai/llm/registry.py`'s
`"application_answer"` route), backed by a persistent per-user answer cache
so a repeated question across applications never costs a second LLM call.
The vision fallback (`"form_vision_answer"` route) is the last resort, for
fields nothing else could resolve at all. Both already gate on
`ANSWER_REVIEW_CONFIDENCE_THRESHOLD = 0.80` — this change only makes WHY a
gated answer was left blank visible (`unfilled_reason`), not the gate
itself.

## Validation logic (unchanged, for reference)

`find_unfilled_required_fields`/`find_validation_errors`
(`automation/browser/selectors.py`) run after every fill pass; a required
field with no value anywhere in the profile is `manual_required` (nothing
automation can do without a human), and a validation error the ATS itself
raised about something already filled is `needs_review` (never
auto-submitted over). Both checks are now also recorded onto
`self.state` (see "Files modified" above) in addition to driving the
same decisions they always did.

## Dynamic/conditional field handling (unchanged, for reference)

`ApplicationFlowManager._fill_page`'s round loop already handles this:
answering "Do you require sponsorship?" can reveal "Which visa do you
hold?", which didn't exist when the page was first scanned. Rounds compare
control IDENTITIES (never values) before/after each pass and keep going
while a pass reveals something new, stopping the moment one doesn't — so a
static page costs exactly one round, and a page that keeps revealing new
sections is a warning, not a silent miss.

## Browser/Incognito integration

Untouched by design, per the task's explicit instruction. `BrowserManager`
(`automation/browser/browser_manager.py`) — including its
CDP/incognito-attach mode, the visible-vs-headless split, and the "leave
the browser open for manual review" handoff (`should_keep_browser_open`,
`_OPEN_REVIEW_SESSIONS`) — is used exactly as it was before. `ApplicationState`
doesn't touch the browser at all; it only records what `ApplicationFlowManager`
already observed through it.

## Error and retry handling (unchanged, for reference)

Navigation is PROVEN, never assumed: `page_navigator.py::advance_to_next_page`
compares a structural signature of the page before and after a click, and a
click that doesn't verifiably change anything is retried once (only after
something about the page actually changed — an overlay dismissed, a
validation-flagged field got filled) before the run stops and hands over
with the form's own reason attached, exactly as before. Every "why did this
stop" reason (`missing_required`, `validation_errors`, a human gate, a
CAPTCHA, a navigation failure) is now additionally captured on
`self.state`, so an operator inspecting a stuck run gets the same reason
the end user does, plus which specific fields are implicated and why.

## API, database, dependency, and schema changes

- **API**: none. `GET /applications/{id}/live` is unchanged code
  (`response_model=None`, returns `LIVE_RUN_STATE[...]` verbatim) — it
  automatically exposes the new `"application_state"` key the moment a run
  populates it, with no route edit.
- **Database**: none. `ApplicationState` is in-memory and per-run, exactly
  like `LIVE_RUN_STATE` already was — no new table, column, or migration.
  Persisting it durably (e.g. folding a snapshot into `AutomationRun` at the
  end of a run) is a natural next step, deliberately left out of this
  change to keep it additive and reviewable.
- **Dependencies**: none.
- **Schema**: `FieldFillResult` gained one new optional field
  (`unfilled_reason`, default `None`) — backward compatible with every
  existing construction site (checked against the whole test suite; see
  "Testing" below).

## Testing

- `automation/tests/test_application_state.py` (8 tests, new): decision
  timeline, per-page field recording (filled clears an earlier pending
  entry), which `unfilled_reason`s become `pending_user_input` and which
  don't (`"fill_failed"` deliberately does not — it's a mechanical problem,
  not a "should a human decide" one), validation errors replace rather than
  accumulate, missing-required entries dedupe, snapshot shape, and the
  50-entry decision cap.
- `automation/tests/test_application_flow_manager.py` (+4 tests): confirms
  `checkpoint()` actually feeds `self.state` and `LIVE_RUN_STATE`, and that
  the missing-required/validation-error findings land on `self.state`.
- `automation/tests/test_selectors.py` (+5 tests, 63 total): `resolve_form_root`
  — see "Live-tested against a real posting" above for what each one pins.
- `automation/tests/test_generic_adapter.py` (+3 tests, 14 total): `form_root`
  caching — see "`form_root` caching" above for what each one pins.
- Re-ran, and confirmed still green after every round of changes above:
  `test_selectors.py` (63), `test_generic_adapter.py` (14),
  `test_workday_adapter.py`, `test_greenhouse_adapter.py`,
  `test_lever_adapter.py`, `test_field_handlers.py`,
  `test_field_handlers_phase8.py`, `test_field_mapper.py` (250 combined),
  `test_vision_pass_on_page.py`, `test_cover_letter.py`,
  `test_application_state.py`, and the full `backend/tests/` directory
  (309). `test_application_flow_manager.py` itself: 46/46 of its own tests
  pass in isolation; the file also has a **pre-existing**, order-dependent
  Playwright `sync_playwright()`/`requires_chromium` fixture collision when
  several browser-using test files run together in one session (documented
  in its own `conftest.py`) — confirmed unrelated to this change by
  reproducing the identical 36-error split both before and after every
  round of edits, and by running every affected test individually, where
  all pass.

**Beyond the unit/integration suite**, this was also verified against a
real, live external site (not a synthetic fixture) — see "Live-tested
against a real posting" and "`form_root` caching" above for the full
account: a real application run against a live Atlassian/iCIMS posting,
through the actual product end-to-end (real HTTP API, real database, real
browser via CDP, real LLM call for the open-ended dropdown question),
confirmed broken (0% confidence) before the fix and confirmed working (75%
confidence, fields correctly filled, visually verified in a screenshot)
after it — three times total: once per iteration of the heuristic, and
once more after adding the `form_root` cache, which reproduced the exact
same 75%-confidence result with the fill step measurably faster (~13s vs
~33s).
