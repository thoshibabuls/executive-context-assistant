# eca backend

Modular monolith (see `docs/BACKEND_DESIGN.md`). Slices 0.1–0.2: foundation only.

## Setup

```bash
python -m venv .venv                       # at the repository root
.venv/Scripts/pip install -e "backend[dev]"   # Linux/macOS: .venv/bin/pip
.venv/Scripts/pre-commit install
docker compose up -d db                    # PostgreSQL 17 + pgvector, creates role eca_app
```

Copy `.env.example` to `.env` (never commit `.env`).

## Database migrations (release step, owner role)

```bash
cd backend
alembic upgrade head        # uses API_MIGRATION_DATABASE_URL and API_DB_RUNTIME_ROLE
```

## Run the API

```bash
cd backend
uvicorn eca.api.app:app --reload                                          # Linux/macOS
uvicorn eca.api.app:app --loop eca.platform.runtime:selector_loop_factory  # Windows (psycopg async needs a selector loop)
```

`GET /healthz` (process alive) and `GET /readyz` (database reachable and schema at the migration head).

## Checks

```bash
cd backend
ruff check . && ruff format --check .
mypy
lint-imports
ECA_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres pytest
```

Database tests (`-m db`, including RT-15) need `ECA_TEST_DATABASE_URL` pointing at a role that can
create databases and roles; they are skipped locally without it and fail in CI without it.
