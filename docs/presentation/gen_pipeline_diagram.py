"""Figure 1: how emails, meetings and a question flow through the system.

Run: python docs/presentation/gen_pipeline_diagram.py  ->  pipeline_diagram.html
Then: node docs/presentation/render.cjs pipeline_diagram  ->  .png (3840 x 2160) and .pdf

Box titles are plain language; the small line under each title names the exact technology.
Facts: AI_PIPELINE.md §6 and §9, CONTEXT_ARCHITECTURE.md, config/models.yaml.
"""

from __future__ import annotations

from pathlib import Path

from diagram_kit import BLUE, Figure, esc

f = Figure()

IN_X, IN_W = 60, 210
COL_W, COL_GAP = 196, 29
COLS = [300 + i * (COL_W + COL_GAP) for i in range(7)]
TOP = 74
A_Y, B_Y, ROW_H = TOP + 40, TOP + 262, 156
STORE_Y, STORE_H = TOP + 460, 104
D_Y, D_H = TOP + 642, 270


def row(y: int, steps: list[tuple[str, str, str, bool]]) -> None:
    for i, (title, tech, detail, ai) in enumerate(steps):
        f.box(COLS[i], y, COL_W, ROW_H, title, tech, esc(detail), "ai" if ai else "", ("AI",) if ai else ())
        if i:
            f.arrow((COLS[i - 1] + COL_W, y + ROW_H / 2), (COLS[i], y + ROW_H / 2))


# ---- emails and calendar ------------------------------------------------------------------
f.panel(280, TOP, 1580, 206, "A", "Emails and calendar")
f.box(IN_X, A_Y, IN_W, ROW_H, "Gmail, Calendar", "Read-only", "The user's inbox and calendar", "io")
f.arrow((IN_X + IN_W, A_Y + ROW_H / 2), (COLS[0], A_Y + ROW_H / 2))
row(A_Y, [
    ("Fetch", "Gmail + Calendar APIs", "New mail and events every 5 minutes", False),
    ("Clean up", "Python", "Strips quoted replies and signatures", False),
    ("Filter", "Python rules", "Skips bulk mail; sent mail is always kept", False),
    ("Find tasks", "Gemini 3.1 Flash-Lite", "Tasks, promises and deadlines", True),
    ("Check", "Python rules", "Each item checked against the email", False),
    ("Index", "gemini-embedding-2", "Searchable by meaning and keyword", True),
    ("Summarize", "Gemini 3.1 Flash-Lite", "Only threads with 8 or more messages", True),
])

# ---- meetings -----------------------------------------------------------------------------
f.panel(280, TOP + 222, 1580, 206, "B", "Meeting recordings")
f.box(IN_X, B_Y, IN_W, ROW_H, "Recording", "Uploaded", "Audio or video of a meeting", "io")
f.arrow((IN_X + IN_W, B_Y + ROW_H / 2), (COLS[0], B_Y + ROW_H / 2))
row(B_Y, [
    ("Upload", "Signed upload link", "The same file is never processed twice", False),
    ("Get audio", "ffmpeg", "Keeps only the sound track", False),
    ("Transcribe", "Gemini 3.5 transcribe", "Text, with speakers matched to attendees", True),
    ("Find tasks", "Gemini 3.8 Flash", "Actions and decisions, using past meetings", True),
    ("Check", "Python rules", "Same checks as for email", False),
    ("Index", "gemini-embedding-2", "Searchable transcript", True),
])

# ---- the database -------------------------------------------------------------------------
chips = ["Emails and events", "Tasks, promises, deadlines", "People", "Meetings", "Search index",
         "Background jobs"]
f.box(280, STORE_Y, 1580, STORE_H, "", cls="store",
      body=(f'<div style="position:absolute;left:72px;top:14px;width:440px">'
            f'<div class="t">C&#8194;Database</div>'
            f'<div class="k" style="color:{BLUE}">PostgreSQL 17 + pgvector</div>'
            f'<div class="d" style="margin-top:2px">Each user can see only their own data</div></div>'
            '<div style="position:absolute;left:520px;top:34px;display:flex;gap:10px">'
            + "".join(f'<span class="chip">{esc(c)}</span>' for c in chips) + "</div>"))
f.cylinder(300, STORE_Y + 30)
ax = COLS[6] + 34
f.arrow((ax, A_Y + ROW_H), (ax, STORE_Y))
f.label(ax + 12, B_Y + 40, 120, "saves")
bx = COLS[5] + COL_W / 2
f.arrow((bx, B_Y + ROW_H), (bx, STORE_Y))
f.label(bx + 12, B_Y + ROW_H + 8, 80, "saves")

# ---- answering a question -----------------------------------------------------------------
f.panel(280, TOP + 600, 1580, 334, "D", "Answering a question")
STAGES = {"q": (IN_X, IN_W), "u": (300, 240), "a": (590, 500), "g": (1140, 250), "v": (1440, 210),
          "o": (1700, 160)}


def steps(items: list[str]) -> str:
    cells = "".join(f"<div>{esc(t)}</div>" for t in items)
    return f'<div class="steps" style="margin-top:10px">{cells}</div>'


q_y = D_Y + (D_H - ROW_H) / 2
x, w = STAGES["q"]
f.box(x, q_y, w, ROW_H, "Question", "Web app", "Typed by the user", "io")
x, w = STAGES["u"]
f.box(x, D_Y, w, D_H, "Understand", "Rules, then Gemini", cls="ai", tags=("AI",), body=steps([
    "Works out who, what and when",
    "AI is used only when the rules cannot tell",
]))
x, w = STAGES["a"]
f.box(x, D_Y, w, D_H, "Gather context", "Python + PostgreSQL search", cls="ai", tags=("AI",), body=steps([
    "Only this user's data and connected accounts",
    "Finds the people, projects and dates mentioned",
    "Searches by meaning and by keyword",
    "Follows related items, up to two steps away",
    "The user's own edits win over AI guesses",
    "Keeps only the most relevant pieces",
]))
x, w = STAGES["g"]
f.box(x, D_Y, w, D_H, "Write answer", "Gemini Flash-Lite or Flash", cls="ai", tags=("AI",), body=steps([
    "Quick lookups use a smaller model, harder questions a stronger one",
    "Says so when nothing relevant is found",
]))
x, w = STAGES["v"]
f.box(x, D_Y, w, D_H, "Check answer", "Python rules", body=steps([
    "Each fact must match its source",
    "Unsupported facts are marked as guesses",
]))
x, w = STAGES["o"]
f.box(x, q_y, w, ROW_H, "Answer", "Web app", "Each fact links to its source", "io")

mid = D_Y + D_H / 2
order = ["q", "u", "a", "g", "v", "o"]
for left, right in zip(order, order[1:]):
    f.arrow((STAGES[left][0] + STAGES[left][1], mid), (STAGES[right][0], mid))

sx = STAGES["a"][0] + 250
f.arrow((sx, STORE_Y + STORE_H), (sx, D_Y))
f.label(sx + 12, STORE_Y + STORE_H + 8, 120, "looks up")

out = Path(__file__).with_name("pipeline_diagram.html")
out.write_text(f.html("How data flows through the Executive Context Assistant"), encoding="utf-8")
print(out)
