# Implementation Plan — Executive Context Assistant

**Status:** Phase 0 in progress — slices 0.1 and 0.2 implemented; their CI lint and test jobs pass, but the CI run is red because the `secrets` job fails before scanning (§0); slice 0.3 implemented, local verification in progress (§0: 100-run criterion not yet complete). Phase 1: code written, untested (§0.5). Phase 2: code complete, tests deferred (§0.6) — not complete until its tests and exit criteria pass
**Date:** 2026-10-02
**Authority:** Execution order, slices, deliverables and exit criteria. Architecture is defined in `TECHNICAL_DESIGN.md` and the documents in its §5.1; this plan must not introduce behaviour or architecture that those documents do not describe (`CLAUDE.md`: "If implementation requires changing product behavior, stop and update the appropriate document first").

---

## 0. Implementation status

Last updated 2026-10-02 (Phase 1 slices 1.1–1.9 code written and untested, §0.5; slice 0.3 locally verified; slices 0.4 and 0.5 implemented, local results in §0.4; CI run 2: lint and test pass, overall red because of the `secrets` job).

| Slice | Code | Verification |
|---|---|---|
| 0.1 Repository hygiene | Implemented (commit `5b71b09`) | `.env` git-ignored and untracked (unit tests pass, locally and in CI). gitleaks pre-commit hook over all files: passed locally. **Git-history scan (CI `secrets` job): not performed.** The job fails in its install step before scanning (§0.1). Gemini key rotation: not verifiable from the repository (owner action) |
| 0.2 Backend skeleton | Implemented (commit `5b71b09`) | **RT-15 passes** on PostgreSQL in CI. **"CI green" is not met**: the lint and test jobs pass, but the overall run is red because of the `secrets` job (§0.1) |
| 0.3 Reliability core | Implemented (commit `c5d7c0e`, "Slice 0.3: reliable event infrastructure") | **Locally verified**: exit criteria 1–4 met locally (§0.3), including 100 consecutive RT-01 + RT-05 runs. **Not "complete"**: CI has not run on this commit, and the `secrets` job bug (§0.1) keeps any run red |
| 0.4 AI provider layer | Implemented (commit "Slice 0.4: AI provider layer") | Local gates pass (§0.4). **Exit not met**: the live smoke test has not run (no `GEMINI_API_KEY` in the build environment; it needs one run with a restricted key provided as an environment secret). Cassette replay determinism: met (tested) |
| 0.5 Evaluation harness | Implemented (commit `95c06e4`) | Local gates pass (§0.4). Runner end to end on stubs: met. **Freeze of `golden-v0.1`: not done**: labels are draft and need the labelling owner's review (Q12); a candidate manifest (`frozen: false`) is committed |

### 0.5 Phase 1 code status (2026-10-02) — code written, NOT tested

At the owner's instruction ("code only, no tests"), slices 1.1–1.9 were written without running any test. Static checks were run and pass: ruff, ruff format, mypy strict (140 source files), lint-imports (10 contracts kept); the web app passes `tsc --noEmit`. **No pytest run covers migrations 0008–0010 or any slice 1.1, 1.2 or 1.4–1.9 code; no slice below is "locally verified".** Test files were only edited (migration head `0010`, per-revision table sets), not run.

| Slice | Code on `claude/practical-allen-tqqjse` | Tests | Not done |
|---|---|---|---|
| 1.1 Identity and sessions | Google OIDC (PKCE, `state`, `nonce`, JWKS-verified ID token), server-side sessions, CSRF double submit, `audit_log`, `DELETE /api/v1/me` (recent re-auth, deletion request recorded), `users`, self Person, `fk_*_user` (0006), worker users policy, sign-in/session definer lookups (0009) | Not written / not run | Auth-flow tests with mocked Google, CSRF rejection, session expiry |
| 1.2 Connections | Connect with incremental scopes, envelope-encrypted refresh tokens (AES-256-GCM, KEK from env), `granted_scopes` and capability gating, disconnect with revoke, `needs_reauth` on `invalid_grant`, routes | Not written / not run | Token-never-logged and denied-scope tests; source purge on disconnect (1.9) |
| 1.3 Ingestion core | As committed in `695e282` | Written; individual files passed during development; full suite not run | RT tests not run as a full suite |
| 1.4 Extract and apply | Migration 0008, AI-01 v1 prompt/schema, extraction lifecycle, apply (grounding, dates, mapping, confidence, matching, evidence, events, fold, triage), AI-02 behind the disabled role, R1/R2 | Not written / not run | RT-01 complete, RT-02/02b/03/03b/12/13/14, E1–E4, E13, E14, DIR-120, CC chains at L2; placeholder cassettes; baseline; X1/X2 runners |
| 1.5 Gmail adapter | `history.list` incremental, profile `historyId` before import, 30-day import with page tokens, 404 → bounded re-sync, label → neutral categories, 4,500 units/min bucket | Not written / not run | Contract tests with recorded fixtures (404, 429, `invalid_grant`) |
| 1.6 Calendar adapter | Migration 0010 (`meetings`, `meeting_participants`); `events.list` with `singleEvents`, window −30/+60 days, cursor `query_fingerprint|syncToken`, etag skip, 410 → bounded re-sync, daily roll-forward; `meetings` module (upsert, participants diff, cancellations, `MeetingChanged`) | Not written / not run | Contract tests |
| 1.7 Work API | Routes for work items (keyset signed cursors, `ETag`, `If-Match` → 412, `base_version` → field-level 409, `Idempotency-Key` on create, commands confirm/reject/complete/reopen/cancel/start, merge, delete of user items), decisions, evidence, conversations (needs-response, detail, mark-handled, `priority_override`), people (list, context, patch, merge, aliases), organizations, meetings, sources; `feedback_events` on every correction; `idempotency_keys`; OpenAPI exported to `web/openapi.json` and TypeScript types generated (`web/lib/schema.d.ts`) | Not written / not run | API tests (412/409/422, cursors, idempotent replay, RLS 404s) |
| 1.8 Priority and Today | `attention`: features and score per TECHNICAL_DESIGN §12.6 with **initial, unfitted** weights in `config/priority.yaml`; writes through `work.set_item_priority` / `communication.set_conversation_priority` with version checks; handlers on `WorkItemChanged`, `MessageNormalized`, `MeetingChanged`; 15-minute sweep; `GET /api/v1/today`; minimal Next.js `web/` (Today and Tasks pages, confirm/reject/edit/done, provenance labels, evidence links) | Not written / not run | Priority unit tests, weight fitting on labelled pairs (AI_EVALUATION §4.5), UI tests |
| 1.9 Deletion and retention | Provider deletion (`SourceItemDeleted`: bodies purged, evidence quotes redacted, items archived or `has_source_gap`, meetings cancelled); source purge on disconnect (`?purge=true`, batched, tombstones where redacted evidence of user-touched items remains); account deletion job (ordered, resumable, progress in `deletion_jobs`; tokens revoked; `ai_calls.user_id` → NULL; roll-ups folded into NULL-user rows; event consumptions and outbox; the user's audit rows replaced by one content-free record; user row deleted under `users_worker_delete`); user gate (handlers skip events of a `deleting` user; API 410); nightly retention (bodies 30/180 days, `ai_calls` 90 days, `audit_log` 1 year, dispatched outbox 7 days, expired idempotency keys; expired `oauth_states` removed on creation); `GET /api/v1/data-summary` | Not written / not run | RT-10, RT-11 |

Exit criteria of slices 1.1–1.9 are **not met**: each requires tests that have not been run. The Phase 1 exit (real Gmail test account, RT suite, `golden-v1.0`, E1–E4) is not met.

### 0.6 Phase 2 code status — code complete, tests deferred

At the owner's instruction ("Phase 2 — coding only; testing is deferred to a later task"), slices 2.1–2.5 are written in the order 2.1 → 2.2 → 2.3 → 2.5 → 2.4 without writing or running any test. Static checks (ruff, ruff format, mypy strict, lint-imports) run through the pre-commit hooks on every commit. No live Gemini call is made; experiments X6 and X7 are not run. Phase 2 is **not complete** until the deferred tests below and the Phase 2 exit criteria (§4) pass.

Phase 2 builds on the Phase 1 code of §0.5, which is itself untested. Phase 2 code that depends on Phase 1 behaviour (apply, normalization, sessions, CSRF, the worker) inherits that risk.

**Deferred tests** (from the Tests column of §4; none written or run in this task):

| Slice | Deferred tests |
|---|---|
| 2.1 Indexing | Index idempotency (re-index of unchanged content makes no AI call and writes no new rows; replay and duplicate events converge); mention accuracy (alias-scan and extraction mentions against labelled mentions) |
| 2.2 Retrieval | L3 suites (`CONTEXT_EVALUATION.md` §3, §8.2: chain recall, Recall@K, NDCG@10, context precision, useful-token ratio, anchor and temporal-window accuracy); E6 (`AI_EVALUATION.md` §4.6) |
| 2.3 Change feed and day view | CC-23, CC-24, CC-25, CC-26 |
| 2.4 Chat | E7, E8, E10, E15, E16; L4/L5; SS session scripts |
| 2.5 Projects | CC-21, CC-22 |
| Phase 2 exit | G3/G4 thresholds on S1–S3 and S6–S12 (`CONTEXT_EVALUATION.md` §14); retrieval-method ablation R1–R5 run once and recorded (§10) |

Also deferred: unit tests of the pure functions added in Phase 2 (chunking, token estimate, alias scan, continuation scoring, temporal resolver, RRF and §9.2 ranking, packet layout and budgets, coverage rendering, net-change fold, planner rules, grounding checks), integration tests of the new migrations (0011–0017: up/down/up, privilege matrix, RLS fail-closed on every new table, RT-15 extension), API tests of the new routes (SSE events, `Idempotency-Key` replay, rate limit, 404 on other users' IDs) and the deletion paths that now include chunks, traces, links, checkpoints, projects and chat (RT-10, RT-11 extensions).

**Code status per slice** (commits on `main`; no test written or run; static checks pass on every commit):

| Slice | Code | Migrations |
|---|---|---|
| 2.1 Indexing | Chunking per §9.10, natural-key index handlers (`MessageNormalized`, `MeetingChanged`) with an advisory lock per source item, idempotent chunk upserts, AI-04 only for changed chunks (cap 4 calls, FTS-only at the hard budget cap), removal on `SourceItemDeleted`, alias scan into `entity_mentions`, `continues` links in `entity_links`, operator `eca ops reembed` | 0011 `chunks`, 0012 `entity_links` |
| 2.2 Retrieval | Typed retrievers S1, S2, S6–S9, S12 (+ overdue, deadlines, who-is-waiting-on-me), one-statement hybrid search with RRF, scope before ranking, ranking, cards, packet layout and budgets, coverage block, content-free traces | 0013 `retrieval_traces` |
| 2.3 Change feed and day view | Net-change fold, S10 and S11 retrievers, `GET /changes`, `PUT /checkpoints/{surface}`, `GET /days/{date}`, hourly `time_sweep` | 0014 `user_checkpoints` |
| 2.5 Projects | Projects module (suggestions, confirm/reject/edit/assign), S3 project context and topic mode, project alias scan and change-feed scope, project and topic routes | 0015 `projects`, `project_members`, `work_items.project_id`; 0016 concurrent indexes |
| 2.4 Chat | Planner (rules, AI-05), deterministic list answers, AI-06/AI-07 with claim kinds, grounding checks, abstention and degradation, per-user budget caps, session context (4 turns + focus map), SSE route with `Idempotency-Key` and rate limit, minimal web chat page (`web/app/chat`) | 0017 `chat_sessions`, `chat_messages` |

**CI run 3** — [37050602378](https://github.com/thoshibabuls/executive-context-assistant/actions/runs/37050602378) (push to `main`, commit `6eb2d35`: Phase 1 and Phase 2 code, first CI run of either): `lint` passed (ruff, ruff format, mypy strict, lint-imports 13 contracts, gate A0); `test` failed with 16 failed, 312 passed, 1 deselected; `secrets` failed before scanning (the workflow bug of §0.1). The 16 failures trace to Phase 1 code or to tests that Phase 1 left stale: 10 raise `AttributeError: deleted_at` because the `conversations` table mirror in `eca/communication/models.py` lacked the `deleted_at` column that migration 0007 creates (hit by `communication.priority_inputs` in the attention handler); 4 RT-01 pipeline crash tests see the worker subprocess exit with code 1 instead of 97 (cause not confirmed from the log; the production composition builds the AI client, which needs `API_AI_MODE=replay` or a key); `test_privilege_matrix_after_upgrade_and_after_down_up` and `test_registries_are_isolated_from_the_default_registry` assert Batch A-era expectations (table list, no production handlers). Phase 2's own read queries (`communication/read.py`) also use `conversations.deleted_at`, so the mirror column was added in a follow-up commit (required by Phase 2; no schema change). The other failures are left for the deferred-test task and are not hidden.

Existing test files were edited only to move the migration head constants and per-revision table sets to `0017`; they were not run. `tests/integration/test_privileges.py` still lists only the Batch A business tables (stale since Phase 1, which added `extractions`, `work_items` and others); it is expected to fail until it is updated with the deferred tests.

**Known gaps left in the code (to resolve with the deferred tests):**
- Phase 1, on which Phase 2 builds, has never run in CI or under pytest (§0.5).
- `TranscriptStored` indexing and transcript chunking wait for slice 4.2; S4/S5 stay in Phase 4.
- Item and decision embeddings are not built; their discovery is full-text plus the evidence pivot.
- Decisions, conversations and meetings get no `context_events` from the Phase 1 writers; the change feed and day view read decisions, new awaiting replies and meetings from their own tables.
- The extra retrieval round of §6.4 (`missing_info`) is not built; only the AI-06 → AI-07 escalation is.
- `since_last_meeting` from AI-05 is treated as "recently" until meeting anchors exist (Phase 4).
- A chat turn that fails keeps the stored question; the key is released, so a retry with the same key re-runs and stores the question again. A process crash mid-turn leaves the key "in progress" until it expires (24 h); the client then retries with a new key.
- Project suggestions group hints by exact normalized key (no fuzzy clustering).
- Budget checks read 15-minute roll-ups, so a few calls can pass after a cap is crossed (`AI_COST_MODEL.md` §7.1).
- The web chat page is minimal: one session, no meeting-scoped sessions, no feedback buttons.

**Experiments deferred:** X6 (chat routing per intent) and X7 (is the planner needed?) are not run in this task. The code implements the default routes of `AI_PIPELINE.md` §4 and §8.2; X6 and X7 decide them on the frozen dataset later.

**Phase 2 decisions recorded in the authoritative documents before coding:** module dependencies and the import-linter contracts (`BACKEND_DESIGN.md` §5.2), RLS and roles of every new table (§7.6), Phase 2 schema and migrations (§17.5), jobs (§15), API details (§16.7); chunking, search, ranking, coverage, scope and packet parameters (`CONTEXT_ARCHITECTURE.md` §9.10); thread continuation and time-sweep parameters (§12.7); session context (§13); AI-04/05/06/07 prompts, schemas and grounding details (`AI_PIPELINE.md` §5.8, §11); Phase 2 budget guardrails (`AI_COST_MODEL.md` §7.1).

### 0.1 CI evidence (GitHub Actions)

The remote `github.com/thoshibabuls/executive-context-assistant` exists. Workflow `ci`, run [37015417109](https://github.com/thoshibabuls/executive-context-assistant/actions/runs/37015417109) (run 1, push to `main`, commit `1f6b0c0`, 2026-10-02):

| Job | Result | Detail |
|---|---|---|
| `lint` | **Passed** | ruff, ruff format, mypy (strict), lint-imports (3 contracts kept) |
| `test` | **Passed** | `pytest`: **45 passed, 0 skipped** against the `pgvector/pgvector:pg17` service (PostgreSQL 17.11). Includes the 4 PostgreSQL-backed migration tests and the 7 RT-15 tests. With `CI=true`, a missing database is a failure, not a skip |
| `secrets` | **Failed before scanning** | The install step saves the release archive as `gitleaks.tar.gz`, but `sha256sum -c` checks the original asset name `gitleaks_8.30.1_linux_x64.tar.gz` ("No such file or directory"). This is a deterministic workflow bug, not a finding and not a flake. The history scan never ran |
| **Overall run** | **Red (failure)** | Because of `secrets` |

**Run 2** — [37032838300](https://github.com/thoshibabuls/executive-context-assistant/actions/runs/37032838300) (push to `main`, commit `d386adc`, slices 0.3–0.5, 2026-10-02):

| Job | Result | Detail |
|---|---|---|
| `lint` | **Passed** | ruff, ruff format, mypy (strict, `eca` + `eca_evals`), lint-imports (5 contracts kept), gate A0 step (`python -m eca_evals gate-a0`: PASS) |
| `test` | **Passed** | `pytest`: **292 passed, 0 skipped, 1 deselected** (the `live` smoke test) in 75 s against `pgvector/pgvector:pg17`; same count as locally. Includes the slice 0.3 RT-01/RT-05 tests (once each, not the 100-run loop), the 0.4 telemetry tests and the 0.5 harness tests |
| `secrets` | **Failed before scanning** | Same workflow bug as run 1 (`sha256sum: gitleaks_8.30.1_linux_x64.tar.gz: No such file or directory`). The history scan never ran |
| **Overall run** | **Red (failure)** | Because of `secrets` only |

The workflow runs on pushes to `main` and on pull requests only. Fixing the `secrets` job is a workflow change outside the slice 0.3 code. It is required before any slice can be marked "complete" (§1.1).

### 0.3 Slice 0.3 local results (2026-10-02, Linux cloud session)

Environment: Python 3.11; PostgreSQL 17.11 with pgvector 0.8.7 in Docker (`pgvector/pgvector:pg17`, the CI image), `ECA_TEST_DATABASE_URL` set, `CI=true`.

| Exit criterion | Result |
|---|---|
| 1. ruff, ruff format, mypy (strict, 43 files), lint-imports (`eca.worker` as composition module, 3 contracts) | Pass |
| 2. Full pytest with zero skipped | **165 passed, 0 skipped** (96 PostgreSQL tests, 69 without a database) |
| 3. RT-01 (infrastructure level, 9 tests) + RT-05 (5 tests), 100 consecutive runs, no reruns | **Met: 100 of 100 consecutive runs passed** (14 passed in every run, no failure, no rerun; run time mean 47.0 s, min 43.7 s, max 54.3 s; 78 minutes in total; loop stops at the first failure). Command: `pytest -q -x tests/reliability/test_rt01_infrastructure.py tests/reliability/test_rt05_duplicate_dispatch.py`, repeated. An earlier loop stopped at run 1 on a test defect: case (d) counted the recovery of a periodic `eca.reconcile` job that was running at the crash as a second handler recovery. The test now counts handler jobs only, and the loop was restarted from zero |
| 4. No domain schema; no FK on `outbox.user_id`; migrations create no roles | Met (tested: `test_migrations_0003.py`, `test_privileges.py`) |
| 5. This section updated with measured results | Done |

CI: not run on this commit. Pushes to a non-`main` branch do not trigger the workflow, and the `secrets` job bug (§0.1) still keeps any run red. Slice 0.3 is "locally verified"; it becomes "complete" only with a green CI run.

### 0.4 Slices 0.4 and 0.5 local results (2026-10-02, Linux cloud session)

Same environment as §0.3 (`CI=true`, so a skipped database test is a failure).

| Gate | Result |
|---|---|
| ruff check, ruff format --check | Pass (116 files) |
| mypy strict (`eca`, `eca_evals`) | Pass (77 source files) |
| lint-imports | 5 contracts kept: module boundaries (now also applied to `eca_evals` as a client package), connectors, platform base layer, **only `eca.intelligence` imports `google.genai`**, `eca` never imports `eca_evals`. Both new contracts were shown to break on a probe import |
| pre-commit (all files, incl. gitleaks) | Pass |
| pytest (full suite) | **292 passed, 0 skipped, 1 deselected** (the `live` smoke test, deselected by default); 109 PostgreSQL tests. New: 34 provider tests (registry, pricing, cassettes, attempt policy, client), 26 Gemini-wrapper and provenance tests, 5 client-configuration tests, 13 PostgreSQL telemetry tests (meter per role, RLS and grants, own-transaction meter, roll-up grouping, idempotency and window, unique bucket with NULL user, API reads its own roll-ups only, periodic task registration), 47 evaluation-harness tests, 2 contract tests; migration chain and privilege matrix extended to `0005` (up/down/up) |
| Live smoke test (`pytest -m live tests/live`) | **Not run**: skipped, no `GEMINI_API_KEY` in this environment. `config/models.yaml` stays `verified: false` |
| Gate A0 (`python -m eca_evals gate-a0`) | Pass: manifest, dataset, splits and contamination pass; cassette suites and E14 are n/a until slice 1.4 |
| Stub runs (10,000 resamples) | AI suite (test + challenge, 72 cases): the candidate stub fixes the baseline's forwarded-promise attribution (safety 4 → 0); statement precision +0.108 (CI +0.024 to +0.222), recall +0.167 (CI +0.067 to +0.273); decision `incomplete` (no human review). Context L2 (10 chains, 13 checkpoints): oracle stub state accuracy 1.000, naive stub 0.000; decision `incomplete`. These runs validate the harness, not product quality |

Dataset: `world_v1` email slice, 150 emails, 136 threads, 85 statements; splits 55 dev / 65 test / 23 sealed / 7 challenge (thread level); chains 4 dev / 5 test / 1 sealed. Labels are draft (template-generated, no human review). CI: run 2 (§0.1), lint and test passed; overall red because of the `secrets` job.

### 0.2 Local environments (history; CI above is the reference evidence)

- **Windows 11 (audit before 0.3):** Python 3.11.5; no Docker, no PostgreSQL. `pytest` gave 34 passed, 11 skipped (all `db`-marked); ruff, format, mypy (29 files) and lint-imports pass. The project owner reports that RT-15 passed earlier against a local PostgreSQL. No log of that run is stored.
- **Linux cloud session (2026-10-02):** Python 3.11; the same 34 passed and 11 skipped without a database, and ruff, format, mypy and lint-imports pass. The distribution's pgvector package is 0.6.0, which migration `0001` correctly rejects (≥ 0.8 is required), so the database tests could not run there.
- Commit `5b71b09` records two defects found while running against a database: the readiness probe hung 130 s on an unreachable database (now bounded to 3 s), and uvicorn forced a Proactor loop on Windows (selector loop factory added).

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
| PostgreSQL 16+ with pgvector ≥ 0.8 reachable from the development machine (Docker Desktop with `docker compose up -d db`, or a native install) and `ECA_TEST_DATABASE_URL` set | Local verification of slice 0.3 |
| GitHub remote with Actions enabled | Exists (§0.1) |
| CI `secrets` job fixed (the archive name must match the checksum entry) | "CI green" for slice 0.2 and "complete" for slice 0.3 |
| Runtime roles `eca_app` and `eca_worker` provisioned outside migrations (`BACKEND_DESIGN.md` §7.6, role lifecycle) | Running migrations `0002`+ locally (re-run `docker/postgres/init/01-roles.sql` on an existing volume) and in any hosted environment |

---

## 2. Phase 0 — Foundation

### Slice 0.1 Repository hygiene

- `.gitignore` (`.env`, `.venv/`, `node_modules/`, `__pycache__/`, build output, local DB files) **before** `git init`; `.env.example` with variable names only.
- Rotate the Gemini key currently stored in `.env` if it was ever shared; never commit it.
- Pre-commit: ruff (lint + format), mypy (strict on `eca`), gitleaks secret scan.
- **Exit:** repository initialised; secret scan clean.
- **Status:** implemented. The pre-commit scan is clean. The git-history scan has not run yet, because the CI `secrets` job fails before scanning (§0.1).

### Slice 0.2 Backend skeleton

- Package layout per `TECHNICAL_DESIGN.md` §5.4 and module map (`BACKEND_DESIGN.md` §5); `import-linter` contracts (§5.3).
- Settings (pydantic-settings) matching existing `.env` names (`API_*`, `GEMINI_API_KEY`, `NEXT_PUBLIC_API_URL`).
- FastAPI app, `/healthz`, `/readyz`; domain errors and Problem Details translation (`BACKEND_DESIGN.md` §14); request IDs; structlog; Sentry hook (disabled locally).
- SQLAlchemy async with psycopg 3; unit of work with `SET LOCAL app.user_id`; Alembic baseline; Docker Compose with `pgvector/pgvector` Postgres 16+; RLS helper and fail-closed test (RT-15).
- CI: lint, types, unit + integration tests against Postgres container.
- **Exit:** CI green; RT-15 passes.
- **Status:** code complete. RT-15 passes in CI (§0.1). The exit is not yet met only because the CI run is red: the `secrets` job fails before scanning. The exit is met when the same jobs pass with a fixed `secrets` job.

### Slice 0.3 Reliability core

Authoritative design: `BACKEND_DESIGN.md` §7 (outbox, dispatch, reconciler, access model), §14.3 (retries), §15 (queues), §21 (RT-01 levels, RT-05, RT-15). This slice builds delivery infrastructure only. It creates no domain table, no `users`, no `source_items`.

**Database (migrations `0002`–`0004`, platform; `BACKEND_DESIGN.md` §7.7)**

Three revisions with one concern each, in a linear chain. Each creates its objects together with their grants and RLS in one transaction:

- **`0002_runtime_role_access`**:
  - Checks that both runtime roles exist, have none of SUPERUSER, BYPASSRLS, CREATEROLE or CREATEDB, are distinct, are not the migration role, and are not members of each other, of the migration role or of a privileged role; it fails loudly otherwise (`BACKEND_DESIGN.md` §7.6).
  - Gives the worker role USAGE on `public`, EXECUTE on `eca_current_user_id()`, SELECT on `alembic_version`, and default privileges (SELECT, INSERT, UPDATE, DELETE on tables; USAGE, SELECT on sequences).
  - Revokes the API role's INSERT, UPDATE and DELETE on `alembic_version`. Those privileges were granted by `0001`'s `ON ALL TABLES`, contrary to `BACKEND_DESIGN.md` §7.6.
  - Creates no tables and no roles.
- **`0003_outbox`**:
  - `outbox` and `event_consumptions` exactly as `BACKEND_DESIGN.md` §7.3, with `ix_outbox_pending (next_attempt_at) WHERE status = 'pending'` and `ix_outbox_user (user_id) WHERE user_id IS NOT NULL`. `event_consumptions` has PK `(event_id, handler)` and FK `event_id → outbox(id)`.
  - Grant trims: `REVOKE SELECT, UPDATE, DELETE ON outbox FROM <api>`; `REVOKE ALL ON event_consumptions FROM <api>`; `REVOKE UPDATE ON event_consumptions FROM <worker>`.
  - ENABLE and FORCE RLS, with the policies `outbox_api_insert`, `outbox_worker_all` and `event_consumptions_worker_all`.
  - `outbox.user_id` has **no FK yet**. Slice 1.1 adds `fk_outbox_user` in the migration that creates `users` (`BACKEND_DESIGN.md` §7.3.1).
- **`0004_procrastinate_schema`**:
  - The schema of `procrastinate==3.10.0`, executed from the vendored file `backend/migrations/sql/procrastinate_3.10.0_schema.sql`. It is never applied by `procrastinate schema --apply`, and never read from the installed package at migration time.
  - `REVOKE ALL` on Procrastinate tables and sequences `FROM <api>`; `REVOKE EXECUTE` on each `procrastinate_*` function `FROM PUBLIC`; `GRANT EXECUTE` on them `TO <worker>`.
  - A later Procrastinate upgrade is a new revision that applies Procrastinate's own migration files (`BACKEND_DESIGN.md` §7.7).

The resulting exact privilege state, and what the matrix test asserts, are in `BACKEND_DESIGN.md` §7.6.

**Roles and settings**

- Migrations never create or alter roles. The roles are provisioned outside them (`BACKEND_DESIGN.md` §7.6, role lifecycle):
  - **Local:** `docker/postgres/init/01-roles.sql` creates both `eca_app` and `eca_worker`. Re-run it on a volume created before 0.3.
  - **Tests and CI:** `tests/conftest.py` creates `eca_test_app` and `eca_test_worker` and passes both names to Alembic.
  - **Hosted:** provisioning outside the application, with the hosting decision (Q1).
- Settings `API_WORKER_DATABASE_URL` and `API_DB_WORKER_ROLE` (default `eca_worker`); `migrations/env.py` accepts `-x worker_role=…` like `runtime_role`; `.env.example` lists both. `API_DB_RUNTIME_ROLE` keeps naming the API role.
- `pyproject.toml` pins `procrastinate==3.10.0` exactly.

**Event infrastructure (`eca.platform`)**

- Envelope: `id` (UUIDv7 generated by `publish`), `event_type`, `user_id` (copied from the active unit of work, never passed by the caller; NULL in a unit of work without a user), `aggregate_type`, `aggregate_id`, `payload` (validated by the Pydantic model registered for the type; IDs and small facts only), `correlation` (request ID from logging context), `created_at`.
- Event identity is `outbox.id`. It is the key in `event_consumptions` and in job lock keys.
- `register_event(type, payload_model)`. `publish` rejects an unregistered type at write time. `publish(uow, event)` inserts into `outbox` in the caller's transaction and does nothing else.
- The insert works for the INSERT-only API role (`BACKEND_DESIGN.md` §7.3.3). It is one SQLAlchemy Core `insert(outbox).values(...)` with an explicit column list: no ORM `session.add`, no RETURNING, no `ON CONFLICT`. The application supplies `id`, `user_id`, `event_type`, `aggregate_type`, `aggregate_id`, `payload` and `correlation`. The DDL defaults supply `status`, `attempts`, `next_attempt_at` and `created_at`, which are never read back. `publish` returns the pre-generated `id`.
- Registries are objects. `eca.platform` exposes a default registry filled by the production decorators; the dispatcher and worker take a registry as an argument (`BACKEND_DESIGN.md` §21, crash-test harness).
- Handler registration: decorator `handles(event_type, name=…, queue=…)`. Names are unique and stable (registry rejects duplicates). Domain modules register handlers; the `eca.worker` composition package imports them. `platform` imports no domain module.
- Handler wrapper (transactional mode): one `eca_worker` unit of work per job, with `app.user_id` = the event's `user_id` (unset for system events). First statement: `INSERT INTO event_consumptions … ON CONFLICT DO NOTHING RETURNING event_id`. No row → return without effects. Then the handler body, then commit. Any exception → rollback → Procrastinate retry through the custom `HandlerRetryStrategy` (`BACKEND_DESIGN.md` §14.3). The built-in `RetryStrategy` of Procrastinate 3.10.0 cannot express the policy. Run n = `job.attempts + 1`. After a failed run n < 8 the job is retried after `min(5 s · 2^(n−1) + random(0–1 s), 10 min)`. After run 8, or after a non-retryable error (`ValidationFailed`, `AuthRevoked`, `Gone`), the strategy returns no retry, Procrastinate marks the job `failed` (a dead job), and the strategy logs `handler_job_dead`. Handler attempts are separate from `outbox.attempts`, which counts dispatch failures only. The natural-key mode for handlers that call external services before their transaction is added in slice 1.4, when the first such handler exists.

**Dispatcher (`eca.platform`, run by the worker)**

- Loop: tick every 1 s when idle; loop again at once when a batch was full (batch size configurable, default 100).
- Claim: `SELECT … FROM outbox WHERE status = 'pending' AND next_attempt_at <= now() ORDER BY next_attempt_at, id LIMIT :n FOR UPDATE SKIP LOCKED`, in one `eca_worker` transaction with `app.user_id` unset.
- Per row: the dispatch step of `BACKEND_DESIGN.md` §7.3. Lifecycle `pending → dispatched`, or `pending → failed` at the 10th failed attempt. After failure n the backoff is `min(1 s · 2^(n−1) + random(0–1 s), 5 min)`. Unregistered event type = error. Zero handlers = dispatched.
- Job keys: `queueing_lock = lock = "<handler>:<event_id>"`. `AlreadyEnqueued` = success. Job arguments carry the envelope, which holds no content.
- Stale dispatch: no persisted intermediate state. A crash releases row locks and leaves rows `pending`; the next tick or the reconciler picks them up.
- Duplicate dispatch: absorbed by `queueing_lock` while the first job is queued, by `lock` while it runs, and by `event_consumptions` after it committed (`BACKEND_DESIGN.md` §7.4).

**Worker (`eca.worker`, new composition package)**

- `python -m eca.worker` starts:
  - one Procrastinate worker per queue with the `BACKEND_DESIGN.md` §15 concurrency (`events` 8, `sync` 4, `ingest` 8, `extract` 8, `apply` 8, `embed` 2, `ai_standard` 4, `media` 1, `schedule` 1);
  - the dispatcher loop;
  - the tasks on `schedule`: `reconcile` (every 5 min) and `recover_stalled_jobs` (once at start, then every minute). Recovery uses Procrastinate's worker heartbeats (10 s interval, 30 s stalled timeout) to find jobs of dead workers, re-queues them with `retry_job` (which counts as an attempt), and prunes the stalled workers (`BACKEND_DESIGN.md` §14.3).
- In this slice no domain handlers exist. The production worker registers only infrastructure tasks. Test handlers are loaded only by the test launcher (below).
- `eca.worker` exposes a public function that runs the worker from a registry and a `WorkerConfig` (queue concurrency, dispatcher tick and batch size, heartbeat interval, stalled timeout, handler retry strategy, dispatch backoff). `python -m eca.worker` builds both from code and settings and has no option, setting or environment variable that loads extra modules.
- One driver: Procrastinate's psycopg 3 connector and the SQLAlchemy engine both connect as `eca_worker`. On Windows the worker uses the selector event loop (`BACKEND_DESIGN.md` §2.2).
- Transaction boundaries: the business transaction (with its outbox row) commits before any job exists. Each dispatcher batch is one transaction. Procrastinate writes jobs through its own connection. Each handler job is one transaction that includes its consumption record.
- Graceful stop: stop claiming and fetching, then finish in-flight jobs up to a timeout. Unfinished jobs are recovered by stalled-job recovery.
- `eca.worker` is added to `composition_modules` in the import-linter configuration.
- At start the worker checks `current_user` and refuses to run unless it is connected as the worker role (never the API role).
- Operator command `eca ops outbox retry [--event-id …]` (console script `eca` → `eca.worker.cli`) (`failed` → `pending`, `attempts = 0`, runs as `eca_worker`). It is the only `eca ops` subcommand in this slice, because `failed` must be recoverable.

**Reconciler (infrastructure only, `BACKEND_DESIGN.md` §7.5)**

- Periodic `reconcile` task: one dispatch pass over `pending` rows older than 1 minute and past `next_attempt_at` (same dispatch step, `SKIP LOCKED`, safe beside a live dispatcher). It logs the oldest pending age and the `failed` count.
- It does not re-queue `failed` rows, does not touch dead jobs, and does not read `source_items`, extractions or any domain table. Source-item reconciliation arrives in slice 1.3.

**Reliability utilities and tests**

- Crash points: named hooks in dispatcher and handler code (`dispatch.after_claim`, `dispatch.after_defer`, `dispatch.before_commit`, `handler.after_consumption`, `handler.before_commit`, `handler.after_commit`).
  - They are no-ops unless the environment variable **`ECA_TEST_CRASH_POINT`** names one of them.
  - The variable is read once at process start. An unknown name is a startup error. A set variable while `API_ENV` is production is a startup error: the process refuses to start.
  - When armed, the first time the point is reached the process calls `os._exit(97)` with no cleanup.
  - Tests run the worker or dispatcher in a subprocess with the variable set, check exit code 97, restart without the variable and check the database (`BACKEND_DESIGN.md` §21, crash-test harness).
- Synthetic test fixtures, all under `backend/tests/reliability/support/`, never in `eca`:
  - `synthetic.py` holds test-only event types (prefix `test.`) and handlers. Each handler appends a row to `rt_effects`, which has **no** unique constraint, so a duplicate effect is visible instead of hidden by a key. The table also records `current_setting('app.user_id')` inside the handler. A fixture creates it in the throwaway database and grants it to the test worker role; Alembic never creates it.
  - The flaky handler for case (f) decides from Procrastinate's job attempt number, not from database state.
  - `worker_main.py` is the test launcher: `python -m tests.reliability.support.worker_main --mode all|dispatcher|jobs`. It registers the synthetic handlers in a registry and calls `eca.worker`'s public run function with a fast `WorkerConfig` (retry base 0.05 s, heartbeat 0.5 s, stalled timeout 2 s, tick 0.1 s).
  - `tests/` is not part of the installed package, and nothing in `eca` imports it.
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
- **RT-15 extension:** the role and privilege assertions of `BACKEND_DESIGN.md` §21 for `eca_app` and `eca_worker`, plus the existing RT-15 suite. It includes the publish test: as `eca_app` with `app.user_id` = A, `platform.publish` of A's event succeeds and the worker role sees the row. As `eca_app`, SELECT, `INSERT … RETURNING`, UPDATE and DELETE on `outbox` fail with a privilege error, even when other users' rows exist, and an insert for user B or with NULL `user_id` fails the RLS check.
- Other tests:
  - Unit, no database: envelope and payload validation, registry rules, both backoff formulas and `HandlerRetryStrategy` decisions (attempt limit, non-retryable errors, jitter bounds, cap) with an injected clock and random source, lock-key format, crash points (inert when unset, startup error on an unknown name or in production), worker settings and `WorkerConfig`.
  - PostgreSQL:
    - migrations `0001` → head → base → head;
    - the privilege-matrix test of `BACKEND_DESIGN.md` §7.6 on a freshly migrated database;
    - the migration role-check failures: missing worker role, an unsafe role, membership between the roles;
    - the vendored Procrastinate schema compared with the package's `schema.sql`;
    - the dispatcher lifecycle (`failed` after 10, zero-handler and unregistered types, backoff timing with a controllable clock);
    - the handler wrapper (consumption conflict, rollback leaves no consumption);
    - stalled-job recovery;
    - the reconciler pass;
    - `outbox retry`.

**Not in this slice** (beyond the domain-schema exclusions above):
- a worker health, readiness or dispatcher-heartbeat check, which is designed with the hosting decision (`BACKEND_DESIGN.md` §16.5);
- replay of dead handler jobs;
- `eca ops` subcommands other than `outbox retry`;
- `idempotency_keys`;
- the natural-key handler mode (1.4);
- outbox retention purge (1.9);
- any AI provider code (0.4).

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
4. No domain schema was added (`users`, `source_items` and other domain tables are absent), `outbox.user_id` has no FK, and migrations created no roles.
5. §0 of this plan is updated with the measured results.

Slice 0.3 is **complete** when, in addition, GitHub Actions is green on the slice's commit: lint, tests with the PostgreSQL service including RT-01, RT-05 and RT-15, and a `secrets` job that actually scans the history. This requires the `secrets` workflow fix (§1.1). Until then the slice stays "locally verified", not "complete".

### Slice 0.4 AI provider layer

- Provider layer inside the `intelligence` module (`eca/intelligence/provider/`, prompts in `eca/intelligence/prompts/`, structured-output schemas in `eca/intelligence/output_schemas/`; there is no `eca.ai` package, `BACKEND_DESIGN.md` §5.4): wrapper over `google-genai` (structured output, embeddings, Files API); role registry `config/models.yaml` (model, thinking, temperature, fallback per role); pricing `config/pricing.yaml` with effective dates (`AI_COST_MODEL.md` §2); `ai_calls` meter and 15-minute cost roll-ups (`AI_COST_MODEL.md` §8); attempt cap logic (`AI_PIPELINE.md` §7); cassette recorder/replayer keyed by `(role, prompt_version, input_hash)`; provenance envelope type (`AI_PIPELINE.md` §5.1).
- Import-linter `forbidden` contract: only `eca.intelligence` imports `google.genai`.
- Smoke test: verifies every configured model ID exists (resolves Q8: exact `gemini-embedding-2` ID) and replaces `test_gemini_key.py`'s deprecated default.
- **Depends on:** 0.2 (unit of work, settings) and 0.3 (worker role, migration chain, Procrastinate periodic tasks). 0.4 does not create `users`.
- **Decisions (recorded before coding, `BACKEND_DESIGN.md` §5.5, §7.6, §7.7):**
  - `ai_calls.user_id` and `ai_cost_rollups.user_id`: deferred FK, added by the 1.1 `users` migration.
  - Access model: telemetry tables isolated per role (API INSERT-only on `ai_calls`, SELECT of its own roll-ups; worker all, including cross-user roll-ups).
  - Meter rows in their own short transaction.
  - Configuration at the repository root `config/`, loaded and validated at start.
  - Cassette key, format, modes and storage.
- **Migration `0005_ai_calls`:** `ai_calls`, `ai_cost_rollups` (unique per bucket, user, role and model with `NULLS NOT DISTINCT`), grants, FORCE RLS, four policies; no FK to `users`.
- **Code (`eca.intelligence`):**
  - `provider/registry.py`, `provider/pricing.py`, `provider/meter.py`, `provider/cassette.py`, `provider/gemini.py` (`google-genai` async client: structured output, embeddings, Files API), `provider/attempts.py`, `provenance.py`, the `ai_calls` tables and the `cost_rollup` periodic task (registered by `eca.worker` through `PeriodicTaskSpec`).
  - No production prompts: `prompts/` and `output_schemas/` contain only their layout until slice 1.4.
- **Smoke test:** pytest marker `live`, deselected by default and in CI, skipped without `GEMINI_API_KEY`. It checks every configured model ID with `models.get`, makes one structured call on the T1 role and one embedding on fixed synthetic text, sends no user data and never prints the key. `test_gemini_key.py` is retired once the smoke test has passed with a key.
- **Exit:** smoke test green; cassette replay deterministic.

### Slice 0.5 Evaluation harness

- `evals/ai` and `evals/context` runners, report format with the acceptance scorecard (`AI_EVALUATION.md` §8), simulated clock, paired-bootstrap statistics.
- `world_v1` first slice: fixtures (people, orgs, projects), ~150 labelled emails, chains CC-01–CC-10 in YAML with checkpoints and queries; split assignment (dev/test/sealed/challenge); `MANIFEST.json` with hashes; CI hash and contamination checks (gate A0).
- **Decisions:**
  - Code in `backend/eca_evals/` (public `eca` APIs only, import-linter contract); data in `evals/` at the repository root (`AI_EVALUATION.md` §3.1).
  - Data is synthetic only, with reserved domain names (RFC 2606: `.example`, `example.com`, `example.org`, `.test`).
  - Labels are **draft** until the labelling owner (Q12) reviews them. The freeze tool refuses to release a draft version, so `golden-v0.1` is prepared as a candidate manifest and frozen only after that review.
  - `DIR-120` moves to slice 1.4 (`AI_EVALUATION.md` §13).
  - Gate A0 runs in CI as tests: manifest hashes, contamination and split rules.
- **Exit:** runner executes end-to-end against stub pipelines and produces a scorecard; dataset slice frozen as `golden-v0.1` (expanded to `golden-v1.0` by end of Phase 1). The freeze requires the owner's label review.

---

## 3. Phase 1 — Context foundation

### Phase 1 batches (decided 2026-10-02)

Phase 1 is delivered in batches. The §9 order puts 1.3 and 1.4 on top of 1.1 and 1.2, so **Batch A** (the reliability walking skeleton) builds only the **data model** of 1.1 and 1.2 that 1.3 and 1.4 need, and defers their flows to **Batch B**.

| Batch | Slices | Contents |
|---|---|---|
| **A** | 1.3 + 1.4, plus the 1.1/1.2 data model | **Identity:** `users` table only. **People:** self Person, `persons`, `person_identifiers`, `organizations`. **Connections:** `connections` and `sync_cursors` tables only, with the provider value `fake`. The identity migration also adds `fk_outbox_user`, `fk_ai_calls_user` and `fk_ai_cost_rollups_user` (`BACKEND_DESIGN.md` §7.3.1, §7.6) and the worker role's users-enumeration read policy (§7.6). Then the 1.3 and 1.4 scope below |
| **B** | 1.1 + 1.2 flows | OIDC (PKCE, `state`, `nonce`), sessions, CSRF, `audit_log` flows, `DELETE /api/v1/me`, Google connect with incremental scopes, envelope-encrypted refresh tokens, `granted_scopes`, disconnect with revoke, `needs_reauth` |

Batch A rules:
- **No HTTP endpoint that needs an authenticated user** is added. `GET /api/v1/work-items` (§9) moves to Batch B or D. Tests and evaluation runners read work items through the `work` service. **No development authentication bypass** of any kind.
- Users and the self Person are created only by service functions (`identity.create_user`, `people.create_self_person`). Tests and the evaluation harness call them in an API-role unit of work for the new user's own ID. Batch B's sign-in callback calls the same functions.
- The fake mail connector serves `world_v1` mailboxes and evaluation-chain sources. The fake calendar connector implements the calendar protocol and contract tests only. Calendar sync is wired with the `meetings` module in slice 1.6, because no Batch A module normalizes calendar items.
- Chains with meeting sources (CC-01's and CC-06's transcripts) need the meeting pipeline (AI-10, Phase 4). In Batch A their meeting-dependent checkpoints are reported as blocked, not as passed. CC-37 needs reminders (slice 1.8 or later) and CC-44 is an answer-level (L4) chain, so both are reported with what L2 can and cannot assert.
- AI-01 runs in the default test run and CI from cassettes only. Without a recording key, cassettes are **placeholders**: hand-authored or label-derived responses marked `"origin": "placeholder"`. They test the deterministic pipeline (apply, mapping, dates, confidence, linking). They are never reported as model quality (E1–E4, A1).

### Slice 1.1 Identity and sessions

**Depends on:** 0.3 (outbox, worker role). Tests use mocked Google; the external Google Cloud project is needed only for a real sign-in.

Google OIDC (PKCE, `state`, `nonce`), server-side sessions, CSRF, `users` + self Person, `audit_log`, `DELETE /api/v1/me` request recorded (job built in 1.9). The migration that creates `users` also adds `fk_outbox_user` (`outbox.user_id → users(id)`, `BACKEND_DESIGN.md` §7.3.1), `fk_ai_calls_user` and `fk_ai_cost_rollups_user` (`BACKEND_DESIGN.md` §7.6) and defines the worker role's explicit read policy for enumerating users (`BACKEND_DESIGN.md` §7.6). Tests: auth flows with mocked Google, CSRF rejection, session expiry; FK present and enforced (outbox insert with an unknown `user_id` fails); RT-15 extended to the new tables.

### Slice 1.2 Connections

**Depends on:** 1.1 (`users`). Creates the `connections` and `sync_cursors` tables (owned by `connections`, `BACKEND_DESIGN.md` §5.1), which 1.3 uses.

Connect Gmail and Calendar (incremental scopes), envelope-encrypted refresh tokens (dev KEK from env), `granted_scopes` and feature gating, disconnect with revoke, `needs_reauth` on `invalid_grant`. Tests: token never logged/returned; denied-scope handling.

### Slice 1.3 Ingestion core with a fake connector

**Depends on:**
- 0.3 (outbox, dispatcher, reconciler);
- 0.5 (`world_v1` fixtures read by the fake connectors);
- 1.1 (`users`, which `source_items.user_id` references, and the worker's user-enumeration read policy used by the per-user reconciler scan);
- 1.2 (`connections` and `sync_cursors`).

- Connector protocols, DTOs, registry (`BACKEND_DESIGN.md` §20); fake mail/calendar connectors reading `world_v1`.
- `source_items` with stage machine; `sync_cursors` rules (advance after durable store); normalize (MIME, quote/signature stripping, participants, person/org resolution, conversation reply state); prefilter on neutral categories; outbox events.
- Source-item reconciliation: `reconcile` extended with the stage-SLA scan, run per user (`BACKEND_DESIGN.md` §7.5).
- **Tests:** RT-01 pipeline level, first half (sync commit → `SourceItemStored` → normalized exactly once, fake connector), RT-04 (replay ×3, shuffled), RT-06 (crash mid-sync), RT-07 (duplicate triggers); E1 prefilter false-skip rate.

### Slice 1.4 Extract and apply

**Depends on:** 0.4 (provider layer, cassettes, `ai_calls`), 0.5 (golden slice and runners for the A1 gate), 1.3 (`source_items`, normalization).

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

Provider deletion flow (§9.3), source purge on disconnect, account deletion job (`BACKEND_DESIGN.md` §13.3, including the user's `event_consumptions` and `outbox` rows before the user row; `ai_calls.user_id` set to NULL and the user's `ai_cost_rollups` rows folded into the NULL-user rows), retention purge job (including the 7-day purge of dispatched outbox rows and their consumptions), "Your data" summary endpoint. **Tests:** RT-10, RT-11.

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

**Status:** in progress — coding only; tests deferred (§0.6). Build order 2.1 → 2.2 → 2.3 → 2.5 → 2.4, because chat (2.4) consumes the retrievers, the change feed and topic mode.

**Phase 2 scope notes (decided 2026-10-02):**
- Transcripts do not exist before Phase 4. The index job subscribes to `MessageNormalized` and `MeetingChanged`; the `TranscriptStored` trigger and the transcript window chunker are added with the meeting pipeline (slice 4.2). S4 and S5 (meeting prep and cross-meeting reasoning) stay in Phase 4.
- Work-item and decision cards are not embedded in Phase 2: their discovery uses SQL full-text search over the rendered title or statement plus the evidence pivot from matched chunks (`CONTEXT_ARCHITECTURE.md` §9.10). Item embeddings arrive with `dedupe_embedding`.
- Thread and meeting summaries (AI-03, AI-10) do not exist yet, so no summary chunks are indexed.
- Per-user daily budget caps are enforced for chat and indexing only (`AI_COST_MODEL.md` §7.1); the full guardrail set (global budget, alerts, fitted per-user learning bounds) stays in slice 3.5.
- The minimal web chat page is in scope because PRD §57 items 12–13 require that a test user can ask contextual questions and see source-grounded answers.

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

**Slices 0.1 → 0.2 → 0.3 → 0.4 → 0.5 → 1.1 → 1.2 → 1.3 → 1.4, using the fake connector and recorded AI responses** ("reliability walking skeleton").

Each slice uses only infrastructure created by an earlier slice:

| Slice | Needs | From |
|---|---|---|
| 0.3 | Unit of work, migration baseline, CI | 0.2 |
| 0.4 | Worker role and migration chain; the `ai_calls.user_id` decision before start | 0.3 |
| 0.5 | Nothing beyond the package layout (its runners execute against stub pipelines); it follows 0.4 in plan order but does not depend on it | 0.2 |
| 1.1 | Outbox and worker role (for `fk_outbox_user` and the user read policy) | 0.3 |
| 1.2 | `users` | 1.1 |
| 1.3 | `world_v1` fixtures; `users` and the user read policy; `connections` and `sync_cursors` | 0.5; 1.1; 1.2 |
| 1.4 | Provider layer and cassettes; golden slice and runners; `source_items` | 0.4; 0.5; 1.3 |

Slices 1.1 and 1.2 need no Google OAuth verification (Testing mode is enough, and 1.1's auth tests mock Google). Real Gmail and Calendar traffic starts in 1.5 and 1.6. The walking skeleton is complete at 1.4:

A labelled `world_v1` mailbox flows through sync → outbox → normalize → extract (AI-01 statement schema) → apply (deterministic direction mapping, date resolver, penalty-based confidence, candidate maps, provenance) → work items with evidence and timelines, readable through a minimal `GET /api/v1/work-items` — with RT-01 (pipeline level) to RT-05 and RT-12 to RT-15 green, zero canonical-phrase confusion on `DIR-120`, zero invented dates, and CC-01–CC-10, CC-34–CC-37, CC-40–CC-45 passing at L2 (`AI_PIPELINE_REVIEW.md` Part 3).

Why first: it proves the four high-severity backend guarantees and the extraction quality baseline before any OAuth verification, UI or Google quota concerns, and every later slice builds on these modules unchanged.
