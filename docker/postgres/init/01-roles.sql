-- Local development roles (TECHNICAL_DESIGN.md §17.3).
-- postgres  : schema owner, used only by migrations (app_migrator).
-- eca_app   : runtime role for api/worker; DML only, subject to row-level security.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eca_app') THEN
    CREATE ROLE eca_app LOGIN PASSWORD 'eca_app_dev' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
END
$$;
GRANT CONNECT ON DATABASE eca TO eca_app;
