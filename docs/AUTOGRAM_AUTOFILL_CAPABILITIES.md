# AUTOGRAM Autofill — Capability Matrix

| Capability | Supported | Tested | Reliability | Notes |
|---|---|---|---|---|
| Text input (label/aria/placeholder) | YES | YES | HIGH | field_mapper.py + describe_field; phone/first_name synonym gaps closed (telephone, cell phone/number, forename — A-003) |
| Native select | YES | YES | HIGH | NativeSelectHandler |
| react-select combobox | YES | YES | MEDIUM | ComboboxHandler; option-order ambiguity fixed for country, generalizable bug remains for other confusable-option dropdowns (see failures log) |
| Country picker (name+dial code) | YES | YES | HIGH | CountryPickerHandler + country_dial_codes.py (Sept 2026 fix) |
| Radio groups | YES | YES | HIGH | RadioHandler |
| Checkbox / checkbox groups | YES | YES | HIGH | CheckboxHandler |
| Autonomous agent select() on custom dropdown | YES | YES | MEDIUM | executor.py _select_custom_option (opens+clicks role=option, was calling select_option() before fix) |
| Demographics (gender/disability/veteran/race/hispanic) | YES | YES | HIGH | answer_engine.py, never LLM-answered, saved via Application Profile UI |
| DB session resilience across long browser runs | YES | YES | HIGH | answer_engine.py session_factory + _run_db retry-once |
| Date fields — native `<input type=date>` | YES | YES | HIGH | DateHandler + _coerce_date_string |
| Date fields — native `<input type=month>` | YES | YES | HIGH | DateHandler + _coerce_month_string (Sept 2026 fix — previously no_handler_matched 100% of the time, see B-001) |
| Date fields — text-input calendar-popup style (react-datepicker/MUI/AntD) | PARTIAL | PARTIAL | UNKNOWN | Falls through to TextInputHandler; value fills correctly and doesn't block the NEXT field (test_datepicker_popup_overlay.py), but no click-to-select-a-day / no explicit popup dismissal — untested against a click-based handler running right after a leftover overlay |
| File upload — same-frame / trigger-revealed input | YES | YES | HIGH | ATSAdapter._find_resume_input |
| File upload — widget in a SEPARATE iframe from the main form | YES | YES | HIGH | Sept 2026 fix (B-004) — cross-frame fallback in _find_resume_input |
| iframe forms — single form iframe vs. top-level | YES | YES | HIGH | resolve_form_root compares control counts |
| iframe forms — nested (iframe-in-iframe) | YES | YES | HIGH | page.frames is flat regardless of nesting depth; resolve_form_root already scans it correctly (verified — no bug found) |
| iframe forms — form iframe with an OTHER, low-control-count widget iframe alongside it (e.g. résumé upload) | YES | YES | HIGH | Sept 2026 fix (B-004) |
| iframe forms — conditional/follow-up field reveal, deterministic path | YES | YES | HIGH | A-005 fix: `_fill_page`'s round-loop signature now frame-scoped, was always blind before |
| iframe forms — autonomous agent (Launch Agent) path | NO (by design) | YES | N/A | extraction script never descends into an iframe; a form rendered entirely inside one observes as zero elements and the agent correctly REFUSES completion + escalates to a human (`content_frame_count` in page_classifier.py/observer.py) rather than silently failing. Real capability gap (cannot fill such forms), safely handled, not a bug. Architectural — out of scope for a bounded fix. |
| Cover-letter upload widget in a SEPARATE iframe from the résumé/main form | NO | NO | UNKNOWN | `find_cover_letter_upload_input` only searches `form_root` and only distinguishes resume-vs-cover-letter when BOTH file inputs are on the same root (needs ≥2 file inputs there to disambiguate) — same class of bug as B-004 but not yet fixed; lower real-world frequency (two separate third-party upload widgets on one posting is rare) so left as a documented gap rather than spec-fixed without a concrete repro |
| Shadow DOM | UNKNOWN | NO | UNKNOWN | needs investigation |
| Multi-step forms | YES | PARTIAL | MEDIUM | application_flow_manager.py |
