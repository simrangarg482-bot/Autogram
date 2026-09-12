# Autogram — Full Platform Plan

End‑to‑end plan to take the current codebase from "resume → match → auto‑apply engine"
to a **full job‑application portal** with four product surfaces:

| # | Surface | One line |
|---|---|---|
| A | **Apply engine** | Paste any job link → the agent applies for you (copilot or autopilot). |
| B | **Job search + tailoring** | Search live jobs, get ranked matches, tailor the resume + cover letter per job. |
| C | **Community** | Members share posts, threads, Q&A, and structured knowledge (referrals, interview experiences, salary data, resume examples). |
| D | **Admin console** | A separate app on its own URL, never linked from the site, where staff see and moderate everything. |

Decisions locked for this plan:

- Community supports **all four models** (feed / forum / Q&A / structured), built on one unified post system.
- Admin is a **separate React app + shared backend** with an admin‑only role gate.
- **MVP first = unify the two apply flows into one coherent portal** with real roles, then Community, then Admin.
- The **single‑worker constraint is removed** as part of this plan (Redis + real task queue).

This doc is the master plan; each phase still gets its own PR/session, following the discipline in
[`ARCHITECTURE.md`](./ARCHITECTURE.md) and [`PHASE2_ARCHITECTURE.md`](./PHASE2_ARCHITECTURE.md).

---

## 1. Where we are today

Already built and working (see [`ARCHITECTURE.md`](./ARCHITECTURE.md) roadmap, phases 1–8):

- Resume upload → text extraction (column‑aware PDF, OCR fallback, DOCX tables) → LLM parsing → embedding.
- Multi‑source job ingestion (Adzuna, Remotive, Arbeitnow) with cross‑source dedup + optional scheduler.
- pgvector matching pipeline: retrieval → hard filters → per‑job LLM skill/ATS analysis → blended ranking.
- Resume tailoring + cover‑letter generation endpoints.
- Master candidate profile (personal / education / experience / skills / documents, PII encrypted).
- **Deterministic apply**: `ApplicationFlowManager` + Greenhouse/Lever/generic adapters, field mapper,
  answer engine with per‑user answer cache, field‑handler registry, CAPTCHA short‑circuit, HITL OTP/MFA.
- **Autonomous apply**: observe → decide → act agent loop with human‑intervention pause/resume.
- Frontend SPA (`frontend/`), browser extension (`extension/`), 1,456 tests.

Gaps this plan closes:

1. No user **roles** — every account is identical; no moderator/admin concept.
2. Two apply flows (deterministic + autonomous) are exposed as **separate features**, not one "apply" action.
3. **Process‑memory state** (task registry, event bus, rate limiter, open review sessions) forces single‑worker.
4. No **community** anything.
5. No **admin** surface — support/moderation/ops is all raw SQL today.
6. `frontend/` and `backend/` deploy on Railway; no `admin/` app, no Redis, no worker service.

---

## 2. Target architecture

### 2.1 Deployables

```
autogram/
  backend/       FastAPI API + WebSocket        (Railway service, 1..N replicas after Phase 0)
  worker/        automation task runner         (NEW — Railway service, own Playwright image, 1..N)
  frontend/      member SPA  (app.autogram.…)   (existing)
  admin/         staff SPA   (admin.autogram.…) (NEW — nginx static, IP/SSO gated)
  extension/     browser extension              (existing)
  infra:         Neon Postgres + pgvector · Redis (NEW) · S3-compatible object storage
```

`backend/` and `worker/` are the **same Python codebase, different entrypoints** — `uvicorn app.main:app`
vs. the queue worker process. They share `app/` and `automation/`.

### 2.2 The one boundary that stays

`app.api → automation → app.services / app.models / app.core / app.ai`. Nothing new here; community and
admin code is plain CRUD and lives in `app/`, exactly like the profile system.

### 2.3 New cross‑cutting infra

| Concern | Today | After this plan |
|---|---|---|
| Background apply job | `BackgroundTasks` in the API process | Enqueued to Redis, run by `worker/` (Celery or ARQ — §3.2) |
| Task ownership registry (`runner.py::_REGISTRY`) | Python dict in API process | Redis hash keyed by `task_id`, with a heartbeat/lease |
| Open copilot‑review browser sessions | `_OPEN_REVIEW_SESSIONS` dict | Pinned to the worker that owns the run; API routes the approve call to it via Redis |
| WebSocket event bus (`event_bus.py`) | in‑process subscribers | Redis pub/sub fan‑out; any replica can serve any socket |
| Rate limiter (`middleware.py`) | in‑process sliding window | Redis sorted‑set sliding window (shared, `X-Forwarded-For`‑aware) |
| Startup reconciliation | assumes empty registries ⇒ safe | checks Redis leases; only reclaims runs whose lease expired |

---

## 3. Phase 0 — Foundations (blocks everything else)

### 3.1 Roles & RBAC

- `users.role` enum column: `member` (default) · `moderator` · `admin`. Add via Alembic; backfill `member`.
  Keep it a single column for v1; a `user_roles` table is a later refactor if scoped permissions appear.
- Optional `users.status`: `active` · `suspended` · `banned` (moderation needs this — §6).
- `app/core/auth.py`: add `require_role(*roles)` dependency factory; `get_current_admin = require_role("admin")`,
  `get_current_moderator = require_role("moderator", "admin")`.
- JWT already carries `sub`; add `role` claim so the frontend can hide/show nav without a round trip
  (server still re‑checks the DB row on every protected call — the claim is a hint, not the authority).
- Seed the first admin via a one‑off script (`backend/scripts/make_admin.py <email>`), never via an endpoint.
- Every privileged action (role change, ban, content takedown, config edit) writes to the **existing**
  `audit_log` (`audit_log_repository`).

### 3.2 Redis + task queue

1. Add Redis to `docker-compose.yml` and as a Railway service; `REDIS_URL` in config (fail‑fast).
2. Pick the queue: **Celery** (docs already reference it; mature; prefork workers suit sync Playwright) or
   **ARQ** (async‑native, lighter, fewer moving parts). Recommendation: **Celery** with a dedicated
   `worker/` service and `--concurrency` tuned to Playwright memory, because the automation code is
   sync and CPU/RAM‑heavy per run.
3. Move `app/api/applications.py::_run_application` and the autonomous `runner.py` hand‑off onto the queue.
4. Convert the four process‑memory mechanisms to Redis (table in §2.3). Each is small and individually testable.
5. Rewrite `automation_recovery.reconcile_orphaned_automation_on_startup` to be **lease‑based**: a run is
   orphaned only if its Redis lease (renewed by a heartbeat while the worker holds it) has expired.
6. Delete the "SINGLE WORKER ONLY" section from `README.md` once all six rows are done; add a
   "scaling" section describing the lease model.

### 3.3 Object storage

- Finish the `STORAGE_BACKEND=s3` path (already scaffolded in `app/services/storage/`): rewire text
  extraction and the Playwright resume‑upload path to `local_path()` / streamed download. Required before
  multi‑replica (local disk is not shared).

**Exit criteria for Phase 0:** two `backend/` replicas + one `worker/` can run a real apply end‑to‑end,
a WebSocket client on replica A sees events from a run on the worker, and killing the worker mid‑run
leaves the row recoverable to `needs_review` on restart.

---

## 4. Workstream A — Unified apply portal (MVP)

**Goal:** one action — "Apply to this link" — that picks the right engine automatically.

### 4.1 Single entrypoint

- `POST /applications/start { job_url, mode }` where `mode ∈ {auto, copilot, watch}`:
  - Detect ATS (`automation.ats.detector`).
  - If a deterministic adapter exists **and** `mode != watch` → deterministic flow.
  - Else → autonomous agent loop.
  - The `decide_action` table (autopilot only for public ATS ≥ 0.85 confidence) still governs submit vs. review.
- Deprecate the separate "Autonomous Agent" page as a *primary* nav item; keep it as an advanced/debug view.
- One unified **Application detail** page: timeline, live screenshot, current step, the answer‑review list,
  and the approve/submit button for copilot runs — regardless of which engine ran it.

### 4.2 Link intake everywhere

- Paste box on the dashboard.
- Browser extension "Apply with Autogram" button (already partly there) posts the current tab URL.
- From a job‑search match card (Workstream B) — "Apply" reuses the same endpoint.

### 4.3 Reliability polish

- Retries: `automation_runs.retry_count` exists; wire an actual "Retry" action (re‑enqueue, same application row).
- Per‑user daily cap + working‑hours window enforced in the worker (compliance floors from `ARCHITECTURE.md`).
- Notifications: on `needs_review` / `manual_required` / `applied`, push a WebSocket event + optional email.

**MVP definition of done:** a new user signs up, builds a profile, uploads a resume, pastes 3 job links of
different ATS types, and gets 3 applications to a terminal state (applied / copilot‑waiting / needs‑review)
with a clear reason for each — on a multi‑replica deployment.

---

## 5. Workstream B — Job search + resume tailoring

Mostly built; this workstream is **productization**, not new engines.

- **Search UX:** query + filters (location, remote, salary, seniority, source) → ingest on demand → embed →
  match against the user's active resume → ranked list with score breakdown (vector sim, skill overlap,
  ATS keyword coverage, missing skills).
- **Saved searches / alerts:** persist a search; the scheduler re‑runs it and notifies on new high‑scoring matches.
  (`saved_searches` table; reuses `app/core/scheduler.py`.)
- **Tailoring flow:** from a match → "Tailor" calls the existing endpoint → show diff (original vs. suggested
  bullets), ATS keywords to weave in, honest gaps. User accepts edits → saves a **new `profile_document`**
  tagged to that job (never overwrites the base resume).
- **Cover letter:** existing endpoint; store alongside the tailored resume, offer both to the apply engine.
- **"Apply with tailored resume":** the apply engine already accepts an explicit resume doc — pass the
  tailored one through.
- Track outcome back onto the match (`match_results.status`: `applied` once an application row exists) so the
  funnel is visible in Metrics and Admin.

---

## 6. Workstream C — Community

### 6.1 One unified post model (covers all four requested shapes)

`community_posts`:

| Column | Notes |
|---|---|
| `post_id` PK · `author_id` FK→users · `created_at` · `updated_at` · `edited_at` |
| `kind` | `feed` · `thread` · `question` · `referral` · `interview_experience` · `salary` · `resume_example` |
| `space_id` FK→`community_spaces`, nullable | forum board / topic; `null` = global feed |
| `title` | required for `thread`/`question`/structured kinds, optional for `feed` |
| `body_md` | markdown |
| `payload` JSONB | kind‑specific structured fields (company, role, base/bonus/equity, YOE, verdict, offer y/n, …) |
| `attachments` JSONB | object‑storage keys (resume example PDF, screenshots) |
| `status` | `published` · `hidden` (moderator) · `removed` (author/admin) · `flagged` |
| `pinned` · `locked` | moderator controls |
| `score` | cached vote tally for ranking |
| `answer_count` · `comment_count` | denormalized counters |
| `accepted_answer_id` | for `question` kind |

Supporting tables:

- `community_spaces` — id, slug, name, description, `kind_allowlist` (which post kinds belong here), icon,
  member/mod settings. Seed with: General, Interview Experiences, Referrals, Salary Sharing,
  Resume Reviews, Q&A.
- `community_comments` — threaded (`parent_comment_id`), same `status` lifecycle, on posts **and** answers.
- `community_answers` — only for `question` posts (kept distinct from comments so "accepted answer" and
  answer‑level voting are clean). Or model answers as `kind='answer'` comments — decide in the phase;
  separate table is cleaner.
- `community_votes` — `(user_id, target_type, target_id, value ∈ {-1,+1})`, unique. Drives `score`.
- `community_follows` — `(follower_id, followee_id)` for the social‑feed "following" view.
- `community_reports` — `(reporter_id, target_type, target_id, reason, note, status)` → the admin moderation queue.
- `community_tags` + `community_post_tags` — free‑ish tagging, especially for Q&A.

### 6.2 Feed / ranking

- **Following feed:** posts from followed users + spaces the user joined, reverse‑chron.
- **Discover feed:** ranked by `score`, recency decay, and (optional, later) pgvector similarity to the
  user's profile/skills — reuse `embedding_service`, same trick as job matching.
- Q&A view: unanswered filter, sort by votes, accepted‑answer badge.
- Structured views: each structured `kind` gets a filterable table view (e.g. salary browser filtered by
  company/role/location; referral board filtered by company/status).

### 6.3 Trust, safety, anti‑abuse (build with the feature, not after)

- New accounts: rate‑limited posting, links held for review below a karma threshold (karma = summed vote score).
- Report → `community_reports` row → visible in Admin moderation queue (§7.3).
- Moderator actions: hide/remove post or comment, lock thread, pin, warn user, temp‑suspend, ban
  (sets `users.status`). All audit‑logged.
- Every moderation action optionally notifies the affected user with the reason.
- Markdown is sanitized server‑side (allowlist); user HTML never rendered raw.
- PII scrubbing hint on salary/interview posts (client‑side warning + optional server regex flag for emails/phones).

### 6.4 API (all under `/community`, member auth; mod actions require `moderator`)

```
GET    /community/spaces
GET    /community/feed?scope=following|discover&kind=&space=&cursor=
POST   /community/posts
GET    /community/posts/{id}
PATCH  /community/posts/{id}            (author or mod)
DELETE /community/posts/{id}            (author soft-remove or mod)
POST   /community/posts/{id}/comments
POST   /community/posts/{id}/answers          (question kind)
POST   /community/answers/{id}/accept         (question author)
POST   /community/vote                        {target_type,target_id,value}
POST   /community/follow / DELETE /community/follow/{user_id}
POST   /community/report                      {target_type,target_id,reason}
POST   /community/mod/{action}                (moderator: hide|remove|lock|pin|warn)
```

### 6.5 Frontend (member app)

New nav group "Community": Feed · Spaces · Ask · My posts. Post composer switches fields by `kind`.
Public read‑only view of a post by URL (for sharing) if `status=published`; posting requires login.

---

## 7. Workstream D — Admin console

Separate app: `admin/` (Vite + React, same component style as `frontend/`), deployed static behind nginx,
reachable only at `admin.<domain>`, **not linked anywhere** from the member app. Extra hardening:

- Backend: every `/admin/*` route depends on `get_current_admin`; wrong role → 404 (not 403 — don't confirm the surface exists).
- Login is the same `/auth/login`; a non‑admin token simply can't call any `/admin` route.
- Optional: separate short JWT lifetime for admin sessions, IP allowlist / VPN, and 2FA before commercial launch.
- Admin app served with `X-Robots-Tag: noindex`, no source maps.

### 7.1 Admin surface areas

| Area | What admin sees / does |
|---|---|
| **Overview** | user counts, DAU, applications today by status, community activity, error rate, queue depth, worker health |
| **Users** | search, view full profile, applications, resumes, community activity; change role; suspend/ban; force password reset; delete account (GDPR) |
| **Applications** | every application across all users; filter by ATS/status/failure reason; open a run's screenshots + trace + error log; manually re‑enqueue or force a status |
| **Automation runs** | raw run history, artifacts, timings; adapter success rates per ATS (feeds "which adapter is flaky") |
| **Community moderation** | the `community_reports` queue, bulk hide/remove, user warnings/bans, space management, pinned announcements |
| **Jobs** | ingested job pool, source health, dedup stats, re‑ingest / purge |
| **LLM / cost** | calls by task type, token spend, cache hit rate, model routing config (read; edit is dangerous — gate behind confirm + audit) |
| **Config & feature flags** | toggle autopilot globally, vision fallback, job sync, community posting gates, retention windows |
| **Audit log** | full `audit_log` viewer, filterable by actor/action/target |
| **Retention / GDPR** | run the purge now, per‑user export, per‑user hard delete |

### 7.2 Admin API

Namespaced `/admin/...`, thin wrappers over existing repositories + a few new aggregate queries
(`app/services/admin_metrics_repository.py`). No new business logic — admin *reads* the same data and
calls the same moderation/role functions the member surfaces use.

### 7.3 Moderation queue mechanics

`community_reports.status`: `open` → `actioned` / `dismissed`, with `handled_by` + `resolution_note`.
One report per (user, target); multiple reporters increment a counter and bump priority.

---

## 8. Data model — all new tables

Phase 0: `users.role`, `users.status` (columns).
Workstream B: `saved_searches`, `search_alerts`.
Workstream C: `community_spaces`, `community_posts`, `community_comments`, `community_answers`,
`community_votes`, `community_follows`, `community_reports`, `community_tags`, `community_post_tags`.
Workstream D: `admin_announcements`, `feature_flags` (or reuse a config table); no other new tables —
admin reads existing ones.

Every table: Alembic migration, `ON DELETE CASCADE` from its owner, `created_at`, indexes on FK + any
column used in a `WHERE`/`ORDER BY`. JSONB for structured/queryable blobs, real typed columns for
anything sorted or filtered (same rule the codebase already follows).

---

## 9. Sequenced roadmap

| Phase | Scope | Depends on | Rough size |
|---|---|---|---|
| **0** | Redis, Celery `worker/` service, six process‑memory mechanisms → Redis, lease‑based reconciliation, S3 storage finish, RBAC (`role`/`status`, `require_role`, first‑admin script, JWT claim) | — | Large |
| **A1** | Unified `POST /applications/start` (engine auto‑select), one Application‑detail page, link intake (dashboard + extension) | 0 | Medium |
| **A2** | Retry action, daily cap + working‑hours in worker, notifications (WS + email) | A1 | Small |
| **B1** | Search page with filters → on‑demand ingest/embed/match, score breakdown UI | 0 | Medium |
| **B2** | Tailoring diff UI + save tailored resume as tagged doc, cover letter, "apply with tailored resume" | B1, A1 | Medium |
| **B3** | Saved searches + alerts via scheduler | B1 | Small |
| **C1** | `community_spaces` + `community_posts` + `community_comments` + `community_votes`; feed (following + discover); composer for `feed`/`thread` kinds | 0 | Large |
| **C2** | Q&A kind (answers, accept, vote), tags, structured kinds (referral / interview / salary / resume example) with filtered views | C1 | Medium |
| **C3** | Reports, moderator actions, karma‑gated posting, markdown sanitization, PII flagging | C1 | Medium |
| **D1** | `admin/` app skeleton + auth gate + Overview + Users + Applications/Runs views | 0, A1 | Medium |
| **D2** | Community moderation queue, audit‑log viewer, jobs/LLM/cost dashboards | C3, D1 | Medium |
| **D3** | Feature flags + config editor, retention/GDPR tools, announcements | D1 | Small |

Critical path: **0 → A1 → (B1, C1 in parallel) → C3 → D1 → D2**.

---

## 10. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Phase 0 is a big refactor with no user‑visible payoff | Do it first anyway; every later phase multiplies its cost. Ship each of the six mechanisms as its own PR with its own tests (concurrency suites already exist as a model). |
| Legal/ToS exposure grows with a public portal + community | The `ARCHITECTURE.md` compliance guardrails stay hard requirements. Get the technology‑lawyer review before commercial launch (already flagged). Community adds standard UGC obligations: ToS, DMCA/takedown process, reporting flow — all in C3. |
| Community moderation load | Karma gates + report queue from day one; moderator role before public launch; rate limits on posting. |
| Cost blow‑up (LLM on search + tailoring + community ranking) | Keep LLM off community ranking in v1 (vote score + recency only). Search stays cost‑aware (LLM only on hard‑filtered finalists — unchanged). Admin cost dashboard (D2) makes spend visible early. |
| Admin app leaking into the public bundle | Physically separate app, separate deploy, `noindex`, no source maps, 404 (not 403) on non‑admin, optional IP/VPN gate. |
| Data model churn once real usage arrives | JSONB `payload` on `community_posts` absorbs new structured kinds without migrations; only promote a field to a real column when it needs sorting/filtering. |
| Scope creep across four surfaces at once | MVP is explicitly Workstream A on top of Phase 0. Community and Admin do not block the first usable release. |

---

## 11. Open questions to resolve per phase

- **Queue choice** (Celery vs ARQ) — decide at the start of Phase 0 with a quick spike on worker memory under 3 concurrent Playwright runs.
- **Answers: own table vs. typed comment** — decide at C2.
- **Community discover ranking** — pure vote/recency for v1; pgvector personalization is a C‑follow‑up with usage data.
- **Email provider** — needed for notifications (A2) and moderation notices (C3); pick one (Resend / SES / Postmark).
- **Admin 2FA / SSO** — required before commercial launch; not before internal use.
