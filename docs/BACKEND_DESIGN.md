# Backend Design — Executive Context Assistant

**Status:** Reconciled architecture; slices 0.1–0.2 implemented. Pre-0.3 reconciliation (2026-10-02): outbox FK timing (§7.3.1), database roles and access model (§7.6), reconciler scope (§7.5), AI provider location (§5.4), RT-01 levels (§21). Slice 0.3 finalization (2026-10-02): API-role outbox insert (§7.3.3), exact privilege mechanics and role lifecycle (§7.6), migration structure (§7.7), handler retry strategy and stalled jobs (§14.3, §15), worker health scope (§16.5), crash-test harness (§21)
**Date:** 2026-10-02
**Authority:** This document is authoritative for backend modules, the transaction and event model, the source-of-truth model, extraction/apply stages, idempotency, synchronization, concurrency, deletion, errors and retries, jobs, the HTTP API and the database schema. AI calls and their costs are defined in `AI_PIPELINE.md`; context and retrieval in `CONTEXT_ARCHITECTURE.md`.
**Inputs:** `docs/PRD.md`, `docs/TECHNICAL_DESIGN.md`, `docs/CONTEXT_ARCHITECTURE.md`, `docs/AI_PIPELINE.md`, `CLAUDE.md`, installed skills (`ai-toolkit:backend-api-design`, `ai-toolkit:database-patterns`, `ai-toolkit:python-api-endpoint-creator`), official Google and Procrastinate documentation (References)

---

## 1. Summary

The backend is a **modular monolith** in Python (FastAPI, Pydantic v2, SQLAlchemy 2 async with psycopg 3, Alembic) on one PostgreSQL database. It runs as two process types from one image: `api` and `worker`. Modules follow domain boundaries and talk through service interfaces and persisted domain events.

| Guarantee | Mechanism | Section |
|---|---|---|
| Background work is never lost between a transaction and job submission | Transactional outbox → dispatcher → Procrastinate job; consumer-side dedupe | §7 |
| AI failure cannot corrupt state; apply retries never re-pay AI | Separate `extract` (AI, persists result) and `apply` (deterministic, one transaction) stages | §8 |
| The same email or event never creates duplicate tasks | Idempotency key for every side effect and every externally triggered operation | §10 |
| Unchanged information is not reprocessed | Provider cursors, content hashes, etags, extraction keys including `content_hash` | §11 |
| Concurrent updates are safe | Event-first writes, item row locks, per-user merge lock, versioned optimistic concurrency, user authority | §12 |
| Source of truth is explicit | Four data classes, one authoritative writer per table, rebuild levels | §6 |
| Deletion is real where required | Logical deletion for operations, permanent deletion for privacy, ordered deletion jobs, no cascades | §13 |
| Google stays at the edge | Connector adapters producing normalized objects | §20 |

---

## 2. Conventions

### 2.1 Review of the installed toolkit skills

The toolkit skills were written for Kotlin/Micronaut services (Exposed, `Either`, `@ExecuteOn`, an axios interceptor that rewrites parameter names). Each rule was evaluated for this Python project on its technical merit.

| Rule | Decision | Reason |
|---|---|---|
| Thin router → service → repository | **Adopt** | Keeps business rules out of HTTP and reusable by workers |
| Commit/rollback in the unit-of-work, never in repositories | **Adopt** | One transaction per request/job |
| `response_model` on every endpoint; `201` on create; field-level `422` | **Adopt** | Prevents field leaks; standard semantics |
| `TEXT` not `VARCHAR`; UUID primary keys; `id, created_at, updated_at, deleted_at` on mutable entity tables; partial indexes `WHERE deleted_at IS NULL`; sequential, single-purpose migrations | **Adopt** | Sound Postgres practice |
| POST for all mutations; IDs in query parameters; no path parameters | **Reject** | The rule exists for a Micronaut/axios parameter-binding issue that does not apply to FastAPI. Conventional REST (path IDs, PATCH/DELETE semantics) is what future developers, OpenAPI tooling, generated TypeScript clients and HTTP caches expect (§16) |
| No foreign keys | **Reject in part** | Keep FKs where they protect integrity, especially AI-derived → source references; no cascades (§17.1) |
| Soft delete only | **Reject in part** | Logical deletion where operationally useful; permanent deletion where privacy requires it (§13) |
| Services raise `HTTPException` | **Reject** | Services and workers must not depend on HTTP; the API layer translates domain errors (§14) |
| Offset pagination | **Replace** | Keyset cursor pagination for all collections (§16.4) |

### 2.2 Python stack conventions

- One package `eca` with one sub-package per module (§5). Each module: `router.py`, `service.py`, `repository.py`, `models.py`, `schemas.py`, `events.py`, `tasks.py`.
- Async everywhere (FastAPI, SQLAlchemy async, httpx). Database driver: psycopg 3 (`postgresql+psycopg`) for SQLAlchemy and Procrastinate, so there is one driver. On Windows development hosts psycopg async requires the selector event loop; uvicorn ignores the loop policy and creates a Proactor loop on Windows, so the API is started with `--loop eca.platform.runtime:selector_loop_factory` there (no effect on Linux). Local services (PostgreSQL) run in Docker.
- IDs: UUIDv7 generated in the application. IDs whose re-creation must be stable (evidence) are UUIDv5 derived from their natural key (§10).
- Time: UTC `timestamptz` in storage; user timezone applied in services.

---

## 3. Audit findings and status

| # | Sev | Finding | Status |
|---|---|---|---|
| B1 | H | Earlier text claimed Procrastinate enqueues inside the business transaction. It does not: it uses its own connection, so a crash between commit and enqueue could lose work | **Resolved** — transactional outbox, dispatcher, consumer dedupe (§7) |
| B2 | H | Extraction and state application were one step | **Resolved** — `extract` and `apply` stages (§8) |
| B3 | H | No concurrency design for items | **Resolved** — §12 |
| B4 | H | Idempotency covered only extractions | **Resolved** — every side effect keyed (§10) |
| B5 | M | No per-source processing state | **Resolved** — stage machine + reconciler (§7.5) |
| B6 | M | Duplicate copies of the same email | **Resolved** — `UNIQUE(user_id, rfc822_message_id)` (§10) |
| B7 | M | Calendar mutation handling | **Resolved** — etag comparison, deterministic updates (§9.5, §11.3) |
| B8 | M | Batch API not idempotent | **Resolved by scope** — Batch API deferred from MVP (`AI_PIPELINE.md` §6.3) |
| B9 | M | No API concurrency control or idempotency keys | **Resolved** — `version` + `If-Match`/`base_version`, `Idempotency-Key` (§16) |
| B10 | M | Merged entity references | **Resolved** — `merged_into_id` redirection (§12.5) |
| B11 | M | Module boundaries not enforced | **Resolved** — `import-linter` contracts (§5.3) |
| B12 | M | Gmail quota not budgeted | **Resolved** — per-connection throttle (§11.6) |
| B13 | L | Two Postgres drivers | **Resolved** — psycopg 3 only |
| B14 | L | Migrations at startup | **Resolved** — release step only |
| B15 | L | No dashboard caching strategy | **Resolved by scope** — dashboard is indexed SQL; no cache in MVP (§18) |
| B16 | L | Webhooks could bypass cursors | **Resolved** — webhooks are sync signals (§11.5) |
| B17 | M | Editable content (future Teams/Slack/Outlook) would not be re-extracted because the extraction key lacked the content hash | **Resolved** — key includes `content_hash` (§8.1) |
| B18 | M | Apply invoked adjudication (an AI call) | **Resolved** — adjudication is its own extract-type job; apply never calls AI (§8.3) |

---

## 4. Architecture style and deployment

### 4.1 Modular monolith

A single deployable keeps one transaction around the context core (items, evidence, events, persons, reminders, outbox). A small team, MVP scale (≤ 100 users under Google's Testing mode) and no independent release cadence give no reason for microservices. **Split criteria** (measured): another team needs independent releases; a workload harms others despite queue isolation; a compliance boundary requires separate stores.

### 4.2 Process types (one image)

| Process | Command | Runs | Database role (§7.6) |
|---|---|---|---|
| `api` | `uvicorn eca.api.app:app` | HTTP API, SSE streaming, OAuth callbacks | API role `eca_app` |
| `worker` | `python -m eca.worker` (composition package, like `eca.api`): one Procrastinate worker per queue (§15) + outbox dispatcher loop; reconciler as a periodic task | All background work, including media | Worker role `eca_worker` |
| `release` | `alembic upgrade head` | Migrations, once per deploy | Migration role (schema owner) |

Media processing (ffmpeg) runs on the `media` queue with concurrency 1 in the same worker for the MVP. A separate `worker-media` deployment of the same image is an operational option when resource contention is measured, not a design requirement.

---

## 5. Modules and boundaries

### 5.1 Module map (single writer per table)

| Module | Owns (only writer) | Public services (examples) | Publishes events |
|---|---|---|---|
| `platform` | `outbox`, `event_consumptions`, `idempotency_keys` | UoW, `publish(event)`, job enqueue, clock, config, object storage, errors | — |
| `identity` | `users`, `auth_sessions`, `user_preferences`, `user_checkpoints` | current user, sessions, preferences, checkpoints | `UserCreated`, `UserDeletionRequested` |
| `connections` | `connections`, `sync_cursors` | connect, token access, disconnect, reauth state | `ConnectionStatusChanged` |
| `ingestion` | `source_items` | sync orchestration, uploads registration, stage transitions | `SourceItemStored`, `SourceItemMetadataChanged`, `SourceItemContentChanged`, `SourceItemTrashed`, `SourceItemRestored`, `SourceItemDeleted` |
| `connectors` | none (adapters) | `MailConnector`, `CalendarConnector`, `AuthConnector`, later `ChatConnector` | — |
| `communication` | `conversations`, `messages`, `message_participants` | normalize message, thread context, mark handled | `MessageNormalized`, `ConversationStateChanged` |
| `meetings` | `meetings`, `meeting_participants`, `recordings`, `transcript_segments` | upsert from calendar, uploads, media pipeline, speaker mapping, prep context | `MeetingChanged`, `RecordingUploaded`, `TranscriptStored`, `MeetingProcessed` |
| `people` | `persons`, `person_identifiers`, `organizations`, `entity_mentions` | resolve, merge, person context, profile updates | `PersonChanged`, `PersonsMerged` |
| `work` | `work_items`, `work_item_owners`, `decisions`, `evidence`, `item_evidence`, `context_events`, `entity_links` | `apply_extraction`, `append_event`, fold, user actions, merge, queries by direction | `WorkItemChanged`, `DecisionChanged`, `ConflictDetected` |
| `projects` | `projects`, `project_members` | hint matching, topic mode, project context | `ProjectChanged` |
| `intelligence` | `extractions`, `ai_calls` | `extract_message`, `extract_meeting`, `adjudicate`, `embed`, `summarize`, AI provider layer (§5.4) | `ExtractionCompleted`, `ExtractionFailed` |
| `retrieval` | `chunks`, `retrieval_traces` | index, plan, retrieve, assemble packet, day query | — |
| `chat` | `chat_sessions`, `chat_messages` | sessions, answer stream, reply guidance | — |
| `attention` | `reminders`, `notifications`, `briefings`; priority columns written via owning modules' services | priority, reminders, prep sections, briefing | `ReminderDue` |
| `privacy` | `audit_log`, `export_jobs`, `deletion_jobs` | export, delete account, purge source, retention | — |

### 5.2 Dependency rules

```text
chat → retrieval → (read) work · people · projects · meetings · communication
attention → work · communication · meetings · people        (writes priority via their services)
meetings → intelligence · people · ingestion
chat · retrieval → intelligence                               (model calls only, through its public API; §5.4)
communication → people · ingestion
work → people · projects
ingestion → connections → identity
connectors are imported only by ingestion and connections (via the connector registry)
every module → platform
```

Reactions that would create cycles (e.g., `work` changes → `attention` recomputes) go through outbox events.

### 5.3 Enforcement

- Modules import each other only through `eca/<module>/__init__.py` (services, DTOs, events).
- `import-linter` contracts in CI: layer order (router → service → repository), no cross-module `repository`/`models` imports, `connectors.*` only from `ingestion`/`connections`, `intelligence` cannot import any source-owning module's repository.
- Each module implements `purge_user(user_id)`, `purge_connection(connection_id)` and `export_user(user_id)`.
- Composition packages `eca.api` (slice 0.2) and `eca.worker` (slice 0.3) wire modules together; they are listed as `composition_modules` in the import-linter configuration and are imported by nothing.

### 5.4 AI provider layer location (decision)

The AI provider layer lives **inside the `intelligence` module**. There is no `eca.ai` package.

```text
backend/eca/intelligence/provider/{__init__,gemini,registry,meter,cassette}.py   provider wrapper, role registry, ai_calls meter, cassettes
backend/eca/intelligence/prompts/<role>/v<N>.md                                  versioned prompts
backend/eca/intelligence/output_schemas/<role>.py                                Pydantic structured-output schemas
```

`output_schemas/` is named so that it does not collide with the per-module `schemas.py` (API DTOs, §2.2).

| Reason | Detail |
|---|---|
| Single writer | `intelligence` owns `ai_calls` (§5.1). The meter writes `ai_calls`, so it must be inside the owning module |
| One choke point for cost and safety | Attempt caps (`AI_PIPELINE.md` §7), per-user budgets and degradation (`AI_COST_MODEL.md` §7) and metering apply to every call only if every call passes through one module's public API |
| Existing contracts keep their meaning | The invariant "`intelligence` cannot import a source-owning module's repository" (§5.3, §6.3) covers all AI code only if all AI code is in `intelligence` |
| No new boundary type | A top-level `eca.ai` would be either a 15th domain module (splitting AI ownership with no boundary benefit) or a shared layer like `platform` (importable everywhere, so callers could bypass budgets, and a shared layer would write a domain table) |

Consequences: modules that need a model call (`meetings`, `chat`, `retrieval`, and the extraction jobs) use `eca.intelligence`'s public API only. Slice 0.4 adds an import-linter contract so that only `eca.intelligence` imports `google.genai` (a custom `eca_restricted_imports` contract in `eca_devtools`: grimp collapses external packages to their top-level name, `google`, so the contract inspects the import statements; other `google.*` packages stay available to connectors). Prompts, output schemas and cassettes are versioned with the module.

### 5.5 AI runtime configuration, metering and cassettes (slice 0.4 decisions)

| Topic | Decision |
|---|---|
| Configuration files | `config/models.yaml` (role registry) and `config/pricing.yaml` (prices with effective dates) at the repository root (`TECHNICAL_DESIGN.md` §5.4). `eca.intelligence` loads them once at process start from `API_AI_CONFIG_DIR` (default `<repository root>/config`) and validates them with Pydantic. An invalid file is a startup error. The deployment image must copy `config/`. Changing either file is a major change (`AI_PIPELINE.md` §12) |
| Role registry | Per role: `inventory_id` (`AI_PIPELINE.md` §3), `model`, `fallback`, `thinking`, `temperature`, `max_output_tokens`, `enabled`, and `output_dimensionality` for embeddings. Code names roles, never model IDs. An unknown role and a disabled role raise distinct errors before any call |
| Cost | Computed at call time with `Decimal` from the price entry effective on the call's UTC date (`AI_COST_MODEL.md` §2), and stored as `ai_calls.est_cost_usd`. Thinking tokens are billed as output |
| Meter | Every provider call (live or record mode) writes one `ai_calls` row with IDs and numbers only, never prompt or output text. The row is written in its **own short transaction** (same role, same `app.user_id` as the caller) immediately after the response or error, not in the caller's transaction. Reason: a call whose caller later rolls back (failed job attempt, crash, repair loop) still cost money, and retry overhead must be measurable (`AI_COST_MODEL.md` §8). The insert is one Core INSERT without RETURNING (§7.3.3), so the INSERT-only API role can write it |
| Roll-ups | Periodic task `cost_rollup` (every 15 min, `schedule` queue, lock `cost_rollup`, §15) recomputes the 15-minute buckets of the last hour from `ai_calls` into `ai_cost_rollups` in one transaction: delete the buckets of the window, insert them again grouped by bucket, user, role and model. Rerunning gives the same rows (idempotent), and late commits within the hour are picked up |
| Periodic tasks of domain modules | `eca.platform` defines `PeriodicTaskSpec` (task name, `periodic_id` used as the lock and queueing lock, cron, queue, and an async function of the worker unit-of-work factory and the scheduled tick). `eca.intelligence` exposes its specs through its public API; `eca.worker` registers them on Procrastinate. `platform` still imports no domain module |
| Cassettes | Key `(role, prompt_version, input_hash)`. `input_hash` is the SHA-256 of the canonical JSON of the whole request: model ID, generation settings, system instruction, contents and output schema. A prompt, model or setting change is therefore a cache miss. One JSON file per entry: `<root>/<role>/<prompt_version>/<input_hash>.json` holding the key, model, response text and usage numbers |
| Cassette modes | `live` (production default; no cassettes), `replay` (cassettes only; a miss raises `CassetteMiss` and never reaches the network; default for tests and CI), `record` (live call, then write; evaluation tooling only, refused when `API_ENV` is production). Replayed calls make no provider call and write no `ai_calls` row |
| Cassette storage | Committed: `backend/tests/fixtures/cassettes/` (hand-written, for unit tests) and `evals/ai/cassettes/<suite>/` (reviewed recordings of synthetic evaluation data only). New recordings go to `evals/**/cassettes/live/` (git-ignored) and are copied into a committed directory only after review. A cassette is never recorded from user data |
| Attempt caps | A pure policy in `eca.intelligence` (`AI_PIPELINE.md` §7): background keys get 4 model calls including one repair, fallback model from attempt 3, budget deferrals not counted; interactive calls get one retry, then one fallback call, then degradation. The persistent per-key attempt counter lives with `extractions` (slice 1.4); slice 0.4 provides the policy and an in-process runner for interactive calls |

---

## 6. Source of truth

### 6.1 Data classes

| Class | Meaning | Authority |
|---|---|---|
| **SOURCE** | Copies of provider data and user uploads | Authoritative about *what was said/scheduled*, not about what is true |
| **COMPUTED** | Deterministic code output from source and user data | Reproducible; recomputed on rule change |
| **AI-DERIVED** | Model outputs (`extractions`) and projections built by applying them | Never authoritative; labelled; `verification_status = suggested` until the user acts |
| **USER-AUTHORED** | User actions, overrides, confirmations, user-created records | Highest authority (5); never removed by recomputation |

Columns `origin ∈ {source, computed, ai, user}` and `verification_status ∈ {suggested, confirmed, rejected, user_created}` carry the class on rows that can mix (work items, decisions, persons fields).

### 6.2 Per-table audit

"Recompute" names the rebuild level from §8.5 that regenerates the table: R1 re-fold, R2 re-apply stored extractions, R3 re-extract (AI), R4 re-sync from provider, — = not recomputable.

| Table | Class | Authoritative writer | Allowed readers | Update behaviour | Recompute | Deletion | Conflict behaviour |
|---|---|---|---|---|---|---|---|
| `users`, `user_preferences` | USER-AUTHORED | identity | all modules | User edits | — | Permanent on account deletion | Last write wins (single user) |
| `auth_sessions` | COMPUTED | identity | api | Create/expire | — | Permanent on logout/expiry | — |
| `user_checkpoints` | USER-AUTHORED | identity | retrieval, attention | Upsert on view | — | Permanent with account | Max timestamp wins |
| `connections` | SOURCE (grant) | connections | ingestion, privacy | Status/token rotation | — | Permanent on disconnect+purge or account deletion; token revoked first | Row lock per connection |
| `sync_cursors` | COMPUTED | connections/ingestion | ingestion | Advance after durable store (§11.1) | R4 | Permanent with connection | Lease + job lock |
| `source_items` | SOURCE | ingestion | communication, meetings, work (evidence), privacy | Metadata/stage only; content immutable per `content_hash` (new hash = content change event) | R4 | Logical tombstone on provider trash; permanent on provider deletion, purge, retention | Provider is authoritative |
| `messages` (content columns) | SOURCE | communication | all read paths | Body purge by retention | R4 + normalize | Body permanent purge on retention/provider deletion; row permanent on purge/account deletion | Provider authoritative |
| `messages.triage` | AI-DERIVED (projection of extraction) | communication (from apply event) | attention, retrieval | Replaced when a newer extraction for the message applies | R2 | With message | User "mark handled/low priority" overrides at conversation level |
| `message_participants` | SOURCE | communication | people, retrieval | Insert only | R4 | With message | — |
| `conversations` | COMPUTED + AI-DERIVED (`summary`) + USER-AUTHORED (`handled_by_user_at`, `priority_override`) | communication | attention, retrieval, chat | Reply state recomputed on each message; summary by AI-03 | Computed: from messages; summary: R3 | Permanent with source purge/account | User overrides win; reply state recomputed |
| `persons` | COMPUTED (identity, stats) + AI-DERIVED (`role_title` inferred) + USER-AUTHORED (`importance_user`, edited fields) | people | all | Stats recomputed; user fields only by user | Computed: nightly; AI fields: R2 | Permanent with account; logical `merged_into_id` on merge | User fields win; inferred fields replaced only by newer evidence |
| `person_identifiers` | COMPUTED + USER-AUTHORED (aliases) | people | resolution | Insert; user add/remove | — | Permanent with account | Unique key; merges move identifiers |
| `organizations` | COMPUTED + USER-AUTHORED | people | all | Domain-derived; user rename | — | Permanent with account | User wins |
| `entity_mentions` | COMPUTED (alias) + AI-DERIVED (extraction) | people | retrieval | Insert; re-scan on alias change | Computed + R2 | Permanent with source | — |
| `meetings` (calendar fields) | SOURCE | meetings | all | Upsert on etag change | R4 | Logical `cancelled`; permanent with source purge | Provider authoritative |
| `meetings.summary` (AI-DERIVED), `prep_brief` (COMPUTED sections + AI-DERIVED asks) | AI-DERIVED / COMPUTED | meetings / attention | chat, retrieval, UI | Summary from AI-10 apply; sections recomputed by version; asks from AI-11 on open | R2 (summary) / recompute sections / regenerate asks | With meeting | — |
| `meeting_participants` | SOURCE (calendar) + AI-DERIVED (speaker mapping) + USER-AUTHORED (confirmed mapping) | meetings | all | Calendar diff; mapping events | R4 / R2 | With meeting | User mapping wins |
| `recordings` | SOURCE | meetings | media pipeline | Status transitions | — | Raw media permanent purge after retention; row with account | — |
| `transcript_segments` | SOURCE (machine transcription of user media) | meetings | retrieval, work (evidence) | Replaced atomically per transcription version | Re-transcribe (AI-09) | Permanent with recording purge/account | — |
| `extractions` | AI-DERIVED (raw) | intelligence | work, meetings (apply) | Append-only; status transitions | R3 | Permanent with source purge/account | Unique key prevents duplicates |
| `work_items` | AI-DERIVED or USER-AUTHORED (`origin`) | work | all | **Only via `append_event`** (projection) | R1 (fold), R2 (re-apply) | Logical: `rejected`, `cancelled`, archived; `deleted_at` for user-deleted user items; permanent with source purge (if no remaining evidence) / account | Status fold + authority (`CONTEXT_ARCHITECTURE.md` §8) |
| `decisions` | AI-DERIVED or USER-AUTHORED | work | all | Via events | R1, R2 | As work items | Supersession/conflict events |
| `evidence`, `item_evidence` | AI-DERIVED (links) referencing SOURCE | work | all | Insert; quote redacted on source deletion | R2 (deterministic IDs) | Permanent with source deletion (quote) / account | — |
| `context_events` | USER-AUTHORED (`actor = user`) / AI-DERIVED (`model`) / COMPUTED (`system`, `time`) | work (and owning modules for their entity types via `work.append_event` or `platform.record_event`) | retrieval, attention, UI | Append-only | `user` events: never; others: R2 | Permanent with account; model events for purged sources removed | Fold resolves |
| `entity_links` | AI-DERIVED / COMPUTED / USER-AUTHORED | work | retrieval | Insert/confirm/reject | R2 | Permanent with endpoints | User wins |
| `projects`, `project_members` | USER-AUTHORED or AI-DERIVED (suggested) | projects | all | User actions; suggestions nightly | Suggestions recomputable | Logical archive; permanent with account | User wins |
| `chunks` | COMPUTED (+ embeddings from AI-04) | retrieval | retrieval | Upsert by `(source_item, chunk_index)` and model | Re-chunk/re-embed | Permanent with source deletion/purge/account | — |
| `retrieval_traces` | COMPUTED | retrieval | evaluation | Insert | — | Permanent after 90 days or with account | — |
| `reminders` | COMPUTED | attention | UI, notifications | State transitions (conditional updates) | Re-evaluate rules | Permanent with account; logical states otherwise | Fingerprint uniqueness |
| `notifications` | COMPUTED | attention | UI | State transitions | — | Permanent after 30 days / account | Unique per reminder+channel |
| `briefings` | COMPUTED (deterministic sections and headline) | attention | UI, chat | One per user-day | Re-render | Permanent after 30 days / account | — |
| `feedback_events` | USER-AUTHORED | work/attention/people (writer of the corrected entity) | evaluation, learning | Append-only | — | Permanent with account | — |
| `chat_sessions`, `chat_messages` | USER-AUTHORED (questions) + AI-DERIVED (answers with citation snapshots) | chat | chat, evaluation | Append-only | — | User can delete sessions (permanent); account | — |
| `ai_calls` | COMPUTED (telemetry, no content) | intelligence | ops, cost | Insert | — | Permanent after 90 days; user_id nulled on account deletion | — |
| `ai_cost_rollups` | COMPUTED (aggregates of `ai_calls`, no content) | intelligence (`cost_rollup` task) | ops, cost; API reads its own user's rows (budgets, slice 3.5) | Recomputed per 15-minute bucket | From `ai_calls` within its retention | Kept as aggregates; on account deletion the user's rows are re-keyed to `user_id` NULL (§7.6) | Recompute replaces the bucket |
| `audit_log` | COMPUTED | privacy (via `platform.audit`) | ops | Append-only | — | Retained 1 year without content | — |
| `outbox`, `event_consumptions`, `idempotency_keys` | COMPUTED (delivery mechanics) | platform | platform; `outbox` and `event_consumptions` are read only by the worker role (§7.6) | State transitions | — | Permanent after 7 days (outbox, consumptions) / 24 h (idempotency); a user's outbox rows are deleted by account deletion (§13.3) | Keys enforce uniqueness |

### 6.3 Verified invariants

| Invariant | How it is enforced | Test |
|---|---|---|
| **AI code cannot mutate source-of-truth data** | `intelligence` has no repository for SOURCE tables (import contract); `apply` writes only AI-DERIVED tables and calls `communication.set_triage_projection` for the `messages.triage` projection column; extract and apply both run as the worker role (§7.6), which every module's jobs share, so enforcement is by module contract plus a CI test that runs extract/apply with a SQL audit trigger in test mode rejecting writes to SOURCE tables from those code paths | RT-12 (§21) |
| **User-authored corrections survive recomputation** | User actions are `context_events` with `actor = user`, authority 5; R1/R2 never delete them; items with any user event keep their IDs during R2; fold applies authority before recency | RT-13, `CONTEXT_EVALUATION.md` CC-29–CC-31 |
| **AI-derived state can be reconstructed from source + events** | Projections = fold(events); model events = apply(stored extractions); extractions = AI(source) | RT-14 (rebuild equivalence) |
| **Every AI-derived item references existing source evidence** | FKs `evidence → source_items`, `item_evidence → evidence`; deletion redacts quotes and flags `has_source_gap` rather than leaving dangling rows | RT-11 |
| **Model output never sets `lifecycle_status`** | Fold ignores lifecycle changes from `actor ∈ {model, system}` except `cancelled` proposals, which become prompts | Unit tests on fold |

---

## 7. Transaction and event model

### 7.1 Two kinds of events

| | `context_events` | `outbox` |
|---|---|---|
| Purpose | Domain history: what changed in the user's context (timelines, "what changed", audit of corrections) | Delivery: trigger background work reliably |
| Lifetime | Permanent (until deletion) | 7 days after dispatch |
| Read by | Retrieval, UI, fold | Dispatcher |
| Written | In the same transaction as the state change | In the same transaction as the state change |

### 7.2 Pattern

```text
SOURCE CHANGE (sync page, upload, user action, stage completion)
  → one database transaction:
        source/computed/derived state + context_events (if any) + outbox row(s)   ── COMMIT (atomic)
  → dispatcher (worker loop, every 1 s) claims pending outbox rows (FOR UPDATE SKIP LOCKED)
  → for each subscribed handler: Procrastinate defer(job, queueing_lock = "<handler>:<event_id>")
  → marks the outbox row dispatched (same dispatcher transaction)
  → job runs handler → handler's own transaction records event_consumptions(event_id, handler) + effects
```

Procrastinate does **not** take part in the SQLAlchemy transaction. It writes jobs through its own psycopg connection. The outbox exists precisely so that the business commit and the intent to run work are atomic, and job submission happens afterwards with retries.

### 7.3 Outbox schema and states

```sql
CREATE TABLE outbox (
  id              uuid PRIMARY KEY,                 -- UUIDv7 (time-ordered), generated by platform.publish
  user_id         uuid NULL,                        -- NULL = system event; FK to users(id) added in slice 1.1 (§7.3.1)
  event_type      text NOT NULL,                    -- registered event type, e.g. 'MessageNormalized'
  aggregate_type  text NOT NULL, aggregate_id uuid NOT NULL,
  payload         jsonb NOT NULL,                   -- IDs and small facts only, never content
  correlation     jsonb NOT NULL DEFAULT '{}',      -- request_id / trace ids
  status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','dispatched','failed')),
  attempts        int NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_error      text NULL,                        -- error code and exception class only, never message content
  created_at      timestamptz NOT NULL DEFAULT now(),
  dispatched_at   timestamptz NULL);
CREATE INDEX ix_outbox_pending ON outbox (next_attempt_at) WHERE status = 'pending';
CREATE INDEX ix_outbox_user ON outbox (user_id) WHERE user_id IS NOT NULL;   -- account deletion; supports the slice 1.1 FK

CREATE TABLE event_consumptions (
  event_id    uuid NOT NULL REFERENCES outbox(id),  -- NO ACTION; purge deletes consumptions before their outbox row
  handler     text NOT NULL,                        -- stable registered handler name
  consumed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (event_id, handler));
```

Both tables are created in slice 0.3 by migration `0003` (§7.7). Their access model (roles, grants, RLS) is §7.6; how the API role inserts into `outbox` is §7.3.3.

#### 7.3.1 Deferred foreign key `outbox.user_id → users(id)`

| Question | Answer |
|---|---|
| When is `outbox` created? | Slice 0.3, migration `0003` (platform, §7.7), before any domain table exists |
| Why no FK in 0.3? | `users` does not exist until slice 1.1. `platform` is the base layer and must not create or know identity's tables; creating a stub `users` table in 0.3 would make platform own part of identity's schema |
| When is the FK added? | Slice 1.1, in the **same** migration that creates `users` (identity): `ALTER TABLE outbox ADD CONSTRAINT fk_outbox_user FOREIGN KEY (user_id) REFERENCES users(id)` (`ON DELETE NO ACTION`) |
| What if orphan rows exist then? | The `ADD CONSTRAINT` validates existing rows and the migration fails. Rows are never deleted silently; an operator removes test leftovers explicitly and re-runs. In practice no user-scoped producer exists before 1.1, and dispatched rows expire after 7 days |
| Integrity before the FK (0.3 → 1.1) | `user_id` is never caller-supplied: `platform.publish` copies it from the active unit of work. The API role can insert only rows whose `user_id` equals the transaction's `app.user_id` (§7.6 policy). No `users` table exists, so no user-scoped events are produced except synthetic test events |
| Integrity after the FK | Every non-NULL `outbox.user_id` references an existing user. Account deletion deletes the user's outbox rows (and their consumptions) before the user row (§13.3) |
| Why this is not an artificial dependency | The constraint is added by identity's migration, the higher layer, which already depends on `platform`. `platform` schema and code never reference `users`. Dependency direction stays identity → platform |

`event_consumptions.event_id → outbox(id)` is created with the table in 0.3 because both tables exist then. The same deferred pattern applies to `ai_calls.user_id` and `ai_cost_rollups.user_id` (slice 0.4; `fk_ai_calls_user` and `fk_ai_cost_rollups_user` in the 1.1 migration, §7.6).

#### 7.3.2 States and dispatch

| State | Meaning | Transition |
|---|---|---|
| `pending` | Committed with the business change; not yet turned into jobs | → `dispatched` when all handler jobs are deferred; stays `pending` with backoff on errors |
| `dispatched` | Jobs exist (or ran) for every subscribed handler | Terminal; purged after 7 days |
| `failed` | Dispatch failed 10 times (e.g., unknown event type, persistent queue error) | Alert; operator CLI re-queues (`eca ops outbox retry`: `failed` → `pending`, `attempts = 0`) |

There is no persisted "dispatching" state. A dispatcher claims rows by locking them (`FOR UPDATE SKIP LOCKED`) inside one transaction. A crash releases the locks and leaves the rows `pending`, so the next tick picks them up. "Stale dispatch" therefore means only: `pending` rows whose `next_attempt_at` has passed but which are not advancing (dispatcher stopped or failing). The reconciler (§7.5) and the "oldest pending > 2 min" alert cover that case.

**Dispatch step** (per claimed row): resolve the handlers registered for `event_type`; for each handler, Procrastinate `defer` with `queueing_lock = lock = "<handler>:<event_id>"` (an `AlreadyEnqueued` rejection counts as success); when every handler is deferred, set `dispatched`, `dispatched_at`. A registered event type with zero handlers is marked `dispatched` directly. An unregistered event type is an error, not a skip, because a rolling deploy can briefly run a dispatcher that lacks a new type. On error: `attempts` becomes n (the number of failed dispatch attempts so far), `last_error` is set, and `next_attempt_at = now() + min(1 s · 2^(n−1) + random(0–1 s), 5 min)` (§14.3); when n reaches 10 the row becomes `failed`. `outbox.attempts` counts dispatch failures only; handler job failures never change the outbox row (§14.3). If only some handlers were deferred before an error, the next attempt re-defers all of them; the already-deferred ones are absorbed by `queueing_lock` or `event_consumptions`. Events carry no ordering guarantee; handlers must not assume order.

#### 7.3.3 Publishing: the outbox insert without read access

The API role has INSERT on `outbox` and no SELECT (§7.6). PostgreSQL requires SELECT privilege, and a passing read policy, for any row a statement returns. So an `INSERT … RETURNING` by `eca_app` fails with `permission denied`. This includes the RETURNING that the SQLAlchemy ORM adds on its own to fetch server-generated defaults, and `ON CONFLICT` clauses. This was verified on PostgreSQL 16 on 2026-10-02: a plain INSERT succeeds; the same insert with RETURNING, or through `session.add`, is denied; a NULL or foreign `user_id` is rejected by `outbox_api_insert`; SELECT is denied.

`platform.publish` therefore follows these rules, for both roles (one code path):

| Rule | Detail |
|---|---|
| One SQLAlchemy Core statement | `insert(outbox).values(...)` with an explicit column list. Never the ORM unit of work (`session.add`), never `.returning()`, never `ON CONFLICT` |
| The application supplies identity and content | `id` (UUIDv7 generated before the insert, so `publish` returns it without reading the row back), `user_id` (from the unit of work, §7.3.1), `event_type`, `aggregate_type`, `aggregate_id`, `payload`, `correlation` |
| The database supplies lifecycle columns through the DDL defaults, without reading them back | `status = 'pending'`, `attempts = 0`, `next_attempt_at = now()`, `created_at = now()`, `last_error` and `dispatched_at` NULL. The dispatcher compares `next_attempt_at` with the database's `now()`, so the timestamps come from the same clock. Defaults are filled in by PostgreSQL inside the INSERT and need no RETURNING |
| Nothing read back | `publish` never selects the row it wrote. Tests that need to read the row use the worker role or the migration role |

The security model is unchanged: the API role stays INSERT-only, and its inserts are limited to its own `app.user_id`.

### 7.4 Duplicate dispatch, crash recovery, idempotency

| Failure | What happens | Why it is safe |
|---|---|---|
| Crash after business commit, before dispatch | Row stays `pending`; next dispatcher loop dispatches it | Nothing lost (RT-01) |
| Crash after `defer`, before marking `dispatched` | Row still `pending`; re-dispatched | If the first job is still queued, `queueing_lock` rejects the duplicate (treated as success). If it already ran, the handler finds `event_consumptions(event_id, handler)` and does nothing (RT-05) |
| Two dispatcher instances | `FOR UPDATE SKIP LOCKED` gives each row to one instance | — |
| Handler crash mid-transaction | Rollback; Procrastinate retries (or stalled-job recovery after worker death) | Handler writes and its consumption record commit together |
| Handler with an external call (AI, provider API) | The call happens before the handler's write transaction; a crash between call and commit repeats the call once | Bounded by the AI attempt cap (`AI_PIPELINE.md` §7); results keyed so only one row persists |
| Dispatcher down for a long time | Outbox grows; alert on oldest pending > 2 min | Reconciler (§7.5) also re-dispatches |

Handler idempotency rule: **every handler that writes state inserts `event_consumptions` in the same transaction as its writes; if the insert conflicts, it returns immediately.** Handlers that only enqueue further work rely on natural keys (e.g., `extractions` unique key) instead.

Precise duplicate semantics (slice 0.3):

- Procrastinate's `queueing_lock` rejects a second job only while the first is still queued (`todo`). A duplicate deferred while the first job is running is accepted; `lock` (same key) stops the two from running at the same time, and `event_consumptions` makes the later one a no-op.
- The handler wrapper runs `INSERT INTO event_consumptions … ON CONFLICT DO NOTHING RETURNING event_id` as its first statement. If a concurrent transaction holds the same key, the insert waits for it; after that transaction commits, the insert returns no row and the handler returns without effects.
- "Exactly once" means **effective exactly-once for database effects** written in the handler transaction. Effects outside the database (provider calls, Web Push) are at-least-once and need their own keys (§10).
- Handler names are stable identities (they appear in `event_consumptions.handler` and in lock keys). Renaming a handler creates a new identity.

### 7.5 Reconciler and source-item stage machine

The reconciler is introduced in two steps. Neither step pretends that a later table exists.

**Infrastructure reconciler (slice 0.3).** Periodic task `reconcile` (every 5 min, `schedule` queue, lock `reconcile`). It runs one dispatch pass, with the same dispatch step and `SKIP LOCKED`, over outbox rows that are `pending`, older than 1 minute and past `next_attempt_at`. This restores dispatch progress if the dispatcher loop has stopped inside a live worker. It also logs the oldest pending age and the `failed` count. It does **not** re-queue `failed` rows (operator decision, `eca ops outbox retry`), does not touch Procrastinate dead jobs (alert only), and does not read any domain table.

**Source-item reconciliation (slice 1.3, when `source_items` exists).** It extends `reconcile` with the stage-SLA scan below. Because `source_items` is a user-owned business table, the scan runs per user, one `SET LOCAL app.user_id` transaction each (§7.6); the worker role has no cross-user policy on business tables.

```text
fetched ─normalize─▶ normalized ─prefilter─▶ skipped (terminal, reason)
                                   └────────▶ extract_pending ─extract─▶ extracted ─apply─▶ applied
                                                   │                                  │
                                                   └─▶ needs_attention ◀──────────────┘  (attempt caps exhausted)
indexing runs from `normalized` in parallel and records chunks; it does not change `stage`
```

Columns on `source_items`: `stage`, `stage_attempts`, `stage_updated_at`, `next_attempt_at`, `last_error_code`.

From slice 1.3 the **reconciler** (every 5 min) also finds source items whose stage has not advanced past its SLA (`fetched` > 5 min, `extract_pending` > 15 min, `extracted` > 5 min) and whose `next_attempt_at` has passed, and re-enqueues the stage job with the same deterministic `queueing_lock`. Outbox re-dispatch of rows `pending` for > 1 minute exists from slice 0.3. All stage jobs are idempotent, so the reconciler can run any time.

### 7.6 Database roles and access model

Business data and delivery infrastructure are protected differently. Business tables are isolated **per user** by RLS. Infrastructure tables are isolated **per role** by grants plus role-targeted RLS policies. No authorization decision depends on `app.user_id` being unset, except fail-closed on business tables.

**Roles**

| Role | Used by | Attributes | Credentials |
|---|---|---|---|
| Migration role (`app_migrator` in hosted environments; `postgres` in local compose and CI) | `release` only | Owns all objects; BYPASSRLS (superuser locally) because data migrations span users | Release step only; never given to `api` or `worker` |
| API role `eca_app` (`API_DB_RUNTIME_ROLE`, `API_DATABASE_URL`) | `api` | LOGIN, NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, NOCREATEROLE | API deployment only |
| Worker role `eca_worker` (`API_DB_WORKER_ROLE`, `API_WORKER_DATABASE_URL`; added in slice 0.3) | `worker` (SQLAlchemy and Procrastinate connections) | Same attributes as `eca_app` | Worker deployment only; the API process never holds them |
| `app_readonly` | Future non-content analytics | NOBYPASSRLS; views only | Not created until a consumer exists |

**Role lifecycle (who creates the roles).** Migrations never create, alter or drop roles and never set passwords. The two runtime roles are infrastructure, provisioned before the migrations that grant to them run:

| Environment | Who creates `eca_app` and `eca_worker` | Credentials |
|---|---|---|
| Local development | `docker/postgres/init/01-roles.sql`, idempotent. Docker runs init scripts only on an empty data volume, so a volume created before slice 0.3 gets `eca_worker` by re-running the script (`docker compose exec db psql -U postgres -d eca -f /docker-entrypoint-initdb.d/01-roles.sql`) or by recreating the volume. A native PostgreSQL install runs the same script | Development-only passwords in the script and `.env.example` |
| Tests and CI | `tests/conftest.py` creates the cluster roles `eca_test_app` and `eca_test_worker` (LOGIN, NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, NOCREATEROLE) before migrating each throwaway database, and passes both names to Alembic (`-x runtime_role=…`, `-x worker_role=…`). `ECA_TEST_DATABASE_URL` must be a role that can create databases and roles (`postgres` in CI) | Test-only passwords |
| Hosted | Provisioned outside the application, by infrastructure-as-code or an operator runbook, before the first release that contains slice 0.3; the procedure is written with the hosting decision (Q1) | Platform secret manager. `API_DATABASE_URL` only on the API deployment, `API_WORKER_DATABASE_URL` only on the worker deployment |

The migration role needs no `CREATEROLE`. It owns every object it creates (`outbox`, `event_consumptions`, all Procrastinate objects) and grants on them as owner. `ALTER DEFAULT PRIVILEGES` applies only to objects created by the role that ran it, so **all migrations run as the same migration role**. Runtime roles own nothing.

From migration `0002` on, each migration that grants to a runtime role first checks, and **fails loudly** with an error naming the role and pointing to this section, when:
- either role does not exist;
- either role is SUPERUSER, BYPASSRLS, CREATEROLE or CREATEDB (the documented runtime attributes);
- either role is the migration role;
- the two names are equal;
- either role is a member (directly or indirectly) of the other, of the migration role, or of a role with any of those attributes, because membership would let it inherit more privileges.

Migration `0001` keeps its historical behaviour of skipping grants when the API role is missing. A database migrated that way fails at `0002` until both roles exist. `0002` then re-applies the API role's baseline grants idempotently, so the skipped grants are not lost. The checks live in `eca.platform.roles` (read-only catalog queries).

**Privileges and policies**

| Object | Owner | `eca_app` (API) | `eca_worker` | RLS | FORCE | Policies |
|---|---|---|---|---|---|---|
| User-owned business tables (every table with `user_id`, from slice 1.1) | Migration role | SELECT, INSERT, UPDATE, DELETE | SELECT, INSERT, UPDATE, DELETE | Yes | Yes | `<table>_user_isolation` for all roles: `USING` and `WITH CHECK` `user_id = eca_current_user_id()` (`eca.platform.rls.user_isolation_ddl`) |
| `outbox` | Migration role | **INSERT only** | SELECT, INSERT, UPDATE, DELETE | Yes | Yes | `outbox_api_insert` `FOR INSERT TO eca_app WITH CHECK (user_id = eca_current_user_id())`; `outbox_worker_all` `FOR ALL TO eca_worker USING (true) WITH CHECK (true)` |
| `event_consumptions` | Migration role | **None** | SELECT, INSERT, DELETE | Yes | Yes | `event_consumptions_worker_all` `FOR ALL TO eca_worker USING (true) WITH CHECK (true)` |
| Procrastinate tables (`procrastinate_jobs`, `procrastinate_periodic_defers`, `procrastinate_events`, `procrastinate_workers`) | Migration role | **None** | SELECT, INSERT, UPDATE, DELETE | No (no `user_id`; Procrastinate internals) | — | Grants only |
| Procrastinate sequences (serial and identity sequences of those tables) | Migration role | **None** | USAGE, SELECT | — | — | — |
| Procrastinate functions (`procrastinate_*`) | Migration role | **None** | EXECUTE | — | — | EXECUTE revoked from PUBLIC |
| `ai_calls` (slice 0.4) | Migration role | **INSERT only** | SELECT, INSERT, UPDATE, DELETE | Yes | Yes | `ai_calls_api_insert` `FOR INSERT TO eca_app WITH CHECK (user_id = eca_current_user_id())`; `ai_calls_worker_all` `FOR ALL TO eca_worker USING (true) WITH CHECK (true)` |
| `ai_cost_rollups` (slice 0.4) | Migration role | **SELECT only** | SELECT, INSERT, UPDATE, DELETE | Yes | Yes | `ai_cost_rollups_api_read` `FOR SELECT TO eca_app USING (user_id = eca_current_user_id())`; `ai_cost_rollups_worker_all` `FOR ALL TO eca_worker USING (true) WITH CHECK (true)` |
| `alembic_version` | Migration role | SELECT only (readiness) | SELECT only | No | — | — |
| `eca_current_user_id()` | Migration role | EXECUTE | EXECUTE | — | — | `REVOKE ALL … FROM PUBLIC` |
| Schema `public` | Migration role | USAGE (no CREATE) | USAGE (no CREATE) | — | — | — |

No runtime role holds TRUNCATE, REFERENCES or TRIGGER on any table, or UPDATE on any sequence. The API role has no privilege on any Procrastinate object, so it cannot put jobs on the queue.

**Exact privilege mechanics (slice 0.3).**

- **Default privileges are used, for both runtime roles.** Migration `0001` already set default privileges for the API role: SELECT, INSERT, UPDATE, DELETE on tables and USAGE, SELECT on sequences. Migration `0002` adds the same two default privileges for the worker role. Business tables from slice 1.1 then receive DML for both roles automatically, which matches the business-table row above. Default privileges on functions are not changed.
- **Each migration that creates an infrastructure object trims the defaults explicitly, in the same transaction that creates the object.** The trims:
  - `0003`: `REVOKE SELECT, UPDATE, DELETE ON outbox FROM <api>` (INSERT stays); `REVOKE ALL ON event_consumptions FROM <api>`; `REVOKE UPDATE ON event_consumptions FROM <worker>`. Consumption rows are only inserted (when consumed) and deleted (by purge), never updated, so UPDATE is not justified.
  - `0004`: `REVOKE ALL` on every Procrastinate table and sequence `FROM <api>`; `REVOKE EXECUTE` on every `procrastinate_*` function `FROM PUBLIC`; `GRANT EXECUTE` on them `TO <worker>`. PostgreSQL grants EXECUTE on new functions to PUBLIC by default; that default is revoked per function, never schema-wide, because the extension functions of `vector`, `pg_trgm` and `citext` must stay callable.
- **The `alembic_version` drift is corrected.** In `0001`, `GRANT … ON ALL TABLES` ran after Alembic had already created `alembic_version`, so the API role also holds INSERT, UPDATE and DELETE on it. This was verified on PostgreSQL 16 on 2026-10-02, contrary to the comment in `0001`. Migration `0002` revokes those three privileges from the API role and grants SELECT to the worker role. Its downgrade does not restore them.
- `<api>` and `<worker>` are the configured names (`API_DB_RUNTIME_ROLE`, `API_DB_WORKER_ROLE`; the test roles in CI). Policies name them the same way.

**What the privilege-matrix test asserts** (PostgreSQL, on a freshly migrated database with no test tables, after `alembic upgrade head` and again after down/up):

1. For the API role, the worker role and PUBLIC, on every table in schema `public`: each of SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES and TRIGGER (`has_table_privilege`) equals the table above, and PUBLIC holds none of them. For every sequence: USAGE, SELECT, UPDATE. For every `procrastinate_*` function and `eca_current_user_id()`: EXECUTE.
2. `pg_default_acl` for the migration role contains exactly the table and sequence defaults for the two runtime roles.
3. `relrowsecurity` and `relforcerowsecurity` are true on `outbox` and `event_consumptions`. `pg_policies` on them equals exactly `outbox_api_insert` (INSERT, API role, `WITH CHECK (user_id = eca_current_user_id())`), `outbox_worker_all` (ALL, worker role, `true`/`true`) and `event_consumptions_worker_all` (ALL, worker role, `true`/`true`).
4. Both runtime roles are NOSUPERUSER, NOBYPASSRLS, NOCREATEDB and NOCREATEROLE, distinct, and not members of each other.
5. Every table the migrations create in schema `public` is classified as either an infrastructure table listed above or a business table with the `_user_isolation` policy. A new table that is neither fails the test.

Behavioural checks (what each role can actually do) are in RT-15 (§21).

**AI telemetry tables (slice 0.4).** `ai_calls` and `ai_cost_rollups` hold IDs and numbers, never content (§6.2), so they are classified with the delivery infrastructure: isolated per role by grants and role-targeted policies, not business tables under `_user_isolation`.
- **Writers.** The meter runs in the process that made the call: the API role for interactive calls (it may insert only rows of its own `app.user_id`, without RETURNING, §7.3.3), the worker role for background calls (in the job's user context, or with `user_id` NULL for system calls).
- **Cross-user reads.** The 15-minute roll-up runs as the worker role with `app.user_id` unset and reads all rows through `ai_calls_worker_all`, as the dispatcher reads `outbox`. The rule "no cross-user policy on business tables" is unchanged, because these are not business tables.
- **API reads.** The API reads only its own user's roll-ups (per-user budget checks in slice 3.5) and never raw `ai_calls`.
- **Deferred FKs.** `ai_calls.user_id` and `ai_cost_rollups.user_id` get their FK to `users` in the slice 1.1 migration that creates `users`, like `outbox` (§7.3.1): `fk_ai_calls_user`, `fk_ai_cost_rollups_user`, `ON DELETE NO ACTION`. Until then `user_id` comes only from the unit of work.
- **Account deletion (slice 1.9).** `ai_calls.user_id` is set to NULL (rows stay content-free until the 90-day purge). The user's roll-up rows are added into the matching `user_id` NULL rows and then deleted, so global cost history is kept without the user.

**How `SET LOCAL app.user_id` behaves.** The unit of work runs `set_config('app.user_id', <uuid>, true)` (transaction-local) as its first statement. The value comes only from the authenticated session (API) or from the event or job being processed (worker), never from request input. It disappears at commit or rollback, so a pooled connection never carries it to the next transaction (RT-15).

**API requests.** Each request is one `eca_app` transaction with `app.user_id` = the session's user. Business tables return only that user's rows. On `outbox` the API can only insert, and only rows for its own `app.user_id` (a NULL or foreign `user_id` fails the `WITH CHECK`). It cannot read, update or delete any outbox row, and it has no privilege on `event_consumptions` or Procrastinate objects. No value of `app.user_id` changes this, because these limits are grants and role-targeted policies, not user predicates. The API never enqueues jobs directly: all background work starts from an outbox row.

**Worker transactions.**

| Component | Transaction | `app.user_id` | Sees |
|---|---|---|---|
| Dispatcher, infrastructure reconciler, `eca ops outbox retry` | One `eca_worker` transaction per batch | Unset | All outbox rows, through the explicit `TO eca_worker` policy. Business tables: nothing (fail closed) |
| Event handler job | One `eca_worker` transaction per job | Set to the event's `user_id`; unset for system events (`user_id` NULL) | That user's business rows only; its own `event_consumptions` insert |
| Per-user maintenance scans (later slices: source-item reconciliation, sweeps, purges) | One transaction per user | Set per user | One user at a time |

**Processing many users safely.** Cross-user work happens only on infrastructure tables, which hold IDs and no content. Anything that touches business data runs per user, with `app.user_id` set for that transaction. Job arguments that set `app.user_id` come only from outbox rows. The API cannot write Procrastinate tables, and every outbox row's `user_id` was pinned by the insert policy (API) or by `platform.publish` (worker). A request cannot forge a job for another user. Enumerating users for per-user scans is the one cross-user read the worker needs outside infrastructure; slice 1.1 defines it as an explicit `TO eca_worker` read policy on the identity columns it needs. No other cross-user policy on business tables is allowed.

**Grants and RLS together.** Grants decide which operations a role may attempt on a table. RLS decides which rows. Infrastructure tables use both: if a future migration accidentally granted `eca_app` SELECT on `outbox`, FORCE RLS without a matching policy would still return zero rows.

**Residual risk.** RLS keyed on a session setting protects against missing `WHERE user_id` filters and pooled-connection leaks. It cannot stop code that runs arbitrary SQL as `eca_app`, because such code could call `set_config` itself. Mitigations: parameterized SQLAlchemy only, no SQL built from input, the planner cannot write SQL (`TECHNICAL_DESIGN.md` §17.3), and explicit `user_id` predicates in every repository query. Infrastructure isolation does not depend on this, because it uses role grants.

### 7.7 Slice 0.3 and 0.4 migrations

One concern per revision (§17.4). Each revision creates its objects **together with** their grants, revocations and RLS, in one transaction (`transaction_per_migration`). So no committed intermediate state ever exposes an infrastructure table to the API role. The chain is linear:

| Revision | Depends on | Owns | Does not |
|---|---|---|---|
| `0002_runtime_role_access` | `0001` | The role checks of §7.6 (both runtime roles exist, are safe, distinct and not members of each other). For the worker role: USAGE on schema `public`, EXECUTE on `eca_current_user_id()`, SELECT on `alembic_version`, and default privileges (SELECT, INSERT, UPDATE, DELETE on tables; USAGE, SELECT on sequences). Removes the API role's INSERT, UPDATE and DELETE on `alembic_version` (§7.6) | Create tables, roles or passwords |
| `0003_outbox` | `0002` (the worker defaults must exist before the tables, and both roles must exist for the policies) | `outbox`, `event_consumptions`, `ix_outbox_pending`, `ix_outbox_user`, FK `event_consumptions.event_id → outbox(id)`; the trims of §7.6; ENABLE and FORCE RLS; the three policies | Add any FK to `users` (§7.3.1) |
| `0005_ai_calls` (slice 0.4) | `0004` | `ai_calls` and `ai_cost_rollups` (§5.5), their indexes, grant trims, ENABLE and FORCE RLS and the four policies of the AI telemetry rows above | Add an FK to `users` (deferred to slice 1.1) |
| `0004_procrastinate_schema` | `0003` (only for the linear chain; Procrastinate objects do not reference platform tables) | The Procrastinate schema of the pinned version (§15), executed from a vendored copy `backend/migrations/sql/procrastinate_<version>_schema.sql`; the Procrastinate trims and the EXECUTE grants of §7.6 | Call `procrastinate schema --apply`, or read `schema.sql` from the installed package at migration time (the revision must not change when the package changes) |

Downgrades reverse each revision: `0004` drops the Procrastinate tables, functions and types; `0003` drops both tables; `0002` removes the worker grants and defaults (the `alembic_version` correction is not reverted). The integration test runs `0001` → head → base → head and checks the privilege matrix after each upgrade.

**Procrastinate schema checks and upgrades.** A test applies the installed package's `schema.sql` (`procrastinate.schema.SchemaManager.get_schema()`) to a separate empty database. It then compares the Procrastinate catalog with the migrated database: tables and columns, types, indexes, functions with arguments and source, and triggers. A version bump without a matching migration therefore fails CI. Upgrading Procrastinate means pinning the new version and adding a new revision. That revision applies Procrastinate's own migration files between the two versions, vendored, followed by the same trims and grants. The `0004` file never changes.

---

## 8. Extraction and application

### 8.1 Extraction result lifecycle

```sql
CREATE TABLE extractions (
  id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id),
  source_item_id uuid NOT NULL REFERENCES source_items(id),
  content_hash bytea NOT NULL,                  -- of the source content that was sent
  pipeline text NOT NULL,                       -- email_extract | meeting_extract | adjudicate
  prompt_version text NOT NULL, schema_version text NOT NULL, model text NULL,
  input_hash bytea NOT NULL,                    -- prompt inputs incl. candidate list
  candidate_map jsonb NOT NULL DEFAULT '{}',    -- {"C1": {"entity_type": "work_item", "entity_id": "…", "version": 4}, …}
  parent_extraction_id uuid NULL REFERENCES extractions(id),   -- adjudication → original
  status text NOT NULL CHECK (status IN ('running','succeeded','failed_retryable','failed_permanent','superseded')),
  attempts smallint NOT NULL DEFAULT 0,
  output jsonb NULL, error_code text NULL,
  apply_status text NOT NULL DEFAULT 'not_ready' CHECK (apply_status IN ('not_ready','pending','applied','apply_failed','skipped')),
  applied_at timestamptz NULL,
  created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
  UNIQUE (source_item_id, content_hash, pipeline, prompt_version, parent_extraction_id));
```

| Status | Meaning | Next |
|---|---|---|
| `running` | Row claimed by the extract job (row created before the AI call so concurrent runs see it) | `succeeded` / `failed_*` |
| `succeeded` | Validated output stored; `apply_status = pending`; outbox `ExtractionCompleted` | apply |
| `failed_retryable` | Transient error; `attempts` < cap | Retry with backoff |
| `failed_permanent` | Cap reached, safety block or non-retryable error | Source item `needs_attention`; operator replay |
| `superseded` | A newer content hash or prompt version for the same source has been applied | Kept for audit; not re-applied |

The extract job: (1) `INSERT … ON CONFLICT DO NOTHING` the `running` row, or load the existing row; if `succeeded`, ensure the `ExtractionCompleted` outbox event exists and stop — **no AI call**; (2) build the prompt; (3) call the model **outside any database transaction**; (4) in one transaction store output/status, `ai_calls`, the source stage, and the outbox event.

### 8.2 Apply lifecycle

The apply job for `extraction_id`, in **one transaction**:

1. `pg_advisory_xact_lock(user merge key)` (§12.2).
2. `SELECT … FOR UPDATE` the extraction; if `apply_status = applied` → commit (no-op).
3. Deterministic validation and grounding (`AI_PIPELINE.md` §5.4), date resolution, candidate linking, dedupe/merge.
4. Write evidence (deterministic IDs), item/decision changes via `work.append_event`, `entity_mentions`, `messages.triage` projection, outbox events (`WorkItemChanged`, …).
5. Ambiguous high-priority candidates: the item is created provisionally (`suggested`, confidence band `low`, `pending_adjudication = true`) and an `AdjudicationNeeded` outbox event is written. **Apply never calls a model.**
6. Set `apply_status = applied`, `applied_at`, source item `stage = applied`.

| apply_status | Meaning |
|---|---|
| `not_ready` | Extraction not succeeded |
| `pending` | Ready to apply |
| `applied` | Applied exactly once |
| `apply_failed` | Deterministic error after 8 attempts (bug or data issue); source `needs_attention`; fix code and re-run apply — **no AI call** |
| `skipped` | Superseded before apply |

A retry of apply re-reads the stored output; it never calls the model (RT-02).

### 8.3 Adjudication

`AdjudicationNeeded` → `adjudicate` job (AI-02) → `extractions` row with `pipeline = adjudicate`, `parent_extraction_id` → its own apply job → `append_event` on the provisional item (authority 2) → `pending_adjudication = false`. If the user acted on the item in between, the user's authority-5 values stand.

### 8.4 Content changes and new prompt versions

- New `content_hash` for a source (editable content in future connectors): new extraction key; when it applies, evidence from the old content for that source is marked `superseded` (`item_evidence.relation` history kept) and items whose only support was the old content are re-evaluated (suggested items without remaining support are archived; confirmed items get `has_source_gap`).
- New prompt version: only by explicit, costed re-extraction job (`AI_PIPELINE.md` §10). Apply of the newer extraction supersedes the older one for the same source in the same way.

### 8.5 Recomputation levels

| Level | What | AI calls | Procedure |
|---|---|---|---|
| R1 Re-fold | Recompute item/decision projections from `context_events` | 0 | Per item under row lock; idempotent |
| R2 Re-apply | Rebuild AI-derived projections from stored extractions | 0 | Per user under the merge lock: keep SOURCE, COMPUTED inputs, USER-AUTHORED events and every item/decision that has a user event or `origin = user`; delete model/system events, evidence links and AI-only items/decisions; re-apply all `succeeded` extractions in `occurred_at` order (evidence IDs are deterministic, so they are recreated with the same IDs); R1 for all items |
| R3 Re-extract | New AI output for a window | AI-01/AI-10 | Costed, approved job; then R2 semantics via normal apply |
| R4 Re-sync | Re-fetch from provider | 0 (provider quota) | Bounded window; idempotent upserts |

Limitation: after R2, AI-only items get new IDs; old chat answers keep their citation **snapshots** (`chat_messages.citations` store text and source references), but links to removed item IDs show "item no longer exists".

---

## 9. Event-flow audit

Notation: **txn** = same transaction as the triggering change; **handler** = outbox event handler; **AI** = an AI call from `AI_PIPELINE.md`.

### 9.1 New email (inbound or outbound)

| State | Change | Mode | AI |
|---|---|---|---|
| `source_items` | Insert (stage `fetched`) | txn (sync page) | — |
| Message, participants | Normalize, clean body, prefilter | handler `normalize` | — |
| Conversation | `last_inbound/outbound_at`, `awaiting`, `needs_reply` (from triage when available) | txn of normalize; triage refines after apply | — |
| Person / organization | Resolve or create from headers; interaction stats; `entity_mentions` by alias | txn of normalize | — |
| Task / commitment / deadline / decision / open question | New items or status signals on candidates | `extract` → `apply` | AI-01 (relevant mail only), AI-02 rarely |
| Project | `project_hint` on items/threads; project activity | apply + handler `projects.on_work_item_changed` | — |
| Reminder | Create/cancel/reschedule for affected items; follow-up reminders when a reply arrives or not | handler `attention.evaluate_reminders` | — |
| Priority | Conversation and item scores | handler `attention.recompute_priority` | — |
| Meeting preparation | Prep sections of upcoming meetings with these participants recomputed (version bump) | handler | AI-11 only if the user opens the prep view |
| Daily briefing | Not regenerated; if a materiality-3 change arrives after generation, Today shows "updated since briefing" | handler | — |
| Retrieval | Chunks + embeddings | handler `index` | AI-04 |
| Thread summary | Debounced update | handler | AI-03 |

### 9.2 Email update (provider metadata)

| Update | Effect | AI |
|---|---|---|
| Read/unread, star, importance markers, user labels | Stored in `source_items.raw_metadata`; no derived change in MVP | — |
| Archive (removed from inbox) | Stored; does **not** mark the thread handled (user marks handled explicitly) | — |
| Provider category change (e.g., moved to promotions) | Stored; affects prefilter only for future messages | — |
| Moved to Trash / Spam | `SourceItemTrashed`: message excluded from retrieval and needs-response; reminders for its thread re-evaluated; items keep evidence (restorable) | — |
| Restored from Trash | `SourceItemRestored`: reverse of the above | — |
| Content change (not possible in Gmail; possible for future editable sources) | `SourceItemContentChanged`: re-normalize; new extraction key (§8.4) | AI-01 once for the new content |

### 9.3 Email deletion (permanent at provider, or user purge)

1. `source_items.deleted_at` set; message body and clean body **permanently deleted**; chunks for the message **permanently deleted**.
2. Evidence rows from the message keep their IDs but `quote` is replaced with `[source deleted]`; `item_evidence` retained for history.
3. For each item supported by the message: if other evidence remains → no change except history; if not → suggested items archived (`context_events: source_removed`), confirmed/user items flagged `has_source_gap`.
4. Conversation state recomputed from remaining messages; if no messages remain, the conversation is deleted.
5. Person interaction stats recomputed (nightly is sufficient); priority and reminders re-evaluated via `WorkItemChanged`/`ConversationStateChanged`; prep sections recomputed.
6. No AI call.

### 9.4 Connection revoked or disconnected

Sync paused; `ConnectionStatusChanged`. With "keep data": sources stay but are excluded from retrieval scopes that require an active grant (coverage block reports the gap). With "purge": §13.3 source purge job.

### 9.5 New or changed calendar meeting

| State | Change | AI |
|---|---|---|
| `meetings` | Upsert by provider event ID when etag changed | — |
| Participants | Attendee diff; response status | — |
| People | New persons for unknown attendees | — |
| Project | Title/description matched to project names and hints (trigram) | — |
| Meeting context | `series_key` link to previous meetings; prep scheduling (T−45 min if context exists) | — |
| Reminders | Meeting-prep reminder created/rescheduled/cancelled | — |
| Priority | Meeting proximity feature for attendees' items | — |
| Cancellation | `status = cancelled`; prep/reminders cancelled | — |
| Retrieval | Event chunk re-embedded when description changes | AI-04 |

### 9.6 New meeting content (recording or transcript uploaded and processed)

| State | Change | AI |
|---|---|---|
| Recording, transcript | Stored; sha256 dedupe; a transcript version is never replaced automatically (re-transcription changes speaker labels; an operator-approved re-transcription keeps the old version for existing evidence and requires speaker re-confirmation) | AI-09 (media only) |
| Participants | Speaker mapping (deterministic first, AI-10 proposals, user confirmation); `attended` | AI-10 proposals |
| People | Persons for mapped speakers; `entity_mentions` from transcript | — |
| Decisions, open questions | New; supersession/resolution of earlier ones | AI-10 → apply |
| Tasks, commitments, deadlines | New items; status signals on prior items | AI-10 → apply |
| Project | Hints and mentions | apply |
| Meeting context | Summary; "what changed since previous meeting" computed from events | — (diff is SQL) |
| Reminders, priority, prep sections of the next meeting in series | Re-evaluated / recomputed | — |

### 9.7 New user-created task

`origin = user`, `verification_status = user_created`, authority 5 events. Dedupe check proposes "similar suggested item exists — merge?" but never auto-merges. Later AI evidence can attach to it through the candidate list (status signals → `reported_status` only). Priority and reminders computed as for any item.

### 9.8 User edit of an AI-derived item

- Editing confirms the item (`verification_status = confirmed`) — an explicit user action.
- Edited fields become user-authored (authority 5). Later model evidence touching those fields creates `conflict_detected` + a "possible update" prompt; it never overwrites.
- Fields the user did not edit continue to follow the fold (e.g., `reported_status`).

### 9.9 User corrections and the AI-derived values they override

| Correction | Overrides | Future effect |
|---|---|---|
| Reject item | `verification_status = rejected` (logical) | Dedupe against rejected items; re-suggested only on new explicit owner evidence; up to 3 rejections used as negative examples (AI-01) |
| Confirm item | `verification_status = confirmed` | Authority on confirmed fields = 5 |
| Edit due date / owner / title / type | Those fields | Conflicts surfaced, never overwritten |
| Add or edit notes | `notes` (user-authored) | Never sent to models as instructions; never edited by AI |
| Complete / reopen / cancel | `lifecycle_status` | Model `completed_claim` events after "done" have no effect |
| Merge items | `merged_into_id` | Future candidates match the surviving item |
| Mark person/org/project important; change relationship type or role | `importance_user`, profile fields | Priority features; inferred values ignored |
| Merge persons / add alias | Identifiers | Resolution and mentions re-scan (deterministic) |
| Confirm/correct speaker mapping | `meeting_participants` mapping | Items owned by that speaker re-pointed via events |
| Mark conversation handled / low priority | `handled_by_user_at`, `priority_override` | Needs-response and reminders for that thread suppressed until a new inbound message |
| Snooze / dismiss reminder | Reminder state | Timing preference; dismissal suppression |
| Confirm / reject project; assign item to project | Project status, `project_id` | Hint matching prefers confirmed projects |
| Reject decision / reopen open question | Decision verification / resolution link | Same as items |

Every correction is stored as a `context_events` row (`actor = user`) and a `feedback_events` row (for evaluation and learning).

### 9.10 Time passing

The hourly sweeper writes `became_overdue`, `due_soon`, `became_stale` events (`actor = time`, deterministic dedupe key per entity and time bucket), which drive reminders, priority and "what changed".

---

## 10. Idempotency

### 10.1 Every side effect

| Side effect | Duplicate cause | Key / mechanism |
|---|---|---|
| Source record | Re-sync, overlapping pages, cursor-expiry re-sync, webhook storms | `UNIQUE(connection_id, kind, external_id)`; upsert writes an event only if `content_hash` or tracked metadata changed |
| Same email under different provider IDs | Copies, re-imports, future second account | `messages UNIQUE(user_id, rfc822_message_id)`; extra source linked as alias, stops at `normalized` |
| Normalization outputs (message, participants, persons) | Normalize retried | Message keyed by `source_item_id` (unique); participants PK `(message_id, person_id, role)`; persons by `UNIQUE(user_id, kind, value_normalized)` identifiers |
| Extraction result | Job retried, duplicate job | `UNIQUE(source_item_id, content_hash, pipeline, prompt_version, parent_extraction_id)`; `running` row claimed first |
| Tasks / commitments / decisions | Apply retried; same fact in several sources; parallel processing | `apply_status` guard; per-user merge lock + candidate matching (open, recently closed, rejected) before insert |
| Evidence | Apply retried / R2 | `id = uuid5(extraction_id, candidate_index)`; `UNIQUE(extraction_id, candidate_index)` |
| Item–evidence links | Apply retried | PK `(item_type, item_id, evidence_id)` |
| Item events (`context_events`) | Apply retried, handler replay, sweeper re-run, user double-submit | `UNIQUE(user_id, dedupe_key)`: model `sha256(extraction_id, candidate_index, event_type)`; time `sha256(entity, event_type, hour_bucket)`; user `sha256(idempotency_key or request_id, event_type)` |
| Reminders | Sweep overlaps, event handlers | `UNIQUE(user_id, fingerprint)` (`CONTEXT_ARCHITECTURE.md`/`TECHNICAL_DESIGN.md` reminder fingerprint) |
| Notifications (in-app, Web Push) | Sweep and handler both deliver | `UNIQUE(reminder_id, channel)`; conditional update `pending → sending → sent`; Web Push `Topic` header = reminder ID so the push service collapses undelivered duplicates. A crash after the push service accepted but before `sent` is recorded can cause one duplicate push (at-least-once at the network edge) |
| Outbox events | — (written once in the business transaction) | Primary key; dispatch duplicates handled by `queueing_lock` + `event_consumptions` (§7.4) |
| Event handler effects | At-least-once dispatch | `event_consumptions PRIMARY KEY(event_id, handler)` in the handler transaction |
| Synchronization runs | Periodic + manual + webhook overlap | Procrastinate `lock` and `queueing_lock` `sync:{connection}:{resource}`; cursor lease |
| Cursor advance | Crash mid-run | Cursor written only after all items of the run are committed (§11.1) |
| Meeting processing (transcription) | Retry, same media uploaded twice | `recordings UNIQUE(user_id, sha256)`; transcript keyed `(recording_id, transcription_model)` and replaced atomically |
| Meeting extraction | Retry | Extraction key with transcript `content_hash` |
| Gemini Files API upload | Retry | `recordings.provider_file_ref` reused until expiry; deleted after transcription |
| Uploaded media (init/complete) | Client retry | `Idempotency-Key` on init; sha256 dedupe; `upload-complete` is a state transition `pending_upload → uploaded` (repeat = no-op) |
| Chunks / embeddings | Index retried | `UNIQUE(source_item_id, chunk_index)`; embedding upsert by `(chunk_id, model)` |
| Chat messages | Client retry | `Idempotency-Key` → existing message/stream result |
| User actions (confirm, reject, edit) | Double click, retry | `Idempotency-Key` (optional) + event dedupe key + state check (confirming a confirmed item is a no-op) |
| Exports, deletion jobs | Repeat requests | `UNIQUE(user_id) WHERE status IN ('pending','running')` on job tables |
| Webhook notifications (post-MVP) | Provider redelivery | `webhook_inbox UNIQUE(provider, provider_message_id)`; only triggers sync |

### 10.2 Externally triggered operations

| Operation | Trigger | Idempotency key |
|---|---|---|
| Sign-in callback | Google redirect | `state` (single-use, stored server-side with PKCE verifier) |
| Connect callback | Google redirect | `state` (single-use); connection unique per `(user_id, provider, account_email)` |
| Gmail message ingest | Sync | `(connection_id, 'message', gmail_message_id)` + `rfc822_message_id` |
| Calendar event ingest | Sync | `(connection_id, 'calendar_event', event_id)` + `etag` |
| Recording upload init | User | `Idempotency-Key` header; `(user_id, sha256)` |
| Recording upload complete | User | `recording_id` state transition |
| Chat message | User | `Idempotency-Key` header |
| Reply guidance | User | `Idempotency-Key` header |
| Item/decision/person mutations | User | `If-Match`/`base_version` + optional `Idempotency-Key` |
| Account deletion | User | Deletion job unique per user |
| Webhook (post-MVP) | Provider | `(provider, provider_message_id)` |
| Operator replay CLI | Operator | Same deterministic job keys as automatic runs |

---

## 11. Synchronization

### 11.1 Checkpoints

```sql
CREATE TABLE sync_cursors (
  connection_id uuid NOT NULL REFERENCES connections(id), resource text NOT NULL,   -- 'mail' | 'calendar:<id>'
  cursor text NULL, cursor_obtained_at timestamptz NULL,       -- historyId / nextSyncToken (opaque)
  query_fingerprint text NULL,                                 -- calendar list params bound to the token
  import_state text NOT NULL DEFAULT 'none' CHECK (import_state IN ('none','running','done')),
  import_page_token text NULL, import_until timestamptz NULL, import_processed int NOT NULL DEFAULT 0,
  last_attempt_at timestamptz NULL, last_success_at timestamptz NULL, consecutive_failures int NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','needs_reauth','error')),
  lease_owner text NULL, lease_expires_at timestamptz NULL,
  PRIMARY KEY (connection_id, resource));
```

Rules:

1. **Cursor advances only after every change of the run is committed** as `source_items` (+ outbox). A crash restarts from the old cursor; L1 keys make the repeat cost provider quota only.
2. One run per `(connection, resource)`: Procrastinate `lock` + row lease.
3. The initial import is resumable: `import_page_token` is committed after each page.
4. Provider identifiers are stored only in SOURCE tables (`source_items.external_id`, `external_thread_id`, `raw_metadata`); core modules use internal UUIDs.

### 11.2 Gmail

| Item | Detail |
|---|---|
| Provider IDs | Message `id`, `threadId`, mailbox `historyId`; RFC 822 `Message-ID` header for cross-copy dedupe |
| First connect | Record the profile `historyId` as the cursor **before** the import, so mail arriving during import is caught incrementally |
| Initial import | `messages.list(q="newer_than:30d")` newest first; last 72 h first (interactive); older at ≤ 220 `messages.get`/min (§11.6); page token checkpointed |
| Incremental | `history.list(startHistoryId, historyTypes=[messageAdded, messageDeleted, labelAdded, labelRemoved])`, all pages; `messages.get(format=full)` only for IDs not already stored |
| Versioning | Content immutable; `content_hash` over raw MIME; label changes are metadata (§9.2) |
| Cursor expired (HTTP 404) | Re-sync via `messages.list` from `last_success_at − 1 day` (capped at 30 days; older gaps reported in coverage), idempotent upserts, then a new cursor. A fixed 7-day window would miss mail when the last successful sync was longer ago (e.g., after a long `needs_reauth` period) |
| Deletion | `messageDeleted` → §9.3 |
| Cadence | Every 2 min while the user is active or in work hours, 10 min otherwise, 30 min after 7 inactive days; ±25% jitter |

### 11.3 Calendar

| Item | Detail |
|---|---|
| Provider IDs | Event `id`, `iCalUID`, `recurringEventId`; `etag` and `updated` for versioning |
| Full | `events.list(singleEvents=true, timeMin=now−30d, timeMax=now+60d)`; store `nextSyncToken` and `query_fingerprint` |
| Incremental | `events.list(syncToken)` with identical parameters (else HTTP 400); includes cancelled events |
| Versioning | Unchanged `etag` → skip |
| 410 Gone | Clear cursor; full window sync; idempotent upserts |
| Window roll-forward | Daily full sync with the new window replaces the cursor (sync tokens are bound to the original parameters) |
| Cadence | Every 15 min + on demand when Today or a meeting is opened |

### 11.4 Uploads

`POST /recordings` → pre-signed URL → client upload → `POST /recordings/{id}/complete` → object `HEAD` (size, checksum) → `RecordingUploaded` → media pipeline. Uploads not completed within 24 h are removed.

### 11.5 Webhooks (post-MVP; designed now)

Webhooks are **synchronization signals**, never authoritative data:

1. Verify the request first (Pub/Sub push OIDC token for Gmail watch; per-channel token for Calendar; `clientState` + validation handshake for Microsoft Graph; HMAC signing secret with 5-minute timestamp window for Slack).
2. Insert into `webhook_inbox` (`UNIQUE(provider, provider_message_id)`), acknowledge 2xx within 1 s.
3. Enqueue the normal cursor-based sync with `queueing_lock = sync:{connection}:{resource}` (bursts coalesce into one run).
4. Polling continues as a fallback (Pub/Sub can delay or drop; Gmail notifies at most once per second per user; Gmail watches expire after 7 days and are renewed daily).

### 11.6 Rate limits, backoff, retries

| Provider | Official limit | Budget |
|---|---|---|
| Gmail | 6,000 quota units per user per minute; 1,200,000 per project per minute; `messages.get` 20 units, `messages.list` 5, `history.list` 2, `users.watch` 100; 80 M units/day per project before billing for projects created after 2026-05-01 | In-job token bucket at 4,500 units/min per connection (only one sync job per connection runs, so a local limiter is sufficient) → initial import ≤ ~220 `messages.get`/min; batch HTTP requests of ≤ 50 calls. Steady state ≈ 5,000 units/user/day |
| Calendar | 600 requests per user per minute; 10,000 per project per minute; `403/429 usageLimits` | In-job limit 300/min |
| Gemini | Per-model RPM/TPM per account tier | Queue concurrency caps (`extract` 8, `ai_standard` 4); 429 → backoff, then fallback model |

Backoff: `min(base · 2^(n−1) + random(0–1 s), max)` after the n-th failed attempt (§14.3); `Retry-After` honoured; ±25% jitter on periodic schedules (Google guidance). Retry limits per task class in §14.3.

### 11.7 Crash recovery summary

| Crash point | Recovery |
|---|---|
| During a sync page | Page transaction rolled back; next run repeats from old cursor |
| After pages committed, before cursor update | Next run repeats; upserts are no-ops |
| During the initial import | Resumes from `import_page_token` |
| After source commit, before dispatch | Outbox `pending` → dispatched later (RT-01) |
| After AI response, before extraction commit | One repeated AI call; one row persists |
| After extraction commit, before apply | Apply job (or reconciler) applies once; no AI (RT-02) |
| During apply | Rollback; retry |
| During media processing | Stage-level retry; Gemini file reused or re-uploaded |

---

## 12. Concurrency

### 12.1 Writers per record

| Record | Writers |
|---|---|
| `work_items`, `decisions` | apply, adjudication apply, user API, time sweeper, merge, priority projection |
| `conversations` | normalize, user API, priority projection |
| `persons` | normalize, user API, nightly stats, merges |
| `reminders`, `notifications` | sweeper, handlers, user API |
| `sync_cursors` | sync jobs, connection API |

### 12.2 Rules

1. **Event-first writes with row locks.** All item/decision changes go through `work.append_event`: in one transaction, `SELECT … FOR UPDATE` the row, insert the event (dedupe key), recompute the projection with the fold, increment `version`, write the outbox event.
2. **Per-user serialization only where required.** The apply step (candidate matching + create-or-attach) and merges run under `pg_advisory_xact_lock(hashtextextended('merge:' || user_id, 0))`. Apply transactions contain no network calls, so they are short. Different users never block each other. **No global locks.**
3. **Lock order:** user merge lock → item/decision rows by ascending `id` → person rows by ascending `id`.
4. **Isolation:** READ COMMITTED with explicit locks (no SERIALIZABLE).
5. **Projection-only columns** (priority, stale flags) are written with `UPDATE … WHERE id = $1 AND version = $2` and retried on mismatch; they do not bump `version`.
6. **User precedence:** user actions are authority-5 events; the fold applies authority before recency, so a model event committing after a user edit cannot override it.

### 12.3 Optimistic concurrency on the API

- Resources expose `version` (also returned as a strong `ETag: "v<version>"`).
- `PATCH` with `If-Match: "v<n>"` is **strict**: mismatch → `412 Precondition Failed` (RFC 9110 semantics).
- `PATCH` without `If-Match` must include `base_version` in the body and gets **field-level** checking: if events after `base_version` changed any field in the request, → `409 Conflict` with the current resource and conflicting fields; otherwise the edit applies. This lets a user edit a title while a background status signal updated `reported_status`.
- Action endpoints (confirm, complete, …) are state transitions validated against current state (e.g., completing a cancelled item → `409`).

### 12.4 Concurrent commitment matching

Two sources describing the same commitment (meeting and email) can finish extraction at the same time. Both apply jobs take the same per-user merge lock; the second sees the item created by the first among its candidates and attaches evidence instead of creating a duplicate (RT-03).

### 12.5 Merges and redirection

`merged_into_id` on persons, items and decisions; merge = event + redirect + move identifiers/evidence under the merge lock. Repositories resolve one redirect level on read; a background job re-points references. In-flight extractions referencing merged IDs resolve at apply.

### 12.6 Coverage checklist (mapped in `IMPLEMENTATION_PLAN.md`)

| Requirement | Mechanism | Test |
|---|---|---|
| Row locking | §12.2 rule 1 | RT-03b |
| Optimistic concurrency, version numbers | §12.3 | API tests (412/409) |
| Conflict detection | Field-level check; `conflict_detected` events | API tests; CC-10, CC-31 |
| User edits take precedence over AI | Authority 5 in fold | RT-03b, CC-31 |
| Concurrent commitment matching | Merge lock + candidates | RT-03 |
| Per-user serialization where required | Advisory lock on apply/merge only | RT-03, lock-wait metric |

---

## 13. Deletion

### 13.1 Logical vs. permanent

| Kind | Used for | Mechanism | Reversible |
|---|---|---|---|
| **Logical** (operationally useful) | Rejected items (`verification_status`), cancelled/archived items, auto-archived suggestions, user-deleted user-created items (`deleted_at`, restorable for 30 days then permanent), provider Trash/Spam (`source_items.deleted_at` + `trashed` flag), cancelled meetings, merged entities (`merged_into_id`), paused connections | Status/`deleted_at` columns; partial indexes exclude them | Yes |
| **Permanent** (required) | Account deletion; source disconnect with purge; provider permanent deletion of a message (body, chunks, evidence quotes); retention purges (raw bodies, recordings); user deletion of chat sessions; expiry of exports, outbox, idempotency keys; Gemini Files after use | Ordered deletion jobs, `DELETE` in batches | No |

### 13.2 No cascading deletes

Foreign keys use `ON DELETE NO ACTION`. Deletion jobs remove children before parents in a fixed order. No cascade is justified in the MVP: cascades would hide the order of privacy-relevant deletions and could remove user-authored history unintentionally when a source is purged.

### 13.3 Ordered deletion jobs

| Job | Order |
|---|---|
| Account deletion | Mark user `deleting` (all jobs for this user become no-ops; API returns 410) → revoke Google tokens → delete object storage objects and provider files → per module `purge_user` in order: chat → attention → retrieval → work (events, item links, evidence, items, decisions) → intelligence → meetings → communication → people → projects → ingestion → connections → identity → platform (the user's `event_consumptions` then `outbox` rows, required by the outbox FK, §7.3.1) → user row; `audit_log` keeps a content-free record |
| Source purge (disconnect with purge) | Pause sync → for the connection's source items: chunks → evidence quotes redacted and evidence rows deleted where no user event references the item → AI-only items whose evidence is all from this connection deleted; user-touched items kept with `has_source_gap` → extractions → messages/meetings → source items → cursors → connection |
| Provider deletion of one message | §9.3 |
| Retention purge | Bodies/recordings older than policy: content columns nulled (`body_purged_at`) and objects deleted; rows, evidence quotes and facts kept |

Each step is idempotent (`DELETE … WHERE user_id = $1 LIMIT 5000` loops), resumable, and audited. Backups age out within the stated backup window (privacy notice).

---

## 14. Errors and retries

### 14.1 Domain errors (no HTTP concepts)

`eca.platform.errors` defines: `NotFound`, `Conflict` (with conflicting fields), `PreconditionFailed` (version mismatch, missing scope), `ValidationFailed` (with field errors), `PermissionDenied`, `RateLimited` (with retry-after), `BudgetExceeded`, `UpstreamUnavailable`, `AuthRevoked`, `CursorExpired`, `Gone` (user deleting). Services and workers raise only these; they never import FastAPI.

### 14.2 API translation (RFC 9457 Problem Details)

| Domain error | HTTP |
|---|---|
| `ValidationFailed` | 422 |
| `NotFound` (including other users' IDs — never 403, to avoid existence leaks) | 404 |
| `Conflict` | 409 |
| `PreconditionFailed` | 412 (version) / 409 with `code = scope_required` (missing grant) |
| `PermissionDenied` | 403 |
| `RateLimited`, `BudgetExceeded` | 429 + `Retry-After` |
| `UpstreamUnavailable` | 503 |
| `AuthRevoked` | 409 `code = reauth_required` |
| `Gone` | 410 |

```json
{"type": "https://errors.eca.app/conflict", "title": "Item changed", "status": 409,
 "detail": "due_at changed since version 6", "instance": "/api/v1/work-items/0192…",
 "code": "conflict", "errors": [{"field": "due_at", "message": "changed by another update"}],
 "current_version": 8, "request_id": "req_01…"}
```

Content type `application/problem+json`. FastAPI's `RequestValidationError` is mapped to the same shape with field errors.

### 14.3 Worker retry policy

`delay = min(base · 2^(n−1) + random(0–1 s), max_delay)`, where n ≥ 1 is the number of failed attempts so far, so the first retry waits about `base`. "Max attempts" counts every run, including the first. A provider `Retry-After` overrides the delay.

| Task class | base | max_delay | Max attempts | After max |
|---|---|---|---|---|
| Sync | 30 s | 30 min | Unlimited while connection active | Connection `error` after 12 consecutive failures; alert |
| Normalize / apply / index / handlers | 5 s | 10 min | 8 | Source `needs_attention` / extraction `apply_failed` / dead job alert |
| Extract (AI) | 10 s | 30 min | Attempt cap 4 model calls (`AI_PIPELINE.md` §7) | `failed_permanent`, source `needs_attention` |
| Media | 60 s | 1 h | 4 | Recording failed at stage; user notified |
| Outbox dispatch | 1 s | 5 min | 10 | `failed`, alert |

Non-retryable: validation errors, `AuthRevoked` (connection → `needs_reauth`), `Gone`, safety blocks. Procrastinate's stalled-job recovery re-queues jobs whose worker died.

**Handler jobs on Procrastinate (slice 0.3).** Procrastinate is pinned to **3.10.0** (§15). Its built-in `RetryStrategy` computes `wait + linear_wait · attempts + exponential_wait ** (attempts + 1)` from integer seconds. Exponential growth there uses the base as the exponent's base, not as a multiplier, and there is no jitter and no cap. So it cannot produce `5 s · 2^(n−1) + random(0–1 s)` capped at 10 min (inspected in `procrastinate/retry.py`, 3.10.0). The smallest custom strategy is enough:

- `HandlerRetryStrategy(procrastinate.BaseRetryStrategy)` in `eca.platform`. Fields: `base_s = 5`, `max_delay_s = 600`, `max_attempts = 8`, a list of non-retryable exception types (`ValidationFailed`, `AuthRevoked`, `Gone`), and an injectable clock and random source for unit tests.
- It implements only `get_retry_decision(exception, job)`. Procrastinate's `job.attempts` is the number of earlier runs (0 on the first run), so n = `job.attempts + 1`.
- It returns `None` (no retry) when the exception is non-retryable or n ≥ 8. Otherwise it returns `RetryDecision(retry_at = now + min(5 · 2^(n−1) + U[0, 1), 600) s)`. Retry waits are therefore about 5, 10, 20, 40, 80, 160 and 320 s, plus jitter. The 10-minute cap is part of the formula but is not reached within 8 attempts.
- When it returns `None`, Procrastinate marks the job `failed`. That is the "dead job". The strategy logs `handler_job_dead` with `event_id`, `handler`, `job_id` and the error class (never content).
- In slice 0.3 dead jobs are reported by that log line only. They are not replayed automatically, and `eca ops` replay of dead jobs comes later (§14.4).

**Relationship to outbox dispatch attempts.** These are two separate counters:
- `outbox.attempts` counts failures to turn an event into jobs (defer errors, unregistered type): base 1 s, max 5 min, `failed` at 10 (§7.3.2).
- `procrastinate_jobs.attempts` counts runs of one handler job.

Once a row is `dispatched`, handler failures never send it back to `pending` and never touch `outbox.attempts`. The reconciler does not touch handler jobs (§7.5).

**Stalled jobs.** Procrastinate 3.10.0 workers record heartbeats in `procrastinate_workers` (defaults: update every 10 s, stalled after 30 s). The task `recover_stalled_jobs` (§15) runs once when the worker starts and then every minute. Each pass does three things:
- calls `job_manager.get_stalled_jobs(seconds_since_heartbeat = stalled timeout)`;
- re-queues each job with `retry_job` (no delay);
- prunes the stalled workers.

`retry_job` increments the job's `attempts`, so a worker death counts as one of the 8 attempts. This Procrastinate-internal heartbeat is used only for stalled-job recovery. It is not a health or readiness endpoint (§16.5).

### 14.4 Dead letters and replay

`eca ops` CLI: list `needs_attention`, `apply_failed`, `failed` outbox, dead jobs; `replay --stage …`, `refold`, `reapply --user …`, `reextract --window … --dry-run`, `outbox retry`.

---

## 15. Jobs and queues

Procrastinate is pinned exactly (`procrastinate==3.10.0`, slice 0.3; requires Python ≥ 3.10 and brings `psycopg[pool]`). Its schema is applied only by Alembic (§7.7).

| Queue | Concurrency (per worker) | Tasks |
|---|---|---|
| `events` | 8 | Outbox handlers (priority, reminders, people/project reactions, invalidations) |
| `sync` | 4 | `sync_mail`, `sync_calendar`, `import_mail` |
| `ingest` | 8 | `normalize` |
| `extract` | 8 | `extract_email` (AI-01), `adjudicate` (AI-02, if enabled), `thread_summary` (AI-03, long threads) |
| `apply` | 8 | `apply_extraction` |
| `embed` | 2 | `index` (AI-04) |
| `ai_standard` | 4 | `meeting_extract` (AI-10), `meeting_asks` (AI-11, on view) |
| `media` | 1 | `media_prepare`, `transcribe` (AI-09) |
| `schedule` | 1 | Periodic tasks |

| Task | Trigger | `lock` / `queueing_lock` | Idempotency |
|---|---|---|---|
| `sync_mail` | Periodic (adaptive), manual | `sync:{conn}:mail` | Cursor rule, source keys |
| `import_mail` | First connect | `import:{conn}` | Page-token checkpoint |
| `sync_calendar` | Every 15 min, on demand | `sync:{conn}:cal:{id}` | Etag, source keys |
| `normalize` | `SourceItemStored` | `norm:{source_item}` | Stage check |
| `extract_email` | `MessageNormalized` (relevant) | `extract:{source_item}:{content_hash}:{prompt_version}` | Extraction key |
| `apply_extraction` | `ExtractionCompleted` | `apply:{extraction}` | `apply_status` |
| `adjudicate` | `AdjudicationNeeded` | `adj:{parent_extraction}:{candidate}` | Extraction key |
| `index` | `MessageNormalized`, `TranscriptStored`, `MeetingChanged` | `index:{source_item}` | Chunk/embedding upsert |
| `thread_summary` | `ConversationStateChanged` (debounced 10 min) | `summary:{conversation}` | Keyed by last message |
| `media_prepare`, `transcribe` | `RecordingUploaded` | `media:{recording}` | sha256, transcript key |
| `meeting_extract` | `TranscriptStored` / mapping confirmed | `mx:{recording}:{content_hash}:{v}` | Extraction key |
| `recompute_priority` | `WorkItemChanged`, `ConversationStateChanged`, `PersonChanged` | `prio:{entity}` | Pure projection |
| `evaluate_reminders` | Item/conversation/meeting events | `rem:{entity}` | Fingerprint |
| `reminder_sweep` | Every 5 min | `reminder_sweep` | Fingerprint, notification key |
| `time_sweep` | Hourly | `time_sweep` | Time-bucket dedupe keys |
| `prep_sections` (deterministic) | T−45 min; recomputed on relevant events | `prep:{meeting}:{version}` | Versioned |
| `meeting_asks` (AI-11) | User opens prep view with non-empty sections | `asks:{meeting}:{version}` | Versioned |
| `daily_briefing` (queue `events`; deterministic, no AI) | Before work start, active users only | `brief:{user}:{date}` | One per user-day |
| `reconcile` | Every 5 min | `reconcile` | Re-enqueue only (slice 0.3: outbox re-dispatch; slice 1.3 adds source-item stages, §7.5) |
| `recover_stalled_jobs` (slice 0.3) | Once at worker start, then every 1 min (`schedule`) | `recover_stalled_jobs` | Re-queues jobs of workers whose Procrastinate heartbeat is stale (§14.3) |
| `outbox_dispatch` | Worker loop, every 1 s | Row-level `SKIP LOCKED` | §7.4 |
| `retention_purge` | Nightly | `retention` | Batched, idempotent |
| `delete_account`, `purge_source` | User request | `delete:{user}` / `purge:{conn}` | Resumable |
| `cost_rollup` | Every 15 min | `cost_rollup` | Recompute the window (delete, insert) |

Per-user fairness: at most 4 concurrent `extract` jobs per user (checked at job start against running jobs for that user; excess re-deferred with a short delay), so one large import cannot starve other users.

---

## 16. HTTP API

### 16.1 Principles

- Conventional REST over JSON at `/api/v1`. Resource collections are plural nouns; resource IDs are **path parameters** (`/work-items/{item_id}`); filters, sorting and pagination are **query parameters**.
- Methods: `GET` read (safe), `POST` create or non-idempotent command, `PATCH` partial update (JSON Merge Patch semantics on the documented editable fields), `PUT` full replacement only for singleton settings documents (`/preferences`), `DELETE` remove (idempotent; a repeated DELETE returns 204 or 404 consistently as documented per resource).
- State transitions that carry domain meaning (confirm, reject, complete, reopen, merge, snooze) are **action sub-resources** with `POST` (`/work-items/{id}/confirm`). They are commands that create events with authority and audit, not field patches; this keeps lifecycle rules out of generic PATCH handling.
- snake_case JSON; UUIDs as strings; ISO 8601 UTC timestamps.
- FastAPI generates OpenAPI 3.1; the web app uses a generated TypeScript client (e.g., `openapi-typescript` + `openapi-fetch`).
- Auth: session cookie (`HttpOnly; Secure; SameSite=Lax`) + CSRF header `X-CSRF-Token` on unsafe methods; Next.js calls the API same-origin through a rewrite.
- Responses carry `X-Request-Id`. Every AI-derived resource includes `origin`, `verification_status` and the `provenance` envelope (source, confidence, `derived_at`, extraction method, model, prompt version, evidence) defined in `AI_PIPELINE.md` §5.1.

### 16.2 Status codes

`200` read/update, `201` create (with `Location`), `202` accepted async job (with status URL), `204` no content, `304` not modified (only where ETags are offered), `400` malformed, `401` unauthenticated, `403` forbidden, `404` not found (also for other users' IDs), `409` conflict, `410` gone (account deleting), `412` precondition failed, `422` validation, `429` rate/budget limited, `503` upstream unavailable. Errors use Problem Details (§14.2).

### 16.3 Idempotency and concurrency

- `Idempotency-Key` header accepted on `POST` creates and commands (required for `/recordings` and chat messages); stored 24 h in `idempotency_keys (user_id, key, request_hash, status_code, response)`. Same key + same body → stored response; same key + different body → `422`.
- `PATCH` and `DELETE` are idempotent by method semantics plus versions (§12.3).

### 16.4 Pagination (keyset cursors)

All collections use `?limit=` (default 25, max 100) and `?cursor=` → `{"items": [...], "next_cursor": "…" | null}`. The cursor is an opaque, signed encoding of the sort key and `id` of the last item.

Why cursor pagination: the collections here (changes, events, needs-response, items sorted by priority or due date, chat history) change continuously as sync and background jobs run. Offset pagination skips or repeats rows when items are inserted or re-ranked between page requests, and `OFFSET n` scans grow linearly. Keyset cursors are stable under inserts and use the same indexes as the queries (§17.3). Totals are returned only where cheap (`total` on small bounded lists such as connections).

### 16.5 Endpoint catalog (MVP)

**Auth and user**

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/auth/google/login` | Redirect to Google OIDC (PKCE, `state`, `nonce`) |
| GET | `/api/v1/auth/google/callback` | Complete sign-in |
| POST | `/api/v1/auth/logout` | End session (204) |
| GET | `/api/v1/me` | User, timezone, granted capabilities |
| GET / PUT | `/api/v1/preferences` | Preferences document |
| DELETE | `/api/v1/me` | Request account deletion (202 + job status URL; recent re-auth required) |

**Connections**

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/connections` | List with status, scopes, last sync, import progress |
| POST | `/api/v1/connections` | Start connect `{provider, capability}` → `{authorization_url}` |
| GET | `/api/v1/connections/google/callback` | Complete connect |
| POST | `/api/v1/connections/{connection_id}/sync` | Manual sync (202) |
| DELETE | `/api/v1/connections/{connection_id}?purge=true|false` | Disconnect (202 when purging) |

**Today, changes, briefing**

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/today` | Attention, commitments, waiting-for, meetings, deadlines, needs-response |
| GET | `/api/v1/changes?since=&scope=&cursor=` | Net changes (`CONTEXT_ARCHITECTURE.md` §10.11) |
| PUT | `/api/v1/checkpoints/{surface}` | Mark surface seen |
| GET | `/api/v1/briefings/{date}` | Daily briefing |
| GET | `/api/v1/days/{date}` | Structured day view ("what happened") |

**Work items and decisions**

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/work-items?direction=&status=&verification=&person_id=&project_id=&due_before=&cursor=` | Lists |
| POST | `/api/v1/work-items` | User-created item (201) |
| GET | `/api/v1/work-items/{item_id}` | Item with fold, timeline, evidence, conflicts (`ETag`) |
| PATCH | `/api/v1/work-items/{item_id}` | Edit fields (`If-Match` or `base_version`) |
| DELETE | `/api/v1/work-items/{item_id}` | Delete a user-created item (logical, 30-day restore); AI items use `/reject` |
| POST | `/api/v1/work-items/{item_id}/confirm` · `/reject` · `/complete` · `/reopen` · `/cancel` | Lifecycle/verification commands |
| POST | `/api/v1/work-items/{item_id}/merge` | `{into_id}` |
| GET | `/api/v1/decisions?project_id=&since=&kind=&cursor=` · `/api/v1/decisions/{decision_id}` | Decisions and open questions |
| PATCH | `/api/v1/decisions/{decision_id}` | Edit |
| POST | `/api/v1/decisions/{decision_id}/confirm` · `/reject` · `/resolve` | Commands |

**Conversations**

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/conversations?awaiting=user&cursor=` | Needs-response with reasons |
| GET | `/api/v1/conversations/{conversation_id}` | Email context panel |
| POST | `/api/v1/conversations/{conversation_id}/mark-handled` | Command |
| PATCH | `/api/v1/conversations/{conversation_id}` | `priority_override` |

**People, organizations, projects**

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/people?q=&sort=importance&cursor=` · `/api/v1/people/{person_id}` | People, person context |
| PATCH | `/api/v1/people/{person_id}` | Importance, role, relationship type |
| POST | `/api/v1/people/{person_id}/merge` · `/aliases` | Merge, add alias |
| GET / PATCH | `/api/v1/organizations` · `/api/v1/organizations/{org_id}` | Organizations |
| GET / POST | `/api/v1/projects` | List / create |
| GET / PATCH | `/api/v1/projects/{project_id}` | Project context / edit |
| POST | `/api/v1/projects/{project_id}/confirm` · `/items` | Confirm suggestion; assign item |
| GET | `/api/v1/topics?q=` | Topic-mode grouping |

**Meetings and recordings**

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/meetings?from=&to=&cursor=` · `/api/v1/meetings/{meeting_id}` | Meetings |
| GET | `/api/v1/meetings/{meeting_id}/prep` | Prep brief |
| PUT | `/api/v1/meetings/{meeting_id}/speakers` | Confirm speaker mapping |
| POST | `/api/v1/recordings` | Upload init → pre-signed URL (201, `Idempotency-Key`, sha256 dedupe) |
| POST | `/api/v1/recordings/{recording_id}/complete` | Start processing (202) |
| GET | `/api/v1/recordings/{recording_id}` | Status by stage |
| POST | `/api/v1/recordings/{recording_id}/retry` | Retry failed stage |
| PUT | `/api/v1/recordings/{recording_id}/meeting` | Link to a calendar meeting |

**Chat**

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/chat/sessions` | Create (global or meeting scope) |
| GET | `/api/v1/chat/sessions?cursor=` · `/api/v1/chat/sessions/{session_id}` | History |
| DELETE | `/api/v1/chat/sessions/{session_id}` | Permanent delete |
| POST | `/api/v1/chat/sessions/{session_id}/messages` | Ask; `text/event-stream` response with events `plan`, `sources`, `delta`, `final`, `error` (`Idempotency-Key`). Clients use fetch-based SSE (EventSource cannot POST) |
| POST | `/api/v1/conversations/{conversation_id}/reply-guidance` | SSE; draft for copying only |
| POST | `/api/v1/chat/messages/{message_id}/feedback` | Feedback |

**Sources, reminders, privacy**

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/evidence/{evidence_id}` | Quote, source metadata, provider deep link or transcript timestamp |
| GET | `/api/v1/sources/{source_item_id}` | Source view (respecting retention) |
| GET | `/api/v1/reminders?state=&cursor=` | Reminders |
| POST | `/api/v1/reminders/{reminder_id}/snooze` · `/dismiss` · `/acted` | Commands |
| POST | `/api/v1/push-subscriptions` · DELETE `/api/v1/push-subscriptions/{id}` | Web Push |
| GET | `/api/v1/data-summary` | What is connected, stored, retained |
| POST | `/api/v1/exports` · GET `/api/v1/exports/{export_id}` | Export job |

**Health**: `GET /healthz`, `GET /readyz` (API process: database reachable, schema at head). The worker has no HTTP server and no health or readiness check in slice 0.3. In 0.3, dispatch progress is observed through the oldest-pending age and `failed` count that the reconciler logs (§7.5, §19). A worker liveness or readiness signal (for example a dispatcher heartbeat) is designed together with the hosting decision (Q1), because its form depends on the platform's probes. It is not part of slice 0.3. Procrastinate's internal worker heartbeat (§14.3) is a different thing: it serves stalled-job recovery only.

### 16.6 Inbound rate limits

Per user, enforced in the API process (in-memory token buckets; with ≤ 2 API instances the effective limit is at most 2× the configured value, which is acceptable because AI spend is bounded separately by budget caps): chat 20/min, reply guidance 10/min, uploads 10/hour (≤ 2 GB, ≤ 3 h), mutations 120/min, reads 600/min; auth endpoints 10/min per IP. Move to a shared store only if instances grow.

---

## 17. Database schema

### 17.1 Conventions

- PostgreSQL 16+; extensions `vector` (pgvector ≥ 0.8), `pg_trgm`, `citext`.
- UUIDv7 primary keys (application-generated); UUIDv5 where IDs must be reproducible (evidence).
- `TEXT` for strings; enumerations as `TEXT` + `CHECK`.
- Mutable entity tables: `id, created_at, updated_at, deleted_at` (+ `version` where optimistic concurrency applies); `updated_at` by trigger; partial indexes `WHERE deleted_at IS NULL`. Append-only tables (`context_events`, `evidence`, `extractions` status aside, `ai_calls`, `audit_log`, `outbox`, `event_consumptions`, `feedback_events`) have `created_at` only.
- **Foreign keys** (`ON DELETE NO ACTION`, no cascades) on: every `user_id → users` (for `outbox`, `ai_calls` and `ai_cost_rollups`, which exist before `users`, the FK is added by the slice 1.1 migration that creates `users`: §7.3.1, §7.6); provenance (`extractions.source_item_id`, `evidence.source_item_id`, `evidence.extraction_id`, `item_evidence.evidence_id`, `context_events.evidence_id`, `work_items.reported_status_evidence_id`); parent–child (`messages → source_items/conversations`, `message_participants → messages/persons`, `transcript_segments → recordings`, `meeting_participants → meetings/persons`, `chunks → source_items`); entity references on items (`owner/counterparty/requester → persons`, `project_id → projects`, `merged_into_id → same table`). Polymorphic references (`context_events.entity_id`, `item_evidence.item_id`, `entity_links`) are validated in the `work` service.
- Row-level security (ENABLE + FORCE) on every user-owned business table: `USING`/`WITH CHECK (user_id = eca_current_user_id())`, where `eca_current_user_id()` reads `current_setting('app.user_id', true)`; the UoW runs `SET LOCAL app.user_id` (as `set_config(…, true)`) per transaction; an unset variable yields no rows (fail closed). Cross-user maintenance jobs iterate users and set the variable per user transaction. Infrastructure tables (`outbox`, `event_consumptions`, Procrastinate objects) are isolated per role by grants and role-targeted policies, not by `app.user_id` (§7.6). Only the migration role bypasses RLS.
- **Provenance columns** (`AI_PIPELINE.md` §5.1) on every AI-derived row or column group: `extraction_id` (FK to `extractions`, null for non-LLM methods), `extraction_method` (`llm`, `llm_adjudicated`, `rule`, `deterministic`, `embedding_match`, `transcription`), `model` (null for non-LLM), `derived_at`, `confidence`, `confidence_band`; evidence via `evidence`/`item_evidence` (items, decisions) or `covered_source_ids uuid[]` (summaries). Column groups use a prefix where a table mixes classes: `messages.triage_*`, `conversations.summary_*`, `meetings.summary_*`, `persons.role_*`, `meeting_participants.mapping_*`. Per-change provenance lives in `context_events.extraction_id` and `payload`. Inserts without complete provenance are rejected by the `work`/owning service (tested by `AI_EVALUATION.md` E14).

### 17.2 Core DDL (abridged)

```sql
CREATE TABLE source_items (
  id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id),
  connection_id uuid NULL REFERENCES connections(id),
  kind text NOT NULL CHECK (kind IN ('message','calendar_event','recording','transcript_file')),
  provider text NOT NULL, external_id text NOT NULL, external_thread_id text NULL,
  content_hash bytea NOT NULL, provider_version text NULL,          -- etag / changeKey
  categories text[] NOT NULL DEFAULT '{}',                           -- neutral: inbox, sent, promotions, social, updates, trash, spam
  occurred_at timestamptz NOT NULL, trashed boolean NOT NULL DEFAULT false,
  stage text NOT NULL DEFAULT 'fetched' CHECK (stage IN ('fetched','normalized','skipped','extract_pending',
        'extracted','applied','needs_attention')),
  stage_attempts smallint NOT NULL DEFAULT 0, stage_updated_at timestamptz NOT NULL, next_attempt_at timestamptz NULL,
  last_error_code text NULL, raw_metadata jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL, deleted_at timestamptz NULL);
CREATE UNIQUE INDEX ux_source_items_ext ON source_items (connection_id, kind, external_id);
CREATE INDEX ix_source_items_stage ON source_items (stage, next_attempt_at)
  WHERE stage IN ('fetched','extract_pending','extracted') AND deleted_at IS NULL;
CREATE INDEX ix_source_items_user_time ON source_items (user_id, occurred_at DESC) WHERE deleted_at IS NULL;

CREATE TABLE messages (
  id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id),
  source_item_id uuid NOT NULL UNIQUE REFERENCES source_items(id),
  conversation_id uuid NOT NULL REFERENCES conversations(id),
  rfc822_message_id text NULL, in_reply_to text NULL,
  sender_person_id uuid NULL REFERENCES persons(id),
  direction text NOT NULL CHECK (direction IN ('inbound','outbound')),
  sent_at timestamptz NOT NULL, subject text NULL,
  body_text text NULL, body_clean text NULL, body_purged_at timestamptz NULL, snippet text NULL,
  is_bulk boolean NOT NULL DEFAULT false, prefilter_reason text NULL,
  triage jsonb NULL, triage_extraction_id uuid NULL REFERENCES extractions(id),
  alias_source_item_ids uuid[] NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL, deleted_at timestamptz NULL);
CREATE UNIQUE INDEX ux_messages_rfc822 ON messages (user_id, rfc822_message_id) WHERE rfc822_message_id IS NOT NULL;
CREATE INDEX ix_messages_conv_time ON messages (conversation_id, sent_at);
CREATE INDEX ix_messages_sender_time ON messages (user_id, sender_person_id, sent_at DESC) WHERE deleted_at IS NULL;

CREATE TABLE work_items (
  id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id),
  type text NOT NULL CHECK (type IN ('task','commitment','request','follow_up','deadline')),
  title text NOT NULL, description text NULL,
  owner_person_id uuid NULL REFERENCES persons(id), counterparty_person_id uuid NULL REFERENCES persons(id),
  requester_person_id uuid NULL REFERENCES persons(id), project_id uuid NULL REFERENCES projects(id),
  project_hint text NULL,
  due_at timestamptz NULL, due_precision text NULL, due_text text NULL, due_kind text NULL,
  lifecycle_status text NOT NULL CHECK (lifecycle_status IN ('open','in_progress','done','cancelled')),
  verification_status text NOT NULL CHECK (verification_status IN ('suggested','confirmed','rejected','user_created')),
  origin text NOT NULL CHECK (origin IN ('ai','user')),
  commitment_strength text NULL CHECK (commitment_strength IN ('explicit','probable','suggestion','inferred')),
  statement_kind text NULL CHECK (statement_kind IN ('promise','request','acceptance','report_commitment','report_request','assignment')),
  direction text NOT NULL CHECK (direction IN ('my_task','my_commitment','delegated','waiting_for','shared','observed','unresolved')),
                                                            -- computed by the deterministic mapping (AI_PIPELINE.md §5.5); recomputable by re-apply
  notes text NULL,                                          -- user-authored only; never written by AI
  confidence real NULL, confidence_band text NULL,
  reported_status text NULL, reported_status_at timestamptz NULL,
  reported_status_evidence_id uuid NULL REFERENCES evidence(id),
  extraction_id uuid NULL REFERENCES extractions(id),        -- creating extraction (null for user-created)
  extraction_method text NOT NULL CHECK (extraction_method IN ('llm','llm_adjudicated','rule','deterministic','user')),
  model text NULL, derived_at timestamptz NOT NULL,
  user_fields text[] NOT NULL DEFAULT '{}',                 -- fields set at authority 5
  pending_adjudication boolean NOT NULL DEFAULT false,
  has_conflict boolean NOT NULL DEFAULT false, has_source_gap boolean NOT NULL DEFAULT false,
  stale boolean NOT NULL DEFAULT false, archived boolean NOT NULL DEFAULT false,
  priority_score real NULL, priority_reasons jsonb NULL, priority_override smallint NULL, priority_computed_at timestamptz NULL,
  dedupe_embedding halfvec(768) NULL,
  first_evidence_at timestamptz NULL, last_activity_at timestamptz NULL,
  merged_into_id uuid NULL REFERENCES work_items(id),
  version int NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL, deleted_at timestamptz NULL);
CREATE INDEX ix_wi_direction_open ON work_items (user_id, direction, due_at)
  WHERE lifecycle_status IN ('open','in_progress') AND verification_status <> 'rejected'
    AND merged_into_id IS NULL AND NOT archived AND deleted_at IS NULL;
CREATE INDEX ix_wi_owner_open ON work_items (user_id, owner_person_id, due_at)
  WHERE lifecycle_status IN ('open','in_progress') AND verification_status <> 'rejected'
    AND merged_into_id IS NULL AND NOT archived AND deleted_at IS NULL;
CREATE INDEX ix_wi_counterparty_open ON work_items (user_id, counterparty_person_id, due_at)
  WHERE lifecycle_status IN ('open','in_progress') AND verification_status <> 'rejected'
    AND merged_into_id IS NULL AND NOT archived AND deleted_at IS NULL;
CREATE INDEX ix_wi_due_open ON work_items (user_id, due_at)
  WHERE due_at IS NOT NULL AND lifecycle_status IN ('open','in_progress') AND deleted_at IS NULL;
CREATE INDEX ix_wi_project ON work_items (user_id, project_id) WHERE deleted_at IS NULL;
CREATE INDEX ix_wi_hint_trgm ON work_items USING gin (project_hint gin_trgm_ops) WHERE project_hint IS NOT NULL;
-- dedupe candidates: exact distance over the user's few thousand items; no ANN index needed

CREATE TABLE evidence (
  id uuid PRIMARY KEY,                                   -- uuid5(extraction_id, candidate_index)
  user_id uuid NOT NULL REFERENCES users(id),
  source_item_id uuid NOT NULL REFERENCES source_items(id),
  extraction_id uuid NULL REFERENCES extractions(id), candidate_index int NULL,
  quote text NOT NULL, char_start int NULL, char_end int NULL, start_ms int NULL, end_ms int NULL,
  occurred_at timestamptz NOT NULL, created_at timestamptz NOT NULL,
  UNIQUE (extraction_id, candidate_index));

CREATE TABLE item_evidence (
  item_type text NOT NULL CHECK (item_type IN ('work_item','decision')), item_id uuid NOT NULL,
  evidence_id uuid NOT NULL REFERENCES evidence(id),
  relation text NOT NULL CHECK (relation IN ('supports','updates','contradicts','completes','superseded')),
  user_id uuid NOT NULL REFERENCES users(id), created_at timestamptz NOT NULL,
  PRIMARY KEY (item_type, item_id, evidence_id));
CREATE INDEX ix_item_evidence_evidence ON item_evidence (evidence_id);

CREATE TABLE context_events (
  id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id),
  entity_type text NOT NULL, entity_id uuid NOT NULL, event_type text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}', evidence_id uuid NULL REFERENCES evidence(id),
  actor text NOT NULL CHECK (actor IN ('system','model','user','time')),
  authority smallint NOT NULL CHECK (authority BETWEEN 1 AND 5), materiality smallint NOT NULL CHECK (materiality BETWEEN 0 AND 3),
  extraction_id uuid NULL REFERENCES extractions(id),        -- set for actor = 'model'
  dedupe_key bytea NOT NULL, occurred_at timestamptz NOT NULL, recorded_at timestamptz NOT NULL,
  UNIQUE (user_id, dedupe_key));
CREATE INDEX ix_ce_feed ON context_events (user_id, recorded_at DESC, id DESC);
CREATE INDEX ix_ce_entity ON context_events (user_id, entity_type, entity_id, occurred_at);

CREATE INDEX ix_conv_needs_reply ON conversations (user_id, priority_score DESC, last_inbound_at DESC, id)
  WHERE awaiting = 'user' AND needs_reply AND handled_by_user_at IS NULL AND deleted_at IS NULL;
CREATE UNIQUE INDEX ux_person_identifiers ON person_identifiers (user_id, kind, value_normalized);
CREATE INDEX ix_person_alias_trgm ON person_identifiers USING gin (value_normalized gin_trgm_ops) WHERE kind = 'name_alias';
CREATE INDEX ix_entity_mentions ON entity_mentions (user_id, entity_type, entity_id, occurred_at DESC);
CREATE UNIQUE INDEX ux_reminders_fp ON reminders (user_id, fingerprint);
CREATE INDEX ix_reminders_due ON reminders (fire_at) WHERE state = 'pending';
CREATE UNIQUE INDEX ux_notifications ON notifications (reminder_id, channel);
CREATE UNIQUE INDEX ux_recordings_sha ON recordings (user_id, sha256);
CREATE UNIQUE INDEX ux_chunks ON chunks (source_item_id, chunk_index);
CREATE INDEX ix_chunks_hnsw ON chunks USING hnsw (embedding halfvec_cosine_ops);
CREATE INDEX ix_chunks_fts ON chunks USING gin (tsv);
CREATE INDEX ix_chunks_user_time ON chunks (user_id, occurred_at DESC);
CREATE INDEX ix_chunks_persons ON chunks USING gin (person_ids);
```

`outbox`, `event_consumptions` (§7.3), `extractions` (§8.1), `sync_cursors` (§11.1) as defined above. The remaining tables (`users`, `auth_sessions`, `user_preferences`, `user_checkpoints`, `connections`, `conversations`, `message_participants`, `persons`, `person_identifiers`, `organizations`, `entity_mentions`, `meetings`, `meeting_participants`, `recordings`, `transcript_segments`, `projects`, `project_members`, `decisions`, `work_item_owners`, `entity_links`, `chunks`, `reminders`, `notifications`, `briefings`, `feedback_events`, `chat_sessions`, `chat_messages`, `retrieval_traces`, `ai_calls`, `audit_log`, `idempotency_keys`, `export_jobs`, `deletion_jobs`) follow the conventions above; their columns are listed in `TECHNICAL_DESIGN.md` §9 and `CONTEXT_ARCHITECTURE.md` §5. `decisions` mirrors `work_items` for `origin`, `verification_status`, `user_fields`, `version`, `merged_into_id` and the provenance columns.

### 17.3 Index-to-query map

| Query | Index |
|---|---|
| What am I waiting for? / What did I promise? / Who is waiting on me? | `ix_wi_direction_open` |
| What did I promise Sarah? / What did John promise? | `ix_wi_counterparty_open` / `ix_wi_owner_open` |
| Who needs my response? | `ix_conv_needs_reply` |
| Deadlines / next action | `ix_wi_due_open` |
| Person context | `ix_wi_owner_open`, `ix_wi_counterparty_open`, `ix_messages_sender_time`, `ix_entity_mentions` |
| What changed / day view | `ix_ce_feed` |
| Item timelines | `ix_ce_entity`, `item_evidence` PK |
| Topic discovery | `ix_chunks_hnsw`, `ix_chunks_fts`, `ix_chunks_user_time` |
| Project hint match | `ix_wi_hint_trgm` |
| Reconciler: outbox re-dispatch (slice 0.3) / source-item stage scan (slice 1.3) | `ix_outbox_pending` / `ix_source_items_stage` |
| Reminder sweep | `ix_reminders_due` |
| Outbox dispatch | `ix_outbox_pending` |

### 17.4 Growth and migrations

Partition `chunks`, `messages`, `context_events` by `HASH(user_id)` when any exceeds ~20 M rows (queries already lead with `user_id`). Alembic sequential revisions, one concern each, expand-then-contract, `CREATE INDEX CONCURRENTLY` on large tables, run only in the release step.

---

## 18. Caching

| Cache | Key | Invalidation |
|---|---|---|
| Meeting prep sections and asks (`meetings.prep_brief`) | Meeting + max `version` of included entities | Version change → sections recomputed; asks regenerated on next open |
| Daily briefing (`briefings`) | `(user_id, date)` | One per day; "updated since briefing" banner on later material changes |
| Access tokens | Connection ID, in process memory | Expiry − 60 s |
| Model registry, prices, priority weights | Process memory | Reload on deploy |
| Item `ETag` | `version` | Natural |
| Gemini implicit caching | Stable prompt prefix | Provider-managed |

**Not in the MVP** (complexity audit, `ARCHITECTURE_REVIEW.md` §E): rendered-card cache, query-embedding cache, dashboard ETag/`context_version` counter, chat packet reuse, Redis. Dashboard and list queries are indexed SQL over small per-user sets.

---

## 19. Observability

| Area | MVP |
|---|---|
| Logs | structlog JSON with `request_id`, `event_id`, `job_id`, `source_item_id`, `stage`, `user_ref` (hashed), `error_code`; never message content or tokens |
| Correlation | `outbox.correlation` and job args carry `request_id`/trace IDs so one email can be followed through sync → normalize → extract → apply → reminder |
| Errors | Sentry with PII scrubbing and request bodies disabled |
| AI telemetry | `ai_calls` (`AI_COST_MODEL.md` §8) |
| Operational metrics | SQL views: sync lag, items per stage and age, outbox oldest pending, queue depth/age (Procrastinate tables), dead jobs, `needs_attention` count, apply lock wait, 409/412 rates, provider error rates, chat latency |
| Alerts | Sync lag > 30 min for > 10% connections; outbox oldest pending > 2 min; `needs_attention` growth > 50/h; dead handler jobs > 0; provider error rate > 5% for 10 min; budget thresholds; RLS errors |
| Tracing | Code instrumented with OpenTelemetry API; exporter enabled once a backend is chosen with hosting (deferred) |

---

## 20. Connector portability

### 20.1 Normalized objects

Connectors return only these DTOs; nothing outside `connectors/` sees provider payloads.

| DTO | Fields (abridged) |
|---|---|
| `NormalizedMessage` | `external_id`, `thread_external_id`, `rfc822_id`, `sent_at`, `from`, `to`, `cc`, `subject`, `body_text`, `body_html`, `categories` (neutral), `headers_subset`, `content_hash`, `provider_version`, `deep_link` |
| `NormalizedConversationRef` | `external_id`, `kind` (`email_thread`, `chat_thread`, `channel`), `title` |
| `NormalizedEvent` | `external_id`, `series_external_id`, `ical_uid`, `start`, `end`, `timezone`, `title`, `description`, `attendees` (email, name, response), `organizer`, `conference_uri`, `status`, `provider_version` |
| `NormalizedPerson` | `email`, `display_name`, `provider_user_id` |
| `SyncBatch[T]` | `items`, `deleted_external_ids`, `next_cursor` (opaque), `has_more` |

### 20.2 Mapping

| Provider object | Internal object | Notes |
|---|---|---|
| Gmail message | `Message` in `Conversation(kind=email_thread)` | Immutable content; labels → `categories` |
| Outlook message | `Message` in `Conversation(kind=email_thread)` | Graph delta queries; `internetMessageId` → `rfc822_id`; `changeKey` → `provider_version`; content can change for drafts (only sent/received mail ingested) |
| Teams chat/channel message | `Message` in `Conversation(kind=chat_thread or channel)` | Messages editable/deletable → `content_hash` change → re-extraction (§8.4); no subject; extraction per debounced thread window |
| Slack message | `Message` in `Conversation(kind=chat_thread or channel)` | `(channel, ts)` as ID; thread via `thread_ts`; edits/deletes via events; user ID → email via `users.info` (needs email scope) |
| Google Calendar event | `Meeting` | `recurringEventId` → `series_key`; `etag` |
| Microsoft calendar event | `Meeting` | `seriesMasterId` → `series_key`; `changeKey`; delta queries for calendar views |

### 20.3 Provider differences to handle in adapters

| Difference | Handling |
|---|---|
| Cursor model (history ID / sync token / delta link / timestamps) | Opaque `next_cursor`; adapter-specific expiry → `CursorExpired` |
| Push model (Pub/Sub / channels / Graph subscriptions / Slack events) | Webhook verifiers per provider; signals only (§11.5) |
| Editable content | `content_hash` + `SourceItemContentChanged` |
| Identity | Email first; `provider_user_id` identifiers when email is unavailable |
| Categories | Adapter maps to neutral `categories`; prefilter uses neutral values only |
| Deep links | Adapter supplies `deep_link`; core stores it opaquely |

Core tables contain no provider-specific columns; provider IDs live only in `source_items.external_id`, `external_thread_id`, `raw_metadata`.

---

## 21. Testing and reliability regression suite

| Layer | Content |
|---|---|
| Unit | Fold, authority, date resolution, prefilter, priority, reminder rules, cursor logic with fake connectors, error translation |
| Integration | Postgres (pgvector image) in Docker; migrations up/down; RLS fail-closed |
| Contract | Recorded Gmail/Calendar fixtures including 404, 410, 429, `invalid_grant`; the same suite later runs against Microsoft/Slack adapters |
| API | httpx `AsyncClient`: auth, CSRF, 409/412, `Idempotency-Key`, cursors, Problem Details |
| AI and context | `AI_EVALUATION.md`, `CONTEXT_EVALUATION.md` |

**Reliability regression tests (required before Phase 1 exit; run in CI):**

| ID | Scenario | Assertion |
|---|---|---|
| RT-01 | Crash after source transaction, before job dispatch (kill dispatcher after the sync commit) | After restart the event is dispatched; the message is normalized, extracted and applied exactly once. Built in two levels (below); green for Phase 1 exit only at pipeline level |
| RT-02 | Crash after AI extraction commit, before apply | Apply runs via job or reconciler; one `extractions` row; AI call count = 1; state equals the no-crash run |
| RT-02b | Crash after AI response, before extraction commit | At most one extra AI call; one extraction row; final state identical |
| RT-03 | Two workers apply extractions describing the same commitment (meeting + email) concurrently | One work item with two evidence rows; no deadlock |
| RT-03b | User edits `due_at` while a model `new_deadline` signal applies | User value kept; `conflict_detected` event; no lost events; versions monotonic |
| RT-04 | Same source chain replayed 3 times, in order and shuffled | Identical state hash over items, evidence, events, reminders |
| RT-05 | Outbox row dispatched twice (crash between defer and mark) | Each handler's effects occur once (`event_consumptions`) |
| RT-06 | Crash mid-sync after some pages committed | No gaps, no duplicates; cursor advanced only at the end |
| RT-07 | Duplicate sync triggers (manual + periodic + simulated webhook) | One sync run; one set of source items |
| RT-08 | Same recording uploaded twice | One recording, one transcription, one meeting extraction |
| RT-09 | Two reminder sweeps run concurrently | One reminder, one notification per channel |
| RT-10 | Account deletion job crashes midway and resumes; jobs for the user still queued | Deletion completes; queued jobs no-op; no rows remain for the user except the content-free audit record |
| RT-11 | Provider deletes a message that supports a confirmed and a suggested item | Body/chunks deleted; quotes redacted; suggested item archived; confirmed item `has_source_gap`; no dangling FK |
| RT-12 | Extract/apply code paths attempt a write to a SOURCE table (test-mode audit trigger) | Write rejected; test fails if any such write occurs |
| RT-13 | R2 rebuild after user corrections | All user-authored values and user-touched item IDs preserved |
| RT-14 | R2 rebuild equivalence | Rebuilt AI-derived state equals pre-rebuild state (excluding IDs of AI-only items) |
| RT-15 | RLS with `app.user_id` unset and set to another user; from slice 0.3 also the role model of §7.6 | Zero rows returned; writes rejected; context does not leak across pooled transactions. From 0.3: with `app.user_id` = A, `eca_app` publishes A's event through `platform.publish` (a plain insert, §7.3.3), and the worker role then sees the row. Whatever `app.user_id` is, `eca_app` fails with a privilege error (not zero rows) on SELECT, `INSERT … RETURNING`, UPDATE and DELETE on `outbox`, also when rows of other users exist. It cannot insert a row for another user or with NULL `user_id` (RLS violation). It has no access to `event_consumptions` or to any Procrastinate table or function. `eca_worker` with `app.user_id` unset sees zero business rows. The privilege matrix equals §7.6 |

**RT-01 levels.** The eventual assertion above does not change. It is built in two levels:

| Level | Slice | Setup | Proves | Does not prove |
|---|---|---|---|---|
| Infrastructure | 0.3 | Synthetic event type and synthetic handlers. Each handler appends to a test-only effect table **without** a unique constraint, so any duplicate effect is visible. Real outbox, dispatcher, Procrastinate worker, `event_consumptions`, crash points (`IMPLEMENTATION_PLAN.md` slice 0.3) | Outbox persistence atomic with the business commit (and absent after rollback); dispatch after a crash; handler registration; duplicate-delivery safety; consumption dedupe; crash and retry recovery; effective exactly-once database effects per handler | Gmail normalization, email or AI extraction, apply, the real message lifecycle, `source_items` processing, production email correctness |
| Pipeline | 1.3 (sync → normalize, fake connector) and 1.4 (normalize → extract → apply, recorded AI responses) | `world_v1` messages through the real stages | The full assertion: normalized, extracted and applied exactly once; AI call count 1 | — |

RT-05 is defined on infrastructure only and is complete at slice 0.3; it keeps running unchanged once real handlers exist. RT-02, RT-02b, RT-04, RT-06 and RT-07 cover the neighbouring pipeline crash and replay cases.

**Slice 0.3 crash-test harness.** Synthetic handlers and crash tests must not leave anything in production code paths except inert crash hooks.

| Concern | Decision |
|---|---|
| Registries | Event types and handlers are recorded in registry objects. `eca.platform` exposes one default registry, which the production decorators (`register_event`, `handles`) fill. The dispatcher and the worker take a registry as an argument. In-process tests build their own registry, so test types never reach the default one |
| Worker composition | `eca.worker` exposes a public function that builds and runs the worker from a registry and a `WorkerConfig`. The config holds queue concurrency, dispatcher tick and batch size, Procrastinate heartbeat interval and stalled timeout, the handler retry strategy, and dispatch backoff. `python -m eca.worker` builds the default registry by importing the domain modules' handler registrations explicitly (none in 0.3) and builds `WorkerConfig` from settings and §14.3/§15 defaults. The production entry point has **no** option, setting or environment variable that loads extra modules |
| Where test handlers live | `backend/tests/reliability/support/synthetic.py`: test-only event types (prefix `test.`), payload models and handlers. Each handler appends to `rt_effects`. The flaky handler, for case (f), decides from Procrastinate's job attempt number (`JobContext`), not from database state, because a failed attempt rolls back |
| Effect table | `rt_effects (id bigint generated always as identity, event_id uuid, handler text, user_id uuid NULL, seen_app_user_id text NULL, marker text NULL, created_at timestamptz DEFAULT now())`, with **no unique constraint**. `seen_app_user_id` records `current_setting('app.user_id', true)` inside the handler, which proves that the wrapper set the user context. It is created by a test fixture with the migration role in the throwaway database, never by Alembic, and only the test worker role gets SELECT and INSERT on it |
| How the subprocess loads them | Tests start `python -m tests.reliability.support.worker_main --mode all|dispatcher|jobs` (working directory `backend/`). `all` is the production composition; `dispatcher` and `jobs` run one half, so a test can order the dispatcher and the handler jobs, as RT-05 needs. `eca.worker.run_worker` takes the same `mode` argument; the production entry point always uses `all`. This test-only launcher imports `synthetic` into a registry and calls `eca.worker`'s public run function with a fast `WorkerConfig`: handler retry base 0.05 s, heartbeat 0.5 s, stalled timeout 2 s, tick 0.1 s. The production `python -m eca.worker` is never used to load test code |
| Test-only business table | `rt_business (id, user_id, note)` under per-user RLS (`user_isolation_ddl`), written by the test API role together with an event, for the commit and rollback cases of RT-01 |
| Why production cannot load them | `tests/` is not part of the installed package (`[tool.setuptools.packages.find] include = ["eca*"]`) and must not be copied into the deployment image. No production code imports `tests`, and the production entry point has no module-loading hook |
| Crash injection | Environment variable **`ECA_TEST_CRASH_POINT`** = one of `dispatch.after_claim`, `dispatch.after_defer`, `dispatch.before_commit`, `handler.after_consumption`, `handler.before_commit`, `handler.after_commit`. It is read once at process start by `eca.platform` (both entry points call the same setup). An unknown value is a startup error. A set value while `API_ENV` is production is a startup error: the process refuses to start rather than ignoring it. When armed, the first time the named point is reached the process calls `os._exit(97)` with no cleanup. Exit code 97 lets the test assert that the crash happened where intended. Tests set the variable only for the first subprocess and restart without it |
| Restart and recovery in tests | After a crash the test starts a fresh launcher process. Dispatcher crashes leave rows `pending`, which the next tick claims. Handler crashes leave the job `doing` under a dead worker. The test waits longer than the fast config's 2 s stalled timeout and restarts the launcher; the start-up stalled-job recovery pass (§14.3) re-queues the job. The test then checks `rt_effects`, `event_consumptions`, `outbox` and `procrastinate_jobs` |

---

## 22. Resolved questions

| # | Question | Resolution |
|---|---|---|
| BQ1 | Foreign keys | Kept where they protect integrity (provenance, ownership, parent–child, item references), no cascades (§17.1, §13.2) |
| BQ2 | API convention | Conventional REST with path IDs, PATCH/DELETE semantics, action sub-resources for commands (§16) |
| BQ3 | UUIDv7 generation | Application-side; works on Postgres 16+ |
| BQ4 | Reminder delivery | In-app + optional Web Push (`TECHNICAL_DESIGN.md` §15) |
| BQ5 | Separate media worker | Not required for MVP; `media` queue with concurrency 1 (§4.2) |

---

## 23. Decision summary

| Decision | Choice | Rejected | Why |
|---|---|---|---|
| Style | Modular monolith; `api` + `worker` | Microservices | One transaction for the context core; small team |
| Event delivery | Transactional outbox + dispatcher + Procrastinate + consumer dedupe | Direct enqueue; Redis broker; outbox-as-queue without Procrastinate | Atomic intent with business data; mature worker features (retries, locks, periodic, stalled-job recovery) without new infrastructure |
| AI stages | `extract` (AI, persisted) and `apply` (deterministic) | Combined | Failure isolation; retries without AI cost |
| Idempotency | Keys on every side effect and external trigger | Extraction-only keys | Duplicate tasks destroy trust |
| Sync | Cursor after durable store; resumable import; etags; webhooks as signals | Time-window polling; webhook payload as data | No reprocessing; crash-safe |
| Concurrency | Event-first + row locks + per-user merge lock + versions; user authority | Global locks; SERIALIZABLE; last-write-wins | Correct and parallel across users |
| Source of truth | Four classes, single writer, rebuild levels | Mutable AI-written rows | Inference never silently authoritative |
| Deletion | Logical where useful; permanent where required; ordered jobs; no cascades | Soft delete only; cascades | Privacy and explicit order |
| API | REST, path IDs, PATCH/DELETE, action sub-resources, Problem Details, keyset cursors, Idempotency-Key, If-Match/base_version | POST-only RPC with query IDs | Conventional, tool-friendly, explicit concurrency |
| Errors | Domain errors in services; HTTP mapping only in API | HTTPException in services | Workers share services |
| Caching | Minimal, version-keyed | Redis; broad caches | Not required at MVP scale |

---

## References (consulted 2026-10-02)

- Gmail API usage limits — https://developers.google.com/workspace/gmail/api/reference/quota
- Gmail sync — https://developers.google.com/workspace/gmail/api/guides/sync
- Gmail push notifications — https://developers.google.com/workspace/gmail/api/guides/push
- Calendar API quotas — https://developers.google.com/workspace/calendar/api/guides/quota
- Calendar sync — https://developers.google.com/workspace/calendar/api/guides/sync
- Procrastinate documentation — https://procrastinate.readthedocs.io/en/stable/
- RFC 9110 (HTTP Semantics: conditional requests), RFC 9457 (Problem Details for HTTP APIs)
