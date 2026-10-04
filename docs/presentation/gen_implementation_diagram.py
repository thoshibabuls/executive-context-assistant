"""Figure 2: what the system is made of today (as built, Phase 4.5).

Run: python docs/presentation/gen_implementation_diagram.py  ->  implementation_diagram.html
Then: node docs/presentation/render.cjs implementation_diagram  ->  .png (3840 x 2160) and .pdf

Box titles are plain language; the small line under each title names the exact technology or the
code package (backend/eca/<name>). Facts: Dockerfile, backend/pyproject.toml, backend/eca/*,
docker-compose.yml, eca/ingestion/tasks.py, config/models.yaml, TECHNICAL_DESIGN.md (Web Push).
"""

from __future__ import annotations

from pathlib import Path

from diagram_kit import Figure, esc

f = Figure()

LEFT_X, LEFT_W = 60, 220
CX, CW = 360, 1000
RIGHT_X, RIGHT_W = 1460, 400
PROC_Y, PROC_H = 108, 128
MOD_X = [510, 793, 1076]
MOD_W, MOD_H = 264, 136
ROW_Y = [254, 410, 566, 722, 878]
LAYERS = ["Bring data in", "Understand it", "Remember it", "Use it", "Keep it safe"]


def mid(y: float, h: float) -> float:
    return y + h / 2


# ---- the application ----------------------------------------------------------------------
f.box(CX, 40, CW, 1000, "", cls="group")
f.text(CX + 20, 54, 900, '<div class="t">The application</div>'
       '<div class="k">One Python codebase · one Docker image</div>', "")
f.box(380, PROC_Y, 470, PROC_H, "Web server", "FastAPI", "Serves the web app and streams answers")
f.box(870, PROC_Y, 470, PROC_H, "Background worker", "Procrastinate",
      "Syncs every 5 minutes, runs AI jobs, sends reminders")

MODULES = [
    [("Google connectors", "connectors", "Reads Gmail and Calendar"),
     ("Account access", "connections", "Google permissions, stored encrypted"),
     ("Sync", "ingestion", "Tracks what is new, retries failures")],
    [("Email processing", "communication", "Cleans, filters and summarizes"),
     ("Meetings", "meetings", "Audio, transcripts, speakers"),
     ("AI gateway", "intelligence", "Picks the model, enforces the budget, logs cost")],
    [("Tasks and decisions", "work", "Each linked to the email or meeting behind it"),
     ("People", "people", "People and organizations"),
     ("Projects", "projects", "Groups related work")],
    [("Search", "retrieval", "Finds the right context for a question"),
     ("Chat", "chat", "Answers questions and checks the answers"),
     ("Reminders", "attention", "Priorities, reminders, daily briefing")],
    [("Sign-in", "identity", "Google sign-in and sessions"),
     ("Privacy", "privacy", "Audit log, data export, deletion"),
     ("Platform", "platform", "Database access, jobs, file storage")],
]
for r, (layer, mods) in enumerate(zip(LAYERS, MODULES)):
    f.text(384, ROW_Y[r] + 12, 112, esc(layer), "plabel")
    for c, (title, package, detail) in enumerate(mods):
        ai = package == "intelligence"
        f.box(MOD_X[c], ROW_Y[r], MOD_W, MOD_H, title, package, esc(detail), "ai" if ai else "",
              ("AI",) if ai else ())

# ---- left: the user and Google ------------------------------------------------------------
f.box(LEFT_X, PROC_Y, LEFT_W, PROC_H, "Web app", "Next.js", "What the user sees", "io")
f.arrow((LEFT_X + LEFT_W, mid(PROC_Y, PROC_H)), (380, mid(PROC_Y, PROC_H)), both=True)
f.label(LEFT_X + LEFT_W + 6, PROC_Y + 12, 68, "requests", "center")

f.box(LEFT_X, ROW_Y[0], LEFT_W, MOD_H, "Gmail, Calendar", "Google APIs, read-only",
      "Where emails and events come from", "ext")
f.arrow((LEFT_X + LEFT_W, mid(ROW_Y[0], MOD_H)), (MOD_X[0], mid(ROW_Y[0], MOD_H)))
f.label(LEFT_X + LEFT_W + 6, ROW_Y[0] + 18, 68, "emails,<br>events", "center")

f.box(LEFT_X, ROW_Y[4], LEFT_W, MOD_H, "Google sign-in", "OpenID Connect", "Verifies the user", "ext")
f.arrow((LEFT_X + LEFT_W, mid(ROW_Y[4], MOD_H)), (MOD_X[0], mid(ROW_Y[4], MOD_H)))
f.label(LEFT_X + LEFT_W + 6, ROW_Y[4] + 24, 68, "identity", "center")

# ---- right: storage and outside services --------------------------------------------------
db_items = ["Tasks, people, meetings, emails", "Search index: meaning and keyword", "Queue of background jobs",
            "Each user sees only their own data"]
f.box(RIGHT_X, PROC_Y, RIGHT_W, ROW_Y[0] + MOD_H - PROC_Y, "Database", "PostgreSQL 17 + pgvector", cls="store",
      body='<ul class="d" style="margin:8px 0 0 20px;display:grid;gap:5px">'
           + "".join(f"<li>{esc(t)}</li>" for t in db_items) + "</ul>")
f.cylinder(RIGHT_X + RIGHT_W - 54, PROC_Y + 16)
f.arrow((1340, mid(PROC_Y, PROC_H)), (RIGHT_X, mid(PROC_Y, PROC_H)), both=True)
f.label(1346, PROC_Y + 12, 108, "reads, saves", "center")

f.box(RIGHT_X, ROW_Y[1], RIGHT_W, MOD_H, "Google Gemini", "AI models",
      "Reached only through the AI gateway", "ext")
f.arrow((MOD_X[2] + MOD_W, mid(ROW_Y[1], MOD_H)), (RIGHT_X, mid(ROW_Y[1], MOD_H)), both=True)
f.label(MOD_X[2] + MOD_W + 6, ROW_Y[1] + 18, 108, "AI requests", "center")

f.box(RIGHT_X, ROW_Y[3], RIGHT_W, MOD_H, "Browser notifications", "Web Push",
      "Reminder alerts, with no content inside", "ext")
f.arrow((MOD_X[2] + MOD_W, mid(ROW_Y[3], MOD_H)), (RIGHT_X, mid(ROW_Y[3], MOD_H)))
f.label(MOD_X[2] + MOD_W + 6, ROW_Y[3] + 24, 108, "alerts", "center")

f.box(RIGHT_X, ROW_Y[4], RIGHT_W, MOD_H, "File storage", "Local disk", "Meeting recordings")
f.arrow((MOD_X[2] + MOD_W, mid(ROW_Y[4], MOD_H)), (RIGHT_X, mid(ROW_Y[4], MOD_H)), both=True)
f.label(MOD_X[2] + MOD_W + 6, ROW_Y[4] + 24, 108, "recordings", "center")

out = Path(__file__).with_name("implementation_diagram.html")
out.write_text(f.html("What the Executive Context Assistant is made of"), encoding="utf-8")
print(out)
