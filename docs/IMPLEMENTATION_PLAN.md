# Implementation Plan — Executive Context Assistant

**Status:** Ready to start (Phase 0)
**Date:** 2026-10-02
**Authority:** Execution order, slices, deliverables and exit criteria. Architecture is defined in `TECHNICAL_DESIGN.md` and the documents in its §5.1; this plan must not introduce behaviour or architecture that those documents do not describe (`CLAUDE.md`: "If implementation requires changing product behavior, stop and update the appropriate document first").

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

### Slice 0.3 Reliability core

- `outbox`, `event_consumptions`, `platform.publish(event)`; dispatcher loop (`SKIP LOCKED`, states, backoff, `failed` after 10); Procrastinate setup (psycopg 3) with queues from `BACKEND_DESIGN.md` §15; handler decorator that records `event_consumptions` in the handler transaction; reconciler; job key conventions (`lock`, `queueing_lock`).
- Crash-injection test utilities (kill points between commit/dispatch/handler).
- **Tests:** RT-01, RT-05.
- **Exit:** both pass repeatedly (100 runs) without flakiness.

### Slice 0.4 AI provider layer

- `eca.ai.provider` wrapper over `google-genai` (structured output, embeddings, Files API); role registry `config/models.yaml` (model, thinking, temperature, fallback per role); pricing `config/pricing.yaml` with effective dates (`AI_COST_MODEL.md` §2); `ai_calls` meter and 15-minute cost roll-ups (`AI_COST_MODEL.md` §8); attempt cap logic (`AI_PIPELINE.md` §7); cassette recorder/replayer keyed by `(role, prompt_version, input_hash)`; provenance envelope type (`AI_PIPELINE.md` §5.1).
- Smoke test: verifies every configured model ID exists (resolves Q8: exact `gemini-embedding-2` ID) and replaces `test_gemini_key.py`'s deprecated default.
- **Exit:** smoke test green; cassette replay deterministic.

### Slice 0.5 Evaluation harness

- `evals/ai` and `evals/context` runners, report format with the acceptance scorecard (`AI_EVALUATION.md` §8), simulated clock, paired-bootstrap statistics.
- `world_v1` first slice: fixtures (people, orgs, projects), ~150 labelled emails, chains CC-01–CC-10 in YAML with checkpoints and queries; split assignment (dev/test/sealed/challenge); `MANIFEST.json` with hashes; CI hash and contamination checks (gate A0).
- **Exit:** runner executes end-to-end against stub pipelines and produces a scorecard; dataset slice frozen as `golden-v0.1` (expanded to `golden-v1.0` by end of Phase 1).

---

## 3. Phase 1 — Context foundation

### Slice 1.1 Identity and sessions

Google OIDC (PKCE, `state`, `nonce`), server-side sessions, CSRF, `users` + self Person, `audit_log`, `DELETE /api/v1/me` request recorded (job built in 1.9). Tests: auth flows with mocked Google, CSRF rejection, session expiry.

### Slice 1.2 Connections

Connect Gmail and Calendar (incremental scopes), envelope-encrypted refresh tokens (dev KEK from env), `granted_scopes` and feature gating, disconnect with revoke, `needs_reauth` on `invalid_grant`. Tests: token never logged/returned; denied-scope handling.

### Slice 1.3 Ingestion core with a fake connector

- Connector protocols, DTOs, registry (`BACKEND_DESIGN.md` §20); fake mail/calendar connectors reading `world_v1`.
- `source_items` with stage machine; `sync_cursors` rules (advance after durable store); normalize (MIME, quote/signature stripping, participants, person/org resolution, conversation reply state); prefilter on neutral categories; outbox events.
- **Tests:** RT-04 (replay ×3, shuffled), RT-06 (crash mid-sync), RT-07 (duplicate triggers); E1 prefilter false-skip rate.

### Slice 1.4 Extract and apply

- `extractions` lifecycle (`BACKEND_DESIGN.md` §8.1); AI-01 prompt v1 + schema; candidate lists (`CONTEXT_ARCHITECTURE.md` §12.1).
- Apply: grounding, date resolution, direction rules, candidate matching and merge (`TECHNICAL_DESIGN.md` §13.6) under the per-user merge lock; evidence with deterministic IDs; `context_events`; `work.append_event` with row lock, fold, `version`, `user_fields`; provisional items + `AdjudicationNeeded` (AI-02 job); `messages.triage` projection.
- Recomputation commands R1 (re-fold) and R2 (re-apply).
- Rules prefilter (outbound never prefiltered), AI-01 statement schema with `gist`, stored `candidate_map`, deterministic statement-to-direction mapping (`AI_PIPELINE.md` §5.5), deterministic deadline resolver with the no-invented-dates rule (§5.4), penalty-based confidence (§5.3), forwarded-content attribution, `notes` field, provenance columns on every AI-derived row (`BACKEND_DESIGN.md` §17.1). AI-02 implemented behind a disabled flag.
- Experiments X1 (adjudication lift) and X2 (AI-01 model choice) on the slice; X9 (calibration) at Phase 1 exit.
- **Tests:** RT-02, RT-02b, RT-03, RT-03b, RT-12, RT-13, RT-14; E1–E4 (incl. `DIR-120` and invented-date check), E13, E14 on the `world_v1` slice; CC-01–CC-10, CC-34–CC-37, CC-40–CC-45 at L2.
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

Provider deletion flow (§9.3), source purge on disconnect, account deletion job (`BACKEND_DESIGN.md` §13.3), retention purge job, "Your data" summary endpoint. **Tests:** RT-10, RT-11.

**Phase 1 exit:** PRD §57 items 1–3, 5–9, 20 (v1) and 21 demonstrable with a real Gmail test account; RT-01–RT-07 and RT-10–RT-15 green; `golden-v1.0` (full email set) frozen and baselined; E1–E4, E13, E14 targets met on its `test` split; X1, X2 and X9 decided and recorded; measured AI-01 tokens within 30% of `AI_COST_MODEL.md` §3; CC-01–CC-10 pass at L2.

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
| Outbox atomicity and duplicate dispatch | §7 | 0.3 | RT-01, RT-05 |
| Extract/apply separation | §8 | 1.4 | RT-02, RT-02b |
| Replay idempotency | §10 | 1.3, 1.4 | RT-04 |
| AI cannot write source tables | §6.3 | 1.4 | RT-12 |
| Recomputation preserves user data | §8.5 | 1.4 | RT-13, RT-14 |

---

## 9. Recommended first implementation slice

**Slices 0.1 → 0.2 → 0.3 → 0.4 → 1.3 → 1.4, using the fake connector and recorded AI responses** ("reliability walking skeleton"):

A labelled `world_v1` mailbox flows through sync → outbox → normalize → extract (AI-01 statement schema) → apply (deterministic direction mapping, date resolver, penalty-based confidence, candidate maps, provenance) → work items with evidence and timelines, readable through a minimal `GET /api/v1/work-items` — with RT-01 to RT-05 and RT-12 to RT-15 green, zero canonical-phrase confusion on `DIR-120`, zero invented dates, and CC-01–CC-10, CC-34–CC-37, CC-40–CC-45 passing at L2 (`AI_PIPELINE_REVIEW.md` Part 3).

Why first: it proves the four high-severity backend guarantees and the extraction quality baseline before any OAuth verification, UI or Google quota concerns, and every later slice builds on these modules unchanged.
