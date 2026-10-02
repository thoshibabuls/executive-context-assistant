-- Local development roles (BACKEND_DESIGN.md §7.6). Migrations never create roles.
-- postgres  : schema owner, used only by migrations (app_migrator).
-- eca_app   : API runtime role; DML only, subject to row-level security.
-- eca_worker: worker runtime role; same attributes, separate credentials.
-- Idempotent. Docker runs it only on an empty volume; on an existing volume run:
--   docker compose exec db psql -U postgres -d eca -f /docker-entrypoint-initdb.d/01-roles.sql
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eca_app') THEN
    CREATE ROLE eca_app LOGIN PASSWORD 'eca_app_dev' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eca_worker') THEN
    CREATE ROLE eca_worker LOGIN PASSWORD 'eca_worker_dev' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
END
$$;
GRANT CONNECT ON DATABASE eca TO eca_app;
GRANT CONNECT ON DATABASE eca TO eca_worker;
