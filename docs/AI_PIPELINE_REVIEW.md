# AI Pipeline Review — Executive Context Assistant

**Status:** Final pre-implementation engineering review of the AI layer
**Date:** 2026-10-02
**Reviewed:** `PRD.md`, `TECHNICAL_DESIGN.md`, `BACKEND_DESIGN.md`, `CONTEXT_ARCHITECTURE.md`, `CONTEXT_EVALUATION.md`, `AI_PIPELINE.md`, `AI_EVALUATION.md`, `AI_COST_MODEL.md`, `IMPLEMENTATION_PLAN.md`, `ARCHITECTURE_REVIEW.md`, `CLAUDE.md`. No RAG, context-engineering, evaluation or cost-pipeline skill is installed in this environment.

Issue classes: **BLOCKING** (must be resolved before Phase 1 implementation), **IMPORTANT** (must be resolved before the phase that depends on it), **MINOR** (improvement; does not change the design), **FUTURE** (post-MVP).

---

## Part 1 — Review findings

### 1.1 Problems found and fixed in this review

| # | Class (before fix) | Problem | Fix (applied) |
|---|---|---|---|
| R1 | IMPORTANT | AI-01 output had only `owner`/`counterparty` and a model-chosen `type`; "John will send X", "I will send X", "waiting for John" and "John asked me to do X" could collapse into the same type/direction | Statement schema with `statement_kind` + speaker; **deterministic mapping** to type and direction, new directions `observed` and `unresolved`; contrastive dataset `DIR-120` with zero tolerance on canonical phrases (`AI_PIPELINE.md` §5.2, §5.5; `AI_EVALUATION.md` E3) |
| R2 | IMPORTANT | The rule that models do not invent dates was implied, not explicit; `due_iso` could be stored | `due_at` only from the deterministic resolver on a verbatim `due_text`; `due_iso_guess` diagnostic only; vague phrases → `fuzzy`; invented-date metric = 0 (`AI_PIPELINE.md` §5.4; E4) |
| R3 | IMPORTANT | Confidence came straight from the model, which is poorly calibrated, yet thresholds drove visibility and reminders | Penalty formula in Phase 1, isotonic calibration after `golden-v1.0` (experiment X9); ECE gate (`AI_PIPELINE.md` §5.3; E14) |
| R4 | IMPORTANT | Status signals reference prompt-local candidate IDs ("C3") that were not stored; apply could not resolve them safely after merges | `extractions.candidate_map` stored with entity IDs and versions (`BACKEND_DESIGN.md` §8.1) |
| R5 | IMPORTANT | Answers could not distinguish user-authored facts or AI recommendations; no abstention path | Claim kinds `source / user / inference / recommendation / absence`; `answerable`; abstention pre-check without a model call (`AI_PIPELINE.md` §5.7) |
| R6 | IMPORTANT | Online answer verification checked only citation existence | Deterministic grounding checks for dates, numbers and names in `source` claims; relabelling and confidence capping; agreement with judge measured (E7) |
| R7 | IMPORTANT | Prefilter rules could drop the user's own outbound mail, breaking "What did I promise?" | Outbound mail never prefiltered (except auto-replies) |
| R8 | IMPORTANT | Forwarded content could make the forwarder the promiser | Forwarded attribution, strength cap, no `my_commitment` for the forwarder; CC-34 |
| R9 | IMPORTANT | Re-transcription changes diarization labels and would silently invalidate confirmed speaker mappings | No automatic re-transcription; transcript versioning; re-confirmation required (`AI_PIPELINE.md` §15) |
| R10 | MINOR | LLM calls where code suffices: list answers (AI-06), briefing headline (AI-12), session summary (AI-13), short-thread summaries (AI-03), eager prep briefs (AI-11) | Deterministic list answers and briefing; AI-12/AI-13 retired; gist timelines; AI-11 reduced to lazy "suggested asks" over deterministic sections |
| R11 | IMPORTANT | T2 adjudication (AI-02) assumed to help without evidence (12% of heavy-user cost) | Disabled until experiment X1 shows lift |
| R12 | MINOR | 30-minute transcription windows created speaker-label consistency problems | Single call up to 3 h; windows only as fallback |
| R13 | IMPORTANT | Cost model underestimated AI-01 input (1,500 vs ~2,000 tokens) and ignored that outbound mail is always processed | Unit costs and profiles recomputed (`AI_COST_MODEL.md` §3–§4) |
| R14 | IMPORTANT | Evaluation could not detect overconfident answers, missing abstention, unnecessary escalation or direction confusion in near-identical phrasing | E15 abstention/overconfidence, E16 routing efficiency, `UNANS-40`, `ROUTE-60`, `DIR-120`, chains CC-34–CC-46 |
| R15 | MINOR | No deterministic `needs_reply` when AI-01 fails | Heuristic fallback labelled "unanalysed" |
| R16 | MINOR | No place for user-authored notes; questions "overdue", "who is waiting on me", "last three meetings" not specified | `notes` field (never AI-edited); `CONTEXT_ARCHITECTURE.md` §10.12a |

### 1.2 End-to-end flow traces

**A. Email ingestion**

| Aspect | Detail |
|---|---|
| Inputs | Gmail `history.list` changes → `messages.get` (full MIME) |
| Transformations | MIME parse → body cleaning (quotes, signatures, forwarded blocks marked) → participants → person/org resolution → conversation reply state → rules prefilter → AI-01 → apply (grounding, statement mapping, dates, confidence, candidate resolution, merge) → events → priority → reminders → dashboard query |
| AI calls | AI-01 (relevant mail); AI-04 (index); AI-03 only for long threads; AI-02 only if enabled |
| Non-AI logic | Everything except language labelling: prefilter, identities, reply state, mapping, dates, confidence, dedupe, priority, reminders, Today |
| Stored state | `source_items`, `messages` (+ triage/gist projection), `conversations`, `persons`, `extractions` (+ `candidate_map`), `evidence`, `work_items`, `decisions`, `context_events`, `chunks`, `reminders` |
| Provenance | Envelope on items, decisions, triage, mentions; per-change provenance on events |
| Failure points | Provider errors, AI-01 failure/malformed output, apply bug, embedding failure |
| Retries | Sync backoff; AI-01 attempt cap 4 with fallback model; apply 8 attempts (no AI); reconciler |
| Idempotency | Source keys, RFC 822 ID, extraction key with content hash, `apply_status`, deterministic evidence IDs, event dedupe keys, per-user merge lock |
| Cost | ≈ $0.0011 per relevant email (AI-01 + embedding) |
| Latency | p95 ≤ 5 min from provider change to Today (polling 2–10 min + processing) |
| Evaluation | E1–E4, E12–E14, `DIR-120`, CC-01–CC-10, CC-34–CC-37, CC-40–CC-45, RT-01–RT-07 |

**B. Email updates**

| Change | Affected entities | Recomputation | AI |
|---|---|---|---|
| New reply in thread | Conversation reply state; candidate items (status signals, acceptance); persons' stats; reminders (follow-up/acted-upon); priority | Normalize + apply + handlers | AI-01 for the new message only |
| Label change | Metadata; Trash/Spam visibility | Deterministic | none |
| Deletion | Body/chunks deleted, quotes redacted, items archived or flagged, conversation/person/priority/reminders recomputed | Deterministic (`BACKEND_DESIGN.md` §9.3) | none |
| Deadline change in a later email | Item due date (authority 4) or conflict; old reminder cancelled by fingerprint change; new reminder | Apply + reminder handler | AI-01 for the new message |
| Project context | Hints and mentions on new items; project activity | Handler | none |

**C. Meeting processing**

| Aspect | Detail |
|---|---|
| Inputs | Uploaded audio/video/transcript; optional calendar link |
| Transformations | sha256 dedupe → ffmpeg audio → AI-09 → segments → deterministic speaker matching → AI-10 (prior-meeting context, candidate map) → apply (segment-level grounding, mapping, dates relative to meeting start) → index → change diff → prep sections |
| AI calls | AI-09, AI-10, AI-04; AI-06/AI-07 for meeting Q&A |
| Non-AI logic | Media handling, speaker matching, mapping, dates, merge, diff, prep sections, structured lookups |
| Stored state | `recordings`, `transcript_segments` (versioned), `extractions`, items, decisions, `meeting_participants` mappings, chunks |
| Provenance | Evidence with `segment_seq`, `start_ms`, `end_ms`; model and prompt version |
| Failure points | Transcription failure/output limit, unmapped speakers, long-transcript quality, T2 outage |
| Retries | Stage-level; window fallback; user may upload a transcript instead |
| Idempotency | sha256, transcript version, extraction key |
| Cost | ≈ $0.37 per meeting hour |
| Latency | p95 ≤ 15 min per recorded hour |
| Evaluation | E11, E2–E4 on transcripts, E13 speaker mapping, CC-11–CC-17, CC-38, CC-39, CC-46; experiments X4, X5, X10 |

**D. Chat**

| Aspect | Detail |
|---|---|
| Inputs | Question, session (last 4 turns, entity focus, scope), now/timezone |
| Transformations | Pre-parse → AI-05 if needed → permission scope → typed retrievers / hybrid discovery → expansion (≤ 2 hops) → fold → rank and budget → abstention pre-check → deterministic rendering or AI-06/AI-07 → grounding checks → stream |
| AI calls | AI-05 (≈ 2/3 of questions), AI-04 query embedding, AI-06 or AI-07 (none for list intents) |
| Non-AI logic | Retrieval, ranking, packet assembly, coverage, list rendering, verification, abstention |
| Stored state | `chat_messages` (claims with kinds and citation snapshots), `retrieval_traces`, `ai_calls` |
| Provenance | Each claim: kind + citations → evidence → source; answer: model, prompt version, trace ID |
| Failure points | Planner error, empty retrieval, model outage, grounding failure |
| Retries | One retry + one fallback; then deterministic degradation |
| Idempotency | `Idempotency-Key` on the message request |
| Cost | $0 (list) / $0.0016 (lookup) / $0.0225 (synthesis) |
| Latency | p95 ≤ 1 s deterministic, ≤ 3 s lookup, ≤ 7 s synthesis |
| Evaluation | E6–E8, E10, E15, E16, `CONTEXT_EVALUATION.md` S1–S12, SS scripts |

**E. Cross-context reasoning** — follows D with relational expansion across email, meeting, person, project, commitment and deadline (`CONTEXT_ARCHITECTURE.md` §9.8): anchors resolved by SQL/aliases, items and timelines by indexes, evidence capped at ~2 quotes per item, packet ≈ 2.5–7 K tokens, T2 synthesis (AI-07), claim kinds and coverage. Evaluation: CC-01–CC-46, S5, S6, S11, E7, E8, E15.

**F. Daily briefing** — structured state (Today queries) → deterministic sections (priorities, meetings, waiting-on, deadlines, people) → deterministic headline template → stored in `briefings` (COMPUTED) → shown on Today. No AI call; cost $0; failure = previous briefing plus live Today data. Evaluation: deterministic section tests (E12).

### 1.3 Challenge of every AI call

| ID | Why AI is required | Deterministic alternative? | Model sufficient? | Tokens in / out | Cost | Verdict |
|---|---|---|---|---|---|---|
| AI-01 | Commitments, requests and urgency are expressed in free language | Rules handle bulk mail, identities, dates, mapping; they cannot label statements reliably | T1 expected sufficient; X2 compares three models | 2,000 / 325 | $0.00099 | Keep |
| AI-02 | Ambiguous third-party or unresolved-owner statements | Leave as `suggested` + user confirmation | Unknown; T2 not assumed better | 3,000 / 1,000 | $0.012 | **Disabled until X1** |
| AI-03 | Readable summary of long threads | Gist timeline for ≤ 8 messages | T1 | 3,000 / 250 | $0.0011 | Keep for long threads / on request (X3) |
| AI-04 | Semantic discovery of topics and paraphrases | FTS alone misses paraphrase | Embedding model | — | $0.0002/1 K | Keep |
| AI-05 | Classify unusual questions | Rules for common intents | T1 | 1,500 / 200 | $0.00068 | Keep, verify with X7 |
| AI-06 | Answers needing wording from retrieved text (transcript lookups) | Deterministic rendering for list intents and structured lookups | T1 | 4,000 / 400 | $0.0016 | Narrowed |
| AI-07 | Multi-source synthesis, status chains, "what changed", next action | Grouped lists as degradation only | T2 (X6 checks per intent) | 8,000 / 1,400 | $0.0225 | Keep |
| AI-08 | Drafting a reply | None for drafting | T2 | 6,000 / 1,200 | $0.018 | Keep |
| AI-09 | Speech to text with diarization | None | Speech model (X5) | audio | $0.30/h | Keep |
| AI-10 | Decisions, items, speaker proposals from a long transcript | Rules cannot | T2 default; X4 tests T1 | 22,000 / 5,000 per hour | $0.0705/h | Keep, verify |
| AI-11 | Suggest questions linking prior meetings | Deterministic sections cover facts | T2; X8 tests T1 | 2,000 / 600 | $0.0075 | Lazy only |
| AI-12 | Briefing headline | Template | — | — | — | **Retired** |
| AI-13 | Session summary | Last 4 turns + entity focus map | — | — | — | **Retired** |
| AI-14 | Paraphrase/support judging offline | Deterministic checks first | Pro | 4,000 / 800 | $0.0176 | Keep (offline) |

Common behaviour for all calls (`AI_PIPELINE.md` §7, §14):

| Question | Background extraction roles (AI-01/02/03/09/10) | Interactive roles (AI-05/06/07/08/11) |
|---|---|---|
| Call fails | Backoff, fallback model from attempt 3, cap 4, then `needs_attention`; source data intact | One retry, one fallback, then deterministic degradation |
| Output malformed | One repair call (counted); then failed | Retry once; then degradation |
| Low confidence | Item kept `suggested` with band `low`; not shown proactively; no reminders | Answer confidence capped; claims relabelled `inference`; abstain when nothing supported |
| Escalation trigger | AI-02 only if enabled (§8.2) | AI-06 → AI-07 on failed grounding or `missing_info` |
| Safe to retry / duplicates | Yes: unique extraction key, `running` claim, once-only apply | Yes: `Idempotency-Key`; no state except chat messages |
| Grounded | Evidence quotes required, verified in apply | Citations required, verified online |
| Deterministic enough for evaluation | Temperature 0, structured output, cassettes; variance measured over 3 runs | Same; rubric judged with calibrated judges |

### 1.4 Routing audit summary

| Area | Route | Rationale | Open experiment |
|---|---|---|---|
| Email classification | Rules → T1 | Rules remove half the volume; T1 labels the rest | X2 |
| Task/deadline extraction | T1 labels + code | Code owns dates and directions | X2 |
| Ambiguous commitments | T1 + user confirmation | T2 not proven | X1 |
| Priority | Code | Explainable, free | — |
| Meeting extraction | T2 | Long, multi-speaker input | X4, X10 |
| Meeting Q&A | Code → T1 → T2 by intent | Most questions are structured lookups | X6 |
| Cross-meeting reasoning | Code sections + T2 synthesis | Facts deterministic; reasoning over them needs synthesis | X8 (asks) |
| Reply guidance | T2 | Drafting quality | — |
| Daily briefing | Code | List-shaped | — |

The routing objective is quality × reliability ÷ (cost, latency); each open choice has a decision rule in `AI_PIPELINE.md` §13.

---

## Part 2 — AI readiness report

### A. AI architecture summary

The AI layer is a set of narrow, inventoried model calls inside a deterministic system. Models label language (statements, triage, mentions, summaries, transcripts) and synthesize answers over retrieved evidence; code owns facts: dates, directions, identities, merges, confidence calibration, priority, reminders, briefings and list answers. All model outputs are stored before being applied, carry full provenance and are evaluated against a frozen golden dataset with a multi-objective scorecard.

### B. Model routing table

| Class | Model (default → fallback) | Roles |
|---|---|---|
| Deterministic code | — | Prefilter, identities, mapping, dates, confidence, merge, priority, reminders, briefing, list answers, structured meeting lookups, prep sections, grounding checks, abstention pre-check |
| Embeddings / retrieval | `gemini-embedding-2` (768-d) + pgvector + FTS | AI-04; hybrid discovery; dedupe vectors |
| T1 | `gemini-3.1-flash-lite` → `gemini-3.5-flash-lite` | AI-01, AI-03, AI-05, AI-06 |
| T2 | `gemini-3.8-flash` → `gemini-3.5-flash` | AI-07, AI-08, AI-10, AI-11; AI-02 (disabled) |
| Speech | `gemini-3.5-transcribe` → `gemini-3.1-flash-lite` (audio) | AI-09 |
| Judge | `gemini-3.1-pro-preview` → `gemini-3.8-flash` | AI-14 (offline) |

### C. AI operation inventory

15 operations (O1–O15) with method decisions: `AI_PIPELINE.md` §4. 14 call IDs: 11 active (AI-01, 03–11, 14), AI-02 disabled pending X1, AI-12 and AI-13 retired: `AI_PIPELINE.md` §3.

### D. Retrieval architecture

Structured state first, hybrid discovery second, relational expansion ≤ 2 hops, temporal model with bi-temporal events, coverage block, permission scope, per-scenario budgets (`CONTEXT_ARCHITECTURE.md` §6–§10). Audit of the requested questions:

| Question | Data and method | Filters (time / person / project) | Ranking and weighting | Evidence and window | Fallback |
|---|---|---|---|---|---|
| What did John promise? | Resolve John (aliases, focus; ask if ambiguous) → items with owner John, `statement_kind` promise/report, open + recently closed | person = John; optional time | Due date, priority; explicit > probable; user-confirmed first | Item cards + 1–2 quotes each; ~2 K tokens; deterministic | Ambiguous person → clarification; none found → abstention with coverage |
| What am I waiting for? | `direction ∈ {waiting_for, delegated}` + implicit follow-ups | open | Overdue first, due date, priority | Cards + last event; deterministic | Coverage on stale sources |
| What did we decide yesterday? | Decisions with `decided_at` in yesterday's window + meetings/threads of that day | day window (§7.1) | Materiality, confirmed first | Decision statements + quotes; AI-07 only for synthesis | No decisions → say so with coverage |
| What changed since the last meeting? | Change feed since `ends_at` of the last attended meeting, scoped to its attendees and items; net-change fold | anchor time; attendee/person scope | Materiality × priority | Grouped changes with citations; ~5 K | Deterministic grouped list if T2 down |
| What did I promise Sarah? | `direction = my_commitment`, counterparty Sarah | person = Sarah | Due date, priority | Cards + quotes; deterministic | Ambiguous Sarah → clarification |
| Which commitments are overdue? | `due_at < now`, open, `my_commitment` and `waiting_for` | time | Overdue age, priority | Deterministic list | — |
| Who is waiting on me? | `my_commitment` + `my_task` + conversations awaiting the user, grouped by counterparty | open | Priority, due date | Deterministic grouped list | — |
| What should I reply to John first? | Needs-response list filtered to John → top thread → AI-08 on request | person = John | Priority features with reasons | Thread gist timeline + last 3 messages + open items | No awaiting thread → say so |
| What happened while I was away for three days? | Change feed since checkpoint (or 3 days) + next actions + new needs-response | checkpoint anchor | Materiality × priority | AI-07 "since you left" over grouped changes; ~5–7 K | Grouped list if T2 down |
| Unresolved issues from the last three meetings? | Last 3 attended meetings (or series) → open questions not resolved, open items, conflicts; resolution events from any source | time order; optional series/project | Recency, materiality | Deterministic list; AI-07 if synthesis requested | Fewer than 3 meetings → state count |

Vector search alone is never used for these questions; it only supplies anchors and supporting quotes when names or topics are unresolved.

### E. Extraction architecture

| Object | Required fields | Optional | Confidence / evidence / source | Model / prompt / method / timestamp | Owner / direction / status | Dedup key | Update semantics |
|---|---|---|---|---|---|---|---|
| Task / request / action item | `statement_kind`, action title, owner ref, speaker, evidence quote | `due_text`, beneficiary, candidate link | Calibrated confidence; evidence row; source item | Provenance columns + `extractions` | Owner resolved by code; direction by §5.5; lifecycle by user only | Merge rules (`TECHNICAL_DESIGN.md` §13.6) incl. rejected items; evidence `(extraction_id, candidate_index)` | Events + fold; user fields locked |
| Commitment | As task, `statement_kind ∈ {promise, report_commitment, acceptance}` | strength | Strength cap by kind | Same | `my_commitment`, `waiting_for`, `observed`, `unresolved` | Same | `accepted`/`declined`/`completed_claim` signals; never auto-done |
| Deadline | `due_text` verbatim | — | Code/model agreement penalty | Resolver version recorded in event payload | Attribute of an item; standalone `type = deadline` | With the item | `due_changed` events with authority |
| Person | Normalized email (or provider ID) | Name aliases, role (inferred), org | Identity deterministic; role confidence | Method `deterministic`/`rule` | — | `UNIQUE(user_id, kind, value_normalized)` | Merge only by shared email or user |
| Project | Name (user) or hint (AI) | Members, description | Hint count; confirmation | Method `llm` (hint) / `user` | — | `UNIQUE(user_id, lower(name))` for confirmed projects | Suggested after ≥ 3 hints; user confirms |
| Decision / open question | Statement, kind, evidence | Rationale, project | Confidence; evidence | Provenance columns | — | Embedding similarity + meeting/thread | `superseded_by`, `resolved_by`, conflicts |
| Relationship (links) | From, to, relation | — | Confidence | Method `rule`/`embedding_match`/`llm` | — | `UNIQUE(user_id, from, to, relation)` | Confirm / reject by user |
| Entity mention | Surface text, entity, source span | — | Confidence | Method `alias_match`/`extraction` | — | `(source_item_id, entity_id, char_start)` | Re-scan on alias change |

### F. Provenance architecture

Envelope on every AI-derived object (`AI_PIPELINE.md` §5.1), stored as provenance columns plus `extractions` and `evidence` (`BACKEND_DESIGN.md` §17.1), exposed on every API resource, verified by E14 (100% completeness; every provenance question answerable through the API). Answers separate direct facts (`source`), user-authored information (`user`), inferences and recommendations, and must abstain or cite coverage when evidence is missing (`AI_PIPELINE.md` §5.7).

### G. Failure/degradation strategy

Per-operation table in `AI_PIPELINE.md` §14. Principles: source data is never touched by AI failures; background work retries with caps and resumes via the reconciler; interactive paths degrade to deterministic structured answers; insufficient evidence produces "The available context does not establish this" with coverage; stale or disconnected sources are always disclosed.

### H. Evaluation strategy

Frozen `golden-v1.0` with dev/test/sealed/challenge splits; suites E1–E16; contrastive `DIR-120`, `UNANS-40`, `ROUTE-60`, forwarded and identity fixtures; 46 context chains; reliability tests RT-01–RT-15; scorecard over safety (zero tolerance), quality per slice, reliability, latency, cost and human diff review; experiments X1–X10 decide routing. Detection coverage:

| Failure | Detected by |
|---|---|
| Hallucinated commitments | E3 (zero), grounding drop rate, human sample |
| Wrong ownership / direction | E3 owner and direction metrics, `DIR-120` (zero on canonical phrases) |
| Incorrect deadlines / invented dates | E4 exact-date accuracy; invented-date metric (zero) |
| Incorrect person merges | E13 (zero), CC-41, CC-42 |
| Missing evidence | E14 provenance completeness (100%) |
| Irrelevant retrieval | E6 precision/NDCG, context precision |
| Cross-user leakage | RT-15, CC-33 (zero) |
| Stale context | E15 stale disclosure, CC-26, CC-27 |
| Contradictory context | CC-10, CC-39, E15 contradiction disclosure |
| Incomplete answers | E8 required-fact coverage |
| Overconfident answers | E15 overconfidence (≤ 1%, zero on ★), E14 calibration |
| Unnecessary strong-model escalation | E16 routing efficiency, X1/X4/X6/X8 |

The zero-tolerance safety checks were extended, not weakened.

### I. Cost strategy

Per active user per day (post-promotion, AI-02 disabled): ≈ $0.09 light, ≈ $0.21 typical, ≈ $0.42 heavy; 100-user pilot ≈ $505/month; soft/hard caps $1.00/$2.50. Top drivers and levers: chat synthesis (33%), email extraction (25%), transcription (21%), embeddings (6%), meeting extraction (5%) — each with applied and candidate levers and quality guards in `AI_COST_MODEL.md` §11. Assumptions validated: no caching assumed; AI-01 input raised to 2,000 tokens; outbound mail always processed; chat volumes and escalation rates are estimates to be replaced by telemetry after Phase 1 (> 30% deviation triggers a model update).

### J. Security/privacy considerations

| Area | Control |
|---|---|
| Prompt injection | Untrusted content delimited as data; no side-effecting tools; grounding; priority cap for unknown senders; CC-32 |
| Data minimization | Packets carry only selected items and quotes; no full histories; prompts and outputs not logged (only hashes and token counts) |
| Provider terms | Paid-tier Gemini only for user data; Files API uploads deleted after transcription |
| Evaluation data | Synthetic or explicitly consented only; cassettes from synthetic data only; shadow/canary only for consented pilot users |
| Human review | Per-item consent (Limited Use); audit log |
| Isolation | RLS on every retrieval and extraction query; RT-15, CC-33 |
| Deletion | AI artifacts (`extractions`, chunks, evidence quotes, chat snapshots) included in deletion jobs (`BACKEND_DESIGN.md` §13) |
| Notes | User notes never sent to models as instructions |

### K. Known risks

| Risk | Class | Mitigation |
|---|---|---|
| Real-world statement labelling accuracy below `DIR-120` results (messier language) | IMPORTANT | Pilot human review of commitments; confirmation workflow; challenge set refresh |
| Confidence calibration thresholds (0.6/0.8) are provisional until X9 | IMPORTANT | Penalty formula; calibration at Phase 1 exit |
| Online grounding checks may mislabel paraphrased facts | IMPORTANT | Agreement with judge ≥ 0.90 required (E7) in Phase 2 |
| Transcription and diarization quality on real meetings | IMPORTANT (Phase 4) | X5; speaker confirmation; transcript upload alternative |
| Long-transcript extraction quality ("lost in the middle") | MINOR | X10 |
| Body truncation (> 2.5 K tokens) could drop mid-message commitments | MINOR | Challenge long-email cases; adjust heuristic |
| Model churn and promotional price end | MINOR | Registry, fallbacks, A4 gate; post-promotion pricing assumed |

### L. Remaining limitations

- Extraction is English only.
- AI does not mark work complete; completion always needs the user.
- Unmapped meeting speakers produce `unresolved` items until confirmed.
- Gist timelines are less fluent than summaries for medium threads (accepted for cost and drift reasons; X3 can reverse).
- No reranker; no learned routing; no fine-tuning.
- The judge model is a preview model; judge metrics depend on calibration against humans.

### M. Decisions made

1. Models label language; code decides facts (dates, directions, identity, confidence, priority, reminders).
2. Statement-kind schema with deterministic direction mapping and `observed`/`unresolved` directions.
3. No invented dates: `due_at` only from verbatim `due_text` via the resolver.
4. Calibrated confidence (penalties, then isotonic).
5. Stored candidate maps for status signals.
6. Claim kinds with deterministic grounding checks and abstention.
7. Deterministic list answers, briefing, prep sections and structured meeting lookups; AI-12 and AI-13 retired; AI-11 lazy; AI-03 only for long threads.
8. AI-02 disabled until X1.
9. Single-call transcription; no automatic re-transcription.
10. Routing changes decided by experiments X1–X10 on the frozen dataset.
11. Evaluation extended with E15, E16, `DIR-120`, `UNANS-40`, `ROUTE-60`, CC-34–CC-46.

### N. Rejected alternatives

| Alternative | Reason |
|---|---|
| Let the model choose type and direction directly | Collapses near-identical phrasings; not testable deterministically |
| Store model-resolved dates | Invented or mis-resolved dates; code resolver is testable |
| Use raw model confidence | Poor calibration; thresholds unreliable |
| LLM for list answers and briefings | No quality gain over structured rendering; adds latency, cost and hallucination risk |
| T2 everywhere / T2 adjudication by default | No evidence of lift; cost |
| Rolling summary-of-summaries | Drift |
| Windowed transcription by default | Speaker-label inconsistency |
| Agentic retrieval loops | Unpredictable cost; harder to evaluate |
| Judge-only evaluation | Noisy where exact checks exist |

### O. Implementation dependencies

| Dependency | Needed by | Class |
|---|---|---|
| `.gitignore` before `git init`; real key out of version control | Slice 0.1 | IMPORTANT |
| Owner for dataset labelling and human review (TD Q12) | Slice 0.5 | IMPORTANT |
| `DIR-120` and email slice of `world_v1` labelled | Slice 1.4 | IMPORTANT |
| Exact model IDs verified (embedding) | Slice 0.4 | IMPORTANT |
| Paid-tier Gemini key | Before real user data (slice 1.5) | IMPORTANT |
| Experiments X1, X2, X9 completed | Phase 1 exit | IMPORTANT |
| Transcription capability check (segments + diarization JSON) | Start of Phase 4 | IMPORTANT (Phase 4) |
| Google OAuth app type and verification start | First external pilot | IMPORTANT (pilot) |

### P. Open questions

| # | Question | Class | Default |
|---|---|---|---|
| AQ1 | Enable AI-02? | IMPORTANT | Decided by X1 |
| AQ2 | Which T1 model for AI-01? | IMPORTANT | Decided by X2 |
| AQ3 | Is the planner (AI-05) needed? | MINOR | Decided by X7 |
| AQ4 | T1 sufficient for meeting extraction and asks? | MINOR | X4, X8 |
| AQ5 | Should gist timelines replace AI-03 entirely? | MINOR | X3 |
| AQ6 | Multilingual extraction | FUTURE | English only |
| AQ7 | Batch API import, rerankers, explicit context caching | FUTURE | Deferred (`AI_COST_MODEL.md` §6) |
| AQ8 | Chat-platform extraction per thread window (Teams/Slack) | FUTURE | Designed in `BACKEND_DESIGN.md` §20 |

---

## Part 3 — Verdict

1. **Is the AI architecture internally consistent?** Yes. After this review, `AI_PIPELINE.md`, `AI_COST_MODEL.md`, `AI_EVALUATION.md`, `CONTEXT_ARCHITECTURE.md`, `CONTEXT_EVALUATION.md`, `BACKEND_DESIGN.md`, `TECHNICAL_DESIGN.md`, `IMPLEMENTATION_PLAN.md` and `ARCHITECTURE_REVIEW.md` agree on the call inventory, routing, schemas, direction mapping, date rule, provenance, claim kinds, degradation, costs and evaluation. A cross-reference and term sweep found no remaining contradictions.
2. **Is the AI pipeline sufficiently specified for implementation?** Yes for Phases 0–3: schemas, mapping tables, validation rules, retry/degradation, provenance storage and gates are defined. Meeting processing (Phase 4) is specified but depends on experiment X5 and a transcription capability check at the start of that phase.
3. **Are the evaluation mechanisms sufficient to prevent regressions?** Yes, provided the frozen dataset is built as planned: every major change passes a per-slice scorecard with zero-tolerance safety metrics, cost and latency bands, reliability metrics and human diff review; the failure classes requested in this review are each detected by a named metric.
4. **Are the cost controls sufficient for an MVP?** Yes: per-call metering, per-user soft/hard caps with degradation routes, attempt caps, skip rules, cost acceptance bands for changes, and a pilot forecast of ≈ $505/month with worst-case heavy users at the soft cap.
5. **What must be resolved before Phase 1 implementation?** No BLOCKING design issues. Before or during Phase 0: add `.gitignore` and keep the key out of git; name the dataset/labelling owner; verify model IDs. Experiments X1, X2, X9 run inside Phase 1 and must be decided before Phase 1 exit.

**Verdict: ready for implementation.**

**First implementation slice:** the reliability walking skeleton from `IMPLEMENTATION_PLAN.md` §9 — slices 0.1 → 0.2 → 0.3 → 0.4 → 1.3 → 1.4 with the fake connector and recorded AI responses — now explicitly including the AI-01 statement schema, the deterministic statement-to-direction mapping, the date resolver with the no-invented-dates rule, the penalty-based confidence, stored candidate maps and provenance columns. Exit when RT-01–RT-05 and RT-12–RT-15 are green, `DIR-120` shows zero canonical-phrase confusion, the invented-date metric is zero, and CC-01–CC-10 plus CC-34–CC-37 and CC-40–CC-45 pass at L2.
