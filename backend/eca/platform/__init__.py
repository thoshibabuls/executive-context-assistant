"""Shared base layer: configuration, errors, database, unit of work, RLS helpers, logging.

Owns (BACKEND_DESIGN.md §5.1): ``outbox``, ``event_consumptions``, ``idempotency_keys``
(added in later slices). Imports no domain module and no HTTP framework.
"""
