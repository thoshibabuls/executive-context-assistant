"""Audit log, export and deletion jobs.

Owns (single writer, BACKEND_DESIGN.md §5.1): audit_log, export_jobs, deletion_jobs.
Implemented in slice 1.9; other modules import only from this package root.
"""
