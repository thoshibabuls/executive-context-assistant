"""Figure 1: the AI pipeline as a data-flow view (source data in, cited answer out).

Run: python docs/presentation/gen_pipeline_diagram.py  ->  pipeline_diagram.html
Then: node docs/presentation/render.mjs pipeline_diagram  ->  .png (3840 x 2160) and .pdf

Facts come from AI_PIPELINE.md §6 (pipelines), §9 (skip rules), CONTEXT_ARCHITECTURE.md (assembly),
config/models.yaml (roles and tiers) and config/budgets.yaml (budget guard).
"""

from __future__ import annotations

from pathlib import Path

from diagram_kit import AI, BLUE, INK, MUTED, Figure, esc

f = Figure()

# ---- header -------------------------------------------------------------------------------
f.text(60, 34, 1800, "Figure 1 · Data-flow view · scope: one user's data, from source to cited answer", "kicker")
f.text(60, 56, 1800, "Eight model roles, each at a marked step; everything else is deterministic code", "title")
f.text(60, 102, 1800,
       "Read left to right. A labelled arrow names the data that moves; a short unlabelled arrow means "
       "“next step”. Lavender steps call a model; white steps are plain code.", "sub")

# ---- geometry -----------------------------------------------------------------------------
IN_X, IN_W = 60, 170                      # input column
COL_W, COL_GAP = 186, 39                  # ingestion step columns
COLS = [300 + i * (COL_W + COL_GAP) for i in range(7)]
A_Y, B_Y, ROW_H = 182, 344, 108           # step rows of panels A and B
STORE_Y, STORE_H = 500, 74
D_Y, D_H = 632, 200                       # question stages


def row(y: int, steps: list[tuple[str, str, str, tuple[str, ...]]]) -> None:
    for i, (title, kind, detail, tags) in enumerate(steps):
        f.box(COLS[i], y, COL_W, ROW_H, title, kind, esc(detail), "ai" if tags else "", tags)
        if i:
            f.arrow((COLS[i - 1] + COL_W, y + ROW_H / 2), (COLS[i], y + ROW_H / 2))


# ---- A: email and calendar ----------------------------------------------------------------
f.panel(280, 150, 1580, 150, "A", "Email and calendar ingestion · worker process")
f.box(IN_X, A_Y, IN_W, ROW_H, "Mail + calendar", "Source", "Gmail and Google Calendar, read-only", "io")
f.arrow((IN_X + IN_W, A_Y + 54), (COLS[0], A_Y + 54))
f.label(IN_X + IN_W + 6, A_Y + 22, 62, "changes")
row(A_Y, [
    ("Sync", "Worker · every 5 min", "incremental cursors; mail and calendar", ()),
    ("Normalize", "Code", "MIME parsed; quotes and signatures stripped", ()),
    ("Prefilter", "Code · rules", "bulk mail dropped; sent mail always kept", ()),
    ("Extract", "LLM · fast tier", "tasks, commitments, deadlines as JSON", ("AI-01",)),
    ("Apply", "Code", "grounding, dates, confidence, merge", ()),
    ("Index", "Embedding model", "768-d vectors plus full-text", ("AI-04",)),
    ("Summarize", "LLM · fast tier", "only threads with 8+ relevant messages", ("AI-03",)),
])

# ---- B: meetings --------------------------------------------------------------------------
f.panel(280, 312, 1580, 150, "B", "Meeting ingestion · API, then worker")
f.box(IN_X, B_Y, IN_W, ROW_H, "Recording", "Source · upload", "audio or video of a meeting", "io")
f.arrow((IN_X + IN_W, B_Y + 54), (COLS[0], B_Y + 54))
f.label(IN_X + IN_W + 6, B_Y + 22, 62, "file")
row(B_Y, [
    ("Upload", "API · signed URL", "sha256 check: each file is processed once", ()),
    ("Extract audio", "Worker · ffmpeg", "audio track only; length capped", ()),
    ("Transcribe", "Speech model", "then speakers matched to attendees in code", ("AI-09",)),
    ("Extract", "LLM · strong tier", "actions, decisions; sees prior meetings", ("AI-10",)),
    ("Apply", "Code", "same rules as email", ()),
    ("Index", "Embedding model", "transcript chunks plus full-text", ("AI-04",)),
])

# ---- C: the store -------------------------------------------------------------------------
f.box(280, STORE_Y, 1580, STORE_H, "", cls="store",
      body=(f'<div style="position:absolute;left:68px;top:12px;width:470px">'
            f'<div class="t">C&#8194;Context store</div>'
            f'<div class="k" style="color:{BLUE}">PostgreSQL 17 + pgvector · per-user RLS</div></div>'
            '<div style="position:absolute;left:560px;top:20px;display:flex;gap:8px">'
            + "".join(f'<span class="chip">{esc(c)}</span>' for c in (
                "source items", "work items + evidence", "people", "meetings", "vectors (HNSW)",
                "full-text", "change feed", "job queue + outbox"))
            + "</div>"))
f.cylinder(298, STORE_Y + 16)
# A writes from the end of its chain, down through the empty seventh slot of row B.
ax = COLS[6] + 30
f.arrow((ax, A_Y + ROW_H), (ax, STORE_Y))
f.label(ax + 12, 330, 150, "writes items,<br>summaries,<br>vectors")
bx = COLS[5] + COL_W / 2
f.arrow((bx, B_Y + ROW_H), (bx, STORE_Y))
f.label(bx - 192, B_Y + ROW_H + 13, 180, "writes items, vectors", "right")

# ---- D: answering a question --------------------------------------------------------------
f.panel(280, 592, 1580, 250, "D", "Answering a question · API process")
STAGES = {  # name: (x, w)
    "q": (IN_X, IN_W), "u": (300, 200), "a": (580, 520), "g": (1180, 220), "v": (1480, 170), "o": (1730, 130),
}


def steps(items: list[tuple[str, str]], cols: int = 1) -> str:
    cells = "".join(f'<div><span class="n">{n}</span>{t}</div>' for n, t in items)
    return (f'<div class="steps" style="grid-template-columns:repeat({cols},1fr);'
            f'margin-top:8px;column-gap:16px">{cells}</div>')


x, w = STAGES["q"]
f.box(x, D_Y + 46, w, ROW_H, "Question", "Person · web app", "typed into Ask", "io")
x, w = STAGES["u"]
f.box(x, D_Y, w, D_H, "Understand", "Code first", cls="ai", tags=("AI-05",), body=steps([
    ("1", "Parse intent, people, dates"),
    ("2", "Plan with AI-05 only when the rules cannot classify"),
]))
x, w = STAGES["a"]
f.box(x, D_Y, w, D_H, "Assemble context", "Code + one query embedding", cls="ai", tags=("AI-04",), body=steps([
    ("3", "Scope: RLS, active sources"), ("7", "Fold facts by authority"),
    ("4", "Resolve people, projects, time"), ("8", "Rank to a token budget"),
    ("5", "Hybrid search, fused by RRF"), ("9", "Render packet with IDs"),
    ("6", "Expand at most 2 hops"), ("", ""),
], cols=2))
x, w = STAGES["g"]
f.box(x, D_Y, w, D_H, "Generate", "LLM · fast or strong", cls="ai", tags=("AI-06", "AI-07"), body=steps([
    ("10", "Abstain early if nothing is relevant"),
    ("11", "Lookup (AI-06) or synthesis (AI-07); lists are rendered in code"),
]))
x, w = STAGES["v"]
f.box(x, D_Y, w, D_H, "Verify", "Code", body=steps([
    ("12", "Check each claim’s dates, numbers and names against its cited item; "
           "unsupported claims are flagged “inferred”"),
]))
x, w = STAGES["o"]
f.box(x, D_Y + 46, w, ROW_H, "Answer", "Output", "every claim cited", "io")

mid = D_Y + D_H / 2
order = ["q", "u", "a", "g", "v", "o"]
names = {"q": "text", "u": "intent,<br>anchors", "a": "packet,<br>cite IDs", "g": "claims<br>+ IDs",
         "v": "checked<br>claims"}
for left, right in zip(order, order[1:]):
    x1 = STAGES[left][0] + STAGES[left][1]
    x2 = STAGES[right][0]
    f.arrow((x1, mid), (x2, mid))
    lines = names[left].count("<br>") + 1
    f.label(x1 + 6, mid - 10 - 18 * lines, x2 - x1 - 12, names[left], "center")

sx = STAGES["a"][0] + 300
f.arrow((sx, STORE_Y + STORE_H), (sx, D_Y))
f.label(sx + 12, STORE_Y + STORE_H + 13, 420, "reads entities, vectors, text, relations")

# ---- E: every model call is governed ------------------------------------------------------
gy = 858
f.raw(f'<div class="abs panel" style="left:60px;top:{gy}px;width:1800px;height:44px;'
      f'background:#F6F1FC;border:1.5px solid #D9C9EE"></div>')
ARROW = ('<svg width="30" height="10" style="flex:none"><path d="M1 5 H23" stroke="#5A6676" stroke-width="2"/>'
         '<path d="M21 1 L29 5 L21 9 Z" fill="#5A6676"/></svg>')
chain = ["role registry · config/models.yaml", "budget guard · $1 soft, $2.50 hard per user-day",
         "provider adapter · fallback model", "ai_calls meter · model, tokens, latency, cost"]
f.raw(f'<div class="abs" style="left:76px;top:{gy + 8}px;width:1770px;display:flex;align-items:center;gap:12px">'
      f'<span class="plabel" style="color:{AI};margin-right:18px"><b>E</b>Every model call passes</span>'
      + ARROW.join(f'<span class="chip" style="border-color:#D9C9EE">{esc(c)}</span>' for c in chain)
      + "</div>")

# ---- legend, model tiers, caption ---------------------------------------------------------
sw = "width:30px;height:18px;border-radius:5px"
f.raw('<div class="abs legend" style="left:60px;top:918px">'
      f'<span><i style="{sw};border:1.5px solid #B9C3CF;background:#fff"></i>code step</span>'
      f'<span><i style="{sw};border:1.5px solid #8A63C0;background:#F6F1FC"></i>model call</span>'
      f'<span><i style="font:600 11px/18px IBM Plex Mono,monospace;color:#fff;background:{AI};'
      f'border-radius:5px;padding:0 5px">AI-nn</i>role in config/models.yaml</span>'
      f'<span><i style="{sw};border:2px solid {INK};border-radius:10px"></i>input or output</span>'
      f'<span><i style="{sw};border:1.5px solid {BLUE};background:#EAF2FB"></i>data store</span>'
      '<span><i><svg width="34" height="10"><path d="M1 5 H28" stroke="#5A6676" stroke-width="2"/>'
      '<path d="M26 1 L33 5 L26 9 Z" fill="#5A6676"/></svg></i>data flow (labelled) or next step</span>'
      '</div>')
f.text(60, 948, 1800,
       f'<b style="color:{INK}">Model tiers, today → Azure:</b> fast, Gemini 3.1 Flash-Lite → OpenAI mini'
       " · strong, Gemini 3.8 Flash → OpenAI full · embedding, gemini-embedding-2 → "
       "text-embedding-3-small (768-d) · speech, Gemini → Azure AI Speech. Every step drawn is built "
       "and tested.", "cap")
f.text(60, 984, 1800,
       f"<b>Figure 1.</b> How one user’s data becomes a cited answer. <b>(A)</b> The worker pulls mail and "
       "calendar changes every 5 minutes; rules drop bulk mail before any model sees it, and code, not the "
       "model, settles dates, confidence and merges. Calendar changes update meetings in code only. "
       "<b>(B)</b> Uploaded meetings are transcribed and extracted under the same rules. <b>(C)</b> Everything "
       "lands in one PostgreSQL store behind per-user row-level security (RLS). <b>(D)</b> A question is "
       "answered from a packet assembled mostly in code: vector (HNSW) and full-text results are fused by "
       "reciprocal rank fusion (RRF), and every claim is checked before it is shown. <b>(E)</b> No step "
       "calls a model directly.", "cap")

out = Path(__file__).with_name("pipeline_diagram.html")
out.write_text(f.html("Figure 1 · AI pipeline, data-flow view"), encoding="utf-8")
print(out)
