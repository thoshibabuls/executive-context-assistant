"""Meetings, participants, recordings and transcripts.

Owns (single writer, BACKEND_DESIGN.md §5.1): meetings, meeting_participants, recordings,
transcript_segments. Slice 1.6: meetings from calendar events. Phase 4: uploads (4.1), the media
pipeline and transcripts (4.2), speaker mapping and the meeting summary (4.3), prep storage (4.4).
Other modules import only from this package root.
"""

from eca.meetings import tasks as _tasks  # registers handlers
from eca.meetings.events import (
    MEETING_CHANGED,
    RECORDING_STAGE_DUE,
    RECORDING_UPLOADED,
    MeetingChanged,
    RecordingStageDue,
    RecordingUploaded,
)
from eca.meetings.recordings import (
    MAX_MEDIA_BYTES,
    MAX_TRANSCRIPT_BYTES,
    MeetingSuggestion,
    RecordingView,
    UploadInit,
    classify_upload,
    complete_upload,
    delete_user_objects,
    get_recording,
    init_upload,
    link_meeting,
    list_recordings_page,
    meeting_suggestions,
    recording_for_meeting,
    recordings_by_source,
    retry_recording,
)
from eca.meetings.service import (
    MeetingDetail,
    MeetingView,
    cancel_from_deleted_source,
    get_meeting_details,
    meeting_details_between,
    meetings_between,
    meetings_by_sources,
    purge_sources,
    purge_user,
    upcoming_attendee_ids,
    upsert_from_source,
)
from eca.meetings.tasks import MEDIA_SWEEP_TASK, periodic_tasks

del _tasks

__all__ = [
    "MAX_MEDIA_BYTES",
    "MAX_TRANSCRIPT_BYTES",
    "MEDIA_SWEEP_TASK",
    "MEETING_CHANGED",
    "RECORDING_STAGE_DUE",
    "RECORDING_UPLOADED",
    "MeetingChanged",
    "MeetingDetail",
    "MeetingSuggestion",
    "MeetingView",
    "RecordingStageDue",
    "RecordingUploaded",
    "RecordingView",
    "UploadInit",
    "cancel_from_deleted_source",
    "classify_upload",
    "complete_upload",
    "delete_user_objects",
    "get_meeting_details",
    "get_recording",
    "init_upload",
    "link_meeting",
    "list_recordings_page",
    "meeting_details_between",
    "meeting_suggestions",
    "meetings_between",
    "meetings_by_sources",
    "periodic_tasks",
    "purge_sources",
    "purge_user",
    "recording_for_meeting",
    "recordings_by_source",
    "retry_recording",
    "upcoming_attendee_ids",
    "upsert_from_source",
]
