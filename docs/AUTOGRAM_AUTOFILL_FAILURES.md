# AUTOGRAM Autofill — Failure Log

Format per entry:
```
Failure ID:
Platform:
URL:
Field:
Expected:
Actual:
Failure Category:
Root Cause:
Existing Behavior:
Fix:
Files Changed:
Regression Test:
Status:
```

## Entries

```
Failure ID: B-001
Platform: generic (any ATS using <input type="month"> — common for "expected graduation" / cert expiry)
URL: local fixture
Field: Expected graduation (input type=month)
Expected: field filled with 2026-05
Actual: no_handler_matched, field never touched, 0 attempts of value ever applied
Failure Category: FIELD_NOT_DETECTED / WRONG_FIELD_CLASSIFICATION (no handler claimed the field at all)
Root Cause: DateHandler.supports() only matched input_type == "date"; TextInputHandler's _TEXT_FILLABLE_INPUT_TYPES set does not include "month" either, so the field fell through every handler in the registry with no fallback.
Existing Behavior: 100% failure rate on every input[type=month] field regardless of candidate data completeness.
Fix: Extended DateHandler to also claim input_type == "month", added _coerce_month_string() (YYYY-MM normalization from bare year, full date, "Month YYYY", "MM/YYYY"), fill/verify branch on field.input_type.
Files Changed: backend/automation/forms/field_handlers.py
Regression Test: backend/automation/tests/test_date_coercion.py (test_coerce_month_string, test_month_input_now_has_a_handler_and_fills_correctly)
Status: FIXED

Failure ID: B-002
Platform: generic (react-datepicker / MUI / AntD style text-input date pickers that open a calendar popup on focus/typing)
URL: local fixture
Field: Available start date (text input + JS-driven calendar popup, popup does not auto-close on blur)
Expected: leftover popup would block the NEXT field's fill (click intercepted)
Actual: NOT reproduced — human_type()'s press_sequentially() focuses the element directly rather than relying on a successful click(), so a leftover full-viewport overlay from the previous field does not block the next field's typed fill in this fixture.
Failure Category: N/A (hypothesis tested, not confirmed)
Root Cause: N/A
Existing Behavior: Text-input date-picker widgets fill correctly value-wise via TextInputHandler even with an undismissed popup left open; DateHandler still only handles the native date/month controls, not this pattern, so this is coverage/observability, not a functional bug found.
Fix: None needed for the scenario tested. Note: a click-based handler (RadioHandler/CheckboxHandler/ComboboxHandler option click) immediately after a leftover full-screen overlay is a still-untested combination — worth a follow-up adversarial fixture.
Files Changed: none
Regression Test: backend/automation/tests/test_datepicker_popup_overlay.py (documents the tested-and-passing scenario as a capability pin)
Status: NO BUG FOUND (documented for coverage)

Failure ID: B-003
Platform: generic (react-select-style single dropdown "Other" reveal, and native checkbox-group "Other" reveal)
URL: local fixtures
Field: "How did you hear about us?" dropdown -> "Please specify" text field; checkbox-group "Other" -> "Please specify" text field
Expected: selecting/checking "Other" mounts a new field that didn't exist at scan time; that field is then discoverable and fillable; switching away unmounts it cleanly
Actual: NOT reproduced as a bug — ComboboxHandler's real click() and CheckboxHandler's native check()/uncheck() both correctly fire the page's own listeners, and describe_field() correctly re-resolves a freshly-mounted field on a fresh locator call.
Failure Category: N/A (hypothesis tested, not confirmed)
Root Cause: N/A
Existing Behavior: confirmed correct at the field-handler level for both the single-dropdown and checkbox-group shapes.
Fix: none needed. This closes a real coverage gap — previously this pattern was only asserted structurally in application_flow_manager.py's docstring (the re-scan loop), never at the field_handlers.py unit level.
Files Changed: none
Regression Test: backend/automation/tests/test_conditional_other_reveal.py, backend/automation/tests/test_checkbox_group_other_reveal.py
Status: NO BUG FOUND (documented for coverage)

Failure ID: B-004
Platform: generic (any career site embedding a third-party résumé-upload widget — e.g. Filestack/Uploadcare-style — in its own iframe, separate from the main application form's iframe/page)
URL: local fixture
Field: résumé file input, rendered inside iframe #2 while the rest of the form (5 fields) is inside iframe #1
Expected: adapter.upload_resume() finds and fills the file input regardless of which frame it lives in
Actual: upload_resume() -> False, résumé never found or uploaded, 100% of the time on this shape
Failure Category: FILE_UPLOAD_FAILURE / IFRAME_FAILURE
Root Cause: `resolve_form_root` correctly picks the frame with the MOST form controls for the rest of the form (by design, to avoid an incidental chat-widget iframe winning) — but an upload-widget iframe genuinely has only ~1 control (the file input itself), so it can never win that comparison. `ATSAdapter._find_resume_input` (automation/ats/base.py) only ever searched `self.form_root` — first directly, then again after clicking an upload-trigger button found on that SAME root — never any other frame on the page.
Existing Behavior: single-root search, trigger-click fallback still scoped to the same root.
Fix: after form_root (and its trigger) both come up empty, `_find_resume_input` now falls back to scanning every OTHER frame on `self.page` directly for a bare file input. No change to `resolve_form_root` or any Session-A-owned file — the fix is entirely in `automation/ats/base.py`, which no one had claimed.
Files Changed: backend/automation/ats/base.py
Regression Test: backend/automation/tests/test_resume_upload_separate_iframe.py
Status: FIXED
```

---
Failure ID: A-001
Platform: generic (any ComboboxHandler/ReactSelectHandler/CountryPickerHandler dropdown)
URL: job-boards.greenhouse.io/warp/jobs/4324888004 (reproduction), generalizable
Field: any custom dropdown with two options where one's text contains the other's (e.g. "Contract" / "Contract to Hire")
Expected: exact match selected
Actual: first DOM-order match selected (could be the containing option, not the exact one)
Failure Category: DROPDOWN_FAILURE / COMBOBOX_FAILURE
Root Cause: `_find_matching_option` returned the first option satisfying `_values_match` (bidirectional substring), so a containing option earlier in DOM order beat the exact match later in DOM order. Previously patched only for country (CountryPickerHandler + dial codes); root cause was generic.
Existing Behavior: single-pass, first-match-wins across all _DropdownHandler subclasses.
Fix: `_find_matching_option` now does exact-normalized-match pass first (across ALL options), falls back to original first-loose-match pass only if no exact match exists. Strictly additive — cannot regress any case with no exact match.
Files Changed: automation/forms/field_handlers.py
Regression Test: automation/tests/test_option_ranking.py (5 tests)
Status: FIXED

---
Failure ID: A-002
Platform: generic (any native `<input type=date>` fed a résumé-derived start/end date)
URL: n/a (unit-level; applies to education/experience date fields on any ATS)
Field: education/experience start_date / end_date rendered as a native date input
Expected: partial dates ("2018", "2018-08") fill the native date input (defaulted to day 01)
Actual: `_coerce_date_string` returned the partial string unchanged; native `type=date` silently rejects a non-YYYY-MM-DD value (fill() neither raises nor sets anything) — field left blank
Failure Category: DATE_FAILURE
Root Cause: `_coerce_date_string`'s format list had no case for bare-year or year-month, despite `EducationEntry.start_date`/`ExperienceEntry.start_date` being documented as commonly exactly that shape (résumés rarely give a full date).
Existing Behavior: DateHandler.fill()/verify() both route through `_coerce_date_string`.
Fix: added `\d{4}-\d{2}` -> `+"-01"` and `\d{4}` -> `+"-01-01"` cases.
Files Changed: automation/forms/field_handlers.py
Regression Test: automation/tests/test_date_coercion.py (6 tests)
Status: FIXED

---
Failure ID: A-003
Platform: generic (Greenhouse/Lever/any ATS routed through FieldMapper, i.e. the deterministic ats/base.py path)
URL: n/a (unit-level; label-vocabulary gap)
Field: phone ("Telephone"/"Telephone Number"/"Cell Phone"/"Cell Number") and first_name ("Forename")
Expected: maps to the ("phone"/"first_name", 0.9) attribute, same as `question_ontology.classify_field` (autonomous-agent path) already does for these exact labels
Actual: FieldMapper.map_field() returned None — field left unfilled, unmapped
Failure Category: WRONG_FIELD_CLASSIFICATION (field silently undetected)
Root Cause: `FIELD_SYNONYMS["phone"]` had no "telephone" entry, and critically "telephone" does NOT contain "phone" on a word boundary (word-boundary matching, by design, to avoid false positives like "state" inside "States" — see `_synonym_pattern` docstring), so it could never match via the existing "phone" synonym either. "cell phone"/"cell number" had no entry at all. `FIELD_SYNONYMS["first_name"]` had no "forename" entry. The two classification layers (question_ontology.py for the autonomous agent vs FIELD_SYNONYMS for the deterministic ATS-adapter path) had drifted apart on vocabulary coverage.
Existing Behavior: FieldMapper.map_field() is a static synonym-table lookup; question_ontology.py already covered these labels for the OTHER (autonomous) code path, confirmed via test_field_label_generalization.py passing already.
Fix: added "telephone", "telephone number", "cell phone", "cell number" to FIELD_SYNONYMS["phone"]; added "forename" to FIELD_SYNONYMS["first_name"]. Purely additive (new alternation branches only add matches, never remove any — verified no regression across 110 related tests).
Files Changed: automation/forms/field_mapper.py
Regression Test: automation/tests/test_field_mapper.py (3 new tests: test_telephone_matches_phone, test_cell_phone_and_cell_number_match_phone, test_forename_matches_first_name)
Status: FIXED

---
Failure ID: A-004
Platform: generic (VirtualizedListboxHandler — long, always-expanded role="listbox" panels: country/university/location lists)
URL: n/a (unit-level; same bug class as A-001, different call site)
Field: any virtualized listbox with two options where one's text contains the other's
Expected: exact match preferred
Actual: first-loose-match-wins, same root cause as A-001 but this call site was missed in that fix
Failure Category: DROPDOWN_FAILURE
Root Cause: `VirtualizedListboxHandler._find_option` had its own independent first-`_values_match`-wins loop, not routed through `_find_matching_option`.
Existing Behavior: n/a
Fix: same two-pass (exact-first, then original loose fallback) pattern applied directly to `_find_option`.
Files Changed: automation/forms/field_handlers.py
Regression Test: automation/tests/test_option_ranking.py::test_virtualized_listbox_prefers_exact_match_over_a_containing_one
Status: FIXED

Audit note: every remaining `_values_match` call site in field_handlers.py was checked — the rest are either (a) comparator functions (`_matches`) passed INTO the now-fixed `_find_matching_option`/`_search_with_scrolling`, so already covered, or (b) `NativeSelectHandler._resolve_one`, which already does full tiered exact-before-loose matching AND refuses on ambiguity (the best-designed matcher in the file — `option_matching.py::match_option`, used by the answer-engine/LLM path, is equally solid). The gap was specifically in the four `_DropdownHandler`/`RadioHandler`/`VirtualizedListboxHandler` loops that bypassed tiering entirely; all four are now fixed (A-001, A-004, + RadioHandler fix logged in mission doc).

---
Failure ID: A-005
Platform: generic (any ATS that renders its form inside a same-origin iframe — iCIMS confirmed live)
URL: n/a (unit-level; same class as the already-fixed `_still_missing_required` iframe blind spot)
Field: any conditional/follow-up field revealed after an earlier answer, on an iframe-rendered form
Expected: `ApplicationFlowManager._fill_page`'s round loop detects the new field and fills it (same as it already does for a top-level-rendered form)
Actual: the loop always read "revealed nothing" and silently stopped after round 0 — conditional follow-up fields on iframe-rendered ATS forms were never filled, with nothing in the log indicating why
Failure Category: DYNAMIC_FIELD_FAILURE / IFRAME_FAILURE
Root Cause: `capture_page_signature(page)` scans `.controls` from the top-level `page` only (`visible_control_identities` calls `page.evaluate(...)`, never a `Frame`). `_fill_page`'s before/after comparison for `newly_visible_controls()` always used this page-scoped signature, so on an iframe-rendered form both `before` and `after` always read `.controls == ()` — never differing, so `revealed` was always empty, indistinguishable from "genuinely nothing changed." The exact same root cause (`resolve_form_root` existing specifically because scanning `page` on an iframe-rendered form finds nothing) had already been fixed once for `_still_missing_required` (see that call site's comment in application_flow_manager.py) but was never applied to this second, independent call site.
Existing Behavior: `capture_page_signature(page)` — `.controls` always top-level-page-scoped, no parameter to change that.
Fix: `capture_page_signature` gained an optional `controls_root: Page | Frame | None` param (default `page` — every other call site unaffected). `_fill_page` now captures its round-loop before/after pair with `controls_root=adapter.form_root`. Critically, the value `_fill_page` RETURNS (used downstream as the `before` signature for `_advance_to_next_page`'s click/navigation detection) is recomputed as a plain page-scoped signature right before returning — mixing scopes there would make `.controls` differ on every comparison regardless of real navigation, a false-positive "page advanced" on every page-turn for an iframe-rendered form. Verified this specific risk is avoided (see test_a_page_scoped_before_against_an_iframe_scoped_after_would_have_false_positived, which documents the exact failure mode being guarded against).
Files Changed: automation/applications/page_navigator.py, automation/applications/application_flow_manager.py
Regression Test: automation/tests/test_iframe_signature_scoping.py (6 tests)
Status: FIXED — verified via git-stash A/B comparison that `test_application_flow_manager.py`'s 36 pre-existing fixture-collision errors are byte-identical with and without this change (46 passed / 36 errors both ways); zero regression.
