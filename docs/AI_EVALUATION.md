# AI Evaluation — Executive Context Assistant

**Status:** Reconciled (pre-implementation)
**Date:** 2026-10-02
**Authority:** This document is authoritative for AI quality evaluation, the frozen golden dataset, the acceptance scorecard for major AI changes, judges, human review and AI gates. Context continuity and cross-source tests are in `CONTEXT_EVALUATION.md`; backend reliability regression tests (RT-01–RT-15) in `BACKEND_DESIGN.md` §21; cost acceptance rules in `AI_COST_MODEL.md` §9.
**Inputs:** `docs/PRD.md` §41–45, `docs/AI_PIPELINE.md`, `docs/AI_COST_MODEL.md`, `docs/CONTEXT_ARCHITECTURE.md`, `CLAUDE.md` ("AI features require golden examples. Do not accept 'looks good' as an evaluation strategy.")

---

## 1. Principles

1. **Balanced objectives.** A change is judged on quality, cost, latency and reliability together (§8). A higher benchmark score that costs more, is slower or fails more often is not automatically better.
2. **Frozen ground truth.** Major model and prompt changes are accepted only against a frozen, versioned golden dataset with a held-out split that is never used for tuning (§3).
3. **Deterministic metrics first;** LLM judges only for free text, calibrated against humans (§9).
4. **Slices, not averages.** No important slice (operation, sender type, difficulty, adversarial set) may regress beyond tolerance, even if the average improves.
5. **Zero-tolerance safety metrics** (hallucinated commitments, forbidden facts, false absence, false closure, cross-tenant leakage) block regardless of other gains.
6. **Benchmarks are a proxy.** Human review of diffs and online signals from the pilot complement offline scores (§10, §12).
7. No real user data without explicit consent (Google Limited Use).

---

## 2. Coverage map

| Operation (`AI_PIPELINE.md` §4) | Suite | Method under test |
|---|---|---|
| O1 Email classification | E1 | Rules prefilter + AI-01 triage |
| O2 Email summarization | E12 | AI-01 gist, deterministic gist timeline, AI-03 for long threads, deterministic counts |
| O3 Task extraction | E2, DIR-120 | AI-01 statements + deterministic mapping + validation |
| O4 Deadline extraction | E4 | AI-01 `due_text` spans + deterministic resolver |
| O5 Commitment extraction | E3, DIR-120 | AI-01 statements + mapping rules (+ AI-02 only if X1 passes) |
| O6 Person/entity extraction | E13 | Deterministic identity, rules, AI-01 mentions, speaker mapping |
| O7 Priority scoring | E5 | Deterministic formula with AI-01 features |
| O8 Meeting summarization | E11 | AI-09 + AI-10 |
| O9 Meeting task extraction | E11, E2–E4 on transcripts | AI-10 + validation |
| O10 Meeting question answering | E10 | Deterministic structured lookups + scoped retrieval + AI-06/AI-07 |
| O11 Cross-meeting reasoning | `CONTEXT_EVALUATION.md` CC-11–CC-17, CC-38, CC-39, S4, S5 | Relational chains + AI-07/AI-11 |
| O12 Contextual chat | E6, E7, E8, E10, E15, E16 + `CONTEXT_EVALUATION.md` | Retrieval + deterministic rendering + AI-05/06/07 |
| O13 Reply guidance | E10 (reply), human review | AI-08 |
| O14 Daily briefing | Deterministic section and headline template tests | SQL + templates (no AI) |
| O15 Reminder generation | E9 | Deterministic rules |
| Provenance and calibration (all AI-derived objects) | E14 | Envelope completeness, confidence calibration |
| Abstention and overconfidence | E15 | Abstention pre-check, `answerable`, grounding checks |
| Routing efficiency (unnecessary escalation) | E16 | Experiments X1–X10 |
| Reliability of AI stages | §5 + RT-01–RT-15 | Schema validity, failures, retries, crash recovery |

---

## 3. Frozen golden dataset

### 3.1 Content (`world_v1`)

A synthetic, hand-reviewed "executive world": one fictional CTO, ~40 people in 8 organizations, 5 projects, 4 weeks of activity.

| Content | Size | Notes |
|---|---|---|
| Emails | ~1,500 | Newsletters, notifications, ambiguous promises, outbound commitments, forwarded copies, long threads, injection attempts |
| Calendar events | ~60 | Recurring series, reschedules, cancellations |
| Meeting transcripts | 12 | VTT with speakers; 6 also rendered to audio (TTS, ≥ 2 voices) |
| Chat questions | ~250 | All scenarios, meeting Q&A, reply requests, plus the review question set (`AI_PIPELINE_REVIEW.md` §D) |
| `DIR-120` contrastive statements | 120 | 6 statement kinds × speaker (self / owner / third party) × channel (email / transcript) with near-identical wording, e.g. "John will send X" vs "I will send X" vs "waiting for John on X" vs "John asked me to do X" |
| `UNANS-40` unanswerable questions | 40 | Questions whose answer the dataset does not establish (missing source, out-of-window, disconnected source) |
| `ROUTE-60` routing set | 60 | Questions labelled with the minimal sufficient route (deterministic / AI-06 / AI-07) |
| Forwarded and quoted mail | 30 | Forwarded commitments, quoted replies repeating earlier promises |
| Identity fixtures | 25 persons | Same person with two addresses; two people with near-identical names; domain changes |
| Labels | see layout | Two reviewers on commitments, directions, priority pairs and routing labels |

Code and data are separate (slice 0.5 decision). **Data** lives at the repository root under `evals/` (layout below). **Code** (runners, scorecard, statistics, manifest and contamination tools, gate A0) is the Python package `backend/eca_evals/`, installed and checked with the backend (ruff, mypy strict, pytest, import-linter). `eca_evals` may import only `eca`'s public package roots (`eca.<module>`, `eca.platform`); `eca` never imports `eca_evals`; neither imports `google.genai` outside `eca.intelligence`. Commands: `python -m eca_evals <command>` from `backend/`.

```text
evals/ai/datasets/world_v1/
  MANIFEST.json                    # version, label schema version, SHA-256 of every file, split of every case
  scenario.yaml                    # people, orgs, projects, timeline of ground-truth facts
  sources/emails/*.eml  calendar/events.jsonl  meetings/<id>/{transcript.vtt, audio.opus?, meta.json}
  labels/
    triage.jsonl  items.jsonl  decisions.jsonl  entities.jsonl  speakers.jsonl
    summaries.jsonl                # key points per thread/meeting for coverage scoring
    priority_pairs.jsonl  reminders.jsonl  queries.jsonl
evals/ai/baselines/<config_hash>.json   # stored results of the current production configuration
evals/ai/judges/                        # versioned rubrics (data)
evals/ai/cassettes/<suite>/             # reviewed recordings on synthetic data (committed)
evals/ai/cassettes/live/  evals/ai/reports/   # git-ignored
backend/eca_evals/ai/                   # runners (code)
```

### 3.2 Splits

| Split | Share | Use | Visibility |
|---|---|---|---|
| `dev` | 40% | Prompt iteration, debugging, few-shot selection | Full |
| `test` | 45% | **Gate for every major change** | Aggregates and failure categories; individual cases viewable only when investigating a regression, never copied into prompts |
| `sealed` | 15% | Release gate and quarterly audit only | Aggregates only |
| `challenge` | separate (~200 cases) | Adversarial and edge cases: prompt injection, ambiguous promises, third-party claims, timezone/relative-date edges, very long threads, forwarded chains, near-duplicate commitments, speaker confusion | Aggregates and per-category results |

Splits are stratified by operation, sender type (VIP, colleague, external, bulk) and difficulty, and assigned at the **chain/thread level** so related cases never cross splits.

### 3.3 Freeze rules

0. Labels are **draft** until a named human owner (Q12) has reviewed them. `MANIFEST.json` records `labels_status` (`draft` or `reviewed`), the reviewers and the review record. The freeze tool refuses to mark a version released (`frozen: true`) while any label file is draft. CI verifies hashes of draft candidates too, so data changes always go through the manifest tool.
1. A released version (e.g., `golden-v1.0`) is immutable: CI verifies every file against `MANIFEST.json` hashes and fails on any difference.
2. Label corrections create a new version (`v1.1`) with a changelog; the current production configuration is re-baselined on the new version before any candidate is compared.
3. The gate configuration pins the dataset version; changing it is itself a reviewed change.
4. Contamination check in CI: prompts and few-shot files are scanned for n-gram overlap with `test`, `sealed` and `challenge` texts; any overlap above threshold fails the build.
5. Versions are refreshed at most quarterly, adding anonymized, consented production failures; old versions are kept for trend comparison.

---

## 4. Suites and metrics

Targets are MVP release targets on the `test` split; the gate rules in §8 use tolerances relative to the baseline.

### 4.1 E1 — Email classification

| Metric | Method | Target |
|---|---|---|
| `needs_reply` precision / recall | Deterministic | P ≥ 0.85, R ≥ 0.80 |
| Category macro-F1 | Deterministic | ≥ 0.80 |
| Prefilter false-skip rate (relevant mail skipped by rules) | Deterministic | ≤ 2% overall; 0% for VIP senders |
| `request_type`, `business_impact` accuracy | Deterministic | ≥ 0.75 (feeds priority) |

### 4.2 E2 — Task extraction

| Metric | Method | Target |
|---|---|---|
| Precision, recall | Match = same type family + owner + counterparty + description similarity ≥ 0.8 (embedding); ambiguous matches adjudicated by judge and spot-checked | P ≥ 0.85, R ≥ 0.75 |
| False positives per 100 relevant emails | Deterministic | ≤ 3 |
| Grounding drop rate | Apply logs | Tracked; spikes investigated |

### 4.3 E3 — Commitment extraction

| Metric | Method | Target |
|---|---|---|
| Precision of `explicit` commitments | Deterministic | ≥ 0.90 |
| Recall (all commitments) | Deterministic | ≥ 0.75 |
| `commitment_strength` accuracy | Deterministic | ≥ 0.80 |
| `statement_kind` accuracy | Deterministic vs labels | ≥ 0.90 |
| Direction accuracy (my_commitment / my_task / waiting_for / delegated / observed) | Deterministic | ≥ 0.95 overall |
| **Direction confusion on `DIR-120`** (any case mapped to the wrong type or direction among the canonical patterns) | Deterministic | **0** for the 4 canonical phrases; ≥ 0.95 overall |
| Wrong owner (owner set to the wrong person) | Deterministic | ≤ 2%; **0** for `my_commitment` assigned to the user when the speaker was someone else |
| Forwarded-content attribution (no `my_commitment` for the forwarder) | Deterministic | **0** violations |
| Hallucinated commitments after grounding | Deterministic + human sample | **0** |
| Adjudication lift (AI-02, experiment X1) | Precision/recall with vs without AI-02 on the escalation slice | AI-02 stays disabled unless X1 passes |

### 4.4 E4 — Deadline extraction

| Metric | Method | Target |
|---|---|---|
| Exact-date accuracy at labelled precision | Deterministic | ≥ 0.90 |
| Owner and source accuracy | Deterministic | ≥ 0.90 / ≥ 0.95 |
| Resolver unit tests (relative dates, business days, timezones, DST) | Deterministic | 100% |
| **Invented dates** (`due_at` set without a verbatim `due_text` in the evidence, or from `due_iso_guess`) | Deterministic over stored items | **0** |
| Vague phrases ("soon") stored with `due_at = null`, precision `fuzzy` | Deterministic | 100% |

### 4.5 E5 — Priority

| Metric | Method | Target |
|---|---|---|
| Pairwise agreement with human preferences | Deterministic | ≥ 0.75 |
| Precision@5 of the attention set | Deterministic | ≥ 0.80 |
| NDCG@10 of needs-response ranking | Deterministic | ≥ 0.80 |
| Reason consistency (top reasons match top features) | Deterministic | 100% |

Weights are fitted on `priority_pairs` in `dev` and validated on `test`.

`labels/priority_pairs.jsonl` (format decided 2026-10-03; the labels are not written yet): one JSON object per line, `{"pair_id", "split", "a": {"kind": "work_item" | "conversation", "case_id", "features": {<feature>: value in [0, 1]}}, "b": {…}, "preferred": "a" | "b", "reviewers": […]}`, with the §12.6 feature names of `TECHNICAL_DESIGN.md`. Pairwise agreement = the share of pairs where the preferred side scores higher. The offline fit and its agreement are produced by `eca ops priority-fit --pairs … --split dev` (`TECHNICAL_DESIGN.md` §12.8). Online, the per-user pairs that users create by overriding priorities are stored in `priority_pairs` with the same feature names, and `user_priority_weights.agreement` reports the fitted agreement per user.

### 4.6 E6 — Retrieval relevance

| Metric | Method | Target |
|---|---|---|
| Recall@10 vs required sources | `retrieval_traces` | ≥ 0.90 list scenarios, ≥ 0.80 synthesis |
| Precision@10, NDCG@10 | Deterministic | Tracked; tolerance §8 |
| Context precision, useful-token ratio | `CONTEXT_EVALUATION.md` §8.2 | ≥ 0.6 / ≥ 0.4 |

### 4.7 E7 — Groundedness

| Metric | Method | Target |
|---|---|---|
| Citation validity | Deterministic | ≥ 0.98 |
| Claim support rate | Judge per claim, human-audited sample | ≥ 0.95 |
| Absence claims with coverage; false absence | Deterministic | ≥ 0.98; **0** false absence |
| Claim-kind accuracy (source / user / inference / recommendation / absence) | `claims.kind` vs labels + judge | ≥ 0.95; **0** recommendations rendered as `source` |
| Online grounding check agreement (deterministic check vs judge) | Comparison | ≥ 0.90 (validates the online checks in `AI_PIPELINE.md` §5.7) |

### 4.8 E8 — Factual accuracy

| Metric | Method | Target |
|---|---|---|
| Required-fact coverage | Deterministic + judge for paraphrase | ≥ 0.85 |
| Forbidden-fact rate | Deterministic + judge | **0** on ★ and challenge-safety cases; ≤ 1% overall |
| Entity accuracy in answers | Cited IDs | ≥ 0.95 |

### 4.9 E9 — Reminder usefulness

| Metric | Method | Target |
|---|---|---|
| Precision (fired in expected windows) | Simulated clock | ≥ 0.80 |
| False-positive rate (should-not-remind) | Simulated clock | ≤ 0.10 |
| Miss rate | Simulated clock | ≤ 0.10 |
| Suppression correctness | Deterministic | 100% |

### 4.10 E10 — Chat, meeting Q&A and reply guidance

| Metric | Method | Target |
|---|---|---|
| Answer relevance and completeness | Calibrated judge rubric | ≥ 4.0 / 5 mean |
| Routing correctness on `ROUTE-60` | Deterministic | ≥ 0.95 |
| Meeting Q&A exact answers (owners, deadlines, decisions) | Deterministic vs labels | ≥ 0.90 |
| Reply guidance: previous agreement and status correct | Judge + human review | ≥ 0.90 |
| Reply drafts: no invented commitments or facts | Judge + human review | **0** |

### 4.11 E11 — Meetings

| Metric | Method | Target |
|---|---|---|
| Word error rate (TTS audio) | Deterministic | Tracked; drives transcription model choice |
| Diarization error rate | Deterministic | Tracked |
| Decision recall / open-question recall | Deterministic vs labels | ≥ 0.85 / ≥ 0.80 |
| Meeting items and deadlines | As E2–E4 | Same targets |
| Summary key-point coverage; unsupported statements | Key points from `summaries.jsonl`; judge for support | ≥ 0.80; ≤ 2% |

### 4.12 E12 — Summarization (gist, gist timeline, long-thread summary, briefing)

| Metric | Method | Target |
|---|---|---|
| Key-point coverage (gist timelines for 3–8 messages; AI-03 for ≥ 8) | Labelled key points | ≥ 0.80 (experiment X3 compares both) |
| Unsupported statements | Judge | ≤ 2% |
| Gist faithfulness | Judge on sample | ≥ 0.95 |
| Briefing sections and headline match the structured state | Deterministic | 100% |
| Daily email summary counts | Deterministic | 100% match |

### 4.13 E13 — Person and entity extraction

| Metric | Method | Target |
|---|---|---|
| Wrong person merges (including near-identical names in the identity fixtures) | Deterministic | **0** |
| Same person with two addresses: not auto-merged; unified after user merge | Deterministic | 100% |
| Mention resolution precision / recall | `entities.jsonl` | ≥ 0.90 / ≥ 0.70 |
| Organization assignment accuracy | Deterministic | ≥ 0.95 |
| Auto-applied speaker mapping accuracy | `speakers.jsonl` | ≥ 0.95 |

### 4.14 E14 — Provenance and calibration

| Metric | Method | Target |
|---|---|---|
| Provenance completeness (source, confidence, timestamp, method, model, evidence) on every AI-derived object | Deterministic over the run's database | 100% |
| Expected calibration error per role (confidence vs correctness) | Deterministic | ≤ 0.10 |
| Confidence band precision (high-band items correct) | Deterministic | ≥ 0.90 |
| Provenance answers: for a random 50 objects, every question in `AI_PIPELINE.md` §5.1 resolvable through the API | Deterministic | 100% |

### 4.15 E15 — Abstention and overconfidence

| Metric | Method | Target |
|---|---|---|
| Abstention recall on `UNANS-40` (system says the context does not establish the answer) | Deterministic (`answerable = false` or abstention template) | ≥ 0.90 |
| False abstention on answerable questions | Deterministic | ≤ 5% |
| Overconfident answers (answer `confidence = high` with any unsupported or forbidden claim) | Judge + deterministic | ≤ 1%; **0** on ★ cases |
| Stale-context disclosure (sync gap, disconnected source) | Deterministic | 100% |
| Contradiction disclosure (conflicting sources shown, not resolved silently) | `CONTEXT_EVALUATION.md` CC-10, CC-39 | 100% |

### 4.16 E16 — Routing efficiency

| Metric | Method | Target |
|---|---|---|
| Unnecessary escalation rate (a cheaper route on `ROUTE-60` meets the E10 rubric equally) | Run cheaper arm on escalated cases | ≤ 10% |
| AI-02 escalations that changed the outcome (if enabled) | Compare with T1 result | ≥ 30%, else disable |
| Deterministic-answer share for list intents | Deterministic | 100% |
| Strong-model calls per active user per day vs `AI_COST_MODEL.md` §4 | Telemetry on evaluation tenant | Within 20% |

---

## 5. Reliability metrics (every live run)

| Metric | Target |
|---|---|
| Schema-valid on first attempt, per role | ≥ 99% |
| Repair-call rate | ≤ 1% |
| `failed_permanent` rate | ≤ 0.5% |
| Timeout / provider error rate (after retries) | ≤ 0.5% |
| Retry overhead (calls with attempt > 1) | ≤ 5% |
| Run-to-run variance (3 repeated live runs at the configured temperature) on primary metrics | ≤ 1.5 points |
| Fallback-model parity on primary metrics | Within 5 points of the primary model |

Backend crash/duplicate/concurrency behaviour is covered by RT-01–RT-15 and must be green for any release.

---

## 6. Latency metrics

| Operation | Target |
|---|---|
| Chat list answers (O12, O10 lookups) | p95 ≤ 3 s end-to-end; time to first token ≤ 1.2 s |
| Chat synthesis, cross-meeting, reply guidance | p95 ≤ 7 s; time to first token ≤ 2.5 s |
| Email extraction (background) | p95 ≤ 5 min from sync to applied state |
| Meeting processing (1 h recording) | p95 ≤ 15 min to ready |
| Prep brief | Available before T−30 min for 99% of meetings with context |

Latency is measured on live runs with production-like packets; regressions beyond 15% on any p95 fail the gate unless offset per §8.

---

## 7. Cost metrics

Each live run reports cost per operation, tokens per role (mean, p95) and projected typical/heavy daily cost (`AI_COST_MODEL.md` §4). Acceptance thresholds are defined in `AI_COST_MODEL.md` §9.

---

## 8. Acceptance scorecard for major changes

A major change (`AI_PIPELINE.md` §12) runs: baseline (current production config) vs candidate on `test` + `challenge`, live, 3 repetitions for primary metrics; `sealed` additionally before a release.

### 8.1 Scorecard

| Dimension | Measured | Pass condition |
|---|---|---|
| Safety (zero tolerance) | Hallucinated commitments after grounding, invented dates, canonical-phrase direction confusion on `DIR-120`, `my_commitment` assigned to the user for someone else's promise, forbidden facts on ★/challenge-safety cases, false absence, false closure, wrong person merges, cross-tenant leakage, reply drafts with invented facts, recommendations rendered as source facts, user-authored values changed by AI (`CONTEXT_EVALUATION.md` CC-29–CC-31, CC-43) | All zero |
| Quality | Primary metrics of affected operations, per slice | No slice regresses by more than the tolerance (2 points absolute, or the lower bound of the 95% paired-bootstrap CI below −2 points) |
| Reliability | §5 | All targets met; no metric worse than baseline beyond 0.5 points |
| Latency | §6 | Targets met; p95 not worse than baseline by > 15% |
| Cost | `AI_COST_MODEL.md` §9 | Within the band for the size of the change |
| Human review | 20 randomly sampled output diffs (baseline vs candidate) reviewed by a person | No systematic regression noted |

### 8.2 Decision rule

Accept a candidate when **all** pass conditions hold **and** at least one of:

- a primary quality metric improves significantly (95% CI excludes 0), or
- projected cost decreases ≥ 10%, or
- p95 latency decreases ≥ 15%, or
- a reliability metric improves (e.g., first-attempt schema validity, failure rate), or
- the change is required (model retirement, security fix) and meets the pass conditions.

Ties on all dimensions keep the baseline. A quality gain on the average that hides a slice regression is rejected (principle 4).

### 8.3 Statistics

Paired bootstrap over cases (10,000 resamples) for differences; cluster resampling by thread/chain; minimum slice size 30 cases (smaller slices are reported but not gated). Run-to-run variance from the 3 repetitions is added to the tolerance check.

### 8.4 Gates

| Gate | When | Blocks if |
|---|---|---|
| A0 | Every PR | Dataset hash mismatch; contamination check fails; cassette-based suite regresses; E14 provenance completeness < 100% |
| A1 | Major change to extraction roles (AI-01, AI-02, AI-10), schemas, statement mapping, confidence calibration or prefilter | Scorecard fails on E1–E4 (incl. `DIR-120`), E11, E13, E14 |
| A2 | Priority weights or features | Scorecard fails on E5 |
| A3 | Retrieval, packet assembly, grounding checks, abstention, chat roles (AI-05–AI-08, AI-11) | Scorecard fails on E6–E8, E10, E15, E16 or `CONTEXT_EVALUATION.md` G3/G4 |
| A4 | Model ID, fallback, thinking level or temperature change | Full scorecard on all affected suites, including fallback parity |
| A5 | Release | `sealed` split confirms `test` results within tolerance; all §4 targets met; `CONTEXT_EVALUATION.md` G6; RT-01–RT-15 green |

---

## 9. LLM-as-judge

- Used only for paraphrase matching, claim support, relevance, completeness, summary faithfulness and reply-draft checks.
- Judge role `judge` (`AI_PIPELINE.md` §2), never the generator's model and prompt.
- Versioned rubrics in `evals/ai/judges/`; structured per-claim verdicts with quotes.
- Calibration: 100 human-labelled examples per rubric; Cohen's κ ≥ 0.7 before a judge metric can gate; recalibrated when judge model or rubric changes. Judge drift is checked by re-scoring a fixed calibration set on every judge change.

## 10. Human review

| What | Sample | Frequency |
|---|---|---|
| Candidate vs baseline output diffs | 20 per major change | Each gate run |
| Explicit and probable commitments | 50 | Weekly in pilot (consented data) |
| High-priority items (≥ 80) | 30 | Weekly |
| Reply guidance drafts | 20 | Weekly |
| Judge/deterministic disagreements | All | Nightly |
| User-reported wrong answers | All | Weekly, root-caused to stage and converted into `challenge` cases for the next dataset version |

## 11. Harness

- Offline runner replays the dataset through the real pipelines with a simulated clock.
- Cassettes (recorded model responses keyed by role, prompt version and input hash) make CI deterministic and free; any major change forces live runs for affected roles. Format, modes and storage: `BACKEND_DESIGN.md` §5.5.
- Stub pipelines (deterministic code, no model) let the runners, statistics and scorecard be tested end to end before the real pipelines exist (slice 0.5).
- Live runs write normal `ai_calls` rows to a dedicated evaluation tenant so cost and latency use the production meter.
- Reports: JSON + Markdown per run (`evals/ai/reports/<date>_<config_hash>`), including the scorecard and slice tables.

## 12. Online evaluation

| Stage | Scope | Guardrails |
|---|---|---|
| Shadow (model switches only) | Candidate runs on a 10% sample of consented pilot traffic; outputs stored, not shown | Cost cap per day; compare with production outputs on agreement and validation failures |
| Canary | Candidate serves 20% of pilot users for 7 days | Roll back if reject rate, thumbs-down rate, re-ask rate or reminder dismiss rate worsen beyond thresholds, or cost per useful outcome rises > 10% |
| Steady state | All users | Weekly dashboards |

Online metrics: confirm/reject ratio of suggested items; deadline/owner edit rate; reminder dismiss/snooze rates; chat thumbs-down and re-ask rate; correction rate; cost per useful answer and per useful item (`AI_COST_MODEL.md` §8); context-recovery-time proxy (time from opening Today to first action).

## 13. Building the dataset

1. Hand-write the scenario timeline and the ★ cases first; generate variants with a strong model; reviewers check 20% of variants and 100% of commitment and priority labels.
2. Label with two reviewers where judgement is needed (commitments, priority pairs, summaries' key points); resolve disagreements; record agreement.
3. Assign splits at chain/thread level; write `MANIFEST.json`; tag `golden-v1.0`.
4. Run the current configuration to create the first baseline.
5. Phase alignment: email slice (~150 emails, CC-01–CC-10) by slice 0.5 (`IMPLEMENTATION_PLAN.md`, the authority for execution order); `DIR-120` with AI-01 and the statement mapping it tests, by slice 1.4; full email set, forwarded-mail and identity fixtures by end of Phase 1; chat questions, `UNANS-40` and `ROUTE-60` by Phase 2; meetings by Phase 4 (`IMPLEMENTATION_PLAN.md`).
6. Experiment results (X1–X10, `AI_PIPELINE.md` §13) are stored with the run reports and referenced in the change that adopts or rejects a route.
