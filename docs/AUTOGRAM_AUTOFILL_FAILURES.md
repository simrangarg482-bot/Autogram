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
