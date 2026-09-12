# Autogram Platform — Phase‑by‑Phase Execution Plan

Companion to [`PLATFORM_PLAN.md`](./PLATFORM_PLAN.md) (the "what" and "why"). This is the "in what
order, touching which files, done when". Each phase is one PR/session, following the discipline in
[`ARCHITECTURE.md`](./ARCHITECTURE.md).

**Legend:** S ≈ 1–2 days · M ≈ 3–5 days · L ≈ 1–2 weeks. Estimates assume one developer familiar with the codebase.

**Dependency chain:**
`0 → 1 → 2 → 3 → 4 → 5` (MVP), then `6, 7, 8` and `9 → 10 → 11` in parallel, then `12 → 13 → 14`.

| Phase | Title | Size | Blocks |
|---|---|---|---|
| 0 | RBAC & identity | S | everything privileged — **✅ implemented (migration `f4a5b6c7d8e9` not yet applied)** |
| 1 | Redis + shared rate limiter + event bus | M | 2, multi‑replica — **✅ implemented (`REDIS_URL` optional: unset ⇒ in‑process + loud startup warning)** |
| 2 | Task queue + `worker/` service + lease ownership | L | 4, 5, scale — **split 2a–2f; all ✅ implemented** |
| 3 | Shared object storage (finish S3 path) | M | multi‑replica — **mostly ✅ implemented (text extraction + Playwright upload rewired; `logs/` screenshots/traces deferred to Phase 12)** |
| 4 | Unified apply entrypoint (**MVP core**) | M | 5 |
| 5 | Apply reliability + notifications (**MVP done**) | M | — |
| 6 | Job search productization | M | 7, 8 |
| 7 | Resume tailoring flow | M | — |
| 8 | Saved searches + alerts | S | — |
| 9 | Community core (spaces, posts, comments, votes, feed) | L | 10, 11 |
| 10 | Community Q&A + structured kinds + tags | M | — |
| 11 | Community trust & safety | M | 13 |
| 12 | Admin app skeleton + core views | M | 13, 14 |
| 13 | Admin moderation + observability | M | — |
| 14 | Admin config, feature flags, GDPR tools | S | — |

---

## Phase 0 — RBAC & identity

**Goal:** every account has a role and a status; privileged dependencies exist and are audited.

**Backend**
- Migration: `users.role` (`member`|`moderator`|`admin`, default `member`, not null) + `users.status` (`active`|`suspended`|`banned`, default `active`). Backfill existing rows.
- `app/core/auth.py`: `require_role(*roles)` dependency factory; export `get_current_moderator`, `get_current_admin`. Add `role` to the JWT payload in `create_access_token` (hint only — DB row stays the authority; re‑read on every protected call, as `get_current_user` already does).
- `get_current_user`: reject `status in (suspended, banned)` with 403 + reason.
- `backend/scripts/make_admin.py <email>` — one‑off promotion, no endpoint.
- `POST /auth/signup`, `/auth/login`, `/auth/me`: include `role` in the response body.
- Audit: `audit_log_repository.record_event` already exists — call it on any future role/status change (helper `app/services/user_admin_repository.py::set_role` / `set_status`, used by Phase 12/13).

**Frontend** (`frontend/`)
- `api.js` / `App.jsx`: store `user.role`; no visible change yet (no moderator UI in the member app).

**Tests**
- `tests/test_auth.py`: role defaults, `require_role` allows/denies matrix, suspended/banned user blocked, JWT carries role but tampered role is ignored (DB wins).

**Exit criteria:** a promoted admin can pass `get_current_admin`; a member gets 404 from a route guarded by it (404, not 403 — don't confirm hidden surfaces); every role change lands in `audit_log`.

---

## Phase 1 — Redis + shared rate limiter + event bus

**Goal:** two `backend/` replicas share rate limiting and live events. No worker yet.

**Infra**
- `docker-compose.yml`: add `redis:7`. Railway: add Redis plugin. `app/core/config.py`: `REDIS_URL`.
- `app/core/redis_client.py`: one pooled `redis.Redis` (sync) + `redis.asyncio.Redis` (async) factory.

> **Implemented deviation from the plan:** `REDIS_URL` is **optional**, not
> fail‑fast. Unset ⇒ the app keeps the in‑process rate limiter and in‑process
> event fan‑out (every existing single‑process dev/test workflow runs unchanged
> with zero new setup), and `app/main.py` logs a prominent warning that this is
> single‑process‑only. Set ⇒ shared Redis. Hard fail‑fast would have broken
> every bare `uvicorn`/`pytest` run that doesn't have Redis; the loud warning
> gives the same "don't deploy multi‑replica without it" signal. Phase 2's
> worker will make Redis a hard requirement *for that service*.

**Rate limiter** (`app/core/middleware.py`)
- Replace the in‑process `_hits` deque with a Redis sorted‑set sliding window (`ZADD`/`ZREMRANGEBYSCORE`/`ZCARD`, keyed `rl:{ip}`, TTL 2×window).
- Honor `X-Forwarded-For` first hop when `TRUST_PROXY=true` (Railway terminates TLS upstream) — otherwise the limiter sees one IP for everyone.
- Fallback: if Redis is unreachable, fail **open** with a logged warning (a rate limiter outage must not take the API down).

**Event bus** (`app/services/event_bus.py`)
- Keep the `WorkflowEvent` / `WORKFLOW_EVENTS` contract **unchanged** (the frontend contract test depends on it).
- `publish()` → `PUBLISH autogram:events:{stream_id}` (JSON) instead of (or in addition to) in‑process fan‑out.
- WebSocket handlers subscribe via a per‑connection `redis.asyncio` pubsub on that channel; drop the in‑process `_subscribers` dict.
- Preserve the stated guarantees (live tap, not a durable log; DB is the source of truth) — Redis pubsub has the same semantics, so the module docstring only needs the "why in‑process" section rewritten to "why Redis pubsub".

**Tests**
- `test_rate_limit_redis.py`: window correctness, multi‑client isolation, Redis‑down fails open.
- Update `test_event_bus.py` to run against a real/fake Redis (`fakeredis`); the fan‑out semantics tests stay.
- `automation/tests/test_frontend_backend_contract.py` must still pass untouched.

**Exit criteria:** `RATE_LIMIT_PER_MINUTE` is enforced across 2 API processes combined; a WebSocket client on process A receives an event published by process B.

---

## Phase 2 — Task queue + `worker/` service + lease ownership

**Goal:** apply runs execute in a separate worker process; ownership is lease‑based; the single‑worker constraint is gone.

**Queue choice (do this spike first, ~half a day):** run 3 concurrent Playwright applies under Celery prefork vs. ARQ, measure RSS and completion. Default recommendation: **Celery** (sync Playwright fits prefork; mature; already referenced in docs).

**Worker service**
- `worker/` = same repo, entrypoint `celery -A automation.worker.celery_app worker`. Own Dockerfile inheriting the Playwright/Chromium base image. Railway service `autogram-worker`.
- `automation/worker/celery_app.py`, `automation/worker/tasks.py`:
  - `run_application_task(application_id)` — the body of today's `app/api/applications.py::_run_application`.
  - `run_autonomous_task(task_id)` — the body of today's `runner.py` thread hand‑off.

**Ownership → Redis leases**
- `automation/ownership/lease.py`: `acquire_lease(kind, id, worker_id, ttl)` (`SET NX PX`), `renew_lease` (heartbeat every ~15 s while the run holds it), `release_lease`.
- `runner.py::_REGISTRY` → the live `TaskHandle` still lives in the worker that owns the run (a paused task is resumed by that worker); the *fact of ownership* + a `resume_requested` / `cancel_requested` flag moves to Redis so the API (any replica) can signal the owning worker. Worker polls its own tasks' flags on each loop iteration.
- `application_flow_manager._OPEN_REVIEW_SESSIONS` → the browser stays pinned to the owning worker; `POST /applications/{id}/approve` publishes an `approve_requested` Redis message on `autogram:control:{application_id}`; the owning worker consumes it and drives Submit. If no worker holds the lease → 409 with "session expired, start a new attempt" (same UX as today's restart case).

**Reconciliation** (`app/services/automation_recovery.py`, `runner.py`)
- Replace "registries are empty at startup ⇒ everything active is orphaned" with: **a row is orphaned iff its Redis lease is absent/expired.** Run this reconcile on an interval (every ~60 s) in a lightweight beat task, not only at startup — so a dead worker's runs recover within a minute without waiting for an API restart.
- Keep the `processing → needs_review` (never `failed`) rule verbatim — the submission‑ambiguity reasoning is unchanged.

**API** (`app/api/applications.py`, `app/api/autonomous_agent.py`)
- `_run_application` / thread hand‑off → `.delay(application_id)` / `.delay(task_id)`.
- `BackgroundTasks` import removed from the apply path.

**Docs**
- Delete the "SINGLE WORKER ONLY" block from `README.md`; delete the "why in‑process" sections from `runner.py`, `event_bus.py`, `automation_recovery.py`, `middleware.py`. Add `SCALING.md` describing the lease model + how many workers to run.

**Tests**
- `automation/tests/test_reconciler_concurrency.py`, `test_crash_recovery.py`, `test_duplicate_automation_guard.py` — rewrite the "empty registry" assumptions to lease assertions; the *behaviours* they check (no double‑apply, `processing`→`needs_review`, one recovery wins) all stay.
- New `test_lease.py`: acquire/renew/expire, two workers can't both hold one lease, expired lease is reclaimed.
- New `test_worker_control_channel.py`: approve/resume/cancel routed to the owning worker; 409 when no owner.

**Exit criteria:** 2 API replicas + 2 workers; a full apply runs on worker 1 while worker 2 runs another; `kill -9` on worker 1 mid‑run → the row is `needs_review` within 60 s with `may_have_submitted` recorded; approve on a copilot run started by worker 2 succeeds regardless of which API replica received the call.

### Phase 2 sub‑phases (each its own PR — this is the largest phase and it rewrites the most safety‑critical code in the repo)

| Sub | Scope | Behaviour change? | Status |
|---|---|---|---|
| **2a** | `automation/worker/` Celery app + `worker/` service entrypoint + Dockerfile; `automation/coordination/lease.py` (Redis `SET NX PX` acquire / renew / release / read) + tests. Nothing calls them yet. | none — additive only | ✅ implemented |
| **2b** | `automation/coordination/control.py` — Redis control channel: `resume`/`cancel`/`approve` flags + a one‑shot OTP mailbox. `runner.py`'s `signal_resume`/`request_cancel`/`deliver_secret` hand off to it when there's no local handle **and** the run's lease is held elsewhere. | none — `lease.is_held` is always False until 2c acquires leases | ✅ implemented |
| **2c** | Worker package → `app/workers/` (it needs `app.api`, `automation/` can't import that). `app/workers/dispatch.py` — broker‑aware: `CELERY_BROKER_URL` set → `.delay()`, unset → the existing `BackgroundTasks`/daemon‑thread path. `runtime.py` wraps each run in acquire → heartbeat → release. `runner.execute_task_sync` shared by thread + worker. `loop.py` consumes `control.*` for cancel/resume/OTP. Autonomous + deterministic **autopilot** runs queue to the worker; **copilot** runs stay in‑process (see 2f). | **yes** — autopilot + autonomous runs move to the worker when a broker is set | ✅ implemented |
| **2d** | Lease‑based reconciliation: rewrite `reconcile_orphaned_*` to "orphaned iff lease absent/expired"; add a ~60 s beat task alongside the startup pass. `processing → needs_review` rule kept verbatim. Rewrite `test_reconciler_concurrency` / `test_crash_recovery` / `test_duplicate_automation_guard` assumptions. | **yes** — recovery no longer assumes single process | ✅ implemented |
| **2e** | Docs: delete "SINGLE WORKER ONLY" from `README.md`; strip the "why in‑process" sections from `runner.py` / `event_bus.py` / `automation_recovery.py` / `middleware.py`; add `SCALING.md`. | none | ✅ implemented |
| **2f** | Cross‑worker **copilot approve**: `dispatch_application_run` no longer special‑cases copilot — with a broker set, autopilot AND copilot runs alike queue to the worker. The worker (`app/workers/runtime.py::_watch_for_copilot_approval`) keeps a finished `copilot_review` run's lease + browser open and polls `control.consume_signal("application", id, "approve")`. `POST /applications/{id}/approve` (`app/api/applications.py::apply_copilot_approval`) tries the same‑process fast path first (unchanged when there's no broker); when the browser isn't local but the lease is held elsewhere, it deposits the `approve` signal and returns a `copilot_review` status telling the caller to poll. Gives up after `COPILOT_APPROVAL_MAX_WAIT_SECONDS` (default 30 min) and lets the lease release, so an unattended review is reconciled to `needs_review` exactly like a dead process today. | **yes** — copilot runs can scale out | ✅ implemented |

---

## Phase 3 — Shared object storage (finish the S3 path)

**Goal:** no automation path depends on local disk, so replicas/workers are interchangeable.

**Backend**
- `app/services/storage/` is already scaffolded. The two previously-tracked gaps are now rewired — **✅ implemented**:
  - `app/services/text_extraction.py::extract_text` — reads the stored document via `StorageBackend.local_path()` (a no-op under local storage; a cleaned-up temp download under S3), not a hardcoded local path. pdfplumber/python-docx both need a real file on disk.
  - The Playwright resume‑upload path (`automation/ats/base.py::ATSAdapter.upload_resume`) — materializes the résumé via the same `local_path()` before `set_input_files`.
- `document_storage.py` / `file_storage.py` already route through the backend selector — verified every caller uses `local_path()` / the accessor, none the raw column. The one exception (`document_storage.save_task_upload_file`, autonomous-task live uploads) is deliberate and documented: it always writes local because it's staging a browser upload, same reasoning as the two rewired call sites above.
- **Remaining:** `logs/` (screenshots, traces) → object storage too, since a worker writing to its own local `logs/` is invisible to the API serving `GET /applications/{id}/runs`. `automation_runs.screenshot_paths` / `trace_path` would become object keys; the API would stream them back (or issue short‑lived signed URLs). Not yet started — deferred because there is no consumer of these columns via any API today (the viewer is Phase 12's `GET /admin/runs/{id}`), so there's nothing to verify a round trip against yet. Do this alongside Phase 12, or before a genuine multi-replica/multi-worker deployment goes live, whichever comes first.

**Infra**
- Provision an S3‑compatible bucket (Cloudflare R2 or AWS S3). Set `STORAGE_BACKEND=s3` + creds in both `backend/` and `worker/`.

**Tests**
- `tests/test_storage_backends.py` — shared contract for both backends (local + moto-mocked S3): save/read/exists/delete/`local_path`, factory selection, `file_storage.py`/`document_storage.py` end-to-end. `tests/test_text_extraction_storage.py` — `extract_text` against a local path and an S3 locator. `automation/tests/test_generic_adapter.py::test_upload_resume_materializes_an_s3_stored_document_first` — the Playwright upload path against an S3 locator.

**Exit criteria:** with `STORAGE_BACKEND=s3` and local `storage/` deleted, upload → extract → parse → apply all work from a fresh container. "View run screenshots" waits on the `logs/` item above plus a real viewer (Phase 12).

---

## Phase 4 — Unified apply entrypoint (MVP core)

**Goal:** one action — "apply to this link" — that picks the engine automatically.

**Backend** (`app/api/applications.py`)
- `POST /applications/start` body gains `mode: "auto" | "copilot" | "watch"` (default `auto`).
- Engine selection: `detect_ats(url)` → if `registry.get_adapter_class(platform)` is not `None` and `mode != "watch"` → deterministic (`run_application_task`); else → autonomous (`run_autonomous_task`), carrying `mode`.
- The existing `decide_action` table still governs auto‑submit vs. copilot vs. needs‑review.
- One response shape regardless of engine: `{ application_id, engine, status }`. Autonomous runs get an `applications` row too (today they may only have an `autonomous_tasks` row) — unify so `GET /applications` lists everything. Add `applications.engine` column (`deterministic`|`autonomous`) + `applications.autonomous_task_id` nullable FK.
- Migration for the two columns; backfill `deterministic` for existing rows.

**Frontend** (`frontend/`)
- Dashboard: prominent "Paste a job link" box → `POST /applications/start`.
- **One** `ApplicationDetail` page serving both engines: timeline, live screenshot, current step, answer‑review list, approve/submit button for copilot. Branch on `engine` only where the two genuinely differ (autonomous shows the decision log; deterministic shows the adapter step list).
- Demote `/agent` (Autonomous Agent) from the primary "Workspace" nav group to an "Advanced" section; keep the page for power users / debugging.
- Extension (`extension/`): "Apply with Autogram" posts the active tab URL to the same endpoint.

**Tests**
- `tests/test_applications_api.py`: engine selection matrix (known ATS→deterministic, unknown→autonomous, `watch`→autonomous), unified list includes autonomous runs, one response schema.
- Frontend: `EntryFlow.test.jsx` extended — paste link → application appears → detail page renders for both engines.

**Exit criteria:** paste a Greenhouse link, a Workday link, and a custom‑portal link → 3 applications, correct engine each, all visible in one list, all open the same detail page.

---

## Phase 5 — Apply reliability + notifications (MVP done)

**Goal:** the apply flow is trustworthy and tells the user what happened.

**Backend**
- **Retry**: wire `POST /applications/{id}/retry` to re‑enqueue on the same row (respects `RETRYABLE_STATUSES`, increments `automation_runs.retry_count`). Autonomous path gets the same.
- **Compliance floors in the worker**: per‑user daily cap + working‑hours window enforced before a run starts (`automation/browser/session.py::HumanPacing` already has typing/action delays; add the daily/window check here). Over cap → row stays `pending` with a "scheduled for <time>" reason; a beat task releases them.
- **Email**: pick a provider (Resend / SES / Postmark), `app/services/notifications.py` with `send_email()` + a `notification_preferences` concept on the user (columns or a small table). Trigger on `applied`, `needs_review`, `manual_required`, `copilot_review`. WebSocket event already fires; email is the out‑of‑session channel.

**Frontend**
- Retry button on failed/needs‑review applications with the recovery reason shown inline.
- Settings page: notification toggles.
- Toast + optional browser notification on terminal status while the tab is open.

**Tests**
- `test_retry.py`: retry re‑enqueues, non‑retryable rejected, retry_count increments.
- `test_pacing_caps.py`: daily cap defers, working‑hours window respected, beat releases deferred rows.
- `test_notifications.py`: each terminal status sends exactly one email, preferences respected (mock the provider).

**Exit criteria (MVP):** a new user signs up → builds profile → uploads resume → pastes 3 links → within the daily cap gets 3 terminal outcomes, each with a clear reason, an email, and a working retry — on a 2‑replica + 2‑worker deployment.

---

## Phase 6 — Job search productization

**Goal:** a real search page, not just the matching API.

**Backend** (`app/api/jobs.py`, `app/services/matching/`)
- `POST /search` — `{ query, location?, remote?, salary_min?, seniority?, sources? }` → on‑demand ingest (if the pool is thin for that query) → embed pending → match against the user's active resume → ranked results with the full score breakdown (`vector_similarity`, `skill_overlap_ratio`, `blended_score`, `matched_skills`, `missing_skills`, `ats_found/missing_keywords`).
- Cache search results per `(user, query_hash)` for a short TTL so paging/re‑sorting doesn't re‑run the pipeline.
- `GET /search/history` — recent queries.

**Frontend** (`frontend/src/pages/JobsAndMatches.jsx` → split)
- Search bar + filter chips → results list.
- `MatchCard` shows the score ring + an expandable breakdown (why this score, which skills matched/missing, ATS keyword coverage).
- "Apply" on a card → `POST /applications/start` with that `apply_url` (reuses Phase 4).
- "Tailor" on a card → Phase 7.

**Tests**
- `test_search_api.py`: on‑demand ingest triggers only when pool thin, result caching, filter application, breakdown fields present.

**Exit criteria:** search "backend engineer, remote, >£70k" → ranked list with visible score reasoning → apply directly from a result.

---

## Phase 7 — Resume tailoring flow

**Goal:** per‑job tailored resume + cover letter, saved as tagged documents, usable by the apply engine.

**Backend**
- Tailoring + cover‑letter endpoints already exist (`POST /resumes/matches/{id}/tailor`, `/cover-letter`). Add:
  - `POST /resumes/matches/{id}/tailor/accept` — takes the accepted bullet rewrites → renders a new resume document (same format as the base) → saves as a `profile_document` with `document_type=resume`, `job_type_tag` = this job, `is_default=false`, linked to the match.
  - Cover letter saved similarly (`document_type=cover_letter`).
- `applications.start` already accepts an explicit resume doc — no change; the frontend just passes the tailored `document_id`.

**Frontend**
- Tailoring view: original vs. suggested bullets as a diff, checkboxes to accept per bullet, ATS keywords to weave in, honest gaps with learning suggestions.
- "Save tailored resume" → shows in Resumes tagged to the job.
- "Apply with this tailored resume" → `POST /applications/start` with the tailored doc.

**Tests**
- `test_tailoring_accept.py`: accepted rewrites produce a new doc, base resume untouched, doc tagged to the job, apply picks it up.

**Exit criteria:** from a match → tailor → accept 4 of 6 rewrites → save → apply with the tailored resume; base resume unchanged.

---

## Phase 8 — Saved searches + alerts

**Goal:** searches re‑run themselves and notify on new strong matches.

**Backend**
- Migration: `saved_searches` (`id`, `user_id`, `name`, `query_json`, `min_score`, `active`, `created_at`) + `search_alerts` (`id`, `saved_search_id`, `job_id`, `score`, `notified_at`).
- `app/core/scheduler.py`: a job that, per active saved search, runs the Phase 6 pipeline, records matches above `min_score` not already alerted, and sends a digest email (reuse Phase 5 `notifications.py`).
- `POST/GET/PATCH/DELETE /searches`.

**Frontend**
- "Save this search" on the search page; a "Saved searches" list with new‑match badges; alert digest links back into results.

**Tests**
- `test_saved_searches.py`: scheduler produces alerts only above threshold, no duplicate alerts for the same job, digest content.

**Exit criteria:** save a search → scheduler tick → new qualifying job → one alert email with a working link.

---

## Phase 9 — Community core

**Goal:** members can post to spaces, comment, vote, and read a feed.

**Backend** (new `app/api/community.py`, `app/services/community_repository.py`, `app/models/community.py`)
- Migrations:
  - `community_spaces` (`space_id`, `slug`, `name`, `description`, `kind_allowlist` JSONB, `icon`, `is_default_join`, `created_at`). Seed: General, Interview Experiences, Referrals, Salary Sharing, Resume Reviews, Q&A.
  - `community_posts` — see `PLATFORM_PLAN.md` §6.1 (this phase implements `kind ∈ {feed, thread}` only; the column supports all values from day one).
  - `community_comments` (`comment_id`, `post_id`, `parent_comment_id`, `author_id`, `body_md`, `status`, `score`, `created_at`, `edited_at`).
  - `community_votes` (`user_id`, `target_type`, `target_id`, `value ∈ {-1,1}`, unique `(user_id, target_type, target_id)`).
  - `community_follows` (`follower_id`, `followee_id`, unique).
- Endpoints:
  ```
  GET  /community/spaces
  GET  /community/feed?scope=following|discover&space=&kind=&cursor=
  POST /community/posts            {kind, space_id?, title?, body_md, payload?}
  GET  /community/posts/{id}
  PATCH/DELETE /community/posts/{id}         (author; DELETE = soft status=removed)
  POST /community/posts/{id}/comments
  POST /community/vote                       {target_type, target_id, value}
  POST /community/follow / DELETE /community/follow/{user_id}
  ```
- Ranking: `discover` = `score` (cached vote tally) with recency decay + `following` = followed users/spaces reverse‑chron. No LLM.
- Markdown: server‑side sanitize on write (allowlist renderer, e.g. `bleach` + a markdown lib); store raw + never trust client HTML.
- Counters (`comment_count`, `score`) updated transactionally on write.

**Frontend** (`frontend/` — new "Community" nav group)
- Feed page (following / discover toggle), space list, post detail with threaded comments, composer (fields switch by `kind`), vote buttons, follow button.
- Public read‑only post view by URL when `status=published`; composing requires login.

**Tests**
- `test_community_core.py`: post/comment CRUD, soft delete hides from feed, one vote per user (flip/remove), follow feed vs discover ordering, sanitization strips scripts.

**Exit criteria:** two members: A posts to "Interview Experiences", B comments and upvotes, A sees it in their feed, the post is publicly viewable by link.

---

## Phase 10 — Community Q&A + structured kinds + tags

**Goal:** the remaining post shapes.

**Backend**
- Migration: `community_answers` (`answer_id`, `post_id`, `author_id`, `body_md`, `status`, `score`, `is_accepted`, `created_at`) + `community_tags` (`tag_id`, `slug`, `name`) + `community_post_tags` (`post_id`, `tag_id`). Add `community_posts.accepted_answer_id` nullable FK.
- Endpoints:
  ```
  POST /community/posts/{id}/answers          (kind=question only)
  POST /community/answers/{id}/accept          (question author only)
  POST /community/answers/{id}/vote            (reuses community_votes with target_type=answer)
  GET  /community/questions?filter=unanswered|top&tag=
  ```
- Structured kinds (`referral`, `interview_experience`, `salary`, `resume_example`): validate `payload` against a per‑kind Pydantic schema (`app/models/community_payloads.py`) — e.g. `salary` requires `company`, `role`, `base`, `location`, `yoe`; `total_comp` derived.
- Filtered browse endpoints per structured kind: `GET /community/salary?company=&role=&location=&min_yoe=`, `GET /community/referrals?company=&status=`, etc. — plain SQL over `payload` JSONB with GIN indexes on the hot keys.
- `resume_example` attachments go to object storage (Phase 3), keys in `attachments` JSONB.

**Frontend**
- Q&A view (ask, answer, accept, unanswered filter, tag filter).
- Per‑kind composer forms + per‑kind table/browse views with filters (salary browser, referral board, interview‑experience list).

**Tests**
- `test_community_qa.py`: answer/accept/vote, only author accepts, accepted answer surfaces first.
- `test_community_structured.py`: payload schema rejection, JSONB filter queries, attachment round trip.

**Exit criteria:** ask a question → get two answers → accept one; post a salary data point → find it via the salary browser filtered by company + YOE.

---

## Phase 11 — Community trust & safety

**Goal:** abuse is reportable, moderators can act, new accounts are gated.

**Backend**
- Migration: `community_reports` (`report_id`, `reporter_id`, `target_type`, `target_id`, `reason`, `note`, `status ∈ {open, actioned, dismissed}`, `report_count`, `handled_by`, `resolution_note`, `created_at`, `handled_at`). Unique `(reporter_id, target_type, target_id)`; duplicate reports bump `report_count` + priority.
- Endpoints:
  ```
  POST /community/report                    {target_type, target_id, reason, note?}
  POST /community/mod/hide|remove|lock|pin   (get_current_moderator)   {target_type, target_id, reason}
  POST /community/mod/warn                   {user_id, message}
  POST /community/mod/suspend|ban            {user_id, until?, reason}   (sets users.status)
  ```
- Karma = summed vote score across a user's posts+answers. Gates (config‑driven, Phase 14 makes them flags):
  - `< karma_post_threshold` → links in posts held (`status=flagged`) until a mod clears them; posting rate‑limited (N/day).
  - brand‑new account (age < 24 h) → cannot post structured `salary`/`referral` kinds.
- Every mod action → `audit_log` + optional user notification with the reason (reuse Phase 5 email).
- PII flag: regex scan for email/phone on `salary`/`interview_experience` bodies → soft warning to the author on write, `payload._pii_flag` set for the mod queue.

**Frontend (member app)**
- "Report" on posts/comments/answers.
- If the current user is `moderator`/`admin`: inline hide/remove/lock/pin controls (the *queue* lives in the admin app — Phase 13).
- Suspended/banned users see a clear notice and read‑only community.

**Tests**
- `test_community_moderation.py`: report dedup + count, each mod action + audit entry, karma gate holds a low‑karma link post, ban sets status and blocks posting, warned user gets a notification.

**Exit criteria:** member reports a post → a moderator hides it → author is notified → the action is in the audit log → a fresh low‑karma account's link post is held for review.

---

## Phase 12 — Admin app skeleton + core views

**Goal:** a separate, unlinked admin SPA with the highest‑value read views.

**New app** (`admin/` — Vite + React, mirrors `frontend/`'s stack and component style)
- Deploy: static build behind nginx at `admin.<domain>`, `X-Robots-Tag: noindex`, **no source maps**, not referenced anywhere in `frontend/`.
- Auth: same `/auth/login`; on load, call `/admin/me` — non‑admin token → hard bounce to a blank 404‑style page.
- Optional now, required before commercial launch: IP allowlist / VPN, short admin‑JWT lifetime.

**Backend** (new `app/api/admin/` package, every route `Depends(get_current_admin)`, non‑admin → 404)
- `app/services/admin_metrics_repository.py` — aggregate queries only; no new business logic.
- Endpoints:
  ```
  GET /admin/me
  GET /admin/overview                     users, DAU, applications today by status, community activity,
                                          queue depth (Redis), worker heartbeats, error rate
  GET /admin/users?q=&role=&status=&cursor=
  GET /admin/users/{id}                   full profile + resumes + applications + community activity
  POST /admin/users/{id}/role             {role}          -> user_admin_repository.set_role + audit
  POST /admin/users/{id}/status           {status, reason}
  GET /admin/applications?status=&ats=&failure=&user=&cursor=
  GET /admin/applications/{id}            + run history
  GET /admin/runs/{id}                    screenshots (signed URLs), trace, error log, timings
  POST /admin/applications/{id}/requeue
  POST /admin/applications/{id}/force-status   {status, reason}   (dangerous — confirm + audit)
  ```

**Admin frontend views:** Overview dashboard · Users (search/detail/role/status) · Applications (filter/detail) · Automation runs (artifacts viewer) · Audit log (read).

**Tests**
- `test_admin_auth.py`: member/moderator → 404 on every `/admin` route; admin → 200; role/status changes audited.
- `test_admin_views.py`: overview aggregates correct, user detail joins, run artifact URLs signed + expiring.

**Exit criteria:** an admin logs into `admin.<domain>`, sees system health, opens any user, views a failed run's screenshots + trace, promotes a user to moderator — a member hitting the same URL gets nothing.

---

## Phase 13 — Admin moderation + observability

**Goal:** the moderation queue and the "what's happening / what's it costing" dashboards.

**Backend** (`app/api/admin/`)
- ```
  GET  /admin/moderation/queue?status=open&cursor=      (community_reports, priority-ordered)
  POST /admin/moderation/{report_id}/action             {action, resolution_note}  -> reuses Phase 11 mod ops
  POST /admin/moderation/{report_id}/dismiss
  GET  /admin/spaces / POST /admin/spaces / PATCH /admin/spaces/{id}
  POST /admin/announcements                             (pinned banner in the member app)
  GET  /admin/jobs                                      pool size, per-source health, dedup stats
  POST /admin/jobs/reingest / POST /admin/jobs/purge
  GET  /admin/llm/cost?range=                           calls by task type, tokens, $, cache hit rate
  GET  /admin/adapters/health                           per-ATS success rate, common failure reasons
  ```
- `admin_announcements` migration (`id`, `body_md`, `level`, `active`, `starts_at`, `ends_at`).
- LLM cost: aggregate from whatever the LLM router already logs; if it doesn't persist call metadata, add an `llm_calls` metrics table written by `app/ai/llm/router.py` (task, model, tokens, latency, cache hit).

**Admin frontend:** Moderation queue (bulk actions) · Spaces management · Announcements editor · Jobs dashboard · LLM/cost dashboard · Adapter health.

**Member frontend:** render an active `admin_announcement` as a dismissible banner.

**Tests**
- `test_admin_moderation.py`: queue ordering by priority/report_count, action resolves the report + performs the mod op + audits, dismiss closes without acting.
- `test_admin_observability.py`: cost aggregation math, adapter health from run history, announcement lifecycle.

**Exit criteria:** a reported post appears in the queue → admin removes it from the queue view in one action (content hidden, reporter+author notified, audited); the cost dashboard shows today's LLM spend broken down by task.

---

## Phase 14 — Admin config, feature flags, GDPR tools

**Goal:** operational levers without a deploy, and compliance tooling.

**Backend**
- `feature_flags` migration (`key`, `value_json`, `description`, `updated_by`, `updated_at`) + `app/services/feature_flags.py` (cached read, Redis‑invalidated on write). Flags: global autopilot on/off, vision fallback, job sync, community posting gates (`karma_post_threshold`, new‑account limits), retention windows.
- ```
  GET/PUT /admin/flags
  GET/PUT /admin/config/model-routing          (edit app/ai/llm routing — confirm + audit; validate against the registry)
  POST    /admin/retention/purge-now
  GET     /admin/users/{id}/export             full data export (GDPR)
  DELETE  /admin/users/{id}                     hard delete + cascade + object-storage cleanup (double confirm + audit)
  ```
- Wire the flags into their call sites (`decide_action`, `vision_fallback`, scheduler, community gates, `retention_service`).

**Admin frontend:** Feature flags editor (typed inputs, "danger" styling on autopilot) · Model‑routing editor · Retention panel · Per‑user export/delete with confirmation modals.

**Tests**
- `test_feature_flags.py`: write invalidates cache across processes, call sites honor flags, bad model‑routing config rejected.
- `test_gdpr_tools.py`: export completeness, hard delete removes rows + objects + leaves an audit tombstone.

**Exit criteria:** an admin flips global autopilot off → new applies go to copilot within seconds on all workers, no deploy; a user data export contains profile + resumes + applications + community content; hard delete leaves nothing but an audit record.

---

## Cross‑phase standing rules

- Every schema change: Alembic migration, `ON DELETE CASCADE` from owner, indexes on FK + `WHERE`/`ORDER BY` columns, JSONB only for non‑queried blobs.
- Every privileged or destructive action writes to `audit_log`.
- `ruff check .` and the scoped `mypy` must pass; new modules get tests in the same PR.
- Member app never imports admin code; `automation/` never imports `app.api`.
- Feature‑flag anything risky (Phase 14) so it can be disabled without a deploy.
- Update the relevant doc (`README.md`, `ARCHITECTURE.md`, `SCALING.md`, or a new `COMMUNITY.md` / `ADMIN.md`) in the same PR that changes the behaviour it describes.
