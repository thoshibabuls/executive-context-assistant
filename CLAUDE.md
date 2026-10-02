# Executive Context Assistant — Engineering Rules

## Product

This repository implements an AI Executive Context Assistant.

The product's primary differentiator is CONTEXT CONTINUITY.

The assistant connects:

- people
- conversations
- meetings
- tasks
- commitments
- deadlines
- projects
- decisions

The product must help the user understand:

- what happened
- what matters
- what they owe
- what others owe them
- what is coming next
- what they should do next

## Engineering principle

Do not confuse the PRD with the implementation.

The PRD defines product behavior.

Technical Design defines architecture.

Implementation Plan defines execution.

Code should follow those documents.

If implementation requires changing product behavior, stop and update the appropriate document first.

## Context principle

Context is a first-class system.

Never solve context problems by blindly increasing the model context window.

Retrieve relevant information.

Prefer the smallest sufficient context.

Track source, freshness and confidence.

## Source-of-truth principle

External source data is authoritative for what actually happened.

AI-derived information is an inference until confirmed.

User-confirmed information has higher authority than an AI inference.

Never silently convert uncertain inference into fact.

## AI principle

Do not use an LLM for deterministic operations when normal code is sufficient.

Use structured outputs for extraction.

Use cheaper models for simple high-volume operations.

Reserve stronger models for complex reasoning.

Track model, tokens, latency and estimated cost.

## Cost principle

Every AI call must have a reason.

Avoid:

- repeated processing
- unnecessary long prompts
- redundant retrieval
- unnecessary model escalation
- retry loops that consume tokens
- processing unchanged data

Prefer:

- incremental processing
- caching
- batching
- model routing
- deterministic preprocessing

## Reliability principle

External integrations can fail.

AI calls can fail.

Network calls can fail.

The application must be idempotent and recoverable.

A failed AI operation must not corrupt source data.

## Integration principle

Core product logic must not depend on Google-specific semantics.

Normalize external systems into internal concepts.

Future integrations include Microsoft 365, Teams and Slack.

## Security principle

Never expose data outside the user's authorization boundary.

Never commit secrets.

Never log OAuth credentials or sensitive source content unnecessarily.

## Development principle

Before major implementation:

1. inspect existing code
2. understand architecture
3. identify reusable components
4. read relevant skills
5. read technical documentation
6. update the implementation plan

Do not rewrite working systems without evidence.

## Testing principle

Every feature needs:

- unit tests
- integration tests where appropriate
- failure-path tests
- AI evaluation where AI is involved

AI features require golden examples.

Do not accept "looks good" as an evaluation strategy.

## Scope principle

Build the MVP first.

Do not introduce:

- unnecessary microservices
- unnecessary agents
- unnecessary graph databases
- unnecessary vector databases
- unnecessary orchestration frameworks

Complexity must be justified by a product requirement.

## Documentation principle

Keep these documents synchronized:

docs/PRD.md
docs/TECHNICAL_DESIGN.md
docs/CONTEXT_ARCHITECTURE.md
docs/BACKEND_DESIGN.md
docs/AI_PIPELINE.md
docs/AI_COST_MODEL.md
docs/AI_EVALUATION.md
docs/IMPLEMENTATION_PLAN.md

If architecture changes materially, update the document before continuing implementation.
