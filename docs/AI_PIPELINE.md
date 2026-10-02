# AI Pipeline — Executive Context Assistant

**Status:** Reviewed architecture (pre-implementation; see `AI_PIPELINE_REVIEW.md`)
**Date:** 2026-10-02
**Authority:** This document is authoritative for the AI operations catalog, the processing method chosen for each operation, the AI call inventory, model routing, structured outputs, statement-to-direction rules, confidence computation, provenance, answer grounding, prompts, skip/retry/degradation rules and routing experiments. Prices, unit costs, budgets and cost guardrails are in `AI_COST_MODEL.md`. Quality gates and the frozen golden dataset are in `AI_EVALUATION.md`. Transactions and jobs around AI calls are in `BACKEND_DESIGN.md`; context assembly in `CONTEXT_ARCHITECTURE.md`.
**Inputs:** `docs/PRD.md`, `docs/TECHNICAL_DESIGN.md`, `docs/CONTEXT_ARCHITECTURE.md`, `docs/BACKEND_DESIGN.md`, `CLAUDE.md`. The "RAG architecture", "context-engineering", "evaluation" and "cost-aware LLM pipeline" skills referenced in requests are not installed in this environment.

---

## 1. Principles

1. **The cheapest sufficient method wins:** deterministic code → rules → embeddings/retrieval → small model (T1) → strong model (T2). Multimodal models only where the input is media. A stronger model is not assumed to be better; routing choices that cannot yet be justified are experiments (§13).
2. **Every AI call has an inventory ID** (§3). A call without one fails code review.
3. **AI output is stored, then applied** by a deterministic stage that never calls a model (`BACKEND_DESIGN.md` §8).
4. **No reprocessing of unchanged content:** results keyed by `(source_item_id, content_hash, pipeline, prompt_version)`.
5. **The model classifies language; code decides facts.** Dates, directions, owners' identities, priorities, reminders and dedupe are computed by code from model-labelled spans (§5.4, §5.5).
6. **Smallest sufficient context** per call (`CONTEXT_ARCHITECTURE.md` §9).
7. **Structured outputs everywhere**, followed by deterministic validation.
8. **Provenance on every AI-derived object** (§5.1); **answers distinguish** source facts, inferences, user-authored information and recommendations, and **abstain** when evidence is insufficient (§5.7).
9. **User state is never overridden by AI** (§15).
10. **Optimize quality × reliability ÷ (cost, latency)**, judged on the frozen golden dataset and online signals (`AI_EVALUATION.md`).

---

## 2. Model classes and registry

| Class | Default model | Fallback | Thinking | Used for |
|---|---|---|---|---|
| T1 small | `gemini-3.1-flash-lite` | `gemini-3.5-flash-lite` | minimal (low for AI-06) | High-volume extraction, planning, lookup answers, on-demand thread summaries |
| T2 strong | `gemini-3.8-flash` | `gemini-3.5-flash` | low / medium | Synthesis across sources, meeting extraction, reply guidance, suggested meeting asks, adjudication (if enabled) |
| Speech (multimodal) | `gemini-3.5-transcribe` | `gemini-3.1-flash-lite` with audio input | — | Meeting transcription with diarization |
| Embedding | `gemini-embedding-2`, 768 dimensions | — | — | Chunks, item dedupe vectors, queries, project-hint matching |
| Judge (offline) | `gemini-3.1-pro-preview` | `gemini-3.8-flash` (high) | high | Evaluation only |

`config/models.yaml` (repository root; loading and validation: `BACKEND_DESIGN.md` §5.5) maps **roles** to inventory ID, model, thinking level, temperature, max output tokens, fallback and an `enabled` flag. Code references roles only. Registry changes are major changes (§12). Model IDs come from Google's pages on 2026-10-02; the exact `gemini-embedding-2` ID is verified by the slice 0.4 live smoke test (it appears with and without `-preview`). Until that test has run with a key, every model ID in the registry is marked `verified: false`.

**Generation settings:** temperature 0 for extraction, classification, planning, adjudication and answers; 0.3 for reply drafts. Structured output (JSON Schema from Pydantic) for every role.

---

## 3. AI call inventory

Unit and daily costs: `AI_COST_MODEL.md` §3–§4 (heavy-user calls/day shown here).

| ID | Role | Operations (§4) | Class | Trigger | Status | Heavy calls/day |
|---|---|---|---|---|---|---|
| AI-01 | `email_extract` | O1 classification, O2 gist, O3 tasks, O4 deadline phrases, O5 commitments, O6 body mentions, O7 priority features | T1 | Relevant email reaches `extract_pending` | Active | 90 |
| AI-02 | `adjudicate` | O5 ambiguous commitments | T2 medium | Apply finds an ambiguous candidate with provisional priority ≥ 60 | **Disabled until experiment X1 shows lift** | 0 (5 if enabled; cap 20) |
| AI-03 | `thread_summary` | O2 long-thread summary | T1 | Thread with ≥ 8 relevant messages gets a new one (debounced 10 min), or user presses "Summarize" | Active, on demand | ~5 |
| AI-04 | `embed` | Indexing, dedupe vectors, query embeddings, project-hint matching | Embedding | Chunk/item created or changed; chat query | Active | ~120 K tokens |
| AI-05 | `plan_query` | O10, O12 | T1 | Question not classified by rules | Active | 10 |
| AI-06 | `answer_lookup` | O10 transcript lookups; O12 questions combining filters that templates cannot render | T1 low | Lookup intent needing language over retrieved text | Active | 3 |
| AI-07 | `answer_synthesis` | O12 synthesis, O10 synthesis, O11 cross-meeting, "what changed", project status | T2 low | Synthesis intent | Active | 6 |
| AI-08 | `reply_guidance` | O13 | T2 low | User requests a reply draft | Active | 1 |
| AI-09 | `transcribe` | O8–O9 input | Speech | Recording audio prepared | Active | 2 h/week |
| AI-10 | `meeting_extract` | O8, O9, speaker-mapping proposals, O11 status signals | T2 medium (X4 tests T1) | Transcript stored | Active | 2/week |
| AI-11 | `meeting_asks` | O11 suggested questions for a meeting | T2 low | User opens a meeting's prep view and the deterministic sections contain open items or unresolved questions | Active, lazy | ~1.5 |
| AI-12 | `briefing_headline` | — | — | — | **Retired**: headline is a deterministic template | 0 |
| AI-13 | `session_summary` | — | — | — | **Retired**: session context = last 4 turns + entity focus map | 0 |
| AI-14 | `judge` | Evaluation | Judge | Offline | Active (offline) | — |

Retired and disabled IDs are kept so references and history stay unambiguous.

**Avoided by design:** per-message summary calls (gist from AI-01), LLM thread summaries for short threads (deterministic gist timeline), LLM list answers (deterministic rendering), person-card blurbs, digest narratives, LLM priority, LLM reminders, LLM briefing text, Batch API import in the MVP.

---

## 4. Operations catalog and method decisions

### 4.1 Decision matrix

"Primary" marks the method that produces the result.

| Operation | Deterministic | Rules | Embeddings | Retrieval | T1 | T2 | Multimodal | Async |
|---|---|---|---|---|---|---|---|---|
| O1 Email classification | assist | **primary** prefilter | — | — | **primary** for relevant mail (AI-01) | — | — | yes |
| O2 Email summarization | **primary** counts, gist timeline | thread-length gating | — | — | gist (AI-01); long threads (AI-03) | — | — | yes |
| O3 Task extraction | **primary** type/direction mapping, grounding | — | dedupe | candidates | span + statement labelling (AI-01) | — | — | yes |
| O4 Deadline extraction | **primary** date resolution | calendar times, business days | — | — | `due_text` span only (AI-01) | — | — | yes |
| O5 Commitment extraction | **primary** direction mapping, grounding | strength rules | dedupe | candidates | statement labelling (AI-01) | AI-02 only if X1 passes | — | yes |
| O6 Person/entity extraction | **primary** identity | aliases, signatures, domains | project hints | — | mentions (AI-01) | speaker proposals (AI-10) | — | yes |
| O7 Priority scoring | **primary** formula | caps | — | — | 2 features (AI-01) | — | — | yes |
| O8 Meeting summarization | — | — | — | prior meetings | — | **primary** (AI-10) | AI-09 | yes |
| O9 Meeting task extraction | **primary** mapping, grounding, timestamps | — | dedupe | prior items | — | span labelling (AI-10) | AI-09 | yes |
| O10 Meeting Q&A | **primary** for structured lookups (items/decisions lists) | intent rules | — | scoped hybrid | transcript lookups (AI-06) | synthesis (AI-07) | — | no |
| O11 Cross-meeting reasoning | **primary** linking, change diff, prep sections | — | topic discovery | relational chains | — | synthesis (AI-07), asks (AI-11) | — | prep sections async; AI lazy |
| O12 Contextual chat | **primary** list answers, verification | intent rules | query embedding | typed retrievers | planner (AI-05), lookups (AI-06) | synthesis (AI-07) | — | no |
| O13 Reply guidance | packet assembly | — | — | thread + person + items | — | **primary** (AI-08) | — | no |
| O14 Daily briefing | **primary** (sections and headline templates) | thresholds | — | SQL | — | — | — | yes |
| O15 Reminder generation | **primary** | suppression, caps | — | — | — | — | — | yes |

### 4.2 Operation specifications

**O1 Email classification.** Rules first: bulk headers (`List-Unsubscribe`, `Precedence: bulk`, `Auto-Submitted`), no-reply senders, provider categories mapped to neutral `promotions/social/updates`, calendar invitations, self-copies, known notification domains. **Outbound mail written by the user is never prefiltered** (needed for "What did I promise?"), except auto-replies. VIP senders and threads where the user has replied bypass the prefilter. AI-01 returns triage fields for relevant mail. If triage is missing (AI failure), a deterministic `needs_reply` heuristic applies (user in To, sender is a known person, message contains a question or request pattern, user has not replied) and the result is labelled "unanalysed".

**O2 Email summarization.** Daily counts are deterministic. Each relevant message gets a ≤ 20-word gist from AI-01. A thread's summary is the deterministic **gist timeline** (date, sender, gist) for up to 8 messages. AI-03 runs only for threads with ≥ 8 relevant messages or when the user presses "Summarize"; it is regenerated from clean messages (no summary-of-summary).

**O3 Task, O4 deadline and O5 commitment extraction.** One AI-01 call labels statements with `statement_kind`, speaker, owner reference, `due_text` and evidence. Code maps statements to types and directions (§5.5), resolves dates (§5.4), checks grounding and merges with candidates. AI-02 is disabled until X1.

**O6 Person/entity extraction.** Identity from headers and attendees (normalized email); organizations from non-public domains; aliases from display names; role/title from signature rules (marked inferred). AI-01 `mentions` are resolved by code against aliases (trigram) and project names (trigram + embedding); unresolved mentions stay unresolved. Persons merge only on shared email or user action.

**O7 Priority scoring.** Deterministic formula (`TECHNICAL_DESIGN.md` §12.6) with `request_type` and `business_impact` from AI-01.

**O8–O9 Meetings.** ffmpeg extracts audio; AI-09 transcribes the whole recording in **one call** (≤ 3 h); if the output limit is hit, the job falls back to 60-minute windows and AI-10 receives per-window speaker labels to reconcile. AI-10 produces summary, topics, concerns, decisions, open questions, items, status signals and speaker-mapping proposals with segment-level evidence. Speaker mapping: deterministic attendee/name matching first; AI-10 proposals auto-apply only at confidence ≥ 0.9.

**O10 Meeting Q&A.** Questions answerable from the meeting's structured extraction ("Who owns…?", "What deadlines…?", "What did we decide?") are rendered deterministically with citations. Questions needing transcript wording ("What did John say about security?") use scoped hybrid retrieval + AI-06. Synthesis ("What concerns were raised?") uses AI-07.

**O11 Cross-meeting reasoning.** Prep view sections (purpose, what changed since the last related meeting, open items both directions, unresolved questions, deadlines before the meeting) are deterministic and precomputed. AI-11 adds 3–5 suggested asks only when the user opens the prep view and there is something to ask about. Chat questions use AI-07 over relational chains (`CONTEXT_ARCHITECTURE.md` §10.4–10.5).

**O12 Contextual chat.** Pre-parse → AI-05 if needed → typed retrievers → abstention pre-check (§5.7) → deterministic rendering for list intents (waiting-for, promised, overdue, deadlines, who is waiting on me, needs response, agenda, day view) → AI-06 lookups or AI-07 synthesis → verification.

**O13 Reply guidance.** Thread gist timeline (or AI-03 summary), last three messages, open items between the user and the person, relevant decisions → AI-08. Never sent; copy only.

**O14 Daily briefing.** Entirely deterministic: sections from SQL, headline from a template ("3 things need your attention today: …"). AI-12 retired.

**O15 Reminders.** Deterministic rules and templates (`TECHNICAL_DESIGN.md` §15).

---

## 5. Structured outputs, provenance and grounding

### 5.1 Provenance envelope (every AI-derived object)

```json
"provenance": {
  "source": [{"source_item_id": "uuid", "kind": "message|meeting|recording|calendar_event", "occurred_at": "…"}],
  "confidence": 0.86, "confidence_band": "high",
  "derived_at": "2026-10-01T09:14:03Z",
  "extraction_method": "llm|llm_adjudicated|rule|deterministic|embedding_match|transcription",
  "model": "gemini-3.1-flash-lite | null",
  "prompt_version": "email_extract/v3 | null",
  "extraction_id": "uuid | null",
  "evidence": [{"evidence_id": "uuid", "quote": "…", "char_start": 120, "char_end": 188, "start_ms": null, "end_ms": null}]
}
```

| Question | Answered by |
|---|---|
| Where did this come from / which source record? | `source[]` → `source_items` (provider deep link via `GET /api/v1/sources/{id}`) |
| Which evidence supports it? | `evidence[]` (verbatim quotes with offsets/timestamps) |
| Which model / prompt version? | `model`, `prompt_version`, `extraction_id` → `extractions` row with raw output |
| When was it produced? | `derived_at` (per-change times in `context_events.recorded_at`) |
| Which method? | `extraction_method` |
| What confidence? | `confidence`, `confidence_band` (§5.3) |

An object without a source and (for LLM methods) at least one evidence span is not created. Later changes carry their own provenance in `context_events`. Storage: `BACKEND_DESIGN.md` §17.1.

### 5.2 Email extraction (AI-01) output schema

```json
{
  "gist": "string (≤ 20 words)",
  "triage": {"category": "action|fyi|scheduling|newsletter|notification|personal|other",
             "needs_reply": true, "request_type": "reply|review|approve|decide|provide_info|none",
             "urgency_signals": ["deadline", "explicit_urgency", "escalation"],
             "business_impact": "low|medium|high", "impact_reason": "string", "confidence": 0.0},
  "statements": [{
      "statement_kind": "promise|request|acceptance|report_commitment|report_request|assignment",
      "action": "string (imperative, ≤ 12 words)",
      "speaker": "sender",
      "owner_ref": "speaker|self|sender|recipient:<email>|name:<text>|unknown",
      "beneficiary_ref": "self|sender|recipient:<email>|name:<text>|unknown|null",
      "due_text": "string|null (verbatim span)",
      "due_iso_guess": "string|null (diagnostic only, never stored as a date)",
      "is_deadline_only": false,
      "in_forwarded_content": false,
      "forwarded_author_ref": "email|name|null",
      "candidate_id": "C3|null",
      "confidence": 0.0,
      "evidence_quote": "string (verbatim)"}],
  "status_signals": [{"candidate_id": "C3",
      "signal": "progress|completed_claim|delay|new_deadline|cancelled|resolved|accepted|declined",
      "new_due_text": "string|null", "evidence_quote": "string", "confidence": 0.0}],
  "decisions": [{"kind": "decision|open_question", "statement": "string", "evidence_quote": "string", "confidence": 0.0}],
  "project_hint": "string|null",
  "mentions": [{"surface_text": "string", "type": "person|organization|project"}]
}
```

The candidate list (`C1…C8`) sent in the prompt is stored with the extraction (`extractions.candidate_map`: candidate ID → entity type, entity ID, version), so apply resolves `candidate_id` deterministically even if the entity changed or was merged in between (`BACKEND_DESIGN.md` §8.1).

### 5.3 Confidence computation

Model self-reported confidence is poorly calibrated, so it is only one input:

1. **Before calibration data exists (Phase 1):** `confidence = min(0.95, model_conf × Π penalties)` with penalties: fuzzy (not exact) grounding × 0.85; owner unresolved × 0.6; code/model date disagreement × 0.8; `report_*` statement × 0.8; forwarded content × 0.8; model `owner_ref` inconsistent with the direction mapping × 0.6.
2. **After `golden-v1.0` exists:** per-role isotonic calibration fitted on the `dev` split over the features above plus sender type and candidate linkage; validated by expected calibration error on `test` (`AI_EVALUATION.md` E14). The calibration map is versioned with the prompt.

Bands: `low` < 0.6 ≤ `medium` < 0.8 ≤ `high`. Visibility, reminders and promotion thresholds use the calibrated value (`CONTEXT_ARCHITECTURE.md` §12.5).

### 5.4 Dates are never invented

- `due_at` is set **only** by the deterministic resolver from `due_text`, which must occur verbatim in the evidence quote. Reference time: the source's `occurred_at` (meeting start for transcripts) in the speaker's timezone if known, else the user's.
- `due_iso_guess` is diagnostic: if it disagrees with the resolver, confidence is penalized; it is never stored as `due_at`.
- Unparseable or vague phrases ("soon", "shortly") → `due_at = null`, `due_precision = fuzzy`, `due_text` kept; fuzzy deadlines never trigger hard-deadline reminders.
- No `due_text` → no deadline, even if the model guessed one.
- Calendar dates (meeting times) come from calendar data, not models.

### 5.5 Statement-to-direction mapping (deterministic)

Code maps each statement to a work-item type and direction. Speaker = sender for email, resolved speaker for transcripts; `self` = the user's Person.

| `statement_kind` | Speaker | Owner (resolved) | Type | Direction | Max strength |
|---|---|---|---|---|---|
| `promise` | self | self | commitment | `my_commitment` | explicit |
| `promise` | P ≠ self | P | commitment | `waiting_for` if beneficiary is self or self is in To; else `observed` | explicit |
| `request` | P ≠ self | self (addressee) | request | `my_task` (requester P) | explicit |
| `request` | self | Q | request | `delegated` (requester self) | explicit |
| `request` | P | Q (both ≠ self) | request | `observed` | explicit |
| `acceptance` | owner of a candidate request | — | status signal `accepted`: request → commitment (direction kept: `my_commitment` or `waiting_for`) | — | explicit |
| `report_commitment` ("John will send…" said by someone other than John; "waiting for John" said by self) | S | T ≠ S | commitment | `waiting_for` if beneficiary is self (or S = self); else `observed` | **probable** |
| `report_request` ("John asked me to do X" said by self) | self | self | request | `my_task` (requester John) | **probable** |
| `assignment` ("Priya will own the rollout" in a meeting) | S | T | task | `my_task` if T = self; `delegated` if S = self; else `observed` | probable unless T confirms |
| any | unresolved speaker (unmapped meeting voice) | — | as above | **`unresolved`** (hidden from personal lists until mapping confirmed) | probable |

Consequences for the four canonical phrases:

| Phrase | Said by | Result |
|---|---|---|
| "John will send X" | John | commitment, owner John, `waiting_for` |
| "John will send X" | Sarah | commitment, owner John, `waiting_for`/`observed`, strength ≤ probable |
| "I will send X" | user | commitment, owner user, `my_commitment` |
| "waiting for John" (on X) | user | commitment, owner John, `waiting_for`, strength probable |
| "John asked me to do X" | user | request, owner user, requester John, `my_task`, strength probable |

They cannot collapse into the same type/direction because the mapping depends on `statement_kind`, speaker and owner, which are validated against participants. The contrastive dataset `DIR-120` tests every row (`AI_EVALUATION.md` §4.3). Forwarded content: statements inside forwarded blocks are attributed to `forwarded_author_ref` when resolvable, capped at `probable`, and never produce `my_commitment` for the forwarder.

### 5.6 Meeting extraction (AI-10) output schema

Same `statements`, `status_signals` and `decisions` shapes as §5.2 with `speaker` = speaker label and evidence `{segment_seq, start_ms, end_ms, quote}`, plus `summary` (≤ 150 words), `topics[]`, `concerns[]` and `speaker_mapping[] {label, person_email|name, confidence, quote}`.

### 5.7 Answers: claim kinds, grounding and abstention

Answer schema (AI-06, AI-07; AI-08 adds `context`, `previous_agreement`, `current_status`, `draft`; AI-11 returns `asks[]` with the same claim structure):

```json
{"answerable": true,
 "answer_markdown": "string",
 "claims": [{"text": "string", "citations": ["S3"],
             "kind": "source|user|inference|recommendation|absence"}],
 "confidence": "low|medium|high",
 "missing_info": "string|null"}
```

| Kind | Meaning | UI rendering |
|---|---|---|
| `source` | Directly supported by cited source evidence | Plain statement with source chips |
| `user` | User-authored or user-confirmed information (confirmed items, edits, preferences, notes) | "You confirmed / You set" label |
| `inference` | Derived from evidence but not stated (e.g., "likely delayed") | "Inferred" label |
| `recommendation` | AI suggestion (next action, reply draft, ask) | "Suggestion" label; never phrased as fact |
| `absence` | Statement that something was not found | Must cite `COVERAGE` with sync times |

**Deterministic grounding checks (online, every answer):**

1. Every citation ID exists in the packet.
2. Every date, number and proper name in a `source` claim appears in the rendered text of at least one cited item; otherwise the claim is relabelled `inference` and flagged.
3. `absence` claims must cite coverage; otherwise rewritten with coverage.
4. `user` claims must cite an item with `user_fields` or `verification_status ∈ {confirmed, user_created}`.
5. `recommendation` claims are rendered as suggestions regardless of model wording.
6. If no `source` or `user` claim survives for a factual question, the answer is replaced by the abstention template.
7. Answer-level `confidence` is capped at `medium` when any claim was relabelled.

**Abstention:**

- **Pre-check (no model call):** if the plan's anchor cannot be resolved, or retrieval returns no structured match and no chunk above the minimum fused score, the system returns: "The available context does not establish this." plus coverage (sources searched, windows, sync times) and the closest related items if any.
- **Model-level:** the model sets `answerable = false` when the packet does not support an answer; the same template is rendered with `missing_info`.

### 5.8 Phase 2 implementation details (decided 2026-10-02)

**AI-05 `plan_query` output (`plan_query.v1`).** `{intent, person_names[≤ 3], topic (≤ 120 chars) | null, time_expression, since_date | null, needs_clarification, confidence}`. `intent` is one of the code-defined retrievers: `waiting_for`, `promised`, `who_waiting_on_me`, `needs_response`, `overdue`, `deadlines`, `person`, `topic_status`, `project`, `day_view`, `what_changed`, `next_action`, `email_context`, `unsupported`. `time_expression` is one of `today`, `yesterday`, `this_week`, `last_week`, `recently`, `since_last_meeting`, `since_date` or null. Rules run first (keyword and pattern rules, alias lookup for names); AI-05 runs only when no rule decides the intent. Its output only selects a retriever and its parameters; it never produces SQL. If AI-05 is unavailable, the question is routed to `topic_status` with the whole question as the topic (planner `fallback`).

**Routing (default until experiment X6).** Deterministic rendering, no AI call: `waiting_for` (S7), `promised` (S8), `needs_response` (S9), `who_waiting_on_me`, `overdue`, `deadlines`, `day_view` (S10), `email_context` (S1 panel). AI-06 (T1): `person` (S2), and list intents that carry a topic qualifier the templates cannot express ("What did I promise about the budget?"). AI-07 (T2): `topic_status` (S6), `project` (S3), `what_changed` (S11), `next_action` (S12). AI-06 escalates once to AI-07 when no `source` or `user` claim survives the grounding checks and `missing_info` is set. The extra retrieval round of §6.4 is not built in Phase 2.

**AI-06 / AI-07 output (`answer.v1`).** The §5.7 schema: `answer_markdown` ≤ 4,000 characters, at most 20 claims of ≤ 400 characters, citations matching `S<n>` or `COVERAGE`.

**Grounding checks (§5.7), exact rules.**
- Rule 2 compares normalized tokens: lower case; month and weekday names reduced to their first three letters; ordinal suffixes removed (`8th` → `8`); numbers without leading zeros. Checked tokens of a `source` claim: every number, month, weekday and capitalized word that is not a common function word. Packet cards render dates as `Thu 8 Oct 2026` (plus time and zone for datetimes), so a claim's date can be matched. A claim with any unmatched token is relabelled `inference` and flagged.
- The displayed answer is rendered from the verified claims (one sentence per claim, labelled by kind, with its citation chips); the model's `answer_markdown` is not shown, so a relabelled claim can never appear as fact.
- Absence claims without `COVERAGE` get the coverage citation and the coverage sentence appended.
- Abstention template: "The available context does not establish this." + the coverage sentence + up to 3 closest related items. Degraded template (model unavailable, budget cap): "A written answer is not available right now." + the deterministic list of retrieved items with citations.
- An answer whose only surviving claims are `absence` claims (with coverage) is shown, not replaced by the abstention template: "no completion evidence, searched … synced …" is the qualified absence of §5.7 rule 3.

**Claim kinds of deterministic answers.** An item that is user-created, confirmed or has user-set fields → `user`; an AI-derived item with `commitment_strength = explicit` → `source`; other AI-derived items → `inference` (rendered "possible" when the confidence band is low); a computed reply state ("awaiting your reply") → `source` citing the conversation card.

**Answer provenance.** `chat_messages.provenance` stores `{source: [source refs of cited items], confidence_band, derived_at, extraction_method: llm | deterministic, model, prompt_version, ai_call_ids, evidence: [evidence IDs]}`. Answers report a band, not a number, so the `Provenance` model of §5.1 (numeric confidence) is not used for answers.

### 5.9 Phase 3 implementation details (decided 2026-10-03)

**AI-03 `thread_summary` (`thread_summary.v1`).**
- *Relevant message* = a live message of the thread with an applied AI-01 triage (prefiltered mail has none).
- *Trigger:* the debounced sweep for threads with ≥ 8 relevant messages whose newest relevant message is at least 10 minutes old, or the user's request (threads with ≥ 2 relevant messages). Never for a thread whose summary already covers its newest relevant message (`summary_through_message_id`), and not again for a message that failed (`summary_failed_through`).
- *Input* (no summary-of-summary): the newest 20 relevant messages, oldest first, each as `[Mn] date · sender · direction` and its clean body cut to 400 tokens (its AI-01 gist when the body was purged by retention), at most 6,000 tokens in total (older messages dropped first); every body is delimited untrusted data.
- *Output:* `{summary: string ≤ 900 characters, key_points: [{text ≤ 300 characters, messages: ["M3", …]}] (≤ 6)}`. Key points citing no existing `Mn` are dropped. The summary is narrative and always labelled "AI summary"; it never sets item state.
- *Provenance:* `conversations.summary_*` (`BACKEND_DESIGN.md` §17.6): method `llm`, model, prompt version, `derived_at`, AI call IDs, `covered_source_ids` (the source items of the included messages) and `summary_through_message_id`.
- *Calls:* primary, one retry, one fallback (the interactive policy of §7, because the job keeps no extraction row); after that the failure is recorded for that message and the gist timeline stays the thread summary (§14).

**Gist timeline** (O2): deterministic rendering of the newest 8 relevant messages: local date, sender label and the stored AI-01 gist (`messages.triage.gist`), labelled AI-derived. No model call.

**AI-08 `reply_guidance` (`reply_guidance.v1`).**
- *Packet* (budget: dynamic 4,000, hard 8,000 tokens; scenario `RG`): the S1 email-context retriever for the thread, the S2 person-context retriever for the newest inbound sender (fixed anchor), the gist timeline or the AI-03 summary, the last 3 messages (clean text, ≤ 400 tokens each, delimited), coverage, and the user's optional instructions as the question (delimited; user-authored notes are never packed).
- *Output:* `{answerable, context: [claim] (≤ 5), previous_agreement: [claim] (≤ 5), current_status: [claim] (≤ 5), draft: string ≤ 2,000 characters | null, confidence, missing_info}` with the §5.7 claim structure.
- *Checks:* the §5.7 grounding checks run on each section's claims (citations exist; dates, numbers and names of `source` claims appear in the cited items; `absence` cites coverage; `user` claims cite user-backed items; anything else is relabelled and flagged). The draft is a `recommendation`: shown as "Suggestion — draft for copying; review before sending", never as fact. Numbers, dates and capitalized names in the draft that appear in no packet item are listed as `draft_warnings` ("not found in the sources"); the draft is kept, so the user sees exactly what is unsupported. `answerable = false` or no surviving `source`/`user` claim → the abstention template with coverage and no draft.
- *Degradation:* model failure or the hard budget cap → the deterministic context section (thread card, open items both directions, last message gist) without a draft, with a notice (§14).
- *Never sent:* no send, draft or calendar action exists (read-only scopes, PRD §51 Level 3 is outside the MVP); the result is stored only as the request's idempotency response for 24 hours.

---

## 6. Pipelines

### 6.1 Email

```text
sync → source_items → normalize + rules prefilter (code; outbound never prefiltered)
     → [relevant] extract: AI-01 (candidate_map stored) → apply (code): grounding, §5.4 dates, §5.5 mapping,
       §5.3 confidence, candidate resolution, merge, events, triage projection, gist
     → index: AI-04 (parallel)
     → long threads only: AI-03 (≥ 8 relevant messages or on request)
     → priority, reminders, briefing sections: code
```

### 6.2 Meetings

```text
upload → sha256 dedupe → ffmpeg (audio only) → AI-09 single call (window fallback) → transcript_segments
       → deterministic speaker matching → AI-10 (prior-meeting context; candidate_map stored)
       → apply (code) → AI-04 index → change diff (SQL) → prep sections (code)
```

### 6.3 Initial import (Batch API deferred)

30-day import on the standard API, throttled (`BACKEND_DESIGN.md` §11.6): last 72 hours first. The Batch API is deferred; the saving at pilot scale is quantified in `AI_COST_MODEL.md` §6. Batch remains allowed for offline evaluation.

### 6.4 Chat

```text
question → pre-parse (code) → [if needed] AI-05 → scope + retrieval (code, SQL, AI-04 query embedding)
         → abstention pre-check → list intent: deterministic rendering | lookup: AI-06 | synthesis: AI-07
         → grounding checks (§5.7) → SSE stream
         → at most one extra retrieval round if `missing_info` names a retrievable gap
```

---

## 7. Retry and attempt policy

| Error | Behaviour | Counts toward attempt cap |
|---|---|---|
| 429 / 5xx / timeout | Backoff with jitter (`BACKEND_DESIGN.md` §14.3); from attempt 3 the role's fallback model | yes |
| Schema invalid | One repair call with the validation error | yes |
| Safety block / empty response | `failed_permanent` with reason | yes |
| Budget exceeded | Deferred until the window resets | no |

**Attempt cap:** 4 model calls per `(source_item, content_hash, pipeline, prompt_version)` including repair; then `failed_permanent` and `needs_attention`. Interactive calls: one retry, one fallback, then degradation (§14). Retries cannot create duplicate state: extraction rows are unique per key and apply is once-only (`BACKEND_DESIGN.md` §8, §10).

---

## 8. Cost-aware routing strategy

### 8.1 Static routing

Each operation's default (§4.1) is the cheapest method that met the quality targets; where this is not yet established, the default is the conservative choice and an experiment (§13) decides.

### 8.2 Escalation (cascade, never broadcast)

| From | To | Condition | Cap |
|---|---|---|---|
| Rules prefilter skip | AI-01 | VIP sender, user previously replied in thread, or outbound by user | — |
| AI-01 candidate | AI-02 | Only if enabled after X1: `statement_kind ∈ {report_*}` or owner unresolved, **and** provisional priority ≥ 60 | 20/user/day |
| Rules intent | AI-05 | Pre-parse confidence below threshold | — |
| Deterministic list rendering | AI-06 | Question contains qualifiers templates cannot express | — |
| AI-06 | AI-07 | Grounding checks fail or `missing_info` requires synthesis | once per question |
| Deterministic prep sections | AI-11 | User opens prep view; sections non-empty | once per meeting version |
| AI-09 single call | Windowed AI-09 | Output limit reached | — |

### 8.3 Input-size routing

- Email bodies over 2.5 K tokens: deterministic truncation (first and last paragraphs plus paragraphs with dates, participant names or request patterns); never escalated because of length.
- Transcripts: full transcript to AI-10 (≤ 3 h audio, ≈ 60 K tokens of text worst case); experiment X10 checks quality on long meetings.
- Chat packets: per-scenario budgets (`CONTEXT_ARCHITECTURE.md` §9.6).

### 8.4 Degradation

Per-operation degradation is in §14. Budget values: `AI_COST_MODEL.md` §7.

### 8.5 Thinking levels

T1 roles: `minimal` (AI-06 `low`). T2: `low` for AI-07, AI-08, AI-11; `medium` for AI-10 and AI-02. Thinking tokens are billed as output and included in unit costs.

---

## 9. Skip rules (no AI call)

| Situation | Behaviour |
|---|---|
| Same source, same `content_hash`, `pipeline`, `prompt_version` | Stored extraction reused |
| Gmail label change (read, archive, star, category, Trash) | Metadata/visibility only (`BACKEND_DESIGN.md` §9.2) |
| Message deleted | Deterministic recomputation (`BACKEND_DESIGN.md` §9.3) |
| Calendar change | Deterministic meeting update; description change → re-embed only |
| Apply retry, re-fold, re-apply | No AI (§10) |
| Prompt version change | No automatic re-extraction; costed, approved job |
| Same recording uploaded again | Existing recording returned |
| Meeting re-extraction with a new prompt | AI-10 only; transcript reused; **no automatic re-transcription** (§15) |
| Dashboard, lists, people, projects, daily summary, briefing, reminders, list answers, structured meeting lookups | Deterministic |
| Thread with < 8 relevant messages | Gist timeline; no AI-03 |
| Meeting prep view not opened, or no open items/questions | No AI-11 |
| Abstention pre-check fails | No answer call |
| Prefiltered mail (non-VIP, not outbound) | No AI-01, no embedding |

---

## 10. Recomputation without AI

| Need | Mechanism | AI calls |
|---|---|---|
| Fold rules changed | Re-fold projections from `context_events` | 0 |
| Mapping, grounding, date, confidence-calibration or merge logic changed | Re-apply stored extractions (`BACKEND_DESIGN.md` §8.5) | 0 |
| Priority weights / reminder rules / briefing templates changed | Recompute | 0 |
| Prompt or model changed | Re-extract a chosen window after the frozen-dataset gate, costed and approved | AI-01/AI-10 for that window |
| Embedding model changed | Background re-embed | AI-04 |

Because statements (not final types) are stored, changes to the §5.5 mapping are applied by re-apply without new AI calls.

---

## 11. Prompts

- Versioned `eca/intelligence/prompts/<role>/v<N>.md` with Pydantic schema `eca/intelligence/output_schemas/<role>.py` (provider layer location: `BACKEND_DESIGN.md` §5.4); `prompt_version` and `schema_version` on every `extractions` and `ai_calls` row.
- Order: stable instructions and schema → user card → candidates or packet → delimited untrusted content → question.
- Untrusted content is declared as data; no side-effecting tools (`TECHNICAL_DESIGN.md` §17.6).
- Up to 3 of the user's recent rejections from the same sender as negative examples in AI-01 (deterministic selection). Few-shot examples come only from the `dev` split.
- Phase 2 prompts and schemas: `plan_query/v1.md` with `output_schemas/plan_query.py` (`plan_query.v1`); `answer_lookup/v1.md` with `output_schemas/answer_lookup.py` and `answer_synthesis/v1.md` with `output_schemas/answer_synthesis.py` (both `answer.v1`). AI-04 has no prompt file; its input format is the version string `embed/v1` (`CONTEXT_ARCHITECTURE.md` §9.10). Interactive calls (AI-05, AI-06, AI-07) follow the interactive attempt policy of §7: primary, one retry, one fallback call, then degradation. Phase 3: `thread_summary/v1.md` with `output_schemas/thread_summary.py` (`thread_summary.v1`) and `reply_guidance/v1.md` with `output_schemas/reply_guidance.py` (`reply_guidance.v1`); both use the interactive policy (§5.9).

---

## 12. Change management

A **major change** is any change to a production prompt or schema, model ID or fallback, thinking level, temperature, role enablement (e.g., AI-02), routing or escalation threshold, candidate rules, packet budgets, retrieval ranking, statement mapping, confidence calibration, priority weights or prefilter rules. Major changes pass the frozen-dataset scorecard (`AI_EVALUATION.md` §8) including cost (`AI_COST_MODEL.md` §9).

---

## 13. Routing experiments (run on the frozen dataset; results recorded in `evals/ai/reports/`)

| ID | Question | Arms | Decision rule | When |
|---|---|---|---|---|
| X1 | Does T2 adjudication improve ambiguous commitments? | AI-01 only vs AI-01 + AI-02 on the escalation slice | Enable AI-02 if explicit-commitment precision +≥ 3 points or recall +≥ 5 points without precision loss, within `AI_COST_MODEL.md` §9 | Slice 1.4 |
| X2 | Which T1 model for AI-01? | `gemini-3.1-flash-lite`, `gemini-3.5-flash-lite`, `gemini-3.8-flash` | Scorecard; cheapest within tolerance of best | Slice 1.4 |
| X3 | Are gist timelines enough for thread context? | Gist timeline vs AI-03 for threads of 3–8 messages | Keep timeline if E12 coverage and S1 answers within tolerance | Phase 3 |
| X4 | Is T2 needed for meeting extraction? | AI-10 on T2 vs T1 | Switch to T1 if E11 within tolerance | Slice 4.3 |
| X5 | Transcription model and windowing | transcribe vs Flash-Lite audio; single call vs windows | WER/DER and cost scorecard | Slice 4.2 |
| X6 | Chat routing per intent | Deterministic render vs AI-06 vs AI-07 | Minimal route meeting E10 rubric (E16 routing efficiency) | Slice 2.4 |
| X7 | Is the planner needed? | Rules only vs rules + AI-05 | Keep AI-05 only if routing accuracy +≥ 5 points | Slice 2.4 |
| X8 | Model for meeting asks | AI-11 on T2 vs T1 | Cheapest within rubric tolerance | Slice 4.4 |
| X9 | Confidence calibration | Penalty formula vs isotonic calibration | Lower expected calibration error | End of Phase 1 |
| X10 | Long transcripts | Full transcript vs per-hour extraction merged by code | E11 on > 90-minute meetings | Slice 4.3 |

---

## 14. Failure and degradation per operation

| Operation | Failure | Degraded behaviour (never fabricate) |
|---|---|---|
| O1/O3–O7 (AI-01) | T1 down, timeout, malformed after repair | Message stored and shown "unanalysed"; deterministic `needs_reply` heuristic; no items created; retried with backoff and fallback model; reconciler resumes |
| O2 | AI-03 down | Gist timeline (always available once AI-01 ran); otherwise snippets |
| O5 (AI-02 if enabled) | T2 down | Provisional item stays `suggested`, band `low`, "please confirm" |
| O6 | Embeddings down | Trigram matching only; project hints unresolved until re-run |
| O7, O14, O15 | — (deterministic) | Missing AI features → priority computed without them (feature weight 0) and reason "not analysed" |
| O8/O9 (AI-09) | Transcription down | Recording `failed` at stage; user may upload a transcript file instead; retry later |
| O8/O9 (AI-10) | T2 down | Transcript searchable (chunks); meeting shows "summary pending"; retry |
| O9 | Speaker unmapped | Items `unresolved` direction; confirmation prompt |
| O10, O12 (AI-06/AI-07) | Model down or timeout | Deterministic answer for list/lookup intents; for synthesis, structured lists of the retrieved items with citations and a notice that a written answer is unavailable |
| O10–O13 | Embeddings down | FTS + structured retrieval only; coverage notes reduced recall |
| O11 (AI-11) | T2 down | Deterministic prep sections only |
| O12 | Insufficient evidence | Abstention template with coverage (§5.7) |
| O12 | Stale sources (sync lag, `needs_reauth`) | Answer includes coverage gap; absence claims forbidden without caveat |
| O13 (AI-08) | T2 down | Context section (deterministic) without draft |
| All | Provider outage | Background queues back off; interactive paths degrade as above; existing context unaffected |
| All | Rate limit | Backoff honouring `Retry-After`; interactive requests get a short retry then degrade |
| All | Partial processing | Stage states visible; partial results labelled; reconciler completes later |
| All | Budget cap | Routes in `AI_COST_MODEL.md` §7 |

---

## 15. User state and AI recomputation

| User state | Guarantee | Mechanism |
|---|---|---|
| User-created tasks | Never merged, archived, rejected or re-typed by AI; AI evidence may attach only as `reported_status` | `origin = user`; merge requires user action |
| User-edited fields (deadline, owner, title, type) | Never changed by model, system or time events | `user_fields` + authority 5 in fold; conflicts shown as prompts |
| User priorities | `priority_override` wins over computed score | Formula reads override first |
| User dismissals (reminders, rejected items) | Not re-sent / not re-suggested without material change or new explicit owner evidence | Reminder fingerprints; rejected-item candidates |
| User corrections (merges, aliases, speaker mappings, project assignments) | Survive re-apply and re-extraction | User events never deleted; re-apply keeps user-touched IDs |
| User-authored notes | Never sent to models as instructions; never edited by AI | `work_items.notes` and `decisions.notes` (user-authored) |
| Confirmed speaker mappings | Survive meeting re-extraction; **re-transcription is never automatic** because diarization labels change; an operator-approved re-transcription keeps the old transcript version for existing evidence and asks the user to re-confirm mappings | `transcript_segments.transcription_model` versioning |

Recomputation (R1 re-fold, R2 re-apply, R3 re-extract) never deletes user events, keeps every user-touched item ID, and the fold gives authority-5 user events precedence over model, system and time events regardless of order, so user state always wins (`BACKEND_DESIGN.md` §8.5, `CONTEXT_ARCHITECTURE.md` §8).
