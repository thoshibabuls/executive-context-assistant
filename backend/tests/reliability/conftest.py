from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.conftest import RUNTIME_ROLE, WORKER_ROLE, TempDatabase, new_database
from tests.reliability.support.synthetic import create_rt_tables


@pytest.fixture
def rt_db(admin_base_url: str) -> Iterator[TempDatabase]:
    """A migrated database per crash test, with the test-only ``rt_effects`` and ``rt_business``."""
    with new_database(admin_base_url, migrate=True) as db:
        create_rt_tables(db.admin_url, api_role=RUNTIME_ROLE, worker_role=WORKER_ROLE)
        yield db
