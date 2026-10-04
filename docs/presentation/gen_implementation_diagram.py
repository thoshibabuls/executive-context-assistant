"""Figure 2: the system as built today (container and code view).

Run: python docs/presentation/gen_implementation_diagram.py  ->  implementation_diagram.html
Then: node docs/presentation/render.cjs implementation_diagram  ->  .png (3840 x 2160) and .pdf

Facts come from the code: Dockerfile (one image, api and worker commands), backend/pyproject.toml
(FastAPI, Procrastinate, google-genai, import-linter contracts), backend/eca/* (one package per
module, each the single writer of its tables), docker-compose.yml (pgvector/pgvector:pg17),
eca/ingestion/tasks.py and eca/platform/jobs.py (schedules), config/models.yaml (roles).
"""

from __future__ import annotations

from pathlib import Path

from diagram_kit import AI, BLUE, INK, Figure, esc

f = Figure()

# ---- header -------------------------------------------------------------------------------
f.text(60, 34, 1800, "Figure 2 · Container and code view · as built, October 2026 (Phase 4.5)", "kicker")
f.text(60, 56, 1800, "One codebase, two processes, one database: the system as it runs today", "title")
f.text(60, 102, 1800,
       "What runs on Google sources and Gemini today. Arrows show data flow and name the protocol or "
       "payload; a double-headed arrow means both directions.", "sub")

# ---- geometry -----------------------------------------------------------------------------
LEFT_X, LEFT_W = 60, 230
CX, CW = 360, 1000                        # the Docker image / package container
RIGHT_X, RIGHT_W = 1460, 400
PROC_Y, PROC_H = 220, 84
MOD_X = [500, 787, 1074]
MOD_W, MOD_H = 266, 88
ROW_Y = [345, 444, 543, 642, 741]
LAYERS = ["Sources", "Processing", "Context model", "Answers", "Foundation"]


def mid(y: int, h: int) -> float:
    return y + h / 2


# ---- the container ------------------------------------------------------------------------
f.box(CX, 156, CW, 694, "", cls="group")
f.text(CX + 20, 168, 900, '<div class="t">eca · one Python 3.11 package, one Docker image</div>'
       '<div class="k" style="margin-top:3px">Modular monolith · each table has exactly one owning module</div>',
       "")
f.box(380, PROC_Y, 460, PROC_H, "API process", "FastAPI + uvicorn · cmd: api",
      "routes, sessions, rate limits; streams Ask answers")
f.box(880, PROC_Y, 460, PROC_H, "Worker process", "Procrastinate · python -m eca.worker",
      "sync and reminders every 5 min; job recovery each minute")
f.text(380, 318, 960, "Domain modules · both processes load them · import-linter enforces the boundaries",
       "plabel")

MODULES = [
    [("connectors", "Gmail and Calendar adapters behind one protocol", ""),
     ("connections", "OAuth grants; envelope-encrypted tokens", ""),
     ("ingestion", "sync stages, cursors, retries", "")],
    [("communication", "cleaning, prefilter, thread summaries", ""),
     ("meetings", "media, transcripts, speaker matching", ""),
     ("intelligence", "role registry, Gemini adapter, budget, meter", "ai")],
    [("work", "work items, evidence, apply, fold", ""),
     ("people", "persons, organizations, mentions", ""),
     ("projects", "projects and topic matching", "")],
    [("retrieval", "planner, hybrid search, context packet", ""),
     ("chat", "answer turns and grounding checks", ""),
     ("attention", "priority, reminders, briefing, Web Push", "")],
    [("identity", "Google sign-in (OIDC), sessions", ""),
     ("privacy", "audit log, export, deletion", ""),
     ("platform", "database, RLS, outbox, jobs, storage", "")],
]
for r, (layer, mods) in enumerate(zip(LAYERS, MODULES)):
    f.text(384, ROW_Y[r] + 8, 108, esc(layer), "plabel", "line-height:1.35")
    for c, (name, detail, cls) in enumerate(mods):
        f.box(MOD_X[c], ROW_Y[r], MOD_W, MOD_H, name, "", esc(detail), f"mod {cls}",
              ("AI-nn",) if cls == "ai" else ())

# ---- left: people and sources -------------------------------------------------------------
f.box(LEFT_X, PROC_Y - 6, LEFT_W, 96, "Web app", "Person · Next.js", "Today, Ask, Tasks, People", "io")
f.arrow((LEFT_X + LEFT_W, mid(PROC_Y, PROC_H)), (380, mid(PROC_Y, PROC_H)), both=True)
f.label(LEFT_X + LEFT_W + 6, PROC_Y - 4, 58, "HTTPS,<br>SSE", "center")

f.box(LEFT_X, ROW_Y[0], LEFT_W, MOD_H, "Gmail + Calendar", "External · read-only", "Google APIs", "ext")
f.arrow((LEFT_X + LEFT_W, mid(ROW_Y[0], MOD_H)), (MOD_X[0], mid(ROW_Y[0], MOD_H)))
f.label(LEFT_X + LEFT_W + 6, ROW_Y[0] + 4, 58, "mail,<br>events", "center")

f.box(LEFT_X, ROW_Y[4], LEFT_W, MOD_H, "Google sign-in", "External · OIDC", "identity and consent", "ext")
f.arrow((LEFT_X + LEFT_W, mid(ROW_Y[4], MOD_H)), (MOD_X[0], mid(ROW_Y[4], MOD_H)))
f.label(LEFT_X + LEFT_W + 6, ROW_Y[4] + 4, 58, "ID<br>token", "center")

f.text(LEFT_X, ROW_Y[1] + 10, LEFT_W,
       f'<div class="k" style="color:{BLUE};margin-bottom:6px">Where it runs</div>'
       "<div class=\"d\">Locally today: the API and the worker run on the host, PostgreSQL runs in "
       "Docker. Hosted deployment is still on the Phase 4.5 list.</div>", "")

# ---- right: state and outside services ----------------------------------------------------
store_body = ('<ul class="d" style="margin:6px 0 0 18px;display:grid;gap:3px">'
              + "".join(f"<li>{esc(t)}</li>" for t in (
                  "work items, people, meetings, evidence",
                  "vectors (HNSW, 768-d) and full-text search",
                  "Procrastinate job queue and outbox",
                  "row-level security on every query",
                  "ai_calls cost meter and audit log"))
              + "</ul>")
f.box(RIGHT_X, PROC_Y, RIGHT_W, 211, "PostgreSQL 17 + pgvector", "Data store · Docker today", cls="store",
      body=store_body)
f.cylinder(RIGHT_X + RIGHT_W - 50, PROC_Y + 14)
f.arrow((CX + CW, 300), (RIGHT_X, 300), both=True)
f.label(CX + CW + 6, 248, 88, "SQL with<br>per-user RLS", "center")

f.box(RIGHT_X, ROW_Y[1], RIGHT_W, MOD_H, "Gemini API", "External · google-genai SDK",
      "11 roles, routed by config/models.yaml", "ext")
f.arrow((MOD_X[2] + MOD_W, mid(ROW_Y[1], MOD_H)), (RIGHT_X, mid(ROW_Y[1], MOD_H)), both=True)
f.label(MOD_X[2] + MOD_W + 6, ROW_Y[1] + 2, 108, "prompts,<br>JSON", "center")

f.box(RIGHT_X, ROW_Y[3], RIGHT_W, MOD_H, "Push services", "External · Web Push, VAPID",
      "FCM, Mozilla, Apple, Windows", "ext")
f.arrow((MOD_X[2] + MOD_W, mid(ROW_Y[3], MOD_H)), (RIGHT_X, mid(ROW_Y[3], MOD_H)))
f.label(MOD_X[2] + MOD_W + 6, ROW_Y[3] + 2, 108, "empty<br>push", "center")

f.box(RIGHT_X, ROW_Y[4], RIGHT_W, MOD_H, "Object storage", "Local disk · signed URLs",
      "meeting recordings", "")
f.arrow((MOD_X[2] + MOD_W, mid(ROW_Y[4], MOD_H)), (RIGHT_X, mid(ROW_Y[4], MOD_H)), both=True)
f.label(MOD_X[2] + MOD_W + 6, ROW_Y[4] + 2, 108, "recordings", "center")

# ---- legend and caption -------------------------------------------------------------------
sw = "width:30px;height:18px;border-radius:5px"
f.raw('<div class="abs legend" style="left:60px;top:884px">'
      f'<span><i style="{sw};border:1.5px solid #B9C3CF;background:#fff"></i>process or module (code)</span>'
      f'<span><i style="{sw};border:1.5px solid #8A63C0;background:#F6F1FC"></i>calls a model</span>'
      f'<span><i style="{sw};border:2px solid {INK};border-radius:10px"></i>person</span>'
      f'<span><i style="{sw};border:1.5px solid {BLUE};background:#EAF2FB"></i>data store</span>'
      f'<span><i style="{sw};border:1.5px dashed #8A94A3"></i>outside service</span>'
      '<span><i><svg width="40" height="10"><path d="M8 5 H32" stroke="#5A6676" stroke-width="2"/>'
      '<path d="M30 1 L37 5 L30 9 Z" fill="#5A6676"/><path d="M10 1 L3 5 L10 9 Z" fill="#5A6676"/></svg></i>'
      'data flow, one or both ways</span></div>')
f.text(60, 924, 1800,
       "<b>Figure 2.</b> The system as it runs today. One Python package, <b>eca</b>, ships as one Docker "
       "image and runs as two processes. The <b>API</b> serves the Next.js web app and streams answers; the "
       "<b>worker</b> runs scheduled and queued jobs from a queue that lives in PostgreSQL, so there is no "
       "separate message broker. Fifteen domain modules sit behind import boundaries that CI enforces, and "
       "every table has exactly one module that writes it. <b>PostgreSQL 17 with pgvector</b> is the only "
       "stateful service: the context model, vectors, full-text search, the job queue and the outbox share "
       "one database under per-user row-level security (RLS). Gemini is reached only through the "
       f'<span style="color:{AI}"><b>intelligence</b></span> module, so moving to Azure OpenAI is an adapter '
       "change. Reminder pushes carry no content: the browser fetches it after sign-in.", "cap")

out = Path(__file__).with_name("implementation_diagram.html")
out.write_text(f.html("Figure 2 · As-built container and code view"), encoding="utf-8")
print(out)
