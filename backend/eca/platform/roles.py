"""Runtime-role safety checks used by migrations (BACKEND_DESIGN.md §7.6, role lifecycle).

Migrations never create or alter roles. Before a migration grants anything to the API or the
worker role, it calls :func:`check_runtime_roles`, which fails loudly when the roles are not
provisioned or are unsafe. The check only reads catalogs.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from eca.platform.rls import validate_identifier

_HINT = "Provision runtime roles outside migrations (BACKEND_DESIGN.md §7.6, role lifecycle)."

_ATTRIBUTES_SQL = text(
    "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = :role"
)

# Roles that ``role`` is a member of (directly or indirectly) and that would give it more power
# than a runtime role may have: a privileged role, the migration role, or the other runtime role.
_UNSAFE_MEMBERSHIP_SQL = text(
    """
    SELECT r.rolname
      FROM pg_roles r
     WHERE r.rolname <> :role
       AND pg_has_role(:role, r.oid, 'MEMBER')
       AND (r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb
            OR r.rolname = :other OR r.rolname = current_user)
     ORDER BY r.rolname
    """
)


class RuntimeRoleError(RuntimeError):
    """A runtime role is missing or unsafe; the migration must not continue."""


def check_runtime_roles(connection: Connection, *, api_role: str, worker_role: str) -> None:
    """Fail unless both runtime roles exist, are distinct, unprivileged and not nested."""
    api = validate_identifier(api_role)
    worker = validate_identifier(worker_role)
    if api == worker:
        raise RuntimeRoleError(f"API role and worker role must differ (both are {api!r}). {_HINT}")
    current = connection.execute(text("SELECT current_user")).scalar_one()
    for role, other in ((api, worker), (worker, api)):
        if role == current:
            raise RuntimeRoleError(f"Runtime role {role!r} must not be the migration role. {_HINT}")
        attrs = connection.execute(_ATTRIBUTES_SQL, {"role": role}).one_or_none()
        if attrs is None:
            raise RuntimeRoleError(f"Runtime role {role!r} does not exist. {_HINT}")
        unsafe = [
            name
            for name, flag in zip(("SUPERUSER", "BYPASSRLS", "CREATEROLE", "CREATEDB"), attrs, strict=True)
            if flag
        ]
        if unsafe:
            raise RuntimeRoleError(f"Runtime role {role!r} must not have {', '.join(unsafe)}. {_HINT}")
        members_of = connection.execute(_UNSAFE_MEMBERSHIP_SQL, {"role": role, "other": other}).scalars()
        nested = list(members_of)
        if nested:
            raise RuntimeRoleError(
                f"Runtime role {role!r} must not be a member of {', '.join(nested)}. {_HINT}"
            )
