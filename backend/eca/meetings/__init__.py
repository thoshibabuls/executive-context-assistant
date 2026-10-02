"""Meetings, participants, recordings and transcripts.

Owns (single writer, BACKEND_DESIGN.md §5.1): meetings, meeting_participants, recordings,
transcript_segments. Slice 1.6: meetings from calendar events. Recordings, transcripts and
meeting extraction arrive in Phase 4. Other modules import only from this package root.
"""

from eca.meetings import tasks as _tasks  # registers handlers
from eca.meetings.events import MEETING_CHANGED, MeetingChanged
from eca.meetings.service import (
    MeetingView,
    cancel_from_deleted_source,
    meetings_between,
    purge_sources,
    purge_user,
    upcoming_attendee_ids,
    upsert_from_source,
)

del _tasks

__all__ = [
    "MEETING_CHANGED",
    "MeetingChanged",
    "MeetingView",
    "cancel_from_deleted_source",
    "meetings_between",
    "purge_sources",
    "purge_user",
    "upcoming_attendee_ids",
    "upsert_from_source",
]
