# Prompts

Versioned prompts, one directory per role: `prompts/<role>/v<N>.md` (AI_PIPELINE.md §11).
A prompt file never changes after release; a change is a new version and a major change
(AI_PIPELINE.md §12) that passes the frozen-dataset scorecard.

Slice 0.4 adds the layout only. The first production prompt (AI-01 `email_extract`) arrives in
slice 1.4. Few-shot examples may come only from the `dev` split; gate A0 scans this directory for
overlap with the `test`, `sealed` and `challenge` splits.
