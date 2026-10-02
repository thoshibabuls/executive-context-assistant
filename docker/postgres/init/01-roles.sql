-- Local development roles (BACKEND_DESIGN.md §7.6). Migrations never create roles.
-- postgres  : schema owner, used only by migrations (app_migrator).
-- eca_app   : API runtime role; DML only, subject to row-level security.
-- eca_worker: worker runtime role, added to this script in slice 0.3.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eca_app') THEN
    CREATE ROLE eca_app LOGIN PASSWORD 'eca_app_dev' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
END
$$;
GRANT CONNECT ON DATABASE eca TO eca_app;
