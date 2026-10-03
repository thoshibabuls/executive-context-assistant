"""Priority, reminders, notifications, briefings, prep sections.

Owns (single writer, BACKEND_DESIGN.md §5.1): reminders, notifications, briefings. Priority
columns are written through the owning modules' services (work, communication), and the computed
relationship profile through ``people``. Slice 1.8: priority v1 and the Today read model; Phase 3:
relationship profiles (3.4), reminders, notifications and Web Push (3.1). Deterministic only: this
module never imports ``intelligence`` (import-linter contract). Other modules import only from
this package root.
"""

from eca.attention import events as _events  # registers event types
from eca.attention import tasks as _tasks  # registers handlers
from eca.attention.events import REMINDER_DUE, ReminderDue
from eca.attention.priority import PriorityConfig, conversation_features, item_features, score
from eca.attention.profiles import ProfileInputs, compute_profile, refresh_profiles
from eca.attention.purge import purge_expired, purge_user
from eca.attention.push import SubscriptionView, subscribe, unsubscribe
from eca.attention.reminders import (
    NotificationView,
    ReminderView,
    dismiss,
    evaluate,
    get_reminder,
    list_notifications,
    list_reminders,
    mark_acted,
    mark_read,
    snooze,
)
from eca.attention.service import recompute_conversations, recompute_items, sweep_all, sweep_user
from eca.attention.tasks import PRIORITY_SWEEP_TASK, REMINDER_SWEEP_TASK, periodic_tasks, priority_config
from eca.attention.today import Today, build_today
from eca.attention.webpush import VapidKeys, WebPushSender, generate_keys

del _events, _tasks

__all__ = [
    "PRIORITY_SWEEP_TASK",
    "REMINDER_DUE",
    "REMINDER_SWEEP_TASK",
    "NotificationView",
    "PriorityConfig",
    "ProfileInputs",
    "ReminderDue",
    "ReminderView",
    "SubscriptionView",
    "Today",
    "VapidKeys",
    "WebPushSender",
    "build_today",
    "compute_profile",
    "conversation_features",
    "dismiss",
    "evaluate",
    "generate_keys",
    "get_reminder",
    "item_features",
    "list_notifications",
    "list_reminders",
    "mark_acted",
    "mark_read",
    "periodic_tasks",
    "priority_config",
    "purge_expired",
    "purge_user",
    "recompute_conversations",
    "recompute_items",
    "refresh_profiles",
    "score",
    "snooze",
    "subscribe",
    "sweep_all",
    "sweep_user",
    "unsubscribe",
]
