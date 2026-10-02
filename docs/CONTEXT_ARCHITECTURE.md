# Context Architecture — Executive Context Assistant

**Status:** Reconciled architecture (pre-implementation)
**Date:** 2026-10-02
**Authority:** This document is authoritative for the context hierarchy and vocabulary, context lifecycle, status fold, temporal model, retrieval scenarios and context-packet assembly. Transactions, events, schema DDL and deletion are defined in `BACKEND_DESIGN.md`; AI calls in `AI_PIPELINE.md`.
**Inputs:** `docs/PRD.md` v1.0, `docs/TECHNICAL_DESIGN.md`, `docs/BACKEND_DESIGN.md`, `docs/AI_PIPELINE.md`, repository inspection
**Companion:** `docs/CONTEXT_EVALUATION.md` (tests for everything specified here)

> Note on inputs: the request referenced a "context-engineering skill" and a "RAG architecture skill". Neither exists in this repository, in the user's skill directory, or in the installed plugins. This document relies on the PRD, the technical design, and the official Google documentation already cited in `TECHNICAL_DESIGN.md`.

---

## 1. Summary

The product's differentiator is **context continuity**: when Monday's meeting, Tuesday's email and Wednesday's question are about the same obligation, the system must know that, know the obligation's *current* state, and say so with sources (PRD §11, §24, §43).

This document audits `TECHNICAL_DESIGN.md` for context management and specifies the context model in detail. Conclusions:

1. **The core design holds.** A relational model of entities and open obligations, with append-only event timelines and evidence spans, is the right backbone. Most context questions are *state* questions ("what is open, who owes it, what changed") and are answered exactly by indexed SQL, not by similarity search.
2. **The MVP does not need a knowledge graph database.** The user's work context is an **ego-network**: almost every path runs through the user, one person, one meeting or one item. All 12 required scenarios need traversals of at most two hops over typed, indexed foreign keys. That is a SQL join, not a graph query (Section 6).
3. **Temporal retrieval must be first-class**, not an afterthought filter. "Yesterday", "what changed", "since the last meeting", "as of last sync" and "latest status" are the dominant query shapes. The design needs a unified change feed, user checkpoints, bi-temporal timestamps and explicit temporal semantics (Section 7).
4. **The audit found 16 gaps** (Section 3). Five are high severity: no change feed for "What changed?", no coverage model for negative claims ("no completion evidence"), no defined way to compute an item's current status from its timeline, no index of entity mentions beyond email headers, and weak project context before projects are confirmed. Each has a concrete, low-complexity fix.
5. **Retrieval mix:** relational and temporal retrieval answer the majority of scenarios on their own; hybrid (vector + keyword) retrieval is used for *discovery* (finding which entity or item a free-text topic refers to) and then hands off to relational *expansion*. Semantic-only retrieval is never used alone for state questions.

---

## 2. Design principles for context

1. **State before text.** Store obligations, decisions and relationships as rows with status. Retrieve rows first; retrieve text only to support or explain them.
2. **Discover semantically, expand relationally.** Use hybrid search to *find the anchor* (an item, person, project or meeting); use typed joins to *gather its context*.
3. **Time is a primary key of meaning.** Every fact has *when it happened* (`occurred_at`) and *when we learned it* (`recorded_at`). Every answer is "as of" a time.
4. **Latest authoritative state wins, history is kept.** Conflicts are recorded as events and surfaced, never silently overwritten.
5. **Absence must be qualified.** "No update found" is only stated together with what was searched and how fresh the sources are.
6. **Budget by usefulness.** Pack the fewest tokens that let the model answer correctly; measure the share of packed context the answer actually uses.
7. **Deterministic assembly.** Same inputs produce the same context packet, so answers are reproducible, cacheable and testable.

---

## 3. Audit of `TECHNICAL_DESIGN.md` (context management)

Severity: **H** = blocks a PRD success criterion or risks wrong answers; **M** = degrades relevance or continuity; **L** = polish.

| # | Sev | Finding | Effect | Fix (specified in this document) |
|---|---|---|---|---|
| A1 | H | No unified change feed. `item_events` covers work items only; new decisions, reply-state changes, meeting processing, person first-seen and connection failures are not events. No "last seen" checkpoint. | "What changed?" (PRD §25, §58) and the three-day-return test cannot be answered reliably. | Generalize `item_events` into `context_events` for all entity types; add `user_checkpoints`; generate synthetic time-driven events (became overdue, became stale). §7.3–7.4 |
| A2 | H | No coverage model. The answer contract cannot distinguish "no completion evidence exists" from "we have not synced since yesterday" or "Gmail is disconnected". | False negative claims ("John has not sent it") — a correctness failure the PRD explicitly calls out (§11 expected response). | Every context packet carries a `coverage` block: sources searched, time range, last successful sync per source, connection status. Negative claims must cite it. §9.4 |
| A3 | H | Current status of an item is undefined. `lifecycle_status` is user-controlled; model status signals live in events; nothing defines how "John says still pending" is shown versus "open". | Inconsistent status answers; risk of a model signal being presented as fact. | Define the **status fold**: `lifecycle_status` (authoritative) plus `reported_status` (latest signal with source) plus due-date history and conflicts. §8 |
| A4 | H | Person and project mentions are only linked through email headers and attendee lists. "Sarah said the contract is late" inside a transcript or body text does not link to Sarah or to the contract project. | Person and project context miss the most informative sources. | Add `entity_mentions` (source span → entity, confidence) populated by extraction and deterministic name matching. §5.2 |
| A5 | H | Project context is empty early: projects are suggested only after three sources share a hint and need user confirmation. | Scenario "current project context" fails for weeks. | **Topic mode**: when no confirmed project matches, retrieve by hybrid search on the name, cluster by thread/meeting, label the result "inferred grouping". `project_hint` is stored on items and threads immediately. §10.3 |
| A6 | M | `daily_digests` is referenced in the context layers but not defined in the schema. | "What happened yesterday?" has no defined basis. | Define the **day view**: a structured, on-demand SQL query over `context_events`, meetings and conversations for one local day (no stored digest table in the MVP; materialize only if latency requires). §10.10 |
| A7 | M | No cross-thread linking. A new email thread that continues an old topic ("Re-sending: security docs") is a new conversation with no relation. | Cross-email reasoning depends entirely on semantic search at question time. | At ingest, link a new thread to a prior thread when participants overlap ≥ 50% and subject/body similarity ≥ threshold within 30 days (`entity_links.relation = continues`). §10.6 |
| A8 | M | Open questions (`decisions.kind = open_question`) are not in the candidate list for email status signals, only work items are. | An email that resolves "authentication strategy" does not close the open question; next meeting prep shows it as unresolved. | Candidate list for every extraction includes open questions and recent decisions touching the same participants/project. §10.5 |
| A9 | M | Rolling thread summaries are built incrementally from the previous summary. Repeated summarization drifts and loses early facts. | Stale or wrong narrative in thread context. | Replaced after the AI review: deterministic gist timelines for short threads; AI-03 only for long threads, always regenerated from clean messages (no summary-of-summary); summaries never supply facts. §11.3 |
| A10 | M | Temporal semantics undefined: "last week", "yesterday" near midnight, user timezone versus sender timezone, working days. | Wrong windows, wrong "yesterday" for travellers. | Temporal specification. §7.1 |
| A11 | M | "Persistent context" mixes three different things: open obligations, entity knowledge and user preferences. They need different priorities and freshness. | Budget trimming may drop open obligations before stale entity facts. | Refined hierarchy: Active State, Entity Memory, Preferences as separate tiers. §4 |
| A12 | M | Rejected items can be re-extracted from a later mention and reappear as new suggestions. | User corrections appear to be ignored (PRD §57.22). | Deduplicate new candidates against rejected items too; re-surface only on new **explicit** evidence from the owner, labelled "previously dismissed". §12.3 |
| A13 | M | Uploaded meetings without a calendar link have no attendees, so series detection and attendee-overlap retrieval fail. | Cross-meeting reasoning breaks for the "missed meeting" journey. | Participants derived from speaker mapping; upload flow asks to link an event; series match falls back to participant overlap + title similarity. §10.4–10.5 |
| A14 | M | Hybrid search caps results per thread for diversity. For status questions this can cut the very chain the answer needs. | Missing the latest email in a chain. | Diversity caps apply to discovery only; once an anchor item is found, its full evidence chain is loaded relationally. §9.2 |
| A15 | L | Context packets have no defined order beyond "stable prefix first". Long packets suffer from mid-context neglect. | Lower answer quality at larger budgets. | Fixed packet layout: coverage and anchor facts first, timelines next, supporting quotes last, question at the end. §9.3 |
| A16 | L | Rendered cards are regenerated per request. | Possible latency. | Version every mutable entity (`version` column) for optimistic concurrency and prep-brief invalidation. Card rendering is deterministic and cheap; a rendered-card cache is **not** built in the MVP (complexity audit). §9.5 |

No finding requires changing the database technology, the queue, the model tiers or the connector boundary.

**Status:** all findings A1–A16 are applied in `BACKEND_DESIGN.md` (schema §17, events §7, flows §9) and `TECHNICAL_DESIGN.md` (reconciled 2026-10-02).

---

## 4. Context hierarchy

### 4.1 Canonical vocabulary (used by every document)

Context has two independent axes: **lifecycle layer** (how recent and how it is retrieved) and **data class** (how authoritative it is).

| Lifecycle layer | Definition | Tiers below |
|---|---|---|
| **Working context** | The packet assembled for one request (frame, coverage, anchors, folded state, timelines, supporting quotes, recent session turns, question). Built fresh per request; never stored as memory | C0 + items selected from C1–C5 |
| **Session context** | The current chat: last 4 turns, entity focus map, scope | C1 |
| **Recent context** | Episodes of the last 14 days: `context_events`, day view, thread and meeting summaries | C4 |
| **Persistent context** | What the system remembers: open obligations (active state), entity memory, preferences | C2 + C3 + P |
| **Historical context** | Raw messages, transcripts, closed items, old summaries; reached only by retrieval | C5 |

| Data class | Definition | Authority in packets |
|---|---|---|
| **Source context** | Quotes and facts copied from provider data or uploads (`BACKEND_DESIGN.md` §6 SOURCE) | Authoritative about what was said/scheduled (authority 3–4 depending on speaker) |
| **AI-derived context** | Extracted items, decisions, summaries, speaker mappings (`AI-DERIVED`) | Labelled "suggested"/"inferred", with confidence (authority 2) |
| **User-authored context** | Confirmations, edits, overrides, preferences, user-created items (`USER-AUTHORED`) | Highest (5) |
| **Computed context** | Reply state, priority, staleness, coverage (`COMPUTED`) | Deterministic; shown as system status (1) |

Every packed item is rendered with its data class, authority and `as of` time (§9.3).

### 4.2 Tiers

The "persistent" layer is split into tiers because open obligations, entity knowledge and preferences behave differently.

```text
                         ┌────────────────────────────────────────────┐
  always present         │ C0  Frame: user profile, now/timezone,     │  ~300 tokens
                         │     coverage (sync freshness), preferences │
                         ├────────────────────────────────────────────┤
  per conversation       │ C1  Session: last 4 turns,                 │  ≤ 2 K
                         │     entity focus map, scope                │
                         ├────────────────────────────────────────────┤
  the "working set"      │ C2  Active State: open items, open         │  query-filtered
                         │     questions, awaiting-reply threads,     │
                         │     upcoming meetings & deadlines          │
                         ├────────────────────────────────────────────┤
  who / what             │ C3  Entity Memory: people, orgs,           │  cards on mention
                         │     relationships, projects, confirmed     │
                         │     decisions                              │
                         ├────────────────────────────────────────────┤
  what happened lately   │ C4  Recent Episodes (≤ 14 days): events,   │  temporal windows
                         │     day views, thread & meeting            │
                         │     summaries, change feed                 │
                         ├────────────────────────────────────────────┤
  on demand only         │ C5  Archive: raw messages, transcripts,    │  hybrid search
                         │     closed items, old summaries            │
                         └────────────────────────────────────────────┘
  outside the prompt      P   Procedural: priority weights, suppression rules, reminder timing
                              (applied by code, never sent to the model)
```

**Packing priority when the budget is tight:** C0 → anchor facts from C2/C3 that the plan targets → their timelines (C4 events) → supporting evidence quotes → C1 recent turns → other C2 items → C4 summaries → C5 chunks. Within a tier, the ranking in §9.2 applies.

**Mapping:** Working context = C0 + the assembled packet; Session = C1; Recent = C4; Persistent = C2 + C3 + P; Historical = C5 (same as `TECHNICAL_DESIGN.md` §11).

---

## 5. Representation

### 5.1 Context types and their storage

| Context type | Primary representation | Tier |
|---|---|---|
| People | `persons`, `person_identifiers`, computed relationship profile, rendered card | C3 |
| Relationships | User↔person: relationship profile on `persons`. Person↔org: `persons.organization_id`. Person↔project: `project_members`. Co-participation: derived from `message_participants` and `meeting_participants`. AI-suggested links: `entity_links` | C3 |
| Conversations | `conversations` (thread state, awaiting, summary), `messages`, thread continuation links | C2 (awaiting) / C4 / C5 |
| Meetings | `meetings`, `meeting_participants`, `transcript_segments`, meeting summary, series key | C2 (upcoming) / C4 / C5 |
| Tasks | `work_items` (`type = task / request / follow_up`) + `context_events` | C2 |
| Commitments | `work_items` (`type = commitment`) + `context_events` | C2 |
| Deadlines | `work_items.due_*` with due history in `context_events`; standalone dates as `type = deadline` | C2 |
| Projects | `projects` (confirmed), `project_hint` on items/threads/meetings (inferred), `entity_mentions` | C3 |
| Decisions | `decisions` (`kind = decision / open_question`) with supersession and resolution links | C3 (confirmed / recent) / C5 |
| Recent events | `context_events` (≤ 14 days), day view (on-demand query) | C4 |
| Historical context | `chunks` (hybrid index), closed items, old summaries | C5 |

### 5.2 Context-specific schema (applied; authoritative DDL in `BACKEND_DESIGN.md` §17)

```sql
-- A1: one change feed for every entity type (replaces the earlier item-only event table)
context_events(id, user_id,
               entity_type,          -- work_item | decision | conversation | meeting | person | project | connection
               entity_id,
               event_type,           -- created | status_signal | lifecycle_changed | due_changed | completed_claim |
                                     -- resolved | superseded | awaiting_changed | meeting_processed | merged |
                                     -- became_overdue | became_stale | conflict_detected | user_edit | sync_gap
               payload jsonb,        -- before/after values, signal text
               evidence_id NULL,
               actor,                -- system | model | user | time
               authority smallint,   -- see §8.2
               materiality smallint, -- 0 noise · 1 minor · 2 notable · 3 important
               occurred_at,          -- when it happened in the world
               recorded_at)          -- when we learned it
INDEX (user_id, recorded_at DESC); INDEX (user_id, entity_type, entity_id, occurred_at)

-- A1: checkpoints for "what changed since I last looked"
user_checkpoints(user_id, surface,   -- today | chat | person:<id> | project:<id> | meeting:<id>
                 last_seen_at, PRIMARY KEY(user_id, surface))

-- A4: entity mentions beyond headers
entity_mentions(id, user_id, source_item_id, chunk_id NULL, evidence_id NULL,
                entity_type,         -- person | organization | project
                entity_id, surface_text, confidence, method,  -- header | alias_match | extraction | user
                occurred_at)
INDEX (user_id, entity_type, entity_id, occurred_at DESC)

-- A6: day view = on-demand query (no table): context_events (recorded/occurred in the local day),
--     meetings that occurred, conversations with activity, items created/changed, decisions

-- A3: status fold outputs on work_items
work_items + reported_status text NULL,          -- latest model/third-party signal (not authoritative)
           + reported_status_at, reported_status_evidence_id,
           + user_fields text[],                 -- fields set at authority 5
           + has_conflict bool, project_hint text NULL, version int

-- A16: versions (optimistic concurrency, prep-brief invalidation)
persons/projects/meetings/conversations/decisions + version int

-- A7: thread continuation uses entity_links(relation = 'continues', confidence)
-- A13: meeting_participants.origin ∈ {source, ai, user} (speaker mapping is 'ai' until confirmed)
```

### 5.3 Relationship profile (user ↔ person)

Computed nightly and on interaction (deterministic):

| Field | Definition |
|---|---|
| `importance_user` | Manual override (wins) |
| `importance_inferred` | 0–1 from: reciprocity (both directions emailed), user reply speed to them, meetings together in 30 d, role keywords, org importance, open items count |
| `interaction_recency` | Days since last two-way interaction |
| `open_mine` / `open_theirs` | Counts of open items user owes them / they owe user |
| `active_topics` | Top project hints and thread subjects in 30 d (no LLM) |
| `last_meeting_at`, `next_meeting_at` | From meetings |
| Card | Deterministic template rendering of the above at request time (no LLM blurb in the MVP; `AI_PIPELINE.md` §3) |

Person-to-person relationships beyond co-participation (for example "Sarah reports to John") are **not** modelled in the MVP; no PRD scenario requires them.

---

## 6. Retrieval method comparison and the knowledge-graph question

### 6.1 Comparison

| Method | What it is here | Strengths | Weaknesses | Latency (MVP scale) | Cost | Best for |
|---|---|---|---|---|---|---|
| **Relational** | Indexed SQL over entities, items, participants, links | Exact; complete for state; filterable by owner/status/due; cheap; testable | Needs the right entity resolved first; cannot find "what is this about" from free text | 5–50 ms | ~0 | Waiting-for, promised, needs-response, deadlines, person/meeting expansion |
| **Temporal** | Time-window and ordering queries over `occurred_at` / `recorded_at`, change feed, digests | Native fit for "yesterday", "what changed", "latest status", "since last meeting"; supports as-of answers | Needs bi-temporal timestamps and defined semantics | 5–50 ms | ~0 | Yesterday, what changed, status timelines, next action |
| **Semantic (vector)** | pgvector cosine over chunk/item embeddings | Finds paraphrases and topics without names; robust to wording | Weak on names, codes, numbers, negation and status; no notion of "current"; can return outdated text confidently | 10–80 ms + query embedding ~100–200 ms | embedding per query (≈ $0.00001) | Topic discovery, "what did we decide about X", meeting Q&A |
| **Hybrid (vector + keyword, RRF)** | Semantic + Postgres FTS fused | Adds exact-term recall (people, project codenames, ticket IDs) to semantic recall | Still discovery only; still no state | 20–120 ms + embedding | same as semantic | Default discovery method |
| **Graph** | Traversal over entity-relationship edges (graph DB or GraphRAG) | Variable-length paths; community/theme detection; "how is X connected to Y" | Second store or expensive LLM indexing; edges must be kept consistent with source; little benefit for 1–2-hop queries | Varies; GraphRAG indexing is LLM-heavy | High indexing cost for GraphRAG | Not needed in MVP (6.2) |

**Rule:** state questions are answered from relational + temporal retrieval. Hybrid retrieval is used to *discover* anchors and to gather supporting text. Semantic-only retrieval is never used to answer a state question.

### 6.2 Does the MVP need a knowledge graph?

**No.** The required traversals, with hop counts:

| Scenario | Traversal | Hops |
|---|---|---|
| Waiting-for | user → work_items (owner ≠ user, counterparty = user) | 1 |
| Promised | user → work_items (owner = user) → counterparty person | 1–2 |
| Needs response | user → conversations (awaiting = user) → sender person | 1–2 |
| Person context | person → items / threads / meetings / org | 1 |
| Meeting prep | meeting → attendees → their open items; meeting → prior meetings (series or overlap) → open questions | 2 |
| Cross-meeting | meeting → prior meetings → decisions / open questions → later events | 2 (+ time order) |
| Cross-email | item → evidence → messages → threads; thread → continuing threads | 2 |
| Project | project → items / decisions / threads / meetings / members | 1 |
| Decision chain | decision → superseded_by → … | recursive, short (recursive CTE) |

Every path is anchored on the user or on one entity and is at most two typed hops. These are indexed joins in Postgres. The structure of executive context is an **ego-network** (a star around the user with short spokes), which is exactly where a general graph database adds the least.

What a graph would add, and why it is deferred:

- *Variable-length relationship discovery* ("how am I connected to the CFO of Vendor X?") — not in the PRD MVP.
- *Community/theme summaries* (GraphRAG) for global questions ("recurring risks across all projects") — not in MVP; expensive to index and refresh as mail arrives daily.
- *Person-to-person organisational graph* — PRD §39 excludes an enterprise-wide organisational graph.

**The relational model is already a property graph in disguise.** Nodes are entity tables; edges are FK columns, participant tables, `entity_links` and `entity_mentions`. If a graph becomes necessary, these tables project directly into a graph (Apache AGE inside the same Postgres, or an export to a graph DB) without remodelling.

**Revisit triggers** (any one, measured by `CONTEXT_EVALUATION.md`):

1. A failure category in the evaluation suite whose root cause is a traversal of three or more hops, affecting ≥ 5% of a scenario's test cases.
2. A product requirement for relationship discovery beyond the user's ego-network.
3. Relational expansion p95 latency > 200 ms after indexing and partitioning.

---

## 7. Temporal model

### 7.1 Temporal semantics

| Expression | Resolution (user's timezone from `users.timezone`; meeting timezone for meeting-local phrases) |
|---|---|
| "today" | Local calendar day of *now* |
| "yesterday" | Previous local calendar day. Before 04:00 local, the planner also considers the day before as "yesterday" when the question is about meetings that ended after midnight (ask if ambiguous) |
| "this week" | Monday 00:00 to Sunday 24:00 local (configurable week start) |
| "last week" | Previous Monday–Sunday. "In the last week" means rolling 7 × 24 h |
| "recently" | Rolling 7 days |
| "since the last meeting (with X)" | From `ends_at` of the most recent occurred meeting with X |
| "what changed" (no anchor) | Since `user_checkpoints.last_seen_at` for the surface; if older than 14 days, the last 7 days with a note |
| Relative deadlines in sources ("Friday", "EOD", "next week") | Resolved relative to the *source's* `occurred_at` in the sender's timezone when known, otherwise the user's; `due_precision` records the granularity (datetime / date / week / fuzzy) |
| "Working day" | Monday–Friday unless `work_hours` says otherwise; no holiday calendar in MVP |

### 7.2 Bi-temporal timestamps

- `occurred_at`: when the thing happened (email sent, meeting held, statement made).
- `recorded_at`: when the system ingested or derived it.

Uses:

- **Status timelines** order by `occurred_at` (what happened first in the world).
- **"What changed since I last looked"** filters by `recorded_at` (what is new *to the user*), so a late-processed meeting recording from last Tuesday still appears as a change today.
- **Evaluation replay** uses `recorded_at ≤ T` to reconstruct what the system knew at time T.

### 7.3 Change feed

`context_events` is the single change feed. Writers:

| Writer | Events |
|---|---|
| Extraction pipeline | created, status_signal, completed_claim, due_changed, resolved, superseded, conflict_detected, merged |
| Sync / normalization | awaiting_changed (thread now waiting on user / other), meeting_processed, person created (first contact), sync_gap |
| User actions | user_edit, lifecycle_changed |
| Time sweeper (hourly) | became_overdue, became_stale, due_soon (24 h) — synthetic events with `actor = time` |

**Materiality** (0–3) is assigned deterministically: e.g., new explicit commitment from/to a VIP = 3, deadline moved = 3, completion claim = 2, progress signal = 1, new thread from unknown sender = 0. "What changed" shows materiality ≥ 2 by default.

### 7.4 Net-change folding

Multiple events on the same entity since an anchor collapse into one **net change**: compare the entity's folded state at the anchor with its folded state now (status, due date, owner, resolution). Example: due date moved Oct 8 → Oct 10 → Oct 8 shows "no net change to deadline" plus "deadline was discussed twice" only if asked for detail.

---

## 8. Status fold (current state of an item)

### 8.1 Output

For each work item, the fold produces:

```text
lifecycle_status      open | in_progress | done | cancelled         (authoritative; changes only by user or creation)
verification_status   suggested | confirmed | rejected | user_created
reported_status       e.g. "still pending" | "in progress" | "claims sent" | "delayed"  (non-authoritative)
reported_by / at      person + timestamp + evidence
due_at (current)      + due_history[] with evidence and authority
conflicts[]           e.g. two different deadlines from equal-authority sources
last_activity_at      latest event occurred_at
stale                 no activity beyond policy
```

The answer layer must present `reported_status` as *reported* ("John's Oct 1 email says it is still pending"), never as lifecycle fact.

### 8.2 Authority levels (used for fold and conflicts)

| Level | Source | Example |
|---|---|---|
| 5 | User edit or confirmation | User changes deadline to Oct 10 |
| 4 | Explicit statement by the item's owner in a source | John: "I'll have it to you by the 10th" |
| 3 | Explicit statement by another participant | Sarah: "John's docs are coming on the 10th" |
| 2 | Model inference from a source (probable, implied) | "We should be able to wrap this up soon" |
| 1 | Summary-derived or time-derived | Stale flag, summary text |

### 8.3 Fold rules

1. Apply events in `occurred_at` order.
2. A field set at authority 5 changes only by another authority-5 event. Higher-authority later evidence that disagrees creates `conflict_detected` and a "possible update" prompt.
3. Otherwise, a later event with authority ≥ the current value's authority replaces it; the old value moves to history.
4. A later event with lower authority does not replace; if it disagrees, it is recorded as a conflict only when its authority ≥ 3.
5. `completed_claim` sets `reported_status = "claims done"`; it never sets `lifecycle_status = done` (PRD §17).
6. Ties at equal authority and equal time: keep both, `has_conflict = true`.
7. Fields set by the user are recorded in `work_items.user_fields`; the fold never changes them from model, system or time events.

### 8.4 Where the fold runs

The fold executes inside `work.append_event` in the same transaction that inserts the event, with the item row locked (`SELECT … FOR UPDATE`) and `version` incremented (`BACKEND_DESIGN.md` §12.2). Because it orders by `occurred_at` and applies authority before recency, concurrent writers converge on the same result regardless of commit order. Re-folding all items from `context_events` (rebuild level R1) produces the same projections and needs no AI call.

---

## 9. Context assembly

### 9.1 Pipeline

```text
question ─▶ plan (rules → T1 planner AI-05 if needed)
         ─▶ scope            (permission filter §9.7: user RLS, active connections, session scope, retention)
         ─▶ resolve anchors  (entities: person/org/project/meeting/item; time window)
         ─▶ discover         (hybrid search only if anchors are unresolved or the intent is topical)
         ─▶ expand           (typed relational recipes per scenario, ≤ 2 hops)
         ─▶ fold             (status fold for items; net-change fold for "what changed")
         ─▶ rank & budget    (tiers + §9.2 scoring + per-scenario budget)
         ─▶ render packet    (fixed layout §9.3, citation IDs, coverage block)
         ─▶ generate         (AI-06 T1 for list scenarios, AI-07 T2 for synthesis)
         ─▶ verify           (citations exist, negative claims cite coverage, reported vs. lifecycle wording)
```

### 9.2 Ranking inside a tier

```text
score = base_relevance                       # RRF score for discovered chunks; 1.0 for relationally matched rows
      × recency_decay(age, half_life[type])  # open items: no decay; events 7 d; messages 14 d; transcripts 30 d; decisions 120 d
      × authority_weight                     # user 1.4 · confirmed 1.3 · explicit 1.1 · inferred 1.0 · summary 0.8
      × materiality_weight                   # events only
      × (1 + 0.3·anchor_match)               # touches the resolved anchor entity
```

**Chain completeness beats diversity (A14).** Once an anchor item is selected, its full evidence chain (all `context_events` with evidence) is included before any other item's chain, up to the budget. Per-thread diversity caps apply only to discovery results.

### 9.3 Packet layout (fixed order)

```text
[FRAME]      user, now (local), timezone, scope
[COVERAGE]   sources searched + windows; last successful sync per source; connection status; gaps
[ANCHORS]    resolved entities with cards (person/project/meeting/item)            ← most important first
[STATE]      folded items / decisions relevant to the question, each with [S#] IDs
[TIMELINE]   chronological events for anchor items (occurred_at), with [S#] quotes
[SUPPORT]    additional quotes/chunks from discovery
[SESSION]    last turns + entity focus
[QUESTION]   the user's question (last, so it is closest to generation)
```

The stable instructions and schema precede this packet, which lets implicit caching work when packets are large.

### 9.4 Coverage block (A2)

```text
COVERAGE: searched email (Gmail, synced 10:42 today), calendar (synced 10:35), meetings (3 uploaded in window)
          window 2026-09-29 → 2026-10-02. Gmail status: active. No gaps.
```

Answer rules:

- A statement of absence ("No completion evidence has been detected") must cite `[COVERAGE]` and include the sync time.
- If any relevant source is stale (> 1 h behind for email during work hours) or disconnected, the answer must say so ("Gmail has not synced since yesterday 18:10; newer updates may be missing").

### 9.5 Cards (A16)

Cards are rendered deterministically at request time from the entity's current row and fold (`profile` ∈ {`compact`, `standard`, `timeline`}). Identical inputs produce identical text, which makes prompts reproducible for evaluation and friendly to Gemini implicit caching. Entity `version` columns serve optimistic concurrency and prep-brief invalidation; no rendered-card cache is built in the MVP.

### 9.6 Budgets per scenario

| Scenario | Answer tier | Dynamic budget | Hard cap | p95 latency target |
|---|---|---|---|---|
| 1 Current email | deterministic panel; T2 on "explain" | 2 K | 4 K | 300 ms panel / 4 s explain |
| 2 Person | deterministic card and lists; T1 for transcript/thread lookups | 3 K | 6 K | 300 ms / 3 s |
| 3 Project | T2 | 5 K | 10 K | 6 s |
| 4 Meeting prep | deterministic sections (precomputed); T2 suggested asks on open | 2 K (asks) | 4 K | instant sections / 5 s asks |
| 5 Cross-meeting | T2 | 7 K | 16 K | 7 s |
| 6 Cross-email | T2 | 6 K | 14 K | 7 s |
| 7 Waiting for | deterministic | — | — | 1 s |
| 8 Promised | deterministic | — | — | 1 s |
| 9 Needs response | deterministic | — | — | 1 s |
| 10 Yesterday | deterministic day view; T2 if detail asked | 4 K | 8 K | 1 s / 6 s |
| 11 What changed | T2 (deterministic grouped list if T2 unavailable) | 5 K | 10 K | 6 s |
| 12 Next action | T2 (short) | 4 K | 8 K | 5 s |

Answer tiers map to `AI_PIPELINE.md`: deterministic = rendered from structured results with citations, no AI call; T1 = AI-06 (lookups over retrieved text); T2 = AI-07 (AI-08 for reply guidance, AI-11 for meeting asks). Every route runs the abstention pre-check and grounding checks (`AI_PIPELINE.md` §5.7).

### 9.7 Permission-aware retrieval

Every retriever applies the same scope before ranking:

| Filter | Rule |
|---|---|
| User | RLS on every user-owned table (`app.user_id`; delivery infrastructure is isolated by database role, `BACKEND_DESIGN.md` §7.6), plus explicit `user_id` predicates in every SQL, vector and FTS query; unset context returns nothing |
| Source authorization | Sources from connections that are revoked or disconnected are excluded unless the user chose "keep data" on disconnect; the coverage block reports the gap. Calendar-derived meetings are excluded if the calendar grant was revoked |
| Trash and deletion | Trashed/spam sources excluded; deleted sources gone (evidence quotes redacted) |
| Session scope | Meeting-scoped sessions see the meeting, its linked prior meetings and their items only |
| Retention | Purged bodies are not retrievable; answers use evidence quotes and summaries and say the original is no longer stored |
| Verification | Rejected items excluded; suggested items included with labels; archived items only on explicit request |

### 9.8 Cross-source path without sending history

The question "What do I need to send John before Thursday's architecture review?" follows the path **EMAIL → PERSON → PROJECT → MEETING → COMMITMENT → DEADLINE** entirely in SQL and indexed search; only the final packet reaches the model.

| Step | Operation | Index | Output size |
|---|---|---|---|
| 1 Email → Person | Resolve "John" via `person_identifiers` aliases + session focus; or from the open email's sender | `ux_person_identifiers`, trigram | 1 person ID |
| 2 Person → Project | Projects where John is a member or that share `project_hint`s with John's open items | `project_members`, `ix_wi_hint_trgm` | 1–2 projects |
| 3 Project/Person → Meeting | Next meeting on Thursday with John as attendee or linked to the project | `meetings (user_id, starts_at)`, `meeting_participants` | 1 meeting |
| 4 → Commitment | Open items with owner = self and counterparty = John or project = P; open questions from the previous meeting in the series | `ix_wi_owner_open`, `ix_ce_entity` | 3–8 items with folds |
| 5 → Deadline | `due_at` ≤ meeting start, sorted; due history from timelines | `ix_wi_due_open` | ordered list |
| 6 Evidence | Up to 2 quotes per item (email + meeting) | `item_evidence` | ~10 quotes |
| Packet | Frame + coverage + 3 cards + folded items + quotes | — | ≈ 2.5–3.5 K tokens |

No raw thread, transcript or history beyond those quotes is sent. If the plan cannot resolve John or the meeting, hybrid discovery runs first (§10.3, §10.6) and then the same expansion.

### 9.9 Indexing and hybrid search parameters

| Source | Chunk unit | Size | Metadata |
|---|---|---|---|
| Email | One chunk per message (`body_clean`: quoted replies and signatures removed deterministically); split at paragraph boundaries above 1,200 tokens | 50–1,200 tokens | sender, recipients, conversation, `sent_at`, subject prefix |
| Transcript | Speaker-turn windows of ~600 tokens with one-turn overlap | 400–800 tokens | meeting, start/end ms, speakers |
| Calendar event | Title + description + attendees | small | meeting |
| Work item / decision | Rendered card text (also used as `dedupe_embedding`) | small | item ID |
| Thread / meeting summary | Summary text | ≤ 400 tokens | entity ID, `as_of` |

Prefiltered bulk mail is not chunked or embedded. Hybrid search runs one SQL statement: top 40 by HNSW cosine on `chunks.embedding` and top 40 by `ts_rank_cd` on `chunks.tsv`, both filtered by the §9.7 scope and the plan's time/person/meeting filters, fused with Reciprocal Rank Fusion (`Σ 1/(60 + rank)`), then ranked by §9.2. pgvector `hnsw.iterative_scan = relaxed_order` prevents per-user filters from starving results. Query embeddings use `gemini-embedding-2` (768 dimensions, normalized) with the retrieval instruction in the input text. No reranker in the MVP.

### 9.10 Phase 2 implementation parameters (decided 2026-10-02)

Values the sections above leave open, fixed before coding. Changing any of them is a major change (`AI_PIPELINE.md` §12).

| Topic | Decision |
|---|---|
| Token estimate | ⌈characters / 4⌉, deterministic, no tokenizer call. Used for chunk sizes, packet budgets and `retrieval_traces.context_tokens` |
| Email chunks | Text = the message's `body_clean` (quoted history and signatures already removed by normalization). One chunk when ≤ 1,200 tokens; otherwise paragraphs (blank-line separated) packed greedily up to 1,200 tokens; a paragraph above 1,200 is split at sentence ends, then at whitespace; a last piece under 50 tokens is merged into the previous chunk. An empty body indexes the subject alone; no subject and no body → no chunk. `title` = subject. `person_ids` = sender, To and Cc persons |
| Calendar chunks | One chunk: description (if any) and an `Attendees:` line with display names; `title` = meeting title. Cancelled meetings have no chunks |
| Not chunked | Prefiltered mail (no `MessageNormalized`), duplicate RFC 822 copies (they stop at `normalized`), trashed or deleted sources (removed or excluded at query time) |
| AI-04 input (`embed/v1`) | Document: `title: {title or "none"} \| text: {text}`; query: `task: search result \| query: {question}`. The format string is part of the cassette key. Vectors are L2-normalized in code before storage (768 dimensions). `chunks.content_hash` = SHA-256 of the document input, so unchanged text is never re-embedded |
| FTS | Configuration `english`; `tsv` = A-weighted title + B-weighted text. Query = OR of the question's topic terms (alphanumeric tokens of ≥ 2 characters, at most 12) through `to_tsquery('english', 't1 \| t2 …')`; rank `ts_rank_cd(tsv, query, 32)` |
| Vector search | `SET LOCAL hnsw.iterative_scan = relaxed_order` and `SET LOCAL hnsw.ef_search = 100` in the query transaction; only rows with `embedding_model` = the current AI-04 model; top 40 by cosine distance |
| Fusion | RRF with k = 60 over the two top-40 lists; the 20 best chunks go on to ranking; per-conversation cap of 3 chunks applies to discovery only (A14) |
| Match floor (abstention pre-check) | A chunk counts as a match only if it is in the FTS list or its cosine similarity is ≥ 0.60 |
| Item and decision discovery | No item or decision embeddings in Phase 2 (they arrive with `dedupe_embedding`). Discovery uses SQL full-text search over the rendered title or statement, computed in the query over the user's rows, plus the pivot from matched chunks to items through `evidence.source_item_id` |
| Ranking weights (§9.2) | Authority: user-created or user-edited 1.4, confirmed 1.3, explicit (strength) 1.1, other AI-derived 1.0, summary 0.8. Materiality (events only): 0 → 0.5, 1 → 0.8, 2 → 1.0, 3 → 1.2. Anchor match: the row references a resolved anchor person, conversation, project or item |
| Budget enforcement (§9.6) | Frame, coverage and question are always packed. Anchors and their timelines (chain completeness) may use up to the hard cap. Every other section is packed only while the packet stays within the dynamic budget; an item that does not fit is skipped, never cut. Deterministic scenarios (S7–S9, overdue, deadlines) have no token budget and list at most 50 items |
| Session block | The last 4 turns, each cut to its first 300 tokens, at most 1,500 tokens in total; older turns go first |
| Coverage | Sources: email (connections with the mail capability, resource `mail`) and calendar (resource `calendar`). Last success = `sync_cursors.last_success_at`. A source is **stale** when its last success is older than 1 h during the user's work hours (Monday–Friday 09:00–18:00 local unless `users.work_hours` says otherwise) or older than 24 h at any time. Gaps: not connected, `needs_reauth`, `error`, `paused`, disconnected with kept data, calendar scope missing, stale. The block is rendered by a deterministic template |
| Source authorization (§9.7) | A purge deletes data, so it is never retrieved. A connection disconnected without purge ("keep data") stays retrievable and is reported as a gap. `calendar_event` sources are excluded when their connection no longer holds the calendar capability. Trashed and deleted source items are excluded from chunk search, and evidence quotes whose source is trashed or deleted are not packed |
| Meeting session scope | Allowed sources: the meeting's source item plus up to 2 prior meetings (same `series_key`, else ≥ 50% participant overlap within 60 days); items and decisions qualify when their evidence comes from those sources |
| Citations | Packed items get `S1`…`Sn` in layout order; the coverage block is cited as `COVERAGE` |
| Untrusted text | Every quote, chunk, subject and name in a packet is delimited by `<<<` and `>>>`; occurrences of those markers inside content are replaced by `‹‹‹` and `›››`. User notes are never packed (`AI_PIPELINE.md` §15) |
| Traces | One `retrieval_traces` row per assembly, content-free: plan, candidate and selected IDs with scores, coverage statuses and times, token counts, latency; the question only as a SHA-256 hash |

---

## 10. Retrieval design per scenario

Each scenario lists: **trigger**, **anchors**, **retrieval steps**, **output**, **cost**, and **failure modes handled**. "SQL" means indexed relational queries; "temporal" means window/order/change-feed queries; "hybrid" means vector + FTS with RRF.

### 10.1 Current email context

- **Trigger:** user opens a message or thread (UI side panel), or asks "what's this about?".
- **Anchors:** the message, its conversation, sender and recipients (persons), resolved org.
- **Steps:**
  1. SQL: conversation state (awaiting, needs_reply, priority + reasons), gist timeline or long-thread summary (`as_of`).
  2. SQL: items extracted from this message and this thread (any status), with fold.
  3. SQL: open items between user and the sender, both directions (top 5 by priority).
  4. SQL + temporal: next meeting with the sender (≤ 14 days) and last meeting (with summary line).
  5. SQL: threads linked by `continues` (A7) and decisions linked to this thread or its project hint.
  6. Hybrid (only if steps 2–5 return < 3 items): related chunks from other threads with the same participants, last 30 days, top 3.
- **Output:** deterministic panel. LLM only for the explicit "explain" action.
- **Cost:** zero LLM by default.
- **Failure modes:** sender unknown (new person) → show "first contact"; thread long → summary plus last 3 messages only.

### 10.2 Current person context

- **Trigger:** People page, mention of a person in chat, or email panel.
- **Anchors:** person (resolved via identifiers, session focus, or disambiguation prompt if two candidates score within 10%).
- **Steps:**
  1. SQL: relationship profile and card.
  2. SQL: open items user owes them; open items they owe user (folded).
  3. SQL: threads awaiting reply from/to them.
  4. Temporal: interaction timeline last 30 days (messages, meetings, mentions via `entity_mentions`), newest first, collapsed per thread/meeting.
  5. SQL: active projects (members, or hints on shared items).
  6. Temporal: next and last meeting.
- **Output:** card + lists; chat answers use T1 over this packet.
- **Failure modes:** alias ambiguity ("John") → ask or choose by session focus and recency, stating the choice.

### 10.3 Current project context

- **Trigger:** "What's happening with the cloud migration?", project page.
- **Anchors:** confirmed project (name/alias trigram + embedding match over project names). If none matches above threshold → **topic mode** (A5).
- **Steps (confirmed project):**
  1. SQL: project card, members.
  2. SQL: open items, open questions, decisions (recent first) linked by FK or `project_hint` match.
  3. Temporal: change feed for those entities in last 14 days.
  4. SQL: threads and meetings linked to the project (FK, hint, or `entity_mentions`), last 30 days, summaries.
  5. Hybrid (restricted to the project's linked sources + the project name): top supporting quotes.
- **Steps (topic mode):** hybrid search on the topic over items, decisions and chunks (last 60 days) → group results by thread/meeting → take the top 3 groups → expand each group relationally (items, decisions, participants) → label the answer "grouped by topic; not a confirmed project" and offer "Create project".
- **Output:** T2 synthesis: status, open items, recent decisions, blockers, people.
- **Failure modes:** topic too broad → ask a narrowing question with the top 3 groups as options.

### 10.4 Current meeting preparation

- **Trigger:** deterministic sections precomputed 45 min before a meeting with context; "What should I know before today's meeting?".
- **Anchors:** the calendar meeting (resolved by time: next meeting today, or by title/person); attendees.
- **Steps:**
  1. SQL: attendees → persons → relationship cards (compact).
  2. SQL: open items involving any attendee (both directions), folded, due before or near the meeting first.
  3. Temporal + SQL: **prior meetings** = same `series_key`, else ≥ 50% participant overlap in last 60 days, else title similarity ≥ 0.8 with ≥ 1 shared participant (A13). Take the last 2.
  4. SQL: from prior meetings — open questions (unresolved), decisions, items created there; their subsequent events (resolved / superseded / updated).
  5. Temporal: change feed for attendees and their items since the last prior meeting (`since last meeting` semantics).
  6. SQL: threads with attendees active in last 14 days (summaries, awaiting state).
  7. Hybrid (restricted to attendee-linked sources, 30 days) on meeting title + description: top 3 supporting quotes.
- **Output:** deterministic prep sections (purpose, what changed since last time, open items by direction, unresolved questions, deadlines before the meeting) with citations, recomputed when a relevant `context_events` row arrives (version check). When the user opens the prep view and the sections are non-empty, AI-11 adds 3–5 suggested asks (`recommendation` claims) from those sections only.
- **Failure modes:** meeting with no context → sections show "first meeting with …" and no AI call; AI-11 unavailable → sections only.

### 10.5 Cross-meeting reasoning

- **Trigger:** "What should I ask about authentication in today's meeting?", "What did we decide across the last architecture meetings?", meeting chat referencing earlier meetings.
- **Anchors:** current/target meeting (if any) and the topic.
- **Steps:**
  1. Prior meeting set as in 10.4 step 3 (extend to last 5 for "across meetings").
  2. Hybrid restricted to those meetings: decisions, open questions and transcript chunks matching the topic.
  3. SQL: for each matched decision/open question — resolution and supersession chain (recursive CTE), and later events from **any source** (emails can resolve meeting questions — A8).
  4. Temporal: order everything by `occurred_at`; mark latest state.
- **Output:** T2: chronological reasoning — "Sept 29: unresolved → Oct 1 email from Sarah proposes OAuth → not yet decided → ask: …", with citations per step.
- **Failure modes:** decisions conflicting across meetings → present supersession chain; no prior meeting → say so with coverage.

### 10.6 Cross-email reasoning

- **Trigger:** "What's the status of the security review?", "What has Microsoft said about pricing across all threads?".
- **Anchors:** topic and/or organization/person.
- **Steps:**
  1. Discovery (hybrid) over item cards and decision cards first; then over message chunks (last 60 days), filtered by org/person if given.
  2. Pivot: matched chunks → their extracted items (via `evidence`) → anchor items.
  3. Expand each anchor item's full timeline across all threads and meetings (chain completeness, §9.2).
  4. SQL: threads linked by `continues` (A7) to any thread in the chain.
  5. Temporal: order by `occurred_at`; status fold.
- **Output:** T2: current status first, then the chain, then gaps, with coverage.
- **Failure modes:** nothing extracted (only discussion) → answer from chunks, labelled "from discussion; no tracked item", suggest creating one.

### 10.7 "What am I waiting for?"

- **Steps (SQL only):**
  1. `work_items` with direction `waiting_for` or `delegated` (directions are computed by the deterministic statement mapping, `AI_PIPELINE.md` §5.5), `lifecycle_status ∈ {open, in_progress}`, `verification_status ≠ rejected`; `observed` and `unresolved` items excluded.
  2. Conversations where `awaiting = other` and the last outbound message contained a question/request (triage flag) for ≥ 1 working day — the implicit waiting-for.
  3. Fold each item (reported status, due, last activity, stale).
  4. Order: overdue first, then due soonest, then priority; group by person/org when > 7 items.
- **Output:** deterministic rendering (no AI call): who, what, due, last update, source; low-confidence items flagged "possible".
- **Coverage:** included; "no update since …" uses coverage.

### 10.8 "What did I promise?"

- **Steps (SQL only):**
  1. `work_items` with direction `my_commitment` (including requests converted by an `accepted` signal), open, not rejected; optional person/org filter on the counterparty ("What did I promise Sarah?").
  2. Include `commitment_strength ∈ {explicit, probable}`; `suggestion`/`inferred` only when confidence ≥ 0.7, labelled.
  3. Fold; order by due then priority; group by counterparty.
- **Output:** deterministic list with source (email date or meeting + timestamp).
- **Note:** commitments made by the user in meetings depend on speaker mapping; items with direction `unresolved` (unmapped speaker) are listed separately under "possible — confirm speaker".

### 10.9 "Who needs my response?"

- **Steps (SQL only):**
  1. Conversations with `awaiting = user` and `needs_reply = true`, not snoozed/handled, last inbound within 14 days.
  2. Open `request` items where owner = self and request_type ∈ {reply, review, approve, decide, provide_info} (catches meeting-originated asks).
  3. Merge by person; priority from features (sender importance, deadline, explicit request, active project, meeting proximity); reasons from templates.
- **Output:** deterministic ranked list with template reasons (PRD §28); no AI call.
- **Failure modes:** user replied by other means → "Mark handled" sets `awaiting = none` with a user event.

### 10.10 "What happened yesterday?"

- **Steps:**
  1. Temporal: resolve the day window (§7.1).
  2. SQL **day view** (on demand, no stored digest): events with `occurred_at` in the window, plus events `recorded_at` in the window for late-processed sources (flagged "learned today").
  3. Day view content: meetings that occurred (summary line + decisions), threads with activity ranked by priority (summary line), items created/changed (materiality ≥ 2), decisions, reply-state changes.
  4. Detail on request: expand any line relationally.
- **Output:** deterministic rendering of the structured day view; AI-07 (T2) only for follow-up questions that need synthesis. The same view backs `GET /api/v1/days/{date}`.

### 10.11 "What changed?"

- **Steps:**
  1. Anchor time: explicit ("since Monday"), or `user_checkpoints` for the surface, or "since last meeting with X".
  2. Temporal: `context_events` with `recorded_at > anchor`, materiality ≥ 2, scoped to the surface (global / person / project / meeting attendees).
  3. Net-change fold per entity (§7.4); drop entities with no net change unless the user asks for detail.
  4. Group: new obligations (mine / theirs), status changes, deadline changes, resolved/superseded decisions, new awaiting replies, overdue/stale transitions, processed meetings, sync gaps.
  5. Rank by materiality × priority.
- **Output:** T2 concise "since <anchor>" briefing with citations; after display, update the checkpoint.
- **This is the core of the PRD §58 three-day-return test.**

### 10.12 "What should I do next?"

- **Steps:**
  1. SQL: active state working set — my overdue/due-today items, awaiting replies with priority ≥ 60, meeting in next 2 h with non-empty prep sections.
  2. Temporal: free time until the next meeting (calendar), current time of day.
  3. Score: priority × feasibility (fits available time by item type heuristics) × time sensitivity.
  4. Take top 3 candidates with reasons.
- **Output:** T2 short recommendation (PRD Level 2 "Recommend") rendered as `recommendation` claims, each with reason and source; never an action. If T2 is unavailable, the deterministic top 3 with template reasons is shown.

### 10.12a Additional intents (deterministic unless noted)

| Question | Retrieval | Filters | Output |
|---|---|---|---|
| "Which commitments are overdue?" | `ix_wi_due_open` | `due_at < now`, open, directions `my_commitment` and `waiting_for` (grouped), not rejected | Deterministic list, overdue age, last activity |
| "Who is waiting on me?" | Union of `my_commitment`, `my_task` (requests to the user) and conversations awaiting the user | open; grouped by counterparty/requester | Deterministic grouped list ranked by priority |
| "What should I reply to John first?" | §10.9 filtered to John's conversations, then reply guidance (AI-08) for the top thread on request | person filter; awaiting = user | Ranked threads; draft only when asked |
| "What are the unresolved issues from the last three meetings?" | Last 3 occurred meetings the user attended (by `ends_at`), or last 3 in a named series; open questions not resolved, open items created there, conflicts | time order; optional series/project filter | Deterministic list; AI-07 if the user asks for synthesis |
| "What changed since the last meeting?" | §10.11 anchored at `ends_at` of the most recent occurred meeting the user attended (or with the named person/series); scoped to that meeting's attendees and items | anchor + scope | AI-07 grouped summary; deterministic grouped list on failure |
| "What happened while I was away for three days?" | §10.11 anchored at the user checkpoint (or explicit "three days") plus §10.12 next actions | anchor = checkpoint | AI-07 "since you left" briefing |

### 10.13 Summary of methods per scenario

| Scenario | Relational | Temporal | Hybrid | Graph |
|---|---|---|---|---|
| 1 Current email | primary | supporting | fallback | — |
| 2 Person | primary | primary | — | — |
| 3 Project | primary (confirmed) | supporting | primary in topic mode | — |
| 4 Meeting prep | primary | primary | supporting | — |
| 5 Cross-meeting | primary (chains) | primary | discovery | — (recursive CTE for chains) |
| 6 Cross-email | primary (expansion) | primary | discovery | — |
| 7 Waiting for | only | supporting | — | — |
| 8 Promised | only | supporting | — | — |
| 9 Needs response | only | supporting | — | — |
| 10 Yesterday | supporting | primary | — | — |
| 11 What changed | supporting | primary | — | — |
| 12 Next action | primary | primary | — | — |

---

## 11. Specification per context type

Fields: **Persistence** (where and how long), **Freshness** (maximum acceptable lag), **Priority** (tier and packing order), **Retrieval**, **Authority** (who can set facts), **Summarization**, **Staleness**, **Conflict**.

### 11.1 People

| Aspect | Policy |
|---|---|
| Persistence | `persons` + identifiers until user deletion; merged persons keep identifier history |
| Freshness | Identity/headers: sync lag (≤ 10 min). Relationship profile: nightly + on interaction. Card: rendered at request time from current data |
| Priority | C3; packed only when the person is an anchor or an attendee/counterparty of anchor items |
| Retrieval | Identifier lookup (email exact, alias trigram, session focus) → relational expansion; mentions via `entity_mentions` |
| Authority | Email address = source. Name, role, org: user edit > signature/header (newest) > model inference |
| Summarization | Deterministic template card (topics from project hints and thread subjects; no LLM) |
| Staleness | Role/title older than 180 days marked "as of"; no interaction for 90 days → drops out of active people lists (kept) |
| Conflict | Two persons sharing an email → merge. Same name, different emails → never auto-merge; disambiguate by org and context; user merge action |

### 11.2 Relationships

| Aspect | Policy |
|---|---|
| Persistence | Derived profile fields on `persons`; `project_members`; `entity_links` for AI-suggested links |
| Freshness | Nightly; importance recomputed immediately on user override |
| Priority | Used mainly as **ranking features** (priority, reminders), not as prompt text; included in person cards |
| Retrieval | SQL over participants and links |
| Authority | User importance/relationship type > derived stats > model inference (e.g., "client") |
| Summarization | None beyond the card |
| Staleness | Interaction-based importance decays with a 60-day half-life unless manually set |
| Conflict | Manual value always wins; inferred type changes only with new evidence and stays `inferred` |

### 11.3 Conversations (threads)

| Aspect | Policy |
|---|---|
| Persistence | `conversations` + `messages` (bodies per retention policy), per-message gists, long-thread summary, continuation links |
| Freshness | Reply state (`awaiting`, `needs_reply`): sync lag. Gist timeline: as soon as AI-01 applies. Long-thread summary: ≤ 10 min debounce |
| Priority | Awaiting-user threads are C2 (Active State); others C4/C5 |
| Retrieval | SQL by participants, state and time; hybrid over message chunks for topics |
| Authority | Message content = source. Reply state = deterministic. `needs_reply` = model (T1) unless user marks handled |
| Summarization | Deterministic gist timeline (date, sender, gist) for threads up to 8 relevant messages; AI-03 summary for longer threads or on request, always regenerated from clean messages (no summary-of-summary, which also resolves A9 drift); summaries are narrative only |
| Staleness | Awaiting-user thread with no activity for 14 days leaves "needs response" (still visible under "older") |
| Conflict | Not applicable to raw messages; reply state from user action ("handled") wins |

### 11.4 Meetings

| Aspect | Policy |
|---|---|
| Persistence | `meetings`, participants, transcript segments (until user deletion), summary, prep sections; raw media per retention |
| Freshness | Calendar: ≤ 15 min. Processed meeting outputs: as soon as pipeline completes. Prep brief: invalidated on relevant change events |
| Priority | Upcoming (≤ 24 h) meetings are C2; past meetings C4 (≤ 14 days) or C5 |
| Retrieval | Temporal (by time), SQL (by attendee, series), hybrid over transcript chunks for topic questions; meeting-scoped sessions restrict to the meeting + prior meetings |
| Authority | Calendar fields = source. Transcript = source of what was said. Summary/decisions/items = inference until confirmed. Speaker mapping = inference unless ≥ 0.9 or confirmed |
| Summarization | One structured summary per meeting (topics, decisions, open questions, items, concerns); no re-summarization of summaries |
| Staleness | Prep briefs older than their newest relevant event are rebuilt; summaries never expire (fixed past event) |
| Conflict | Calendar change (time/attendees) supersedes earlier calendar state; transcript vs. calendar attendees → transcript evidence marks `attended` |

### 11.5 Tasks (task, request, follow-up)

| Aspect | Policy |
|---|---|
| Persistence | `work_items` + `context_events` + evidence, until user deletion; done/cancelled items retained |
| Freshness | Created/updated within extraction lag (≤ 5 min after sync for interactive path) |
| Priority | C2 when open; packed first for agenda/next-action/person scenarios |
| Retrieval | SQL by direction, status, due, person, project; hybrid over item cards for topic discovery |
| Authority | §8.2 levels; lifecycle only by user |
| Summarization | None (cards are rendered, not summarized) |
| Staleness | No activity for 7 days (tasks) → `stale`; suggested + unconfirmed + no new evidence for 14 days + priority < medium → auto-archived (hidden, recoverable) |
| Conflict | Status fold (§8.3); conflicts surfaced in UI and answers |

### 11.6 Commitments

| Aspect | Policy |
|---|---|
| Persistence | As tasks; `commitment_strength` retained permanently for audit |
| Freshness | As tasks |
| Priority | C2; `my_commitment` and overdue `waiting_for` rank above generic tasks (commitment weight) |
| Retrieval | SQL by owner/counterparty (scenarios 7, 8); timeline expansion for status (5, 6) |
| Authority | Only explicit owner statements create `explicit` commitments; third-party claims create at most `probable` |
| Summarization | None |
| Staleness | `waiting_for` past due with no event for 5 working days → stale + reminder candidate |
| Conflict | Status fold; completion claims never close; a later explicit retraction ("we can't deliver") sets `reported_status = withdrawn` and prompts the user |

### 11.7 Deadlines

| Aspect | Policy |
|---|---|
| Persistence | `due_at`, `due_precision`, `due_text`, `due_kind` on items; every change as `due_changed` event |
| Freshness | Immediate on extraction; time-driven `due_soon` / `became_overdue` events hourly |
| Priority | Deadline urgency is the strongest time-sensitive priority feature |
| Retrieval | SQL range queries on `due_at`; temporal for "this week", "coming up" |
| Authority | User > owner explicit > other participant explicit > inferred; code-resolved dates beat model-resolved dates when they disagree |
| Summarization | None |
| Staleness | Fuzzy deadlines ("soon", "next week") never trigger hard-deadline reminders; shown with precision label |
| Conflict | Different dates at equal authority → `has_conflict`, both shown with sources |

### 11.8 Projects

| Aspect | Policy |
|---|---|
| Persistence | Confirmed `projects` until deleted; `project_hint` text on items/threads/meetings immediately; `entity_mentions` for project names |
| Freshness | Hints: at extraction. Project suggestion: nightly clustering of hints. Project card: on version change |
| Priority | C3; packed when anchored or when a project is a feature of priority |
| Retrieval | Name/alias match → relational expansion; **topic mode** when unconfirmed (§10.3) |
| Authority | User-created/confirmed > suggested (≥ 3 sources share a hint) > single hint |
| Summarization | Project status summary generated on demand (T2) and cached by project version |
| Staleness | No linked activity for 30 days → "quiet"; 90 days → archived from active lists |
| Conflict | Item linked to two projects → allowed (many-to-many via `entity_links`); user can reassign |

### 11.9 Decisions (and open questions)

| Aspect | Policy |
|---|---|
| Persistence | `decisions` with `superseded_by_id` / `resolved_by_id`, evidence; permanent until deletion |
| Freshness | At meeting/email extraction |
| Priority | Confirmed and recent (≤ 30 days) decisions are C3; open questions linked to upcoming meetings are C2 |
| Retrieval | Temporal + hybrid over decision cards for "what did we decide about X"; supersession chain via recursive CTE |
| Authority | User-confirmed > explicit decision language in source with decision-maker present > inferred agreement |
| Summarization | None; the statement is the summary |
| Staleness | Decisions do not expire; superseded decisions are shown only as history. Open questions with no activity for 30 days → "possibly abandoned" label |
| Conflict | A later contradicting decision creates `superseded` only when explicit ("we're switching to B"); otherwise `conflict_detected` and both shown |

### 11.10 Recent events

| Aspect | Policy |
|---|---|
| Persistence | `context_events` (permanent, small rows); day view computed on demand |
| Freshness | Events written in the same transaction as the change; the day view always reflects current events, including late-processed sources |
| Priority | C4; materiality ≥ 2 by default |
| Retrieval | Temporal by `recorded_at` (what changed) or `occurred_at` (what happened), scoped by entity |
| Authority | Inherits from the event's source and actor |
| Summarization | Structured day view (phrased by AI-06 only when asked in chat); net-change folding instead of summarizing event lists |
| Staleness | Events older than 14 days leave C4 but remain queryable for timelines |
| Conflict | Represented as `conflict_detected` events |

### 11.11 Historical context

| Aspect | Policy |
|---|---|
| Persistence | `chunks`, messages (body retention per policy), transcripts, closed items, old summaries |
| Freshness | Embeddings within minutes (interactive) or hours (backfill) |
| Priority | C5; packed last, only via retrieval |
| Retrieval | Hybrid with time filters and recency decay; then pivot to items/entities when possible |
| Authority | Source text; older than any newer statement on the same item |
| Summarization | Existing thread/meeting summaries used for skimming; no new summarization at query time unless the user asks for a long-range recap (T2, bounded) |
| Staleness | When raw bodies are purged, evidence quotes and summaries remain; answers note "original message no longer stored" |
| Conflict | Historical text never overrides current folded state |

---

## 12. Write-side rules that make continuity possible

Retrieval can only connect what ingestion linked. These rules run at ingest time.

### 12.1 Candidate context for extraction

Every T1 extraction call receives up to 8 **candidates** so the model can attach updates instead of creating duplicates:

1. Open items and open questions in the same thread.
2. Open items and open questions involving the same participants (last 60 days).
3. Items semantically close to the message (`dedupe_embedding` ≥ 0.80) with a shared participant or project hint.
4. Most recent decisions on the same project hint.
5. Items rejected in the last 90 days that match 1–3 (flagged as rejected, §12.3).

Ordering: same thread → same participants + highest priority → semantic → rejected. The candidate list is part of the extraction's `input_hash`; it is captured when the extract job runs. Because new items may appear between extraction and apply, the **apply** stage re-runs deterministic candidate matching under the per-user merge lock (`TECHNICAL_DESIGN.md` §13.6, `BACKEND_DESIGN.md` §12.4); that is what prevents duplicates when two sources are processed concurrently.

### 12.2 Linking outputs

- Status signals attach to candidates by ID; invalid IDs are dropped.
- New items go through dedupe/merge (`TECHNICAL_DESIGN.md` §13.6), now including open questions and decisions.
- Thread continuation (A7): computed for new threads deterministically (participants + subject trigram + body embedding).
- `entity_mentions` (A4): alias matching on clean body and transcript text against the user's known persons/projects, plus mentions returned by extraction.

### 12.3 Respecting corrections (A12)

- Candidates include items rejected in the last 90 days, flagged as rejected.
- A new mention matching a rejected item is dropped, unless it is an **explicit** statement by the owner after the rejection date; then it is re-suggested with the label "previously dismissed" and the new evidence.

### 12.4 Time-driven events

The hourly sweeper writes `became_overdue`, `due_soon`, `became_stale` events (`actor = time`, deduplicated per entity and hour bucket), so time-based changes appear in the change feed and in "what changed".

### 12.5 Memory promotion rules

| From | To | Rule |
|---|---|---|
| Source text | Extraction (detected) | Prefilter passes; AI-01/AI-10 succeeds |
| Detected | Work item / decision (`suggested`) | Schema valid; evidence quote found in source; confidence ≥ 0.5; not a duplicate (else attached as evidence) |
| Suggested | Visible on Today | Confidence ≥ 0.6, or priority ≥ 80 (shown as "possible — please confirm") |
| Suggested | Confirmed | **User action only** |
| Suggested | Archived (logical, recoverable) | No confirmation and no new evidence for 14 days and priority < 40 |
| Any | Rejected | User action; feeds suppression and negative examples |
| Interaction statistics | `importance_inferred` | Deterministic nightly; `importance_user` always wins |
| Project hint | Suggested project | ≥ 3 sources share the hint; confirmed by the user |
| Speaker label | Mapped person | Automatic at confidence ≥ 0.9; otherwise user confirmation |

### 12.6 Transactions

Every write described in this section happens inside the apply transaction or an outbox-driven handler transaction defined in `BACKEND_DESIGN.md` §7–§9; no context state is written outside those paths.


### 12.7 Phase 2 parameters for linking and time events (decided 2026-10-02)

| Rule | Decision |
|---|---|
| Thread continuation (§12.2, A7) | Computed by the index job for the first indexed message of a conversation. Candidates: other conversations with chunks in the 30 days before the message whose participant overlap \|A ∩ B\| / min(\|A\|, \|B\|), over `person_ids` without the user, is ≥ 0.5. A link is written to the best candidate when the subject trigram similarity (after removing `Re:`/`Fwd:` prefixes) is ≥ 0.5 or the cosine similarity of the two first chunks is ≥ 0.80. `confidence` = the higher of the two scores; one link per new conversation; `relation = continues`, `origin = computed`, method `deterministic` (subject) or `embedding_match` (body) |
| Alias scan (`entity_mentions`, A4) | Person name aliases of ≥ 2 words and ≥ 5 characters, and names and aliases of confirmed projects of ≥ 4 characters, are matched as whole-word, case-insensitive phrases in each chunk's text; the self Person is excluded. Confidence 0.9 (persons) and 0.8 (projects); `method = alias_match`; the source's alias mentions are replaced on every re-index. Extraction mentions (AI-01 `mentions`, resolved within participants) are written by apply as before |
| Time sweep (§12.4) | Hourly, per user. `became_overdue`: open item with a non-fuzzy `due_at` ≤ now. `due_soon`: `due_at` in (now, now + 24 h]. `became_stale`: open item whose `last_activity_at` is older than 7 days, or a `waiting_for`/`delegated` item past due with no event for 5 working days; it also sets the projection column `stale`, which the sweep clears once activity resumes. Dedupe key: SHA-256 of (`time`, item, event type, the UTC hour bucket of the transition time: `due_at` for overdue, `due_at` − 24 h for due soon, the moment the stale policy was crossed for stale), so a rerun or a late sweep writes each transition once. `actor = time`, authority 1, materiality 2, `occurred_at` = the transition time. Time events set no field and do not count as activity: the status fold (§8) ignores them for `last_activity_at` |

---

## 13. Session context

| Element | Rule |
|---|---|
| Turns kept verbatim | Last 4 (≤ 1.5 K tokens) |
| Older turns | Dropped from the packet after turn 4; the entity focus map and scope carry continuity (AI-13 session summaries retired after review; `CONTEXT_EVALUATION.md` SS scripts verify coreference) |
| Entity focus map | Entities cited in recent answers, with recency; used to resolve "he", "that", "the review" |
| Scope | Global or meeting-scoped; a meeting scope allows prior meetings found by §10.4 step 3 |
| Reuse | None in the MVP; each turn assembles a fresh packet (packet reuse removed in the complexity audit) |
| Expiry | Session focus resets after 2 h of inactivity; summary kept for history |
| Focus map content (Phase 2) | `chat_sessions.session_entities`: at most 10 entries `{type, id, label, turn}`, newest first, from each turn's resolved anchors and the entities its answer cited. "he/him/his/she/her/they/them" resolve to the newest person; "it/that/this" to the newest item, decision, project or conversation. The map is updated in the transaction that stores the answer |
| Change anchor in chat | "What changed?" without a time anchors at the `chat` checkpoint, else the `today` checkpoint (§7.1 rules for old checkpoints); the `chat` checkpoint moves to the answer time once the answer is stored |

---

## 14. Cost, latency and simplicity check

- **Scenarios 7, 8, 9 and the dashboard** run with zero or one cheap model call; their correctness depends on extraction quality, not retrieval cleverness.
- **Scenarios 1 and 2** are UI panels with no model call by default.
- **Scenarios 3, 5, 6, 11, 12** need one T2 call each with 4–7 K dynamic tokens ≈ $0.015–0.025 per answer at post-promotion prices.
- **Scenario 4** uses precomputed deterministic sections; AI-11 adds suggested asks only when the user opens the prep view.
- **Scenario 10** is served from the on-demand day view phrased by AI-06.
- **Components** added by this document: `context_events` (one change feed), `user_checkpoints`, `entity_mentions`, version columns, one hourly sweeper, one deterministic fold function. No new infrastructure.

---

## 15. Reconciliation status

The changes this document required in `TECHNICAL_DESIGN.md` (single `context_events` feed, checkpoints, entity mentions, fold, scenario-based retrieval, candidate lists with open questions/decisions/rejections, time sweeper, evaluation references) were applied on 2026-10-02. The earlier `daily_digests` table and rendered-card cache were replaced by on-demand queries and deterministic rendering after the complexity audit (`ARCHITECTURE_REVIEW.md` §E).

---

## 16. Decision summary (context-specific)

| Decision | Choice | Rejected alternatives | Why |
|---|---|---|---|
| Backbone | Relational entities + folded state + event timelines | Vector memory; knowledge graph | State questions need exact, correctable answers; ego-network is shallow |
| Knowledge graph | **Not in MVP**; revisit on measured triggers (§6.2) | Graph DB now; GraphRAG now | No ≥ 3-hop scenario; avoids a second store and LLM indexing cost |
| Temporal | Bi-temporal events, change feed, checkpoints, explicit semantics | Time filters only | "What changed", "yesterday" and "latest status" are core questions |
| Discovery | Hybrid (vector + FTS, RRF) | Vector only; keyword only | Names and codes need keyword; paraphrase needs vectors |
| Expansion | Typed relational recipes, ≤ 2 hops | Agentic multi-step search | Predictable latency/cost; testable |
| Current status | Status fold with authority levels; reported vs. lifecycle | Overwrite latest; LLM decides | Keeps inference non-authoritative; explains changes |
| Negative claims | Coverage block required | Silent absence | Prevents false "not received" claims |
| Projects | Hints immediately + topic mode + user-confirmed projects | Wait for confirmation; auto-create projects | Useful from day one without polluting entities |
| Summaries | Narrative aids with drift control; never fact sources | Hierarchical summaries as memory | Summaries drift; facts must trace to evidence |
