"""Audit log, export and deletion jobs.

Owns (single writer, BACKEND_DESIGN.md §5.1): audit_log, export_jobs, deletion_jobs.
Slice 1.9: account deletion, source purge on disconnect, nightly retention, "Your data".
Composition only (``eca.api``, ``eca.worker``) imports this package; it imports the modules
whose data it deletes, through their package roots.
"""

from eca.privacy import tasks as _tasks  # registers handlers
from eca.privacy.events import SOURCE_PURGE_REQUESTED, SourcePurgeRequested
from eca.privacy.service import (
    DataSummary,
    RetentionReport,
    data_summary,
    get_deletion_job,
    request_source_purge,
    run_account_deletion,
    run_retention,
    run_source_purge,
)
from eca.privacy.tasks import RETENTION_TASK, periodic_tasks

del _tasks

__all__ = [
    "RETENTION_TASK",
    "SOURCE_PURGE_REQUESTED",
    "DataSummary",
    "RetentionReport",
    "SourcePurgeRequested",
    "data_summary",
    "get_deletion_job",
    "periodic_tasks",
    "request_source_purge",
    "run_account_deletion",
    "run_retention",
    "run_source_purge",
]
