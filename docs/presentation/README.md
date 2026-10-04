# Presentation figures

Two figures that explain the system to non-technical readers without losing technical accuracy.
Both are drawn from the code and the design documents and are regenerated from source.

| Figure | File | What it answers |
|---|---|---|
| 1 · How data flows | `pipeline_diagram.png` (3840 × 2160), `pipeline_diagram.pdf` (vector) | How does one user's data become a cited answer, and where exactly does a model run? |
| 2 · What it is made of | `implementation_diagram.png` (3840 × 2160), `implementation_diagram.pdf` (vector) | What runs today: which processes, which modules, which database and which outside services? |

Use the PNG on slides. Use the PDF in documents; it stays sharp at any size.

## Regenerate

```bash
cd docs/presentation
python gen_pipeline_diagram.py          # writes pipeline_diagram.html
python gen_implementation_diagram.py    # writes implementation_diagram.html
NODE_PATH=$(npm root -g) node render.cjs pipeline_diagram
NODE_PATH=$(npm root -g) node render.cjs implementation_diagram
```

`render.cjs` needs Playwright with Chromium. It writes the PNG and the PDF, and exits non-zero if the
IBM Plex fonts did not load, any text overflows its box, any label touches a box, a tag or another
label, any label sits within 6 px of an arrow, or an `AI-nn` tag covers a title. Change wording in the
generator, never in the PNG.

`diagram_kit.py` holds the shared look, so both figures read the same way:

| Mark | Meaning |
|---|---|
| Large plain-language title in each box | what the step or part does |
| Small monospace line under the title | the exact technology, model or code package (`backend/eca/<name>`) |
| Lavender box with an `AI` tag | this step calls an AI model |
| Dark-outlined rounded box | where data comes in or goes out (the user, a source, the answer) |
| Blue box with a cylinder | the database |
| Dashed outline | an outside service we use but do not run |
| Labelled arrow | the data that moves; an unlabelled arrow means "next step" |

The figures have no headline, subtitle, caption or legend on purpose: the slide carries the title,
and every box names itself. Text is at least 17 px on a 1920 px canvas so it reads on a projector.

## Where each fact comes from

| Claim on a figure | Source |
|---|---|
| Mail and calendar sync every 5 minutes, with incremental cursors | `backend/eca/ingestion/tasks.py` (`*/5 * * * *`), `backend/eca/connectors/gmail.py` |
| Prefilter drops bulk mail; outbound mail is never prefiltered | `AI_PIPELINE.md` §6.1, §9 |
| Roles, tiers and which ones have a fallback | `config/models.yaml` |
| The AI gateway enforces a per-user daily budget | `config/budgets.yaml` |
| Steps for answering a question | `AI_PIPELINE.md` §6.4, `CONTEXT_ARCHITECTURE.md` |
| One image, `api` and `worker` commands, ffmpeg | `Dockerfile` |
| PostgreSQL 17 with pgvector | `docker-compose.yml` (`pgvector/pgvector:pg17`) |
| The fifteen building blocks inside the application | `backend/eca/*/__init__.py`, `backend/pyproject.toml`, `.github/workflows/ci.yml` |
| Worker syncs every 5 minutes and sends reminders | `backend/eca/ingestion/tasks.py`, `backend/eca/attention/tasks.py` |
| Web Push carries no payload; endpoints limited to known push services | `TECHNICAL_DESIGN.md` (Web Push) |

Left out on purpose: the disabled AI-02 adjudication step, reply guidance (AI-08) and meeting prep
questions (AI-11), which are not on the paths drawn, and the offline evaluation judge (AI-14).
Webhooks are not drawn: they are designed but not built. Model names come from `config/models.yaml`.
