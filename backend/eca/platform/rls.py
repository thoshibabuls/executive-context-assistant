"""Row-level security foundation (BACKEND_DESIGN.md §17.1, TECHNICAL_DESIGN.md §9.2).

Migrations call these helpers so every user-owned table gets the same fail-closed policy:

* ``ENABLE`` + ``FORCE ROW LEVEL SECURITY`` (the table owner is also subject to the policy;
  only superusers and roles with BYPASSRLS skip it, and the runtime role has neither).
* ``USING`` and ``WITH CHECK`` compare ``user_id`` with ``eca_current_user_id()``, which reads
  the transaction-local ``app.user_id``. An unset value yields NULL, so no row matches.
"""

from __future__ import annotations

import re

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

CURRENT_USER_ID_FUNCTION = "eca_current_user_id"

CREATE_CURRENT_USER_ID_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION {CURRENT_USER_ID_FUNCTION}() RETURNS uuid
LANGUAGE sql STABLE PARALLEL SAFE
AS $$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$
"""

DROP_CURRENT_USER_ID_FUNCTION_SQL = f"DROP FUNCTION IF EXISTS {CURRENT_USER_ID_FUNCTION}()"


def validate_identifier(name: str) -> str:
    """Return ``name`` if it is a plain lower-case SQL identifier, else raise ``ValueError``."""
    if not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


_identifier = validate_identifier


def user_isolation_ddl(table: str, *, column: str = "user_id") -> list[str]:
    """Statements that put ``table`` under per-user row-level security."""
    t = _identifier(table)
    c = _identifier(column)
    policy = _identifier(f"{t}_user_isolation"[:63])
    return [
        f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY {policy} ON {t} "
        f"USING ({c} = {CURRENT_USER_ID_FUNCTION}()) "
        f"WITH CHECK ({c} = {CURRENT_USER_ID_FUNCTION}())",
    ]


def drop_user_isolation_ddl(table: str) -> list[str]:
    t = _identifier(table)
    policy = _identifier(f"{t}_user_isolation"[:63])
    return [
        f"DROP POLICY IF EXISTS {policy} ON {t}",
        f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY",
        f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY",
    ]


def runtime_role_grants_ddl(role: str, *, schema: str = "public") -> list[str]:
    """DML-only privileges for the runtime role on current and future tables (no DDL, no BYPASSRLS)."""
    r = _identifier(role)
    s = _identifier(schema)
    return [
        f"GRANT USAGE ON SCHEMA {s} TO {r}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {s} TO {r}",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {s} TO {r}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {s} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {r}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {s} GRANT USAGE, SELECT ON SEQUENCES TO {r}",
        f"GRANT EXECUTE ON FUNCTION {CURRENT_USER_ID_FUNCTION}() TO {r}",
    ]


def revoke_runtime_role_grants_ddl(role: str, *, schema: str = "public") -> list[str]:
    r = _identifier(role)
    s = _identifier(schema)
    return [
        # Identifiers below are validated by _identifier(); no user input reaches this SQL.
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {s} REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {r}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {s} REVOKE USAGE, SELECT ON SEQUENCES FROM {r}",
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {s} FROM {r}",
        f"REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {s} FROM {r}",
    ]
