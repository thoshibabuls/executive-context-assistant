# AI Cost Model — Executive Context Assistant

**Status:** Reviewed (pre-implementation; see `AI_PIPELINE_REVIEW.md`)
**Date:** 2026-10-02
**Authority:** This document is authoritative for AI prices, unit costs, usage profiles, per-user and pilot cost estimates, cost drivers and levers, sensitivity, cost guardrails, cost telemetry and cost acceptance rules for changes. The operations and calls priced here are defined in `AI_PIPELINE.md`.

---

## 1. Principles

1. Cost is a product requirement (PRD §6.6, §37, §45): optimize **cost per useful outcome**, not lowest price per token, and never at the expense of zero-tolerance quality metrics.
2. Estimates use **post-promotion** prices.
3. Every number traces to `calls × (input_tokens × input_price + output_tokens × output_price)`.
4. **No cache discounts are assumed** (conservative): chat packets have stable prefixes below Gemini's 4,096-token implicit-cache minimum and meeting transcripts are not reused across calls.
5. Measured values from `ai_calls` replace estimates after Phase 1; a > 30% gap between measured and modelled tokens triggers a model update.

---

## 2. Prices (Gemini API paid tier, per 1 M tokens; Google pricing page, 2026-10-02)

| Model | Role class | Input | Output (incl. thinking) | Notes |
|---|---|---|---|---|
| `gemini-3.1-flash-lite` | T1 | $0.25 (text) / $0.50 (audio) | $1.50 | — |
| `gemini-3.5-flash-lite` | T1 fallback | $0.30 | $2.50 | — |
| `gemini-3.8-flash` | T2 | $1.50 (promotional $0.75 until 2026-12-31) | $7.50 (promotional $3.75) | Post-promotion prices used |
| `gemini-3.5-flash` | T2 fallback | $1.50 | $9.00 | — |
| `gemini-3.1-pro-preview` | Judge | $2.00 | $12.00 | Offline only |
| `gemini-3.5-transcribe` | Speech | $0.003 / audio min | $0.002 / audio min | ≈ $0.30 per audio hour |
| `gemini-embedding-2` | Embedding | $0.20 | — | — |
| Batch API | any | 50% of standard | 50% | Up to 24 h latency |
| Implicit cache hits | Gemini 3.x | ~10% of input price | — | Prefix ≥ 4,096 tokens (not assumed) |

Prices live in `config/pricing.yaml` with effective dates (`effective_from` inclusive, `effective_to` exclusive, UTC dates). The cost of a call uses the entry effective on the call's date, so the promotional T2 price applies until 2026-12-31 and the listed price after it; the estimates in this document keep using post-promotion prices (§1).

---

## 3. Unit costs per AI call

| ID | Role | Model | Input tokens | Output tokens (incl. thinking) | Unit cost | Basis of the input estimate |
|---|---|---|---|---|---|---|
| AI-01 | `email_extract` | T1 | 2,000 | 325 | $0.00099 | Instructions + schema ~800, user card ~150, 8 candidates ~480, clean body ~400–600 |
| AI-02 | `adjudicate` | T2 medium | 3,000 | 1,000 | $0.0120 | Disabled until X1 |
| AI-03 | `thread_summary` | T1 | 3,000 | 250 | $0.0011 | Threads ≥ 8 relevant messages only |
| AI-04 | `embed` | Embedding | per 1 K | — | $0.0002 per 1 K tokens | — |
| AI-05 | `plan_query` | T1 | 1,500 | 200 | $0.00068 | — |
| AI-06 | `answer_lookup` | T1 low | 4,000 | 400 | $0.0016 | Scoped transcript/thread chunks |
| AI-07 | `answer_synthesis` | T2 low | 8,000 | 1,400 | $0.0225 | Packet budgets `CONTEXT_ARCHITECTURE.md` §9.6 |
| AI-08 | `reply_guidance` | T2 low | 6,000 | 1,200 | $0.0180 | — |
| AI-09 | `transcribe` | Speech | 1 h audio | transcript | $0.30 per hour | — |
| AI-10 | `meeting_extract` | T2 medium | 22,000 per meeting hour | 5,000 | $0.0705 per meeting hour | ~12–15 K transcript tokens/hour + prior context + instructions |
| AI-11 | `meeting_asks` | T2 low | 2,000 | 600 | $0.0075 | Deterministic prep sections only |
| AI-12, AI-13 | retired | — | — | — | $0 | Deterministic replacements |
| AI-14 | `judge` | Pro | 4,000 | 800 | $0.0176 | Offline |

**Derived unit costs (PRD §45):** per relevant email ≈ $0.0011 (AI-01 + embedding); per email received (heavy profile) ≈ $0.0006; per chat answer ≈ $0 (deterministic list), $0.0016 (lookup), $0.0225 (synthesis); per meeting hour ≈ $0.37; per reply draft ≈ $0.018.

---

## 4. Usage profiles and daily cost per active user

| Assumption | Light | Typical | Heavy |
|---|---|---|---|
| Emails received + sent / day | 40 + 5 | 90 + 15 | 150 + 30 |
| Relevant (50% of inbound + all outbound) | 25 | 60 | 105 |
| Long-thread summaries (AI-03) | 0.5 | 2 | 5 |
| Embedded tokens | 25 K | 60 K | 120 K |
| Chat: planner / lookup / synthesis (list intents are deterministic) | 2 / 1 / 1 | 5 / 2 / 3 | 10 / 3 / 6 |
| Reply drafts | 0.2 | 0.5 | 1 |
| Recorded meeting hours / week | 0.5 | 1 | 2 |
| Meeting prep views opened with asks | 0.3 | 0.7 | 1.5 |

| Call | Light $/day | Typical $/day | Heavy $/day |
|---|---|---|---|
| AI-01 | 0.0248 | 0.0594 | 0.1040 |
| AI-03 | 0.0006 | 0.0022 | 0.0055 |
| AI-04 | 0.0050 | 0.0120 | 0.0240 |
| AI-05 | 0.0014 | 0.0034 | 0.0068 |
| AI-06 | 0.0016 | 0.0032 | 0.0048 |
| AI-07 | 0.0225 | 0.0675 | 0.1350 |
| AI-08 | 0.0036 | 0.0090 | 0.0180 |
| AI-09 | 0.0214 | 0.0429 | 0.0857 |
| AI-10 | 0.0050 | 0.0101 | 0.0201 |
| AI-11 | 0.0023 | 0.0053 | 0.0113 |
| **Total (AI-02 disabled)** | **≈ $0.09** | **≈ $0.21** | **≈ $0.42** |
| AI-02 enabled (if X1 passes) | +$0.012 | +$0.024 | +$0.060 |
| At promotional T2 prices (until 2026-12-31) | ≈ $0.07 | ≈ $0.17 | ≈ $0.32 |

Per month (22 active days): light ≈ $1.95, typical ≈ $4.75, heavy ≈ $9.15.

**Heavy-user cost shares:** AI-07 33%, AI-01 25%, AI-09 21%, AI-04 6%, AI-10 5%, AI-08 4%, others 6%.

---

## 5. One-time and periodic costs

| Item | Estimate | Notes |
|---|---|---|
| Initial 30-day import, heavy user | ≈ $3.50 | 3,150 relevant messages × $0.00099 + embeddings ≈ $0.38 |
| Initial import, typical / light user | ≈ $2.00 / ≈ $0.85 | — |
| Full live evaluation run | ≈ $15 | ~1,500 emails, ~250 chat cases, 12 meetings, ~300 judged answers |
| Experiment runs (X1–X10) | ≈ $5–30 each | Arms × affected slices |
| CI evaluation (cassettes) | ≈ $0 | Live only for changed roles |
| Re-extraction of a 30-day window (heavy) | ≈ $3.10 | Never automatic |

**Pilot forecast (100 users: 20 heavy, 60 typical, 20 light; 22 active days):** ≈ $23/day, ≈ $505/month (≈ $570 with AI-02 enabled); ≈ $205 one-time import; evaluation and experiments ≈ $100–200/month during development.

---

## 6. Savings from design choices (heavy user, per day)

| Choice | Saving |
|---|---|
| Rules prefilter of inbound bulk mail (75 of 150 inbound) | ≈ $0.08 |
| T1 instead of T2 for AI-01 ($0.0054 → $0.00099 per email) | ≈ $0.46 |
| One combined AI-01 call instead of separate classification + extraction | ≈ $0.06 |
| Deterministic list answers instead of AI-06 (9 per day) | ≈ $0.02 and ~1 s latency per answer |
| Gist timeline instead of AI-03 for threads of 3–8 messages | ≈ $0.02 |
| Lazy AI-11 on deterministic sections instead of full T2 prep briefs | ≈ $0.025 |
| AI-02 disabled until proven | ≈ $0.06 |
| Deterministic briefing, reminders, priority, digests, person cards | ≈ $0.02 and fewer failure modes |
| No reprocessing on label changes | up to ≈ $0.10 |
| Audio-only meetings | large (video tokens far exceed audio) |
| **Deferred:** Batch API for import | ≈ $1.75 once per heavy user; ≈ $100 for the whole pilot |

Batch import is deferred because the saving does not justify a submission ledger, polling and up to 24 h latency at pilot scale; revisit above ~500 new users per month.

---

## 7. Guardrails

| Guardrail | Default | Action (routes in `AI_PIPELINE.md` §14) |
|---|---|---|
| Per-user daily soft cap | $1.00 | AI-07 → deterministic structured answer + AI-06 where possible, with notice; AI-11 off |
| Per-user daily hard cap | $2.50 | Background AI paused except VIP and outbound extraction; chat deterministic intents only; operator alert |
| Per-call input caps | `CONTEXT_ARCHITECTURE.md` §9.6 | Packet trimmed by tier priority |
| AI-02 escalations (if enabled) | 20 / user / day | Keep T1 result |
| Meetings | ≤ 3 h per upload; ≤ 10 h per user per week | Reject with message |
| Attempt cap | 4 calls per extraction key | `failed_permanent` |
| Re-extraction | Dry-run estimate + operator approval | — |
| Global daily budget | Per environment | Alert at 70%; stop background AI at 100% |
| Evaluation budget | $200 / month default | Live runs queued or approved |

---

## 8. Telemetry

Every call writes `ai_calls` (role, model, prompt version, input / cached / output / thinking tokens, audio seconds, latency, estimated cost, status, attempt, user) in its own short transaction, so failed and rolled-back attempts are counted. Roll-ups every 15 minutes into `ai_cost_rollups` (per 15-minute bucket, user, role and model; `BACKEND_DESIGN.md` §5.5, §7.6); the metrics below are computed from the roll-ups:

| Metric | Definition |
|---|---|
| Cost per active user per day | Sum of the user's `est_cost_usd` on days with app activity |
| Cost per relevant email, per meeting hour, per chat answer (by route) | By role |
| Cost per useful answer | Chat cost / answers without thumbs-down and not re-asked within 2 minutes |
| Cost per useful item | Extraction cost / items confirmed or not rejected within 7 days |
| Deterministic-answer share | Chat questions answered without a model call |
| Escalation rates | AI-06 → AI-07; AI-01 → AI-02 (if enabled) |
| Retry overhead | Calls with attempt > 1 / all calls |
| Measured vs modelled tokens | Per role; alert if > 30% above §3 |

Alerts: user over soft cap; global budget at 70%; any role's 7-day average unit cost > 130% of §3; retry overhead > 5%.

---

## 9. Cost acceptance rules for changes

A major change (`AI_PIPELINE.md` §12) reports on the frozen dataset: total run cost and cost per operation vs baseline; mean and p95 tokens per role; projected typical and heavy daily cost (§4 volumes).

| Change in projected typical-user cost | Requirement |
|---|---|
| ≤ +5% | Accept if quality, latency and reliability pass |
| +5% to +20% | Only with a statistically significant quality or reliability gain on an affected primary metric (`AI_EVALUATION.md` §8) and owner sign-off |
| > +20% | Written trade-off note and product sign-off |
| Any decrease | Accept if no slice regresses beyond tolerance and zero-tolerance metrics stay at zero |

---

## 10. Sensitivity (heavy user, per day, AI-02 disabled baseline ≈ $0.42)

| Change | Effect |
|---|---|
| T2 promotion (until 2026-12-31) | −$0.09 |
| Prefilter skips 30% of inbound instead of 50% | +$0.03 |
| Chat synthesis doubles (12/day) | +$0.14 |
| AI-02 enabled / with doubled escalation | +$0.06 / +$0.12 |
| Recorded meetings 5 h/week | +$0.16 |
| Email volume doubles | +$0.13 |
| AI-01 input 3,000 tokens instead of 2,000 (long bodies, more candidates) | +$0.03 |
| Worst plausible combination | ≈ $1.00/day: at the soft cap, degradation applies |

---

## 11. Top cost drivers and levers

| Rank | Driver (heavy share) | Levers already applied | Further levers (evaluate before use) | Quality guard |
|---|---|---|---|---|
| 1 | AI-07 chat synthesis (33%) | Deterministic list answers; lookups on T1; per-scenario budgets; SQL-first packets; abstention pre-check skips the call when nothing is found | X6 routing per intent; reduce default synthesis budget from 6 K to 4 K where E6 recall holds; T1 for single-source summaries | E7, E8, E10, CONTEXT G4 |
| 2 | AI-01 extraction (25%) | Prefilter; T1; one combined call; content-hash keys; no reprocessing on labels | X2 cheapest T1 model; trim candidate list to 5 when E2/E3 hold; shorter instructions via prompt compression tested on dev | E1–E4, DIR-120, zero hallucinated commitments |
| 3 | AI-09 transcription (21%) | Audio only; sha256 dedupe; no re-transcription | X5 Flash-Lite audio (~$0.06/h); silence trimming before upload (ffmpeg) | E11 WER/DER, speaker mapping |
| 4 | AI-04 embeddings (6%) | Prefiltered mail not embedded; quoted replies stripped | Do not embed outbound duplicates already quoted; delay embedding of low-priority mail to nightly batch | E6 recall |
| 5 | AI-10 meeting extraction (5%) | One call per meeting; transcript reused | X4 T1 for meeting extraction (~$0.013/h) | E11, CC-11–CC-17 |
| (if enabled) | AI-02 adjudication (+13%) | Disabled until X1; capped | — | E3 |
