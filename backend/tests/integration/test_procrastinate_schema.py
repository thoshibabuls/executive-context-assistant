"""The migrated Procrastinate schema equals the installed package's ``schema.sql`` (BACKEND_DESIGN.md §7.7).

A Procrastinate version bump without a matching Alembic revision fails here.
"""

from __future__ import annotations

from collections.abc import Iterator

import procrastinate
import psycopg
import pytest
from procrastinate.schema import SchemaManager

from tests.conftest import TempDatabase, new_database

pytestmark = pytest.mark.db

CATALOG_QUERIES = {
    "columns": """
        SELECT table_name, column_name, data_type, is_nullable, column_default
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name LIKE 'procrastinate%%'""",
    "indexes": """
        SELECT indexname, indexdef FROM pg_indexes
         WHERE schemaname = 'public' AND tablename LIKE 'procrastinate%%'""",
    "constraints": """
        SELECT conname, pg_get_constraintdef(c.oid) FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid WHERE t.relname LIKE 'procrastinate%%'""",
    "functions": """
        SELECT p.oid::regprocedure::text, md5(p.prosrc), pg_get_function_result(p.oid)
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'public' AND p.proname LIKE 'procrastinate%%'""",
    "triggers": """
        SELECT tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t
          JOIN pg_class c ON c.oid = t.tgrelid
         WHERE c.relname LIKE 'procrastinate%%' AND NOT t.tgisinternal""",
    "types": """
        SELECT t.typname, coalesce(string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder), '')
          FROM pg_type t LEFT JOIN pg_enum e ON e.enumtypid = t.oid
         WHERE t.typname LIKE 'procrastinate%%' AND t.typtype IN ('e', 'c') GROUP BY t.typname""",
    "composite_attributes": """
        SELECT t.typname, a.attname, format_type(a.atttypid, a.atttypmod)
          FROM pg_type t JOIN pg_attribute a ON a.attrelid = t.typrelid
         WHERE t.typname LIKE 'procrastinate%%' AND t.typtype = 'c'""",
}


def _catalog(url: str) -> dict[str, set[tuple[object, ...]]]:
    with psycopg.connect(url) as conn:
        return {name: {tuple(r) for r in conn.execute(q).fetchall()} for name, q in CATALOG_QUERIES.items()}  # type: ignore[arg-type]


@pytest.fixture
def package_schema_db(admin_base_url: str) -> Iterator[TempDatabase]:
    with new_database(admin_base_url, migrate=False) as db:
        with psycopg.connect(db.admin_url, autocommit=True) as conn:
            conn.execute(SchemaManager.get_schema())  # type: ignore[arg-type]
        yield db


def test_migrated_schema_equals_installed_package_schema(
    migrated_db: TempDatabase, package_schema_db: TempDatabase
) -> None:
    migrated = _catalog(migrated_db.admin_url)
    reference = _catalog(package_schema_db.admin_url)
    assert all(migrated[k] for k in ("columns", "functions", "triggers", "types"))
    for name in CATALOG_QUERIES:
        assert migrated[name] == reference[name], name


def test_installed_procrastinate_is_the_pinned_version() -> None:
    from importlib.metadata import version

    assert version("procrastinate") == "3.10.0"
    assert hasattr(procrastinate, "BaseRetryStrategy")
