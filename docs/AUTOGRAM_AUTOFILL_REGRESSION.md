# AUTOGRAM Autofill — Regression Log

Baseline (2026-09-24, before this session's changes): `pytest automation/tests --ignore=test_chat_api_integration.py` → 25 failed (pre-existing, same set as clean HEAD), 1647+ passed, 158 skipped, ~550-567 errors (local Playwright `page`-fixture flakiness in full-suite runs, not present when files run individually — pre-existing, verified in prior session).

## Runs
(appended per fix)

- 2026-09-24 Session B, after B-001 (input[type=month] handler) + new test_datepicker_popup_overlay.py:
  `pytest test_date_coercion.py test_field_handlers.py test_field_handlers_phase8.py test_datepicker_popup_overlay.py test_option_ranking.py test_dropdown_live_form_shapes.py test_selectors.py -q`
  → 171 passed, 0 failed.

- 2026-09-24 Session A, after A-001/A-002/A-003/A-004 + RadioHandler exact-match fix:
  `pytest automation/tests --ignore=test_chat_api_integration.py -q`
  → 25 failed, 1663 passed, 156 skipped, 582 errors.
  Failed set is BYTE-IDENTICAL to the documented pre-existing clean-HEAD baseline (test_autonomous_loop.py FakeBrowserManager fixture gaps + test_chat_and_event_bus.py — unrelated to any field-handling change). Zero regressions.
  Error count (582 vs earlier-session baseline ~544-567) continues to track the known local Playwright `page`-fixture collision in full-suite runs (files pass individually/in small batches — see test_option_ranking.py/test_date_coercion.py/test_radio_option_ranking.py/test_field_mapper.py all green in isolation, 300+ passed across targeted batches this session).
