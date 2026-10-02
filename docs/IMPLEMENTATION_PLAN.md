# Implementation Plan — Executive Context Assistant

**Status:** Phase 0 in progress — slices 0.1 and 0.2 implemented (0.2 exit not yet re-verified, §0); slice 0.3 defined, not started
**Date:** 2026-10-02
**Authority:** Execution order, slices, deliverables and exit criteria. Architecture is defined in `TECHNICAL_DESIGN.md` and the documents in its §5.1; this plan must not introduce behaviour or architecture that those documents do not describe (`CLAUDE.md`: "If implementation requires changing product behavior, stop and update the appropriate document first").

---

## 0. Implementation status

Last updated 2026-10-02 (fresh-session audit and documentation-only pre-0.3 reconciliation).

| Slice | Code | Verification |
|---|---|---|
| 0.1 Repository hygiene | Implemented (commit `5b71b09`) | `.env` git-ignored and untracked (unit tests pass). gitleaks pre-commit hook over all files: passed in the current environment. Git-history scan (CI `secrets` job): not run, no remote. Gemini key rotation: not verifiable from the repository (owner action) |
| 0.2 Backend skeleton | Implemented (commit `5b71b09`) | Exit criterion "CI green; RT-15 passes" **not met in the current environment** (below) |
| 0.3 Reliability core | Not started | Scope fixed in §2 of this plan, slice 0.3 |

### 0.1 Current environment (fresh session, 2026-10-02)

- Windows 11; Python 3.11.5 virtual environment at the repository root.
- Docker is not available. No PostgreSQL listens on `localhost:5432`. `ECA_TEST_DATABASE_URL` is not set.
- No git remote, so GitHub Actions has never run.
- `pytest` at the audit: **33 passed, 12 skipped**. All skipped tests are `db`-marked (4 migration tests, 8 RT-15 tests).
- After `test_user_id_must_be_a_uuid` moved to `tests/unit/test_platform.py` (it checks a constructor argument and opens no connection): **34 passed, 11 skipped**.
- `ruff check`, `ruff format --check`, `mypy` (strict, 29 source files) and `lint-imports` (3 contracts kept): pass.
- **Not verified in this environment:** migration tests, RT-15, CI.

### 0.2 Previous environment evidence (kept as history, not as current verification)

- The message of commit `5b71b09` lists the integration and RT-15 tests. It also records two defects found while running against a database: the readiness probe hung 130 s on an unreachable database (now bounded to 3 s), and uvicorn forced a Proactor loop on Windows (selector loop factory added).
- The project owner reports that RT-15 passed against a local PostgreSQL in that environment.
- No test log is stored in the repository, and the run cannot be reproduced in the current environment.

---

## 1. Approach

- **Reliability core before features.** The hardest invariants (outbox, extract/apply separation, idempotency, concurrency, source-of-truth boundaries) are built and tested first, with a fake connector and recorded AI responses, before real Google integration.
- **Vertical slices.** Each slice ends with working, tested behaviour and passing gates.
- **Evaluation alongside code.** Every AI-touching slice ships with its golden cases and gates (`AI_EVALUATION.md`, `CONTEXT_EVALUATION.md`).
- **Every feature has** unit tests, integration tests where appropriate, failure-path tests and AI evaluation where AI is involved (`CLAUDE.md`).

### 1.1 External prerequisites (owner: product/eng lead)

| Item | Needed by |
|---|---|
| Google Cloud project; OAuth consent screen in Testing; test users added | Slice 1.1 |
| Gemini API key on the **paid tier** (free tier forbidden for user data) | Slice 0.4 (smoke test can run on any key; user data only on paid) |
| Decision on OAuth app type (Q2) and start of verification | Before first external pilot |
| Hosting decision (Q1) | Before first hosted pilot (not needed for local development) |
| Owner for dataset labelling and weekly human review (Q12) | Slice 0.5 |
| PostgreSQL 16+ with pgvector ≥ 0.8 reachable from the development machine (Docker Desktop with `docker compose up -d db`, or a native install) and `ECA_TEST_DATABASE_URL` set | Re-verifying the slice 0.2 exit; all of slice 0.3 |
| GitHub remote with Actions enabled | "CI green" exits of slices 0.2 and 0.3 |

---

## 2. Phase 0 — Foundation

### Slice 0.1 Repository hygiene

- `.gitignore` (`.env`, `.venv/`, `node_modules/`, `__pycache__/`, build output, local DB files) **before** `git init`; `.env.example` with variable names only.
- Rotate the Gemini key currently stored in `.env` if it was ever shared; never commit it.
- Pre-commit: ruff (lint + format), mypy (strict on `eca`), gitleaks secret scan.
- **Exit:** repository initialised; secret scan clean.

### Slice 0.2 Backend skeleton

- Package layout per `TECHNICAL_DESIGN.md` §5.4 and module map (`BACKEND_DESIGN.md` §5); `import-linter` contracts (§5.3).
- Settings (pydantic-settings) matching existing `.env` names (`API_*`, `GEMINI_API_KEY`, `NEXT_PUBLIC_API_URL`).
- FastAPI app, `/healthz`, `/readyz`; domain errors and Problem Details translation (`BACKEND_DESIGN.md` §14); request IDs; structlog; Sentry hook (disabled locally).
- SQLAlchemy async with psycopg 3; unit of work with `SET LOCAL app.user_id`; Alembic baseline; Docker Compose with `pgvector/pgvector` Postgres 16+; RLS helper and fail-closed test (RT-15).
- CI: lint, types, unit + integration tests against Postgres container.
- **Exit:** CI green; RT-15 passes.
- **Status:** code complete; exit pending (§0): RT-15 must pass on PostgreSQL in the current environment, and CI must be green once a remote exists.

### Slice 0.3 Reliability core

Authoritative design: `BACKEND_DESIGN.md` §7 (outbox, dispatch, reconciler, access model), §14.3 (retries), §15 (queues), §21 (RT-01 levels, RT-05, RT-15). This slice builds delivery infrastructure only. It creates no domain table, no `users`, no `source_items`.

**Database (migration `0002`, platform)**

- `outbox` and `event_consumptions` exactly as `BACKEND_DESIGN.md` §7.3, with `ix_outbox_pending (next_attempt_at) WHERE status = 'pending'` and `ix_outbox_user (user_id) WHERE user_id IS NOT NULL`; `event_consumptions` PK `(event_id, handler)` and FK `event_id → outbox(id)`.
- `outbox.user_id` has **no FK yet**. Slice 1.1 adds `fk_outbox_user` in the migration that creates `users` (`BACKEND_DESIGN.md` §7.3.1).
- Procrastinate schema for the pinned Procrastinate version, applied by an Alembic migration (never by `procrastinate schema --apply` at runtime). A later Procrastinate upgrade ships its schema migration as a new Alembic revision.
- Worker role `eca_worker` and the privilege and policy matrix of `BACKEND_DESIGN.md` §7.6. The migration grants DML and default privileges to `eca_worker`. It revokes `eca_app`'s default grants on `outbox` (except INSERT), on `event_consumptions` and on all Procrastinate objects. It enables and forces RLS on `outbox` and `event_consumptions` with the role-targeted policies `outbox_api_insert`, `outbox_worker_all` and `event_consumptions_worker_all`. The migration refuses a worker role that is SUPERUSER or BYPASSRLS, like `0001`.
- Settings `API_WORKER_DATABASE_URL` and `API_DB_WORKER_ROLE` (default `eca_worker`); `.env.example`, `docker/postgres/init/` and the test fixtures create the role. `API_DB_RUNTIME_ROLE` keeps naming the API role.

**Event infrastructure (`eca.platform`)**

- Envelope: `id` (UUIDv7 generated by `publish`), `event_type`, `user_id` (copied from the active unit of work, never passed by the caller; NULL in a unit of work without a user), `aggregate_type`, `aggregate_id`, `payload` (validated by the Pydantic model registered for the type; IDs and small facts only), `correlation` (request ID from logging context), `created_at`.
- Event identity is `outbox.id`. It is the key in `event_consumptions` and in job lock keys.
- `register_event(type, payload_model)`. `publish` rejects an unregistered type at write time. `publish(uow, event)` inserts into `outbox` in the caller's transaction and does nothing else.
- Handler registration: decorator `handles(event_type, name=…, queue=…)`. Names are unique and stable (registry rejects duplicates). Domain modules register handlers; the `eca.worker` composition package imports them. `platform` imports no domain module.
- Handler wrapper (transactional mode): one `eca_worker` unit of work per job, with `app.user_id` = the event's `user_id` (unset for system events). First statement: `INSERT INTO event_consumptions … ON CONFLICT DO NOTHING RETURNING event_id`. No row → return without effects. Then the handler body, then commit. Any exception → rollback → Procrastinate retry (handler class in `BACKEND_DESIGN.md` §14.3: base 5 s, max 10 min, 8 attempts; after that a dead job and an alert log). The natural-key mode for handlers that call external services before their transaction is added in slice 1.4, when the first such handler exists.

**Dispatcher (`eca.platform`, run by the worker)**

- Loop: tick every 1 s when idle; loop again at once when a batch was full (batch size configurable, default 100).
- Claim: `SELECT … FROM outbox WHERE status = 'pending' AND next_attempt_at <= now() ORDER BY next_attempt_at, id LIMIT :n FOR UPDATE SKIP LOCKED`, in one `eca_worker` transaction with `app.user_id` unset.
- Per row: the dispatch step of `BACKEND_DESIGN.md` §7.3. Lifecycle `pending → dispatched`, or `pending → failed` at 10 attempts. Backoff `min(1 s · 2^attempts + random(0–1 s), 5 min)`. Unregistered event type = error. Zero handlers = dispatched.
- Job keys: `queueing_lock = lock = "<handler>:<event_id>"`. `AlreadyEnqueued` = success. Job arguments carry the envelope, which holds no content.
- Stale dispatch: no persisted intermediate state. A crash releases row locks and leaves rows `pending`; the next tick or the reconciler picks them up.
- Duplicate dispatch: absorbed by `queueing_lock` while the first job is queued, by `lock` while it runs, and by `event_consumptions` after it committed (`BACKEND_DESIGN.md` §7.4).

**Worker (`eca.worker`, new composition package)**

- `python -m eca.worker` starts one Procrastinate worker per queue with the `BACKEND_DESIGN.md` §15 concurrency (`events` 8, `sync` 4, `ingest` 8, `extract` 8, `apply` 8, `embed` 2, `ai_standard` 4, `media` 1, `schedule` 1), the dispatcher loop, and the periodic tasks on `schedule`: `reconcile` (every 5 min) and Procrastinate stalled-job recovery for jobs whose worker died. In this slice only infrastructure tasks and test handlers are registered.
- One driver: Procrastinate's psycopg 3 connector and the SQLAlchemy engine both connect as `eca_worker`. On Windows the worker uses the selector event loop (`BACKEND_DESIGN.md` §2.2).
- Transaction boundaries: the business transaction (with its outbox row) commits before any job exists. Each dispatcher batch is one transaction. Procrastinate writes jobs through its own connection. Each handler job is one transaction that includes its consumption record.
- Graceful stop: stop claiming and fetching, then finish in-flight jobs up to a timeout. Unfinished jobs are recovered by stalled-job recovery.
- `eca.worker` is added to `composition_modules` in the import-linter configuration.
- Operator command `eca ops outbox retry [--event-id …]` (`failed` → `pending`, `attempts = 0`, runs as `eca_worker`). It is the only `eca ops` subcommand in this slice, because `failed` must be recoverable.

**Reconciler (infrastructure only, `BACKEND_DESIGN.md` §7.5)**

- Periodic `reconcile` task: one dispatch pass over `pending` rows older than 1 minute and past `next_attempt_at` (same dispatch step, `SKIP LOCKED`, safe beside a live dispatcher). It logs the oldest pending age and the `failed` count.
- It does not re-queue `failed` rows, does not touch dead jobs, and does not read `source_items`, extractions or any domain table. Source-item reconciliation arrives in slice 1.3.

**Reliability utilities and tests**

- Crash points: named hooks in dispatcher and handler code (`dispatch.after_claim`, `dispatch.after_defer`, `dispatch.before_commit`, `handler.after_consumption`, `handler.before_commit`, `handler.after_commit`). They are no-ops unless enabled by a test-only environment variable, and they refuse to activate when `API_ENV` is production. When armed, the process exits immediately with `os._exit` (no cleanup). Tests run the worker or dispatcher in a subprocess, kill it at a crash point, restart it and check the database.
- Synthetic test fixtures: test-only event types and handlers. Each handler appends a row to a test-only effect table **without** a unique constraint, so a duplicate effect is visible instead of hidden by a key.
- **RT-01, infrastructure level** (`BACKEND_DESIGN.md` §21):
  - (a) A business transaction commits a row and an outbox event while no dispatcher runs. A dispatcher started later dispatches it. The effect occurs once.
  - (b) Same, with a crash at `dispatch.after_claim`.
  - (c) A rolled-back business transaction leaves no outbox row and no effect.
  - (d) Crash at `handler.before_commit`: rollback and retry, then one effect.
  - (e) Crash at `handler.after_commit`: the job is re-run, the consumption conflict makes it a no-op, one effect.
  - (f) A handler that raises twice then succeeds: one effect.
  - (g) Two handlers on one event: one effect each.
  - (h) Two dispatchers at once: each event deferred once per handler.
- **RT-05:** crash at `dispatch.after_defer` and at `dispatch.before_commit`. The row is re-dispatched. The duplicate is rejected by `queueing_lock` while the first job is queued, serialized by `lock` while it runs, and a no-op through `event_consumptions` after it committed. Each handler's effect occurs once.
- **RT-15 extension:** the role and privilege assertions of `BACKEND_DESIGN.md` §21 for `eca_app` and `eca_worker`, plus the existing RT-15 suite.
- Other tests:
  - Unit, no database: envelope and payload validation, registry rules, backoff formula, lock-key format, crash points inert in production, worker settings.
  - PostgreSQL: migration `0002` up/down/up; privilege matrix; dispatcher lifecycle (`failed` after 10, zero-handler and unregistered types, backoff timing with a controllable clock); handler wrapper (consumption conflict, rollback leaves no consumption); reconciler pass; `outbox retry`.

**What RT-01 at this level does not prove:** Gmail normalization, email or AI extraction, apply, the real message lifecycle, `source_items` processing or production email correctness. Slices 1.3 and 1.4 extend RT-01 to pipeline level (§8 below); Phase 1 exit requires the pipeline level.

**Test environments**

| Needs | Tests |
|---|---|
| Nothing (local, no infrastructure) | Unit tests; ruff, format, mypy, lint-imports |
| PostgreSQL 16+ with pgvector ≥ 0.8 (`ECA_TEST_DATABASE_URL`) | Migrations, privilege matrix, RT-15, dispatcher/handler/reconciler integration, RT-01 (infrastructure level), RT-05, 100-run loops |
| Docker | None intrinsically. Docker Compose is the supported way to provide that PostgreSQL locally, and the CI service container uses Docker on the runner |
| GitHub remote and Actions | The CI run itself, including the git-history gitleaks scan |

**Exit criteria**

Slice 0.3 is **locally verified** when all of these hold on one machine with PostgreSQL:

1. ruff, ruff format, mypy (strict) and lint-imports (with `eca.worker` as a composition module) pass.
2. The full pytest suite passes with `ECA_TEST_DATABASE_URL` set and **zero skipped tests**. This includes RT-15, which also closes the open slice 0.2 exit locally.
3. RT-01 (infrastructure level) and RT-05 pass **100 consecutive runs** with zero failures and no test-level reruns. The command and its summary line are recorded in the slice's PR or commit description.
4. No domain schema was added (`users`, `source_items` and other domain tables are absent), and `outbox.user_id` has no FK.
5. §0 of this plan is updated with the measured results.

Slice 0.3 is **complete** when, in addition, GitHub Actions is green on the slice's commit (lint, tests with the PostgreSQL service including RT-01, RT-05 and RT-15, gitleaks). Until a remote exists, the slice stays "locally verified", not "complete".

### Slice 0.4 AI provider layer

- Provider layer inside the `intelligence` module (`eca/intelligence/provider/`, prompts in `eca/intelligence/prompts/`, structured-output schemas in `eca/intelligence/output_schemas/`; there is no `eca.ai` package, `BACKEND_DESIGN.md` §5.4): wrapper over `google-genai` (structured output, embeddings, Files API); role registry `config/models.yaml` (model, thinking, temperature, fallback per role); pricing `config/pricing.yaml` with effective dates (`AI_COST_MODEL.md` §2); `ai_calls` meter and 15-minute cost roll-ups (`AI_COST_MODEL.md` §8); attempt cap logic (`AI_PIPELINE.md` §7); cassette recorder/replayer keyed by `(role, prompt_version, input_hash)`; provenance envelope type (`AI_PIPELINE.md` §5.1).
- Import-linter `forbidden` contract: only `eca.intelligence` imports `google.genai`.
- Smoke test: verifies every configured model ID exists (resolves Q8: exact `gemini-embedding-2` ID) and replaces `test_gemini_key.py`'s deprecated default.
- **Exit:** smoke test green; cassette replay deterministic.

### Slice 0.5 Evaluation harness

- `evals/ai` and `evals/context` runners, report format with the acceptance scorecard (`AI_EVALUATION.md` §8), simulated clock, paired-bootstrap statistics.
- `world_v1` first slice: fixtures (people, orgs, projects), ~150 labelled emails, chains CC-01–CC-10 in YAML with checkpoints and queries; split assignment (dev/test/sealed/challenge); `MANIFEST.json` with hashes; CI hash and contamination checks (gate A0).
- **Exit:** runner executes end-to-end against stub pipelines and produces a scorecard; dataset slice frozen as `golden-v0.1` (expanded to `golden-v1.0` by end of Phase 1).

---

## 3. Phase 1 — Context foundation

### Slice 1.1 Identity and sessions

Google OIDC (PKCE, `state`, `nonce`), server-side sessions, CSRF, `users` + self Person, `audit_log`, `DELETE /api/v1/me` request recorded (job built in 1.9). The migration that creates `users` also adds `fk_outbox_user` (`outbox.user_id → users(id)`, `BACKEND_DESIGN.md` §7.3.1) and defines the worker role's explicit read policy for enumerating users (`BACKEND_DESIGN.md` §7.6). Tests: auth flows with mocked Google, CSRF rejection, session expiry; FK present and enforced (outbox insert with an unknown `user_id` fails); RT-15 extended to the new tables.

### Slice 1.2 Connections

Connect Gmail and Calendar (incremental scopes), envelope-encrypted refresh tokens (dev KEK from env), `granted_scopes` and feature gating, disconnect with revoke, `needs_reauth` on `invalid_grant`. Tests: token never logged/returned; denied-scope handling.

### Slice 1.3 Ingestion core with a fake connector

- Connector protocols, DTOs, registry (`BACKEND_DESIGN.md` §20); fake mail/calendar connectors reading `world_v1`.
- `source_items` with stage machine; `sync_cursors` rules (advance after durable store); normalize (MIME, quote/signature stripping, participants, person/org resolution, conversation reply state); prefilter on neutral categories; outbox events.
- Source-item reconciliation: `reconcile` extended with the stage-SLA scan, run per user (`BACKEND_DESIGN.md` §7.5).
- **Tests:** RT-01 pipeline level, first half (sync commit → `SourceItemStored` → normalized exactly once, fake connector), RT-04 (replay ×3, shuffled), RT-06 (crash mid-sync), RT-07 (duplicate triggers); E1 prefilter false-skip rate.

### Slice 1.4 Extract and apply

- `extractions` lifecycle (`BACKEND_DESIGN.md` §8.1); AI-01 prompt v1 + schema; candidate lists (`CONTEXT_ARCHITECTURE.md` §12.1).
- Apply: grounding, date resolution, direction rules, candidate matching and merge (`TECHNICAL_DESIGN.md` §13.6) under the per-user merge lock; evidence with deterministic IDs; `context_events`; `work.append_event` with row lock, fold, `version`, `user_fields`; provisional items + `AdjudicationNeeded` (AI-02 job); `messages.triage` projection.
- Recomputation commands R1 (re-fold) and R2 (re-apply).
- Rules prefilter (outbound never prefiltered), AI-01 statement schema with `gist`, stored `candidate_map`, deterministic statement-to-direction mapping (`AI_PIPELINE.md` §5.5), deterministic deadline resolver with the no-invented-dates rule (§5.4), penalty-based confidence (§5.3), forwarded-content attribution, `notes` field, provenance columns on every AI-derived row (`BACKEND_DESIGN.md` §17.1). AI-02 implemented behind a disabled flag.
- Experiments X1 (adjudication lift) and X2 (AI-01 model choice) on the slice; X9 (calibration) at Phase 1 exit.
- **Tests:** RT-01 pipeline level, complete (normalized, extracted and applied exactly once; AI call count 1 with recorded responses), RT-02, RT-02b, RT-03, RT-03b, RT-12, RT-13, RT-14; E1–E4 (incl. `DIR-120` and invented-date check), E13, E14 on the `world_v1` slice; CC-01–CC-10, CC-34–CC-37, CC-40–CC-45 at L2.
- **Exit:** A1 gate thresholds met on the slice; first baseline stored in `evals/ai/baselines/`; all listed RT tests green.

### Slice 1.5 Gmail adapter

Real Gmail: profile `historyId` before import, 72 h interactive import then throttled 30-day import (≤ 4,500 units/min per connection), `history.list` incremental, 404 bounded re-sync, deletions and Trash handling (`BACKEND_DESIGN.md` §9.2–9.3), adaptive cadence with jitter. Contract tests with recorded fixtures (404, 429, `invalid_grant`).

### Slice 1.6 Calendar adapter

`events.list` with `singleEvents`, sync token + `query_fingerprint`, etag skip, 410 handling, daily window roll-forward, meeting upsert, participants, cancellations. Contract tests.

### Slice 1.7 Work API and corrections

REST endpoints for work items, decisions, conversations, people, organizations (`BACKEND_DESIGN.md` §16): path IDs, PATCH with `If-Match` (412) or `base_version` (field-level 409), action sub-resources, `Idempotency-Key`, keyset cursors, Problem Details. All corrections in `BACKEND_DESIGN.md` §9.9 as user events + `feedback_events`. OpenAPI published; TypeScript client generated.

### Slice 1.8 Priority v1 and Today

Priority features and scoring (`TECHNICAL_DESIGN.md` §12.6) as event handlers; `GET /api/v1/today`; minimal Next.js Today and Tasks pages with confirm/reject/edit (provenance labels, evidence links).

### Slice 1.9 Deletion and retention

Provider deletion flow (§9.3), source purge on disconnect, account deletion job (`BACKEND_DESIGN.md` §13.3, including the user's `event_consumptions` and `outbox` rows before the user row), retention purge job (including the 7-day purge of dispatched outbox rows and their consumptions), "Your data" summary endpoint. **Tests:** RT-10, RT-11.

**Phase 1 exit:** PRD §57 items 1–3, 5–9, 20 (v1) and 21 demonstrable with a real Gmail test account; RT-01 (pipeline level), RT-02–RT-07 and RT-10–RT-15 green; `golden-v1.0` (full email set) frozen and baselined; E1–E4, E13, E14 targets met on its `test` split; X1, X2 and X9 decided and recorded; measured AI-01 tokens within 30% of `AI_COST_MODEL.md` §3; CC-01–CC-10 pass at L2.

---

## 4. Phase 2 — Assistant

| Slice | Content | Tests |
|---|---|---|
| 2.1 Indexing | Chunking and search parameters (`CONTEXT_ARCHITECTURE.md` §9.9), AI-04 embeddings, FTS, `entity_mentions`, thread continuation links (§12.2) | Index idempotency; mention accuracy |
| 2.2 Retrieval | Typed retrievers (S1, S2, S6–S9, S12), hybrid search with RRF, ≤ 2-hop expansion, permission scope (§9.7), ranking, coverage block, packet layout and budgets | L3 suites; E6 |
| 2.3 Change feed and day view | `user_checkpoints`, net-change fold, `/changes`, `/days/{date}` (S10, S11); hourly time sweeper | CC-23–CC-26 |
| 2.4 Chat | Planner (rules + AI-05), deterministic list answers, AI-06 lookups, AI-07 synthesis with claim kinds and citations, online grounding checks and abstention (`AI_PIPELINE.md` §5.7), SSE endpoint, session context (last 4 turns + entity focus), `retrieval_traces`; experiments X6, X7 | E7, E8, E10, E15, E16; L4/L5; SS scripts |
| 2.5 Projects | Hint matching, topic mode (S3), project suggestions and confirmation | CC-21, CC-22 |

**Phase 2 exit:** PRD §57 items 12–13; `CONTEXT_EVALUATION.md` G3/G4 thresholds on S1–S3 and S6–S12; ablation R1–R5 run once and recorded (validates the no-graph decision).

---

## 5. Phase 3 — Executive intelligence

| Slice | Content | Tests |
|---|---|---|
| 3.1 Reminders | Rules, fingerprints, suppression, caps, quiet hours (`TECHNICAL_DESIGN.md` §15); notifications with idempotency; Web Push | RT-09; E9 |
| 3.2 Summaries and briefing | Gist timelines; AI-03 for threads ≥ 8 relevant messages or on request (experiment X3); fully deterministic daily briefing | E12; cost per user |
| 3.3 Reply guidance | AI-08 with copy-only drafts | Human review sample |
| 3.4 People | People page, relationship profiles, person context (S2) | S2 suite |
| 3.5 Priority fitting and budgets | Fit weights on `priority_pairs`, per-user learning bounds, cost guardrails and degradation routes (`AI_COST_MODEL.md` §7, `AI_PIPELINE.md` §8.4) | E5; budget-cap tests |

**Phase 3 exit:** PRD §57 items 4, 10, 11, 20, 22; E5 and E9 targets.

---

## 6. Phase 4 — Meeting intelligence

| Slice | Content | Tests |
|---|---|---|
| 4.1 Upload | `POST /recordings` (pre-signed URL, `Idempotency-Key`, sha256 dedupe), complete, status, retry | RT-08 |
| 4.2 Media and transcription | ffmpeg audio extraction, AI-09 single call via Files API (window fallback; file deleted after), transcript versioning (no automatic re-transcription), transcript-file parsing; experiment X5 | E11 (WER, diarization) |
| 4.3 Meeting extraction | Speaker mapping (`unresolved` direction until confirmed), prior-meeting context, AI-10 with statement schema, apply, "what changed since previous meeting"; experiments X4, X10 | CC-11–CC-17, CC-38, CC-39, CC-46; E2–E4 on transcripts |
| 4.4 Meeting prep and chat | Deterministic prep sections with version invalidation; AI-11 suggested asks on open (experiment X8); structured meeting lookups; meeting-scoped chat sessions (`CONTEXT_ARCHITECTURE.md` §13) | S4, S5 suites; CC-46 |

**Phase 4 exit:** PRD §57 items 14–19; full MVP release gate (`AI_EVALUATION.md` A5).

---

## 7. Post-MVP (not scheduled)

Gmail/Calendar push (webhooks as sync signals), Batch API import with submission ledger, Microsoft Graph and Slack adapters (contract suite reused), OpenTelemetry exporter, separate media worker deployment, rerankers, graph retrieval only if `CONTEXT_EVALUATION.md` §10 triggers fire.

---

## 8. Concurrency and integrity coverage

| Requirement | Mechanism (`BACKEND_DESIGN.md`) | Slice | Test |
|---|---|---|---|
| Row locking | `append_event` locks the item row (§12.2) | 1.4 | RT-03b |
| Optimistic concurrency | `version`, `If-Match` → 412, `base_version` → 409 (§12.3) | 1.7 | API tests |
| Version numbers | `version` on items, decisions, persons, conversations, meetings, projects, reminders | 1.4, 1.7 | API tests |
| Conflict detection | Field-level check; `conflict_detected` events (§12.3, `CONTEXT_ARCHITECTURE.md` §8.3) | 1.4, 1.7 | CC-10, CC-31 |
| User edits take precedence over AI | Authority 5, `user_fields` | 1.4 | RT-03b, RT-13 |
| Concurrent commitment matching | Apply under per-user merge lock with re-matching (§12.4) | 1.4 | RT-03 |
| Per-user serialization where required (no global locks) | Advisory lock keyed by user on apply/merge only | 1.4 | RT-03; lock-wait metric |
| Outbox atomicity and duplicate dispatch | §7 | 0.3 (infrastructure); 1.3, 1.4 (pipeline) | RT-01 (infrastructure level in 0.3, pipeline level in 1.3–1.4), RT-05 |
| Infrastructure isolation by role (API cannot read cross-user delivery data) | §7.6 | 0.3 | RT-15 (extended) |
| Extract/apply separation | §8 | 1.4 | RT-02, RT-02b |
| Replay idempotency | §10 | 1.3, 1.4 | RT-04 |
| AI cannot write source tables | §6.3 | 1.4 | RT-12 |
| Recomputation preserves user data | §8.5 | 1.4 | RT-13, RT-14 |

---

## 9. Recommended first implementation slice

**Slices 0.1 → 0.2 → 0.3 → 0.4 → 1.3 → 1.4, using the fake connector and recorded AI responses** ("reliability walking skeleton"):

A labelled `world_v1` mailbox flows through sync → outbox → normalize → extract (AI-01 statement schema) → apply (deterministic direction mapping, date resolver, penalty-based confidence, candidate maps, provenance) → work items with evidence and timelines, readable through a minimal `GET /api/v1/work-items` — with RT-01 (pipeline level) to RT-05 and RT-12 to RT-15 green, zero canonical-phrase confusion on `DIR-120`, zero invented dates, and CC-01–CC-10, CC-34–CC-37, CC-40–CC-45 passing at L2 (`AI_PIPELINE_REVIEW.md` Part 3).

Why first: it proves the four high-severity backend guarantees and the extraction quality baseline before any OAuth verification, UI or Google quota concerns, and every later slice builds on these modules unchanged.
