"""Projects, hint matching and topic mode.

Owns (single writer, BACKEND_DESIGN.md §5.1): projects, project_members. Depends on ``work``
(items, decisions, context events) and ``people`` only (§5.2). Other modules import only from this
package root.
"""

from eca.projects.purge import purge_user
from eca.projects.service import (
    CONFIRMED,
    MATCH_MIN_SIMILARITY,
    QUIET_AFTER,
    Member,
    ProjectView,
    activity_status,
    alias_catalog,
    assign_item,
    create_project,
    edit_project,
    get_project,
    list_projects,
    match_projects,
    members_of,
    normalize_hint,
    verify_project,
)
from eca.projects.suggest import (
    PROJECT_SUGGEST_TASK,
    SuggestReport,
    periodic_tasks,
    suggest_all,
    suggest_user,
)

__all__ = [
    "CONFIRMED",
    "MATCH_MIN_SIMILARITY",
    "PROJECT_SUGGEST_TASK",
    "QUIET_AFTER",
    "Member",
    "ProjectView",
    "SuggestReport",
    "activity_status",
    "alias_catalog",
    "assign_item",
    "create_project",
    "edit_project",
    "get_project",
    "list_projects",
    "match_projects",
    "members_of",
    "normalize_hint",
    "periodic_tasks",
    "purge_user",
    "suggest_all",
    "suggest_user",
    "verify_project",
]
