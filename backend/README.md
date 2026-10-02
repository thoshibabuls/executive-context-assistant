# eca backend

Modular monolith (see `docs/BACKEND_DESIGN.md`). Slices 0.1–0.3: foundation and reliability core
(outbox, dispatcher, Procrastinate worker); no domain code yet.

## Setup

```bash
python -m venv .venv                       # at the repository root
.venv/Scripts/pip install -e "backend[dev]"   # Linux/macOS: .venv/bin/pip
.venv/Scripts/pre-commit install
docker compose up -d db                    # PostgreSQL 17 + pgvector, creates roles eca_app and eca_worker
```

Copy `.env.example` to `.env` (never commit `.env`). Migrations never create roles: on a database
volume created before slice 0.3, create `eca_worker` by re-running the init script:

```bash
docker compose exec db psql -U postgres -d eca -f /docker-entrypoint-initdb.d/01-roles.sql
```

## Database migrations (release step, owner role)

```bash
cd backend
alembic upgrade head        # API_MIGRATION_DATABASE_URL, API_DB_RUNTIME_ROLE, API_DB_WORKER_ROLE
```

## Run the API

```bash
cd backend
uvicorn eca.api.app:app --reload                                          # Linux/macOS
uvicorn eca.api.app:app --loop eca.platform.runtime:selector_loop_factory  # Windows (psycopg async needs a selector loop)
```

`GET /healthz` (process alive) and `GET /readyz` (database reachable and schema at the migration head).

## Run the worker

```bash
cd backend
python -m eca.worker        # API_WORKER_DATABASE_URL (worker role): dispatcher, queue workers, reconciler
eca ops outbox retry [--event-id <uuid>]   # failed outbox rows -> pending
```

## Checks

```bash
cd backend
ruff check . && ruff format --check .
mypy
lint-imports
ECA_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres pytest
```

Database tests (`-m db`, including RT-01, RT-05 and RT-15) need `ECA_TEST_DATABASE_URL` pointing at a
role that can create databases and roles; they are skipped locally without it and fail in CI without it.
RT-01 and RT-05 start worker subprocesses (`tests/reliability/support/worker_main.py`).
