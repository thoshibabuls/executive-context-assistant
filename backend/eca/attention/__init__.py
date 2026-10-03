"""Priority, reminders, notifications, briefings, prep sections.

Owns (single writer, BACKEND_DESIGN.md §5.1): reminders, notifications, briefings. Priority
columns are written through the owning modules' services (work, communication), and the computed
relationship profile through ``people``. Slice 1.8: priority v1 and the Today read model; Phase 3:
relationship profiles (3.4). Deterministic only: this module never imports ``intelligence``
(import-linter contract). Other modules import only from this package root.
"""

from eca.attention import tasks as _tasks  # registers handlers
from eca.attention.priority import PriorityConfig, conversation_features, item_features, score
from eca.attention.profiles import ProfileInputs, compute_profile, refresh_profiles
from eca.attention.service import recompute_conversations, recompute_items, sweep_all, sweep_user
from eca.attention.tasks import PRIORITY_SWEEP_TASK, periodic_tasks, priority_config
from eca.attention.today import Today, build_today

del _tasks

__all__ = [
    "PRIORITY_SWEEP_TASK",
    "PriorityConfig",
    "ProfileInputs",
    "Today",
    "build_today",
    "compute_profile",
    "conversation_features",
    "item_features",
    "periodic_tasks",
    "priority_config",
    "recompute_conversations",
    "recompute_items",
    "refresh_profiles",
    "score",
    "sweep_all",
    "sweep_user",
]
