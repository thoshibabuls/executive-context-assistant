# Architecture Review — Executive Context Assistant

**Status:** Final pre-implementation review
**Date:** 2026-10-02
**Reviewed:** `PRD.md`, `TECHNICAL_DESIGN.md`, `CONTEXT_ARCHITECTURE.md`, `BACKEND_DESIGN.md`, `AI_PIPELINE.md`, `AI_COST_MODEL.md`, `AI_EVALUATION.md`, `CONTEXT_EVALUATION.md`, `IMPLEMENTATION_PLAN.md`, `CLAUDE.md`, installed skills (`ai-toolkit:backend-api-design`, `ai-toolkit:database-patterns`, `ai-toolkit:python-api-endpoint-creator`)

---

## Reconciliation log (changes made in this pass)

| Change | Where |
|---|---|
| `docs/prd.md` renamed to `docs/PRD.md` and `claude.md` to `CLAUDE.md` to match the names every document and `CLAUDE.md` itself use (case matters on Linux CI and for Claude Code on case-sensitive systems) | Repository |
| Created the three documents `CLAUDE.md` requires but which did not exist: `AI_PIPELINE.md`, `AI_EVALUATION.md`, `IMPLEMENTATION_PLAN.md` | `docs/` |
| Document authority map; `TECHNICAL_DESIGN.md` now summarizes and delegates instead of restating rules | `TECHNICAL_DESIGN.md` §5.1 |
| Foreign keys kept on integrity edges (especially AI-derived → source), no cascades; logical vs permanent deletion documented | `BACKEND_DESIGN.md` §13, §17.1 |
| API changed from POST-only/query-ID (Kotlin toolkit convention) to conventional REST: path IDs, GET/POST/PATCH/PUT/DELETE, action sub-resources, Problem Details, keyset cursors, `Idempotency-Key`, `If-Match` (412) / `base_version` (409) | `BACKEND_DESIGN.md` §2.1, §16 |
| Services raise domain errors; only the API layer maps to HTTP | `BACKEND_DESIGN.md` §14 |
| Outbox: schema, states (`pending`/`dispatched`/`failed`), dispatcher, duplicate dispatch, crash recovery, `event_consumptions`; explicit statement that Procrastinate does not join SQLAlchemy transactions | `BACKEND_DESIGN.md` §7 |
| Extract/apply separation with lifecycles; apply never calls AI (adjudication became its own job); extraction key includes `content_hash` | `BACKEND_DESIGN.md` §8 |
| Idempotency audit for every side effect and every external trigger | `BACKEND_DESIGN.md` §10 |
| Source-of-truth audit per table; invariants with tests | `BACKEND_DESIGN.md` §6 |
| Event-flow audit (new email, email update, deletion, meetings, user tasks/edits/corrections, revocation, time) | `BACKEND_DESIGN.md` §9 |
| Gmail cursor-expiry re-sync window corrected from "last 7 days" to "since last successful sync − 1 day (max 30 days)" | `BACKEND_DESIGN.md` §11.2 |
| AI call inventory (14 calls) with trigger, input, output, class, frequency, cache, batch, skip, retry, cost | `AI_PIPELINE.md` §3 |
| Unified context vocabulary (working/session/recent/persistent/historical × source/computed/AI-derived/user-authored); permission-aware retrieval; cross-source path example; indexing parameters; memory promotion rules | `CONTEXT_ARCHITECTURE.md` §4.1, §9.7–9.9, §12.5 |
| Reliability regression tests RT-01–RT-15 (including the four requested crash/concurrency/replay tests) | `BACKEND_DESIGN.md` §21 |
| Complexity removals (below, §E) applied across documents | All |
| AI pipeline redesign: 15 operations with an explicit method choice each (deterministic, rules, embeddings, retrieval, T1, T2, multimodal, async); provenance envelope on every AI-derived object; cascade routing with degradation; briefing reduced to deterministic sections + T1 headline (made fully deterministic in the AI review); per-message gist folded into AI-01 | `AI_PIPELINE.md` §4, §5.1, §8 |
| Separate cost model with light/typical/heavy profiles, pilot forecast, sensitivity and cost acceptance rules | `AI_COST_MODEL.md` |
| Frozen golden dataset governance (splits, manifest hashes, contamination check) and multi-objective acceptance scorecard | `AI_EVALUATION.md` §3, §8 |
| AI layer engineering review: statement-kind schema with deterministic direction mapping, date rule, calibrated confidence, stored candidate maps, claim kinds and abstention, deterministic list answers and briefing, AI-12/AI-13 retired, AI-02 disabled pending X1, gist timelines, lazy meeting asks, single-call transcription, new datasets (DIR-120, UNANS-40, ROUTE-60) and chains CC-34–CC-46, routing experiments X1–X10 | `AI_PIPELINE_REVIEW.md` |

---

## A. Confirmed decisions

1. Modular monolith in Python (FastAPI, SQLAlchemy 2 async on psycopg 3, Alembic, Pydantic v2); processes `api` and `worker` from one image; Next.js web app.
2. One PostgreSQL 16+ database with pgvector ≥ 0.8, FTS, `pg_trgm`, RLS fail-closed.
3. Relational context model: entities, work items, decisions, evidence, `context_events` timelines, status fold with authority levels.
4. Four data classes (source, computed, AI-derived, user-authored); single writer per table; AI cannot write source tables; user corrections at authority 5 survive recomputation.
5. Transactional outbox → dispatcher → Procrastinate; handler dedupe via `event_consumptions`; reconciler.
6. Extract (AI, persisted, keyed by source + content hash + pipeline + prompt version) separate from apply (deterministic, once-only, under a per-user merge lock).
7. Idempotency key for every side effect and external trigger.
8. Concurrency: event-first writes with row locks, per-user advisory lock only for apply/merge, versions with `If-Match`/`base_version`; no global locks.
9. Synchronization: provider cursors advanced only after durable storage; resumable import; etag versioning; webhooks (post-MVP) are sync signals.
10. Deletion: logical where operationally useful, permanent where privacy requires; ordered jobs; no cascades.
11. Retrieval: typed retrievers per scenario; hybrid discovery (vector + FTS, RRF) then ≤ 2-hop relational expansion; coverage block for absence claims; permission scope on every query.
12. No knowledge graph in the MVP (all traversals ≤ 2 hops in an ego-network); measurable revisit triggers.
13. AI: 14 inventoried calls; Flash-Lite (T1), Flash (T2), transcribe, embedding; Pro offline only; config-based roles; skip rules; attempt caps; per-user budgets.
14. Deterministic priority with template reasons; deterministic reminders with fingerprints and caps.
15. Meetings: sha256-deduplicated uploads, audio only, transcribe then extract with prior-meeting context.
16. Conventional REST API with Problem Details and keyset cursors.
17. Security: Google OIDC, server-side sessions + CSRF, KMS envelope encryption, read-only scopes, no model tools, paid-tier Gemini, Limited Use compliance, audit log.
18. Evaluation: frozen, versioned golden dataset (dev/test/sealed/challenge) gating every major AI change through a scorecard of quality, cost, latency and reliability; replayable context chains; deterministic metrics first; calibrated judges; human review; reliability regression tests; CI gates; shadow and canary for model switches.
19. Provenance envelope (source, confidence, timestamp, extraction method, model, evidence) on every AI-derived object.

## B. Rejected alternatives

| Alternative | Why rejected |
|---|---|
| Microservices | One transaction spans the context core; small team; no independent release need |
| Document database | Relational query shape; transactions across items/evidence/events |
| External vector DB (Pinecone, Qdrant, Weaviate) | Second store, data copy, double isolation and deletion work; pgvector suffices at MVP scale |
| Gemini File Search (managed RAG) | Cannot join with items or apply our ranking; vendor lock of retrieval; another copy of restricted data |
| Graph DB / GraphRAG | No ≥ 3-hop scenario; costly LLM indexing; second store |
| Vector-only RAG | Fails state questions (waiting-for, promised, latest status) |
| Agentic tool loop / multi-agent / LangGraph | Fixed workflows; unpredictable cost; PRD §39 |
| Direct job enqueue after commit | Work lost on crash between commit and enqueue |
| Redis/Celery | Extra service; non-transactional enqueue |
| Custom outbox-as-queue worker (no Procrastinate) | Would re-implement retries, locks, periodic tasks and stalled-job recovery |
| Combined extract+apply | Partial state on failure; retries re-pay AI |
| SERIALIZABLE isolation / global locks | Retry storms; blocks unrelated users |
| LLM-assigned priority; LLM-decided reminders | Not explainable; costly; inconsistent |
| POST-only RPC API with query IDs | Kotlin/axios-specific convention; worse tooling fit |
| Offset pagination | Unstable on continuously changing collections |
| Soft-delete-only; cascading deletes | Privacy non-compliance; hidden deletion side effects |
| Batch API import in MVP | Ledger/polling complexity for ≈ $80 total savings at pilot scale |
| Push notifications (Pub/Sub) in MVP | Extra GCP setup; polling fallback still required |

## C. Remaining risks

| Risk | Type | Impact | Mitigation / owner |
|---|---|---|---|
| Google restricted-scope verification and annual security assessment gate public launch; Testing mode limits to 100 users with 7-day refresh tokens | Product/compliance | Pilot friction (weekly reconnect); launch delay | Start verification in Phase 1; consider Internal app for a single-Workspace pilot (Q2) |
| Extraction precision below target (false commitments) | Product/AI | Trust loss (PRD Risk 1, 2) | Grounding checks, confidence gates, A1 gate, human review |
| Model churn (Gemini IDs retired with short notice; promotional T2 price ends 2026-12-31) | Technical/cost | Breakage; cost doubles for T2 | Config registry, fallbacks, A4 gate; cost model already uses post-promotion prices |
| `gemini-embedding-2` ID inconsistency across Google pages | Technical | Wrong model ID | Phase 0 smoke test (Q8) |
| Golden dataset effort and labelling ownership undecided | Process | Gates without data | Assign owner (Q12) before slice 0.5 |
| Per-user merge lock contention for very active users | Technical | Apply latency | Short transactions; lock-wait metric; narrow lock scope to per-counterparty if needed |
| Speaker diarization quality on real recordings | AI | Wrong owners for meeting commitments | Confidence ≥ 0.9 for auto-mapping; confirmation UI; E11 |
| Retention defaults vs PRD §39 wording | Product | Misaligned expectations | Visible, configurable policy (Q5) |
| Hosting choice undecided | Operational | Deployment work later | Target-agnostic image; decide before first hosted pilot (Q1) |
| Single-region, single database | Operational | Outage = full outage | Managed Postgres with PITR; acceptable for MVP |

## D. Known limitations (the MVP does not solve)

- No sending email, Gmail drafts, calendar changes or other actions (PRD Level 3).
- No Microsoft 365, Teams, Slack, Drive or chat-platform data (interfaces only).
- No real-time push; mail appears within the polling interval (2–10 minutes).
- English only.
- One Google account per user; no organization/team tenancy or shared context.
- No video-frame or screen-share understanding; audio only.
- No learned ranker or fine-tuning; personalization is bounded rule adjustments.
- No relationship discovery beyond the user's ego-network; no person-to-person org graph.
- After a full re-apply rebuild (R2), AI-only items get new IDs; old chat citations keep text snapshots but links show "no longer exists".
- Web Push can deliver a duplicate notification if a worker crashes after the push service accepted it (at-least-once at the network edge, collapsed by `Topic` when still queued).
- A crash between receiving an AI response and committing it costs one repeated AI call.
- Mail older than 30 days at connect time and gaps longer than 30 days after a long disconnection are not imported automatically (reported in coverage).

## E. Complexity audit

| Component | PRD / rule requiring it | Verdict |
|---|---|---|
| Background job queue (Procrastinate) | PRD §12 periodic analysis, §19 reminders, §21 meeting processing, §46 asynchronous background processing | **Keep** |
| Transactional outbox + dispatcher | PRD §46 reliability; `CLAUDE.md` reliability principle (idempotent, recoverable, failed AI must not corrupt data) | **Keep** |
| `event_consumptions` | Exactly-once effects under at-least-once dispatch (same rule) | **Keep** |
| Reconciler | Crash recovery (same rule) | **Keep** |
| `context_events` (domain event log) | PRD §11 continuity, §25 "what changed", §34 corrections, §46 auditability | **Keep** (distinct from outbox) |
| pgvector in Postgres | PRD §10 historical/decision questions, §23 meeting chat | **Keep** |
| External vector database | None | **Not used** |
| Graph database / GraphRAG | None | **Not used** |
| Agents / agent framework / LangGraph | None (PRD §39 excludes multi-agent) | **Not used**; one bounded planner call |
| Redis / external cache | None | **Not used** |
| Rendered-card cache | None (rendering is cheap) | **Removed** |
| Query-embedding cache | None (≈ $0.00001 per query) | **Removed** |
| Dashboard ETag / per-user `context_version` counter | None (indexed SQL is fast) | **Removed** (hot-row writes avoided) |
| Chat packet reuse | None | **Removed** |
| `daily_digests` table | PRD §12 digest — satisfied by an on-demand day view | **Removed** (materialize later if needed) |
| Person-card LLM blurb, digest narrative LLM call | None | **Removed** (deterministic templates) |
| Batch API import + submission ledger | PRD §37 says "should be considered"; saving ≈ $80 at pilot scale (`AI_COST_MODEL.md` §6) | **Deferred** |
| Shared Postgres rate-limiter table | None at MVP scale (one sync job per connection; queue concurrency caps) | **Removed** (in-job and in-process limiters) |
| Webhooks / Pub/Sub / webhook inbox | None for MVP (polling meets latency) | **Deferred** (designed) |
| Separate media worker deployment | None | **Deferred** (`media` queue in the worker) |
| OpenTelemetry exporter/backend | PRD §45 observability is met by `ai_calls`, logs, Sentry | **Deferred** (code instrumented) |
| KMS envelope encryption | PRD §46 security, §55 Risk 5 | **Keep** (env KEK in dev) |
| RLS | PRD §46 source-level authorization | **Keep** |
| AI-02 adjudication | PRD §37 "ambiguous commitments → more capable processing" | **Keep**, capped at 20/user/day |
| `idempotency_keys` | Client retries for uploads and chat (duplicate prevention) | **Keep**, limited to those endpoints and creates |
| `entity_mentions`, `entity_links` | PRD §26 people intelligence, §11 continuity (thread continuation) | **Keep** |
| Web Push | PRD §19 reminders (in-app satisfies minimum) | **Optional** in Phase 3; defer if time-constrained |
| Session summary (AI-13) | PRD §35 session context is met by last 4 turns + entity focus map | **Removed** in the AI review (`AI_PIPELINE_REVIEW.md`) |
| Briefing headline LLM call (AI-12) | PRD §30 briefing is list-shaped | **Removed** (deterministic template) |
| LLM list answers, LLM thread summaries for short threads, eager prep briefs | None (deterministic rendering, gist timelines, lazy asks) | **Removed / made lazy** |
| AI-02 adjudication | PRD §37 "ambiguous commitments → more capable processing" | **Disabled until experiment X1 shows lift** |

## F. Cost audit

| Rank | Operation | Share of heavy-user daily cost | Mitigations in design |
|---|---|---|---|
| 1 | AI-07 chat synthesis | ≈ 33% | Deterministic list answers; lookups on T1; abstention pre-check; per-scenario budgets; experiment X6 |
| 2 | AI-01 email extraction | ≈ 25% | Rules prefilter of inbound bulk mail; content-hash keys; no reprocessing on label changes; one combined call; experiment X2 |
| 3 | AI-09 transcription | ≈ 21% | Audio only; sha256 dedupe; no automatic re-transcription; experiment X5 |
| 4 | AI-04 embeddings | ≈ 6% | Prefiltered mail not embedded; quoted replies stripped |
| 5 | AI-10 meeting extraction | ≈ 5% | One call per meeting; experiment X4 (T1) |
| — | AI-02 adjudication | 0% (≈ 13% if enabled) | Disabled until X1 |
| — | Daily briefing | 0% | Deterministic |
| — | Initial import | ≈ $0.85–3.50 one-time per user | 30-day window, prefilter, throttling; Batch API later |
| — | Re-extraction after prompt changes | Variable | Never automatic; dry-run cost + approval |
| — | Evaluation | ≈ $15 per full live run | Cassettes in CI; live runs only for affected roles; judge only where deterministic checks cannot decide |

Steady state per active user per day: ≈ $0.09 (light), ≈ $0.21 (typical), ≈ $0.42 (heavy) at post-promotion prices, with soft/hard caps at $1.00/$2.50 (`AI_COST_MODEL.md`).

## G. Reliability audit

| Place | Failure | Loss / duplicate / corruption / inconsistency | Protection |
|---|---|---|---|
| Sync page | Crash mid-page | Loss | Page transaction; cursor advanced only after run (RT-06) |
| Cursor expiry | History unavailable | Loss | Re-sync from last success − 1 day (max 30 days); coverage reports older gaps |
| Duplicate triggers | Manual + periodic (+ webhook later) | Duplicate | Job locks; source keys (RT-07) |
| Business commit → job | Crash before dispatch | Loss | Outbox (RT-01) |
| Dispatch | Crash after defer, before mark | Duplicate | `queueing_lock` + `event_consumptions` (RT-05) |
| Extraction | Crash after AI response, before commit | Duplicate spend | One repeated call max; unique key (RT-02b) |
| Extraction → apply | Crash between | Loss | Apply job + reconciler; no AI (RT-02) |
| Apply | Error mid-transaction | Corruption | Single transaction; rollback |
| Concurrent apply | Same commitment from two sources | Duplicate | Per-user merge lock + re-matching (RT-03) |
| User edit vs model signal | Race | Lost update | Row lock, authority 5, `user_fields` (RT-03b) |
| Person/item merges with in-flight jobs | Stale IDs | Inconsistency | `merged_into_id` redirection |
| Reminder sweep + handlers | Double fire | Duplicate | Fingerprints; notification keys (RT-09) |
| Web Push | Crash after send | Duplicate (residual) | `Topic` header; accepted limitation |
| Provider deletion | Source removed | Dangling references | FKs; quote redaction; `has_source_gap` (RT-11) |
| Account deletion vs queued jobs | Jobs recreate data | Inconsistency | User `deleting` state; jobs no-op; resumable job (RT-10) |
| Recomputation | Rebuild loses corrections | Corruption | User events never deleted; user-touched IDs kept (RT-13, RT-14) |
| Embedding model change | Mixed vectors | Inconsistent retrieval | `embedding_model` per row; queries filter by model; background re-embed |
| Migrations | Concurrent migration by api/worker | Corruption | Release-step migrations only |
| Model retirement | Calls fail | Loss of processing | Fallback roles; stage retry; reconciler |
| Database | Instance failure | Loss | Managed backups with point-in-time recovery |
| Timezones | Wrong "yesterday"/deadline | Inconsistency | Temporal spec; resolver tests (E4) |

## H. Security audit

| Area | Risk | Control | Residual |
|---|---|---|---|
| Authorization (IDOR) | Access to other users' IDs | RLS fail-closed + explicit filters; 404 for foreign IDs; RT-15, CC-33 | Low |
| RLS bypass | Migration role or cross-user maintenance jobs | API and worker roles cannot bypass RLS; cross-user jobs touch business data only per user with `app.user_id` set per transaction; cross-user access exists only on delivery infrastructure, granted to the worker role by explicit policies (§J); `SET LOCAL` only (no session-level leakage across pooled connections) | Low; review every cross-user job |
| Source authorization | Data from revoked grants still used | Connection-status scope filter; coverage reports gaps | Low |
| Token storage | Refresh token theft | KMS envelope encryption; never logged or returned; revocation on disconnect | Low; KEK in env during dev only |
| Sessions/CSRF | Session hijack, CSRF | HttpOnly/Secure/SameSite cookies; CSRF header; OAuth `state`, PKCE, `nonce` | Low |
| Prompt injection | Malicious email content | Delimited untrusted content; no side-effecting tools; grounding; priority cap for unknown senders; CC-32 | Medium (wrong suggestions remain possible, labelled) |
| Sensitive data leakage | Content in logs, Sentry, telemetry | IDs only; Sentry scrubbing and bodies disabled; `ai_calls` content-free | Low; verify in code review |
| Third-party processing | Gemini data use | Paid tier only; Files deleted after use | Low |
| Human access | Support reading mail | Limited Use: per-item consent + audit | Low |
| Deletion completeness | Data remaining in backups, object storage, Gemini Files, Sentry, chat citation snapshots, exports | Ordered deletion job covers DB, objects, provider files, chat sessions, exports; backups age out within the stated window | Medium until backup window is documented in the privacy notice |
| Restricted-scope compliance | Launch blocked or app suspended | Verification + annual assessment planned | Open (product decision Q2) |
| Secrets in repository | Real Gemini key in `.env` | `.gitignore` before `git init`; gitleaks; rotate if exposed | Open until slice 0.1 |
| Supply chain | Malicious dependencies | Pinned versions, lockfiles, dependency scanning in CI | Low |

## I. Final verdict

**Ready with minor changes.** The subsequent AI layer review (`AI_PIPELINE_REVIEW.md`) found no blocking issues and concluded "ready for implementation".

The architecture is internally consistent across all documents after this reconciliation, and no design-level blocking issue remains. The minor changes are setup and ownership tasks, not architecture changes, and all are scheduled in `IMPLEMENTATION_PLAN.md`:

1. Add `.gitignore` before `git init` and keep the Gemini key out of version control (slice 0.1).
2. Verify the exact model IDs, especially `gemini-embedding-2`, with the Phase 0 smoke test (slice 0.4).
3. Assign an owner for golden-dataset labelling and weekly human review (before slice 0.5).
4. Decide the OAuth app type and start Google verification (before the first external pilot, not before coding).
5. Decide hosting (before the first hosted pilot, not before coding).

## J. Pre-0.3 reconciliation (2026-10-02)

A fresh-session audit after slices 0.1–0.2 found four unresolved points that block slice 0.3. All are resolved in the authoritative documents; this table only records the decisions.

| Issue | Decision | Rationale | Rejected alternatives | Implementation consequence |
|---|---|---|---|---|
| `outbox.user_id REFERENCES users(id)`, but `users` arrives in slice 1.1 | Create `outbox` in 0.3 without the FK; identity's 1.1 migration that creates `users` adds `fk_outbox_user` (`BACKEND_DESIGN.md` §7.3.1) | Keeps the FK; platform never references identity's schema; the constraint is added by the higher layer | Drop the FK (loses integrity); stub `users` in 0.3 (platform owning identity schema); move outbox to 1.1 (blocks the reliability core) | 0.3 migration has no FK; 1.1 migration adds it and fails loudly on orphans; account deletion removes outbox rows before the user row |
| Dispatcher and reconciler are cross-user, but RLS fails closed when `app.user_id` is unset | Separate worker role `eca_worker`. Business tables keep per-user RLS for every role. Delivery infrastructure (`outbox`, `event_consumptions`, Procrastinate) is isolated by role: API is INSERT-only on its own outbox rows; only the worker role reads across users, through explicit `TO eca_worker` policies (`BACKEND_DESIGN.md` §7.6) | Worker authorization is explicit (a role with its own credentials), not "variable unset"; no value of `app.user_id` gives the API role cross-user infrastructure access | Disable RLS on outbox and share one role (API could read every user's events); a "system mode" session variable (settable by any code in the API role); dispatcher as the migration role (DDL powers, bypasses all RLS); SECURITY DEFINER functions (hides logic in SQL); per-user dispatch loops (no global `SKIP LOCKED`, cannot handle system events) | 0.3 adds the role, grants, revokes of default grants, policies and RT-15 extensions; the worker gets its own database URL |
| RT-01 asserts normalize → extract → apply, which do not exist in 0.3 | RT-01 is built in two levels: infrastructure level (synthetic events and handlers, 0.3) and pipeline level (1.3–1.4). Phase 1 exit requires the pipeline level (`BACKEND_DESIGN.md` §21) | Tests the delivery guarantee as soon as it exists without weakening the end-to-end requirement | Defer RT-01 to 1.4 (0.3 would have no crash test for the outbox); redefine RT-01 as synthetic only (weakens the guarantee) | 0.3 adds crash points, synthetic handlers and an effect table without unique keys |
| Reconciler described over `source_items`, which arrives in 1.3; AI package named both `eca.ai` and `intelligence` | Reconciler in 0.3 handles outbox re-dispatch only; 1.3 adds the per-user source-item scan (`BACKEND_DESIGN.md` §7.5). AI provider layer lives in `eca.intelligence`; no `eca.ai` package (`BACKEND_DESIGN.md` §5.4) | No pretend tables; `intelligence` already owns `ai_calls` and is the single choke point for budgets and attempt caps | Reconciler stub over a placeholder table; top-level `eca.ai` as a shared layer (bypassable budgets, shared layer writing a domain table) or as a 15th module (split ownership) | 0.4 builds `eca/intelligence/provider/` and adds an import-linter rule that only `eca.intelligence` imports `google.genai` |
