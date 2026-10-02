# Prompts (AI_PIPELINE.md §11)

Versioned `eca/intelligence/prompts/<role>/v<N>.md`, rendered by `eca.intelligence.extraction`
with `str.format` placeholders (`{user_name}`). The prompt version string is `<role>/v<N>` and is
part of every cassette key and `extractions` row.

| Role | Version | Status |
|---|---|---|
| `email_extract` (AI-01) | v1 | Active |
| `adjudicate` (AI-02) | v1 | Disabled (`config/models.yaml`, experiment X1) |
| `plan_query` (AI-05) | v1 | Active (Phase 2): routes questions the rules cannot route; never answers |
| `answer_lookup` (AI-06) | v1 | Active (Phase 2): T1 answers over a packet, schema `answer.v1` |
| `answer_synthesis` (AI-07) | v1 | Active (Phase 2): T2 synthesis over a packet, schema `answer.v1` |

AI-04 (`embed`) has no prompt file: its input format is the version string `embed/v1`
(`eca/intelligence/embedding.py`, CONTEXT_ARCHITECTURE.md §9.10). The Phase 2 prompts are plain
text (no placeholders); the packet and the question arrive rendered, untrusted text delimited.
