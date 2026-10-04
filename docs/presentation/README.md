# Presentation figures

Two figures for slides and written reviews. Both are drawn from the code and the design documents, not
from memory, and both are regenerated from source.

| Figure | File | What it answers |
|---|---|---|
| 1 · Data-flow view | `pipeline_diagram.png` (3840 × 2160), `pipeline_diagram.pdf` (vector) | How does one user's data become a cited answer, and where exactly does a model run? |
| 2 · Container and code view | `implementation_diagram.png` (3840 × 2160), `implementation_diagram.pdf` (vector) | What runs today: which processes, which modules, which database and which outside services? |

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

`diagram_kit.py` holds the shared vocabulary, so both figures read the same way:

| Mark | Meaning |
|---|---|
| White box, grey outline | a step or module that is plain code |
| Lavender box with a purple `AI-nn` tag | a step that calls a model; the tag is the role in `config/models.yaml` |
| Dark-outlined pill | an input or output of the whole figure (a person, a source, the answer) |
| Blue box with a cylinder | a data store |
| Dashed outline | a service we call but do not run |
| Labelled arrow | the data that moves; a short unlabelled arrow means "next step" |

## Design rules applied

| Rule | Where it comes from | How the figures apply it |
|---|---|---|
| The title states the takeaway, not the topic | PLOS, *Ten simple rules for better figures* (rule 1, know your message) | "Eight model roles, each at a marked step; everything else is deterministic code" |
| Name the view and its scope | C4 model, notation checklist | Kicker line: "Data-flow view · scope: one user's data, from source to cited answer" |
| Every box states its type and technology | C4 model, notation checklist | Each box has a small-caps kind line, e.g. "LLM · fast tier", "Procrastinate · python -m eca.worker" |
| Label every arrow with what moves | C4; Taverna, *Software architecture diagram arrows* | "changes", "packet, cite IDs", "SQL with per-user RLS"; only in-stage order arrows are unlabelled, and the legend says so |
| Inputs on one side, outputs on the other; one direction of flow | Formation, *Clear, effective technical diagrams* | Sources and the question on the left, the cited answer on the right, flow left to right |
| One shape per kind of element; colour only where it means something | Formation; InfoQ, *Crafting architectural diagrams* | Five shapes; purple is used only for model calls, blue only for stores and numbering |
| Group steps into stages instead of a flat list | Ilograph, *More common diagram mistakes* | Figure 1 panels A–E and the four question stages; Figure 2 layers |
| Explain every acronym and include a legend | InfoQ; C4 | Legend row; caption spells out RLS, HNSW and RRF |
| A figure needs a caption that can be read on its own | PLOS, rule 10 | "Figure 1." and "Figure 2." captions describe each panel |
| Remove decoration | PLOS, rule 8 | No shadows, gradients or icons except the database cylinder |

Sources: [C4 model notation](https://c4model.com/diagrams/notation) ·
[InfoQ: Crafting architectural diagrams](https://www.infoq.com/articles/crafting-architectural-diagrams) ·
[Software architecture diagram arrows](https://dev.to/ctaverna/software-architecture-diagram-arrows-30lb) ·
[Formation: clear technical diagrams](https://formation.dev/blog/how-to-create-clear-effective-technical-diagrams) ·
[Ilograph: common diagram mistakes](https://www.ilograph.com/blog/posts/more-common-diagram-mistakes/) ·
[Ten simple rules for better figures (PLOS)](https://journals.plos.org/ploscompbiol/article?id=10.1371/journal.pcbi.1003833)

## Where each fact comes from

| Claim on a figure | Source |
|---|---|
| Mail and calendar sync every 5 minutes, with incremental cursors | `backend/eca/ingestion/tasks.py` (`*/5 * * * *`), `backend/eca/connectors/gmail.py` |
| Prefilter drops bulk mail; outbound mail is never prefiltered | `AI_PIPELINE.md` §6.1, §9 |
| Roles, tiers and which ones have a fallback | `config/models.yaml` |
| Budget guard: $1 soft, $2.50 hard per user per day | `config/budgets.yaml` |
| Question steps 1–12 | `AI_PIPELINE.md` §6.4, `CONTEXT_ARCHITECTURE.md` |
| One image, `api` and `worker` commands, ffmpeg | `Dockerfile` |
| PostgreSQL 17 with pgvector | `docker-compose.yml` (`pgvector/pgvector:pg17`) |
| Fifteen modules, one writer per table, import boundaries enforced in CI | `backend/eca/*/__init__.py`, `backend/pyproject.toml`, `.github/workflows/ci.yml` |
| Job recovery every minute, reminders every 5 minutes | `backend/eca/platform/jobs.py`, `backend/eca/attention/tasks.py` |
| Web Push carries no payload; endpoints limited to known push services | `TECHNICAL_DESIGN.md` (Web Push) |
| Runs locally; hosted deployment not built | `docker-compose.yml`, `IMPLEMENTATION_PLAN.md` Phase 4.5 |

What the figures leave out on purpose: AI-02 adjudication (built, disabled), AI-08 reply guidance and
AI-11 meeting asks (they are not on the question path drawn in Figure 1), and AI-14 (offline evaluation
only). Webhooks are not drawn: they are designed as sync signals and not built.
